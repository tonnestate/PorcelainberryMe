"""Free, self-hosted, auditable appointment calendar for PorcelainberryMe.

No external calendar or PSTN provider. Deterministic slots, explicit confirmation,
UTC-backed transaction-safe booking and tenant/assistant/caller isolation.
Voice syntax intentionally narrow: unsupported spoken dates are not guessed.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .agent_knowledge import DEFAULT_GREETING

ID = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
REQUEST = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
LOCAL = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
CLOCK = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
SPOKEN_DE = re.compile(
    r"\b(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?P<year>\d{4})"
    r"\s*(?:um\s*)?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?"
    r"\s*(?:uhr)?\b", re.IGNORECASE,
)
SPOKEN_ISO = re.compile(
    r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"[ t](?P<hour>\d{2}):(?P<minute>\d{2})\b",
    re.IGNORECASE,
)
CONFIRM = {
    "ja termin buchen",
    "ja bitte termin buchen",
    "ja bitte verbindlich buchen",
}
CANCEL = {"nein", "abbrechen", "doch nicht", "nein danke"}
DEFAULT_LEAD_MINUTES = 60
DEFAULT_HORIZON_DAYS = 90


class AppointmentError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _ident(value: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise AppointmentError("invalid_identity")
    return value


def _request(value: str) -> str:
    if not isinstance(value, str) or not REQUEST.fullmatch(value):
        raise AppointmentError("invalid_idempotency_key")
    return value


def _clock(value: str) -> int:
    if not isinstance(value, str) or not CLOCK.fullmatch(value):
        raise AppointmentError("invalid_business_hours")
    return int(value[:2]) * 60 + int(value[3:])


def _windows(schedule: dict) -> dict[str, list[tuple[int, int]]]:
    if not isinstance(schedule, dict) or not schedule:
        raise AppointmentError("explicit_weekly_hours_required")
    result: dict[str, list[tuple[int, int]]] = {}
    for day, windows in schedule.items():
        if not isinstance(day, str) or day not in {str(i) for i in range(7)}:
            raise AppointmentError("invalid_weekday")
        if not isinstance(windows, list) or len(windows) > 4:
            raise AppointmentError("invalid_business_windows")
        parsed = []
        for window in windows:
            if not isinstance(window, (list, tuple)) or len(window) != 2:
                raise AppointmentError("invalid_business_windows")
            start, end = _clock(window[0]), _clock(window[1])
            if start >= end:
                raise AppointmentError("invalid_business_windows")
            parsed.append((start, end))
        parsed.sort()
        if any(left[1] > right[0] for left, right in zip(parsed, parsed[1:])):
            raise AppointmentError("overlapping_business_hours")
        result[day] = parsed
    return result


@dataclass(frozen=True)
class CalendarConfig:
    timezone: str
    duration_min: int
    lead_min: int
    horizon_days: int
    weekly: dict[str, list[tuple[int, int]]]


class AppointmentStore:
    def __init__(self, path: str, *, clock=None):
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise AppointmentError("absolute_calendar_database_required")
        self.path = path
        self.clock = clock or time.time
        with closing(self._conn()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS pb_calendars (
                    tenant TEXT NOT NULL,
                    assistant TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    duration_min INTEGER NOT NULL,
                    lead_min INTEGER NOT NULL,
                    horizon_days INTEGER NOT NULL,
                    weekly_json TEXT NOT NULL,
                    PRIMARY KEY(tenant,assistant)
                );
                CREATE TABLE IF NOT EXISTS pb_appointments (
                    id TEXT PRIMARY KEY,
                    tenant TEXT NOT NULL,
                    assistant TEXT NOT NULL,
                    caller TEXT NOT NULL,
                    local_start TEXT NOT NULL,
                    start_utc INTEGER NOT NULL,
                    end_utc INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('BOOKED','CANCELLED')),
                    idem TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE(tenant,assistant,caller,idem)
                );
                CREATE INDEX IF NOT EXISTS pb_calendar_occupied
                    ON pb_appointments(tenant,assistant,start_utc,end_utc,status);
            """)

    def _conn(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def configure(self, tenant: str, assistant: str, *, tz: str,
                  weekly: dict, duration_min: int,
                  lead_min: int = DEFAULT_LEAD_MINUTES,
                  horizon_days: int = DEFAULT_HORIZON_DAYS) -> CalendarConfig:
        _ident(tenant)
        _ident(assistant)
        if type(duration_min) is not int or duration_min < 15 or duration_min > 120:
            raise AppointmentError("invalid_duration")
        if type(lead_min) is not int or not 0 <= lead_min <= 10080:
            raise AppointmentError("invalid_lead_time")
        if type(horizon_days) is not int or not 1 <= horizon_days <= 365:
            raise AppointmentError("invalid_horizon")
        if not isinstance(tz, str):
            raise AppointmentError("explicit_timezone_required")
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            raise AppointmentError("invalid_timezone") from exc
        parsed = _windows(weekly)
        config = CalendarConfig(tz, duration_min, lead_min, horizon_days, parsed)
        with closing(self._conn()) as db:
            with db:
                db.execute(
                    "INSERT INTO pb_calendars VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(tenant,assistant) DO UPDATE SET "
                    "timezone=excluded.timezone,"
                    "duration_min=excluded.duration_min,"
                    "lead_min=excluded.lead_min,"
                    "horizon_days=excluded.horizon_days,"
                    "weekly_json=excluded.weekly_json",
                    (tenant, assistant, tz, duration_min, lead_min,
                     horizon_days, json.dumps(parsed, sort_keys=True)),
                )
        return config

    def _config(self, db, tenant: str, assistant: str) -> CalendarConfig:
        row = db.execute(
            "SELECT * FROM pb_calendars WHERE tenant=? AND assistant=?",
            (tenant, assistant),
        ).fetchone()
        if row is None:
            raise AppointmentError("assistant_calendar_not_configured")
        weekly = {day: [tuple(window) for window in windows]
                  for day, windows in json.loads(row["weekly_json"]).items()}
        return CalendarConfig(
            row["timezone"], row["duration_min"], row["lead_min"],
            row["horizon_days"], weekly,
        )

    def validate_calendar(self, tenant: str, assistant: str) -> CalendarConfig:
        _ident(tenant)
        _ident(assistant)
        with closing(self._conn()) as db:
            return self._config(db, tenant, assistant)

    @staticmethod
    def _utc(local_start: str, tz: str) -> tuple[int, datetime]:
        if not isinstance(local_start, str) or not LOCAL.fullmatch(local_start):
            raise AppointmentError("invalid_local_slot")
        try:
            naive = datetime.strptime(local_start, "%Y-%m-%dT%H:%M")
        except ValueError as exc:
            raise AppointmentError("invalid_local_slot") from exc
        zone = ZoneInfo(tz)
        a = naive.replace(tzinfo=zone, fold=0)
        b = naive.replace(tzinfo=zone, fold=1)
        # Reject DST gaps AND ambiguous local minutes. Never silently assign
        # a different UTC instant for the same user-spoken time.
        if a.utcoffset() != b.utcoffset():
            raise AppointmentError("ambiguous_or_nonexistent_local_slot")
        utc = a.astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) != naive:
            raise AppointmentError("nonexistent_local_slot")
        return int(utc.timestamp()), naive

    def _candidate(self, cfg: CalendarConfig, local_start: str) -> tuple[int, int]:
        start_utc, naive = self._utc(local_start, cfg.timezone)
        minutes = naive.hour * 60 + naive.minute
        weekday = str(naive.weekday())
        allowed = any(
            minutes >= begin and minutes + cfg.duration_min <= finish
            and (minutes - begin) % cfg.duration_min == 0
            for begin, finish in cfg.weekly.get(weekday, [])
        )
        if not allowed:
            raise AppointmentError("outside_configured_business_hours")
        now = int(self.clock())
        if start_utc < now + cfg.lead_min * 60:
            raise AppointmentError("slot_too_soon_or_past")
        if start_utc > now + cfg.horizon_days * 86400:
            raise AppointmentError("slot_beyond_horizon")
        return start_utc, start_utc + cfg.duration_min * 60

    @staticmethod
    def _occupied(db, tenant: str, assistant: str, start: int, end: int) -> bool:
        return db.execute(
            "SELECT 1 FROM pb_appointments WHERE tenant=? AND assistant=? "
            "AND status='BOOKED' AND start_utc<? AND end_utc>? LIMIT 1",
            (tenant, assistant, end, start),
        ).fetchone() is not None

    def check(self, tenant: str, assistant: str, local_start: str) -> bool:
        _ident(tenant)
        _ident(assistant)
        with closing(self._conn()) as db:
            cfg = self._config(db, tenant, assistant)
            start, end = self._candidate(cfg, local_start)
            return not self._occupied(db, tenant, assistant, start, end)

    def available(self, tenant: str, assistant: str, day: str) -> list[str]:
        _ident(tenant)
        _ident(assistant)
        try:
            date_obj = date.fromisoformat(day)
            if date_obj.isoformat() != day:
                raise ValueError("non_canonical_date")
        except (ValueError, TypeError) as exc:
            raise AppointmentError("invalid_calendar_date") from exc
        with closing(self._conn()) as db:
            cfg = self._config(db, tenant, assistant)
            slots = []
            for start, end in cfg.weekly.get(str(date_obj.weekday()), []):
                for minute in range(start, end - cfg.duration_min + 1, cfg.duration_min):
                    slot = f"{day}T{minute // 60:02d}:{minute % 60:02d}"
                    try:
                        utc_start, utc_end = self._candidate(cfg, slot)
                    except AppointmentError:
                        continue
                    if not self._occupied(db, tenant, assistant, utc_start, utc_end):
                        slots.append(slot)
            return slots

    def book(self, tenant: str, assistant: str, caller: str,
             local_start: str, *, idempotency_key: str) -> dict:
        _ident(tenant)
        _ident(assistant)
        _ident(caller)
        _request(idempotency_key)
        with closing(self._conn()) as db:
            db.execute("BEGIN IMMEDIATE")
            with db:
                prior = db.execute(
                    "SELECT * FROM pb_appointments "
                    "WHERE tenant=? AND assistant=? AND caller=? AND idem=?",
                    (tenant, assistant, caller, idempotency_key),
                ).fetchone()
                if prior:
                    if prior["local_start"] != local_start:
                        raise AppointmentError("booking_idempotency_conflict")
                    return dict(prior)
                cfg = self._config(db, tenant, assistant)
                start, end = self._candidate(cfg, local_start)
                if self._occupied(db, tenant, assistant, start, end):
                    raise AppointmentError("slot_already_booked")
                booking_id = uuid4().hex
                now = int(self.clock())
                db.execute(
                    "INSERT INTO pb_appointments("
                    "id,tenant,assistant,caller,local_start,start_utc,end_utc,"
                    "status,idem,created_at) VALUES(?,?,?,?,?,?,?,'BOOKED',?,?)",
                    (booking_id, tenant, assistant, caller, local_start,
                     start, end, idempotency_key, now),
                )
                return dict(db.execute(
                    "SELECT * FROM pb_appointments WHERE id=?", (booking_id,)
                ).fetchone())

    def cancel(self, tenant: str, assistant: str, caller: str,
               booking_id: str) -> dict:
        _ident(tenant)
        _ident(assistant)
        _ident(caller)
        if not isinstance(booking_id, str) or not re.fullmatch(r"[0-9a-f]{32}", booking_id):
            raise AppointmentError("booking_not_found")
        with closing(self._conn()) as db:
            db.execute("BEGIN IMMEDIATE")
            with db:
                booking = db.execute(
                    "SELECT * FROM pb_appointments WHERE id=? AND tenant=? "
                    "AND assistant=? AND caller=?",
                    (booking_id, tenant, assistant, caller),
                ).fetchone()
                if not booking:
                    raise AppointmentError("booking_not_found")
                if booking["status"] == "BOOKED":
                    db.execute(
                        "UPDATE pb_appointments SET status='CANCELLED' WHERE id=?",
                        (booking_id,),
                    )
                return dict(db.execute(
                    "SELECT * FROM pb_appointments WHERE id=?", (booking_id,)
                ).fetchone())

    def bookings(self, tenant: str, assistant: str, caller: str) -> list[dict]:
        _ident(tenant)
        _ident(assistant)
        _ident(caller)
        with closing(self._conn()) as db:
            return [
                dict(x) for x in db.execute(
                    "SELECT * FROM pb_appointments "
                    "WHERE tenant=? AND assistant=? AND caller=? "
                    "ORDER BY start_utc DESC LIMIT 100",
                    (tenant, assistant, caller),
                ).fetchall()
            ]


def _spoken_slot(text: str) -> str | None:
    if not isinstance(text, str) or len(text) > 1200:
        return None
    matches = list(SPOKEN_DE.finditer(text)) + list(SPOKEN_ISO.finditer(text))
    if len(matches) != 1:
        return None
    match = matches[0]
    try:
        minute = int(match.groupdict().get("minute") or "0")
        day, month, year, hour = (
            int(match.group(key)) for key in ("day", "month", "year", "hour")
        )
        parsed = datetime(year, month, day, hour, minute)
        return parsed.strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return None


class AppointmentVoiceResponder:
    """Two-phase, explicit voice booking with zero blind LLM tool calls.

    A spoken date+time proposes a slot only; the subsequent specific phrase
    confirms booking. No background auto-booking or external calendar write.
    """

    def __init__(self, path: str, tenant: str, assistant: str,
                 caller: str, fallback, *, clock=None):
        if not isinstance(path, str) or not Path(path).is_file():
            raise AppointmentError("appointment_database_not_initialized")
        self.store = AppointmentStore(path, clock=clock)
        self.tenant, self.assistant, self.caller = (
            _ident(tenant), _ident(assistant), _ident(caller)
        )
        self.store.validate_calendar(tenant, assistant)
        self.fallback = fallback
        self.greeting = getattr(fallback, "greeting", DEFAULT_GREETING)
        self._clock = clock or time.time
        self._pending: tuple[str, str, int] | None = None

    async def respond(self, text: str, history: list[dict]) -> str:
        normalized = re.sub(r"[^\w ]+", " ", text.casefold()).strip()
        normalized = re.sub(r"\s+", " ", normalized)
        if normalized in CANCEL:
            self._pending = None
            return "Die Terminbuchung wurde abgebrochen. Es wurde kein neuer Termin gebucht."
        if normalized in CONFIRM:
            pending, self._pending = self._pending, None
            if pending is None or int(self._clock()) - pending[2] > 120:
                return "Bitte nennen Sie zuerst ein konkretes Datum und eine Uhrzeit."
            slot, idem, _ = pending
            try:
                booking = await asyncio.to_thread(
                    self.store.book, self.tenant, self.assistant,
                    self.caller, slot, idempotency_key=idem,
                )
            except AppointmentError as exc:
                if exc.code == "slot_already_booked":
                    return "Der Termin wurde inzwischen vergeben. Bitte wählen Sie einen anderen."
                return "Der Termin konnte nicht bestätigt werden. Bitte versuchen Sie eine andere Zeit."
            local = datetime.strptime(booking["local_start"], "%Y-%m-%dT%H:%M")
            return (
                f"Der Termin am {local:%d.%m.%Y} um {local:%H:%M} Uhr ist "
                "verbindlich in unserem Kalender gebucht."
            )
        intent = any(word in normalized.split() for word in (
            "termin", "terminbuchung", "buchen", "buchung",
            "reservieren", "frei", "verfügbar",
        ))
        slot = _spoken_slot(text) if intent else None
        if slot is not None:
            self._pending = None
            try:
                open_slot = await asyncio.to_thread(
                    self.store.check, self.tenant, self.assistant, slot,
                )
            except AppointmentError:
                return "Diese Uhrzeit kann ich nicht anbieten. Bitte nennen Sie einen anderen Termin."
            if not open_slot:
                return "Dieser Termin ist bereits vergeben. Bitte nennen Sie eine andere Uhrzeit."
            idem = uuid4().hex
            self._pending = (slot, idem, int(self._clock()))
            dt = datetime.strptime(slot, "%Y-%m-%dT%H:%M")
            return (
                f"Der Termin am {dt:%d.%m.%Y} um {dt:%H:%M} Uhr ist verfügbar. "
                "Wenn Sie verbindlich buchen möchten, sagen Sie bitte: "
                "Ja, Termin buchen."
            )
        return await self.fallback.respond(text, history)
