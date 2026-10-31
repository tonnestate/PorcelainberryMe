"""Offline recovery: one owner event -> one native reply -> one channel send."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from porcelainberryme.contracts import InboundEvent
from porcelainberryme.harness_ananasme import AnanasMeOwnerHarness, HarnessAdmission
from porcelainberryme.harness_reply import NativeHarnessReplyDispatcher
from porcelainberryme.harness_pipeline import HarnessChannelPipeline
from porcelainberryme.ledger import Ledger
from porcelainberryme.service import Gateway


def message(sender="trusted-owner", event_id="provider-1"):
    return InboundEvent(
        tenant_id="owner-tenant", channel="whatsapp", external_id=event_id,
        sender=sender, recipient="my-account", text="Bitte prüfen",
    )


class MemoryTransport:
    def __init__(self):
        self.sent = []
    def send(self, payload):
        self.sent.append(payload)
        return "provider-accepted-1"


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dbpath = Path(self.temp.name) / "state.sqlite3"
        self.mid = "a" * 32
        self.transport = MemoryTransport()
        self.owner_policy = lambda event: event.sender == "trusted-owner"
        self.harness = AnanasMeOwnerHarness(
            token="k"*32,
            verify_owner=self.owner_policy,
            resolve_thread=lambda event: "b"*32,
        )

    def tearDown(self):
        self.temp.cleanup()

    def pipeline(self):
        ledger = Ledger(self.dbpath)
        gateway = Gateway(
            ledger, transports={"whatsapp":self.transport},
            authorize=lambda outgoing: outgoing.tenant_id == "owner-tenant",
        )
        dispatcher = NativeHarnessReplyDispatcher(
            harness=self.harness, gateway=gateway,
            channel_sender=lambda ev: "my-account",
            authorize_return=self.owner_policy,
        )
        return HarnessChannelPipeline(
            ledger=ledger, tenant_id="owner-tenant", harness=self.harness,
            dispatcher=dispatcher, authorize_owner=self.owner_policy,
        )

    def test_restart_recovers_admitted_reply_without_restarting_ai(self):
        pump = self.pipeline()
        pump.ledger.ingest(message())
        with patch.object(self.harness, "submit_owner_message",
                          return_value=HarnessAdmission(self.mid, "QUEUED", False)) as native:
            accepted = pump.admit_next()
        self.assertEqual(accepted.state, "HANDLED")
        self.assertEqual(native.call_count, 1)
        self.assertEqual(accepted.receipt, self.mid)
        with patch.object(self.harness, "observe_native_reply",
                          return_value={"state":"HANDED_OFF","reply":None}):
            waiting = pump.deliver_ready()
        self.assertEqual(waiting.waiting, 1)
        self.assertEqual(self.transport.sent, [])
        # New process view: disk ledger survives restart.
        restarted = self.pipeline()
        with patch.object(self.harness, "observe_native_reply",
                          return_value={"state":"DONE","reply":"Die Antwort"}):
            first = restarted.deliver_ready()
            second = restarted.deliver_ready()
        self.assertEqual(first.delivered, 1)
        self.assertEqual(second.already_recorded, 1)
        self.assertEqual(len(self.transport.sent), 1)
        self.assertEqual(self.transport.sent[0].text, "Die Antwort")
        self.assertEqual(self.transport.sent[0].recipient, "trusted-owner")
        self.assertIsNone(restarted.admit_next())
        self.assertEqual(native.call_count, 1)

    def test_denied_sender_never_reaches_native_harness(self):
        pump = self.pipeline()
        pump.ledger.ingest(message(sender="customer"))
        with patch.object(self.harness, "submit_owner_message") as native:
            result = pump.admit_next()
            self.assertEqual(result.state, "UNKNOWN")
            self.assertEqual(pump.deliver_ready().reviewed, 0)
            native.assert_not_called()
        self.assertEqual(self.transport.sent, [])

    def test_reject_incompatible_ledger_and_harness(self):
        pump = self.pipeline()
        with tempfile.TemporaryDirectory() as second:
            wrong = Ledger(Path(second) / "other.sqlite3")
            with self.assertRaisesRegex(ValueError, "shared_delivery_ledger_required"):
                HarnessChannelPipeline(
                    ledger=wrong, tenant_id="owner-tenant", harness=self.harness,
                    dispatcher=pump.dispatcher, authorize_owner=self.owner_policy,
                )
        another = AnanasMeOwnerHarness(
            token="k"*32, verify_owner=self.owner_policy,
            resolve_thread=lambda ev: "b"*32,
        )
        with self.assertRaisesRegex(ValueError, "native_harness_identity_required"):
            HarnessChannelPipeline(
                ledger=pump.ledger, tenant_id="owner-tenant", harness=another,
                dispatcher=pump.dispatcher, authorize_owner=self.owner_policy,
            )

    def test_no_generic_crm_messages_in_harness_reply_queue(self):
        ledger = Ledger(self.dbpath)
        ledger.ingest(message())
        claim = ledger.claim_pending(tenant_id="owner-tenant")
        ledger.acknowledge_claim(claim, state="HANDLED", receipt=self.mid)
        self.assertEqual(ledger.owner_harness_admissions(tenant_id="owner-tenant"), [])
        with self.assertRaisesRegex(PermissionError, "not_bound_to_inbound_event"):
            ledger.assert_harness_admission(message(), self.mid)


if __name__ == "__main__":
    unittest.main()
