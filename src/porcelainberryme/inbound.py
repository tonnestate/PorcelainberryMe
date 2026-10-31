"""Explicit tenant-scoped inbound processing. No background agent or implicit retry."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .contracts import InboundEvent
from .ledger import Ledger


@dataclass(frozen=True, slots=True)
class InboundProcessingResult:
    state: str
    external_id: str
    receipt: str | None = None


class InboundProcessor:
    """One-at-a-time dispatcher into an EXISTING, injected authoritative owner.

    The handler must do its own permission checks and persist a real external
    receipt before returning. The claim token is passed as an idempotency
    reference for that handler. A failed/uncertain handler is NOT retried.
    """

    def __init__(self, ledger: Ledger, *, tenant_id: str,
                 handler: Callable[[InboundEvent, str], str],
                 authorize: Callable[[InboundEvent], bool]):
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_scope_required")
        if not callable(handler) or not callable(authorize):
            raise ValueError("explicit_handler_and_policy_required")
        self.ledger = ledger
        self.tenant_id = tenant_id
        self._handler = handler
        self._authorize = authorize

    def run_once(self, *, channel: str | None = None) -> InboundProcessingResult | None:
        claim = self.ledger.claim_pending(tenant_id=self.tenant_id, channel=channel)
        if claim is None:
            return None
        status = "UNKNOWN"
        receipt = None
        try:
            if not self._authorize(claim.event):
                raise PermissionError("inbound_not_authorized")
            receipt = self._handler(claim.event, claim.token)
            if not isinstance(receipt, str) or not receipt.strip() or len(receipt) > 512:
                receipt = None
                raise ValueError("authoritative_receipt_required")
            status = "HANDLED"
        except Exception:
            # Failure might follow a completed remote effect. Do not retry.
            receipt = None
        if not self.ledger.acknowledge_claim(claim, state=status, receipt=receipt):
            raise RuntimeError("claim_ownership_lost")
        return InboundProcessingResult(status, claim.event.external_id, receipt)
