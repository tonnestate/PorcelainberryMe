"""Opt-in official Meta WhatsApp Business text sender.

Inbound WhatsApp remains with the existing TonnEstate/Aurora connector until
its signed webhook contract is audited. No unofficial account bridge here.
"""
from __future__ import annotations

import json
import re
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .contracts import OutboundMessage
from .service import DeliveryRejected

_E164 = re.compile(r"^\+?[1-9][0-9]{6,14}$")
_API_VERSION = re.compile(r"^v[0-9]{1,3}\.[0-9]{1,2}$")


class WhatsAppCloudTransport:
    def __init__(self, *, phone_number_id: str, access_token: str, api_version: str):
        if not phone_number_id.isdigit() or not access_token or not _API_VERSION.fullmatch(api_version):
            raise ValueError("whatsapp_configuration_incomplete")
        self.phone_number_id = phone_number_id
        self.access_token = access_token
        self.api_version = api_version

    def send(self, message: OutboundMessage) -> str:
        if message.channel != "whatsapp" or message.sender != self.phone_number_id:
            raise ValueError("whatsapp_sender_mismatch")
        if not _E164.fullmatch(message.recipient):
            raise ValueError("invalid_whatsapp_recipient")
        endpoint = ("https://graph.facebook.com/" + self.api_version + "/" +
                    self.phone_number_id + "/messages")
        payload = {
            "messaging_product": "whatsapp", "recipient_type": "individual",
            "to": message.recipient.removeprefix("+"), "type": "text",
            "text": {"preview_url": False, "body": message.text},
        }
        request = Request(
            endpoint, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + self.access_token,
                     "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=15) as response:
                result = json.loads(response.read(65536))
        except HTTPError as exc:
            # Definitive 4xx rejection; timeouts/5xx may be ambiguous.
            if 400 <= exc.code < 500 and exc.code != 429:
                raise DeliveryRejected("whatsapp_provider_refused") from exc
            raise
        messages = result.get("messages", []) if isinstance(result, dict) else []
        ref = messages[0].get("id") if messages and isinstance(messages[0], dict) else None
        if not isinstance(ref, str) or not ref:
            raise RuntimeError("whatsapp_receipt_missing")
        return ref
