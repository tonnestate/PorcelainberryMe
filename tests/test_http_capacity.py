"""Live call-control remains responsive under slow or excessive connections."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from porcelainberryme.ip_calls import BoundedCallHTTPServer


class HTTPIngressCapacityTests(unittest.TestCase):
    def test_loopback_only_and_bounded_limits(self):
        class Empty(BaseHTTPRequestHandler):
            pass
        for settings in ({"max_connections": 0},
                         {"max_connections": 257},
                         {"request_timeout": 0},
                         {"request_timeout": 35}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                BoundedCallHTTPServer(("127.0.0.1", 0), Empty, **settings)
        with self.assertRaisesRegex(ValueError, "loopback"):
            BoundedCallHTTPServer(("0.0.0.0", 0), Empty)

    def test_slow_request_cannot_exhaust_process_or_block_bounded_ingress(self):
        entered = threading.Event()
        release = threading.Event()
        class Slow(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/hold":
                    entered.set()
                    release.wait(3)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *_):
                pass

        server = BoundedCallHTTPServer(
            ("127.0.0.1", 0), Slow,
            max_connections=1, request_timeout=3,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        response = []
        worker = threading.Thread(
            target=lambda: response.append(urlopen(base + "/hold", timeout=4).read()),
            daemon=True,
        )
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(HTTPError) as busy:
                urlopen(base + "/more", timeout=2)
            self.assertEqual(busy.exception.code, 503)
            release.set()
            worker.join(4)
            self.assertEqual(response, [b"ok"])
            # First handler releases the semaphore when it finishes.
            self.assertEqual(urlopen(base + "/ok", timeout=2).read(), b"ok")
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
