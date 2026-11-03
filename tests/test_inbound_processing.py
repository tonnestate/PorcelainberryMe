import tempfile
from pathlib import Path
import threading
import unittest

from porcelainberryme.contracts import InboundEvent
from porcelainberryme.ledger import Ledger
from porcelainberryme.inbound import InboundProcessor


def event(i="msg-1", tenant="tenant-a"):
    return InboundEvent(
        tenant_id=tenant, channel="whatsapp", external_id=i,
        sender="person", recipient="owner", text="test",
    )


class InboundProcessingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "shared.sqlite3"
        self.db = Ledger(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_tenant_scoped_claim_and_ack(self):
        self.db.ingest(event())
        self.db.ingest(event("msg-2", "tenant-b"))
        one = self.db.claim_pending(tenant_id="tenant-a")
        self.assertIsNotNone(one)
        self.assertEqual(one.event.external_id, "msg-1")
        self.assertIsNone(self.db.claim_pending(tenant_id="tenant-a"))
        self.assertEqual(self.db.pending(tenant_id="tenant-a"), [])
        self.assertEqual(len(self.db.pending(tenant_id="tenant-b")), 1)
        self.assertTrue(self.db.acknowledge_claim(one, state="HANDLED", receipt="crm-123"))
        self.assertFalse(self.db.acknowledge_claim(one, state="HANDLED", receipt="crm-123"))
        self.assertEqual(self.db.inbound_status(
            tenant_id="tenant-a", channel="whatsapp", external_id="msg-1"
        ), {"state": "HANDLED", "processing_receipt": "crm-123"})

    def test_no_unscoped_read_or_claim(self):
        self.db.ingest(event())
        with self.assertRaises(TypeError):
            self.db.pending()
        with self.assertRaises(TypeError):
            self.db.claim_pending()
        with self.assertRaises(ValueError):
            self.db.pending(tenant_id="")
        with self.assertRaises(ValueError):
            self.db.claim_pending(tenant_id="")

    def test_atomic_claim_across_two_database_instances(self):
        for i in range(30):
            self.db.ingest(event(f"msg-{i}"))
        other = Ledger(self.path)
        found = []
        lock = threading.Lock()
        def worker(db):
            while True:
                claim = db.claim_pending(tenant_id="tenant-a")
                if claim is None:
                    return
                with lock:
                    found.append(claim.event.external_id)
                self.assertTrue(db.acknowledge_claim(claim, state="HANDLED", receipt=claim.token))
        a = threading.Thread(target=worker, args=(self.db,))
        b = threading.Thread(target=worker, args=(other,))
        a.start()
        b.start()
        a.join(timeout=10)
        b.join(timeout=10)
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(len(found), 30)
        self.assertEqual(len(set(found)), 30)

    def test_owner_receipt_required(self):
        self.db.ingest(event())
        claim = self.db.claim_pending(tenant_id="tenant-a")
        with self.assertRaises(ValueError):
            self.db.acknowledge_claim(claim, state="HANDLED")
        self.assertTrue(self.db.acknowledge_claim(claim, state="UNKNOWN"))

    def test_after_crash_unknown_does_not_replay(self):
        self.db.ingest(event())
        self.db.claim_pending(tenant_id="tenant-a")
        self.assertEqual(Ledger(self.path).recover_interrupted_inbound(tenant_id="tenant-a"), 1)
        self.assertIsNone(self.db.claim_pending(tenant_id="tenant-a"))
        self.assertEqual(self.db.inbound_status(
            tenant_id="tenant-a", channel="whatsapp", external_id="msg-1")["state"], "UNKNOWN")

    def test_inbound_processor_requires_real_handler_and_policy(self):
        self.db.ingest(event())
        with self.assertRaises(ValueError):
            InboundProcessor(self.db, tenant_id="tenant-a", handler=None, authorize=None)
        calls = []
        consumer = InboundProcessor(
            self.db, tenant_id="tenant-a", authorize=lambda e: True,
            handler=lambda e, token: calls.append(token) or "fruit-receipt-1",
        )
        result = consumer.run_once()
        self.assertEqual(result.state, "HANDLED")
        self.assertEqual(result.receipt, "fruit-receipt-1")
        self.assertEqual(len(calls), 1)
        self.assertIsNone(consumer.run_once())

    def test_denial_avoids_handler_and_requires_reconciliation(self):
        self.db.ingest(event())
        called = []
        consumer = InboundProcessor(
            self.db, tenant_id="tenant-a",
            authorize=lambda e: False,
            handler=lambda e, token: called.append(e) or "never",
        )
        self.assertEqual(consumer.run_once().state, "UNKNOWN")
        self.assertEqual(called, [])
        self.assertIsNone(consumer.run_once())


if __name__ == "__main__":
    unittest.main()
