"""Offline security and integration tests for reviewed company knowledge."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
import struct
import tempfile
import unittest

from porcelainberryme.agent_knowledge import (
    ApprovedKnowledgeResponder, KnowledgeError, KnowledgeStore,
    MAX_UNKNOWN, RETENTION_SECONDS,
)
from porcelainberryme.local_voice import AudioPacket, VoiceTurnEngine


class KnowledgeReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = [1_800_000_000]
        self.path = str(Path(self.temp.name) / "agent.sqlite")
        self.db = KnowledgeStore(self.path, clock=lambda: self.now[0])
        self.db.configure("tenantA", "agent", fallback="Nicht freigegeben.")
        self.db.configure("tenantB", "agent", fallback="Bitte nachfragen.")

    def test_draft_is_never_spoken_until_human_approves_exact_revision(self):
        r = self.db.propose(
            "tenantA", "agent", key="hours", question="Wann habt ihr geöffnet?",
            answer="Montag bis Freitag von 9 bis 17 Uhr.",
        )
        self.assertEqual(r, 1)
        self.assertEqual(self.db.answer("tenantA", "agent",
                                        "Wann habt ihr geöffnet?").response, "Nicht freigegeben.")
        self.assertFalse(self.db.review("tenantA", "agent",
                                        key="hours", expected_revision=2, approve=True))
        self.assertTrue(self.db.review("tenantA", "agent",
                                       key="hours", expected_revision=1, approve=True))
        approved = self.db.answer("tenantA", "agent", "WANN HABT IHR GEÖFFNET?!")
        self.assertTrue(approved.approved)
        self.assertEqual(approved.knowledge_id, "hours")
        self.assertEqual(approved.response, "Montag bis Freitag von 9 bis 17 Uhr.")

    def test_edited_approved_fact_immediately_becomes_draft(self):
        self.db.propose("tenantA", "agent", key="price",
                        question="Was kostet das Produkt?", answer="100 Euro.")
        self.db.review("tenantA", "agent", key="price", expected_revision=1, approve=True)
        revision = self.db.propose("tenantA", "agent", key="price",
                                   question="Was kostet das Produkt?", answer="200 Euro.")
        self.assertEqual(revision, 2)
        self.assertFalse(self.db.review("tenantA", "agent",
                                        key="price", expected_revision=1, approve=True))
        self.assertFalse(self.db.answer("tenantA", "agent",
                                        "Was kostet das Produkt?").approved)
        self.assertTrue(self.db.review("tenantA", "agent",
                                       key="price", expected_revision=2, approve=True))
        self.assertEqual(self.db.answer("tenantA", "agent",
                                        "Was kostet das Produkt?").response, "200 Euro.")
        self.assertTrue(self.db.disable("tenantA", "agent", "price"))
        self.assertFalse(self.db.answer("tenantA", "agent",
                                        "Was kostet das Produkt?").approved)

    def test_cross_tenant_and_agent_data_never_leak(self):
        self.db.propose("tenantA", "agent", key="secret",
                        question="Welche interne Kennzahl gilt?", answer="Nur Tenant A")
        self.db.review("tenantA", "agent", key="secret",
                       expected_revision=1, approve=True)
        self.assertEqual(self.db.answer("tenantB", "agent",
                                        "Welche interne Kennzahl gilt?").response, "Bitte nachfragen.")
        self.db.configure("tenantA", "another")
        self.assertFalse(self.db.answer("tenantA", "another",
                                        "Welche interne Kennzahl gilt?").approved)
        self.assertEqual(self.db.entries("tenantB", "agent"), [])
        self.assertEqual(self.db.entries("tenantA", "another"), [])
        with self.assertRaisesRegex(KnowledgeError, "profile_missing"):
            self.db.answer("tenantA", "missing", "Welche interne Kennzahl gilt?")

    def test_each_assistant_has_own_approved_greeting(self):
        self.db.configure(
            "tenantA", "agent",
            greeting="Willkommen bei unserem Kundenservice.",
        )
        self.db.configure(
            "tenantB", "agent",
            greeting="Guten Tag beim zweiten Unternehmen.",
        )
        self.assertEqual(self.db.profile("tenantA", "agent").greeting,
                         "Willkommen bei unserem Kundenservice.")
        self.assertEqual(self.db.profile("tenantB", "agent").greeting,
                         "Guten Tag beim zweiten Unternehmen.")
        with self.assertRaisesRegex(KnowledgeError, "invalid_greeting"):
            self.db.configure("tenantA", "agent", greeting="")
        with self.assertRaisesRegex(KnowledgeError, "invalid_greeting"):
            self.db.configure("tenantA", "agent", greeting="x" * 501)

    def test_existing_knowledge_profile_is_migrated_without_data_loss(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "legacy.sqlite")
            with sqlite3.connect(path) as con:
                con.execute(
                    "CREATE TABLE pb_kb_agents ("
                    "tenant TEXT NOT NULL, assistant TEXT NOT NULL,"
                    "fallback TEXT NOT NULL, retain_unknown INTEGER NOT NULL,"
                    "PRIMARY KEY(tenant, assistant))"
                )
                con.execute(
                    "INSERT INTO pb_kb_agents VALUES (?,?,?,?)",
                    ("legacy", "assistant", "Alter Fallback", 0),
                )
            migrated = KnowledgeStore(path)
            profile = migrated.profile("legacy", "assistant")
            self.assertEqual(profile.fallback, "Alter Fallback")
            self.assertEqual(profile.greeting, "Guten Tag. Wie kann ich Ihnen helfen?")

    def test_unknown_question_logging_requires_explicit_opt_in(self):
        self.db.answer("tenantA", "agent", "Wie kann ich einen Rabatt bekommen?")
        self.assertEqual(self.db.unanswered("tenantA", "agent"), [])
        self.db.configure("tenantA", "agent", retain_unknown=True)
        self.db.answer("tenantA", "agent", "Wie kann ich einen Rabatt bekommen?")
        self.db.answer("tenantA", "agent", "WIE kann ich einen Rabatt bekommen?!")
        rows = self.db.unanswered("tenantA", "agent")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 2)
        self.assertEqual(self.db.unanswered("tenantB", "agent"), [])
        self.db.configure("tenantA", "agent", retain_unknown=False)
        self.assertEqual(self.db.unanswered("tenantA", "agent"), [])
        self.assertEqual(self.db.purge_unanswered("tenantA", "agent"), 0)

    def test_unknown_retention_and_data_deletion(self):
        self.db.configure("tenantA", "agent", retain_unknown=True)
        self.db.answer("tenantA", "agent", "Brauche ich eine Unterlage?")
        self.now[0] += RETENTION_SECONDS + 1
        self.assertEqual(self.db.unanswered("tenantA", "agent"), [])
        self.db.answer("tenantA", "agent", "Wann ist der nächste Termin?")
        self.assertEqual(len(self.db.unanswered("tenantA", "agent")), 1)
        self.assertEqual(self.db.purge_unanswered("tenantA", "agent"), 1)
        self.assertEqual(self.db.unanswered("tenantA", "agent"), [])

    def test_sql_injection_is_not_confused_with_an_identity(self):
        with self.assertRaises(KnowledgeError):
            self.db.configure("tenantA' OR 1=1 --", "agent")
        with self.assertRaises(KnowledgeError):
            self.db.propose("tenantA", "agent", key="not valid",
                            question="Frage", answer="Antwort")
        with self.assertRaises(KnowledgeError):
            self.db.answer("tenantA", "agent", "")
        with self.assertRaises(KnowledgeError):
            KnowledgeStore("relative.sqlite")
        self.db.propose("tenantA", "agent", key="q",
                        question="Wie lautet ' OR 1=1 --?", answer="Keine Aussage.")
        self.assertEqual(len(self.db.entries("tenantA", "agent")), 1)

    def test_duplicate_questions_do_not_overwrite_other_keys(self):
        self.db.propose("tenantA", "agent", key="q1",
                        question="Wann ist geöffnet?", answer="9 Uhr")
        with self.assertRaisesRegex(KnowledgeError, "another_key"):
            self.db.propose("tenantA", "agent", key="q2",
                            question="WANN ist geöffnet?!", answer="18 Uhr")
        self.assertEqual(self.db.entries("tenantA", "agent")[0]["answer"], "9 Uhr")

    def test_persistent_review_across_restart(self):
        self.db.propose("tenantA", "agent", key="hours",
                        question="Öffnungszeiten?", answer="9 bis 17 Uhr")
        self.db.review("tenantA", "agent", key="hours",
                       expected_revision=1, approve=True)
        reopened = KnowledgeStore(self.path)
        self.assertEqual(reopened.answer("tenantA", "agent",
                                         "ÖFFNUNGSZEITEN?!").response, "9 bis 17 Uhr")


class KnowledgeInCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_voice_turn_uses_only_reviewed_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "agent.sqlite")
            store = KnowledgeStore(path)
            store.configure("tenant", "assistant")
            store.propose("tenant", "assistant", key="open",
                          question="Wann habt ihr geöffnet",
                          answer="Wir haben Montag bis Freitag geöffnet.")
            store.review("tenant", "assistant", key="open",
                         expected_revision=1, approve=True)

            class STT:
                async def transcribe(self, pcm):
                    return "Wann habt ihr geöffnet?"

            class TTS:
                def __init__(self):
                    self.received = []

                async def synthesize(self, text):
                    self.received.append(text)
                    return AudioPacket(struct.pack("<320h", *([1100] * 320)), 16000)

            class Speaker:
                def interrupt(self):
                    pass

                async def play(self, packet):
                    return True

            tts = TTS()
            responder = ApprovedKnowledgeResponder(path, "tenant", "assistant")
            engine = VoiceTurnEngine(STT(), responder, tts, Speaker())
            speech = struct.pack("<320h", *([1200] * 320))
            silent = b"\x00" * 640
            for _ in range(12):
                engine.receive(speech)
            for _ in range(35):
                engine.receive(silent)
            if engine._worker is not None:
                await engine._worker
            self.assertEqual(engine.turns_completed, 1)
            self.assertEqual(tts.received, ["Wir haben Montag bis Freitag geöffnet."])
            await engine.close()

    async def test_missing_profile_fails_before_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "agent.sqlite")
            KnowledgeStore(path)
            with self.assertRaisesRegex(KnowledgeError, "profile_missing"):
                ApprovedKnowledgeResponder(path, "tenant", "not_enrolled")


if __name__ == "__main__":
    unittest.main()
