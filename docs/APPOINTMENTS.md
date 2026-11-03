# PorcelainberryMe: Local appointment calendar (no provider subscription)

## Status

This is a functional, self-hosted *internal* appointment ledger for independent AI Internet calls.
It checks schedule availability and handles authorized, two-phase voice booking and cancellation.
Implemented in src/porcelainberryme/appointments.py with SQLite transactions, IANA time zones,
DST ambiguity rejection, idempotency, tenant isolation and privacy-preserving iCalendar export.

**This is not yet a Google/Microsoft calendar integration, and not public PSTN calling.**
Existing externally shared calendars cannot be assumed empty; enabling this feature without
an authoritative external calendar source risks double booking. Only configure/advertise
appointments backed by this internal calendar until external calendar sync is implemented.
Offline automated tests are not a live user speech or VPS deployment acceptance.

## Initialization and tenant configuration

No calendar or opening hours are fabricated. An operator must supply the IANA time zone
and explicit weekly business windows. Monday = 0 ... Sunday = 6.

    export PB_APPOINTMENT_DB='/private/existing/appointments.sqlite'
    python -m porcelainberryme.appointments --db "$PB_APPOINTMENT_DB" configure --tenant company01 --assistant assistant01 --timezone Europe/Berlin --duration-min 30 --weekly-json '{"0":[["09:00","12:00"],["13:00","17:00"]],"1":[["09:00","17:00"]],"2":[["09:00","17:00"]],"3":[["09:00","17:00"]],"4":[["09:00","17:00"]]}' --lead-min 60 --horizon-days 90

To list slots for a calendar date (no model inference):

    python -m porcelainberryme.appointments --db "$PB_APPOINTMENT_DB" slots --tenant company01 --assistant assistant01 --date 2026-11-09

Only a trusted operator should use this local CLI; it does not establish customer portal
role-based administration or a public-facing API. Keep the DB private and backed up.

## Spoken booking and cancellation

Set PB_APPOINTMENT_DB in the **existing PorcelainberryMe voice-worker process**.
On startup, the worker verifies that the target tenant/assistant calendar exists
before originating the IP call. The caller is the *verified recipient Internet identity*.
An authenticated agent may initiate a call only if the recipient has opted in.

Example two-phase spoken commands:

1. Caller: 'Ich möchte einen Termin am 09.11.2026 um 14 Uhr buchen.'
2. Assistant: 'Der Termin ... ist verfügbar. ... Ja, Termin buchen.'
3. Caller: 'Ja, Termin buchen.'
4. The assistant executes a durable SQLite booking and speaks confirmation ONLY after commit.

Cancellation is also two-phase:

1. Caller: 'Ich möchte meinen Termin am 09.11.2026 um 14 Uhr stornieren.'
2. Assistant identifies only that caller's own existing booked appointment,
   and asks for the exact confirmation phrase 'Ja, Termin stornieren.'
3. Caller confirms, then the assistant cancels the booking and responds after commit.

'Ja' alone is not sufficient for booking or cancellation. A delayed confirmation
expires after 120 seconds, and a different booking owner cannot cancel the appointment.
Unsupported natural-language dates, ambiguous dates/time zones and unavailable slots
fail closed; no general LLM may pretend that a booking was executed.

Transcription is currently configured for German, and the deterministic parser
accepts explicit German numeric dates and 24-hour clock forms. It cannot yet
reliably parse spoken ordinal dates, regional date phrasing or flexible conditions.
Offline tests do not prove that the local Whisper model transcribes this grammar
accurately enough in real calls. A voice-specific NLU parser needs separate proof.

## Booking records, operator cancellation and export

    python -m porcelainberryme.appointments --db "$PB_APPOINTMENT_DB" list --tenant company01 --assistant assistant01 --caller customer01

    python -m porcelainberryme.appointments --db "$PB_APPOINTMENT_DB" export-ics --tenant company01 --assistant assistant01 > /private/exports/appointments.ics

An iCalendar export is a read-only snapshot of BOOKED appointments in UTC.
It contains no caller identity or name, and cancelled records are excluded.
This is **not** live Google Calendar/Outlook synchronization, booking notifications,
reminders, round-robin team allocation or external invitations. Do not claim those features.

## Security, isolation and correctness

- Only explicitly configured time windows are bookable. No implicit holidays/opening hours.
- A booking commits inside BEGIN IMMEDIATE, so concurrent requests cannot book the same slot.
- Caller + idempotency key cannot create duplicate bookings; changed slot causes an error.
- All read, cancel and booking operations are scoped by tenant and assistant.
- Cancellation is owner-scoped and cannot retroactively cancel a started appointment.
- Local times in DST gaps or overlapping clock hours are rejected rather than silently
  converted to the wrong UTC time. Booking records persist across restarts.
- Operator keys, profiles and local calendar data never go to third-party calendar APIs.
- Availability is a best-effort snapshot; another caller may book before confirmation,
  in which case the actual SQLite commit fails and the assistant states the slot was taken.

## Remaining product requirements

The complete Fonio-parity goal still requires live multi-device media, highly natural
speech/interruptions with measured latency, inbound mobile push, configurable workflows,
verified staff transfers, third-party calendar integrations where permitted, customer
management UI, per-tenant rate/billing rules, contact integrations, and legal privacy review.
Real public mobile/landline numbers require a network interconnect and cannot be provided
through bare Internet with no telephone operator cost.
