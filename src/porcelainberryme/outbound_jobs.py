"""Durable, explicitly approved Internet voice jobs for a supervised host.

The MCP model cannot approve a call. A trusted local process first records
an unguessable approval after checking consent. A separate supervised worker
atomically fences any possible dial *before* invoking the existing LiveKit
outbound agent. Crashes never trigger another unapproved/ambiguous call.
No PSTN, carrier account, external LLM, new agent or public HTTP listener.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
from typing import Callable

_IDENTITY = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
_APPROVAL = re.compile(r"^[a-f0-9]{32}$")
_TERMINAL = frozenset({"FINISHED_UNVERIFIED", "FAILED", "UNKNOWN"})
_STATES = frozenset({
    "APPROVED", "QUEUED", "DISPATCHING", "FINISHED_UNVERIFIED",
    "FAILED", "UNKNOWN", "EXPIRED",
})


class CallApprovalError(ValueError):
    """An approval or call request cannot be admitted."""


def _identity(value: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise CallApprovalError("invalid_approved_internet_identity")
    return value


def _approval(value: str) -> str:
    if not isinstance(value, str) or not _APPROVAL.fullmatch(value):
        raise CallApprovalError("invalid_call_approval_id")
    return value


class DurableCallJobs:
    """Per-file SQLite authority ledger, created only by a trusted host.

    State is monotonic; DISPATCHING is written *before* crossing the remote
    call-origination boundary. No worker can automatically redispatch it,
    including after process death, lost ack or a service restart.
    """

    def __init__(self, database: str | Path, *, clock: Callable[[], float] = time.time):
        path = Path(database)
        if not path.is_absolute() or not path.parent.is_dir():
            raise ValueError("absolute_existing_call_jobs_directory_required")
        if path.is_symlink():
            raise PermissionError("call_jobs_database_symlink_disallowed")
        if not callable(clock):
            raise ValueError("call_jobs_clock_required")
        self.database = path
        self.clock = clock
        if not path.exists():
            # Do not replace an existing file or silently weaken its mode.
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        if (path.is_symlink() or not path.is_file()
                or stat.S_IMODE(path.stat().st_mode) & 0o077):
            raise PermissionError("call_jobs_database_must_be_private")
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS outbound_call_jobs (
                  approval_id TEXT PRIMARY KEY,
                  tenant TEXT NOT NULL,
                  agent TEXT NOT NULL,
                  recipient TEXT NOT NULL,
                  consent_fingerprint TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  expires_at INTEGER NOT NULL,
                  state TEXT NOT NULL CHECK(state IN (
                    'APPROVED','QUEUED','DISPATCHING','FINISHED_UNVERIFIED',
                    'FAILED','UNKNOWN','EXPIRED')),
                  claimed_at INTEGER,
                  completed_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS outbound_calls_claim_idx
                  ON outbound_call_jobs(tenant,agent,state,created_at);
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.database), timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    @staticmethod
    def _public(row: sqlite3.Row) -> dict:
        # No recipient, consent evidence, actor, database path or bearer tokens.
        return {
            "schema": "porcelainberryme.outbound-job.v1",
            "approval_id": row["approval_id"],
            "state": row["state"],
            "call_result_verified": False,
        }

    def approve(self, *, tenant: str, agent: str, recipient: str,
                consent_reference: str, ttl_seconds: int = 120) -> str:
        """Trusted host only: record an independently verified consent event.

        consent_reference is hashed before persistence; it is never returned
        to the LLM/MCP client. The caller is responsible for authenticating
        the user and verifying live call consent outside this method.
        """
        from hashlib import sha256

        tenant, agent, recipient = map(_identity, (tenant, agent, recipient))
        if agent == recipient:
            raise CallApprovalError("cannot_approve_self_dial_to_agent")
        if (not isinstance(consent_reference, str)
                or not 8 <= len(consent_reference) <= 256):
            raise CallApprovalError("host_consent_reference_required")
        if type(ttl_seconds) is not int or not 30 <= ttl_seconds <= 300:
            raise CallApprovalError("invalid_call_approval_ttl")
        now = int(self.clock())
        approval_id = secrets.token_hex(16)
        digest = sha256(consent_reference.encode("utf-8")).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                INSERT INTO outbound_call_jobs
                (approval_id,tenant,agent,recipient,consent_fingerprint,
                 created_at,expires_at,state)
                VALUES(?,?,?,?,?,?,?,'APPROVED')
            """, (approval_id, tenant, agent, recipient, digest,
                  now, now + ttl_seconds))
            db.commit()
        return approval_id

    def _row(self, db, approval_id, tenant, agent, recipient):
        row = db.execute("""
            SELECT * FROM outbound_call_jobs WHERE approval_id=?
        """, (approval_id,)).fetchone()
        if (row is None or row["tenant"] != tenant or row["agent"] != agent
                or row["recipient"] != recipient):
            # Do not reveal whether a differently scoped approval exists.
            raise PermissionError("call_approval_not_authorized")
        return row

    def request(self, *, approval_id: str, tenant: str,
                agent: str, recipient: str) -> dict:
        """MCP may activate an existing, scope-locked approval exactly once."""
        approval_id = _approval(approval_id)
        tenant, agent, recipient = map(_identity, (tenant, agent, recipient))
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, approval_id, tenant, agent, recipient)
            now = int(self.clock())
            if row["state"] == "APPROVED":
                new_state = "EXPIRED" if now >= row["expires_at"] else "QUEUED"
                db.execute(
                    "UPDATE outbound_call_jobs SET state=? WHERE approval_id=?",
                    (new_state, approval_id),
                )
                row = self._row(db, approval_id, tenant, agent, recipient)
            db.commit()
            return self._public(row)

    def status(self, *, approval_id: str, tenant: str,
               agent: str, recipient: str) -> dict:
        approval_id = _approval(approval_id)
        tenant, agent, recipient = map(_identity, (tenant, agent, recipient))
        with self._db() as db:
            return self._public(self._row(
                db, approval_id, tenant, agent, recipient,
            ))

    def claim(self, *, tenant: str, agent: str) -> dict | None:
        """Only workers with a trusted tenant/agent configuration claim work.

        An accepted queue request expires if the worker did not start it in
        the consent window; it is never dialed arbitrarily hours later.
        """
        tenant, agent = _identity(tenant), _identity(agent)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            now = int(self.clock())
            db.execute("""
                UPDATE outbound_call_jobs SET state='EXPIRED'
                WHERE tenant=? AND agent=? AND state='QUEUED'
                  AND expires_at<=?
            """, (tenant, agent, now))
            row = db.execute("""
                SELECT * FROM outbound_call_jobs
                WHERE tenant=? AND agent=? AND state='QUEUED'
                ORDER BY created_at, approval_id LIMIT 1
            """, (tenant, agent)).fetchone()
            if row is None:
                db.commit()
                return None
            db.execute("""
                UPDATE outbound_call_jobs SET state='DISPATCHING', claimed_at=?
                WHERE approval_id=? AND state='QUEUED'
            """, (now, row["approval_id"]))
            db.commit()
            return {
                "approval_id": row["approval_id"],
                "tenant": tenant,
                "agent": agent,
                "recipient": row["recipient"],
            }

    def finish(self, *, approval_id: str, tenant: str, agent: str,
               state: str) -> None:
        if state not in _TERMINAL:
            raise CallApprovalError("invalid_terminal_job_state")
        approval_id = _approval(approval_id)
        tenant, agent = _identity(tenant), _identity(agent)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("""
                UPDATE outbound_call_jobs
                   SET state=?, completed_at=?
                 WHERE approval_id=? AND tenant=? AND agent=?
                   AND state='DISPATCHING'
            """, (state, int(self.clock()), approval_id, tenant, agent)).rowcount
            if count != 1:
                db.rollback()
                raise CallApprovalError("outbound_job_not_in_dispatch")
            db.commit()

    def reconcile_stale(self, *, tenant: str, agent: str,
                        after_seconds: int = 7200) -> int:
        """Safe, status-only crash recovery: never redispatch unknown calls."""
        if type(after_seconds) is not int or after_seconds < 3900:
            raise CallApprovalError("invalid_stale_job_timeout")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute("""
                UPDATE outbound_call_jobs
                   SET state='UNKNOWN', completed_at=?
                 WHERE tenant=? AND agent=? AND state='DISPATCHING'
                   AND claimed_at<=?
            """, (int(self.clock()), _identity(tenant), _identity(agent),
                  int(self.clock()) - after_seconds))
            db.commit()
            return result.rowcount


async def worker_once(store: DurableCallJobs, *, tenant: str, agent: str,
                      identity_secret: str, voice_settings: dict,
                      voice_worker=None) -> bool:
    """Claim one job, originate at most once, never infer human delivery.

    The trusted supervisor holds the signing secret. Only the existing call
    registry verifies target enrollment/opt-in/DND/cooldown and invites the
    approved Internet identity. The worker cannot approve its own calls.
    """
    if not isinstance(identity_secret, str) or len(identity_secret) < 32:
        raise ValueError("trusted_agent_identity_secret_required")
    if not isinstance(voice_settings, dict):
        raise ValueError("worker_local_voice_settings_required")
    job = await asyncio.to_thread(store.claim, tenant=tenant, agent=agent)
    if job is None:
        return False
    if voice_worker is None:
        from .voice_livekit_agent import call_human
        voice_worker = call_human
    from .ip_calls import mint_identity_token
    try:
        token = mint_identity_token(identity_secret, tenant, agent, "agent")
        await voice_worker(
            target=job["recipient"], tenant=tenant, agent_id=agent,
            control_token=token, request_id=job["approval_id"],
            **voice_settings,
        )
    except asyncio.CancelledError:
        await asyncio.shield(asyncio.to_thread(
            store.finish, approval_id=job["approval_id"],
            tenant=tenant, agent=agent, state="UNKNOWN",
        ))
        raise
    except Exception:
        # Network ambiguity and model failures must NEVER cause blind redial.
        await asyncio.to_thread(
            store.finish, approval_id=job["approval_id"],
            tenant=tenant, agent=agent, state="FAILED",
        )
    else:
        await asyncio.to_thread(
            store.finish, approval_id=job["approval_id"],
            tenant=tenant, agent=agent, state="FINISHED_UNVERIFIED",
        )
    return True


async def serve_worker(store: DurableCallJobs, *, tenant: str, agent: str,
                       identity_secret: str, voice_settings: dict,
                       poll_seconds: float = 1.0,
                       stop_event: asyncio.Event | None = None) -> None:
    if not isinstance(poll_seconds, (float, int)) or not 0.2 <= poll_seconds <= 5:
        raise ValueError("invalid_call_worker_poll_interval")
    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        worked = await worker_once(
            store, tenant=tenant, agent=agent,
            identity_secret=identity_secret, voice_settings=voice_settings,
        )
        if not worked:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
            except asyncio.TimeoutError:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Host-only durable voice approvals and worker")
    sub = parser.add_subparsers(dest="command", required=True)
    approve = sub.add_parser("approve", help="TRUSTED HOST ONLY: record verified human consent")
    approve.add_argument("--tenant", required=True)
    approve.add_argument("--agent", required=True)
    approve.add_argument("--recipient", required=True)
    approve.add_argument("--consent-reference", required=True)
    approve.add_argument("--ttl", type=int, default=120)
    worker = sub.add_parser("work", help="Run supervised local outbound voice worker")
    worker.add_argument("--tenant", required=True)
    worker.add_argument("--agent", required=True)
    worker.add_argument("--once", action="store_true")
    args = parser.parse_args()
    database = os.environ.get("PB_CALL_JOBS_DB", "")
    if not database:
        parser.error("PB_CALL_JOBS_DB absolute private SQLite path required")
    store = DurableCallJobs(database)
    if args.command == "approve":
        if os.environ.get("PB_CALL_APPROVAL_HOST_ENABLED") != "1":
            parser.error("host approval disabled; explicit trusted operator required")
        approval_id = store.approve(
            tenant=args.tenant, agent=args.agent, recipient=args.recipient,
            consent_reference=args.consent_reference, ttl_seconds=args.ttl,
        )
        # The approval token is a secret: only operator stdout; never logfile.
        print(approval_id)
        return
    secret = os.environ.get("PB_CALL_IDENTITY_SECRET", "")
    voice_settings = {
        "whisper_model": os.environ.get("PB_LOCAL_WHISPER_DIR", ""),
        "ollama_model": os.environ.get("PB_LOCAL_OLLAMA_MODEL", ""),
        "piper_binary": os.environ.get("PB_LOCAL_PIPER_BIN", ""),
        "piper_model": os.environ.get("PB_LOCAL_PIPER_MODEL", ""),
    }
    if (len(secret) < 32 or
            any(not voice_settings[k] for k in (
                "whisper_model", "piper_binary", "piper_model"))
            or not (voice_settings["ollama_model"]
                    or os.environ.get("PB_LOCAL_VOICE_CHAT_MODEL")
                    or os.environ.get("PB_AGENT_KNOWLEDGE_DB"))):
        parser.error("trusted signing secret and installed local voice models required")
    if args.once:
        asyncio.run(worker_once(
            store, tenant=args.tenant, agent=args.agent,
            identity_secret=secret, voice_settings=voice_settings,
        ))
    else:
        asyncio.run(serve_worker(
            store, tenant=args.tenant, agent=args.agent,
            identity_secret=secret, voice_settings=voice_settings,
        ))


if __name__ == "__main__":
    main()
