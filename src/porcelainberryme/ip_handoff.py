"""Warm AI-to-human handoff within an existing self-hosted LiveKit room.

This module owns only the authenticated control-plane workflow. A handoff
does not call a public telephone number, silently redirect a customer, or
move a caller to a second room. It offers a staff member a seat in the
existing room; the originating agent leaves only after observing that staff
has joined with its provisioned microphone identity.
"""
from __future__ import annotations

from contextlib import closing

HANDOFF_OFFER_SECONDS = 45
HANDOFF_JOIN_SECONDS = 60
ACTIVE_HANDOFF = ("OFFERED", "ACCEPTED", "CONNECTED")


class HandoffMixin:
    def _init_handoff_schema(self, con):
        con.executescript("""
            CREATE TABLE IF NOT EXISTS ip_handoff_routes (
              tenant TEXT NOT NULL, assistant TEXT NOT NULL,
              staff TEXT NOT NULL, PRIMARY KEY(tenant, assistant)
            );
            CREATE TABLE IF NOT EXISTS ip_call_handoffs (
              call_id TEXT PRIMARY KEY,
              tenant TEXT NOT NULL, assistant TEXT NOT NULL,
              target TEXT NOT NULL,
              state TEXT NOT NULL CHECK(state IN
                ('OFFERED','ACCEPTED','CONNECTED','DECLINED','CANCELLED','EXPIRED')),
              idem TEXT NOT NULL,
              created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
              media_issued INTEGER NOT NULL DEFAULT 0,
              connected_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS ip_handoff_target
              ON ip_call_handoffs(tenant,target,state);
        """)

    def configure_handoff_route(self, tenant, assistant, staff):
        """Trusted local enrollment, not an unauthenticated HTTP endpoint."""
        from .ip_calls import CallError, _identity
        _identity(tenant)
        _identity(assistant)
        if staff is not None:
            _identity(staff)
            if staff == assistant:
                raise CallError("handoff_staff_must_be_human")
        with closing(self._conn()) as con:
            with con:
                if staff is None:
                    con.execute(
                        "DELETE FROM ip_handoff_routes WHERE tenant=? AND assistant=?",
                        (tenant, assistant),
                    )
                    return
                enrolled = con.execute(
                    "SELECT 1 FROM ip_call_users WHERE tenant=? AND subject=?",
                    (tenant, staff),
                ).fetchone()
                if enrolled is None:
                    raise CallError("handoff_staff_not_enrolled", 404)
                con.execute(
                    "INSERT INTO ip_handoff_routes VALUES(?,?,?) "
                    "ON CONFLICT(tenant,assistant) DO UPDATE SET staff=excluded.staff",
                    (tenant, assistant, staff),
                )

    @staticmethod
    def _handoff_row(con, call_id):
        return con.execute(
            "SELECT * FROM ip_call_handoffs WHERE call_id=?", (call_id,),
        ).fetchone()

    @staticmethod
    def _handoff_visible(row):
        if row is None:
            return None
        return {
            "state": row["state"],
            "target": row["target"],
            "expires_at": row["expires_at"],
        }

    def _handoff_with_call(self, con, call, viewer):
        result = self._visible(call, viewer)
        if call["state"] == "ACTIVE":
            result["handoff"] = self._handoff_visible(
                self._handoff_row(con, call["id"])
            )
        return result

    def _expire_handoffs(self, con, now):
        # Expiry happens in the same transaction as other call transitions.
        overdue = con.execute(
            "SELECT h.call_id AS handoff_call_id,h.target AS handoff_staff,c.* "
            "FROM ip_call_handoffs h JOIN ip_calls c ON c.id=h.call_id "
            "WHERE h.state IN ('OFFERED','ACCEPTED') AND h.expires_at<=?",
            (now,),
        ).fetchall()
        for call in overdue:
            con.execute(
                "UPDATE ip_call_handoffs SET state='EXPIRED' WHERE call_id=?",
                (call["id"],),
            )
            for user in (call["caller"], call["callee"], call["handoff_staff"]):
                self._event(con, call, user, "HANDOFF_EXPIRED", now)

    def offer_handoff(self, claims, call_id, *, idempotency_key):
        """Only the originating AI identity may offer a configured human."""
        from .ip_calls import CallError, IDEMPOTENCY
        if claims["role"] != "agent":
            raise CallError("only_agent_can_offer_handoff", 403)
        if (not isinstance(idempotency_key, str)
                or IDEMPOTENCY.fullmatch(idempotency_key) is None):
            raise CallError("stable_idempotency_key_required")
        now = self._now()
        with closing(self._open()) as con:
            with con:
                call = self._get(con, claims["tenant"], claims["sub"], call_id)
                if call["caller"] != claims["sub"] or not call["agent"]:
                    raise CallError("only_originating_agent_can_transfer", 403)
                if call["state"] != "ACTIVE":
                    raise CallError("handoff_requires_active_call", 409)
                old = self._handoff_row(con, call_id)
                if old is not None:
                    if old["idem"] == idempotency_key:
                        return self._handoff_visible(old)
                    if old["state"] in ACTIVE_HANDOFF:
                        raise CallError("handoff_already_pending", 409)
                route = con.execute(
                    "SELECT staff FROM ip_handoff_routes "
                    "WHERE tenant=? AND assistant=?",
                    (call["tenant"], call["caller"]),
                ).fetchone()
                if route is None:
                    raise CallError("handoff_route_not_configured", 409)
                target = route["staff"]
                if target in (call["caller"], call["callee"]):
                    raise CallError("handoff_invalid_staff", 409)
                user = con.execute(
                    "SELECT do_not_disturb FROM ip_call_users "
                    "WHERE tenant=? AND subject=?",
                    (call["tenant"], target),
                ).fetchone()
                if user is None:
                    raise CallError("handoff_staff_not_enrolled", 409)
                if user["do_not_disturb"]:
                    raise CallError("handoff_staff_do_not_disturb", 409)
                busy = con.execute(
                    "SELECT 1 FROM ip_calls c WHERE c.tenant=? "
                    "AND c.state IN ('RINGING','ACTIVE') "
                    "AND (c.caller=? OR c.callee=? OR "
                    "EXISTS(SELECT 1 FROM ip_call_handoffs h "
                    "WHERE h.call_id=c.id AND h.target=? "
                    "AND h.state IN ('OFFERED','ACCEPTED','CONNECTED'))) LIMIT 1",
                    (call["tenant"], target, target, target),
                ).fetchone()
                if busy:
                    raise CallError("handoff_staff_busy", 409)
                expires = min(call["expires_at"], now + HANDOFF_OFFER_SECONDS)
                con.execute(
                    "INSERT INTO ip_call_handoffs("
                    "call_id,tenant,assistant,target,state,idem,created_at,"
                    "expires_at,media_issued,connected_at) "
                    "VALUES(?,?,?,?,'OFFERED',?,?,?,0,NULL) "
                    "ON CONFLICT(call_id) DO UPDATE SET "
                    "target=excluded.target,state='OFFERED',"
                    "idem=excluded.idem,created_at=excluded.created_at,"
                    "expires_at=excluded.expires_at,media_issued=0,connected_at=NULL",
                    (call_id, call["tenant"], call["caller"], target,
                     idempotency_key, now, expires),
                )
                for recipient in (call["caller"], call["callee"], target):
                    self._event(
                        con, call, recipient,
                        "HANDOFF_OFFERED" if recipient == target else "HANDOFF_PENDING",
                        now,
                    )
                return self._handoff_visible(self._handoff_row(con, call_id))

    def decide_handoff(self, claims, call_id, action):
        from .ip_calls import CallError
        if action not in ("accept", "decline", "cancel", "complete"):
            raise CallError("invalid_handoff_action")
        now = self._now()
        with closing(self._open()) as con:
            with con:
                call = self._get(con, claims["tenant"], claims["sub"], call_id)
                handoff = self._handoff_row(con, call_id)
                if handoff is None:
                    raise CallError("handoff_not_found", 404)
                if call["state"] != "ACTIVE":
                    raise CallError("handoff_requires_active_call", 409)
                actor = claims["sub"]
                if action in ("accept", "decline"):
                    if actor != handoff["target"] or claims["role"] != "human":
                        raise CallError("only_staff_can_decide_handoff", 403)
                    expected = ("OFFERED",)
                else:
                    if actor != call["caller"] or claims["role"] != "agent":
                        raise CallError("only_originating_agent_can_transfer", 403)
                    expected = ("OFFERED", "ACCEPTED") if action == "cancel" else ("ACCEPTED",)
                target = {
                    "accept": "ACCEPTED", "decline": "DECLINED",
                    "cancel": "CANCELLED", "complete": "CONNECTED",
                }[action]
                if handoff["state"] == target:
                    return self._handoff_visible(handoff)
                if handoff["state"] not in expected:
                    raise CallError("invalid_handoff_transition", 409)
                if handoff["expires_at"] <= now:
                    raise CallError("handoff_expired", 409)
                if action == "complete" and not handoff["media_issued"]:
                    raise CallError("handoff_staff_media_not_issued", 409)
                expires = (
                    min(call["expires_at"], now + HANDOFF_JOIN_SECONDS)
                    if action == "accept" else handoff["expires_at"]
                )
                con.execute(
                    "UPDATE ip_call_handoffs SET "
                    "state=?,expires_at=?,connected_at=? WHERE call_id=?",
                    (target, expires, now if action == "complete" else None, call_id),
                )
                for recipient in (call["caller"], call["callee"], handoff["target"]):
                    self._event(con, call, recipient, "HANDOFF_" + target, now)
                return self._handoff_visible(self._handoff_row(con, call_id))
