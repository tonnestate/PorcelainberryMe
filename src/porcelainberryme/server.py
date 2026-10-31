"""Loopback-only signed inbound HTTP adapter for existing trusted connectors.

Never exposes an agent, SMTP sender, phone caller, or model provider.
Each accepted event is durably queued; the existing FruitStack worker may
read pending events and acknowledge them through the in-process Ledger API.
"""
from __future__ import annotations

import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import time

from .contracts import InboundEvent
from .ledger import IdempotencyConflict, Ledger

MAX_BODY = 65536
SKEW_SECONDS = 300


def signature(secret: str, timestamp: str, payload: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"),
                      timestamp.encode("ascii") + b"." + payload,
                      hashlib.sha256).hexdigest()
    return "sha256=" + digest


def authenticated(secret: str, timestamp: str, supplied: str, payload: bytes,
                  *, now: int | None = None) -> bool:
    try:
        valid_timestamp = timestamp.isascii() and timestamp.isdigit()
        if not valid_timestamp:
            return False
        epoch = int(timestamp)
        if abs((int(time.time()) if now is None else now) - epoch) > SKEW_SECONDS:
            return False
        return hmac.compare_digest(signature(secret, timestamp, payload), supplied)
    except (ValueError, OverflowError, UnicodeEncodeError):
        return False


def handler_for(ledger: Ledger, *, secret: str):
    if not isinstance(secret, str) or len(secret) < 32:
        raise ValueError("signing_secret_too_short")

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, data: dict):
            encoded = json.dumps(data, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            if self.path == "/healthz":
                self._reply(200, {"status": "up", "mode": "inbound_only"})
            else:
                self._reply(404, {"error": "not_found"})

        def do_POST(self):
            if self.path != "/v1/inbound":
                self._reply(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
                if not 0 < length <= MAX_BODY:
                    raise ValueError("invalid_length")
                payload = self.rfile.read(length)
            except (TypeError, ValueError):
                self._reply(413, {"error": "invalid_size"})
                return
            timestamp = self.headers.get("X-PB-Timestamp", "")
            supplied = self.headers.get("X-PB-Signature", "")
            if not authenticated(secret, timestamp, supplied, payload):
                self._reply(401, {"error": "invalid_signature"})
                return
            try:
                event = InboundEvent.from_dict(json.loads(payload))
                outcome = ledger.ingest(event)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                status = 409 if isinstance(exc, IdempotencyConflict) else 400
                self._reply(status, {"error": str(exc)})
                return
            self._reply(202 if outcome["new"] else 200,
                        {"accepted": True, "duplicate": not outcome["new"],
                         "state": outcome["state"]})

        def log_message(self, format, *args):
            # Avoid printing payloads, phone numbers, and tokens.
            pass

    return Handler


def main():
    secret = os.environ.get("PORCELAINBERRY_SIGNING_SECRET", "")
    path = os.environ.get("PORCELAINBERRY_DB", "")
    if len(secret) < 32 or not path or not Path(path).is_absolute():
        raise SystemExit("Configure a long signing secret and absolute DB path")
    ledger = Ledger(path)
    # Inbound-only processes must not modify another sender process state.
    server = ThreadingHTTPServer(("127.0.0.1", 8873), handler_for(ledger, secret=secret))
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
