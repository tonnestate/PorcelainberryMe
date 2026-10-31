"""Real local HTTP roundtrip, no external carrier credentials or side effects."""
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from porcelainberryme.contracts import InboundEvent
from porcelainberryme.intake_client import LocalSignedIntake
from porcelainberryme.ledger import IdempotencyConflict, Ledger
from porcelainberryme.server import handler_for


class LocalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.temp.name) / "messages.sqlite3")
        self.secret = "a" * 48
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), handler_for(self.ledger, secret=self.secret)
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = LocalSignedIntake(secret=self.secret, port=self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    @staticmethod
    def event(text="Hallo"):
        return InboundEvent(
            tenant_id="tenant-a", channel="whatsapp",
            external_id="wamid-1", sender="4912345@c.us",
            recipient="owner", text=text,
        )

    def test_local_signed_roundtrip_dedup_and_conflict(self):
        accepted = self.client.submit(self.event())
        self.assertEqual(accepted, {"new": True, "state": "PENDING"})
        duplicate = self.client.submit(self.event())
        self.assertEqual(duplicate, {"new": False, "state": "PENDING"})
        self.assertEqual(len(self.ledger.pending(tenant_id="tenant-a")), 1)
        with self.assertRaises(IdempotencyConflict):
            self.client.submit(self.event(text="abweichend"))

    def test_wrong_signing_secret_is_rejected(self):
        wrong = LocalSignedIntake(secret="b" * 48, port=self.server.server_port)
        with self.assertRaises(PermissionError):
            wrong.submit(self.event())


if __name__ == "__main__":
    unittest.main()
