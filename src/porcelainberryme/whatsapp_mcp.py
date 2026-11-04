"""Opt-in, owner-approved WhatsApp sending through the existing TonnEstate bridge.

Approvals can only be created by trusted host Python, never by MCP tools.
Uncertain provider outcomes are terminal UNKNOWN, never automatically retried.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time

from .contracts import OutboundMessage
from .service import DeliveryRejected
from .tonnestate_whatsapp import TonnEstateWhatsAppTransport


_ID = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
_APPROVAL = re.compile(r"^[a-f0-9]{32}$")
_PHONE = re.compile(r"^[0-9]{5,20}@c\.us$")


def private_file(value: str, *, create: bool = False) -> str:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError("whatsapp_absolute_private_file_required")
    if create:
        try:
            fd = os.open(value, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                         getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    try:
        info = os.lstat(value)
    except OSError as exc:
        raise PermissionError("whatsapp_private_file_missing") from exc
    if (not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_uid != os.geteuid()):
        raise PermissionError("whatsapp_private_file_permissions")
    return value


def owner_token_reader(filename: str):
    private_file(filename)

    def read():
        fd = os.open(filename, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode)
                    or stat.S_IMODE(info.st_mode) & 0o077
                    or info.st_uid != os.geteuid()):
                raise PermissionError("whatsapp_token_permissions")
            with os.fdopen(fd, encoding="utf-8") as stream:
                fd = -1
                token = stream.read(4097).strip()
        finally:
            if fd >= 0:
                os.close(fd)
        if (not 16 <= len(token) <= 4096
                or any(char.isspace() for char in token)):
            raise PermissionError("whatsapp_token_invalid")
        return token

    read()
    return read


class OwnerWhatsAppApprovals:
    """Host-only consent store; no arbitrary model-selected recipients or text."""

    def __init__(self, database: str):
        self.database = private_file(database, create=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS whatsapp_mcp_approvals (
                    approval_id TEXT PRIMARY KEY,
                    tenant TEXT NOT NULL, agent TEXT NOT NULL,
                    sender TEXT NOT NULL, recipient TEXT NOT NULL,
                    body TEXT NOT NULL, consent_hash TEXT NOT NULL,
                    expires_at INTEGER NOT NULL, state TEXT NOT NULL,
                    UNIQUE (tenant, consent_hash)
                );
            """)

    @contextmanager
    def _db(self):
        with closing(sqlite3.connect(self.database, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    @staticmethod
    def scope(tenant, agent, sender):
        if (not isinstance(tenant, str) or not _ID.fullmatch(tenant)
                or not isinstance(agent, str) or not _ID.fullmatch(agent)
                or not isinstance(sender, str) or not sender.strip()
                or len(sender) > 256):
            raise ValueError("whatsapp_owner_scope_invalid")

    def approve(self, *, tenant: str, agent: str, sender: str,
                recipient: str, text: str, consent_reference: str,
                ttl_seconds: int = 120) -> str:
        """Host verifies explicit user authorization for this exact body/recipient."""
        self.scope(tenant, agent, sender)
        if not isinstance(recipient, str) or not _PHONE.fullmatch(recipient):
            raise ValueError("whatsapp_recipient_invalid")
        if (not isinstance(text, str) or not text.strip()
                or len(text) > 3000 or "\x00" in text):
            raise ValueError("whatsapp_text_invalid")
        if (not isinstance(consent_reference, str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", consent_reference)):
            raise ValueError("whatsapp_consent_required")
        if type(ttl_seconds) is not int or not 30 <= ttl_seconds <= 300:
            raise ValueError("whatsapp_approval_ttl_invalid")
        digest = sha256(
            json.dumps([tenant, consent_reference]).encode("utf-8")
        ).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM whatsapp_mcp_approvals "
                "WHERE tenant=? AND consent_hash=?", (tenant, digest),
            ).fetchone()
            if row:
                if any(row[key] != value for key, value in (
                    ("agent", agent), ("sender", sender),
                    ("recipient", recipient), ("body", text),
                )):
                    raise PermissionError("whatsapp_consent_conflict")
                return row["approval_id"]
            active = db.execute(
                "SELECT COUNT(*) FROM whatsapp_mcp_approvals "
                "WHERE tenant=? AND agent=? AND state='APPROVED' AND expires_at>?",
                (tenant, agent, int(time.time())),
            ).fetchone()[0]
            if active >= 64:
                raise OverflowError("whatsapp_pending_capacity")
            retained = db.execute(
                "SELECT COUNT(*) FROM whatsapp_mcp_approvals "
                "WHERE tenant=? AND agent=?", (tenant, agent),
            ).fetchone()[0]
            if retained >= 10000:
                raise OverflowError("whatsapp_retained_capacity")
            approval_id = secrets.token_hex(16)
            db.execute(
                "INSERT INTO whatsapp_mcp_approvals VALUES "
                "(?,?,?,?,?,?,?,?,'APPROVED')",
                (approval_id, tenant, agent, sender, recipient, text,
                 digest, int(time.time()) + ttl_seconds),
            )
            return approval_id

    def prune_terminal(self, *, tenant, agent, sender,
                       retention_days: int = 30) -> int:
        """Host-only privacy maintenance. UNKNOWN requires reconciliation."""
        self.scope(tenant, agent, sender)
        if type(retention_days) is not int or not 1 <= retention_days <= 365:
            raise ValueError("whatsapp_retention_invalid")
        cutoff = int(time.time()) - retention_days * 86400
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                "DELETE FROM whatsapp_mcp_approvals "
                "WHERE tenant=? AND agent=? AND sender=? AND expires_at<? "
                "AND state IN ('ACCEPTED','FAILED','EXPIRED','REVOKED')",
                (tenant, agent, sender, cutoff),
            )
            return result.rowcount

    def _row(self, db, approval_id, tenant, agent, sender):
        self.scope(tenant, agent, sender)
        if not isinstance(approval_id, str) or not _APPROVAL.fullmatch(approval_id):
            raise ValueError("whatsapp_approval_id_invalid")
        row = db.execute(
            "SELECT * FROM whatsapp_mcp_approvals WHERE approval_id=?",
            (approval_id,),
        ).fetchone()
        if (row is None or (row["tenant"], row["agent"], row["sender"])
                != (tenant, agent, sender)):
            raise PermissionError("whatsapp_approval_not_authorized")
        return row

    @staticmethod
    def _public(row):
        state = "UNKNOWN" if row["state"] == "ATTEMPTING" else row["state"]
        return {
            "schema": "porcelainberryme.mcp.whatsapp-send.v1",
            "approval_id": row["approval_id"],
            "state": state, "provider_accepted": state == "ACCEPTED",
            "human_delivered": False,
        }

    def status(self, *, approval_id, tenant, agent, sender):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, approval_id, tenant, agent, sender)
            if row["state"] == "APPROVED" and row["expires_at"] <= int(time.time()):
                db.execute(
                    "UPDATE whatsapp_mcp_approvals SET state='EXPIRED' "
                    "WHERE approval_id=? AND state='APPROVED'", (approval_id,),
                )
                row = self._row(db, approval_id, tenant, agent, sender)
            return self._public(row)

    def revoke(self, *, approval_id, tenant, agent, sender):
        """Host-only cancellation before dispatch. Never revoke an attempted send."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, approval_id, tenant, agent, sender)
            if row["state"] == "APPROVED":
                db.execute(
                    "UPDATE whatsapp_mcp_approvals SET state='REVOKED' "
                    "WHERE approval_id=? AND state='APPROVED'", (approval_id,),
                )
                row = self._row(db, approval_id, tenant, agent, sender)
            return self._public(row)

    def send_once(self, *, approval_id, tenant, agent, sender, api_token):
        """Atomic claim, one provider attempt, no redial on crash or timeout."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, approval_id, tenant, agent, sender)
            if row["state"] != "APPROVED":
                return self._public(row)
            if row["expires_at"] <= int(time.time()):
                db.execute(
                    "UPDATE whatsapp_mcp_approvals SET state='EXPIRED' "
                    "WHERE approval_id=?", (approval_id,),
                )
                return self._public(self._row(db, approval_id, tenant, agent, sender))
            db.execute(
                "UPDATE whatsapp_mcp_approvals SET state='ATTEMPTING' "
                "WHERE approval_id=? AND state='APPROVED'", (approval_id,),
            )
            message = OutboundMessage(
                tenant_id=tenant, idempotency_key=approval_id,
                channel="whatsapp", sender=sender,
                recipient=row["recipient"], text=row["body"],
            )
        try:
            TonnEstateWhatsAppTransport(
                owner_sender=sender, api_token=api_token,
            ).send(message)
            state = "ACCEPTED"
        except DeliveryRejected:
            state = "FAILED"
        except Exception:
            state = "UNKNOWN"
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE whatsapp_mcp_approvals SET state=? "
                "WHERE approval_id=? AND state='ATTEMPTING'",
                (state, approval_id),
            )
            return self._public(self._row(db, approval_id, tenant, agent, sender))


class OwnerWhatsAppMCP:
    """One first-party owner identity, fixed by its authenticated host."""

    def __init__(self, *, tenant: str, agent: str, sender: str,
                 approvals_db: str, token_file: str):
        OwnerWhatsAppApprovals.scope(tenant, agent, sender)
        self.tenant, self.agent, self.sender = tenant, agent, sender
        self.approvals = OwnerWhatsAppApprovals(approvals_db)
        self._token = owner_token_reader(token_file)

    def send(self, approval_id: str) -> dict:
        return self.approvals.send_once(
            approval_id=approval_id, tenant=self.tenant, agent=self.agent,
            sender=self.sender, api_token=self._token(),
        )

    def status(self, approval_id: str) -> dict:
        return self.approvals.status(
            approval_id=approval_id, tenant=self.tenant,
            agent=self.agent, sender=self.sender,
        )
