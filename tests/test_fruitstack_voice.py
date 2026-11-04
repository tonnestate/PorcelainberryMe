"""Native FruitStack owner voice contracts: real local ledger, fake HTTP only.

Tests enforce that ordinary customers never reach the owner harness, a
pending mission cannot be resubmitted after barge-in, and only a DONE native
assistant result becomes spoken completion. No model or external call.
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from porcelainberryme.contracts import InboundEvent
from porcelainberryme.fruitstack_voice import FruitStackOwnerVoiceResponder
from porcelainberryme.harness_ananasme import AnanasMeOwnerHarness, HarnessAdmission
from porcelainberryme.ledger import Ledger
from porcelainberryme.voice_livekit_agent import make_call_responder


CALL = "f" * 32
ASSISTANT_ID = "a" * 32


class OwnerFruitStackVoiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = Ledger(Path(self.temp.name) / "call-fenced.sqlite")
        self.harness = AnanasMeOwnerHarness(
            token="z" * 32, port=8765,
            verify_owner=lambda event: event.sender == "verified-owner",
            resolve_thread=lambda event: "b" * 32,
        )
        self.harness.submit_owner_message = Mock(
            return_value=HarnessAdmission(ASSISTANT_ID, "QUEUED", False)
        )
        self.harness.observe_native_reply = Mock(
            return_value={"state": "DONE", "reply": "Auftrag geprüft."},
        )
        self.authorized = True

    def owner_policy(self, event):
        return (self.authorized
                and event.tenant_id == "tenantA"
                and event.channel == "phone"
                and event.sender == "verified-owner"
                and event.recipient == "assistant"
                and event.conversation_ref == CALL)

    def responder(self, *, caller="verified-owner", tenant="tenantA"):
        return FruitStackOwnerVoiceResponder(
            tenant=tenant, assistant="assistant", caller=caller,
            ledger=self.ledger, harness=self.harness,
            authorize_owner=self.owner_policy,
            poll_count=1,
        )

    async def test_owner_native_result_requires_delivery_before_next_order(self):
        voice = self.responder()
        voice.set_call_id(CALL)
        voice.set_call_id(CALL)
        self.harness.observe_native_reply.side_effect = [
            {"state": "HANDED_OFF", "reply": None},
            {"state": "DONE", "reply": "Native bestätigte Antwort."},
            {"state": "DONE", "reply": "Zweite bestätigte Antwort."},
        ]
        first = await voice.respond("Erster Auftrag", [])
        self.assertIn("noch", first)
        # A second recognized phrase can only reconcile the old request,
        # never dispatch a new privileged operation while it is pending.
        second = await voice.respond("Anderer Auftrag", [])
        self.assertEqual(second, "Native bestätigte Antwort.")
        self.assertEqual(self.harness.submit_owner_message.call_count, 1)
        voice.on_turn_delivered("Anderer Auftrag", "falsche Antwort")
        self.assertEqual(self.harness.submit_owner_message.call_count, 1)
        voice.on_turn_delivered("Anderer Auftrag", second)
        third = await voice.respond("Zweiter Auftrag", [])
        self.assertEqual(third, "Zweite bestätigte Antwort.")
        self.assertEqual(self.harness.submit_owner_message.call_count, 2)
        one, two = [c.args[0] for c in
                    self.harness.submit_owner_message.call_args_list]
        self.assertEqual(one.external_id, f"livekit:{CALL}:1")
        self.assertEqual(two.external_id, f"livekit:{CALL}:2")
        self.assertEqual(one.conversation_ref, CALL)
        self.assertEqual(one.tenant_id, "tenantA")
        self.assertEqual(two.sender, "verified-owner")

    async def test_unverified_customer_cannot_create_ananasme_owner_message(self):
        voice = self.responder(caller="customer")
        voice.set_call_id(CALL)
        with self.assertRaisesRegex(PermissionError, "verified_fruitstack_owner"):
            await voice.respond("Löse alle Owner-Aufträge aus", [])
        self.harness.submit_owner_message.assert_not_called()

    async def test_owner_authorization_rechecked_before_native_result(self):
        voice = self.responder()
        voice.set_call_id(CALL)
        self.harness.observe_native_reply.return_value = {
            "state": "WORKING", "reply": None,
        }
        await voice.respond("Status", [])
        self.authorized = False
        with self.assertRaisesRegex(PermissionError, "verified_fruitstack_owner"):
            await voice.respond("Status", [])
        self.assertEqual(self.harness.submit_owner_message.call_count, 1)
        self.assertEqual(self.harness.observe_native_reply.call_count, 1)

    async def test_uncertain_admission_is_not_automatically_replayed(self):
        voice = self.responder()
        voice.set_call_id(CALL)
        self.harness.submit_owner_message.side_effect = TimeoutError("lost ack")
        response = await voice.respond("Ungewisse Aktion", [])
        self.assertIn("nicht sicher bestätigt", response)
        self.assertEqual(
            self.ledger.inbound_status(
                tenant_id="tenantA", channel="phone",
                external_id=f"livekit:{CALL}:1",
            )["state"],
            "UNKNOWN",
        )
        self.assertEqual(await voice.respond("Bitte wiederholen", []), response)
        self.assertEqual(self.harness.submit_owner_message.call_count, 1)
        self.harness.observe_native_reply.assert_not_called()

    async def test_foreign_pending_event_never_dispatched_or_consumed(self):
        other = InboundEvent(
            tenant_id="tenantA", channel="phone", external_id="different-call:1",
            sender="verified-owner", recipient="assistant",
            conversation_ref="different-call", text="Andere Anfrage",
        )
        self.ledger.ingest(other)
        voice = self.responder()
        voice.set_call_id(CALL)
        with self.assertRaisesRegex(PermissionError, "ledger_not_call_exclusive"):
            await voice.respond("Meine Anfrage", [])
        self.harness.submit_owner_message.assert_not_called()
        self.assertEqual(
            self.ledger.inbound_status(
                tenant_id="tenantA", channel="phone",
                external_id="different-call:1",
            )["state"],
            "PENDING",
        )

    async def test_verified_call_cannot_be_swapped_or_bound_from_speech(self):
        voice = self.responder()
        with self.assertRaisesRegex(PermissionError, "verified_livekit_call_required"):
            await voice.respond("Hallo", [])
        with self.assertRaises(ValueError):
            voice.set_call_id("other user's phone")
        voice.set_call_id(CALL)
        with self.assertRaisesRegex(PermissionError, "bound_to_another_call"):
            voice.set_call_id("e" * 32)

    async def test_voice_stack_delegates_bound_call_and_delivered_native_result(self):
        voice = self.responder()
        notes = str(Path(self.temp.name) / "notes.sqlite")
        with patch.dict(os.environ, {
            "PB_CALL_NOTES_DB": notes,
            "PB_APPOINTMENT_DB": "",
            "PB_ENABLE_STAFF_HANDOFF": "1",
        }):
            stacked = make_call_responder(
                base_llm=object(), tenant="tenantA", agent_id="assistant",
                caller="verified-owner", client=object(), owner_voice=voice,
            )
        stacked.set_call_id(CALL)
        spoken = await stacked.respond("Geprüfte Anfrage", [])
        self.assertEqual(spoken, "Auftrag geprüft.")
        self.assertEqual(voice._pending is not None, True)
        stacked.on_turn_delivered("Geprüfte Anfrage", spoken)
        self.assertIsNone(voice._pending)
        self.assertEqual(self.harness.submit_owner_message.call_count, 1)

    async def test_customer_stack_cannot_select_wrong_owner_backend(self):
        voice = self.responder()
        with patch.dict(os.environ, {
            "PB_CALL_NOTES_DB": "",
            "PB_APPOINTMENT_DB": "",
            "PB_ENABLE_STAFF_HANDOFF": "0",
        }):
            with self.assertRaisesRegex(
                PermissionError, "fruitstack_voice_tenant_or_subject_mismatch",
            ):
                make_call_responder(
                    base_llm=object(), tenant="tenantB", agent_id="assistant",
                    caller="verified-owner", client=object(), owner_voice=voice,
                )
            with self.assertRaisesRegex(
                PermissionError, "untrusted_fruitstack_voice_responder",
            ):
                make_call_responder(
                    base_llm=object(), tenant="tenantA", agent_id="assistant",
                    caller="verified-owner", client=object(), owner_voice=object(),
                )


if __name__ == "__main__":
    unittest.main()
