"""Native AnanasMe reply -> authorized, resumable SIP speech turn.

Call from the existing verified phone/AVCoS harness. No SIP registration,
STT inference, new model, recording or background polling is performed here.
The outbound ledger prevents repeat speaking of an ambiguous attempt.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Callable

from .contracts import InboundEvent, OutboundMessage
from .harness_ananasme import AnanasMeOwnerHarness
from .ledger import Ledger
from .voice_stream import SipVoiceTurn

_TURN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


@dataclass(frozen=True, slots=True)
class PhoneReplyResult:
    state: str
    bytes_submitted: int = 0


def event_from_verified_sip_transcript(session, *, text: str, turn_id: str) -> InboundEvent:
    """Make the text event after an EXISTING trusted SIP+STT pipeline.

    The stable turn_id is supplied by the caller, not generated as an
    arbitrary new ID on each retry. Caller-ID still requires independent
    authentication and permission outside this pure converter.
    """
    if not isinstance(turn_id, str) or not _TURN.fullmatch(turn_id):
        raise ValueError("stable_sip_turn_id_required")
    identity = session.identity
    if identity.direction not in ("incoming", "outgoing"):
        raise ValueError("invalid_call_direction")
    return InboundEvent(
        tenant_id=identity.tenant_id,
        channel="phone",
        external_id=identity.call_id + ":" + turn_id,
        sender=identity.peer,
        recipient=identity.local_address,
        text=text,
        conversation_ref=identity.call_id,
    )


class NativeSipHarnessReply:
    """Deliver one confirmed Aurora answer into the SAME verified SIP session."""

    def __init__(
        self, *, ledger: Ledger, harness: AnanasMeOwnerHarness,
        playback: SipVoiceTurn, authorize_phone: Callable[[InboundEvent], bool],
    ):
        if not callable(authorize_phone):
            raise ValueError("phone_owner_policy_required")
        if not isinstance(ledger, Ledger):
            raise ValueError("shared_ledger_required")
        self.ledger = ledger
        self.harness = harness
        self.playback = playback
        self.authorize_phone = authorize_phone

    def _validate_origin(self, event: InboundEvent):
        session_identity = self.playback.session.identity
        if (not isinstance(event, InboundEvent)
                or event.channel != "phone" or event.kind != "text"
                or event.tenant_id != session_identity.tenant_id
                or event.sender != session_identity.peer
                or event.recipient != session_identity.local_address
                or event.conversation_ref != session_identity.call_id
                or not event.external_id.startswith(session_identity.call_id + ":")):
            raise PermissionError("sip_event_session_mismatch")
        if self.authorize_phone(event) is not True:
            raise PermissionError("sip_owner_not_authorized")

    @staticmethod
    def _return_key(event: InboundEvent, assistant_id: str) -> str:
        return hashlib.sha256(json.dumps(
            ["native-sip-speech", event.tenant_id, event.external_id, assistant_id],
            ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")).hexdigest()

    async def speak_ready(self, event: InboundEvent, assistant_id: str) -> PhoneReplyResult:
        self._validate_origin(event)
        # Proof of the exact verified source event and native chat admission.
        self.ledger.assert_harness_admission(event, assistant_id)
        observed = await asyncio.to_thread(
            self.harness.observe_native_reply, event, assistant_id
        )
        if observed["state"] != "DONE":
            return PhoneReplyResult("AWAITING_NATIVE_RESULT")
        self._validate_origin(event)  # Re-check after a potentially slow HTTP read.
        reply = observed["reply"]
        identity = self.playback.session.identity
        message = OutboundMessage(
            tenant_id=event.tenant_id, channel="phone",
            idempotency_key=self._return_key(event, assistant_id),
            sender=identity.local_address, recipient=identity.peer,
            text=reply,
        )
        reserved = self.ledger.reserve(message)
        if reserved["state"] != "PENDING":
            return PhoneReplyResult("ALREADY_ATTEMPTED")
        if not self.ledger.claim(message):
            return PhoneReplyResult("ALREADY_ATTEMPTED")
        try:
            receipt = await self.playback.play(reply)
        except BaseException:
            self.ledger.finish(message, state="UNKNOWN")
            raise
        if receipt.state == "COMPLETED":
            # ACCEPTED here means *PCM submitted to the SIP adapter*,
            # NOT that the PSTN human actually heard the entire answer.
            self.ledger.finish(
                message, state="ACCEPTED",
                receipt=f"sip-pcm-bytes:{receipt.bytes_written}",
            )
            return PhoneReplyResult("PCM_SUBMITTED", receipt.bytes_written)
        self.ledger.finish(message, state="UNKNOWN")
        return PhoneReplyResult("INTERRUPTED", receipt.bytes_written)
