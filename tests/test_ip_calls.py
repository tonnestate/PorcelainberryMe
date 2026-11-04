"""Offline tests for headless IP call control. No carrier or LiveKit server used."""
from __future__ import annotations

import base64
from io import BytesIO
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from porcelainberryme.media_cleanup import LiveKitRoomTerminator, MediaTeardownError
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
        self.store.provision("tenant_a", "alice")
        self.store.provision("tenant_a", "bob", allow_agent_calls=True)
        self.store.provision("tenant_a", "assistant")

    def _ring(self, caller=None, callee="bob", idem="call-request-0001"):
        return self.store.originate(caller or self.alice, callee, idem)

    @staticmethod
    def _payload(jwt):
        return json.loads(base64.urlsafe_b64decode(
            jwt.split(".")[1] + "=" * (-len(jwt.split(".")[1]) % 4)))

    def test_call_control_without_livekit_configuration(self):
        separate = CallRegistry(
            str(Path(self.tmp.name) / "unconfigured.sqlite"),
            identity_secret=self.secret, clock=lambda: self.tick[0],
        )
        separate.provision("tenant_a", "assistant")
        separate.provision("tenant_a", "bob", allow_agent_calls=True)
        ringing = separate.originate(self.agent, "bob", "not-yet-media-01")
        separate.decide(self.bob, ringing["id"], "accept")
        with self.assertRaisesRegex(CallError, "media_backend_not_configured"):
            separate.media_credentials(self.bob, ringing["id"])

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

    def test_do_not_disturb_blocks_agent_and_human_ringing(self):
        self.store.set_do_not_disturb("tenant_a", "bob", True)
        with self.assertRaisesRegex(CallError, "recipient_do_not_disturb"):
            self._ring(self.agent)
        with self.assertRaisesRegex(CallError, "recipient_do_not_disturb"):
            self._ring(self.alice)
        self.store.set_do_not_disturb("tenant_a", "bob", False)
        self.assertEqual(self._ring(self.agent)["state"], "RINGING")
        with self.assertRaisesRegex(CallError, "account_not_enrolled"):
            self.store.set_do_not_disturb("tenant_a", "ghost", True)
        with self.assertRaisesRegex(CallError, "invalid_dnd_preference"):
            self.store.set_do_not_disturb("tenant_a", "bob", 1)

    def test_revoking_agent_opt_in_cancels_existing_unanswered_invitation(self):
        ringing = self._ring(self.agent)
        self.assertEqual(self.store.status(self.agent, ringing["id"])["state"], "RINGING")
        changed = self.store.update_preferences(
            self.bob, {"allow_agent_calls": False},
        )
        self.assertFalse(changed["allow_agent_calls"])
        self.assertEqual(self.store.status(self.agent, ringing["id"])["state"], "DECLINED")
        self.assertEqual(self.store.status(self.bob, ringing["id"])["state"], "DECLINED")
        self.assertEqual(
            self.store.inbox(self.agent)["events"][-1]["kind"], "DECLINED",
        )
        with self.assertRaisesRegex(CallError, "invalid_call_transition"):
            self.store.decide(self.bob, ringing["id"], "accept")
        # Revoking AI consent does not block a new human-originated call.
        human = self._ring(self.alice, idem="human-after-agent-optout-01")
        self.assertEqual(human["state"], "RINGING")
        self.store.decide(self.bob, human["id"], "decline")
        with self.assertRaisesRegex(CallError, "agent_call_not_opted_in"):
            self._ring(self.agent, idem="agent-after-optout-01")

    def test_do_not_disturb_cancels_existing_ring_and_not_active_calls(self):
        ringing = self._ring(self.alice)
        self.store.update_preferences(self.bob, {"do_not_disturb": True})
        self.assertEqual(self.store.status(self.bob, ringing["id"])["state"], "DECLINED")
        self.assertEqual(self.store.inbox(self.alice)["events"][-1]["kind"], "DECLINED")
        self.store.update_preferences(self.bob, {"do_not_disturb": False})
        active = self._ring(self.alice, idem="human-again-on-dnd-02")
        self.store.decide(self.bob, active["id"], "accept")
        self.store.set_do_not_disturb("tenant_a", "bob", True)
        # Enabling DND does not tear down an already connected conversation.
        self.assertEqual(self.store.status(self.alice, active["id"])["state"], "ACTIVE")
        self.store.decide(self.alice, active["id"], "hangup")

    def test_trusted_operator_consent_revocation_cancels_pending_agent_call(self):
        ringing = self._ring(self.agent)
        self.store.provision("tenant_a", "bob", allow_agent_calls=False)
        self.assertEqual(self.store.status(self.bob, ringing["id"])["state"], "DECLINED")
        self.assertFalse(self.store.preferences(self.bob)["allow_agent_calls"])

    def test_ai_call_cooldown_persists_and_applies_across_other_agents(self):
        first = self._ring(self.agent)
        self.store.decide(self.bob, first["id"], "decline")
        with self.assertRaisesRegex(CallError, "agent_call_cooldown"):
            self._ring(self.agent, idem="second-agent-call-01")
        self.store.provision("tenant_a", "assistant2")
        other_agent = {"tenant": "tenant_a", "sub": "assistant2", "role": "agent"}
        with self.assertRaisesRegex(CallError, "agent_call_cooldown"):
            self._ring(other_agent, idem="other-agent-call-01")
        # Replaying the original idempotency token does not ring again.
        replay = self._ring(self.agent)
        self.assertEqual(replay["id"], first["id"])
        # Human calling is not subject to AI-originator cooldown.
        human = self._ring(self.alice, idem="human-call-0001")
        self.store.decide(self.bob, human["id"], "decline")
        restarted = CallRegistry(
            self.store.path, identity_secret=self.secret,
            clock=lambda: self.tick[0],
        )
        with self.assertRaisesRegex(CallError, "agent_call_cooldown"):
            restarted.originate(self.agent, "bob", "third-agent-call-01")
        self.tick[0] += 121
        self.assertEqual(
            restarted.originate(self.agent, "bob", "third-agent-call-01")["state"],
            "RINGING",
        )

    def test_tenant_call_capacity_prevents_oversubscription(self):
        for index in range(40):
            caller = f"loadcaller{index}"
            callee = f"loadcallee{index}"
            self.store.provision("tenant_a", caller)
            self.store.provision("tenant_a", callee)
            initiated = self.store.originate(
                {"tenant": "tenant_a", "sub": caller, "role": "human"},
                callee, f"capacity-call-{index:03d}",
            )
            self.assertEqual(initiated["state"], "RINGING")
        self.store.provision("tenant_a", "extra_caller")
        self.store.provision("tenant_a", "extra_callee")
        with self.assertRaisesRegex(CallError, "tenant_call_capacity_exceeded"):
            self.store.originate(
                {"tenant": "tenant_a", "sub": "extra_caller", "role": "human"},
                "extra_callee", "capacity-extra-call-01",
            )
        # A separate tenant is unaffected by the capacity of tenant_a.
        self.store.provision("tenant_b", "another")
        self.store.provision("tenant_b", "receiver")
        self.assertEqual(
            self.store.originate(
                {"tenant": "tenant_b", "sub": "another", "role": "human"},
                "receiver", "tenant-b-call-01",
            )["state"], "RINGING",
        )

    def test_event_retention_discards_old_events_but_preserves_missed_result(self):
        called = self._ring()
        self.tick[0] += 31 * 86400
        self.assertEqual(self.store.status(self.alice, called["id"])["state"],
                         "TIMED_OUT")
        events = self.store.inbox(self.bob)["events"]
        self.assertEqual([event["kind"] for event in events], ["TIMED_OUT"])

    def test_closed_call_records_and_related_events_expire_after_retention(self):
        import sqlite3
        call = self._ring()
        self.store.decide(self.bob, call["id"], "decline")
        self.tick[0] += 31 * 86400
        with self.assertRaisesRegex(CallError, "call_not_found"):
            self.store.status(self.alice, call["id"])
        self.assertEqual(self.store.inbox(self.bob)["events"], [])
        with sqlite3.connect(self.store.path) as con:
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM ip_calls WHERE id=?", (call["id"],)
            ).fetchone()[0], 0)

    def test_media_teardown_prevents_premature_history_purge(self):
        import sqlite3
        call = self._ring(self.agent)
        self.store.decide(self.bob, call["id"], "accept")
        self.store.media_credentials(self.bob, call["id"])
        self.store.decide(self.agent, call["id"], "hangup")
        self.tick[0] += 31 * 86400
        # Incomplete media disconnection is a durable safety obligation:
        # the record must survive until the media server confirms cleanup.
        self.assertEqual(self.store.status(self.agent, call["id"])["state"], "ENDED")
        with sqlite3.connect(self.store.path) as con:
            self.assertEqual(con.execute(
                "SELECT completed FROM ip_media_teardown WHERE call_id=?",
                (call["id"],)
            ).fetchone()[0], 0)
        self.assertEqual(self.store.reconcile_media(lambda room: None)["completed"], 1)
        with self.assertRaisesRegex(CallError, "call_not_found"):
            self.store.status(self.bob, call["id"])
        with sqlite3.connect(self.store.path) as con:
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM ip_media_teardown WHERE call_id=?",
                (call["id"],)
            ).fetchone()[0], 0)

    def test_existing_database_is_migrated_without_losing_consent(self):
        import sqlite3
        legacy = str(Path(self.tmp.name) / "legacy.sqlite")
        with sqlite3.connect(legacy) as con:
            con.execute(
                "CREATE TABLE ip_call_users ("
                "tenant TEXT NOT NULL, subject TEXT NOT NULL,"
                "agent_calls INTEGER NOT NULL DEFAULT 0,"
                "PRIMARY KEY(tenant, subject))"
            )
            con.execute("INSERT INTO ip_call_users VALUES(?,?,?)",
                        ("tenant_a", "bob", 1))
        migrated = CallRegistry(legacy, identity_secret=self.secret,
                                clock=lambda: self.tick[0])
        with sqlite3.connect(legacy) as con:
            row = con.execute(
                "SELECT agent_calls, do_not_disturb FROM ip_call_users "
                "WHERE tenant=? AND subject=?", ("tenant_a", "bob")
            ).fetchone()
        self.assertEqual(row, (1, 0))
        migrated.set_do_not_disturb("tenant_a", "bob", True)
        with sqlite3.connect(legacy) as con:
            self.assertEqual(con.execute(
                "SELECT do_not_disturb FROM ip_call_users "
                "WHERE tenant='tenant_a' AND subject='bob'"
            ).fetchone()[0], 1)

    def test_media_teardown_is_durable_and_repeated_until_token_expiration(self):
        created = self._ring(self.agent)
        self.store.decide(self.bob, created["id"], "accept")
        credential = self.store.media_credentials(self.bob, created["id"])
        self.assertLessEqual(credential["expires_at"] - self.tick[0], 12)
        assert self.store.reconcile_media(lambda room: self.fail("active room deleted"))["reviewed"] == 0
        self.store.decide(self.bob, created["id"], "hangup")
        deleted = []
        done = self.store.reconcile_media(lambda room: deleted.append(room))
        self.assertEqual(done["reviewed"], 1)
        self.assertEqual(done["completed"], 0)
        self.assertEqual(deleted, ["pb_" + created["id"]])
        # A short-lived join token remains potentially usable until expiry;
        # deleting the room once is not considered final proof of revocation.
        self.tick[0] += 2
        self.assertEqual(self.store.reconcile_media(lambda room: deleted.append(room))["reviewed"], 1)
        self.tick[0] += 20
        results = self.store.reconcile_media(lambda room: deleted.append(room))
        self.assertEqual(results["completed"], 1)
        self.assertEqual(self.store.reconcile_media(lambda room: deleted.append(room))["reviewed"], 0)
        self.assertEqual(len(deleted), 3)

    def test_media_teardown_retries_after_failure_and_database_restart(self):
        created = self._ring(self.agent)
        self.store.decide(self.bob, created["id"], "accept")
        self.store.media_credentials(self.agent, created["id"])
        self.store.decide(self.bob, created["id"], "hangup")
        result = self.store.reconcile_media(
            lambda room: (_ for _ in ()).throw(OSError("self_hosted_livekit_unavailable"))
        )
        self.assertEqual(result["retry_pending"], 1)
        restarted = CallRegistry(
            self.store.path, identity_secret=self.secret,
            media_key="local-test-key", media_secret=self.media_secret,
            media_url="ws://127.0.0.1:7880", clock=lambda: self.tick[0],
        )
        self.tick[0] += 3
        done = []
        self.assertEqual(
            restarted.reconcile_media(lambda room: done.append(room))["reviewed"], 1
        )
        self.assertEqual(done, ["pb_" + created["id"]])
        self.tick[0] += 20
        self.assertEqual(
            restarted.reconcile_media(lambda room: done.append(room))["completed"], 1
        )

    def test_active_call_expiry_enqueues_room_termination(self):
        created = self._ring()
        self.store.decide(self.bob, created["id"], "accept")
        self.store.media_credentials(self.alice, created["id"])
        self.tick[0] += 3601
        ended = []
        result = self.store.reconcile_media(lambda room: ended.append(room))
        self.assertEqual(result["completed"], 1)
        self.assertEqual(ended, ["pb_" + created["id"]])
        self.assertEqual(self.store.status(self.bob, created["id"])["state"], "TIMED_OUT")

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

    def test_agent_requires_explicit_consent_and_enrollment(self):
        self.store.provision("tenant_a", "bob", allow_agent_calls=False)
        with self.assertRaisesRegex(CallError, "agent_call_not_opted_in"):
            self._ring(self.agent)
        self.store.provision("tenant_a", "bob", allow_agent_calls=True)
        self.assertEqual(self._ring(self.agent)["state"], "RINGING")
        with self.assertRaisesRegex(CallError, "account_not_enrolled"):
            self._ring(self.alice, callee="unregistered", idem="different-request-002")

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


class LiveKitTwirpTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.time = 1_800_000_000
        self.secret = "media-secret-abcdefghijklmnop-123456789"

    def _fake_open(self, request, timeout):
        self.assertEqual(timeout, 3.0)
        self.calls.append(request)
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:7880/twirp/livekit.RoomService/DeleteRoom",
        )
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            json.loads(request.data),
            {"room": "pb_" + "a" * 32},
        )
        auth = request.get_header("Authorization")
        self.assertTrue(auth.startswith("Bearer "))
        token = auth[7:].split(".")
        claims = json.loads(base64.urlsafe_b64decode(
            token[1] + "=" * (-len(token[1]) % 4)))
        self.assertEqual(claims["video"], {"roomCreate": True})
        self.assertEqual(claims["iss"], "local-test-key")

        class Response(BytesIO):
            status = 200

        return Response(b"{}")

    def test_delete_room_uses_real_twirp_protocol_not_provider_sdk(self):
        endpoint = LiveKitRoomTerminator(
            "ws://127.0.0.1:7880", "local-test-key", self.secret,
            http_open=self._fake_open, clock=lambda: self.time,
        )
        endpoint.delete_room("pb_" + "a" * 32)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(ValueError):
            endpoint.delete_room("room-attacker")

    def test_remove_participant_is_scoped_to_room_and_staff_identity(self):
        requests = []

        def accepted(request, timeout):
            self.assertEqual(timeout, 3)
            self.assertTrue(request.full_url.endswith(
                "/twirp/livekit.RoomService/RemoveParticipant",
            ))
            requests.append(request)
            self.assertEqual(json.loads(request.data), {
                "room": "pb_" + "a" * 32,
                "identity": "ip_" + "b" * 24,
            })
            claims = json.loads(base64.urlsafe_b64decode(
                request.get_header("Authorization").split(".")[1] + "==",
            ))
            self.assertEqual(claims["video"], {
                "roomAdmin": True, "room": "pb_" + "a" * 32,
            })
            class Response(BytesIO):
                status = 200
            return Response(b"{}")

        terminator = LiveKitRoomTerminator(
            "ws://127.0.0.1:7880", "local-test-key", self.secret,
            http_open=accepted, clock=lambda: self.time,
        )
        terminator.remove_participant("pb_" + "a" * 32, "ip_" + "b" * 24)
        self.assertEqual(len(requests), 1)
        with self.assertRaisesRegex(ValueError, "invalid_managed"):
            terminator.remove_participant("other_room", "ip_" + "b" * 24)
        with self.assertRaisesRegex(ValueError, "invalid_managed"):
            terminator.remove_participant("pb_" + "a" * 32, "user_payload")

    def test_staff_remove_typed_missing_is_acknowledged_but_proxy_404_fails(self):
        def typed_missing(request, timeout):
            raise HTTPError(request.full_url, 404, "Not Found", {},
                            BytesIO(b'{"code":"not_found","msg":"no participant"}'))

        terminator = LiveKitRoomTerminator(
            "ws://127.0.0.1:7880", "local-test-key", self.secret,
            http_open=typed_missing,
        )
        terminator.remove_participant("pb_" + "a" * 32, "ip_" + "b" * 24)
        terminator.http_open = lambda req, timeout: (_ for _ in ()).throw(
            HTTPError(req.full_url, 404, "Not Found", {}, BytesIO(b"<html>bad proxy</html>"))
        )
        with self.assertRaises(MediaTeardownError):
            terminator.remove_participant("pb_" + "a" * 32, "ip_" + "b" * 24)

    def test_not_found_must_be_typed_twirp_error(self):
        def missing(_request, timeout):
            raise HTTPError(
                "http://127.0.0.1:7880/", 404, "Not Found", {},
                BytesIO(b'{"code":"not_found","msg":"no active room"}'),
            )
        endpoint = LiveKitRoomTerminator(
            "ws://127.0.0.1:7880", "local-test-key", self.secret,
            http_open=missing, clock=lambda: self.time,
        )
        endpoint.delete_room("pb_" + "a" * 32)

        def wrong_route(_request, timeout):
            raise HTTPError(
                "http://127.0.0.1:7880/", 404, "Not Found", {},
                BytesIO(b"<html>wrong proxy</html>"),
            )
        endpoint.http_open = wrong_route
        with self.assertRaises(MediaTeardownError):
            endpoint.delete_room("pb_" + "a" * 32)

    def test_network_failure_does_not_claim_disconnection(self):
        endpoint = LiveKitRoomTerminator(
            "ws://127.0.0.1:7880", "local-test-key", self.secret,
            http_open=lambda req, timeout: (_ for _ in ()).throw(
                URLError("backend unavailable")
            ),
        )
        with self.assertRaises(MediaTeardownError):
            endpoint.delete_room("pb_" + "a" * 32)


if __name__ == "__main__":
    unittest.main()
