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
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

IDENTITY = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
IDEMPOTENCY = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
CALL_ID = re.compile(r"^[a-f0-9]{32}$")
AUDIENCE = "porcelainberryme.ip-calls.v1"
RING_SECONDS = 45
MAX_CALL_SECONDS = 3600
MAX_BODY = 4096


class CallError(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def _identity(value: str) -> str:
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise CallError("invalid_identity")
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


class CallRegistry:
    """SQLite transactions serialize invitations, acceptance and termination.

    Client identity is always sourced from a server-verified token, never
    from request payload fields. Room tokens are minted only after acceptance.
    """

    def __init__(self, path: str, *, identity_secret: str, media_key: str,
                 media_secret: str, media_url: str,
                 clock=None):
        if not isinstance(identity_secret, str) or len(identity_secret) < 32:
            raise ValueError("identity_secret_too_short")
        if not isinstance(media_key, str) or not media_key.strip():
            raise ValueError("media_key_required")
        if not isinstance(media_secret, str) or len(media_secret) < 32:
            raise ValueError("media_secret_too_short")
        if not isinstance(media_url, str) or not media_url.startswith(("ws://", "wss://")):
            raise ValueError("invalid_media_url")
        if identity_secret == media_secret:
            raise ValueError("separate_signing_keys_required")
        self.path = path
        self.identity_secret = identity_secret
        self.media_key = media_key
        self.media_secret = media_secret
        self.media_url = media_url
        self.clock = clock or time.time
        with closing(self._conn()) as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS ip_calls (
                  id TEXT PRIMARY KEY, tenant TEXT NOT NULL,
                  caller TEXT NOT NULL, callee TEXT NOT NULL,
                  agent INTEGER NOT NULL, state TEXT NOT NULL,
                  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                  idem TEXT NOT NULL,
                  UNIQUE(tenant, caller, idem)
                );
                CREATE TABLE IF NOT EXISTS ip_call_events (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  call_id TEXT NOT NULL, tenant TEXT NOT NULL,
                  receiver TEXT NOT NULL, kind TEXT NOT NULL,
                  created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ip_call_inbox
                  ON ip_call_events(tenant, receiver, seq);
            """)

    def _conn(self):
        con = sqlite3.connect(self.path, timeout=20)
        con.row_factory = sqlite3.Row
        return con

    def _now(self):
        return int(self.clock())

    def _event(self, con, call, receiver: str, kind: str, now: int):
        con.execute(
            "INSERT INTO ip_call_events(call_id, tenant, receiver, kind, created_at) "
            "VALUES(?,?,?,?,?)",
            (call["id"], call["tenant"], receiver, kind, now),
        )

    def _transition_expired(self, con, now):
        rows = con.execute(
            "SELECT * FROM ip_calls WHERE state IN ('RINGING', 'ACTIVE') "
            "AND expires_at<=?", (now,)
        ).fetchall()
        for call in rows:
            con.execute("UPDATE ip_calls SET state='TIMED_OUT' WHERE id=?", (call["id"],))
            self._event(con, call, call["caller"], "TIMED_OUT", now)
            self._event(con, call, call["callee"], "TIMED_OUT", now)

    def _open(self):
        con = self._conn()
        con.execute("BEGIN IMMEDIATE")
        self._transition_expired(con, self._now())
        return con

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
            "AND (caller=? OR callee=?)",
            (call_id, tenant, subject, subject),
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
                busy = con.execute(
                    "SELECT 1 FROM ip_calls WHERE tenant=? "
                    "AND state IN ('RINGING', 'ACTIVE') "
                    "AND (caller IN (?, ?) OR callee IN (?, ?)) LIMIT 1",
                    (tenant, caller, callee, caller, callee),
                ).fetchone()
                if busy:
                    raise CallError("endpoint_busy", 409)
                call_id = uuid4().hex
                con.execute(
                    "INSERT INTO ip_calls VALUES(?,?,?,?,?,?,?,?,?)",
                    (call_id, tenant, caller, callee, int(claims["role"] == "agent"),
                     "RINGING", now, now + RING_SECONDS, idem),
                )
                call = con.execute("SELECT * FROM ip_calls WHERE id=?", (call_id,)).fetchone()
                self._event(con, call, callee, "RINGING", now)
                self._event(con, call, caller, "OUTGOING", now)
                return self._visible(call, caller)

    def status(self, claims: dict, call_id: str) -> dict:
        with closing(self._open()) as con:
            with con:
                call = self._get(con, claims["tenant"], claims["sub"], call_id)
                return self._visible(call, claims["sub"])

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
                    events.append({"cursor": row["seq"], "kind": row["kind"],
                                   "call": self._visible(call, claims["sub"])})
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
                target = {"accept": "ACTIVE", "decline": "DECLINED",
                          "hangup": "ENDED"}[decision]
                expected = ("RINGING",) if decision != "hangup" else ("RINGING", "ACTIVE")
                if call["state"] == target:
                    return self._visible(call, claims["sub"])
                if call["state"] not in expected:
                    raise CallError("invalid_call_transition", 409)
                expires_at = now + MAX_CALL_SECONDS if decision == "accept" else now
                con.execute("UPDATE ip_calls SET state=?, expires_at=? WHERE id=?",
                            (target, expires_at, call_id))
                for user in (call["caller"], call["callee"]):
                    self._event(con, call, user, target, now)
                changed = con.execute("SELECT * FROM ip_calls WHERE id=?", (call_id,)).fetchone()
                return self._visible(changed, claims["sub"])

    def media_credentials(self, claims: dict, call_id: str) -> dict:
        now = self._now()
        with closing(self._open()) as con:
            with con:
                call = self._get(con, claims["tenant"], claims["sub"], call_id)
                if call["state"] != "ACTIVE":
                    raise CallError("call_not_accepted", 409)
                # Opaque identity, unique per call. Do not embed real contact data.
                participant = "ip_" + hashlib.sha256(
                    (call_id + ":" + claims["tenant"] + ":" + claims["sub"]).encode()
                ).hexdigest()[:24]
                room = "pb_" + call_id
                jwt = _jwt({
                    "iss": self.media_key, "sub": participant,
                    "iat": now, "nbf": now - 1, "exp": min(now + 60, call["expires_at"]),
                    "video": {
                        "room": room, "roomJoin": True,
                        "canPublish": True, "canSubscribe": True,
                        "canPublishSources": ["microphone"],
                        "canPublishData": False,
                    },
                }, self.media_secret)
                return {"url": self.media_url, "room": room,
                        "participant": participant, "token": jwt,
                        "expires_at": min(now + 60, call["expires_at"])}


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
                claims = self._auth()
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
                            return self._reply(200, registry.media_credentials(claims, call_id))
                raise CallError("not_found", 404)
            except CallError as exc:
                return self._reply(exc.status, {"error": exc.code})
            except (ValueError, TypeError) as exc:
                return self._reply(400, {"error": "invalid_parameter"})

        def do_GET(self):
            self._request()

        def do_POST(self):
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
    )
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(registry))
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
