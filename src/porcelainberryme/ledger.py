"""Durable inbound/outbound event ledger; no blind retry of ambiguous effects.

Exactly-once remote side effects are NOT promised. Inbound claim tokens exclude
concurrent consumers; after an interrupted dispatch, UNKNOWN requires a human
or authoritative external receipt before reprocessing.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading

from .contracts import InboundEvent, OutboundMessage, fingerprint


class IdempotencyConflict(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ClaimedEvent:
    event: InboundEvent
    token: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    def __init__(self, database: str | Path):
        self.database = str(database)
        location = Path(self.database)
        location.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(location, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            location.chmod(0o600)
        else:
            os.close(fd)
        self._lock = threading.RLock()
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS inbound (
                    tenant_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'PENDING',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, channel, external_id)
                );
                CREATE TABLE IF NOT EXISTS outbound (
                    tenant_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    provider_receipt TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, idempotency_key)
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(inbound)")}
            # Non-destructive upgrade of v0.1 databases.
            if "claim_token" not in columns:
                db.execute("ALTER TABLE inbound ADD COLUMN claim_token TEXT")
            if "updated_at" not in columns:
                db.execute("ALTER TABLE inbound ADD COLUMN updated_at TEXT")
            if "processing_receipt" not in columns:
                db.execute("ALTER TABLE inbound ADD COLUMN processing_receipt TEXT")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            with db:
                yield db
        finally:
            db.close()

    def ingest(self, event: InboundEvent) -> dict:
        payload = event.to_dict()
        digest = fingerprint(payload)
        with self._lock, self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO inbound "
                "(tenant_id,channel,external_id,payload,digest,state,created_at,updated_at) "
                "VALUES (?,?,?,?,?,'PENDING',?,?)",
                (event.tenant_id, event.channel, event.external_id,
                 json.dumps(payload, ensure_ascii=False), digest, _now(), _now()),
            )
            inserted = db.execute("SELECT changes()").fetchone()[0] == 1
            row = db.execute(
                "SELECT digest,state FROM inbound WHERE tenant_id=? AND channel=? AND external_id=?",
                (event.tenant_id, event.channel, event.external_id),
            ).fetchone()
            if row["digest"] != digest:
                raise IdempotencyConflict("inbound_id_reused_with_different_payload")
            return {"new": inserted, "state": row["state"]}

    def pending(self, *, tenant_id: str, limit: int = 50) -> list[InboundEvent]:
        """Read-only per-tenant projection, NOT a safe processing claim."""
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_scope_required")
        if not 1 <= limit <= 500:
            raise ValueError("invalid_limit")
        with self._lock, self._db() as db:
            rows = db.execute(
                "SELECT payload FROM inbound WHERE tenant_id=? AND state='PENDING' "
                "ORDER BY created_at, rowid LIMIT ?", (tenant_id, limit)
            ).fetchall()
        return [InboundEvent.from_dict(json.loads(row["payload"])) for row in rows]

    def claim_pending(self, *, tenant_id: str, channel: str | None = None) -> ClaimedEvent | None:
        """Atomically claim one event; safe across processes sharing this database.

        The caller must provide a tenant scope established independently of the
        message, never from an untrusted request body alone.
        """
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_scope_required")
        if channel is not None and channel not in ("email", "whatsapp", "sms", "phone"):
            raise ValueError("invalid_channel")
        with self._lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            params: tuple = (tenant_id,)
            query = "SELECT channel,external_id,payload FROM inbound WHERE tenant_id=? AND state='PENDING'"
            if channel is not None:
                query += " AND channel=?"
                params += (channel,)
            row = db.execute(query + " ORDER BY created_at, rowid LIMIT 1", params).fetchone()
            if row is None:
                return None
            token = secrets.token_hex(24)
            updated = db.execute(
                "UPDATE inbound SET state='PROCESSING',claim_token=?,updated_at=? "
                "WHERE tenant_id=? AND channel=? AND external_id=? AND state='PENDING'",
                (token, _now(), tenant_id, row["channel"], row["external_id"]),
            )
            if updated.rowcount != 1:
                raise RuntimeError("inbound_claim_race")
            return ClaimedEvent(InboundEvent.from_dict(json.loads(row["payload"])), token)

    def acknowledge_claim(self, claim: ClaimedEvent, *, state: str,
                          receipt: str | None = None) -> bool:
        if state not in ("HANDLED", "UNKNOWN"):
            raise ValueError("invalid_inbound_state")
        if receipt is not None and (not isinstance(receipt, str) or not 1 <= len(receipt) <= 512):
            raise ValueError("invalid_processing_receipt")
        if state == "HANDLED" and not receipt:
            raise ValueError("handled_receipt_required")
        event = claim.event
        with self._lock, self._db() as db:
            updated = db.execute(
                "UPDATE inbound SET state=?,processing_receipt=?,updated_at=? "
                "WHERE tenant_id=? AND channel=? AND external_id=? "
                "AND state='PROCESSING' AND claim_token=?",
                (state, receipt, _now(), event.tenant_id, event.channel,
                 event.external_id, claim.token),
            )
            return updated.rowcount == 1

    def recover_interrupted_inbound(self, *, tenant_id: str) -> int:
        """Operator-controlled startup recovery: uncertain work is NOT resent."""
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_scope_required")
        with self._lock, self._db() as db:
            cursor = db.execute(
                "UPDATE inbound SET state='UNKNOWN',updated_at=? "
                "WHERE tenant_id=? AND state='PROCESSING'",
                (_now(), tenant_id),
            )
            return cursor.rowcount

    def inbound_status(self, *, tenant_id: str, channel: str, external_id: str) -> dict | None:
        if not all(isinstance(s, str) and s.strip() for s in (tenant_id, channel, external_id)):
            raise ValueError("inbound_identity_required")
        with self._lock, self._db() as db:
            row = db.execute(
                "SELECT state,processing_receipt FROM inbound "
                "WHERE tenant_id=? AND channel=? AND external_id=?",
                (tenant_id, channel, external_id)
            ).fetchone()
            return dict(row) if row else None

    def mark_inbound(self, event: InboundEvent, state: str) -> bool:
        """Legacy supervised acknowledgement, only from PENDING, never claimed work.

        New integrations MUST use claim_pending + acknowledge_claim. This old
        method stays for v0.1 callers but is not used in production flow.
        """
        if state not in ("HANDLED", "UNKNOWN"):
            raise ValueError("invalid_inbound_state")
        with self._lock, self._db() as db:
            cursor = db.execute(
                "UPDATE inbound SET state=?,updated_at=? WHERE tenant_id=? AND channel=? "
                "AND external_id=? AND state='PENDING'",
                (state, _now(), event.tenant_id, event.channel, event.external_id),
            )
            return cursor.rowcount == 1

    def reserve(self, message: OutboundMessage) -> dict:
        digest = fingerprint(message.to_dict())
        now = _now()
        with self._lock, self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO outbound "
                "(tenant_id,idempotency_key,channel,digest,state,created_at,updated_at) "
                "VALUES (?,?,?,?,'PENDING',?,?)",
                (message.tenant_id, message.idempotency_key, message.channel,
                 digest, now, now),
            )
            inserted = db.execute("SELECT changes()").fetchone()[0] == 1
            row = db.execute(
                "SELECT digest,state,provider_receipt FROM outbound "
                "WHERE tenant_id=? AND idempotency_key=?",
                (message.tenant_id, message.idempotency_key),
            ).fetchone()
            if row["digest"] != digest:
                raise IdempotencyConflict("outbound_key_reused_with_different_payload")
            return {"new": inserted, "state": row["state"],
                    "provider_receipt": row["provider_receipt"]}

    def claim(self, message: OutboundMessage) -> bool:
        with self._lock, self._db() as db:
            cursor = db.execute(
                "UPDATE outbound SET state='ATTEMPTING',updated_at=? "
                "WHERE tenant_id=? AND idempotency_key=? AND state='PENDING'",
                (_now(), message.tenant_id, message.idempotency_key),
            )
            return cursor.rowcount == 1

    def finish(self, message: OutboundMessage, *, state: str,
               receipt: str | None = None) -> None:
        if state not in ("ACCEPTED", "FAILED", "UNKNOWN"):
            raise ValueError("invalid_outbound_state")
        with self._lock, self._db() as db:
            cursor = db.execute(
                "UPDATE outbound SET state=?,provider_receipt=?,updated_at=? "
                "WHERE tenant_id=? AND idempotency_key=? AND state='ATTEMPTING'",
                (state, receipt, _now(), message.tenant_id, message.idempotency_key),
            )
            if cursor.rowcount != 1:
                raise ValueError("outbound_not_claimed")

    def outbound_status(self, tenant_id: str, idempotency_key: str) -> dict | None:
        with self._lock, self._db() as db:
            row = db.execute(
                "SELECT state,provider_receipt FROM outbound "
                "WHERE tenant_id=? AND idempotency_key=?",
                (tenant_id, idempotency_key),
            ).fetchone()
            return dict(row) if row else None

    def recover_interrupted(self) -> int:
        """Outbound startup reconciliation only, no automatic retransmission."""
        with self._lock, self._db() as db:
            cursor = db.execute(
                "UPDATE outbound SET state='UNKNOWN', updated_at=? "
                "WHERE state='ATTEMPTING'", (_now(),),
            )
            return cursor.rowcount
