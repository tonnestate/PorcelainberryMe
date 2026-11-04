"""Trusted inbound AI receptionist with a durable, fenced worker lease.

A human can call a locally enabled AI identity through PorcelainberryMe's
existing Internet-only call registry. The AI must claim an inbound invitation
atomically before joining WebRTC. Another AI worker cannot claim the same
live session; after a crash a worker can reclaim when the short lease expires.

No public phone number, PSTN provider, paid AI or public enrollment endpoint.
Only the trusted operator may enable an inbound AI identity in SQLite.
"""
from __future__ import annotations

from contextlib import closing
import secrets

INBOUND_LEASE_SECONDS = 15


class InboundAgentMixin:
    def _init_inbound_schema(self, db):
        db.executescript("""
            CREATE TABLE IF NOT EXISTS ip_inbound_agents (
                tenant TEXT NOT NULL,
                assistant TEXT NOT NULL,
                enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                PRIMARY KEY(tenant,assistant)
            );
            CREATE TABLE IF NOT EXISTS ip_inbound_leases (
                call_id TEXT PRIMARY KEY,
                tenant TEXT NOT NULL,
                assistant TEXT NOT NULL,
                lease TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                claimed_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ip_inbound_leases_owner
                ON ip_inbound_leases(tenant,assistant,expires_at);
        """)

    def configure_inbound_agent(self, tenant: str, assistant: str,
                                enabled: bool) -> None:
        """Local trusted operator action; not available over the HTTP API."""
        from .ip_calls import CallError, _identity
        _identity(tenant)
        _identity(assistant)
        if type(enabled) is not bool:
            raise CallError("invalid_inbound_agent_setting")
        with closing(self._conn()) as db:
            with db:
                row = db.execute(
                    "SELECT 1 FROM ip_call_users "
                    "WHERE tenant=? AND subject=?",
                    (tenant, assistant),
                ).fetchone()
                if row is None:
                    raise CallError("inbound_agent_not_enrolled", 404)
                db.execute(
                    "INSERT INTO ip_inbound_agents(tenant,assistant,enabled) "
                    "VALUES(?,?,?) ON CONFLICT(tenant,assistant) "
                    "DO UPDATE SET enabled=excluded.enabled",
                    (tenant, assistant, int(enabled)),
                )
                if not enabled:
                    # Withdrawal must cancel already ringing customers too.
                    # Established active sessions remain under their lease
                    # and are handled by their normal media lifecycle.
                    self._revoke_pending_contact(
                        db, tenant, assistant, now=self._now(),
                        block_all=True, block_agents=True,
                    )

    def _check_inbound(self, db, claims, call, *, require_enabled=True):
        from .ip_calls import CallError
        if (claims["role"] != "agent"
                or claims["sub"] != call["callee"]
                or claims["tenant"] != call["tenant"]
                or call["agent"]):
            raise CallError("inbound_agent_not_authorized", 403)
        row = db.execute(
            "SELECT enabled FROM ip_inbound_agents "
            "WHERE tenant=? AND assistant=?",
            (claims["tenant"], claims["sub"]),
        ).fetchone()
        if require_enabled and (row is None or not row["enabled"]):
            raise CallError("inbound_agent_not_enabled", 403)

    def _is_inbound_transferred(self, db, call_id):
        handoff = self._handoff_row(db, call_id)
        return handoff is not None and handoff["state"] == "CONNECTED"

    def claim_inbound(self, claims, call_id: str):
        """Claim once; takeover only after previous lease has expired."""
        from .ip_calls import CallError, MAX_CALL_SECONDS
        now = self._now()
        with closing(self._open()) as db:
            with db:
                call = self._get(db, claims["tenant"], claims["sub"], call_id)
                self._check_inbound(db, claims, call)
                if call["state"] not in ("RINGING", "ACTIVE"):
                    raise CallError("inbound_call_unavailable", 409)
                handoff = self._handoff_row(db, call_id)
                if handoff is not None and handoff["state"] == "CONNECTED":
                    raise CallError("inbound_already_transferred", 409)
                prior = db.execute(
                    "SELECT expires_at FROM ip_inbound_leases WHERE call_id=?",
                    (call_id,),
                ).fetchone()
                if prior is not None and prior["expires_at"] > now:
                    raise CallError("inbound_lease_held", 409)
                lease = secrets.token_hex(16)
                if call["state"] == "RINGING":
                    db.execute(
                        "UPDATE ip_calls SET state='ACTIVE',expires_at=?,closed_at=NULL "
                        "WHERE id=?",
                        (now + MAX_CALL_SECONDS, call_id),
                    )
                    self._event(db, call, call["caller"], "ACTIVE", now)
                    self._event(db, call, call["callee"], "ACTIVE", now)
                db.execute(
                    "INSERT INTO ip_inbound_leases("
                    "call_id,tenant,assistant,lease,expires_at,claimed_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(call_id) DO UPDATE SET "
                    "lease=excluded.lease,expires_at=excluded.expires_at,"
                    "claimed_at=excluded.claimed_at",
                    (call_id, claims["tenant"], claims["sub"], lease,
                     now + INBOUND_LEASE_SECONDS, now),
                )
                return {
                    "id": call_id, "state": "ACTIVE",
                    "peer": call["caller"],
                    "lease": lease, "lease_expires_at": now + INBOUND_LEASE_SECONDS,
                    "recovered": call["state"] == "ACTIVE",
                }

    def heartbeat_inbound(self, claims, call_id: str, lease: str):
        """Fence old workers even when the customer call is still ACTIVE."""
        from .ip_calls import CallError, CALL_ID
        if not isinstance(call_id, str) or not CALL_ID.fullmatch(call_id):
            raise CallError("call_not_found", 404)
        if (not isinstance(lease, str) or len(lease) != 32
                or any(c not in "0123456789abcdef" for c in lease)):
            raise CallError("invalid_inbound_lease", 403)
        now = self._now()
        with closing(self._open()) as db:
            with db:
                call = self._get(db, claims["tenant"], claims["sub"], call_id)
                self._check_inbound(db, claims, call)
                row = db.execute(
                    "SELECT lease,expires_at FROM ip_inbound_leases "
                    "WHERE call_id=?", (call_id,),
                ).fetchone()
                if (call["state"] != "ACTIVE" or row is None
                        or row["lease"] != lease or row["expires_at"] <= now
                        or self._is_inbound_transferred(db, call_id)):
                    raise CallError("inbound_lease_lost", 409)
                expiry = now + INBOUND_LEASE_SECONDS
                db.execute(
                    "UPDATE ip_inbound_leases SET expires_at=? WHERE call_id=?",
                    (expiry, call_id),
                )
                return {"active": True, "lease_expires_at": expiry}

    def finish_inbound(self, claims, call_id: str, lease: str):
        """Only the current worker may end the call after its media exits.

        After a confirmed human handoff, preserve the customer/staff call.
        A delayed, fenced-out old worker cannot end a new worker's call.
        """
        from .ip_calls import CallError
        now = self._now()
        with closing(self._open()) as db:
            with db:
                call = self._get(db, claims["tenant"], claims["sub"], call_id)
                self._check_inbound(db, claims, call, require_enabled=False)
                row = db.execute(
                    "SELECT lease,expires_at FROM ip_inbound_leases WHERE call_id=?",
                    (call_id,),
                ).fetchone()
                if not isinstance(lease, str) or row is None or row["lease"] != lease:
                    raise CallError("inbound_lease_lost", 409)
                # Even if nobody has claimed the expired lease yet, the
                # worker is no longer authorized to end an ACTIVE call.
                # A finished/declined call may still have its old lease
                # record removed safely, as there is no live media to seize.
                if call["state"] == "ACTIVE" and row["expires_at"] <= now:
                    raise CallError("inbound_lease_lost", 409)
                handoff = self._handoff_row(db, call_id)
                transferred = handoff is not None and handoff["state"] == "CONNECTED"
                if transferred:
                    db.execute(
                        "DELETE FROM ip_inbound_leases WHERE call_id=?",
                        (call_id,),
                    )
                    return {"state": call["state"], "transferred": True}
                if call["state"] == "ACTIVE":
                    self._queue_media_teardown(db, call, now)
                    db.execute(
                        "UPDATE ip_calls SET state='ENDED',expires_at=?,closed_at=? "
                        "WHERE id=?", (now, now, call_id),
                    )
                    for user in (call["caller"], call["callee"]):
                        self._event(db, call, user, "ENDED", now)
                    result = "ENDED"
                else:
                    result = call["state"]
                db.execute(
                    "DELETE FROM ip_inbound_leases WHERE call_id=?",
                    (call_id,),
                )
                return {"state": result, "transferred": False}
