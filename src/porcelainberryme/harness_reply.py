"""Guarded bidirectional reply routing from AnanasMe to existing channels.

AnanasMe's exact completed native result is the only answer source. No
automatic sending on QUEUED, WORKING, HANDED_OFF, UNKNOWN or FAILED.
The existing Gateway still enforces channel policy and durable idempotency.
Phone media is not represented as a text/SMS-style outbound message.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Callable

from .contracts import InboundEvent, OutboundMessage
from .harness_ananasme import AnanasMeOwnerHarness
from .service import Gateway


@dataclass(frozen=True, slots=True)
class ReplyDispatchResult:
    state: str
    provider_receipt: str | None = None


class NativeHarnessReplyDispatcher:
    def __init__(
        self, *, harness: AnanasMeOwnerHarness, gateway: Gateway,
        channel_sender: Callable[[InboundEvent], str],
        authorize_return: Callable[[InboundEvent], bool],
    ):
        if not callable(channel_sender) or not callable(authorize_return):
            raise ValueError("return_channel_policy_required")
        self.harness = harness
        self.gateway = gateway
        self.channel_sender = channel_sender
        self.authorize_return = authorize_return

    @staticmethod
    def return_key(event: InboundEvent, message_id: str) -> str:
        key = json.dumps([
            "porcelainberryme-native-return", event.tenant_id, event.channel,
            event.external_id, message_id,
        ], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(key).hexdigest()

    def dispatch(self, event: InboundEvent, message_id: str) -> ReplyDispatchResult:
        if not isinstance(event, InboundEvent) or event.kind != "text":
            raise ValueError("only_text_replies_supported")
        if event.channel not in ("whatsapp", "email", "sms"):
            raise ValueError("phone_reply_requires_sip_audio")
        if self.authorize_return(event) is not True:
            raise PermissionError("return_channel_not_authorized")
        observed = self.harness.observe_native_reply(event, message_id)
        if observed["state"] != "DONE":
            return ReplyDispatchResult("AWAITING_NATIVE_RESULT")
        sender = self.channel_sender(event)
        message = OutboundMessage(
            tenant_id=event.tenant_id,
            channel=event.channel,
            idempotency_key=self.return_key(event, message_id),
            sender=sender,
            recipient=event.sender,
            text=observed["reply"],
        )
        receipt = self.gateway.send(message)
        return ReplyDispatchResult(receipt.state, receipt.receipt)
