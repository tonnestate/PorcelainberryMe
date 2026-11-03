"""Privacy and integration tests: no note persisted without in-call opt-in."""
from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from porcelainberryme.call_notes import (
    CallNotesStore, ConsentedNotesResponder, NoteError,
    build_minimal_note, normalize_spoken,
)


class SafeResponder:
    greeting = "Guten Tag vom Service."
    async def respond(self, text, history):
        return "Vielen Dank für Ihre Frage."


def record_turns(*phrases):
    result = []
    for phrase in phrases:
        result.extend([
            {"role": "user", "content": phrase},
            {"role": "assistant", "content": "Verstanden."},
        ])
    return result


class CallNoteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = [1_800_000_000]
        self.path = str(Path(self.temp.name) / "notes.sqlite")
        self.store = CallNotesStore(self.path, clock=lambda: self.now[0])
        self.notes = ConsentedNotesResponder(
            SafeResponder(), self.store, tenant="firm_a",
            assistant="agent", caller="human_a",
        )

    async def test_no_consent_has_zero_persistence(self):
        history = record_turns("Meine vertraulichen Unternehmensinformationen.")
        self.assertEqual(self.notes.greeting, "Guten Tag vom Service.")
        self.assertEqual(await self.notes.respond("Wie geht es?", []),
                         "Vielen Dank für Ihre Frage.")
        self.assertFalse(await self.notes.persist_if_consented("a" * 32, history))
        self.assertEqual(self.store.list_notes("firm_a", "agent"), [])

    async def test_only_utterances_after_explicit_consent_are_kept(self):
        previous = record_turns("Vertrauliche Vorabfrage aus der ersten Gesprächsphase.")
        self.assertIn("kurze Notiz", await self.notes.respond(
            "Ich stimme der Speicherung einer Gesprächsnotiz zu.", previous,
        ))
        conversation = previous + record_turns(
            "Ich stimme der Speicherung einer Gesprächsnotiz zu.",
            "Bitte melden Sie sich wegen meiner Rechnungsfrage bei mir.",
            "Mir ist ein Rückruf nächste Woche wichtig.",
        )
        self.assertTrue(await self.notes.persist_if_consented("a" * 32, conversation))
        stored = self.store.list_notes("firm_a", "agent", caller="human_a")
        self.assertEqual(len(stored), 1)
        self.assertIn("Rechnungsfrage", stored[0].text)
        self.assertIn("Rückruf", stored[0].text)
        self.assertNotIn("Vertrauliche Vorabfrage", stored[0].text)
        self.assertNotIn("stim me", stored[0].text)
        self.assertNotIn("Ich stimme", stored[0].text)
        self.assertEqual(stored[0].excerpts, 2)

    async def test_revocation_clears_permission_before_hangup(self):
        self.assertIn("kurze Notiz", await self.notes.respond(
            "Ich stimme der Speicherung einer Gesprächsnotiz zu", [],
        ))
        in_memory = record_turns(
            "Ich stimme der Speicherung einer Gesprächsnotiz zu",
            "Bitte senden Sie mir eine Kostenaufstellung",
        )
        self.assertIn("keine Gesprächsnotiz", await self.notes.respond(
            "Ich widerrufe die Speicherung der Gesprächsnotiz.", in_memory,
        ))
        in_memory += record_turns("Ich widerrufe die Speicherung der Gesprächsnotiz.")
        self.assertFalse(await self.notes.persist_if_consented("b" * 32, in_memory))
        self.assertEqual(self.store.list_notes("firm_a", "agent"), [])

    async def test_reconsent_does_not_resurrect_previous_content(self):
        await self.notes.respond("Ich stimme der Speicherung einer Gesprächsnotiz zu", [])
        before = record_turns(
            "Ich stimme der Speicherung einer Gesprächsnotiz zu",
            "Frühere Frage zum privaten Detail",
        )
        await self.notes.respond("Ich widerrufe die Speicherung der Gesprächsnotiz", before)
        pre_new = before + record_turns("Ich widerrufe die Speicherung der Gesprächsnotiz")
        await self.notes.respond(
            "Ich stimme der Speicherung einer Gesprächsnotiz zu", pre_new,
        )
        final = pre_new + record_turns(
            "Ich stimme der Speicherung einer Gesprächsnotiz zu",
            "Frage zum Liefertermin unseres Auftrags",
        )
        await self.notes.persist_if_consented("c" * 32, final)
        note = self.store.list_notes("firm_a", "agent")[0].text
        self.assertIn("Liefertermin", note)
        self.assertNotIn("Frühere Frage", note)

    async def test_tenant_isolation_replay_and_explicit_deletion(self):
        await self.notes.respond("Ich stimme der Speicherung einer Gesprächsnotiz zu", [])
        history = record_turns(
            "Ich stimme der Speicherung einer Gesprächsnotiz zu",
            "Die Anfrage betrifft einen Reparaturtermin",
        )
        self.assertTrue(await self.notes.persist_if_consented("d" * 32, history))
        self.assertFalse(await self.notes.persist_if_consented("d" * 32, history))
        self.assertEqual(self.store.list_notes("firm_b", "agent"), [])
        self.assertEqual(self.store.list_notes("firm_a", "different_agent"), [])
        with self.assertRaisesRegex(NoteError, "note_identity_conflict"):
            self.store.save(
                call_id="d" * 32, tenant="firm_b", assistant="agent",
                caller="human_x", note="Widersprüchliche Daten", excerpts=1,
            )
        self.assertFalse(self.store.delete_call("firm_b", "agent", "d" * 32))
        self.assertEqual(self.store.delete_caller("firm_a", "agent", "human_a"), 1)
        self.assertEqual(self.store.list_notes("firm_a", "agent"), [])

    async def test_14_day_ttl_and_bounded_note_excerpts(self):
        self.store.save(
            call_id="e" * 32, tenant="firm_a", assistant="agent", caller="human_a",
            note="Kundenanliegen: Rückruf", excerpts=1,
        )
        self.now[0] += 14 * 86400 + 1
        self.assertEqual(self.store.prune_expired(), 1)
        self.assertEqual(self.store.prune_expired(), 0)
        self.assertEqual(self.store.list_notes("firm_a", "agent"), [])
        summary = build_minimal_note(record_turns(
            "Ein sehr langer Informationsblock " + "x" * 1000,
            "Ein weiterer Informationsblock " + "y" * 1000,
        ))
        self.assertIsNotNone(summary)
        self.assertLessEqual(len(summary[0]), 600)
        self.assertEqual(summary[1], 2)
        self.assertNotIn("x" * 200, summary[0])

    async def test_free_text_cannot_silently_opt_in(self):
        for phrase in (
            "Kannst du mir eine Gesprächsnotiz zeigen?",
            "Ich stimme zu",
            "Ja",
            "Gesprächsnotiz speichern?",
            "Ich stimme der Speicherung einer Gesprächsnotiz nicht zu",
        ):
            self.assertEqual(await self.notes.respond(phrase, []),
                             "Vielen Dank für Ihre Frage.")
            self.assertFalse(self.notes.consented)
        self.assertEqual(normalize_spoken(
            "Ich stimme der Speicherung einer Gesprächsnotiz zu!"),
            "ich stimme der speicherung einer gesprächsnotiz zu",
        )


if __name__ == "__main__":
    unittest.main()
