"""No-carrier deterministic calendar and two-phase voice booking acceptance tests."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from porcelainberryme.appointments import (
    AppointmentError, AppointmentStore, AppointmentVoiceResponder,
)


class AppointmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = [int(datetime(2026, 11, 3, 10, tzinfo=timezone.utc).timestamp())]
        self.path = str(Path(self.temp.name) / "appointments.sqlite")
        self.store = AppointmentStore(self.path, clock=lambda: self.now[0])
        self.store.configure(
            "tenantA", "agent", tz="Europe/Berlin",
            duration_min=30, lead_min=60, horizon_days=365,
            weekly={
                "0": [["09:00", "12:00"], ["13:00", "17:00"]],
                "1": [["09:00", "17:00"]],
                "2": [["09:00", "17:00"]],
                "3": [["09:00", "17:00"]],
                "4": [["09:00", "17:00"]],
            },
        )

    def test_atomic_booking_and_idempotency(self):
        initial = self.store.available("tenantA", "agent", "2026-11-09")
        self.assertEqual(initial[0], "2026-11-09T09:00")
        self.assertIn("2026-11-09T14:00", initial)
        first = self.store.book(
            "tenantA", "agent", "caller", "2026-11-09T14:00",
            idempotency_key="request-0001",
        )
        self.assertEqual(first["status"], "BOOKED")
        repeat = self.store.book(
            "tenantA", "agent", "caller", "2026-11-09T14:00",
            idempotency_key="request-0001",
        )
        self.assertEqual(repeat["id"], first["id"])
        with self.assertRaisesRegex(AppointmentError, "idempotency_conflict"):
            self.store.book("tenantA", "agent", "caller", "2026-11-09T15:00",
                            idempotency_key="request-0001")
        with self.assertRaisesRegex(AppointmentError, "slot_already_booked"):
            self.store.book("tenantA", "agent", "other", "2026-11-09T14:00",
                            idempotency_key="request-0002")
        self.assertNotIn("2026-11-09T14:00",
                         self.store.available("tenantA", "agent", "2026-11-09"))
        self.assertTrue(self.store.check("tenantA", "agent", "2026-11-09T14:30"))

    def test_cancellation_is_owner_scoped_and_releases_slot(self):
        booked = self.store.book(
            "tenantA", "agent", "caller", "2026-11-09T15:00",
            idempotency_key="request-0003",
        )
        with self.assertRaisesRegex(AppointmentError, "booking_not_found"):
            self.store.cancel("tenantA", "agent", "other", booked["id"])
        cancelled = self.store.cancel("tenantA", "agent", "caller", booked["id"])
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertEqual(self.store.cancel(
            "tenantA", "agent", "caller", booked["id"])["status"], "CANCELLED")
        self.assertTrue(self.store.check("tenantA", "agent", "2026-11-09T15:00"))
        later = self.store.book(
            "tenantA", "agent", "other", "2026-11-09T15:00",
            idempotency_key="request-0004",
        )
        self.assertNotEqual(later["id"], booked["id"])
        self.assertEqual(len(self.store.bookings("tenantA", "agent", "caller")), 1)
        self.assertEqual(len(self.store.bookings("tenantA", "agent", "other")), 1)

    def test_parallel_requests_do_not_double_book(self):
        def book(caller):
            try:
                return self.store.book("tenantA", "agent", caller,
                                       "2026-11-09T10:00",
                                       idempotency_key=f"parallel-{caller}-0001")
            except AppointmentError as err:
                return err.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(book, ["alpha", "beta"]))
        self.assertEqual(sum(isinstance(x, dict) for x in result), 1)
        self.assertEqual(result.count("slot_already_booked"), 1)

    def test_tenant_separation_and_no_implicit_business_hours(self):
        self.store.configure(
            "tenantB", "agent", tz="Europe/Berlin",
            weekly={"0": [["14:00", "15:00"]]}, duration_min=30,
        )
        self.store.book("tenantA", "agent", "customer",
                        "2026-11-09T14:00", idempotency_key="tenant-a-0001")
        self.assertTrue(self.store.check("tenantB", "agent", "2026-11-09T14:00"))
        with self.assertRaisesRegex(AppointmentError, "not_configured"):
            self.store.check("tenantA", "unknown", "2026-11-09T14:00")
        with self.assertRaisesRegex(AppointmentError, "outside_configured"):
            self.store.check("tenantB", "agent", "2026-11-10T14:00")

    def test_dst_gaps_and_ambiguous_times_fail_closed(self):
        self.store.configure(
            "tenantA", "night_agent", tz="Europe/Berlin",
            weekly={"6": [["01:00", "05:00"]]},
            duration_min=30, horizon_days=365, lead_min=0,
        )
        with self.assertRaisesRegex(AppointmentError, "ambiguous_or_nonexistent"):
            self.store.check("tenantA", "night_agent", "2027-03-28T02:30")
        with self.assertRaisesRegex(AppointmentError, "ambiguous_or_nonexistent"):
            self.store.check("tenantA", "night_agent", "2027-10-31T02:30")
        self.assertTrue(self.store.check(
            "tenantA", "night_agent", "2027-03-28T03:00"))

    def test_opening_hours_past_horizon_and_missing_timezone(self):
        with self.assertRaisesRegex(AppointmentError, "outside_configured"):
            self.store.check("tenantA", "agent", "2026-11-09T12:00")
        with self.assertRaisesRegex(AppointmentError, "outside_configured"):
            self.store.check("tenantA", "agent", "2026-11-09T09:15")
        with self.assertRaisesRegex(AppointmentError, "slot_too_soon"):
            self.store.check("tenantA", "agent", "2026-11-03T10:00")
        with self.assertRaisesRegex(AppointmentError, "invalid_timezone"):
            self.store.configure("tenantA", "agent",
                                 tz="Mars/TimeZone", weekly={"0": [["09:00", "10:00"]]},
                                 duration_min=30)
        with self.assertRaisesRegex(AppointmentError, "overlapping_business_hours"):
            self.store.configure("tenantA", "agent", tz="UTC",
                                 weekly={"0": [["09:00", "11:00"], ["10:00", "12:00"]]},
                                 duration_min=30)

    def test_ics_export_contains_only_confirmed_utc_slots_and_no_caller_pii(self):
        kept = self.store.book("tenantA", "agent", "private_person",
                               "2026-11-09T14:00",
                               idempotency_key="ics-booking-0001")
        removed = self.store.book("tenantA", "agent", "other_person",
                                  "2026-11-09T14:30",
                                  idempotency_key="ics-booking-0002")
        self.store.cancel("tenantA", "agent", "other_person", removed["id"])
        feed = self.store.export_ics("tenantA", "agent")
        self.assertIn("BEGIN:VCALENDAR\r\n", feed)
        self.assertEqual(feed.count("BEGIN:VEVENT"), 1)
        self.assertIn(f"UID:{kept['id']}@porcelainberryme.local", feed)
        self.assertIn("DTSTART:20261109T130000Z", feed)
        self.assertIn("DTEND:20261109T133000Z", feed)
        self.assertNotIn("private_person", feed)
        self.assertNotIn("other_person", feed)
        self.assertNotIn(removed["id"], feed)
        with self.assertRaisesRegex(AppointmentError, "not_configured"):
            self.store.export_ics("tenantA", "unknown")

    def test_persists_after_restart_and_respects_slot_changes(self):
        appointment = self.store.book(
            "tenantA", "agent", "caller", "2026-11-09T09:00",
            idempotency_key="durable-0001",
        )
        new = AppointmentStore(self.path, clock=lambda: self.now[0])
        self.assertEqual(new.bookings("tenantA", "agent", "caller")[0]["id"],
                         appointment["id"])
        self.assertFalse(new.check("tenantA", "agent", "2026-11-09T09:00"))


class VoiceBookingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = [int(datetime(2026, 11, 3, 10, tzinfo=timezone.utc).timestamp())]
        self.path = str(Path(self.temp.name) / "appointments.sqlite")
        self.store = AppointmentStore(self.path, clock=lambda: self.now[0])
        self.store.configure(
            "client", "assistant", tz="Europe/Berlin",
            weekly={"0": [["09:00", "17:00"]]},
            duration_min=30, lead_min=0,
        )
        class SafeFallback:
            async def respond(self, text, history):
                return "Bitte formulieren Sie Ihre Anfrage genauer."
        self.voice = AppointmentVoiceResponder(
            self.path, "client", "assistant", "person1",
            SafeFallback(), clock=lambda: self.now[0],
        )

    async def test_propose_does_not_book_without_specific_confirmation(self):
        suggested = await self.voice.respond(
            "Ich möchte einen Termin am 09.11.2026 um 14 Uhr buchen.", [])
        self.assertIn("14:00", suggested)
        self.assertEqual(self.store.bookings("client", "assistant", "person1"), [])
        no = await self.voice.respond("Ja", [])
        self.assertIn("genauer", no)
        self.assertEqual(self.store.bookings("client", "assistant", "person1"), [])
        result = await self.voice.respond("Ja, Termin buchen!", [])
        self.assertIn("verbindlich", result)
        self.assertEqual(len(self.store.bookings("client", "assistant", "person1")), 1)
        again = await self.voice.respond("Ja, Termin buchen", [])
        self.assertIn("zuerst", again)
        self.assertEqual(len(self.store.bookings("client", "assistant", "person1")), 1)

    async def test_cancel_and_expired_pending_confirmation(self):
        await self.voice.respond("Termin 09.11.2026 14:30", [])
        self.assertIn("abgebrochen", await self.voice.respond("Nein", []))
        self.assertEqual(self.store.bookings("client", "assistant", "person1"), [])
        await self.voice.respond("Termin am 09.11.2026 um 14:30 Uhr", [])
        self.now[0] += 121
        late = await self.voice.respond("Ja bitte Termin buchen", [])
        self.assertIn("zuerst", late)
        self.assertEqual(self.store.bookings("client", "assistant", "person1"), [])

    async def test_unsupported_date_spelling_never_guess_or_book(self):
        response = await self.voice.respond(
            "Bitte einen Termin am kommenden Mittwoch nachmittags", [])
        self.assertIn("genauer", response)
        self.assertEqual(self.store.bookings("client", "assistant", "person1"), [])

    async def test_explicit_voice_cancellation_only_by_booking_owner(self):
        self.store.book(
            "client", "assistant", "person1", "2026-11-09T14:00",
            idempotency_key="voice-cancel-booking-1",
        )
        suggestion = await self.voice.respond(
            "Ich möchte meinen Termin am 09.11.2026 um 14 Uhr stornieren.", [],
        )
        self.assertIn("Ja, Termin stornieren", suggestion)
        # No mutation on a request without confirmation.
        self.assertEqual(self.store.bookings(
            "client", "assistant", "person1")[0]["status"], "BOOKED")
        wrong = AppointmentVoiceResponder(
            self.path, "client", "assistant", "intruder",
            self.voice.fallback, clock=lambda: self.now[0],
        )
        self.assertIn("keine stornierbare", await wrong.respond(
            "Termin am 09.11.2026 14 Uhr absagen", [],
        ))
        self.assertIn("zuerst", await wrong.respond("Ja, Termin stornieren", []))
        result = await self.voice.respond("Ja, bitte Termin stornieren!", [])
        self.assertIn("wurde storniert", result)
        self.assertEqual(self.store.bookings(
            "client", "assistant", "person1")[0]["status"], "CANCELLED")
        self.assertTrue(self.store.check(
            "client", "assistant", "2026-11-09T14:00"))
        self.assertIn("zuerst", await self.voice.respond("Ja Termin stornieren", []))

    async def test_rejected_cancellation_has_no_side_effect(self):
        self.store.book(
            "client", "assistant", "person1", "2026-11-09T14:00",
            idempotency_key="voice-cancel-booking-2",
        )
        await self.voice.respond("Termin 09.11.2026 14 Uhr absagen", [])
        await self.voice.respond("Nein", [])
        self.assertEqual(self.store.bookings(
            "client", "assistant", "person1")[0]["status"], "BOOKED")

    async def test_collision_between_proposal_and_confirmation(self):
        await self.voice.respond("Termin am 09.11.2026 um 16 Uhr", [])
        self.store.book("client", "assistant", "other", "2026-11-09T16:00",
                        idempotency_key="another-caller-01")
        result = await self.voice.respond("Ja, Termin buchen", [])
        self.assertIn("inzwischen vergeben", result)
        self.assertEqual(self.store.bookings("client", "assistant", "person1"), [])


if __name__ == "__main__":
    unittest.main()
