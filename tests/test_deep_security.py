"""Regression tests for lease fencing and authenticated opt-in self-service."""
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


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.time = [1_800_000_000]
        self.key = "self-service-identity-secret-abcdefghij12345"
        self.registry = CallRegistry(
            str(Path(self.tmp.name) / "calls.sqlite"),
            identity_secret=self.key, clock=lambda: self.time[0],
        )
        for subject, consent in [
            ("customer", False), ("agent", False), ("friend", False),
        ]:
            self.registry.provision("tenant1", subject,
                                    allow_agent_calls=consent)
        self.registry.provision("tenant2", "customer")
        self.customer = {"tenant": "tenant1", "sub": "customer", "role": "human"}
        self.agent = {"tenant": "tenant1", "sub": "agent", "role": "agent"}
        self.friend = {"tenant": "tenant1", "sub": "friend", "role": "human"}

    def test_preferences_are_private_to_authenticated_identity(self):
        self.assertEqual(self.registry.preferences(self.customer), {
            "allow_agent_calls": False, "do_not_disturb": False,
        })
        self.assertEqual(self.registry.update_preferences(
            self.customer, {"allow_agent_calls": True, "do_not_disturb": True}
        ), {"allow_agent_calls": True, "do_not_disturb": True})
        self.assertEqual(self.registry.preferences(self.friend), {
            "allow_agent_calls": False, "do_not_disturb": False,
        })
        self.assertEqual(self.registry.preferences(
            {"tenant": "tenant2", "sub": "customer", "role": "human"}),
            {"allow_agent_calls": False, "do_not_disturb": False},
        )
        self.assertEqual(self.registry.update_preferences(
            self.customer, {"do_not_disturb": False}
        )["allow_agent_calls"], True)

    def test_human_can_revoke_agent_consent_immediately(self):
        self.registry.update_preferences(
            self.customer, {"allow_agent_calls": True},
        )
        ringing = self.registry.originate(
            self.agent, "customer", "consented-call-0001",
        )
        self.registry.decide(self.customer, ringing["id"], "decline")
        self.registry.update_preferences(
            self.customer, {"allow_agent_calls": False},
        )
        self.time[0] += 121
        with self.assertRaisesRegex(CallError, "agent_call_not_opted_in"):
            self.registry.originate(
                self.agent, "customer", "blocked-call-0002",
            )

    def test_agent_cannot_edit_itself_into_consent_for_another_person(self):
        with self.assertRaisesRegex(CallError, "only_human"):
            self.registry.update_preferences(
                self.agent, {"allow_agent_calls": True},
            )
        for attack in [
            {"allow_agent_calls": "true"},
            {"allow_agent_calls": 1},
            {"do_not_disturb": None},
            {"tenant": "tenant2"},
            {"subject": "customer"},
            {},
        ]:
            with self.assertRaisesRegex(CallError, "invalid_call_preferences"):
                self.registry.update_preferences(self.customer, attack)

    def test_do_not_disturb_persists_across_reinitialization(self):
        self.registry.update_preferences(self.customer, {
            "do_not_disturb": True, "allow_agent_calls": True,
        })
        reopened = CallRegistry(
            self.registry.path, identity_secret=self.key,
            clock=lambda: self.time[0],
        )
        self.assertTrue(reopened.preferences(self.customer)["do_not_disturb"])
        with self.assertRaisesRegex(CallError, "recipient_do_not_disturb"):
            reopened.originate(self.agent, "customer", "dnd-call-0001")
        with self.assertRaisesRegex(CallError, "recipient_do_not_disturb"):
            reopened.originate(self.friend, "customer", "dnd-call-0002")

    def test_preferences_http_requires_bearer_and_does_not_accept_cross_tenant(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.registry))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:" + str(server.server_port)

        def send(method, actor=None, body=None):
            headers = {}
            if actor:
                headers["Authorization"] = "Bearer " + mint_identity_token(
                    self.key, actor["tenant"], actor["sub"], actor["role"],
                    now=self.time[0],
                )
            payload = None if body is None else json.dumps(body).encode()
            if payload is not None:
                headers["Content-Type"] = "application/json"
            req = Request(base + "/v1/ip-calls/preferences",
                          method=method, headers=headers, data=payload)
            try:
                with urlopen(req, timeout=3) as response:
                    return response.status, json.load(response)
            except HTTPError as err:
                return err.code, json.load(err)

        self.assertEqual(send("GET")[0], 401)
        self.assertEqual(send("POST", self.agent,
                              {"do_not_disturb": True})[0], 403)
        status, preferences = send(
            "POST", self.customer, {"allow_agent_calls": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(preferences, {
            "allow_agent_calls": True, "do_not_disturb": False,
        })
        self.assertEqual(send("GET", self.customer)[1], preferences)
        self.assertEqual(send("GET", {
            "tenant": "tenant2", "sub": "customer", "role": "human",
        })[1]["allow_agent_calls"], False)

    def test_finish_requires_unexpired_lease_for_active_call(self):
        self.registry.configure_inbound_agent("tenant1", "agent", True)
        inbound = self.registry.originate(
            self.customer, "agent", "inbound-lease-0001",
        )
        lease = self.registry.claim_inbound(self.agent, inbound["id"])["lease"]
        self.time[0] += 16
        with self.assertRaisesRegex(CallError, "inbound_lease_lost"):
            self.registry.finish_inbound(self.agent, inbound["id"], lease)
        self.assertEqual(self.registry.status(
            self.customer, inbound["id"])["state"], "ACTIVE")
        # A new worker can reclaim without losing the customer's call.
        recovered = self.registry.claim_inbound(self.agent, inbound["id"])
        self.assertTrue(recovered["recovered"])
        self.assertNotEqual(lease, recovered["lease"])
        self.registry.finish_inbound(self.agent, inbound["id"],
                                     recovered["lease"])
        self.assertEqual(self.registry.status(
            self.customer, inbound["id"])["state"], "ENDED")

    def test_terminal_call_may_release_old_lease_without_hangup(self):
        self.registry.configure_inbound_agent("tenant1", "agent", True)
        inbound = self.registry.originate(
            self.customer, "agent", "inbound-lease-0002",
        )
        lease = self.registry.claim_inbound(self.agent, inbound["id"])["lease"]
        self.registry.decide(self.customer, inbound["id"], "hangup")
        self.time[0] += 16
        receipt = self.registry.finish_inbound(
            self.agent, inbound["id"], lease,
        )
        self.assertEqual(receipt["state"], "ENDED")


if __name__ == "__main__":
    unittest.main()
