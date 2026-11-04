"""Strict owner-only FruitStack voice -> existing AnanasMe native result.

Not an agent engine, customer CRM tool or new message consumer. The trusted
host must authenticate the HUMAN participant independently of caller ID,
supply a per-call ledger, and explicitly bind the verified WebRTC call ID.
A normal customer call must never instantiate this responder.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
import re

from .contracts import InboundEvent
from .harness_ananasme import AnanasMeOwnerHarness
from .inbound import InboundProcessor
from .ledger import Ledger

_CALL_ID = re.compile(r"^[a-f0-9]{32}$")
_PENDING_REPLY = (
    "Der FruitStack bearbeitet Ihre letzte Anfrage noch. "
    "Ein bestätigtes Ergebnis liegt nicht vor."
)
_UNKNOWN_REPLY = (
    "Die Ausführung Ihrer letzten Anfrage ist nicht sicher bestätigt. "
    "Bitte prüfen Sie den Vorgang im AnanasMe-Chat."
)
_TOO_LONG_REPLY = (
    "Die bestätigte Antwort ist für die Sprachausgabe zu lang. "
    "Bitte lesen Sie sie im AnanasMe-Chat."
)


class FruitStackOwnerVoiceResponder:
    """Exactly one outstanding native owner request per verified WebRTC call.

    The source-specific inbound ledger is durable and MUST be exclusive to
    this verified call. Status polling reads the native assistant receipt,
    never reruns a potentially accepted mission. An outstanding request may
    be cleared ONLY after its final spoken result was actually delivered.
    """

    def __init__(self, *, tenant: str, assistant: str, caller: str,
                 ledger: Ledger, harness: AnanasMeOwnerHarness,
                 authorize_owner: Callable[[InboundEvent], bool],
                 poll_count: int = 3, poll_interval: float = 0.1):
        for name, value in (("tenant", tenant), ("assistant", assistant),
                            ("caller", caller)):
            if not isinstance(value, str) or not 1 <= len(value) <= 128:
                raise ValueError("invalid_owner_voice_" + name)
        if not isinstance(ledger, Ledger):
            raise ValueError("durable_owner_voice_ledger_required")
        if not isinstance(harness, AnanasMeOwnerHarness):
            raise ValueError("native_owner_harness_required")
        if not callable(authorize_owner):
            raise ValueError("independent_owner_verifier_required")
        if type(poll_count) is not int or not 1 <= poll_count <= 30:
            raise ValueError("invalid_owner_result_poll_count")
        if not isinstance(poll_interval, (int, float)) or not 0 <= poll_interval <= 2:
            raise ValueError("invalid_owner_result_poll_interval")
        self.tenant = tenant
        self.assistant = assistant
        self.caller = caller
        self.ledger = ledger
        self.harness = harness
        self.authorize_owner = authorize_owner
        self.poll_count = poll_count
        self.poll_interval = float(poll_interval)
        self.greeting = "Guten Tag. Sie sprechen mit dem persönlichen FruitStack-Assistenten."
        self.call_id: str | None = None
        self._turn_number = 0
        self._pending: tuple[InboundEvent, str] | None = None
        self._unknown = False
        self._deliverable: str | None = None

    def set_call_id(self, call_id: str) -> None:
        """Bind to actual authenticated control-plane call, not spoken text."""
        if not isinstance(call_id, str) or not _CALL_ID.fullmatch(call_id):
            raise ValueError("verified_livekit_call_id_required")
        if self.call_id is not None and self.call_id != call_id:
            raise PermissionError("owner_voice_bound_to_another_call")
        self.call_id = call_id

    def _authorize(self, event: InboundEvent) -> None:
        if (event.tenant_id != self.tenant
                or event.sender != self.caller
                or event.recipient != self.assistant
                or event.conversation_ref != self.call_id
                or self.authorize_owner(event) is not True):
            raise PermissionError("verified_fruitstack_owner_required")

    async def respond(self, text: str, history: list[dict]) -> str:
        if self.call_id is None:
            raise PermissionError("verified_livekit_call_required")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1200:
            raise ValueError("invalid_verified_voice_transcript")

        # A pending/uncertain owner action fences EVERY later voice turn.
        # New STT content must not turn an unresolved admission into a second
        # mission, even after acoustic barge-in or model timeout.
        if self._pending is None and not self._unknown:
            self._turn_number += 1
            event = InboundEvent(
                tenant_id=self.tenant, channel="phone",
                external_id=f"livekit:{self.call_id}:{self._turn_number}",
                sender=self.caller, recipient=self.assistant,
                conversation_ref=self.call_id, text=text.strip(),
            )
            self._authorize(event)
            # Only a caller-managed *per-call* ledger is allowed. A process
            # restart or duplicate ID is handled by Ledger fingerprint rules.
            await asyncio.to_thread(self.ledger.ingest, event)
            # The configured durable ledger is exclusive to this call.
            # Do not claim or mark another event UNKNOWN if a host operator
            # accidentally supplies a shared, multi-call intake database.
            queue = await asyncio.to_thread(
                self.ledger.pending, tenant_id=self.tenant, limit=2,
            )
            if queue != [event]:
                self._unknown = True
                raise PermissionError("fruitstack_voice_ledger_not_call_exclusive")
            processor = InboundProcessor(
                self.ledger, tenant_id=self.tenant,
                handler=self.harness.receipt_handler,
                authorize=lambda incoming: (
                    self._same_event(event, incoming)
                    and self.authorize_owner(incoming) is True
                ),
                handler_kind="OWNER_HARNESS",
            )
            admission = await asyncio.to_thread(
                processor.run_once, channel="phone",
            )
            if (admission is None or admission.external_id != event.external_id
                    or admission.state != "HANDLED" or not admission.receipt):
                # Could already have crossed the remote admission boundary.
                # NEVER resubmit with a fresh ID or claim success here.
                self._unknown = True
                return _UNKNOWN_REPLY
            self._pending = (event, admission.receipt)

        if self._unknown:
            return _UNKNOWN_REPLY
        event, assistant_id = self._pending
        self._authorize(event)  # Re-check trusted authority for every poll.
        for attempt in range(self.poll_count):
            try:
                observation = await asyncio.to_thread(
                    self.harness.observe_native_reply, event, assistant_id,
                )
            except (OSError, RuntimeError, ValueError, PermissionError):
                # A failed read is not proof of execution failure or success.
                return _UNKNOWN_REPLY
            self._authorize(event)
            if observation.get("state") == "DONE":
                reply = observation.get("reply")
                if not isinstance(reply, str) or not reply.strip():
                    return _UNKNOWN_REPLY
                self._deliverable = (
                    reply.strip() if len(reply.strip()) <= 500 else _TOO_LONG_REPLY
                )
                return self._deliverable
            if observation.get("state") in ("FAILED", "UNKNOWN", "REVIEW_UNVERIFIED"):
                return _UNKNOWN_REPLY
            if attempt + 1 < self.poll_count:
                await asyncio.sleep(self.poll_interval)
        return _PENDING_REPLY

    @staticmethod
    def _same_event(expected: InboundEvent, actual: InboundEvent) -> bool:
        return isinstance(actual, InboundEvent) and actual == expected

    def on_turn_delivered(self, utterance: str, response: str) -> None:
        """A new action is allowed only after the previous DONE answer is heard."""
        if self._deliverable is not None and response == self._deliverable:
            self._pending = None
            self._deliverable = None
