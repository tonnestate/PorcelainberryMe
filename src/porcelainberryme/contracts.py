"""Stable channel-neutral contracts. No provider SDK or LLM imports."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json

CHANNELS = frozenset({"email", "whatsapp", "sms", "phone"})
INBOUND_KINDS = frozenset({"text", "call_started", "call_ended", "call_failed"})
MAX_TEXT = 16000


def _required(name: str, value: object, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\r" in value or "\n" in value:
        raise ValueError(f"invalid_{name}")
    return value.strip()


def _channel(value: str) -> str:
    if value not in CHANNELS:
        raise ValueError("unsupported_channel")
    return value


def fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class InboundEvent:
    tenant_id: str
    channel: str
    external_id: str
    sender: str
    recipient: str
    text: str
    conversation_ref: str = ""
    kind: str = "text"

    def __post_init__(self):
        for key in ("tenant_id", "external_id", "sender", "recipient"):
            _required(key, getattr(self, key))
        _channel(self.channel)
        if self.kind not in INBOUND_KINDS:
            raise ValueError("invalid_kind")
        if not isinstance(self.text, str) or len(self.text) > MAX_TEXT:
            raise ValueError("invalid_text")
        if self.kind == "text" and not self.text.strip():
            raise ValueError("empty_text")
        if not isinstance(self.conversation_ref, str) or len(self.conversation_ref) > 512:
            raise ValueError("invalid_conversation_ref")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: object) -> "InboundEvent":
        if not isinstance(payload, dict):
            raise ValueError("invalid_event")
        try:
            return cls(**payload)
        except TypeError as exc:
            raise ValueError("invalid_event_schema") from exc


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    tenant_id: str
    idempotency_key: str
    channel: str
    sender: str
    recipient: str
    text: str

    def __post_init__(self):
        for key in ("tenant_id", "idempotency_key", "sender", "recipient"):
            _required(key, getattr(self, key))
        _channel(self.channel)
        if not isinstance(self.text, str) or not self.text.strip() or len(self.text) > MAX_TEXT:
            raise ValueError("invalid_text")

    def to_dict(self) -> dict:
        return asdict(self)
