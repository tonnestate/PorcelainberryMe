"""Privacy-preserving, bounded SQL aggregates for retained Internet calls.

No call ID, recipient, transcript, audio or bearer token leaves SQLite.
Only the authenticated participant's own tenant-bound sessions are counted.
This is call-control duration, not measured audio or carrier billing.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone

from .ip_calls import CALL_HISTORY_RETENTION_SECONDS, CallError, _identity

MAX_REPORT_SECONDS = 30 * 86400
DEFAULT_REPORT_SECONDS = 7 * 86400

_COUNTERS = (
    "calls", "incoming", "outgoing", "agent_initiated",
    "accepted_confirmed", "declined", "timed_out", "ended",
    "ringing", "active", "transferred_confirmed", "duration_known_calls",
    "control_session_seconds", "acceptance_unverified",
)
_DAILY = (
    "calls", "incoming", "outgoing", "accepted_confirmed",
    "declined", "timed_out", "transferred_confirmed",
    "control_session_seconds",
)


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
    """Use SQLite grouping rather than importing potentially huge PII rows.

    Recipient and caller have individual indexes; no tenant-wide querying
    or reporting over staff-only handoff relationships is authorized.
    """
    if not isinstance(claims, dict):
        raise CallError("report_identity_required", 403)
    tenant = _identity(claims.get("tenant"))
    subject = _identity(claims.get("sub"))
    if claims.get("role") not in ("human", "agent"):
        raise CallError("report_identity_required", 403)
    start, end = _window(registry, start, end)
    with closing(registry._open()) as db:
        rows = db.execute("""
            WITH scoped AS (
              SELECT c.state, c.agent, c.caller, c.callee,
                     c.created_at, c.closed_at, c.accepted_at,
                     CASE WHEN EXISTS (
                       SELECT 1 FROM ip_call_handoffs h
                        WHERE h.call_id=c.id AND h.tenant=c.tenant
                          AND h.state='CONNECTED'
                     ) THEN 1 ELSE 0 END AS transferred
                FROM ip_calls c
               WHERE c.tenant=?
                 AND (c.caller=? OR c.callee=?)
                 AND c.created_at>=? AND c.created_at<?
            )
            SELECT strftime('%Y-%m-%d', created_at, 'unixepoch') AS day,
                   COUNT(*) AS calls,
                   SUM(CASE WHEN caller=? THEN 1 ELSE 0 END) AS outgoing,
                   SUM(CASE WHEN callee=? THEN 1 ELSE 0 END) AS incoming,
                   SUM(CASE WHEN agent<>0 THEN 1 ELSE 0 END) AS agent_initiated,
                   SUM(CASE WHEN accepted_at IS NOT NULL THEN 1 ELSE 0 END)
                     AS accepted_confirmed,
                   SUM(CASE WHEN state='DECLINED' THEN 1 ELSE 0 END) AS declined,
                   SUM(CASE WHEN state='TIMED_OUT' THEN 1 ELSE 0 END) AS timed_out,
                   SUM(CASE WHEN state='ENDED' THEN 1 ELSE 0 END) AS ended,
                   SUM(CASE WHEN state='RINGING' THEN 1 ELSE 0 END) AS ringing,
                   SUM(CASE WHEN state='ACTIVE' THEN 1 ELSE 0 END) AS active,
                   SUM(transferred) AS transferred_confirmed,
                   SUM(CASE WHEN accepted_at IS NOT NULL AND closed_at IS NOT NULL
                       THEN 1 ELSE 0 END) AS duration_known_calls,
                   SUM(CASE WHEN accepted_at IS NOT NULL AND closed_at IS NOT NULL
                       THEN MAX(0, closed_at - accepted_at) ELSE 0 END)
                     AS control_session_seconds,
                   SUM(CASE WHEN accepted_at IS NULL
                             AND state IN ('ACTIVE','ENDED')
                       THEN 1 ELSE 0 END) AS acceptance_unverified
              FROM scoped
             GROUP BY day ORDER BY day
        """, (tenant, subject, subject, start, end, subject, subject)).fetchall()

    metrics = {key: 0 for key in _COUNTERS}
    daily = []
    for row in rows:
        for key in _COUNTERS:
            metrics[key] += int(row[key])
        daily.append({"day": row["day"],
                      **{key: int(row[key]) for key in _DAILY}})

    return {
        "schema": "porcelainberryme.reporting.calls.v1",
        "scope": "authenticated_participant_only",
        "timezone": "UTC",
        "from": _utc(start), "to_exclusive": _utc(end),
        "history_max_days": CALL_HISTORY_RETENTION_SECONDS // 86400,
        "metrics": metrics,
        "daily": daily,
        "limitations": {
            "duration": "accepted-to-closed control-plane time, not measured RTP/audio talk time",
            "historical_acceptance": "unknown for pre-migration calls without accepted_at",
            "cost": "not measured; no provider billing or SIP rate data",
            "outcomes": "CRM resolution, lead conversion and bookings not inferred",
            "retention": "rolling 30 days; historic data outside retention unavailable",
        },
    }
