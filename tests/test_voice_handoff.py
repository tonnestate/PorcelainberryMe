"""Spoken staff handoff: explicit phrase, no free LLM tool execution."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from porcelainberryme.voice_handoff import HumanHandoffResponder
from porcelainberryme.voice_livekit_agent import LiveKitAgentCall


class FakeBase:
    greeting = "Guten Tag vom Kundenservice."
    async def respond(self, text, history):
        return "Freigegebene Auskunft zum Anliegen."


class Control:
    def __init__(self):
        self.offers = []
        self.actions = []
        self.state = "OFFERED"
        self.approved = "human_staff"

    async def offer_handoff(self, call_id, idem):
        self.offers.append((call_id, idem))
        return {
            "state": self.state,
            "target": self.approved,
        }

    async def change_handoff(self, call_id, action):
        self.actions.append((call_id, action))
        return {
            "state": "CANCELLED" if action == "cancel" else "CONNECTED",
        }

    async def status(self, call_id):
        return {
            "state": "ACTIVE",
            "handoff": {
                "state": "ACCEPTED",
                "target": self.approved,
            },
        }


class HumanHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_explicit_caller_request_offers_staff(self):
        client = Control()
        responder = HumanHandoffResponder(FakeBase(), client)
        responder.set_call_id("a" * 32)
        self.assertEqual(responder.greeting, FakeBase.greeting)
        for phrase in (
            "Kann ich irgendwann jemanden sprechen?",
            "Was kostet mein Tarif?",
            "Mitarbeiter?",
            "Verbinde mich mit fremder Nummer",
            "Ich habe kein Interesse an einer Weiterleitung",
        ):
            self.assertEqual(
                await responder.respond(phrase, []),
                "Freigegebene Auskunft zum Anliegen.",
            )
        self.assertEqual(client.offers, [])
        reply = await responder.respond(
            "Bitte verbinden Sie mich mit einem Mitarbeiter!", [],
        )
        self.assertIn("Mitarbeiter", reply)
        self.assertEqual(len(client.offers), 1)
        self.assertEqual(client.offers[0][0], "a" * 32)
        self.assertTrue(client.offers[0][1].startswith("handoff-"))
        await responder.respond("Bitte verbinden Sie mich mit einem Mitarbeiter!", [])
        self.assertEqual(client.offers[0][1], client.offers[1][1])
        with self.assertRaisesRegex(ValueError, "handoff_bound"):
            responder.set_call_id("b" * 32)

    async def test_local_control_failure_never_promises_success(self):
        class Down(Control):
            async def offer_handoff(self, call_id, idem):
                raise OSError("self hosted service unavailable")

        responder = HumanHandoffResponder(FakeBase(), Down())
        responder.set_call_id("a" * 32)
        self.assertIn(
            "nicht erreichbar",
            await responder.respond(
                "Ich möchte mit einem Menschen sprechen", [],
            ),
        )
        responder = HumanHandoffResponder(FakeBase(), Control())
        self.assertIn(
            "erst nach Beginn",
            await responder.respond(
                "Ich möchte mit einem Mitarbeiter sprechen", [],
            ),
        )

    async def test_cancel_is_specific_and_uses_agent_control(self):
        client = Control()
        responder = HumanHandoffResponder(FakeBase(), client)
        responder.set_call_id("a" * 32)
        self.assertIn(
            "abgebrochen",
            await responder.respond(
                "Ich möchte die Weiterleitung abbrechen!", [],
            ),
        )
        self.assertEqual(client.actions, [("a" * 32, "cancel")])

    async def test_existing_consent_note_pipeline_remains_reachable(self):
        class Retaining(FakeBase):
            def __init__(self):
                self.receipts = []
            async def persist_if_consented(self, call_id, history):
                self.receipts.append((call_id, history))
                return True

        inner = Retaining()
        responder = HumanHandoffResponder(inner, Control())
        transcript = [{"role": "user", "content": "post consent"}]
        self.assertTrue(await responder.persist_if_consented("a" * 32, transcript))
        self.assertEqual(inner.receipts, [("a" * 32, transcript)])

    async def test_handoff_reply_never_acknowledges_pending_native_owner_result(self):
        class DeliveryTracked(FakeBase):
            def __init__(self):
                self.delivered = []
                self.bound = []

            def set_call_id(self, call_id):
                self.bound.append(call_id)

            def on_turn_delivered(self, text, reply):
                self.delivered.append((text, reply))

        inner = DeliveryTracked()
        adapter = HumanHandoffResponder(inner, Control())
        adapter.set_call_id("a" * 32)
        self.assertEqual(inner.bound, ["a" * 32])
        handoff = await adapter.respond(
            "Ich möchte mit einem Mitarbeiter sprechen", [],
        )
        adapter.on_turn_delivered("Ich möchte mit einem Mitarbeiter sprechen", handoff)
        self.assertEqual(inner.delivered, [])
        ordinary = await adapter.respond("Wie geht es weiter?", [])
        adapter.on_turn_delivered("Wie geht es weiter?", ordinary)
        self.assertEqual(inner.delivered, [
            ("Wie geht es weiter?", ordinary),
        ])

    async def test_agent_only_completes_on_observed_accepted_staff_microphone(self):
        client = Control()
        agent = LiveKitAgentCall(
            client=client, tenant="enterprise", callee="customer",
            tts=object(), stt=object(), llm=object(), rtc=object(),
        )
        call_id = "f" * 32
        expected = agent.participant_identity(call_id, "enterprise", "human_staff")
        await agent._complete_handoff_on_staff_track(
            call_id, "ip_" + "0" * 24,
        )
        self.assertEqual(client.actions, [])
        await agent._complete_handoff_on_staff_track(call_id, expected)
        self.assertEqual(client.actions, [(call_id, "complete")])

        client.status = AsyncMock(return_value={
            "state": "ACTIVE", "handoff": {
                "state": "OFFERED", "target": "human_staff",
            },
        })
        await agent._complete_handoff_on_staff_track(call_id, expected)
        self.assertEqual(client.actions, [(call_id, "complete")])


if __name__ == "__main__":
    unittest.main()
