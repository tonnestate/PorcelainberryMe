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
from .whatsapp_mirror import OwnerWhatsAppMirror

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


def handler_for(ledger: Ledger, *, secret: str,
                mirror: OwnerWhatsAppMirror | None = None,
                mirror_secret: str = ""):
    if not isinstance(secret, str) or len(secret) < 32:
        raise ValueError("signing_secret_too_short")
    if mirror is not None and (
            not isinstance(mirror_secret, str) or len(mirror_secret) < 32
            or mirror_secret == secret):
        raise ValueError("whatsapp_mirror_distinct_signing_secret_required")

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
            elif self.path == "/v1/owner-whatsapp-mirror/readyz":
                if mirror is None:
                    self._reply(404, {"error": "whatsapp_mirror_disabled"})
                elif mirror.ready():
                    self._reply(200, {"status": "ready",
                                      "scope": "owner_whatsapp_mirror"})
                else:
                    self._reply(503, {"status": "not_ready"})
            else:
                self._reply(404, {"error": "not_found"})

        def do_POST(self):
            is_mirror = self.path == "/v1/owner-whatsapp-mirror"
            if self.path != "/v1/inbound" and not (is_mirror and mirror is not None):
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
            target_secret = mirror_secret if is_mirror else secret
            if not authenticated(target_secret, timestamp, supplied, payload):
                self._reply(401, {"error": "invalid_signature"})
                return
            try:
                event = InboundEvent.from_dict(json.loads(payload))
                outcome = mirror.ingest(event) if is_mirror else ledger.ingest(event)
            except PermissionError:
                self._reply(403, {"error": "whatsapp_owner_scope_rejected"})
                return
            except OverflowError:
                self._reply(507, {"error": "inbound_capacity_exceeded"})
                return
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
    mirror = None
    mirror_secret = ""
    enabled = os.environ.get("PB_WHATSAPP_MIRROR_ENABLED", "0")
    if enabled not in ("0", "1"):
        raise SystemExit("Invalid WhatsApp mirror enable flag")
    if enabled == "1":
        mirror_secret = os.environ.get("PB_WHATSAPP_MIRROR_SIGNING_SECRET", "")
        mirror = OwnerWhatsAppMirror(
            path,
            tenant=os.environ.get("PB_WHATSAPP_MIRROR_TENANT", ""),
            owner_destination=os.environ.get("PB_WHATSAPP_MIRROR_DESTINATION", ""),
        )
    # The same private loopback service handles signed observer events.
    # Mirrored WhatsApp messages never enter the actionable inbound queue.
    server = ThreadingHTTPServer(
        ("127.0.0.1", 8873),
        handler_for(
            ledger, secret=secret, mirror=mirror, mirror_secret=mirror_secret,
        ),
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
