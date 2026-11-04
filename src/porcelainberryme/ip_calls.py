"""Headless, internet-only call control for PorcelainberryMe.

Self-hosted LiveKit handles WebRTC media; this module owns authenticated
invitations, call consent, tenant isolation, state, and room-scoped JWTs.
It does not connect to PSTN, launch a model, or silently dial a user.
No third-party Python dependency is needed for the control plane.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .ip_handoff import HandoffMixin
from .ip_inbound import InboundAgentMixin
from .web_push import PushMixin, validate_public_key

IDENTITY = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
IDEMPOTENCY = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
CALL_ID = re.compile(r"^[a-f0-9]{32}$")
AUDIENCE = "porcelainberryme.ip-calls.v1"
RING_SECONDS = 45
MAX_CALL_SECONDS = 3600
MEDIA_TOKEN_SECONDS = 12
MEDIA_REVOKE_GRACE_SECONDS = 5
MEDIA_CLEANUP_INTERVAL_SECONDS = 2
MAX_BODY = 4096
AGENT_COOLDOWN_SECONDS = 120
MAX_TENANT_ACTIVE_CALLS = 40
EVENT_RETENTION_SECONDS = 30 * 86400
MAX_EVENTS_PER_RECIPIENT = 1000
CALL_HISTORY_RETENTION_SECONDS = 30 * 86400


class CallError(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def _identity(value: str) -> str:
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise CallError("invalid_identity")
    return value


def validate_livekit_url(value: str) -> str:
    """Require TLS for any non-local RTC endpoint and reject URL credentials.

    Media room JWTs are sensitive bearer credentials. A misconfigured
    remote ws:// server must never receive them over cleartext transport.
    """
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise ValueError("invalid_media_url")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid_media_url") from exc
    if (parsed.scheme not in ("ws", "wss")
            or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")
            or port == 0):
        raise ValueError("invalid_media_url")
    if (parsed.scheme == "ws"
            and parsed.hostname.lower() not in ("localhost", "127.0.0.1", "::1")):
        raise ValueError("unencrypted_remote_media_url")
    return value


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(encoded: str) -> bytes:
    return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))


def _jwt(claims: dict, key: str, *, header: dict | None = None) -> str:
    if not isinstance(key, str) or len(key) < 32:
        raise ValueError("jwt_signing_secret_too_short")
    header = header or {"alg": "HS256", "typ": "JWT"}
    encoded = ".".join(
        _b64(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        for obj in (header, claims)
    )
    return encoded + "." + _b64(hmac.new(key.encode(), encoded.encode(), hashlib.sha256).digest())


def mint_identity_token(secret: str, tenant: str, subject: str, role: str,
                        *, now: int | None = None, lifetime: int = 3600) -> str:
    """Offline/trusted identity issuer only; never exposed as a public endpoint."""
    _identity(tenant)
    _identity(subject)
    if role not in ("human", "agent") or not 60 <= lifetime <= 86400:
        raise CallError("invalid_identity_claim")
    now = int(time.time()) if now is None else int(now)
    return _jwt({
        "aud": AUDIENCE, "tenant": tenant, "sub": subject, "role": role,
        "iat": now, "nbf": now, "exp": now + lifetime,
    }, secret)


def verify_identity_token(token: str, secret: str, *, now: int | None = None) -> dict:
    now = int(time.time()) if now is None else int(now)
    try:
        head, body, signature = token.split(".")
        header = json.loads(_unb64(head))
        claims = json.loads(_unb64(body))
        expected = _b64(hmac.new(secret.encode(), (head + "." + body).encode(),
                                 hashlib.sha256).digest())
        if not hmac.compare_digest(expected, signature):
            raise ValueError("bad_signature")
        if header != {"alg": "HS256", "typ": "JWT"}:
            raise ValueError("bad_header")
        if (claims.get("aud") != AUDIENCE
                or claims.get("role") not in ("human", "agent")
                or type(claims.get("nbf")) is not int
                or type(claims.get("exp")) is not int
                or type(claims.get("iat")) is not int
                or claims["nbf"] > now or claims["iat"] > now
                or claims["exp"] <= now or claims["exp"] - claims["iat"] > 86400):
            raise ValueError("bad_claims")
        _identity(claims["tenant"])
        _identity(claims["sub"])
        return claims
    except (ValueError, KeyError, TypeError, UnicodeError, OverflowError) as exc:
        raise CallError("unauthorized", 401) from exc


class CallRegistry(HandoffMixin, InboundAgentMixin, PushMixin):
    """SQLite transactions serialize invitations, acceptance and termination.

    Client identity is always sourced from a server-verified token, never
    from request payload fields. Room tokens are minted only after acceptance.
    """

    def __init__(self, path: str, *, identity_secret: str, media_key: str | None = None,
                 media_secret: str | None = None, media_url: str | None = None,
                 clock=None, push_public_key: str | None = None):
        if not isinstance(identity_secret, str) or len(identity_secret) < 32:
            raise ValueError("identity_secret_too_short")
        # Call control must run without a media service. No fake LiveKit
        # credentials are required merely to originate/decline a call.
        provided = [bool(media_key), bool(media_secret), bool(media_url)]
        if any(provided):
            if not all(provided):
                raise ValueError("incomplete_media_configuration")
            if not isinstance(media_key, str) or not media_key.strip():
                raise ValueError("media_key_required")
            if not isinstance(media_secret, str) or len(media_secret) < 32:
                raise ValueError("media_secret_too_short")
            validate_livekit_url(media_url)
            if identity_secret == media_secret:
                raise ValueError("separate_signing_keys_required")
        self.path = path
        self.identity_secret = identity_secret
        self.media_key = media_key
        self.media_secret = media_secret
        self.media_url = media_url
        self.clock = clock or time.time
        self.push_public_key = (
            validate_public_key(push_public_key) if push_public_key else None
        )
        with closing(self._conn()) as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS ip_calls (
                  id TEXT PRIMARY KEY, tenant TEXT NOT NULL,
                  caller TEXT NOT NULL, callee TEXT NOT NULL,
                  agent INTEGER NOT NULL, state TEXT NOT NULL,
                  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                  closed_at INTEGER, accepted_at INTEGER,
                  idem TEXT NOT NULL,
                  UNIQUE(tenant, caller, idem)
                );
                CREATE TABLE IF NOT EXISTS ip_call_users (
                  tenant TEXT NOT NULL, subject TEXT NOT NULL,
                  agent_calls INTEGER NOT NULL DEFAULT 0,
                  do_not_disturb INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY(tenant, subject)
                );
                CREATE TABLE IF NOT EXISTS ip_call_events (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  call_id TEXT NOT NULL, tenant TEXT NOT NULL,
                  receiver TEXT NOT NULL, kind TEXT NOT NULL,
                  created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ip_call_inbox
                  ON ip_call_events(tenant, receiver, seq);
                CREATE INDEX IF NOT EXISTS ip_call_event_retention
                  ON ip_call_events(created_at);
                CREATE TABLE IF NOT EXISTS ip_media_teardown (
                  call_id TEXT PRIMARY KEY, room TEXT NOT NULL,
                  latest_token_exp INTEGER NOT NULL,
                  next_attempt INTEGER NOT NULL DEFAULT -1,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  completed INTEGER NOT NULL DEFAULT 0
                );
            """)
            self._init_handoff_schema(con)
            self._init_inbound_schema(con)
            self._init_push_schema(con)
            # Existing installations migrate safely in place; previous
            # registration/consent preferences remain unchanged.
            columns = {
                row["name"] for row in con.execute("PRAGMA table_info(ip_call_users)")
            }
            if "do_not_disturb" not in columns:
                con.execute(
                    "ALTER TABLE ip_call_users ADD COLUMN do_not_disturb "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            columns = {
                row["name"] for row in con.execute("PRAGMA table_info(ip_calls)")
            }
            if "closed_at" not in columns:
                con.execute("ALTER TABLE ip_calls ADD COLUMN closed_at INTEGER")
                con.execute(
                    "UPDATE ip_calls SET closed_at=expires_at "
                    "WHERE state IN ('ENDED','DECLINED','TIMED_OUT')"
                )
            if "accepted_at" not in columns:
                # Old historical sessions cannot be backfilled reliably once
                # bounded ephemeral event logs have expired.
                con.execute("ALTER TABLE ip_calls ADD COLUMN accepted_at INTEGER")
            con.execute(
                "CREATE INDEX IF NOT EXISTS ip_call_reporting_scope "
                "ON ip_calls(tenant,created_at,caller,callee)"
            )
            # Each participant predicate needs its own leftmost index;
            # a shared tenant/date index scans unrelated call volume.
            con.execute(
                "CREATE INDEX IF NOT EXISTS ip_call_reporting_caller "
                "ON ip_calls(tenant,caller,created_at)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS ip_call_reporting_callee "
                "ON ip_calls(tenant,callee,created_at)"
            )
            con.commit()

    def _conn(self):
        con = sqlite3.connect(self.path, timeout=20)
        con.row_factory = sqlite3.Row
        return con

    def provision(self, tenant: str, subject: str, *, allow_agent_calls: bool = False):
        """Trusted local provisioning only; never made available as HTTP."""
        _identity(tenant)
        _identity(subject)
        if type(allow_agent_calls) is not bool:
            raise CallError("invalid_agent_consent")
        with closing(self._conn()) as con:
            with con:
                con.execute(
                    "INSERT INTO ip_call_users(tenant, subject, agent_calls) VALUES(?,?,?) "
                    "ON CONFLICT(tenant,subject) DO UPDATE SET agent_calls=excluded.agent_calls",
                    (tenant, subject, int(allow_agent_calls)),
                )
                if not allow_agent_calls:
                    # A trusted operator can revoke an existing opt-in.
                    # Revoke unanswered agent invitations in the same commit.
                    self._revoke_pending_contact(
                        con, tenant, subject, now=self._now(),
                        block_all=False, block_agents=True,
                    )

    def set_do_not_disturb(self, tenant: str, subject: str, enabled: bool) -> None:
        """Trusted enrollment interface. No unauthenticated HTTP mutation."""
        _identity(tenant)
        _identity(subject)
        if type(enabled) is not bool:
            raise CallError("invalid_dnd_preference")
        with closing(self._conn()) as con:
            with con:
                updated = con.execute(
                    "UPDATE ip_call_users SET do_not_disturb=? "
                    "WHERE tenant=? AND subject=?",
                    (int(enabled), tenant, subject),
                )
                if updated.rowcount != 1:
                    raise CallError("account_not_enrolled", 404)
                if enabled:
                    self._revoke_pending_contact(
                        con, tenant, subject, now=self._now(),
                        block_all=True, block_agents=True,
                    )

    def _revoke_pending_contact(self, con, tenant, subject, *,
                                now, block_all, block_agents):
        """Atomically rescind still-ringing calls and optional staff offers.

        New consent preferences must take effect for INVITATIONS already
        issued, not just future originates. Active/connected conversations
        are deliberately not torn down by a preference update.
        """
        if not block_all and not block_agents:
            return
        pending = con.execute(
            "SELECT * FROM ip_calls WHERE tenant=? AND callee=? "
            "AND state='RINGING' AND (?=1 OR agent=1)",
            (tenant, subject, int(block_all)),
        ).fetchall()
        for call in pending:
            con.execute(
                "UPDATE ip_calls SET state='DECLINED', expires_at=?, "
                "closed_at=? WHERE id=? AND state='RINGING'",
                (now, now, call["id"]),
            )
            for actor in (call["caller"], call["callee"]):
                self._event(con, call, actor, "DECLINED", now)

        if block_all:
            offers = con.execute(
                "SELECT c.*,h.target AS handoff_target "
                "FROM ip_call_handoffs h JOIN ip_calls c ON c.id=h.call_id "
                "WHERE h.tenant=? AND h.target=? AND c.state='ACTIVE' "
                "AND h.state IN ('OFFERED','ACCEPTED')",
                (tenant, subject),
            ).fetchall()
            for call in offers:
                con.execute(
                    "UPDATE ip_call_handoffs SET state='CANCELLED', "
                    "expires_at=? WHERE call_id=? "
                    "AND state IN ('OFFERED','ACCEPTED')",
                    (now, call["id"]),
                )
                # If staff already obtained a media JWT, revoke their
                # participant repeatedly until the last token expires.
                self._schedule_handoff_kick(con, call["id"], now)
                for actor in {call["caller"], call["callee"], subject}:
                    self._event(con, call, actor, "HANDOFF_CANCELLED", now)

    def preferences(self, claims: dict) -> dict:
        """Read only the authenticated account's own consent and DND."""
        with closing(self._conn()) as con:
            row = con.execute(
                "SELECT agent_calls,do_not_disturb FROM ip_call_users "
                "WHERE tenant=? AND subject=?",
                (claims["tenant"], claims["sub"]),
            ).fetchone()
            if row is None:
                raise CallError("account_not_enrolled", 403)
            return {
                "allow_agent_calls": bool(row["agent_calls"]),
                "do_not_disturb": bool(row["do_not_disturb"]),
            }

    def update_preferences(self, claims: dict, changes: dict) -> dict:
        """Allow only a real human identity to control its own opt-in state.

        Neither the agent nor a request parameter can select another account.
        All requested changes are committed atomically.
        """
        if claims["role"] != "human":
            raise CallError("only_human_can_change_call_preferences", 403)
        if (not isinstance(changes, dict) or not changes
                or set(changes) - {"allow_agent_calls", "do_not_disturb"}
                or any(type(value) is not bool for value in changes.values())):
            raise CallError("invalid_call_preferences")
        with closing(self._conn()) as con:
            with con:
                current = con.execute(
                    "SELECT agent_calls,do_not_disturb FROM ip_call_users "
                    "WHERE tenant=? AND subject=?",
                    (claims["tenant"], claims["sub"]),
                ).fetchone()
                if current is None:
                    raise CallError("account_not_enrolled", 403)
                consent = changes.get("allow_agent_calls", bool(current["agent_calls"]))
                dnd = changes.get("do_not_disturb", bool(current["do_not_disturb"]))
                con.execute(
                    "UPDATE ip_call_users SET agent_calls=?,do_not_disturb=? "
                    "WHERE tenant=? AND subject=?",
                    (int(consent), int(dnd), claims["tenant"], claims["sub"]),
                )
                if dnd or not consent:
                    self._revoke_pending_contact(
                        con, claims["tenant"], claims["sub"], now=self._now(),
                        block_all=dnd, block_agents=not consent,
                    )
                return {"allow_agent_calls": consent, "do_not_disturb": dnd}

    def _now(self):
        return int(self.clock())

    def _event(self, con, call, receiver: str, kind: str, now: int):
        cursor = con.execute(
            "INSERT INTO ip_call_events(call_id, tenant, receiver, kind, created_at) "
            "VALUES(?,?,?,?,?)",
            (call["id"], call["tenant"], receiver, kind, now),
        )
        self._queue_event_push(con, call, receiver, kind, cursor.lastrowid, now)
        # A hostile or stuck originator must not fill the event database
        # indefinitely. Keep the newest N events for this recipient.
        cutoff = con.execute(
            "SELECT seq FROM ip_call_events WHERE tenant=? AND receiver=? "
            "ORDER BY seq DESC LIMIT 1 OFFSET ?",
            (call["tenant"], receiver, MAX_EVENTS_PER_RECIPIENT - 1),
        ).fetchone()
        if cutoff is not None:
            con.execute(
                "DELETE FROM ip_call_events WHERE tenant=? AND receiver=? AND seq<?",
                (call["tenant"], receiver, cutoff["seq"]),
            )

    @staticmethod
    def _queue_media_teardown(con, call, now):
        if call["state"] == "ACTIVE":
            con.execute(
                "UPDATE ip_media_teardown SET next_attempt=?, completed=0 "
                "WHERE call_id=? AND completed=0",
                (now, call["id"]),
            )

    def _transition_expired(self, con, now):
        self._expire_handoffs(con, now)
        rows = con.execute(
            "SELECT * FROM ip_calls WHERE state IN ('RINGING', 'ACTIVE') "
            "AND expires_at<=?", (now,)
        ).fetchall()
        for call in rows:
            self._queue_media_teardown(con, call, now)
            con.execute(
                "UPDATE ip_calls SET state='TIMED_OUT',closed_at=? WHERE id=?",
                (now, call["id"]),
            )
            self._event(con, call, call["caller"], "TIMED_OUT", now)
            self._event(con, call, call["callee"], "TIMED_OUT", now)
            handoff = self._handoff_row(con, call["id"])
            if handoff is not None and handoff["state"] == "CONNECTED":
                self._event(con, call, handoff["target"], "TIMED_OUT", now)

    def _prune_closed_calls(self, con, now):
        # Keep history while media revocation is outstanding. Never delete a
        # call whose LiveKit teardown has not been durably confirmed.
        eligible = con.execute(
            "SELECT c.id FROM ip_calls AS c WHERE c.closed_at<? "
            "AND c.state IN ('ENDED','DECLINED','TIMED_OUT') "
            "AND NOT EXISTS(SELECT 1 FROM ip_media_teardown AS m "
            "WHERE m.call_id=c.id AND m.completed=0) "
            "LIMIT 200",
            (now - CALL_HISTORY_RETENTION_SECONDS,),
        ).fetchall()
        for row in eligible:
            call_id = row["id"]
            con.execute("DELETE FROM ip_push_jobs WHERE call_id=?", (call_id,))
            con.execute("DELETE FROM ip_call_events WHERE call_id=?", (call_id,))
            con.execute("DELETE FROM ip_media_teardown WHERE call_id=?", (call_id,))
            con.execute("DELETE FROM ip_inbound_leases WHERE call_id=?", (call_id,))
            con.execute("DELETE FROM ip_handoff_kicks WHERE call_id=?", (call_id,))
            con.execute("DELETE FROM ip_call_handoffs WHERE call_id=?", (call_id,))
            con.execute("DELETE FROM ip_calls WHERE id=?", (call_id,))

    def _open(self):
        con = self._conn()
        try:
            con.execute("BEGIN IMMEDIATE")
            now = self._now()
            self._transition_expired(con, now)
            con.execute("DELETE FROM ip_call_events WHERE created_at<?",
                        (now - EVENT_RETENTION_SECONDS,))
            self._prune_closed_calls(con, now)
            # Persist data-retention maintenance independently of the
            # subsequent API action. A 404/403 must not roll it back.
            con.commit()
            con.execute("BEGIN IMMEDIATE")
            return con
        except BaseException:
            con.rollback()
            con.close()
            raise

    @staticmethod
    def _visible(call, viewer: str):
        return {
            "id": call["id"], "state": call["state"],
            "peer": call["callee"] if viewer == call["caller"] else call["caller"],
            "direction": "outgoing" if viewer == call["caller"] else "incoming",
            "agent_initiated": bool(call["agent"]),
            "created_at": call["created_at"], "expires_at": call["expires_at"],
        }

    def _get(self, con, tenant, subject, call_id):
        if not isinstance(call_id, str) or not CALL_ID.fullmatch(call_id):
            raise CallError("call_not_found", 404)
        call = con.execute(
            "SELECT * FROM ip_calls WHERE id=? AND tenant=? "
            "AND (caller=? OR callee=? OR "
            "EXISTS(SELECT 1 FROM ip_call_handoffs h WHERE h.call_id=ip_calls.id "
            "AND h.tenant=? AND h.target=?))",
            (call_id, tenant, subject, subject, tenant, subject),
        ).fetchone()
        if call is None:
            raise CallError("call_not_found", 404)
        return call

    def originate(self, claims: dict, callee: str, idem: str) -> dict:
        tenant, caller = claims["tenant"], claims["sub"]
        _identity(callee)
        if caller == callee:
            raise CallError("self_call_disallowed")
        if not isinstance(idem, str) or not IDEMPOTENCY.fullmatch(idem):
            raise CallError("stable_idempotency_key_required")
        now = self._now()
        with closing(self._open()) as con:
            with con:
                prior = con.execute(
                    "SELECT * FROM ip_calls WHERE tenant=? AND caller=? AND idem=?",
                    (tenant, caller, idem),
                ).fetchone()
                if prior is not None:
                    if prior["callee"] != callee or prior["agent"] != int(claims["role"] == "agent"):
                        raise CallError("idempotency_conflict", 409)
                    return self._visible(prior, caller)
                users = {
                    row["subject"]: row
                    for row in con.execute(
                        "SELECT subject, agent_calls, do_not_disturb FROM ip_call_users "
                        "WHERE tenant=? AND subject IN (?,?)",
                        (tenant, caller, callee),
                    )
                }
                if caller not in users or callee not in users:
                    raise CallError("account_not_enrolled", 403)
                if users[callee]["do_not_disturb"]:
                    raise CallError("recipient_do_not_disturb", 409)
                # An explicitly disabled AI receptionist must not cause
                # an unanswered customer ring just because its identity
                # still exists in the trusted account directory.
                inbound = con.execute(
                    "SELECT enabled FROM ip_inbound_agents "
                    "WHERE tenant=? AND assistant=?",
                    (tenant, callee),
                ).fetchone()
                if inbound is not None and not inbound["enabled"]:
                    raise CallError("inbound_agent_not_enabled", 409)
                if claims["role"] == "agent":
                    if not users[callee]["agent_calls"]:
                        raise CallError("agent_call_not_opted_in", 403)
                    # Per-recipient cooldown covers all agents in this tenant
                    # and survives service restarts. Replays are caught above.
                    recent = con.execute(
                        "SELECT 1 FROM ip_calls WHERE tenant=? AND callee=? "
                        "AND agent=1 AND created_at>? LIMIT 1",
                        (tenant, callee, now - AGENT_COOLDOWN_SECONDS),
                    ).fetchone()
                    if recent:
                        raise CallError("agent_call_cooldown", 429)
                count = con.execute(
                    "SELECT COUNT(*) FROM ip_calls WHERE tenant=? "
                    "AND state IN ('RINGING', 'ACTIVE')", (tenant,),
                ).fetchone()[0]
                if count >= MAX_TENANT_ACTIVE_CALLS:
                    raise CallError("tenant_call_capacity_exceeded", 429)
                busy = con.execute(
                    "SELECT 1 FROM ip_calls WHERE tenant=? "
                    "AND state IN ('RINGING', 'ACTIVE') "
                    "AND (caller IN (?, ?) OR callee IN (?, ?) OR "
                    "EXISTS(SELECT 1 FROM ip_call_handoffs h "
                    "WHERE h.call_id=ip_calls.id AND h.target IN (?,?) "
                    "AND h.state IN ('OFFERED','ACCEPTED','CONNECTED'))) LIMIT 1",
                    (tenant, caller, callee, caller, callee, caller, callee),
                ).fetchone()
                if busy:
                    raise CallError("endpoint_busy", 409)
                call_id = uuid4().hex
                con.execute(
                    "INSERT INTO ip_calls("
                    "id,tenant,caller,callee,agent,state,created_at,expires_at,closed_at,idem"
                    ") VALUES(?,?,?,?,?,?,?,?,NULL,?)",
                    (call_id, tenant, caller, callee, int(claims["role"] == "agent"),
                     "RINGING", now, now + RING_SECONDS, idem),
                )
                call = con.execute("SELECT * FROM ip_calls WHERE id=?", (call_id,)).fetchone()
                self._event(con, call, callee, "RINGING", now)
                self._event(con, call, caller, "OUTGOING", now)
                return self._visible(call, caller)

    def active_calls(self, claims: dict) -> dict:
        """Authenticated snapshot after reconnect or a suspended PWA wake-up.

        Poll cursors can be lost when events expire; a live-call snapshot
        prevents an otherwise valid unanswered ring or handoff offer from
        disappearing behind an old browser cursor. The snapshot is scoped
        to the caller, recipient, or currently invited/connected employee.
        """
        with closing(self._open()) as con:
            with con:
                rows = con.execute(
                    "SELECT c.* FROM ip_calls AS c "
                    "WHERE c.tenant=? AND c.state IN ('RINGING','ACTIVE') "
                    "AND (c.caller=? OR c.callee=? OR EXISTS("
                    "SELECT 1 FROM ip_call_handoffs AS h "
                    "WHERE h.call_id=c.id AND h.tenant=c.tenant "
                    "AND h.target=? AND h.state IN ('OFFERED','ACCEPTED','CONNECTED')"
                    ")) ORDER BY c.created_at DESC, c.id LIMIT 100",
                    (claims["tenant"], claims["sub"], claims["sub"], claims["sub"]),
                ).fetchall()
                return {
                    "calls": [
                        self._handoff_with_call(con, call, claims["sub"])
                        for call in rows
                    ],
                    "server_time": self._now(),
                }

    def status(self, claims: dict, call_id: str) -> dict:
        with closing(self._open()) as con:
            with con:
                call = self._get(con, claims["tenant"], claims["sub"], call_id)
                if claims["sub"] not in (call["caller"], call["callee"]):
                    # A cancelled/declined/expired offer does not confer
                    # ongoing surveillance of the customer's active call.
                    offer = self._handoff_row(con, call_id)
                    if (offer is None or offer["target"] != claims["sub"]
                            or offer["state"] not in ("OFFERED", "ACCEPTED", "CONNECTED")):
                        raise CallError("call_not_found", 404)
                return self._handoff_with_call(con, call, claims["sub"])

    def inbox(self, claims: dict, after: int = 0) -> dict:
        if type(after) is not int or after < 0:
            raise CallError("invalid_cursor")
        with closing(self._open()) as con:
            with con:
                rows = con.execute(
                    "SELECT seq, call_id, kind, created_at FROM ip_call_events "
                    "WHERE tenant=? AND receiver=? AND seq>? ORDER BY seq LIMIT 100",
                    (claims["tenant"], claims["sub"], after),
                ).fetchall()
                events = []
                for row in rows:
                    call = self._get(con, claims["tenant"], claims["sub"], row["call_id"])
                    if claims["sub"] not in (call["caller"], call["callee"]):
                        offer = self._handoff_row(con, call["id"])
                        if (offer is None or offer["target"] != claims["sub"]
                                or offer["state"] not in ("OFFERED", "ACCEPTED", "CONNECTED")):
                            # Historical staff notification is still acknowledged
                            # so polling can advance. Do not disclose the live
                            # customer's peer/current state after revocation.
                            visible = {"id": call["id"], "state": "HANDOFF_REVOKED"}
                        else:
                            visible = self._handoff_with_call(
                                con, call, claims["sub"],
                            )
                    else:
                        visible = self._handoff_with_call(
                            con, call, claims["sub"],
                        )
                    events.append({"cursor": row["seq"], "kind": row["kind"],
                                   "call": visible})
                return {"events": events, "next_cursor": rows[-1]["seq"] if rows else after}

    def decide(self, claims: dict, call_id: str, decision: str) -> dict:
        if decision not in ("accept", "decline", "hangup"):
            raise CallError("invalid_decision")
        now = self._now()
        with closing(self._open()) as con:
            with con:
                call = self._get(con, claims["tenant"], claims["sub"], call_id)
                if decision in ("accept", "decline") and claims["sub"] != call["callee"]:
                    raise CallError("only_recipient_can_decide", 403)
                # An AI recipient must use a lease-fenced inbound claim
                # rather than an untracked direct acceptance/hangup.
                if (claims["role"] == "agent" and call["callee"] == claims["sub"]
                        and not call["agent"]):
                    raise CallError("inbound_agent_lease_required", 403)
                if decision == "hangup":
                    handoff = self._handoff_row(con, call_id)
                    allowed = (call["caller"], call["callee"])
                    if handoff is not None and handoff["state"] == "CONNECTED":
                        # The human remains authorized after the AI leaves,
                        # regardless of which side originated the call.
                        human = call["callee"] if call["agent"] else call["caller"]
                        allowed = (human, handoff["target"])
                    if claims["sub"] not in allowed:
                        raise CallError("not_authorized_to_hangup", 403)
                target = {"accept": "ACTIVE", "decline": "DECLINED",
                          "hangup": "ENDED"}[decision]
                expected = ("RINGING",) if decision != "hangup" else ("RINGING", "ACTIVE")
                if call["state"] == target:
                    return self._visible(call, claims["sub"])
                if call["state"] not in expected:
                    raise CallError("invalid_call_transition", 409)
                expires_at = now + MAX_CALL_SECONDS if decision == "accept" else now
                if decision == "hangup":
                    self._queue_media_teardown(con, call, now)
                con.execute(
                    "UPDATE ip_calls SET state=?, expires_at=?, closed_at=?, "
                    "accepted_at=CASE WHEN ?='ACTIVE' THEN ? ELSE accepted_at END "
                    "WHERE id=?",
                    (target, expires_at, now if target != "ACTIVE" else None,
                     target, now, call_id),
                )
                for user in (call["caller"], call["callee"]):
                    self._event(con, call, user, target, now)
                handoff = self._handoff_row(con, call_id)
                if handoff is not None and handoff["state"] == "CONNECTED":
                    self._event(con, call, handoff["target"], target, now)
                changed = con.execute("SELECT * FROM ip_calls WHERE id=?", (call_id,)).fetchone()
                return self._visible(changed, claims["sub"])

    def media_credentials(self, claims: dict, call_id: str,
                          *, inbound_lease: str | None = None) -> dict:
        now = self._now()
        with closing(self._open()) as con:
            with con:
                call = self._get(con, claims["tenant"], claims["sub"], call_id)
                if call["state"] != "ACTIVE":
                    raise CallError("call_not_accepted", 409)
                if (claims["role"] == "agent" and not call["agent"]
                        and claims["sub"] == call["callee"]):
                    self._verify_inbound_agent_lease(
                        con, call, claims, inbound_lease, now,
                    )
                handoff = self._handoff_row(con, call_id)
                is_staff = (handoff is not None
                            and claims["sub"] == handoff["target"])
                if is_staff and (claims["role"] != "human"
                                 or handoff["state"] not in ("ACCEPTED", "CONNECTED")):
                    raise CallError("handoff_not_accepted", 403)
                agent_identity = call["caller"] if call["agent"] else call["callee"]
                if handoff is not None and handoff["state"] == "CONNECTED" and (
                    claims["role"] == "agent" and claims["sub"] == agent_identity
                ):
                    raise CallError("agent_already_handed_off", 403)
                if not (self.media_key and self.media_secret and self.media_url):
                    raise CallError("media_backend_not_configured", 503)
                # Opaque identity, unique per call. Do not embed real contact data.
                participant = "ip_" + hashlib.sha256(
                    (call_id + ":" + claims["tenant"] + ":" + claims["sub"]).encode()
                ).hexdigest()[:24]
                room = "pb_" + call_id
                token_expires = min(now + MEDIA_TOKEN_SECONDS, call["expires_at"])
                if is_staff:
                    token_expires = min(token_expires, handoff["expires_at"])
                if token_expires <= now:
                    raise CallError("call_not_accepted", 409)
                # Remember the last valid media token. A subsequent hangup
                # must delete the room and keep deleting through token expiry.
                con.execute(
                    "INSERT INTO ip_media_teardown(call_id,room,latest_token_exp) "
                    "VALUES(?,?,?) ON CONFLICT(call_id) DO UPDATE SET "
                    "latest_token_exp=MAX(latest_token_exp, excluded.latest_token_exp)",
                    (call_id, room, token_expires),
                )
                if is_staff:
                    con.execute(
                        "UPDATE ip_call_handoffs SET media_issued=1 WHERE call_id=?",
                        (call_id,),
                    )
                    self._track_staff_media_token(
                        con, call, claims["sub"], token_expires,
                    )
                jwt = _jwt({
                    "iss": self.media_key, "sub": participant,
                    "iat": now, "nbf": now - 1, "exp": token_expires,
                    "video": {
                        "room": room, "roomJoin": True,
                        "canPublish": True, "canSubscribe": True,
                        "canPublishSources": ["microphone"],
                        "canPublishData": False,
                    },
                }, self.media_secret)
                return {"url": self.media_url, "room": room,
                        "participant": participant, "token": jwt,
                        "expires_at": token_expires}


    def reconcile_media(self, delete_room, *, limit: int = 20) -> dict:
        """Durable, bounded cleanup of ended or timed-out LiveKit rooms.

        Network effects occur outside the SQLite transaction. The leased job
        is retried on a crash, and even successful DeleteRoom is repeated
        until every issued token has expired plus clock skew. A deleted room
        could otherwise be re-created by a still-valid join token.
        """
        if not callable(delete_room):
            raise ValueError("media_deleter_required")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_media_batch_limit")
        now = self._now()
        with closing(self._open()) as con:
            with con:
                jobs = con.execute(
                    "SELECT m.call_id, m.room, m.latest_token_exp, m.attempts "
                    "FROM ip_media_teardown AS m JOIN ip_calls AS c ON c.id=m.call_id "
                    "WHERE m.completed=0 AND m.next_attempt>=0 "
                    "AND m.next_attempt<=? AND c.state IN ('ENDED', 'TIMED_OUT', 'DECLINED') "
                    "ORDER BY m.next_attempt, m.call_id LIMIT ?",
                    (now, limit),
                ).fetchall()
                for job in jobs:
                    # Lease prevents two parallel reconciliation workers
                    # from both deleting and writing contradictory results.
                    con.execute(
                        "UPDATE ip_media_teardown SET next_attempt=? WHERE call_id=?",
                        (now + 30, job["call_id"]),
                    )
        completed = retried = 0
        for job in jobs:
            succeeded = False
            try:
                delete_room(job["room"])
                succeeded = True
            except Exception:
                # Media is NOT confirmed disconnected. Keep a bounded
                # exponential retry instead of declaring hangup complete.
                pass
            stamp = self._now()
            with closing(self._conn()) as con:
                with con:
                    next_attempt = (stamp + MEDIA_CLEANUP_INTERVAL_SECONDS
                                    if succeeded else stamp + min(30, 2 ** min(5, job["attempts"])))
                    final = succeeded and stamp >= job["latest_token_exp"] + MEDIA_REVOKE_GRACE_SECONDS
                    con.execute(
                        "UPDATE ip_media_teardown SET attempts=attempts+1, "
                        "next_attempt=?, completed=? WHERE call_id=?",
                        (-1 if final else next_attempt, int(final), job["call_id"]),
                    )
            if final:
                completed += 1
            else:
                retried += 1
        return {"reviewed": len(jobs), "completed": completed, "retry_pending": retried}


class BoundedCallHTTPServer(ThreadingHTTPServer):
    """A strict concurrency cap for the loopback call-control ingress.

    The standard ThreadingHTTPServer creates an unbounded native thread for
    each connection. A stuck or malicious local proxy client could exhaust
    all Python workers. Reject excess connections immediately rather than
    delaying existing live-call control. Per-socket timeouts prevent idle
    clients from pinning a capacity slot indefinitely.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, *, max_connections: int = 64,
                 request_timeout: float = 10):
        if (type(max_connections) is not int
                or not 1 <= max_connections <= 256):
            raise ValueError("call_http_connection_limit_invalid")
        if (not isinstance(request_timeout, (int, float))
                or not 1 <= request_timeout <= 30):
            raise ValueError("call_http_request_timeout_invalid")
        if (not isinstance(address, tuple) or len(address) != 2
                or address[0] != "127.0.0.1"):
            raise ValueError("call_http_must_bind_to_loopback")
        self._slots = threading.BoundedSemaphore(max_connections)
        self._request_timeout = float(request_timeout)
        super().__init__(address, handler)

    def get_request(self):
        sock, addr = super().get_request()
        sock.settimeout(self._request_timeout)
        return sock, addr

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Connection: close\r\n"
                    b"Cache-Control: no-store\r\n"
                    b"Content-Length: 0\r\n\r\n"
                )
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        # Never write HTTP authorization headers, request bodies, caller IDs,
        # filesystem paths or caught exception string to server logs.
        import sys
        print("ip_call_http_handler_error", file=sys.stderr)


def control_ready(registry: CallRegistry) -> bool:
    """Bounded read-only SQLite probe; do not trust the liveness URL alone.

    A missing/unreadable/corrupt database means call setup is not ready.
    The probe intentionally doesn't acquire the write lock or run retention
    maintenance. Media readiness is checked independently at release gate.
    """
    from urllib.parse import quote
    path = Path(registry.path)
    if not path.is_absolute() or not path.is_file():
        return False
    uri = "file:" + quote(str(path), safe="/") + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=1000")
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not {"ip_calls", "ip_call_users", "ip_call_events",
                    "ip_media_teardown"}.issubset(tables):
                return False
            conn.execute("SELECT 1 FROM ip_calls LIMIT 1").fetchone()
            return True
    except (OSError, sqlite3.Error):
        return False


def make_handler(registry: CallRegistry):
    """Loopback-only headless API; external TLS/auth proxy is separate."""
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status, data):
            payload = json.dumps(data, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _auth(self):
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                raise CallError("unauthorized", 401)
            return verify_identity_token(header[7:], registry.identity_secret,
                                         now=registry._now())

        def _body(self):
            try:
                length = int(self.headers.get("Content-Length", ""))
                if not 0 < length <= MAX_BODY:
                    raise CallError("invalid_body_size", 413)
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise CallError("body_must_be_object")
                return data
            except (ValueError, UnicodeDecodeError) as exc:
                raise CallError("invalid_json") from exc

        def _request(self):
            try:
                uri = urlsplit(self.path)
                if self.command == "GET" and uri.path == "/healthz":
                    return self._reply(200, {"status": "up",
                                              "scope": "internet_calls_control_only"})
                if self.command == "GET" and uri.path == "/readyz":
                    ready = control_ready(registry)
                    return self._reply(200 if ready else 503, {
                        "status": "ready" if ready else "unready",
                        "scope": "internet_calls_control_only",
                        "media_configured": bool(registry.media_url),
                    })
                claims = self._auth()
                if self.command == "GET" and uri.path == "/v1/ip-calls/whoami":
                    return self._reply(200, {
                        "tenant": claims["tenant"], "subject": claims["sub"],
                        "role": claims["role"],
                    })
                if uri.path == "/v1/ip-calls/preferences":
                    if self.command == "GET":
                        return self._reply(200, registry.preferences(claims))
                    if self.command == "POST":
                        return self._reply(200, registry.update_preferences(
                            claims, self._body(),
                        ))
                if self.command == "GET" and uri.path == "/v1/ip-calls/push/config":
                    return self._reply(200, registry.push_config())
                if uri.path == "/v1/ip-calls/push/subscriptions":
                    if self.command == "POST":
                        return self._reply(201, registry.register_push(
                            claims, self._body(),
                        ))
                    if self.command == "DELETE":
                        return self._reply(200, registry.revoke_push(
                            claims, self._body().get("device_id"),
                        ))
                if self.command == "GET" and uri.path == "/v1/ip-calls/active":
                    return self._reply(200, registry.active_calls(claims))
                if self.command == "GET" and uri.path == "/v1/ip-calls/report":
                    from .reporting import call_report
                    qs = parse_qs(uri.query, keep_blank_values=True)
                    if (set(qs) - {"start", "end"}
                            or any(len(values) != 1 for values in qs.values())):
                        raise CallError("invalid_report_window")
                    start = int(qs["start"][0]) if "start" in qs else None
                    end = int(qs["end"][0]) if "end" in qs else None
                    return self._reply(200, call_report(
                        registry, claims, start=start, end=end,
                    ))
                if self.command == "GET" and uri.path == "/v1/ip-calls/events":
                    qs = parse_qs(uri.query)
                    after = int(qs.get("after", ["0"])[0])
                    return self._reply(200, registry.inbox(claims, after))
                if self.command == "POST" and uri.path == "/v1/ip-calls":
                    body = self._body()
                    return self._reply(201, registry.originate(
                        claims, body.get("to"), body.get("idempotency_key")))
                parts = uri.path.strip("/").split("/")
                if len(parts) >= 3 and parts[:2] == ["v1", "ip-calls"]:
                    call_id = parts[2]
                    if len(parts) == 3 and self.command == "GET":
                        return self._reply(200, registry.status(claims, call_id))
                    if len(parts) == 4 and self.command == "POST":
                        if parts[3] in ("accept", "decline", "hangup"):
                            return self._reply(200, registry.decide(claims, call_id, parts[3]))
                        if parts[3] == "media-token":
                            body = self._body() if int(
                                self.headers.get("Content-Length", "0") or "0"
                            ) > 0 else {}
                            return self._reply(200, registry.media_credentials(
                                claims, call_id, inbound_lease=body.get("lease"),
                            ))
                        if parts[3] == "handoff":
                            body = self._body()
                            return self._reply(201, registry.offer_handoff(
                                claims, call_id,
                                idempotency_key=body.get("idempotency_key"),
                                lease=body.get("lease")))
                    if (len(parts) == 5 and self.command == "POST"
                            and parts[3] == "inbound"):
                        if parts[4] == "claim":
                            return self._reply(200, registry.claim_inbound(
                                claims, call_id))
                        if parts[4] == "heartbeat":
                            return self._reply(200, registry.heartbeat_inbound(
                                claims, call_id, self._body().get("lease")))
                        if parts[4] == "finish":
                            return self._reply(200, registry.finish_inbound(
                                claims, call_id, self._body().get("lease")))
                    if (len(parts) == 5 and self.command == "POST"
                            and parts[3] == "handoff"
                            and parts[4] in ("accept", "decline", "cancel", "complete")):
                        body = self._body() if int(
                            self.headers.get("Content-Length", "0") or "0"
                        ) > 0 else {}
                        return self._reply(200, registry.decide_handoff(
                            claims, call_id, parts[4], lease=body.get("lease")))
                raise CallError("not_found", 404)
            except CallError as exc:
                return self._reply(exc.status, {"error": exc.code})
            except (ValueError, TypeError) as exc:
                return self._reply(400, {"error": "invalid_parameter"})

        def do_GET(self):
            self._request()

        def do_POST(self):
            self._request()

        def do_DELETE(self):
            self._request()

        def log_message(self, format, *args):
            # Avoid emitting auth tokens, caller identities and call IDs.
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Internet-only headless call control")
    sub = parser.add_subparsers(dest="cmd", required=True)
    token = sub.add_parser("token", help="Mint an identity token locally, never via HTTP")
    token.add_argument("--tenant", required=True)
    token.add_argument("--subject", required=True)
    token.add_argument("--role", choices=["human", "agent"], required=True)
    dnd = sub.add_parser("dnd", help="Local per-user do-not-disturb setting")
    dnd.add_argument("--tenant", required=True)
    dnd.add_argument("--subject", required=True)
    dnd.add_argument("--enabled", action="store_true")
    dnd.add_argument("--disabled", action="store_true")
    incoming = sub.add_parser(
        "inbound-agent", help="Trusted operator: enable AI to answer Internet calls")
    incoming.add_argument("--tenant", required=True)
    incoming.add_argument("--assistant", required=True)
    incoming.add_argument("--enabled", action="store_true")
    incoming.add_argument("--disabled", action="store_true")
    handoff = sub.add_parser(
        "handoff-route", help="Trusted operator: map AI identity to human staff")
    handoff.add_argument("--tenant", required=True)
    handoff.add_argument("--assistant", required=True)
    handoff.add_argument("--staff")
    handoff.add_argument("--disable", action="store_true")
    enroll = sub.add_parser("enroll", help="Local trusted enrollment/agent-call consent")
    enroll.add_argument("--tenant", required=True)
    enroll.add_argument("--subject", required=True)
    enroll.add_argument("--allow-agent-calls", action="store_true")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8874)
    args = parser.parse_args()
    secret = os.environ.get("PB_CALL_IDENTITY_SECRET", "")
    if len(secret) < 32:
        parser.error("PB_CALL_IDENTITY_SECRET must have >=32 characters")
    if args.cmd == "token":
        print(mint_identity_token(secret, args.tenant, args.subject, args.role))
        return
    path = os.environ.get("PB_CALL_DB", "")
    if not path or not Path(path).is_absolute():
        parser.error("PB_CALL_DB must be an absolute path")
    os.umask(0o077)
    registry = CallRegistry(
        path, identity_secret=secret,
        media_key=os.environ.get("LIVEKIT_API_KEY", ""),
        media_secret=os.environ.get("LIVEKIT_API_SECRET", ""),
        media_url=os.environ.get("LIVEKIT_URL", ""),
        push_public_key=os.environ.get("PB_WEBPUSH_PUBLIC_KEY", ""),
    )
    if args.cmd == "enroll":
        registry.provision(args.tenant, args.subject, allow_agent_calls=args.allow_agent_calls)
        return
    if args.cmd == "dnd":
        if args.enabled == args.disabled:
            parser.error("select exactly one of --enabled or --disabled")
        registry.set_do_not_disturb(args.tenant, args.subject, args.enabled)
        return
    if args.cmd == "inbound-agent":
        if args.enabled == args.disabled:
            parser.error("select exactly one of --enabled or --disabled")
        registry.configure_inbound_agent(
            args.tenant, args.assistant, args.enabled,
        )
        return
    if args.cmd == "handoff-route":
        if args.disable == bool(args.staff):
            parser.error("select either --staff or --disable")
        registry.configure_handoff_route(
            args.tenant, args.assistant, None if args.disable else args.staff,
        )
        return
    server = BoundedCallHTTPServer(("127.0.0.1", args.port),
                                   make_handler(registry))
    stop = threading.Event()
    worker = None
    terminator = None
    notes = None
    if registry.media_url:
        # Only the configured self-hosted LiveKit is contacted. The worker
        # reclaims rooms on hangup, timeout and restart, without paid APIs.
        from .media_cleanup import LiveKitRoomTerminator
        terminator = LiveKitRoomTerminator(
            registry.media_url, registry.media_key, registry.media_secret,
        )
    notes_path = os.environ.get("PB_CALL_NOTES_DB", "").strip()
    if notes_path:
        from .call_notes import CallNotesStore
        notes = CallNotesStore(notes_path)

    push_sender = None
    if registry.push_public_key:
        from .web_push import make_web_push_sender
        push_sender = make_web_push_sender(
            os.environ.get("PB_WEBPUSH_PRIVATE_KEY", ""),
            os.environ.get("PB_WEBPUSH_CONTACT", ""),
        )

    if terminator is not None or notes is not None or push_sender is not None:
        def reconcile_loop():
            import sys
            last_notes_sweep = 0.0
            while not stop.is_set():
                if terminator is not None:
                    try:
                        registry.reconcile_media(terminator.delete_room)
                        registry.reconcile_handoff_kicks(terminator.remove_participant)
                    except (sqlite3.Error, OSError) as exc:
                        # Never emit call identities, notes or secrets.
                        print("ip_media_reconcile_error:" + type(exc).__name__,
                              file=sys.stderr)
                if push_sender is not None:
                    try:
                        registry.reconcile_push(push_sender)
                    except (sqlite3.Error, OSError) as exc:
                        # Credentials, endpoint URLs and subjects are private.
                        print("ip_webpush_reconcile_error:" + type(exc).__name__,
                              file=sys.stderr)
                current = time.monotonic()
                if notes is not None and current - last_notes_sweep >= 3600:
                    try:
                        notes.prune_expired()
                        last_notes_sweep = current
                    except (sqlite3.Error, OSError) as exc:
                        print("ip_note_retention_error:" + type(exc).__name__,
                              file=sys.stderr)
                stop.wait(MEDIA_CLEANUP_INTERVAL_SECONDS if
                          terminator is not None or push_sender is not None
                          else 3600)

        worker = threading.Thread(target=reconcile_loop, daemon=True,
                                  name="ip-control-maintenance")
        worker.start()
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        if worker is not None:
            worker.join(timeout=5)


if __name__ == "__main__":
    main()
