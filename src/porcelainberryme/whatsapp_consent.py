"""Durable WhatsApp draft-to-human-consent bridge for the existing owner account.

MCP may create a DRAFT; only an independently authenticated first-party
host can confirm the exact stored contact and message. Nothing here sends
messages or creates a second WhatsApp transport.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from hashlib import sha256
import json
import re
import secrets
import sqlite3
import time

from .whatsapp_mcp import OwnerWhatsAppApprovals, private_file

_DRAFT = re.compile(r"^[a-f0-9]{32}$")
_CONTACT = re.compile(r"^[0-9]{5,20}@c\.us$")


class OwnerWhatsAppDrafts:
    """Same private SQLite file, separate host-authorized drafting lifecycle."""

    def __init__(self, database: str, *, tenant: str, agent: str, sender: str):
        OwnerWhatsAppApprovals.scope(tenant, agent, sender)
        self.database = private_file(database, create=True)
        self.tenant, self.agent, self.sender = tenant, agent, sender
        self.approvals = OwnerWhatsAppApprovals(database)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS whatsapp_mcp_drafts (
                    draft_id TEXT PRIMARY KEY,
                    tenant TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    body TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    approval_id TEXT,
                    CHECK (state IN ('DRAFT','APPROVED','EXPIRED','REVOKED'))
                );
                CREATE INDEX IF NOT EXISTS whatsapp_mcp_draft_scope
                  ON whatsapp_mcp_drafts (tenant,agent,sender,state,expires_at);
            """)

    @contextmanager
    def _db(self):
        private_file(self.database)
        with closing(sqlite3.connect(self.database, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    @staticmethod
    def _public(draft) -> dict:
        return {
            "schema": "porcelainberryme.mcp.whatsapp-draft.v1",
            "draft_id": draft["draft_id"],
            "state": draft["state"],
            "approved": draft["state"] == "APPROVED",
            "provider_accepted": False,
        }

    def _read(self, db, draft_id: str):
        if not isinstance(draft_id, str) or not _DRAFT.fullmatch(draft_id):
            raise ValueError("whatsapp_draft_id_invalid")
        row = db.execute(
            "SELECT * FROM whatsapp_mcp_drafts WHERE draft_id=?", (draft_id,),
        ).fetchone()
        if row is None:
            return None
        if (row["tenant"], row["agent"], row["sender"]) != (
                self.tenant, self.agent, self.sender):
            raise PermissionError("whatsapp_draft_not_authorized")
        return row

    def _expire(self, db, row):
        if row["state"] == "DRAFT" and row["expires_at"] <= int(time.time()):
            db.execute(
                "UPDATE whatsapp_mcp_drafts SET state='EXPIRED' "
                "WHERE draft_id=? AND state='DRAFT'", (row["draft_id"],),
            )
            return self._read(db, row["draft_id"])
        return row

    def prepare(self, recipient: str, text: str) -> dict:
        """Non-effecting model tool: only stores a content-bound draft."""
        if not isinstance(recipient, str) or not _CONTACT.fullmatch(recipient):
            raise ValueError("whatsapp_recipient_invalid")
        if (not isinstance(text, str) or not text.strip()
                or len(text) > 3000 or "\x00" in text):
            raise ValueError("whatsapp_text_invalid")
        now = int(time.time())
        draft_id = secrets.token_hex(16)
        digest = sha256(json.dumps(
            [self.tenant, self.agent, self.sender, recipient, text],
            separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute(
                "SELECT COUNT(*) FROM whatsapp_mcp_drafts "
                "WHERE tenant=? AND agent=? AND sender=? "
                "AND state='DRAFT' AND expires_at>?",
                (self.tenant, self.agent, self.sender, now),
            ).fetchone()[0]
            retained = db.execute(
                "SELECT COUNT(*) FROM whatsapp_mcp_drafts "
                "WHERE tenant=? AND agent=? AND sender=?",
                (self.tenant, self.agent, self.sender),
            ).fetchone()[0]
            if pending >= 64 or retained >= 10000:
                raise OverflowError("whatsapp_draft_capacity_exceeded")
            db.execute(
                "INSERT INTO whatsapp_mcp_drafts VALUES "
                "(?,?,?,?,?,?,?,?,? ,NULL)",
                (draft_id, self.tenant, self.agent, self.sender,
                 recipient, text, digest, now + 600, "DRAFT"),
            )
            row = self._read(db, draft_id)
        return self._public(row)

    def status(self, draft_id: str) -> dict | None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._read(db, draft_id)
            if row is None:
                return None
            row = self._expire(db, row)
            return self._public(row)

    def review(self, draft_id: str) -> dict:
        """Host only: disclose the exact content for human confirmation."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._read(db, draft_id)
            if row is None:
                raise PermissionError("whatsapp_draft_not_authorized")
            row = self._expire(db, row)
            if row["state"] != "DRAFT":
                raise PermissionError("whatsapp_draft_not_reviewable")
            return {
                "draft_id": draft_id,
                "recipient": row["recipient"],
                "text": row["body"],
                "fingerprint": row["fingerprint"],
                "expires_at": row["expires_at"],
            }

    def confirm(self, *, draft_id: str, verified_recipient: str,
                verified_text: str) -> dict:
        """Host only; operator must authenticate owner and verify exact details.

        Repeated confirmations are idempotent. The immutable draft ID is
        used as the reference for the separately durable send approval.
        """
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._read(db, draft_id)
            if row is None:
                raise PermissionError("whatsapp_draft_not_authorized")
            row = self._expire(db, row)
            if row["state"] != "DRAFT":
                raise PermissionError("whatsapp_draft_not_reviewable")
            if (verified_recipient != row["recipient"]
                    or verified_text != row["body"]):
                raise PermissionError("whatsapp_draft_content_changed")
            if row["fingerprint"] != sha256(json.dumps(
                [self.tenant, self.agent, self.sender, row["recipient"],
                 row["body"]], separators=(",", ":"), ensure_ascii=False,
            ).encode("utf-8")).hexdigest():
                raise PermissionError("whatsapp_draft_integrity_invalid")
            # Host-only consent record is committed before draft mapping.
            # If the process crashes in between, repeating the same
            # verified consent reference returns the same approval ID.
            approval_id = self.approvals.approve(
                tenant=self.tenant, agent=self.agent, sender=self.sender,
                recipient=row["recipient"], text=row["body"],
                consent_reference="draft:" + draft_id, ttl_seconds=120,
            )
            db.execute(
                "UPDATE whatsapp_mcp_drafts SET state='APPROVED',"
                "approval_id=? WHERE draft_id=? AND state='DRAFT'",
                (approval_id, draft_id),
            )
            return self._public(self._read(db, draft_id))

    def resolve(self, draft_id: str) -> tuple[str | None, dict | None]:
        """Map a draft to a send approval only after separate host consent."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._read(db, draft_id)
            if row is None:
                return None, None
            row = self._expire(db, row)
            return (row["approval_id"] if row["state"] == "APPROVED" else None,
                    self._public(row))

    def revoke(self, draft_id: str) -> dict:
        """Host only: decline a draft; cannot undo any started send."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._read(db, draft_id)
            if row is None:
                raise PermissionError("whatsapp_draft_not_authorized")
            row = self._expire(db, row)
            if row["state"] == "DRAFT":
                db.execute(
                    "UPDATE whatsapp_mcp_drafts SET state='REVOKED' "
                    "WHERE draft_id=? AND state='DRAFT'", (draft_id,),
                )
            return self._public(self._read(db, draft_id))

    def prune(self, retention_days: int = 30) -> int:
        if type(retention_days) is not int or not 1 <= retention_days <= 365:
            raise ValueError("whatsapp_draft_retention_invalid")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                "DELETE FROM whatsapp_mcp_drafts "
                "WHERE tenant=? AND agent=? AND sender=? "
                "AND expires_at<? AND state IN "
                "('DRAFT','EXPIRED','APPROVED','REVOKED')",
                (self.tenant, self.agent, self.sender,
                 int(time.time()) - retention_days * 86400),
            )
            return result.rowcount
