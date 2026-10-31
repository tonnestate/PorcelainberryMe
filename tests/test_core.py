import tempfile
from pathlib import Path
import unittest

from porcelainberryme import InboundEvent, OutboundMessage, Ledger, IdempotencyConflict
from porcelainberryme.service import DeliveryRejected, Gateway


def inbound(**changes):
    d = {"tenant_id": "tenant-a", "channel": "email", "external_id": "<id-1>",
         "sender": "a@example.org", "recipient": "b@example.org", "text": "Hallo"}
    d.update(changes)
    return InboundEvent(**d)


def outbound(**changes):
    d = {"tenant_id": "tenant-a", "idempotency_key": "action-1",
         "channel": "email", "sender": "a@example.org",
         "recipient": "b@example.org", "text": "Antwort"}
    d.update(changes)
    return OutboundMessage(**d)


class FakeTransport:
    def __init__(self, outcome="ok"):
        self.calls = 0
        self.outcome = outcome

    def send(self, message):
        self.calls += 1
        if self.outcome == "uncertain":
            raise TimeoutError("provider acknowledgement lost")
        if self.outcome == "refused":
            raise DeliveryRejected("definite refusal")
        return "provider-123"


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.temp.name) / "ledger.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_inbound_dedupe_and_identity_scope(self):
        self.assertTrue(self.ledger.ingest(inbound())["new"])
        self.assertFalse(self.ledger.ingest(inbound())["new"])
        self.assertTrue(self.ledger.ingest(inbound(tenant_id="tenant-b"))["new"])
        self.assertTrue(self.ledger.ingest(inbound(channel="sms"))["new"])
        with self.assertRaises(IdempotencyConflict):
            self.ledger.ingest(inbound(text="changed"))
        self.assertEqual(len(self.ledger.pending(tenant_id="tenant-a")), 3)
        self.assertTrue(self.ledger.mark_inbound(inbound(), "HANDLED"))
        self.assertFalse(self.ledger.mark_inbound(inbound(), "HANDLED"))

    def test_send_is_denied_without_explicit_policy(self):
        trans = FakeTransport()
        gw = Gateway(self.ledger, transports={"email": trans})
        with self.assertRaises(PermissionError):
            gw.send(outbound())
        self.assertEqual(trans.calls, 0)
        self.assertIsNone(self.ledger.outbound_status("tenant-a", "action-1"))

    def test_success_at_most_one_dispatch(self):
        trans = FakeTransport()
        gw = Gateway(self.ledger, transports={"email": trans}, authorize=lambda m: True)
        self.assertEqual(gw.send(outbound()).state, "ACCEPTED")
        self.assertEqual(gw.send(outbound()).state, "ACCEPTED")
        self.assertEqual(trans.calls, 1)
        with self.assertRaises(IdempotencyConflict):
            gw.send(outbound(text="different"))

    def test_network_unknown_is_not_resent(self):
        trans = FakeTransport("uncertain")
        gw = Gateway(self.ledger, transports={"email": trans}, authorize=lambda m: True)
        self.assertEqual(gw.send(outbound()).state, "UNKNOWN")
        self.assertEqual(gw.send(outbound()).state, "UNKNOWN")
        self.assertEqual(trans.calls, 1)

    def test_definitive_provider_rejection(self):
        trans = FakeTransport("refused")
        gw = Gateway(self.ledger, transports={"email": trans}, authorize=lambda m: True)
        self.assertEqual(gw.send(outbound()).state, "FAILED")
        self.assertEqual(gw.send(outbound()).state, "FAILED")
        self.assertEqual(trans.calls, 1)

    def test_crash_interrupted_claim_requires_reconciliation(self):
        msg = outbound()
        self.ledger.reserve(msg)
        self.assertTrue(self.ledger.claim(msg))
        self.assertEqual(self.ledger.recover_interrupted(), 1)
        self.assertEqual(self.ledger.outbound_status("tenant-a", "action-1")["state"], "UNKNOWN")
        self.assertFalse(self.ledger.claim(msg))

    def test_contract_refuses_empty_text(self):
        with self.assertRaises(ValueError):
            inbound(text="")
        with self.assertRaises(ValueError):
            outbound(text="")
        with self.assertRaises(ValueError):
            inbound(channel="unrecognized")


if __name__ == "__main__":
    unittest.main()
