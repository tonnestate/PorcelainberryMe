"""Actual loopback overload and recovery contract for signed HTTP ingress."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from porcelainberryme.server import BoundedInboundHTTPServer


class WaitingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.entered.set()
        self.server.release.wait(timeout=3)
        response = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *_args):
        pass


class BoundedInboundHTTPTests(unittest.TestCase):
    def test_loopback_only_and_connection_and_timeout_limits(self):
        for address, limit, timeout in (
            (("0.0.0.0", 0), 1, 1),
            (("127.0.0.1", 0), 0, 1),
            (("127.0.0.1", 0), 257, 1),
            (("127.0.0.1", 0), True, 1),
            (("127.0.0.1", 0), 1, 0),
            (("127.0.0.1", 0), 1, True),
            (("127.0.0.1", 0), 1, 31),
        ):
            with self.subTest(address=address, limit=limit, timeout=timeout):
                with self.assertRaises(ValueError):
                    BoundedInboundHTTPServer(
                        address, WaitingHandler,
                        max_connections=limit, request_timeout=timeout,
                    )

    def test_held_connection_fails_extra_request_with_503_then_recovers(self):
        server = BoundedInboundHTTPServer(
            ("127.0.0.1", 0), WaitingHandler,
            max_connections=1, request_timeout=2,
        )
        server.entered = threading.Event()
        server.release = threading.Event()
        loop = threading.Thread(target=server.serve_forever, daemon=True)
        loop.start()
        url = f"http://127.0.0.1:{server.server_port}/healthz"
        received = []
        error = []
        def occupy():
            try:
                with urlopen(url, timeout=4) as response:
                    received.append((response.status, response.read()))
            except Exception as exc:
                error.append(type(exc).__name__)
        occupier = threading.Thread(target=occupy, daemon=True)
        try:
            occupier.start()
            self.assertTrue(server.entered.wait(2))
            with self.assertRaises(HTTPError) as rejected:
                urlopen(url, timeout=2)
            self.assertEqual(rejected.exception.code, 503)
            self.assertEqual(
                rejected.exception.headers.get("Cache-Control"), "no-store",
            )
            self.assertEqual(received, [])
            server.release.set()
            occupier.join(timeout=4)
            self.assertFalse(occupier.is_alive())
            self.assertEqual(error, [])
            self.assertEqual(received, [(200, b"ok")])
            for attempt in range(20):
                try:
                    with urlopen(url, timeout=2) as recovered:
                        self.assertEqual(recovered.status, 200)
                        self.assertEqual(recovered.read(), b"ok")
                    break
                except HTTPError as exc:
                    if exc.code != 503 or attempt == 19:
                        raise
                    time.sleep(0.02)
            self.assertEqual(server._slots._value, 1)
        finally:
            server.release.set()
            server.shutdown()
            server.server_close()
            loop.join(timeout=2)
            occupier.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
