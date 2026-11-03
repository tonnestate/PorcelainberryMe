"""Offline tests for headless IP call control. No carrier or LiveKit server used."""
from __future__ import annotations

import base64
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from porcelainberryme.ip_calls import (
    CallError, CallRegistry, make_handler,
    mint_identity_token, verify_identity_token,
)


class InternetCallsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tick = [1_800_000_000]
        self.secret = "identity-secret-abcdefghijklmnop-123456"
        self.media_secret = "media-secret-abcdefghijklmnop-123456789"
        self.store = CallRegistry(
            str(Path(self.tmp.name) / "calls.sqlite"),
            identity_secret=self.secret, media_key="local-test-key",
            media_secret=self.media_secret, media_url="ws://127.0.0.1:7880",
            clock=lambda: self.tick[0],
        )
        self.alice = {"tenant": "tenant_a", "sub": "alice", "role": "human"}
        self.bob = {"tenant": "tenant_a", "sub": "bob", "role": "human"}
        self.agent = {"tenant": "tenant_a", "sub": "assistant", "role": "agent"}
        self.outsider = {"tenant": "tenant_b", "sub": "bob", "role": "human"}

    def _ring(self, caller=None, callee="bob", idem="call-request-0001"):
        return self.store.originate(caller or self.alice, callee, idem)

    @staticmethod
    def _payload(jwt):
        return json.loads(base64.urlsafe_b64decode(
            jwt.split(".")[1] + "=" * (-len(jwt.split(".")[1]) % 4)))

    def test_agent_can_initiate_unpaid_ip_call_not_pstn(self):
        ringing = self._ring(self.agent)
        self.assertTrue(ringing["agent_initiated"])
        self.assertEqual(ringing["state"], "RINGING")
        incoming = self.store.inbox(self.bob)
        self.assertEqual(incoming["events"][0]["kind"], "RINGING")
        self.assertEqual(incoming["events"][0]["call"]["peer"], "assistant")
        self.assertEqual(self.store.inbox(self.alice)["events"], [])
        with self.assertRaisesRegex(CallError, "call_not_accepted"):
            self.store.media_credentials(self.agent, ringing["id"])
        accepted = self.store.decide(self.bob, ringing["id"], "accept")
        self.assertEqual(accepted["state"], "ACTIVE")
        media_agent = self.store.media_credentials(self.agent, ringing["id"])
        media_bob = self.store.media_credentials(self.bob, ringing["id"])
        self.assertEqual(media_agent["room"], media_bob["room"])
        self.assertNotEqual(media_agent["participant"], media_bob["participant"])
        self.assertEqual(self._payload(media_agent["token"])["iss"], "local-test-key")
        video = self._payload(media_agent["token"])["video"]
        self.assertEqual(video["room"], media_agent["room"])
        self.assertEqual(video["canPublishSources"], ["microphone"])
        self.assertFalse(video["canPublishData"])
        self.store.decide(self.bob, ringing["id"], "hangup")
        with self.assertRaisesRegex(CallError, "call_not_accepted"):
            self.store.media_credentials(self.agent, ringing["id"])

    def test_tenant_and_subject_separation(self):
        ringing = self._ring()
        with self.assertRaisesRegex(CallError, "call_not_found"):
            self.store.status(self.outsider, ringing["id"])
        with self.assertRaisesRegex(CallError, "call_not_found"):
            self.store.decide(self.outsider, ringing["id"], "accept")
        with self.assertRaisesRegex(CallError, "only_recipient_can_decide"):
            self.store.decide(self.alice, ringing["id"], "accept")
        with self.assertRaisesRegex(CallError, "call_not_found"):
            self.store.media_credentials(self.outsider, ringing["id"])

    def test_replay_conflict_and_concurrent_busy(self):
        created = self._ring()
        self.assertEqual(self._ring()["id"], created["id"])
        with self.assertRaisesRegex(CallError, "idempotency_conflict"):
            self._ring(callee="charlie")
        with self.assertRaisesRegex(CallError, "endpoint_busy"):
            self._ring(idem="call-request-0002")
        self.store.decide(self.bob, created["id"], "decline")
        with self.assertRaisesRegex(CallError, "invalid_call_transition"):
            self.store.decide(self.bob, created["id"], "accept")
        self.assertEqual(self._ring(idem="call-request-0003")["state"], "RINGING")

    def test_timeout_blocks_late_acceptance_and_releases_slot(self):
        created = self._ring()
        self.tick[0] += 46
        self.assertEqual(self.store.status(self.bob, created["id"])["state"], "TIMED_OUT")
        kinds = [event["kind"] for event in self.store.inbox(self.bob)["events"]]
        self.assertEqual(kinds, ["RINGING", "TIMED_OUT"])
        with self.assertRaisesRegex(CallError, "invalid_call_transition"):
            self.store.decide(self.bob, created["id"], "accept")
        self.assertEqual(self._ring(idem="call-request-0002")["state"], "RINGING")

    def test_active_session_has_bounded_duration(self):
        created = self._ring()
        self.store.decide(self.bob, created["id"], "accept")
        self.tick[0] += 3601
        self.assertEqual(self.store.status(self.alice, created["id"])["state"], "TIMED_OUT")
        with self.assertRaisesRegex(CallError, "call_not_accepted"):
            self.store.media_credentials(self.bob, created["id"])

    def test_identity_signature_expiry_role_and_audience(self):
        token = mint_identity_token(self.secret, "tenant_a", "assistant", "agent",
                                    now=self.tick[0])
        claims = verify_identity_token(token, self.secret, now=self.tick[0])
        self.assertEqual(claims["role"], "agent")
        with self.assertRaisesRegex(CallError, "unauthorized"):
            verify_identity_token(token, self.secret, now=self.tick[0] + 3601)
        with self.assertRaisesRegex(CallError, "unauthorized"):
            verify_identity_token(token[:-1] + ("a" if token[-1] != "a" else "b"),
                                  self.secret, now=self.tick[0])
        with self.assertRaises(CallError):
            mint_identity_token(self.secret, "tenant_a", "assistant", "admin",
                                now=self.tick[0])

    def test_http_authenticated_full_control_lifecycle(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"

        def request(method, endpoint, actor=None, body=None):
            headers = {}
            if actor:
                headers["Authorization"] = "Bearer " + mint_identity_token(
                    self.secret, actor["tenant"], actor["sub"], actor["role"],
                    now=self.tick[0])
            payload = None
            if body is not None:
                payload = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            req = Request(base + endpoint, method=method, headers=headers, data=payload)
            try:
                with urlopen(req, timeout=3) as response:
                    return response.status, json.load(response)
            except HTTPError as exc:
                return exc.code, json.load(exc)

        status, unauthorized = request("GET", "/v1/ip-calls/events")
        self.assertEqual((status, unauthorized["error"]), (401, "unauthorized"))
        status, created = request(
            "POST", "/v1/ip-calls", self.agent,
            {"to": "bob", "idempotency_key": "http-agent-call-01"})
        self.assertEqual(status, 201)
        status, events = request("GET", "/v1/ip-calls/events", self.bob)
        self.assertEqual(status, 200)
        self.assertEqual(events["events"][0]["kind"], "RINGING")
        status, outside = request("GET", f"/v1/ip-calls/{created['id']}", self.outsider)
        self.assertEqual(status, 404)
        status, accepted = request("POST", f"/v1/ip-calls/{created['id']}/accept", self.bob)
        self.assertEqual(accepted["state"], "ACTIVE")
        status, credentials = request(
            "POST", f"/v1/ip-calls/{created['id']}/media-token", self.bob)
        self.assertEqual(status, 200)
        self.assertEqual(credentials["url"], "ws://127.0.0.1:7880")
        self.assertEqual(self._payload(credentials["token"])["video"]["room"],
                         credentials["room"])
        status, ended = request("POST", f"/v1/ip-calls/{created['id']}/hangup", self.agent)
        self.assertEqual(ended["state"], "ENDED")


if __name__ == "__main__":
    unittest.main()
