"""Offline native-completion -> authorized channel return flow."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from porcelainberryme.contracts import InboundEvent
from porcelainberryme.harness_ananasme import AnanasMeOwnerHarness
from porcelainberryme.harness_reply import NativeHarnessReplyDispatcher
from porcelainberryme.ledger import Ledger
from porcelainberryme.service import Gateway


def event(channel="whatsapp", sender="verified-owner", text="Bitte antworte", external_id="external-1"):
    return InboundEvent(
        tenant_id="owner-tenant", channel=channel,
        external_id=external_id, sender=sender,
        recipient="owner-identity", text=text,
    )


class FakeResponse:
    status = 200
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *args): return None
    def read(self, limit=32768):
        import json
        return json.dumps(self.payload).encode("utf-8")


class FakeChannelTransport:
    def __init__(self):
        self.messages = []
    def send(self, message):
        self.messages.append(message)
        return "accepted-provider-receipt"


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.thread = "a"*32
        self.mid = "b"*32
        self.harness = AnanasMeOwnerHarness(
            token="x"*32,
            verify_owner=lambda e: e.sender == "verified-owner",
            resolve_thread=lambda e: self.thread,
        )
        self.transport = FakeChannelTransport()
        self.gateway = Gateway(
            Ledger(Path(self.tmp.name) / "messages.sqlite3"),
            transports={c: self.transport for c in ("email","whatsapp","sms")},
            authorize=lambda m: m.tenant_id == "owner-tenant",
        )
        # Simulate the REAL, durable AnanasMe admission for each source
        # event before allowing any reply to leave its original channel.
        for channel in ("email", "whatsapp", "sms"):
            self.gateway.ledger.ingest(event(channel))
            claimed = self.gateway.ledger.claim_pending(
                tenant_id="owner-tenant", channel=channel)
            self.assertTrue(self.gateway.ledger.acknowledge_claim(
                claimed, state="HANDLED", receipt=self.mid))
        self.dispatcher = NativeHarnessReplyDispatcher(
            harness=self.harness, gateway=self.gateway,
            channel_sender=lambda e: "owner-identity",
            authorize_return=lambda e: e.sender == "verified-owner",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def response(self, state, reply=None, *, thread=None):
        return FakeResponse({
            "schema":"ananasme.assistant-result.v1",
            "message_id":self.mid,
            "thread_id":thread or self.thread,
            "state":state,
            "deliverable":state=="DONE",
            "reply":reply,
        })

    def test_no_send_before_authoritative_completion(self):
        for state in ("QUEUED","WORKING","HANDED_OFF","UNKNOWN","FAILED","REVIEW_UNVERIFIED"):
            with patch("porcelainberryme.harness_ananasme.urlopen",
                       return_value=self.response(state)):
                result = self.dispatcher.dispatch(event(), self.mid)
                self.assertEqual(result.state, "AWAITING_NATIVE_RESULT")
        self.assertEqual(self.transport.messages, [])

    def test_done_generates_once_to_original_verified_channel(self):
        for c in ("whatsapp","email","sms"):
            with patch("porcelainberryme.harness_ananasme.urlopen",
                       return_value=self.response("DONE","Native Aurora Antwort")):
                result = self.dispatcher.dispatch(event(c),self.mid)
                replay = self.dispatcher.dispatch(event(c),self.mid)
                self.assertEqual(result.state,"ACCEPTED")
                self.assertEqual(replay.state,"ACCEPTED")
        self.assertEqual(len(self.transport.messages),3)
        for msg in self.transport.messages:
            self.assertEqual(msg.text,"Native Aurora Antwort")
            self.assertEqual(msg.sender,"owner-identity")
            self.assertEqual(msg.recipient,"verified-owner")
        self.assertEqual(len({m.idempotency_key for m in self.transport.messages}),3)

    def test_owner_spoof_must_never_fetch_or_send(self):
        with patch("porcelainberryme.harness_ananasme.urlopen") as network:
            with self.assertRaises(PermissionError):
                self.dispatcher.dispatch(event(sender="customer"),self.mid)
            network.assert_not_called()
        self.assertEqual(self.transport.messages,[])

    def test_cross_thread_harness_result_rejected(self):
        with patch("porcelainberryme.harness_ananasme.urlopen",
                   return_value=self.response("DONE","Sensitive",thread="c"*32)):
            with self.assertRaisesRegex(RuntimeError,"identity_mismatch"):
                self.dispatcher.dispatch(event(),self.mid)
        self.assertEqual(self.transport.messages,[])

    def test_no_fake_empty_completed_reply(self):
        with patch("porcelainberryme.harness_ananasme.urlopen",
                   return_value=self.response("DONE","")):
            with self.assertRaisesRegex(RuntimeError,"final_reply_invalid"):
                self.dispatcher.dispatch(event(),self.mid)
        self.assertEqual(self.transport.messages,[])

    def test_same_thread_unrelated_assistant_cannot_be_resent(self):
        with patch("porcelainberryme.harness_ananasme.urlopen") as network:
            with self.assertRaisesRegex(PermissionError, "not_bound_to_inbound_event"):
                self.dispatcher.dispatch(event(), "c" * 32)
            network.assert_not_called()
        self.assertEqual(self.transport.messages, [])

    def test_identical_source_id_with_modified_payload_cannot_redirect_reply(self):
        with patch("porcelainberryme.harness_ananasme.urlopen") as network:
            with self.assertRaisesRegex(PermissionError, "not_bound_to_inbound_event"):
                self.dispatcher.dispatch(event(text="Neuer Auftrag"), self.mid)
            with self.assertRaisesRegex(PermissionError, "not_bound_to_inbound_event"):
                self.dispatcher.dispatch(event(external_id="other-id"), self.mid)
            network.assert_not_called()
        self.assertEqual(self.transport.messages, [])

    def test_processing_without_native_admission_is_not_deliverable(self):
        received = event()
        with self.gateway.ledger._db() as db:
            db.execute(
                "UPDATE inbound SET state='UNKNOWN',processing_receipt=NULL "
                "WHERE tenant_id=? AND channel=? AND external_id=?",
                (received.tenant_id, received.channel, received.external_id))
        with patch("porcelainberryme.harness_ananasme.urlopen") as network:
            with self.assertRaisesRegex(PermissionError, "not_bound_to_inbound_event"):
                self.dispatcher.dispatch(received, self.mid)
            network.assert_not_called()

    def test_phone_audio_not_dispatched_as_sms(self):
        with self.assertRaisesRegex(ValueError,"phone_reply_requires_sip_audio"):
            self.dispatcher.dispatch(event("phone"),self.mid)


if __name__ == "__main__":
    unittest.main()
