"""Liveness is not readiness; fail closed when call ledger is unavailable."""
from __future__ import annotations
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from porcelainberryme.ip_calls import CallRegistry, control_ready, make_handler


class CallControlReadinessTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db_file = Path(folder.name) / "calls.sqlite"
        self.store = CallRegistry(
            str(self.db_file),
            identity_secret="long-private-test-signing-secret-0000123",
        )

    def test_real_read_only_probe_rejects_missing_and_corrupt_file(self):
        self.assertTrue(control_ready(self.store))
        self.db_file.unlink()
        self.assertFalse(control_ready(self.store))
        self.assertFalse(self.db_file.exists())  # no silent file re-creation
        self.db_file.write_bytes(b"this is not sqlite")
        self.assertFalse(control_ready(self.store))
        self.assertEqual(self.db_file.read_bytes(), b"this is not sqlite")

    def test_real_http_readyz_differs_from_liveness(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:" + str(server.server_port)
        with urlopen(base + "/readyz", timeout=3) as response:
            self.assertEqual(response.status, 200)
            body = json.load(response)
            self.assertEqual(body["status"], "ready")
            self.assertEqual(body["scope"], "internet_calls_control_only")
            self.assertFalse(body["media_configured"])

        self.db_file.unlink()
        with urlopen(base + "/healthz", timeout=3) as response:
            self.assertEqual(json.load(response)["status"], "up")
        with self.assertRaises(HTTPError) as error:
            urlopen(base + "/readyz", timeout=3)
        self.assertEqual(error.exception.code, 503)
        self.assertEqual(json.load(error.exception)["status"], "unready")


if __name__ == "__main__":
    unittest.main()
