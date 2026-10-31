"""Fully offline HTTP intake and official WhatsApp request-shape tests."""
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from porcelainberryme.contracts import InboundEvent, OutboundMessage
from porcelainberryme.ledger import Ledger
from porcelainberryme.server import handler_for, signature
from porcelainberryme.service import Gateway
from porcelainberryme.whatsapp_cloud import WhatsAppCloudTransport


class HttpIngressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.temp.name) / "ledger.db")
        self.secret = "a" * 40
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), handler_for(self.ledger, secret=self.secret))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.address = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)
        self.temp.cleanup()

    def _post(self, payload, *, good_signature=True):
        encoded = json.dumps(payload).encode("utf-8")
        ts = "1793460000"
        sig = signature(self.secret, ts, encoded)
        if not good_signature:
            sig = "sha256=" + "0" * 64
        req = Request(self.address + "/v1/inbound", data=encoded, method="POST",
                      headers={"X-PB-Timestamp": ts, "X-PB-Signature": sig,
                               "Content-Type": "application/json"})
        with patch("porcelainberryme.server.time.time", return_value=1793460000):
            try:
                with urlopen(req, timeout=5) as response:
                    return response.status, json.loads(response.read())
            except HTTPError as error:
                return error.code, json.loads(error.read())

    def test_signed_ingest_duplicate_and_collision(self):
        item = InboundEvent(
            tenant_id="tenant", channel="whatsapp", external_id="wamid.100",
            sender="+49111111111", recipient="+49222222222", text="Hallo"
        ).to_dict()
        status, data = self._post(item)
        self.assertEqual(status, 202)
        self.assertTrue(data["accepted"])
        status, data = self._post(item)
        self.assertEqual(status, 200)
        self.assertTrue(data["duplicate"])
        status, _ = self._post({**item, "text": "changed"})
        self.assertEqual(status, 409)
        status, _ = self._post(item, good_signature=False)
        self.assertEqual(status, 401)
        self.assertEqual(len(self.ledger.pending()), 1)


class WhatsAppSenderTests(unittest.TestCase):
    def test_authorization_and_provider_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "db.sqlite3")
            adapter = WhatsAppCloudTransport(
                phone_number_id="123456", access_token="test-only",
                api_version="v20.0"
            )
            message = OutboundMessage(
                tenant_id="tenant", idempotency_key="request-1",
                channel="whatsapp", sender="123456", recipient="+491234567890",
                text="Einmalige Antwort"
            )
            response = MagicMock()
            response.read.return_value = b'{"messages":[{"id":"wamid.200"}]}'
            outer = MagicMock()
            outer.__enter__.return_value = response
            with patch("porcelainberryme.whatsapp_cloud.urlopen", return_value=outer) as mocked:
                gateway = Gateway(ledger, transports={"whatsapp": adapter},
                                  authorize=lambda m: True)
                outcome = gateway.send(message)
                self.assertEqual(outcome.state, "ACCEPTED")
                self.assertEqual(outcome.receipt, "wamid.200")
                self.assertEqual(gateway.send(message).state, "ACCEPTED")
                self.assertEqual(mocked.call_count, 1)
                request = mocked.call_args.args[0]
                self.assertIn(b'"messaging_product": "whatsapp"', request.data)
                self.assertEqual(request.get_method(), "POST")

    def test_reserved_not_yet_sent_can_be_claimed_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "db.sqlite3")
            message = OutboundMessage(
                tenant_id="tenant", idempotency_key="pending-send",
                channel="whatsapp", sender="123456", recipient="+491234567890",
                text="Test"
            )
            ledger.reserve(message)  # Crash before any provider attempt.
            class Transport:
                calls = 0
                def send(self, m):
                    self.calls += 1
                    return "accepted-1"
            transport = Transport()
            gateway = Gateway(ledger, transports={"whatsapp": transport},
                              authorize=lambda m: True)
            self.assertEqual(gateway.send(message).state, "ACCEPTED")
            self.assertEqual(transport.calls, 1)


if __name__ == "__main__":
    unittest.main()
