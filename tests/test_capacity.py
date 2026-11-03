"""Small-disk safety: deterministic refusal, no automatic deletion or data loss."""
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from porcelainberryme.contracts import InboundEvent
from porcelainberryme.intake_client import LocalSignedIntake
from porcelainberryme.ledger import Ledger
from porcelainberryme.server import handler_for


def event(i):
    return InboundEvent(
        tenant_id="tenant", channel="sms", external_id=f"id-{i}",
        sender="sender", recipient="owner", text="test",
    )


class BoundedLedgerTests(unittest.TestCase):
    def test_duplicate_is_safe_even_at_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "events.sqlite3", max_inbound_rows=1)
            self.assertTrue(ledger.ingest(event(1))["new"])
            self.assertFalse(ledger.ingest(event(1))["new"])
            with self.assertRaisesRegex(OverflowError, "inbound_capacity_exceeded"):
                ledger.ingest(event(2))
            self.assertEqual(len(ledger.pending(tenant_id="tenant")), 1)
            self.assertEqual(ledger.pending(tenant_id="tenant")[0].external_id, "id-1")

    def test_capacity_rejects_new_ids_over_real_loopback_http(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "events.sqlite3", max_inbound_rows=1)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), handler_for(ledger, secret="s" * 48),
            )
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                client = LocalSignedIntake(secret="s" * 48, port=server.server_port)
                self.assertTrue(client.submit(event(1))["new"])
                self.assertFalse(client.submit(event(1))["new"])
                with self.assertRaisesRegex(OverflowError, "inbound_capacity_exceeded"):
                    client.submit(event(2))
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)

    def test_invalid_capacity_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for n in (0, -1, 100001):
                with self.assertRaises(ValueError):
                    Ledger(Path(directory) / "db.sqlite3", max_inbound_rows=n)


if __name__ == "__main__":
    unittest.main()
