"""Privacy-preserving, scoped operational analytics for retained Internet calls.

Only established call-control facts are reported. This is not a transcript,
billing system, customer/CRM conversion attribution or an audio-duration meter.
Call data have the existing 30-day control-plane retention, and historic
accepted_at unknowns must never be treated as confirmed unanswered calls.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone

from .ip_calls import (
    CALL_HISTORY_RETENTION_SECONDS, CallError, _identity,
)

MAX_REPORT_SECONDS = 30 * 86400
DEFAULT_REPORT_SECONDS = 7 * 86400


def _window(registry, start, end):
    now = registry._now()
    if end is None:
        end = now + 1
    if start is None:
        start = end - DEFAULT_REPORT_SECONDS
    if (type(start) is not int or type(end) is not int
            or start >= end or end > now + 1
            or start < now - CALL_HISTORY_RETENTION_SECONDS
            or end - start > MAX_REPORT_SECONDS):
        raise CallError("invalid_report_window")
    return start, end


def _utc(stamp):
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()


def call_report(registry, claims: dict, *, start: int | None = None,
                end: int | None = None) -> dict:
    """Read only calls in which this signed tenant/identity participated.

    Staff handoff-only access is intentionally excluded, even after an
    offer was accepted. A full tenant dashboard needs a separate operator
    permission, never a broadened agent/ordinary-caller bearer.
    """
    if not isinstance(claims, dict):
        raise CallError("report_identity_required", 403)
    tenant = _identity(claims.get("tenant"))
    subject = _identity(claims.get("sub"))
    if claims.get("role") not in ("human", "agent"):
        raise CallError("report_identity_required", 403)
    start, end = _window(registry, start, end)
    with closing(registry._open()) as db:
        # The call registry itself runs expiration and retention transitions
        # before this SELECT, so no displayed state is a stale ring.
        rows = db.execute("""
            SELECT c.id, c.state, c.agent, c.caller, c.callee,
                   c.created_at, c.closed_at, c.accepted_at,
                   CASE WHEN EXISTS (
                     SELECT 1 FROM ip_call_handoffs h
                      WHERE h.call_id=c.id AND h.tenant=c.tenant
                        AND h.state='CONNECTED'
                   ) THEN 1 ELSE 0 END AS transferred
              FROM ip_calls c
             WHERE c.tenant=? AND (c.caller=? OR c.callee=?)
               AND c.created_at>=? AND c.created_at<?
             ORDER BY c.created_at, c.id
        """, (tenant, subject, subject, start, end)).fetchall()

    counters = {
        "calls": 0, "incoming": 0, "outgoing": 0,
        "agent_initiated": 0,
        "accepted_confirmed": 0,
        "declined": 0, "timed_out": 0, "ended": 0,
        "ringing": 0, "active": 0,
        "transferred_confirmed": 0,
        "duration_known_calls": 0, "control_session_seconds": 0,
        "acceptance_unverified": 0,
    }
    per_day = {}
    for call in rows:
        day = datetime.fromtimestamp(
            call["created_at"], tz=timezone.utc,
        ).date().isoformat()
        item = per_day.setdefault(day, {
            "day": day, "calls": 0, "incoming": 0, "outgoing": 0,
            "accepted_confirmed": 0, "declined": 0,
            "timed_out": 0, "transferred_confirmed": 0,
            "control_session_seconds": 0,
        })
        direction = "outgoing" if call["caller"] == subject else "incoming"
        for stats in (counters, item):
            stats["calls"] += 1
            stats[direction] += 1
        if call["agent"]:
            counters["agent_initiated"] += 1
        state = call["state"].lower()
        if state in ("ringing", "active", "ended", "declined", "timed_out"):
            counters[state] += 1
            if state in item:
                item[state] += 1
        accepted = call["accepted_at"]
        if accepted is not None:
            counters["accepted_confirmed"] += 1
            item["accepted_confirmed"] += 1
        elif state in ("active", "ended"):
            # Pre-migration calls may have been answered, but cannot be
            # reconstructed after ephemeral ACTIVE events have been pruned.
            counters["acceptance_unverified"] += 1
        if call["transferred"]:
            counters["transferred_confirmed"] += 1
            item["transferred_confirmed"] += 1
        closed = call["closed_at"]
        if accepted is not None and closed is not None:
            seconds = max(0, closed - accepted)
            counters["duration_known_calls"] += 1
            counters["control_session_seconds"] += seconds
            item["control_session_seconds"] += seconds
    return {
        "schema": "porcelainberryme.reporting.calls.v1",
        "scope": "authenticated_participant_only",
        "timezone": "UTC",
        "from": _utc(start), "to_exclusive": _utc(end),
        "history_max_days": CALL_HISTORY_RETENTION_SECONDS // 86400,
        "metrics": counters,
        "daily": [per_day[key] for key in sorted(per_day)],
        "limitations": {
            "duration": "accepted-to-closed control-plane time, not measured RTP/audio talk time",
            "historical_acceptance": "unknown for pre-migration calls without accepted_at",
            "cost": "not measured; no provider billing or SIP rate data",
            "outcomes": "CRM resolution, lead conversion and bookings not inferred",
            "retention": "rolling 30 days; historic data outside retention unavailable",
        },
    }
