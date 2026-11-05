"""Durable owner WhatsApp mirror outbox; no second WhatsApp session or consumer.

The existing authenticated n8n/Aurora handler enqueues before any network I/O.
A separately supervised drain worker retries the *same* signed observation,
using the provider's stable message ID. PorcelainberryMe deduplicates by that
ID and rejects a changed payload, so a lost acknowledgement can be retried.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time


def private_database(path: str, *, create: bool = True) -> str:
    if not isinstance(path, str) or not path or not Path(path).is_absolute():
        raise ValueError("pb_mirror_absolute_outbox_required")
    if create:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                         getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise PermissionError("pb_mirror_outbox_missing") from None
    if (not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_uid != os.geteuid()):
        raise PermissionError("pb_mirror_outbox_not_private")
    return path


class OwnerWhatsAppMirrorOutbox:
    """Fixed-identity local spool with crash-safe claims and bounded backlog."""

    def __init__(self, database: str, *, tenant: str, owner: str,
                 clock=None, capacity: int = 10000, retention_days: int = 30):
        if (not isinstance(tenant, str) or not tenant.strip()
                or not isinstance(owner, str) or not owner.strip()
                or len(tenant) > 256 or len(owner) > 256):
            raise ValueError("pb_mirror_outbox_owner_required")
        if type(capacity) is not int or not 100 <= capacity <= 100000:
            raise ValueError("pb_mirror_outbox_capacity_invalid")
        if type(retention_days) is not int or not 1 <= retention_days <= 365:
            raise ValueError("pb_mirror_outbox_retention_invalid")
        self.retention_days = retention_days
        self.tenant, self.owner = tenant, owner
        self.database = private_database(database)
        self.clock = clock or time.time
        self.capacity = capacity
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS owner_whatsapp_mirror_outbox (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant TEXT NOT NULL, owner TEXT NOT NULL,
                    external_id TEXT NOT NULL, digest TEXT NOT NULL,
                    payload TEXT NOT NULL, state TEXT NOT NULL,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt INTEGER NOT NULL,
                    lease_until INTEGER NOT NULL DEFAULT 0,
                    claim_token TEXT NOT NULL DEFAULT '',
                    UNIQUE (tenant, owner, external_id)
                );
                CREATE INDEX IF NOT EXISTS owner_whatsapp_mirror_due
                    ON owner_whatsapp_mirror_outbox
                    (tenant,owner,state,next_attempt,lease_until,seq);
                CREATE TABLE IF NOT EXISTS owner_whatsapp_mirror_worker (
                    tenant TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    last_seen INTEGER NOT NULL,
                    PRIMARY KEY (tenant, owner)
                );
            """)

    @contextmanager
    def _db(self):
        private_database(self.database, create=False)
        with closing(sqlite3.connect(self.database, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def enqueue(self, event: dict) -> dict:
        if (not isinstance(event, dict)
                or event.get("tenant_id") != self.tenant
                or event.get("recipient") != self.owner
                or event.get("channel") != "whatsapp"
                or event.get("kind") != "text"
                or not isinstance(event.get("sender"), str)
                or not event["sender"].endswith("@c.us")
                or not isinstance(event.get("external_id"), str)
                or not event["external_id"]):
            raise PermissionError("pb_mirror_outbox_scope_mismatch")
        payload = json.dumps(
            event, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        now = int(self.clock())
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT digest,state FROM owner_whatsapp_mirror_outbox "
                "WHERE tenant=? AND owner=? AND external_id=?",
                (self.tenant, self.owner, event["external_id"]),
            ).fetchone()
            if row:
                if row["digest"] != digest:
                    raise ValueError("pb_mirror_outbox_id_conflict")
                return {"new": False, "state": row["state"]}
            # Retain a replay receipt for at least the configured window;
            # clear only previously ACKED rows, never uncertain work.
            db.execute(
                "DELETE FROM owner_whatsapp_mirror_outbox "
                "WHERE tenant=? AND owner=? AND state='ACKED' "
                "AND updated_at<?",
                (self.tenant, self.owner,
                 now - self.retention_days * 86400),
            )
            retained = db.execute(
                "SELECT COUNT(*) FROM owner_whatsapp_mirror_outbox "
                "WHERE tenant=? AND owner=?",
                (self.tenant, self.owner),
            ).fetchone()[0]
            if retained >= self.capacity:
                raise OverflowError("pb_mirror_outbox_full")
            db.execute(
                "INSERT INTO owner_whatsapp_mirror_outbox "
                "(tenant,owner,external_id,digest,payload,state,"
                "created_at,updated_at,next_attempt) "
                "VALUES (?,?,?,?,?,'PENDING',?,?,?)",
                (self.tenant, self.owner, event["external_id"],
                 digest, payload, now, now, now),
            )
            return {"new": True, "state": "PENDING"}

    def drain_one(self, dispatch) -> dict | None:
        """Claim one due entry. Retry is safe only against PB's idempotent mirror."""
        if not callable(dispatch):
            raise ValueError("pb_mirror_dispatch_required")
        now = int(self.clock())
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM owner_whatsapp_mirror_outbox "
                "WHERE tenant=? AND owner=? AND "
                "((state='PENDING' AND next_attempt<=?) OR "
                "(state='INFLIGHT' AND lease_until<=?)) "
                "ORDER BY seq LIMIT 1",
                (self.tenant, self.owner, now, now),
            ).fetchone()
            if row is None:
                return None
            token = secrets.token_hex(16)
            db.execute(
                "UPDATE owner_whatsapp_mirror_outbox "
                "SET state='INFLIGHT',lease_until=?,claim_token=?,"
                "updated_at=?,attempts=attempts+1 WHERE seq=?",
                (now + 30, token, now, row["seq"]),
            )
            # Verify the original durable content and owner fence BEFORE
            # any network call. Malformed/mutated records must not crash the
            # worker in a restart loop or be forwarded to another owner.
            raw = row["payload"]
            try:
                message = json.loads(raw)
                valid = (
                    isinstance(message, dict)
                    and isinstance(raw, str)
                    and hashlib.sha256(raw.encode("utf-8")).hexdigest() == row["digest"]
                    and message.get("tenant_id") == self.tenant
                    and message.get("recipient") == self.owner
                    and message.get("external_id") == row["external_id"]
                    and message.get("channel") == "whatsapp"
                    and message.get("kind") == "text"
                    and isinstance(message.get("sender"), str)
                    and message["sender"].endswith("@c.us")
                    and isinstance(message.get("text"), str)
                    and bool(message["text"].strip())
                )
            except (ValueError, TypeError, UnicodeError):
                valid = False
            if not valid:
                db.execute(
                    "UPDATE owner_whatsapp_mirror_outbox "
                    "SET state='QUARANTINED',lease_until=0,claim_token='',"
                    "updated_at=? WHERE seq=? AND state='INFLIGHT' "
                    "AND claim_token=?",
                    (now, row["seq"], token),
                )
                return {"state": "QUARANTINED", "acknowledged": False}
            attempt_number = row["attempts"] + 1
        delivered = False
        try:
            delivered = dispatch(message) is True
        except Exception:
            # Provider exceptions and contacts must never be logged or
            # returned to MCP. Unconfirmed requests are safe to replay at PB.
            delivered = False
        state = "ACKED" if delivered else "PENDING"
        backoff = min(300, 2 ** min(attempt_number, 8))
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute(
                "UPDATE owner_whatsapp_mirror_outbox "
                "SET state=?,next_attempt=?,lease_until=0,"
                "claim_token='',updated_at=? "
                "WHERE seq=? AND state='INFLIGHT' AND claim_token=?",
                (state, now if delivered else now + backoff,
                 int(self.clock()), row["seq"], token),
            )
            if updated.rowcount != 1:
                return {"state": "UNKNOWN", "acknowledged": False}
        return {"state": state, "acknowledged": delivered}

    def heartbeat(self) -> None:
        """Only a running replay worker updates its private liveness record."""
        now = int(self.clock())
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO owner_whatsapp_mirror_worker "
                "(tenant,owner,last_seen) VALUES (?,?,?) "
                "ON CONFLICT(tenant,owner) DO UPDATE SET last_seen=excluded.last_seen",
                (self.tenant, self.owner, now),
            )

    def stats(self) -> dict:
        now = int(self.clock())
        with self._db() as db:
            rows = db.execute(
                "SELECT state,COUNT(*) AS count,MIN(created_at) AS oldest "
                "FROM owner_whatsapp_mirror_outbox WHERE tenant=? AND owner=? "
                "GROUP BY state",
                (self.tenant, self.owner),
            ).fetchall()
            heartbeat = db.execute(
                "SELECT last_seen FROM owner_whatsapp_mirror_worker "
                "WHERE tenant=? AND owner=?",
                (self.tenant, self.owner),
            ).fetchone()
        last_seen = heartbeat["last_seen"] if heartbeat else None
        worker_age = (now - last_seen) if last_seen is not None else None
        counters = {item["state"]: item["count"] for item in rows}
        outstanding = [item["oldest"] for item in rows
                       if item["state"] in ("PENDING", "INFLIGHT")]
        return {
            "schema": "tonnestate.whatsapp-mirror-outbox.v1",
            "pending": counters.get("PENDING", 0),
            "inflight": counters.get("INFLIGHT", 0),
            "acknowledged": counters.get("ACKED", 0),
            "quarantined": counters.get("QUARANTINED", 0),
            "oldest_pending_seconds": max(0, now - min(outstanding)) if outstanding else 0,
            "worker_heartbeat_age_seconds": worker_age,
            "worker_healthy": worker_age is not None and 0 <= worker_age <= 60,
        }

    def prune_confirmed(self, *, after_days: int = 30) -> int:
        if type(after_days) is not int or not 1 <= after_days <= 365:
            raise ValueError("pb_mirror_retention_invalid")
        cutoff = int(self.clock()) - after_days * 86400
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                "DELETE FROM owner_whatsapp_mirror_outbox "
                "WHERE tenant=? AND owner=? AND state='ACKED' "
                "AND updated_at<?",
                (self.tenant, self.owner, cutoff),
            )
            return result.rowcount


def main():
    """Run as a separate supervised process, not as another WhatsApp consumer."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("drain", "status", "check", "prune"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-batch", type=int, default=100)
    parser.add_argument("--max-delay", type=int, default=300)
    args = parser.parse_args()
    if os.environ.get("TE_PB_WHATSAPP_MIRROR_ENABLED") != "1":
        parser.error("owner WhatsApp mirror is disabled")
    database = os.environ.get("TE_PB_WHATSAPP_OUTBOX_DB", "")
    tenant = os.environ.get("TE_PB_WHATSAPP_OWNER_TENANT", "")
    owner = os.environ.get("TE_PB_WHATSAPP_OWNER_ID", "")
    store = OwnerWhatsAppMirrorOutbox(
        database, tenant=tenant, owner=owner,
    )
    if args.max_batch < 1 or args.max_batch > 1000:
        parser.error("max-batch must be 1..1000")
    if args.max_delay < 30 or args.max_delay > 86400:
        parser.error("max-delay must be 30..86400")
    if args.command in ("status", "check"):
        metrics = store.stats()
        print(json.dumps(metrics, sort_keys=True))
        if args.command == "check" and (
                not metrics["worker_healthy"]
                or metrics["quarantined"] > 0
                or metrics["oldest_pending_seconds"] > args.max_delay):
            raise SystemExit(2)
        return
    if args.command == "prune":
        print(json.dumps({"pruned": store.prune_confirmed()}))
        return
    from .porcelainberry_whatsapp_mirror import _signed_push
    processed = 0
    last_heartbeat = -1.0
    while True:
        monotonic_now = time.monotonic()
        if last_heartbeat < 0 or monotonic_now - last_heartbeat >= 10:
            store.heartbeat()
            last_heartbeat = monotonic_now
        result = store.drain_one(_signed_push)
        if result is None:
            if args.once:
                return
            time.sleep(2)
            continue
        processed += 1
        if args.once and processed >= args.max_batch:
            return


if __name__ == "__main__":
    main()
