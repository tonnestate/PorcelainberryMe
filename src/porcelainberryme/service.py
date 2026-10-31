"""Authorized outbound delivery. No agent runtime, implicit tool calls or retries."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from .contracts import OutboundMessage
from .ledger import Ledger


class Transport(Protocol):
    def send(self, message: OutboundMessage) -> str:
        """Return a non-empty provider acceptance/correlation reference."""


class DeliveryRejected(RuntimeError):
    """A definite provider refusal, before acceptance."""


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    state: str
    receipt: str | None


class Gateway:
    def __init__(self, ledger: Ledger, *, transports: dict[str, Transport] | None = None,
                 authorize: Callable[[OutboundMessage], bool] | None = None):
        self.ledger = ledger
        self.transports = transports or {}
        # Fail closed. External callers must use existing FruitStack/CRM policy.
        self.authorize = authorize or (lambda message: False)

    def send(self, message: OutboundMessage) -> DeliveryResult:
        if not self.authorize(message):
            raise PermissionError("outbound_not_authorized")
        transport = self.transports.get(message.channel)
        if transport is None:
            raise ValueError("channel_transport_not_configured")
        reservation = self.ledger.reserve(message)
        if not reservation["new"]:
            return DeliveryResult(reservation["state"], reservation["provider_receipt"])
        if not self.ledger.claim(message):
            current = self.ledger.outbound_status(message.tenant_id, message.idempotency_key)
            return DeliveryResult(current["state"], current["provider_receipt"])
        try:
            receipt = transport.send(message)
            if not isinstance(receipt, str) or not receipt.strip():
                raise RuntimeError("missing_provider_receipt")
        except DeliveryRejected:
            self.ledger.finish(message, state="FAILED")
            return DeliveryResult("FAILED", None)
        except Exception:
            # A network timeout might occur after provider acceptance.
            self.ledger.finish(message, state="UNKNOWN")
            return DeliveryResult("UNKNOWN", None)
        self.ledger.finish(message, state="ACCEPTED", receipt=receipt)
        return DeliveryResult("ACCEPTED", receipt)
