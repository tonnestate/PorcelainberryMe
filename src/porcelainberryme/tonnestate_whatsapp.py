"""Existing TonnEstate/Aurora owner WhatsApp integration — no parallel client.

Calls TonnEstate's existing, bearer-authenticated n8n-only API, which checks
owner identity and delegates to the already running WhatsApp process.
No direct access to the raw WhatsApp bridge or another live message consumer.
"""
from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .contracts import InboundEvent, OutboundMessage
from .service import DeliveryRejected

_ENDPOINT = "http://127.0.0.1:5001/api/internal/v1/aurora/send"


class TonnEstateWhatsAppTransport:
    """Opt-in owner-only send through the pre-existing authenticated CRM API."""

    def __init__(self, *, owner_sender: str, api_token: str):
        if not isinstance(owner_sender, str) or not owner_sender.strip():
            raise ValueError("owner_sender_required")
        if not isinstance(api_token, str) or not api_token.strip():
            raise ValueError("tonnestate_api_token_required")
        self.owner_sender = owner_sender
        self._token = api_token

    def send(self, message: OutboundMessage) -> str:
        if message.channel != "whatsapp" or message.sender != self.owner_sender:
            raise ValueError("wrong_whatsapp_sender")
        if (not message.recipient.endswith("@c.us")
                or message.recipient.endswith("@g.us") or len(message.text) > 3000
                or len(message.idempotency_key) > 128):
            raise ValueError("invalid_existing_whatsapp_bridge_message")
        # Do not submit tenant_id: this owner-scoped TonnEstate endpoint
        # explicitly rejects tenant headers/body, resolves owner server-side.
        payload = {"recipient_ref": message.recipient, "text": message.text,
                   "allow_self": False}
        request = Request(
            _ENDPOINT, method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + self._token,
                     "X-Idempotency-Key": message.idempotency_key,
                     "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=20) as response:
                result = json.loads(response.read(65536))
        except HTTPError as exc:
            # TonnEstate currently also wraps a bridge timeout as HTTP 400
            # ("aurora_send_unavailable"). A 400 therefore does NOT prove
            # that the downstream WhatsApp provider rejected the message.
            # Treat it as ambiguous, never as safe-to-retry FAILED.
            if exc.code == 400:
                raise RuntimeError("tonnestate_whatsapp_ambiguous_400") from exc
            if 400 <= exc.code < 500 and exc.code != 429:
                raise DeliveryRejected("tonnestate_whatsapp_api_refused") from exc
            raise
        # Existing Flask API returns {success, data, meta}.
        data = result.get("data") if isinstance(result, dict) and result.get("success") is True else None
        if not isinstance(data, dict) or data.get("status") != "sent":
            raise RuntimeError("tonnestate_whatsapp_unconfirmed")
        receipt = data.get("sent_message_id")
        if not isinstance(receipt, str) or not receipt:
            raise RuntimeError("tonnestate_whatsapp_receipt_missing")
        return receipt


def from_existing_whatsapp_event(
    payload: dict, *, tenant_id: str, owner_destination: str
) -> InboundEvent:
    """Normalize ALREADY authenticated n8n/Aurora inbound payload.

    This pure function does not register a webhook, run an agent or create a
    parallel consumer. Never trust tenant_id or owner_destination from
    unverified external inputs.
    """
    if not isinstance(payload, dict):
        raise ValueError("invalid_whatsapp_payload")
    # Match the established TonnEstate/Aurora contract: only explicit
    # boolean True denotes a message from the owner. An omitted flag is
    # an incoming event, not a reason to lose the authenticated message.
    if payload.get("from_me") is True:
        raise ValueError("outgoing_whatsapp_message")
    sender = payload.get("sender", "")
    if (not isinstance(sender, str) or not sender.endswith("@c.us")
            or sender.endswith("@g.us") or "\r" in sender or "\n" in sender):
        raise ValueError("invalid_direct_whatsapp_sender")
    return InboundEvent(
        tenant_id=tenant_id, channel="whatsapp",
        external_id=payload.get("message_id", ""),
        sender=payload.get("sender", ""),
        recipient=owner_destination,
        text=payload.get("message", ""),
        conversation_ref=payload.get("conversation_id", ""),
    )
