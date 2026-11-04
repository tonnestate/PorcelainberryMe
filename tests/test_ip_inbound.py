"""Inbound autonomous AI control plane: isolation, fencing and handoff."""
from __future__ import annotations

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


class InboundAgentControlTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = str(Path(self.folder.name) / "calls.sqlite")
        self.now = [1_800_000_000]
        self.secret = "inbound-agent-test-secret-abcdef-1234567"
        self.db = CallRegistry(
            self.path, identity_secret=self.secret,
            media_key="self-hosted-livekit",
            media_secret="independent-test-secret-45678901234567",
            media_url="ws://127.0.0.1:7880",
            clock=lambda: self.now[0],
        )
        self.customer = {
            "tenant": "tenantA", "sub": "customer", "role": "human",
        }
        self.agent = {
            "tenant": "tenantA", "sub": "agentA", "role": "agent",
        }
        self.other_agent = {
            "tenant": "tenantA", "sub": "agentB", "role": "agent",
        }
        self.staff = {
            "tenant": "tenantA", "sub": "staff", "role": "human",
        }
        self.outsider = {
            "tenant": "tenantB", "sub": "agentA", "role": "agent",
        }
        for who in ("customer", "agentA", "agentB", "staff"):
            self.db.provision("tenantA", who)
        self.db.provision("tenantB", "agentA")
        self.db.configure_inbound_agent("tenantA", "agentA", True)

    def call(self, key="inbound-call-0001"):
        return self.db.originate(self.customer, "agentA", key)

    def test_caller_initiates_agent_auto_accepts_and_finishes(self):
        call = self.call()
        self.assertEqual(call["state"], "RINGING")
        self.assertFalse(call["agent_initiated"])
        self.assertEqual(
            self.db.inbox(self.agent)["events"][0]["kind"], "RINGING",
        )
        with self.assertRaisesRegex(CallError, "inbound_agent_lease_required"):
            self.db.decide(self.agent, call["id"], "accept")
        with self.assertRaisesRegex(CallError, "call_not_accepted"):
            self.db.media_credentials(self.customer, call["id"])
        claimed = self.db.claim_inbound(self.agent, call["id"])
        self.assertEqual(claimed["state"], "ACTIVE")
        self.assertEqual(claimed["peer"], "customer")
        self.assertFalse(claimed["recovered"])
        self.assertEqual(self.db.status(self.customer, call["id"])["state"], "ACTIVE")
        with self.assertRaisesRegex(CallError, "inbound_lease_lost"):
            self.db.media_credentials(self.agent, call["id"])
        staff_token = self.db.media_credentials(
            self.agent, call["id"], inbound_lease=claimed["lease"],
        )
        human_token = self.db.media_credentials(self.customer, call["id"])
        self.assertEqual(staff_token["room"], human_token["room"])
        self.assertNotEqual(staff_token["participant"], human_token["participant"])
        self.assertTrue(self.db.heartbeat_inbound(
            self.agent, call["id"], claimed["lease"])["active"])
        result = self.db.finish_inbound(self.agent, call["id"], claimed["lease"])
        self.assertEqual(result, {"state": "ENDED", "transferred": False})
        self.assertEqual(self.db.status(self.customer, call["id"])["state"], "ENDED")
        self.assertEqual(
            [e["kind"] for e in self.db.inbox(self.customer)["events"]],
            ["OUTGOING", "ACTIVE", "ENDED"],
        )
        with self.assertRaisesRegex(CallError, "inbound_lease_lost"):
            self.db.finish_inbound(self.agent, call["id"], claimed["lease"])

    def test_second_worker_cannot_claim_while_first_is_alive(self):
        call = self.call()
        first = self.db.claim_inbound(self.agent, call["id"])
        with self.assertRaisesRegex(CallError, "inbound_lease_held"):
            self.db.claim_inbound(self.agent, call["id"])
        self.now[0] += 13
        self.db.heartbeat_inbound(self.agent, call["id"], first["lease"])
        self.now[0] += 4
        with self.assertRaisesRegex(CallError, "inbound_lease_held"):
            self.db.claim_inbound(self.agent, call["id"])

    def test_crash_recovery_fences_stale_worker_and_keeps_original_call(self):
        call = self.call()
        first = self.db.claim_inbound(self.agent, call["id"])
        self.now[0] += 16
        restarted = CallRegistry(
            self.path, identity_secret=self.secret,
            media_key="self-hosted-livekit",
            media_secret="independent-test-secret-45678901234567",
            media_url="ws://127.0.0.1:7880",
            clock=lambda: self.now[0],
        )
        second = restarted.claim_inbound(self.agent, call["id"])
        self.assertTrue(second["recovered"])
        self.assertNotEqual(first["lease"], second["lease"])
        with self.assertRaisesRegex(CallError, "inbound_lease_lost"):
            self.db.heartbeat_inbound(self.agent, call["id"], first["lease"])
        with self.assertRaisesRegex(CallError, "inbound_lease_lost"):
            self.db.finish_inbound(self.agent, call["id"], first["lease"])
        with self.assertRaisesRegex(CallError, "inbound_lease_lost"):
            self.db.media_credentials(
                self.agent, call["id"], inbound_lease=first["lease"],
            )
        self.assertEqual(restarted.status(self.customer, call["id"])["state"],
                         "ACTIVE")
        self.assertEqual(restarted.finish_inbound(
            self.agent, call["id"], second["lease"])["state"], "ENDED")

    def test_provisioning_role_tenant_and_disabled_agent_isolation(self):
        call = self.call()
        for user in (self.customer, self.other_agent, self.outsider):
            with self.assertRaises(CallError):
                self.db.claim_inbound(user, call["id"])
        with self.assertRaisesRegex(CallError, "inbound_agent_not_enrolled"):
            self.db.configure_inbound_agent("tenantA", "unknown", True)
        with self.assertRaisesRegex(CallError, "invalid_inbound_agent_setting"):
            self.db.configure_inbound_agent("tenantA", "agentA", 1)
        self.db.configure_inbound_agent("tenantA", "agentA", False)
        with self.assertRaisesRegex(CallError, "inbound_agent_not_enabled"):
            self.db.claim_inbound(self.agent, call["id"])
        # Disabling an inbound receptionist rescinds unanswered incoming
        # calls, rather than leaving customers ringing into an offline agent.
        self.assertEqual(self.db.status(self.customer, call["id"])["state"],
                         "DECLINED")
        with self.assertRaisesRegex(CallError, "inbound_agent_not_enabled"):
            self.db.originate(self.customer, "agentA", "disabled-agent-call-002")
        # Normal customer-to-staff calls remain possible.
        ordinary = self.db.originate(
            self.customer, "staff", "ordinary-human-call-003",
        )
        self.assertEqual(ordinary["state"], "RINGING")
        self.db.decide(self.staff, ordinary["id"], "decline")
        self.db.configure_inbound_agent("tenantA", "agentA", True)
        reopened = self.db.originate(
            self.customer, "agentA", "reenabled-inbound-call-004",
        )
        self.assertEqual(reopened["state"], "RINGING")

    def test_expired_ring_is_not_autoaccepted(self):
        call = self.call()
        self.now[0] += 46
        with self.assertRaisesRegex(CallError, "inbound_call_unavailable"):
            self.db.claim_inbound(self.agent, call["id"])
        self.assertEqual(
            self.db.status(self.customer, call["id"])["state"], "TIMED_OUT",
        )

    def test_disabling_inbound_during_session_still_allows_fenced_finish(self):
        call = self.call()
        claim = self.db.claim_inbound(self.agent, call["id"])
        self.db.configure_inbound_agent("tenantA", "agentA", False)
        with self.assertRaisesRegex(CallError, "inbound_agent_not_enabled"):
            self.db.heartbeat_inbound(self.agent, call["id"], claim["lease"])
        self.assertEqual(
            self.db.finish_inbound(self.agent, call["id"], claim["lease"])["state"],
            "ENDED",
        )

    def test_customer_hangup_during_ai_session_is_preserved(self):
        call = self.call()
        claim = self.db.claim_inbound(self.agent, call["id"])
        self.db.decide(self.customer, call["id"], "hangup")
        self.assertEqual(self.db.status(self.agent, call["id"])["state"], "ENDED")
        self.assertEqual(self.db.finish_inbound(
            self.agent, call["id"], claim["lease"])["state"], "ENDED")

    def test_ai_inbound_employee_handoff_preserves_real_customer(self):
        self.db.configure_handoff_route("tenantA", "agentA", "staff")
        call = self.call()
        lease = self.db.claim_inbound(self.agent, call["id"])["lease"]
        with self.assertRaisesRegex(CallError, "inbound_lease_lost"):
            self.db.offer_handoff(
                self.agent, call["id"], idempotency_key="inbound-transfer-001",
            )
        offered = self.db.offer_handoff(
            self.agent, call["id"], idempotency_key="inbound-transfer-001",
            lease=lease,
        )
        self.assertEqual(offered["state"], "OFFERED")
        self.assertEqual(self.db.status(self.staff, call["id"])["peer"],
                         "customer")
        self.db.decide_handoff(self.staff, call["id"], "accept")
        self.db.media_credentials(self.staff, call["id"])
        with self.assertRaisesRegex(CallError, "inbound_lease_lost"):
            self.db.decide_handoff(self.agent, call["id"], "complete")
        self.db.decide_handoff(
            self.agent, call["id"], "complete", lease=lease,
        )
        self.assertEqual(self.db.finish_inbound(
            self.agent, call["id"], lease), {
                "state": "ACTIVE", "transferred": True,
            })
        self.assertEqual(self.db.status(self.customer, call["id"])["state"],
                         "ACTIVE")
        # Finished handoff deletes the inbound lease, so the departed
        # worker has no valid token to obtain fresh room credentials.
        with self.assertRaisesRegex(CallError, "inbound_lease_lost"):
            self.db.media_credentials(self.agent, call["id"], inbound_lease=lease)
        with self.assertRaisesRegex(CallError, "inbound_agent_lease_required"):
            self.db.decide(self.agent, call["id"], "hangup")
        self.assertEqual(self.db.decide(
            self.staff, call["id"], "hangup")["state"], "ENDED")
        events = [e["kind"] for e in self.db.inbox(self.customer)["events"]]
        self.assertIn("HANDOFF_CONNECTED", events)
        self.assertIn("ENDED", events)

    def test_inbound_http_endpoints_require_correct_signed_agent_and_lease(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.db))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(method, suffix, user, payload=None):
            url = f"http://127.0.0.1:{server.server_port}{suffix}"
            token = mint_identity_token(
                self.secret, user["tenant"], user["sub"], user["role"],
                now=self.now[0],
            )
            kwargs = {"method": method, "headers": {
                "Authorization": "Bearer " + token,
            }}
            if payload is not None:
                kwargs["headers"]["Content-Type"] = "application/json"
                kwargs["data"] = json.dumps(payload).encode()
            try:
                with urlopen(Request(url, **kwargs), timeout=2) as response:
                    return response.status, json.load(response)
            except HTTPError as exc:
                return exc.code, json.load(exc)
        call = self.call()
        path = "/v1/ip-calls/" + call["id"]
        self.assertEqual(
            request("POST", path + "/inbound/claim", self.customer)[0], 403,
        )
        status, claimed = request("POST", path + "/inbound/claim", self.agent)
        self.assertEqual((status, claimed["state"]), (200, "ACTIVE"))
        self.assertEqual(
            request("POST", path + "/media-token", self.agent)[1]["error"],
            "inbound_lease_lost",
        )
        self.assertEqual(
            request("POST", path + "/media-token", self.agent,
                    {"lease": claimed["lease"]})[0], 200,
        )
        self.assertEqual(
            request("POST", path + "/inbound/heartbeat", self.agent,
                    {"lease": claimed["lease"]})[1]["active"], True,
        )
        self.assertEqual(
            request("POST", path + "/inbound/finish", self.agent,
                    {"lease": "f" * 32})[1]["error"], "inbound_lease_lost",
        )
        self.assertEqual(
            request("POST", path + "/inbound/finish", self.agent,
                    {"lease": claimed["lease"]})[1]["state"], "ENDED",
        )


if __name__ == "__main__":
    unittest.main()
