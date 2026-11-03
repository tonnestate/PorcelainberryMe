"""Offline transfer state machine, authorization, privacy, restart and HTTP tests."""
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
    CallError, CallRegistry, make_handler, mint_identity_token,
)


class HandoffStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_path = str(Path(self.directory.name) / "calls.sqlite")
        self.now = [1_800_000_000]
        self.secret = "local-private-identity-secret-123456789000"
        self.media_secret = "other-private-livekit-secret-1234567890"
        self.db = CallRegistry(
            self.db_path, identity_secret=self.secret,
            media_key="self-hosted-test", media_secret=self.media_secret,
            media_url="ws://127.0.0.1:7880",
            clock=lambda: self.now[0],
        )
        self.agent = {"tenant": "enterprise", "sub": "reception", "role": "agent"}
        self.client = {"tenant": "enterprise", "sub": "customer", "role": "human"}
        self.staff = {"tenant": "enterprise", "sub": "employee", "role": "human"}
        self.stranger = {"tenant": "enterprise", "sub": "intruder", "role": "human"}
        self.other = {"tenant": "other_tenant", "sub": "employee", "role": "human"}
        for subject in ("reception", "customer", "employee", "intruder"):
            self.db.provision(
                "enterprise", subject, allow_agent_calls=subject == "customer",
            )
        self.db.provision("other_tenant", "employee")
        self.db.configure_handoff_route("enterprise", "reception", "employee")

    def active(self):
        call = self.db.originate(
            self.agent, "customer", "handoff-source-call-01",
        )
        self.db.decide(self.client, call["id"], "accept")
        return call

    def offer(self, call_id, key="handoff-request-01"):
        return self.db.offer_handoff(self.agent, call_id, idempotency_key=key)

    def test_no_route_no_staff_no_consentless_hand_off(self):
        call = self.active()
        with self.assertRaisesRegex(CallError, "only_agent_can_offer_handoff"):
            self.db.offer_handoff(self.client, call["id"],
                                  idempotency_key="handoff-request-01")
        with self.assertRaisesRegex(CallError, "only_agent_can_offer_handoff"):
            self.db.offer_handoff(self.staff, call["id"],
                                  idempotency_key="handoff-request-01")
        with self.assertRaisesRegex(CallError, "handoff_staff_not_enrolled"):
            self.db.configure_handoff_route(
                "enterprise", "reception", "unknown",
            )
        self.db.configure_handoff_route("enterprise", "reception", None)
        with self.assertRaisesRegex(CallError, "handoff_route_not_configured"):
            self.offer(call["id"])

    def test_unapproved_staff_cannot_join_before_acceptance(self):
        call = self.active()
        with self.assertRaisesRegex(CallError, "call_not_found"):
            self.db.media_credentials(self.staff, call["id"])
        offered = self.offer(call["id"])
        self.assertEqual(offered["state"], "OFFERED")
        self.assertEqual(offered["target"], "employee")
        self.assertEqual(self.db.status(self.agent, call["id"])["handoff"]["state"],
                         "OFFERED")
        with self.assertRaisesRegex(CallError, "handoff_not_accepted"):
            self.db.media_credentials(self.staff, call["id"])
        with self.assertRaisesRegex(CallError, "call_not_found"):
            self.db.media_credentials(self.stranger, call["id"])
        with self.assertRaisesRegex(CallError, "call_not_found"):
            self.db.media_credentials(self.other, call["id"])
        with self.assertRaisesRegex(CallError, "only_staff_can_decide_handoff"):
            self.db.decide_handoff(self.client, call["id"], "accept")
        with self.assertRaisesRegex(CallError, "only_staff_can_decide_handoff"):
            self.db.decide_handoff(self.agent, call["id"], "accept")
        with self.assertRaisesRegex(CallError, "not_authorized_to_hangup"):
            self.db.decide(self.staff, call["id"], "hangup")
        self.assertEqual(self.db.status(self.client, call["id"])["state"], "ACTIVE")

    def test_full_handoff_keeps_same_room_and_agent_leaves(self):
        call = self.active()
        offered = self.offer(call["id"])
        self.assertEqual(self.offer(call["id"]), offered)  # idempotent
        with self.assertRaisesRegex(CallError, "handoff_already_pending"):
            self.offer(call["id"], "different-handoff-01")
        self.assertEqual(
            self.db.decide_handoff(self.staff, call["id"], "accept")["state"],
            "ACCEPTED",
        )
        with self.assertRaisesRegex(CallError, "handoff_staff_media_not_issued"):
            self.db.decide_handoff(self.agent, call["id"], "complete")
        staff_media = self.db.media_credentials(self.staff, call["id"])
        customer_media = self.db.media_credentials(self.client, call["id"])
        self.assertEqual(staff_media["room"], customer_media["room"])
        self.assertNotEqual(staff_media["participant"], customer_media["participant"])
        self.assertTrue(
            self.db.decide_handoff(self.agent, call["id"], "complete")["state"]
            == "CONNECTED",
        )
        self.assertEqual(
            self.db.status(self.agent, call["id"])["handoff"]["state"],
            "CONNECTED",
        )
        self.assertEqual(self.db.status(self.client, call["id"])["state"], "ACTIVE")
        with self.assertRaisesRegex(CallError, "agent_already_handed_off"):
            self.db.media_credentials(self.agent, call["id"])
        with self.assertRaisesRegex(CallError, "not_authorized_to_hangup"):
            self.db.decide(self.agent, call["id"], "hangup")
        ended = self.db.decide(self.staff, call["id"], "hangup")
        self.assertEqual(ended["state"], "ENDED")
        self.assertEqual(self.db.status(self.client, call["id"])["state"], "ENDED")
        with self.assertRaisesRegex(CallError, "call_not_accepted"):
            self.db.media_credentials(self.staff, call["id"])
        seen = [e["kind"] for e in self.db.inbox(self.staff)["events"]]
        self.assertIn("HANDOFF_OFFERED", seen)
        self.assertIn("HANDOFF_ACCEPTED", seen)
        self.assertIn("HANDOFF_CONNECTED", seen)
        self.assertIn("ENDED", seen)

    def test_decline_never_breaks_original_conversation(self):
        call = self.active()
        self.offer(call["id"])
        self.db.decide_handoff(self.staff, call["id"], "decline")
        self.assertEqual(self.db.status(self.client, call["id"])["state"], "ACTIVE")
        with self.assertRaisesRegex(CallError, "handoff_not_accepted"):
            self.db.media_credentials(self.staff, call["id"])
        self.assertEqual(self.db.media_credentials(self.agent, call["id"])["room"],
                         "pb_" + call["id"])
        with self.assertRaisesRegex(CallError, "invalid_handoff_transition"):
            self.db.decide_handoff(self.staff, call["id"], "accept")

    def test_handoff_expiry_does_not_hangup_customer(self):
        call = self.active()
        self.offer(call["id"])
        self.now[0] += 46
        self.assertEqual(self.db.status(self.client, call["id"])["state"], "ACTIVE")
        self.assertEqual(
            self.db.status(self.agent, call["id"])["handoff"]["state"],
            "EXPIRED",
        )
        with self.assertRaisesRegex(CallError, "invalid_handoff_transition"):
            self.db.decide_handoff(self.staff, call["id"], "accept")
        self.assertIn(
            "HANDOFF_EXPIRED",
            [e["kind"] for e in self.db.inbox(self.staff)["events"]],
        )

    def test_accepted_staff_not_joined_expires_and_client_stays(self):
        call = self.active()
        self.offer(call["id"])
        self.db.decide_handoff(self.staff, call["id"], "accept")
        self.db.media_credentials(self.staff, call["id"])
        self.now[0] += 61
        self.assertEqual(
            self.db.status(self.client, call["id"])["handoff"]["state"],
            "EXPIRED",
        )
        with self.assertRaisesRegex(CallError, "invalid_handoff_transition"):
            self.db.decide_handoff(self.agent, call["id"], "complete")
        self.assertEqual(self.db.status(self.client, call["id"])["state"], "ACTIVE")

    def test_staff_dnd_or_busy_blocks_offer(self):
        call = self.active()
        self.db.set_do_not_disturb("enterprise", "employee", True)
        with self.assertRaisesRegex(CallError, "handoff_staff_do_not_disturb"):
            self.offer(call["id"])
        self.db.set_do_not_disturb("enterprise", "employee", False)
        busy_call = self.db.originate(
            self.staff, "intruder", "staff-busy-call-01",
        )
        with self.assertRaisesRegex(CallError, "handoff_staff_busy"):
            self.offer(call["id"])
        self.db.decide(self.stranger, busy_call["id"], "decline")
        self.assertEqual(self.offer(call["id"])["state"], "OFFERED")
        with self.assertRaisesRegex(CallError, "endpoint_busy"):
            self.db.originate(
                self.staff, "intruder", "staff-other-call-01",
            )

    def test_staff_rejection_cannot_cancel_full_call(self):
        call = self.active()
        self.offer(call["id"])
        with self.assertRaisesRegex(CallError, "only_originating_agent"):
            self.db.decide_handoff(self.staff, call["id"], "cancel")
        self.db.decide_handoff(self.agent, call["id"], "cancel")
        self.assertEqual(self.db.status(self.client, call["id"])["state"], "ACTIVE")
        self.assertEqual(self.db.status(self.agent, call["id"])["handoff"]["state"],
                         "CANCELLED")
        with self.assertRaisesRegex(CallError, "handoff_not_accepted"):
            self.db.media_credentials(self.staff, call["id"])

    def test_restart_preserves_route_and_offer_state(self):
        call = self.active()
        self.offer(call["id"])
        reopened = CallRegistry(
            self.db_path, identity_secret=self.secret,
            media_key="self-hosted-test", media_secret=self.media_secret,
            media_url="ws://127.0.0.1:7880",
            clock=lambda: self.now[0],
        )
        self.assertEqual(
            reopened.status(self.agent, call["id"])["handoff"]["state"],
            "OFFERED",
        )
        self.assertEqual(
            reopened.decide_handoff(self.staff, call["id"], "accept")["state"],
            "ACCEPTED",
        )

    def test_handoff_http_auth_and_staff_event(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.db))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(method, endpoint, actor, body=None):
            token = mint_identity_token(
                self.secret, actor["tenant"], actor["sub"], actor["role"],
                now=self.now[0],
            )
            kwargs = {
                "method": method,
                "headers": {"Authorization": "Bearer " + token},
            }
            if body is not None:
                kwargs["data"] = json.dumps(body).encode()
                kwargs["headers"]["Content-Type"] = "application/json"
            req = Request(
                f"http://127.0.0.1:{server.server_port}{endpoint}", **kwargs,
            )
            try:
                with urlopen(req, timeout=3) as response:
                    return response.status, json.load(response)
            except HTTPError as exc:
                return exc.code, json.load(exc)
        call = self.active()
        endpoint = f"/v1/ip-calls/{call['id']}"
        status, err = request("POST", endpoint + "/handoff", self.client, {
            "idempotency_key": "staff-invite-0001",
        })
        self.assertEqual((status, err["error"]), (403, "only_agent_can_offer_handoff"))
        status, offered = request("POST", endpoint + "/handoff", self.agent, {
            "idempotency_key": "staff-invite-0001",
        })
        self.assertEqual((status, offered["state"]), (201, "OFFERED"))
        status, inbox = request("GET", "/v1/ip-calls/events", self.staff)
        self.assertEqual(status, 200)
        self.assertEqual(inbox["events"][0]["kind"], "HANDOFF_OFFERED")
        status, accepted = request("POST", endpoint + "/handoff/accept", self.staff)
        self.assertEqual((status, accepted["state"]), (200, "ACCEPTED"))
        status, issued = request("POST", endpoint + "/media-token", self.staff)
        self.assertEqual((status, issued["room"]), (200, "pb_" + call["id"]))
        status, complete = request("POST", endpoint + "/handoff/complete", self.agent)
        self.assertEqual((status, complete["state"]), (200, "CONNECTED"))
        status, ended = request("POST", endpoint + "/hangup", self.staff)
        self.assertEqual((status, ended["state"]), (200, "ENDED"))


if __name__ == "__main__":
    unittest.main()
