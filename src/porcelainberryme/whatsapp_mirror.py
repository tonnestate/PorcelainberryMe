"""Passive, owner-only WhatsApp inbox mirror; never enters the agent work queue.

The existing authenticated n8n/Aurora WhatsApp consumer is the only upstream.
Events arrive on the pre-existing signed local intake server and are stored
separately from actionable messages. Closed-world scope prevents account mixing.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from hashlib import sha256
import json
import sqlite3
import time

from .contracts import InboundEvent
from .ledger import IdempotencyConflict


class OwnerWhatsAppMirror:
    def __init__(self, database: str, *, tenant: str, owner_destination: str,
                 max_rows: int = 10000, retention_days: int = 30,
                 clock=None):
        if (not isinstance(tenant, str) or not tenant.strip()
                or not isinstance(owner_destination, str)
                or not owner_destination.strip()):
            raise ValueError("whatsapp_mirror_owner_scope_required")
        if type(max_rows) is not int or not 100 <= max_rows <= 100000:
            raise ValueError("whatsapp_mirror_capacity_invalid")
        if type(retention_days) is not int or not 1 <= retention_days <= 365:
            raise ValueError("whatsapp_mirror_retention_invalid")
        self.database = database
        self.tenant = tenant
        self.owner_destination = owner_destination
        self.max_rows = max_rows
        self.retention_days = retention_days
        self.clock = clock or time.time
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS whatsapp_owner_mirror (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant TEXT NOT NULL,
                    owner_destination TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    received_at INTEGER NOT NULL,
                    UNIQUE (tenant, owner_destination, external_id)
                );
                CREATE INDEX IF NOT EXISTS whatsapp_owner_mirror_scope
                  ON whatsapp_owner_mirror
                  (tenant,owner_destination,seq);
                CREATE INDEX IF NOT EXISTS whatsapp_owner_mirror_retention
                  ON whatsapp_owner_mirror (received_at);
            """)

    @contextmanager
    def _db(self):
        with closing(sqlite3.connect(self.database, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def purge_expired(self) -> int:
        """Explicit host maintenance; no other tenant or queue is touched."""
        before = int(self.clock()) - self.retention_days * 86400
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                "DELETE FROM whatsapp_owner_mirror "
                "WHERE tenant=? AND owner_destination=? AND received_at<?",
                (self.tenant, self.owner_destination, before),
            )
            return result.rowcount

    def ingest(self, event: InboundEvent) -> dict:
        if (not isinstance(event, InboundEvent)
                or event.tenant_id != self.tenant
                or event.recipient != self.owner_destination
                or event.channel != "whatsapp"
                or event.kind != "text"
                or not event.sender.endswith("@c.us")
                or event.sender.endswith("@g.us")):
            raise PermissionError("whatsapp_mirror_owner_scope_rejected")
        body = json.dumps(
            event.to_dict(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )
        digest = sha256(body.encode("utf-8")).hexdigest()
        now = int(self.clock())
        before = now - self.retention_days * 86400
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT digest FROM whatsapp_owner_mirror "
                "WHERE tenant=? AND owner_destination=? AND external_id=?",
                (self.tenant, self.owner_destination, event.external_id),
            ).fetchone()
            if row is not None:
                if row["digest"] != digest:
                    raise IdempotencyConflict("whatsapp_mirror_id_conflict")
                return {"new": False, "state": "MIRRORED"}
            db.execute(
                "DELETE FROM whatsapp_owner_mirror "
                "WHERE tenant=? AND owner_destination=? AND received_at<?",
                (self.tenant, self.owner_destination, before),
            )
            count = db.execute(
                "SELECT COUNT(*) FROM whatsapp_owner_mirror "
                "WHERE tenant=? AND owner_destination=?",
                (self.tenant, self.owner_destination),
            ).fetchone()[0]
            if count >= self.max_rows:
                raise OverflowError("whatsapp_mirror_capacity_exceeded")
            db.execute(
                "INSERT INTO whatsapp_owner_mirror "
                "(tenant,owner_destination,external_id,digest,payload,received_at) "
                "VALUES (?,?,?,?,?,?)",
                (self.tenant, self.owner_destination, event.external_id,
                 digest, body, now),
            )
            return {"new": True, "state": "MIRRORED"}
