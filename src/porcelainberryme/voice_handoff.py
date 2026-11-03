"""Safe speech-driven human warm handoff for the existing voice worker.

Only narrow, unambiguous German user requests may offer an operator-approved
staff seat. The original call remains ACTIVE while staff decides/joins.
The actual handoff is completed by the agent only after its LiveKit media
callback observes that specific staff member's microphone track.
"""
from __future__ import annotations

import re
from uuid import uuid4

HANDOFF_REQUESTS = frozenset({
    "bitte verbinden sie mich mit einem mitarbeiter",
    "ich möchte mit einem mitarbeiter sprechen",
    "ich moechte mit einem mitarbeiter sprechen",
    "ich möchte mit einem menschen sprechen",
    "bitte verbinden sie mich mit einem menschen",
    "ich möchte einen mitarbeiter sprechen",
})
HANDOFF_CANCEL = frozenset({
    "ich möchte die weiterleitung abbrechen",
    "bitte brechen sie die weiterleitung ab",
})


def normalized_command(text: str) -> str:
    if not isinstance(text, str) or len(text) > 1200:
        return ""
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


class HumanHandoffResponder:
    """Extends existing agent response protocol without a separate service."""

    def __init__(self, responder, client):
        self.responder = responder
        self.client = client
        self.call_id = None
        self.greeting = getattr(
            responder, "greeting", "Guten Tag. Wie kann ich Ihnen helfen?",
        )
        self._idempotency_key = "handoff-" + uuid4().hex

    def set_call_id(self, call_id: str):
        if self.call_id is not None and self.call_id != call_id:
            raise ValueError("handoff_bound_to_another_call")
        self.call_id = call_id

    async def respond(self, text: str, history: list[dict]) -> str:
        phrase = normalized_command(text)
        if phrase in HANDOFF_REQUESTS:
            if self.call_id is None:
                return "Die Weiterleitung kann erst nach Beginn des Anrufs angeboten werden."
            try:
                result = await self.client.offer_handoff(
                    self.call_id, self._idempotency_key,
                )
            except Exception:
                # Do not promise a transfer if staffing, consent, permission,
                # DND or the local control API is unavailable.
                return (
                    "Ein persönlicher Ansprechpartner ist derzeit nicht "
                    "erreichbar. Ich kann Ihnen weiterhin helfen."
                )
            if result["state"] == "OFFERED":
                return (
                    "Ich frage jetzt einen Mitarbeiter an. "
                    "Sie bleiben bis zur Übernahme mit mir verbunden."
                )
            if result["state"] == "ACCEPTED":
                return "Der Mitarbeiter hat angenommen. Ich verbinde Sie, sobald die Audioverbindung steht."
            if result["state"] == "CONNECTED":
                return "Sie sind bereits mit einem Mitarbeiter verbunden."
            return (
                "Die Weiterleitung ist nicht mehr verfügbar. "
                "Sie können mir Ihre Frage weiterhin stellen."
            )
        if phrase in HANDOFF_CANCEL:
            if self.call_id is None:
                return "Es gibt keine aktive Weiterleitung."
            try:
                result = await self.client.change_handoff(self.call_id, "cancel")
            except Exception:
                return (
                    "Die Weiterleitung konnte nicht abgebrochen werden. "
                    "Bitte prüfen Sie, ob bereits ein Mitarbeiter verbunden ist."
                )
            if result["state"] == "CANCELLED":
                return "Die Weiterleitung wurde abgebrochen. Wir bleiben im Gespräch."
            return "Es gibt keine abbrechbare Weiterleitung."
        return await self.responder.respond(text, history)

    async def persist_if_consented(self, call_id, history):
        persist = getattr(self.responder, "persist_if_consented", None)
        if persist is None:
            return False
        return await persist(call_id, history)
