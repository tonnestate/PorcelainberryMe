"""Supervised, restart-safe integration with the *existing* Aurora harness.

No background service, model loading, self-authorization or parallel platform
consumer. Call from a trusted owner-scoped operator only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .contracts import InboundEvent
from .harness_ananasme import AnanasMeOwnerHarness
from .harness_reply import NativeHarnessReplyDispatcher
from .inbound import InboundProcessor, InboundProcessingResult
from .ledger import Ledger


@dataclass(frozen=True, slots=True)
class ChannelPollResult:
    reviewed: int
    delivered: int
    waiting: int
    blocked: int
    uncertain: int
    already_recorded: int


class HarnessChannelPipeline:
    """One guarded path: event -> native admission -> final reply -> channel."""

    def __init__(
        self, *, ledger: Ledger, tenant_id: str, harness: AnanasMeOwnerHarness,
        dispatcher: NativeHarnessReplyDispatcher,
        authorize_owner: Callable[[InboundEvent], bool],
    ):
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_scope_required")
        if not callable(authorize_owner):
            raise ValueError("owner_policy_required")
        if dispatcher.gateway.ledger.database != ledger.database:
            raise ValueError("shared_delivery_ledger_required")
        if dispatcher.harness is not harness:
            raise ValueError("native_harness_identity_required")
        self.ledger = ledger
        self.tenant_id = tenant_id
        self.dispatcher = dispatcher
        self.inbound = InboundProcessor(
            ledger, tenant_id=tenant_id,
            handler=harness.receipt_handler,
            authorize=authorize_owner,
            handler_kind="OWNER_HARNESS",
        )

    def admit_next(self, *, channel: str | None = None) -> InboundProcessingResult | None:
        """Supervised claim: only a verified owner event may reach AnanasMe."""
        return self.inbound.run_once(channel=channel)

    def deliver_ready(self, *, limit: int = 50) -> ChannelPollResult:
        """Check persisted native admissions and send only truly completed replies.

        All admission IDs remain durable after restart. Completed/failed/
        ambiguous outbound attempts are skipped, never automatically replayed.
        GETs to AnanasMe are read-only and safe to repeat while awaiting DONE.
        """
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("invalid_poll_limit")
        reviewed = delivered = waiting = blocked = uncertain = already_recorded = 0
        for event, message_id in self.ledger.owner_harness_admissions(
                tenant_id=self.tenant_id):
            key = self.dispatcher.return_key(event, message_id)
            prior = self.ledger.outbound_status(event.tenant_id, key)
            if prior and prior["state"] != "PENDING":
                already_recorded += 1
                continue
            if reviewed >= limit:
                break
            reviewed += 1
            try:
                result = self.dispatcher.dispatch(event, message_id)
            except (ValueError, PermissionError, RuntimeError, OSError, TimeoutError):
                # A failed read/permission check must never trigger a send.
                blocked += 1
                continue
            if result.state == "AWAITING_NATIVE_RESULT":
                waiting += 1
            elif result.state == "ACCEPTED":
                # Provider acceptance is not delivery to the recipient.
                delivered += 1
            elif result.state in ("UNKNOWN", "ATTEMPTING"):
                uncertain += 1
            else:
                blocked += 1
        return ChannelPollResult(
            reviewed=reviewed, delivered=delivered, waiting=waiting,
            blocked=blocked, uncertain=uncertain,
            already_recorded=already_recorded,
        )
