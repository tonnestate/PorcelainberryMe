"""Local, signed push into PorcelainberryMe from an existing trusted bridge.

The client does not attach to any messaging platform. A caller must have
already authenticated upstream WhatsApp/email/SMS data and assigned the real
tenant scope. Request repeats with the same external ID are deduplicated.
"""
from __future__ import annotations

import json
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .contracts import InboundEvent
from .ledger import IdempotencyConflict
from .server import signature


class LocalSignedIntake:
    def __init__(self, *, secret: str, port: int = 8873, timeout_seconds: int = 5):
        if not isinstance(secret, str) or len(secret) < 32:
            raise ValueError("signing_secret_too_short")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("invalid_port")
        if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 30:
            raise ValueError("invalid_timeout")
        self._secret = secret
        self._url = f"http://127.0.0.1:{port}/v1/inbound"
        self._timeout_seconds = timeout_seconds

    def submit(self, event: InboundEvent) -> dict:
        if not isinstance(event, InboundEvent):
            raise ValueError("invalid_event")
        body = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        timestamp = str(int(time.time()))
        request = Request(
            self._url, method="POST", data=body,
            headers={"Content-Type": "application/json",
                     "X-PB-Timestamp": timestamp,
                     "X-PB-Signature": signature(self._secret, timestamp, body)},
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                reply = json.loads(response.read(4096))
                if response.status not in (200, 202) or reply.get("accepted") is not True:
                    raise RuntimeError("intake_acknowledgement_invalid")
                return {"new": not reply.get("duplicate", False), "state": reply["state"]}
        except HTTPError as exc:
            if exc.code == 507:
                raise OverflowError("inbound_capacity_exceeded") from exc
            if exc.code == 409:
                raise IdempotencyConflict("inbound_id_reused_with_different_payload") from exc
            if exc.code == 401:
                raise PermissionError("intake_signature_rejected") from exc
            raise RuntimeError("intake_delivery_unconfirmed") from exc
