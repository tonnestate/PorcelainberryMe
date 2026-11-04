"""Operational call reporting: authentic participant scope, no personal data."""
from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from porcelainberryme.ip_calls import (
    CallError, CallRegistry, make_handler, mint_identity_token,
)
from porcelainberryme.reporting import call_report


class ReportingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = str(Path(folder.name) / "calls.sqlite")
        self.now = [1_800_000_000]
        self.secret = "test-report-signing-secret-1234567890abcdefgh"
        self.registry = CallRegistry(
            self.path, identity_secret=self.secret,
            clock=lambda: self.now[0],
        )
        self.agent = {"tenant": "t1", "sub": "assistant", "role": "agent"}
        self.bob = {"tenant": "t1", "sub": "bob", "role": "human"}
        self.alice = {"tenant": "t1", "sub": "alice", "role": "human"}
        self.outsider = {"tenant": "t2", "sub": "assistant", "role": "agent"}
        for tenant, actor, allowed in (
            ("t1", "assistant", False), ("t1", "bob", True),
            ("t1", "alice", False), ("t2", "assistant", False),
            ("t2", "bob", True),
        ):
            self.registry.provision(tenant, actor, allow_agent_calls=allowed)

    def _completed_call(self):
        call = self.registry.originate(self.agent, "bob", "report-call-0001")
        self.now[0] += 10
        self.registry.decide(self.bob, call["id"], "accept")
        self.now[0] += 65
        self.registry.decide(self.agent, call["id"], "hangup")
        return call

    def test_exact_accepted_at_duration_and_confirmed_status(self):
        call = self._completed_call()
        report = call_report(self.registry, self.agent)
        self.assertEqual(report["metrics"]["calls"], 1)
        self.assertEqual(report["metrics"]["outgoing"], 1)
        self.assertEqual(report["metrics"]["agent_initiated"], 1)
        self.assertEqual(report["metrics"]["accepted_confirmed"], 1)
        self.assertEqual(report["metrics"]["duration_known_calls"], 1)
        self.assertEqual(report["metrics"]["control_session_seconds"], 65)
        self.assertEqual(report["metrics"]["ended"], 1)
        self.assertEqual(report["metrics"]["acceptance_unverified"], 0)
        self.assertEqual(report["daily"][0]["control_session_seconds"], 65)
        self.assertNotIn(call["id"], json.dumps(report))
        self.assertNotIn("bob", json.dumps(report))
        self.assertEqual(call_report(self.registry, self.bob)["metrics"]["incoming"], 1)

    def test_decline_not_counted_as_answered_or_talk_time(self):
        call = self.registry.originate(self.agent, "bob", "report-declined-01")
        self.now[0] += 4
        self.registry.decide(self.bob, call["id"], "decline")
        stats = call_report(self.registry, self.agent)["metrics"]
        self.assertEqual(stats["declined"], 1)
        self.assertEqual(stats["accepted_confirmed"], 0)
        self.assertEqual(stats["control_session_seconds"], 0)

    def test_unrelated_participant_or_tenant_not_in_report(self):
        self._completed_call()
        # A same-tenant call without the assistant as a participant
        # cannot inflate the assistant's own metrics.
        self.registry.originate(self.alice, "bob", "report-unrelated-01")
        report = call_report(self.registry, self.agent)
        self.assertEqual(report["metrics"]["calls"], 1)
        self.assertEqual(call_report(self.registry, self.outsider)["metrics"]["calls"], 0)
        self.assertEqual(call_report(self.registry, self.alice)["metrics"]["calls"], 1)

    def test_inbound_ai_acceptance_is_recorded_and_persists(self):
        self.registry.configure_inbound_agent("t1", "assistant", True)
        inbound = self.registry.originate(self.alice, "assistant", "report-inbound-0001")
        self.now[0] += 12
        self.registry.claim_inbound(self.agent, inbound["id"])
        stats = call_report(self.registry, self.agent)["metrics"]
        self.assertEqual(stats["accepted_confirmed"], 1)
        self.assertEqual(stats["incoming"], 1)
        reopened = CallRegistry(self.path, identity_secret=self.secret,
                                clock=lambda: self.now[0])
        self.assertEqual(call_report(reopened, self.agent)["metrics"]["accepted_confirmed"], 1)

    def test_premigration_call_is_explicitly_unverified(self):
        call = self._completed_call()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE ip_calls SET accepted_at=NULL WHERE id=?", (call["id"],))
        report = call_report(self.registry, self.agent)["metrics"]
        self.assertEqual(report["accepted_confirmed"], 0)
        self.assertEqual(report["acceptance_unverified"], 1)
        self.assertEqual(report["control_session_seconds"], 0)

    def test_period_invalid_or_outside_retention_is_rejected(self):
        for start, end in (
            (self.now[0] + 2, self.now[0] + 3),
            (self.now[0] - 31 * 86400, self.now[0]),
            (self.now[0], self.now[0]),
            ("bad", self.now[0]), (True, self.now[0]),
        ):
            with self.subTest(start=start, end=end):
                with self.assertRaisesRegex(CallError, "invalid_report_window"):
                    call_report(self.registry, self.agent, start=start, end=end)

    def test_authenticated_http_report_never_shows_contact_or_token(self):
        self._completed_call()
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.registry),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/v1/ip-calls/report"
        token = mint_identity_token(
            self.secret, "t1", "assistant", "agent", now=self.now[0],
        )
        def fetch(suffix="", bearer=token):
            headers = {"Authorization": "Bearer " + bearer} if bearer else {}
            try:
                with urlopen(Request(url + suffix, headers=headers), timeout=3) as result:
                    return result.status, json.load(result)
            except HTTPError as exc:
                return exc.code, json.load(exc)
        code, payload = fetch()
        self.assertEqual(code, 200)
        self.assertEqual(payload["metrics"]["calls"], 1)
        self.assertNotIn("bob", json.dumps(payload))
        self.assertNotIn("assistant", json.dumps(payload))
        self.assertNotIn(token, json.dumps(payload))
        self.assertEqual(fetch(bearer=None)[0], 401)
        self.assertEqual(fetch("?start=abc")[0], 400)
        self.assertEqual(fetch("?start=1&start=2")[0], 400)
        self.assertEqual(fetch("?subject=bob")[0], 400)


if __name__ == "__main__":
    unittest.main()
