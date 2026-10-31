"""SQLite transport ledger with explicit at-most-one automatic send attempt.

ACCEPTED means accepted by the provider, not delivered to the human.
UNKNOWN means a network timeout could have happened after dispatch; never
silently retry it. A trusted operator must reconcile ambiguous external effects.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading

from .contracts import InboundEvent, OutboundMessage, fingerprint


class IdempotencyConflict(ValueError):
    pass


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
                "(tenant_id,channel,external_id,payload,digest,state,created_at) "
                "VALUES (?,?,?,?,?,'PENDING',?)",
                (event.tenant_id, event.channel, event.external_id,
                 json.dumps(payload, ensure_ascii=False), digest, _now()),
            )
            inserted = db.execute("SELECT changes()").fetchone()[0] == 1
            row = db.execute(
                "SELECT digest,state FROM inbound WHERE tenant_id=? AND channel=? AND external_id=?",
                (event.tenant_id, event.channel, event.external_id),
            ).fetchone()
            if row["digest"] != digest:
                raise IdempotencyConflict("inbound_id_reused_with_different_payload")
            return {"new": inserted, "state": row["state"]}

    def pending(self, *, limit: int = 50) -> list[InboundEvent]:
        if not 1 <= limit <= 500:
            raise ValueError("invalid_limit")
        with self._lock, self._db() as db:
            rows = db.execute(
                "SELECT payload FROM inbound WHERE state='PENDING' "
                "ORDER BY created_at, rowid LIMIT ?", (limit,)
            ).fetchall()
        return [InboundEvent.from_dict(json.loads(row["payload"])) for row in rows]

    def mark_inbound(self, event: InboundEvent, state: str) -> bool:
        if state not in ("HANDLED", "UNKNOWN"):
            raise ValueError("invalid_inbound_state")
        with self._lock, self._db() as db:
            cursor = db.execute(
                "UPDATE inbound SET state=? WHERE tenant_id=? AND channel=? "
                "AND external_id=? AND state='PENDING'",
                (state, event.tenant_id, event.channel, event.external_id),
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
        """Called only at process startup; no automatic retransmission."""
        with self._lock, self._db() as db:
            cursor = db.execute(
                "UPDATE outbound SET state='UNKNOWN', updated_at=? "
                "WHERE state='ATTEMPTING'", (_now(),),
            )
            return cursor.rowcount
