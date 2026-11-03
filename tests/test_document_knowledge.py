"""Offline approval, retrieval, provenance and voice tests for business documents."""
from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from porcelainberryme.agent_knowledge import (
    ApprovedKnowledgeResponder, KnowledgeError, KnowledgeStore,
)
from porcelainberryme.document_knowledge import (
    DocumentKnowledgeStore, split_reviewable, terms,
)


class DocumentKnowledgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = str(Path(self.folder.name) / "knowledge.sqlite")
        self.store = KnowledgeStore(self.path)
        self.store.configure("company_a", "assistant", retain_unknown=True)
        self.store.configure("company_b", "assistant")
        self.docs = DocumentKnowledgeStore(self.path)

    def _draft(self, key="policy", tenant="company_a", content=None):
        return self.docs.propose_document(
            tenant, "assistant", key=key, title="Leitfaden Service",
            text=content or (
                "Die Stornierungsfrist für unsere Standardtermine beträgt "
                "vierundzwanzig Stunden vor dem Termin.\n\n"
                "Die Parkplätze unseres Empfangs befinden sich hinter dem Gebäude "
                "an der südlichen Zufahrt."
            ),
        )

    async def test_document_draft_cannot_be_spoken_until_revision_approved(self):
        self.assertEqual(self._draft(), 1)
        voice = ApprovedKnowledgeResponder(
            self.path, "company_a", "assistant",
        )
        query = "Wie lautet die Stornierungsfrist für unsere Standardtermine?"
        self.assertIsNone(self.docs.find_document("company_a", "assistant", query))
        self.assertIn("keine freigegebene", await voice.respond(query, []))
        self.assertFalse(self.docs.review_document(
            "company_a", "assistant", key="policy", revision=2, approve=True,
        ))
        self.assertTrue(self.docs.review_document(
            "company_a", "assistant", key="policy", revision=1, approve=True,
        ))
        answer = await voice.respond(query, [])
        self.assertIn("Laut Dokument Leitfaden Service:", answer)
        self.assertIn("vierundzwanzig Stunden", answer)
        self.assertNotIn("Parkplätze", answer)
        self.assertLessEqual(len(answer), 500)
        # Successful document retrieval must NOT be logged as an open question.
        self.assertEqual(len(self.store.unanswered("company_a", "assistant")), 1)
        self.assertEqual(self.store.unanswered("company_a", "assistant")[0]["count"], 1)

    async def test_replaced_document_is_unapproved_until_new_revision_reviewed(self):
        self._draft()
        self.docs.review_document("company_a", "assistant", key="policy",
                                  revision=1, approve=True)
        query = "Wie lautet die Stornierungsfrist für unsere Standardtermine?"
        self.assertIn("vierundzwanzig", await ApprovedKnowledgeResponder(
            self.path, "company_a", "assistant",
        ).respond(query, []))
        self.assertEqual(
            self._draft(content="Die Stornierungsfrist für unsere Standardtermine "
                                "beträgt achtundvierzig Stunden vor dem Termin."),
            2,
        )
        self.assertIsNone(self.docs.find_document("company_a", "assistant", query))
        self.assertFalse(self.docs.review_document(
            "company_a", "assistant", key="policy", revision=1, approve=True,
        ))
        self.assertTrue(self.docs.review_document(
            "company_a", "assistant", key="policy", revision=2, approve=True,
        ))
        self.assertIn("achtundvierzig", await ApprovedKnowledgeResponder(
            self.path, "company_a", "assistant",
        ).respond(query, []))
        self.assertTrue(self.docs.disable_document("company_a", "assistant", "policy"))
        self.assertIsNone(self.docs.find_document("company_a", "assistant", query))

    async def test_cross_tenant_and_assistant_lookup_fails_closed(self):
        self._draft()
        self.docs.review_document("company_a", "assistant", key="policy",
                                  revision=1, approve=True)
        query = "Wie lautet die Stornierungsfrist für unsere Standardtermine?"
        self.assertIsNone(self.docs.find_document("company_b", "assistant", query))
        self.assertEqual(await ApprovedKnowledgeResponder(
            self.path, "company_b", "assistant",
        ).respond(query, []), self.store.profile("company_b", "assistant").fallback)
        self.store.configure("company_a", "another")
        self.assertIsNone(self.docs.find_document("company_a", "another", query))
        self.assertEqual(self.docs.list_documents("company_b", "assistant"), [])

    async def test_unapproved_and_ambiguous_documents_do_not_leak(self):
        self._draft()
        self.docs.review_document("company_a", "assistant", key="policy",
                                  revision=1, approve=True)
        self._draft(key="competing", content=(
            "Die Stornierungsfrist für unsere Standardtermine beträgt "
            "sieben Tage vor dem Termin."
        ))
        self.docs.review_document("company_a", "assistant", key="competing",
                                  revision=1, approve=True)
        query = "Wie lautet die Stornierungsfrist für unsere Standardtermine?"
        self.assertIsNone(self.docs.find_document("company_a", "assistant", query))
        self.assertIn("keine freigegebene", await ApprovedKnowledgeResponder(
            self.path, "company_a", "assistant",
        ).respond(query, []))

    async def test_incomplete_query_does_not_guess_with_one_token(self):
        self._draft()
        self.docs.review_document("company_a", "assistant", key="policy",
                                  revision=1, approve=True)
        self.assertIsNone(self.docs.find_document(
            "company_a", "assistant", "Stornierungsfrist?",
        ))
        self.assertIsNone(self.docs.find_document(
            "company_a", "assistant", "Wie geht das mit meinen Terminen?",
        ))

    async def test_approved_faq_overrides_document(self):
        self._draft()
        self.docs.review_document("company_a", "assistant", key="policy",
                                  revision=1, approve=True)
        self.store.propose(
            "company_a", "assistant", key="official",
            question="Wie lautet die Stornierungsfrist für unsere Standardtermine?",
            answer="Bitte wenden Sie sich an die Buchungsabteilung.",
        )
        self.store.review("company_a", "assistant",
                          key="official", expected_revision=1, approve=True)
        answer = await ApprovedKnowledgeResponder(
            self.path, "company_a", "assistant",
        ).respond("Wie lautet die Stornierungsfrist für unsere Standardtermine?", [])
        self.assertEqual(answer, "Bitte wenden Sie sich an die Buchungsabteilung.")

    async def test_persistence_across_restart_and_review_history(self):
        self._draft()
        self.docs.review_document("company_a", "assistant", key="policy",
                                  revision=1, approve=True)
        second = DocumentKnowledgeStore(self.path)
        self.assertEqual(second.list_documents("company_a", "assistant"), [
            {"id": "policy", "title": "Leitfaden Service",
             "revision": 1, "status": "APPROVED"}
        ])
        hit = second.find_document(
            "company_a", "assistant",
            "Wie lautet die Stornierungsfrist für unsere Standardtermine?",
        )
        self.assertIsNotNone(hit)
        self.assertEqual(hit.document_id, "policy")
        self.assertEqual(hit.revision, 1)

    def test_input_is_bounded_and_no_sentence_is_truncated(self):
        self.assertEqual(len(split_reviewable("Ein vollständiger Satz.")), 1)
        with self.assertRaisesRegex(KnowledgeError, "document_sentence_too_long"):
            split_reviewable("Ein einziges Wort " + "x" * 350 + ".")
        with self.assertRaisesRegex(KnowledgeError, "invalid_document_size"):
            split_reviewable("x" * 40001)
        with self.assertRaisesRegex(KnowledgeError, "invalid_document_binary_data"):
            split_reviewable("Hallo\x00Welt")
        with self.assertRaisesRegex(KnowledgeError, "invalid_tenant"):
            self.docs.propose_document(
                "company';DROP", "assistant", key="x",
                title="Test", text="Geprüfter Inhalt.",
            )
        self.assertIn("stornierungsfrist", terms("Die Stornierungsfrist gilt"))
        self.assertNotIn("oder", terms("Oder welche Regel gilt?"))


if __name__ == "__main__":
    unittest.main()
