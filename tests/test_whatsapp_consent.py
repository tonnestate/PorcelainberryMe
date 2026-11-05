"""WhatsApp MCP: real private SQLite, exact human consent and no-effect drafts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from porcelainberryme.whatsapp_consent import OwnerWhatsAppDrafts
from porcelainberryme.whatsapp_mcp import OwnerWhatsAppMCP
from porcelainberryme.whatsapp_owner_consent import main as consent_main


class OwnerDraftLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        folder = Path(self.root.name)
        self.database = str(folder / "approved.sqlite3")
        self.token_file = folder / "owner-bearer"
        self.token_file.write_text("a" * 64, encoding="utf-8")
        self.token_file.chmod(0o600)
        self.owner = OwnerWhatsAppMCP(
            tenant="tenantA", agent="owner_agent", sender="personal_owner",
            approvals_db=self.database, token_file=str(self.token_file),
        )
        self.store = OwnerWhatsAppDrafts(
            self.database, tenant="tenantA", agent="owner_agent",
            sender="personal_owner",
        )

    def tearDown(self):
        self.root.cleanup()

    def draft(self, text="Überweisung 1.234,56 € um 18:05 Uhr – สวัสดี"):
        return self.owner.prepare("49151234567@c.us", text)

    def test_no_transmission_without_human_consent(self):
        item = self.draft()
        self.assertEqual(item["state"], "DRAFT")
        self.assertNotIn("recipient", str(item))
        self.assertNotIn("Überweisung", str(item))
        with patch("porcelainberryme.whatsapp_mcp.TonnEstateWhatsAppTransport.send") as send:
            result = self.owner.send(item["draft_id"])
            send.assert_not_called()
        self.assertEqual(result["state"], "DRAFT")
        self.assertFalse(result["provider_accepted"])
        self.assertFalse(self.owner.status(item["draft_id"])["human_delivered"])
        review = self.store.review(item["draft_id"])
        self.assertEqual(review["text"], "Überweisung 1.234,56 € um 18:05 Uhr – สวัสดี")
        self.assertEqual(review["recipient"], "49151234567@c.us")
        self.assertEqual(review["draft_id"], item["draft_id"])

    def test_explicit_host_confirmation_and_one_provider_attempt(self):
        item = self.draft()
        reviewed = self.store.review(item["draft_id"])
        authorized = self.store.confirm(
            draft_id=item["draft_id"],
            verified_recipient=reviewed["recipient"],
            verified_text=reviewed["text"],
        )
        self.assertEqual(authorized["state"], "APPROVED")
        observed = []
        def deliver(_transport, message):
            observed.append(message.to_dict())
            return "provider-ack"
        with patch("porcelainberryme.whatsapp_mcp.TonnEstateWhatsAppTransport.send",
                   deliver):
            first = self.owner.send(item["draft_id"])
            repeated = self.owner.send(item["draft_id"])
        self.assertEqual(first["state"], "ACCEPTED")
        self.assertEqual(repeated["state"], "ACCEPTED")
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["text"], reviewed["text"])
        self.assertEqual(observed[0]["recipient"], reviewed["recipient"])
        self.assertEqual(first["draft_id"], item["draft_id"])
        self.assertFalse(first["human_delivered"])
        self.assertEqual(self.owner.status(item["draft_id"])["state"], "ACCEPTED")
        with self.assertRaisesRegex(PermissionError, "not_reviewable"):
            self.store.confirm(
                draft_id=item["draft_id"],
                verified_recipient=reviewed["recipient"],
                verified_text=reviewed["text"],
            )

    def test_changed_text_or_recipient_does_not_authorize(self):
        item = self.draft()
        for recipient, text in (
            ("49159999999@c.us", "Überweisung 1.234,56 € um 18:05 Uhr – สวัสดี"),
            ("49151234567@c.us", "edited by model"),
        ):
            with self.subTest(recipient=recipient):
                with self.assertRaisesRegex(PermissionError, "content_changed"):
                    self.store.confirm(
                        draft_id=item["draft_id"],
                        verified_recipient=recipient, verified_text=text,
                    )
        self.assertEqual(self.owner.status(item["draft_id"])["state"], "DRAFT")

    def test_cross_owner_tenant_and_subject_denied(self):
        item = self.draft()
        for tenant, agent, sender in (
            ("tenantB", "owner_agent", "personal_owner"),
            ("tenantA", "other_agent", "personal_owner"),
            ("tenantA", "owner_agent", "other_sender"),
        ):
            foreign = OwnerWhatsAppDrafts(
                self.database, tenant=tenant, agent=agent, sender=sender,
            )
            with self.subTest(tenant=tenant, agent=agent, sender=sender):
                with self.assertRaisesRegex(PermissionError, "not_authorized"):
                    foreign.review(item["draft_id"])
                with self.assertRaisesRegex(PermissionError, "not_authorized"):
                    foreign.resolve(item["draft_id"])

    def test_expired_draft_is_never_approved(self):
        item = self.draft()
        with sqlite3.connect(self.database) as db:
            db.execute(
                "UPDATE whatsapp_mcp_drafts SET expires_at=0 WHERE draft_id=?",
                (item["draft_id"],),
            )
        self.assertEqual(self.store.status(item["draft_id"])["state"], "EXPIRED")
        with self.assertRaisesRegex(PermissionError, "not_reviewable"):
            self.store.review(item["draft_id"])
        with patch("porcelainberryme.whatsapp_mcp.TonnEstateWhatsAppTransport.send") as send:
            self.assertEqual(self.owner.send(item["draft_id"])["state"], "EXPIRED")
            send.assert_not_called()

    def test_revocation_prevents_any_send(self):
        item = self.draft()
        self.assertEqual(self.store.revoke(item["draft_id"])["state"], "REVOKED")
        with patch("porcelainberryme.whatsapp_mcp.TonnEstateWhatsAppTransport.send") as send:
            self.assertEqual(self.owner.send(item["draft_id"])["state"], "REVOKED")
            send.assert_not_called()
        with self.assertRaisesRegex(PermissionError, "not_reviewable"):
            self.store.review(item["draft_id"])

    def test_host_revokes_approved_but_not_yet_dispatched_send(self):
        item = self.draft()
        details = self.store.review(item["draft_id"])
        self.store.confirm(
            draft_id=item["draft_id"],
            verified_recipient=details["recipient"],
            verified_text=details["text"],
        )
        self.assertEqual(self.store.revoke(item["draft_id"])["state"], "REVOKED")
        with patch("porcelainberryme.whatsapp_mcp.TonnEstateWhatsAppTransport.send") as send:
            self.assertEqual(self.owner.send(item["draft_id"])["state"], "REVOKED")
            send.assert_not_called()

    def test_unknown_send_mapping_is_retained_for_reconciliation(self):
        item = self.draft()
        details = self.store.review(item["draft_id"])
        self.store.confirm(
            draft_id=item["draft_id"],
            verified_recipient=details["recipient"],
            verified_text=details["text"],
        )
        approved_id, view = self.store.resolve(item["draft_id"])
        self.assertEqual(view["state"], "APPROVED")
        with sqlite3.connect(self.database) as db:
            db.execute(
                "UPDATE whatsapp_mcp_approvals SET state='UNKNOWN' "
                "WHERE approval_id=?", (approved_id,),
            )
            db.execute(
                "UPDATE whatsapp_mcp_drafts SET expires_at=0 "
                "WHERE draft_id=?", (item["draft_id"],),
            )
        self.assertEqual(self.store.prune(retention_days=1), 0)
        self.assertEqual(self.owner.status(item["draft_id"])["state"], "UNKNOWN")
        with patch("porcelainberryme.whatsapp_mcp.TonnEstateWhatsAppTransport.send") as send:
            self.assertEqual(self.owner.send(item["draft_id"])["state"], "UNKNOWN")
            send.assert_not_called()

    def test_old_confirmed_send_mapping_can_be_pruned(self):
        item = self.draft()
        details = self.store.review(item["draft_id"])
        self.store.confirm(
            draft_id=item["draft_id"],
            verified_recipient=details["recipient"],
            verified_text=details["text"],
        )
        approved_id, _ = self.store.resolve(item["draft_id"])
        with sqlite3.connect(self.database) as db:
            db.execute(
                "UPDATE whatsapp_mcp_approvals SET state='ACCEPTED' "
                "WHERE approval_id=?", (approved_id,),
            )
            db.execute(
                "UPDATE whatsapp_mcp_drafts SET expires_at=0 "
                "WHERE draft_id=?", (item["draft_id"],),
            )
        self.assertEqual(self.store.prune(retention_days=1), 1)
        self.assertIsNone(self.store.status(item["draft_id"]))

    def test_draft_preparation_never_accepts_groups_or_empty_content(self):
        for recipient, text in (
            ("12345@g.us", "Hallo"),
            ("481245@c.us", ""),
            ("481245@c.us", "A" * 3001),
            ("481245@c.us", "before\x00after"),
        ):
            with self.subTest(recipient=recipient, text_length=len(text)):
                with self.assertRaises(ValueError):
                    self.owner.prepare(recipient, text)

    def test_concurrent_preparation_and_confirm_has_one_send_approval(self):
        draft = self.draft("One-time authorized message")
        reviewed = self.store.review(draft["draft_id"])
        def approve(_):
            try:
                return self.store.confirm(
                    draft_id=draft["draft_id"],
                    verified_recipient=reviewed["recipient"],
                    verified_text=reviewed["text"],
                )["state"]
            except PermissionError:
                return "DENIED"
        with ThreadPoolExecutor(max_workers=12) as pool:
            states = list(pool.map(approve, range(12)))
        self.assertEqual(states.count("APPROVED"), 1)
        self.assertEqual(states.count("DENIED"), 11)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM whatsapp_mcp_approvals "
                    "WHERE consent_hash IN "
                    "(SELECT consent_hash FROM whatsapp_mcp_approvals)"
                ).fetchone()[0], 1,
            )

    def test_corrupt_draft_fingerprint_fails_closed(self):
        item = self.draft()
        with sqlite3.connect(self.database) as db:
            db.execute(
                "UPDATE whatsapp_mcp_drafts SET body=? WHERE draft_id=?",
                ("MALICIOUS CHANGE", item["draft_id"]),
            )
        changed = self.store.review(item["draft_id"])
        with self.assertRaisesRegex(PermissionError, "integrity_invalid"):
            self.store.confirm(
                draft_id=item["draft_id"],
                verified_recipient=changed["recipient"],
                verified_text=changed["text"],
            )
        self.assertEqual(self.owner.status(item["draft_id"])["state"], "DRAFT")

    def test_cli_fails_without_verified_interactive_terminal(self):
        item = self.draft()
        env = {
            "PB_MCP_WHATSAPP_ENABLED": "1",
            "PB_MCP_WHATSAPP_OWNER_TENANT": "tenantA",
            "PB_MCP_WHATSAPP_OWNER_AGENT": "owner_agent",
            "PB_MCP_WHATSAPP_OWNER_SENDER": "personal_owner",
            "PB_MCP_WHATSAPP_APPROVALS_DB": self.database,
        }
        with patch.dict(os.environ, env), patch.object(
                sys, "argv", ["whatsapp-owner-consent", "approve", item["draft_id"]]):
            with self.assertRaises(SystemExit):
                consent_main()
        self.assertEqual(self.owner.status(item["draft_id"])["state"], "DRAFT")

    def test_cli_explicit_local_operator_confirmation(self):
        item = self.draft()
        env = {
            "PB_MCP_WHATSAPP_ENABLED": "1",
            "PB_MCP_WHATSAPP_OWNER_TENANT": "tenantA",
            "PB_MCP_WHATSAPP_OWNER_AGENT": "owner_agent",
            "PB_MCP_WHATSAPP_OWNER_SENDER": "personal_owner",
            "PB_MCP_WHATSAPP_APPROVALS_DB": self.database,
        }
        terminal = MagicMock()
        terminal.isatty.return_value = True
        terminal.write.return_value = None
        terminal.flush.return_value = None
        with patch.dict(os.environ, env), patch.object(
                sys, "argv", ["whatsapp-owner-consent", "approve", item["draft_id"]]), (
                patch.object(sys, "stdin", terminal)), (
                patch.object(sys, "stdout", terminal)), (
                patch("builtins.input", return_value="WRONG")):
            with self.assertRaises(SystemExit):
                consent_main()
        self.assertEqual(self.owner.status(item["draft_id"])["state"], "DRAFT")

        with patch.dict(os.environ, env), patch.object(
                sys, "argv", ["whatsapp-owner-consent", "approve", item["draft_id"]]), (
                patch.object(sys, "stdin", terminal)), (
                patch.object(sys, "stdout", terminal)), (
                patch("builtins.input", return_value="APPROVE " + item["draft_id"])):
            consent_main()
        self.assertEqual(self.owner.status(item["draft_id"])["state"], "APPROVED")


if __name__ == "__main__":
    unittest.main()
