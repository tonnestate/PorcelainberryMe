"""Authenticated first-party call-me starts only the selected user's AI ring."""
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
    CallRegistry, make_handler, mint_identity_token,
)
from porcelainberryme.outbound_jobs import DurableCallJobs
from porcelainberryme.self_call import AuthenticatedSelfCall

SECRET = "first-party-signed-human-secret-01234567890123456789"


class SelfCallHTTPTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.now = [1800000000]
        self.registry = CallRegistry(
            str(Path(folder.name) / "calls.sqlite"),
            identity_secret=SECRET, clock=lambda: self.now[0],
        )
        self.jobs = DurableCallJobs(
            Path(folder.name) / "jobs.sqlite", clock=lambda: self.now[0],
        )
        for subject, consent in [
            ("assistant", False), ("owner", True),
            ("other_human", True), ("not_opted_in", False),
        ]:
            self.registry.provision("company", subject,
                                    allow_agent_calls=consent)
        self.service = AuthenticatedSelfCall(
            self.registry, self.jobs, agent_id="assistant",
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(self.registry, self_call=self.service),
        )
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, *, actor="owner", role="human", method="POST", body=None):
        if body is None:
            body = {"idempotency_key": "owner-explicit-click-0001"}
        token = mint_identity_token(
            SECRET, "company", actor, role, now=self.now[0],
        )
        base = f"http://127.0.0.1:{self.server.server_port}/v1/ip-calls/call-me"
        request = Request(
            base,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    def status(self, key: str, *, actor="owner"):
        token = mint_identity_token(
            SECRET, "company", actor, "human", now=self.now[0],
        )
        request = Request(
            f"http://127.0.0.1:{self.server.server_port}"
            f"/v1/ip-calls/call-me?approval_id={key}",
            headers={"Authorization": "Bearer " + token},
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    def test_status_is_read_only_and_owner_scoped(self):
        _, created = self.request()
        key = created["approval_id"]
        status, result = self.status(key)
        self.assertEqual(status, 200)
        self.assertEqual(result["state"], "QUEUED")
        self.assertNotIn("owner", json.dumps(result))
        self.assertEqual(self.status(key, actor="other_human")[0], 404)
        self.assertEqual(self.status("../wrong")[0], 404)

    def test_click_to_approved_queue_to_actual_authoritative_call(self):
        code, created = self.request()
        self.assertEqual(code, 202)
        self.assertEqual(created["state"], "QUEUED")
        self.assertNotIn("recipient", created)
        self.assertNotIn("owner", json.dumps(created))
        key = created["approval_id"]
        self.assertEqual(self.request()[1]["approval_id"], key)
        job = self.jobs.claim(tenant="company", agent="assistant")
        self.assertEqual(job["recipient"], "owner")
        self.assertEqual(job["agent"], "assistant")
        self.assertEqual(job["approval_id"], key)
        signed_agent_claims = {
            "tenant": "company", "sub": "assistant", "role": "agent",
        }
        call = self.registry.originate(
            signed_agent_claims, job["recipient"], key,
        )
        self.assertEqual(call["state"], "RINGING")
        self.assertTrue(call["agent_initiated"])
        self.assertEqual(self.request()[1]["state"], "DISPATCHING")
        self.assertIsNone(self.jobs.claim(tenant="company", agent="assistant"))

    def test_human_can_revoke_queued_request_but_not_forge_other_target(self):
        status, created = self.request()
        self.assertEqual(status, 202)
        key = created["approval_id"]
        self.assertEqual(self.request(
            method="DELETE", body={"approval_id": key},
        )[1]["state"], "REVOKED")
        self.assertIsNone(self.jobs.claim(tenant="company", agent="assistant"))
        self.assertEqual(self.request()[1]["state"], "REVOKED")
        status, result = self.request(body={
            "idempotency_key": "click-again-unique-0001",
            "to": "other_human",
        })
        self.assertEqual(status, 400)
        self.assertNotIn("approval_id", result)

    def test_only_human_with_dnd_off_and_agent_optin_can_request(self):
        self.assertEqual(self.request(actor="assistant", role="agent")[0], 403)
        self.assertEqual(self.request(actor="not_opted_in")[0], 403)
        self.registry.set_do_not_disturb("company", "owner", True)
        self.assertEqual(self.request()[0], 409)
        self.registry.set_do_not_disturb("company", "owner", False)
        self.assertEqual(self.request()[0], 202)

    def test_cross_identity_cannot_revoke_other_humans_request(self):
        _, created = self.request()
        status, body = self.request(
            actor="other_human", method="DELETE",
            body={"approval_id": created["approval_id"]},
        )
        self.assertEqual(status, 404)
        self.assertNotIn("owner", json.dumps(body))
        self.assertEqual(self.jobs.status(
            approval_id=created["approval_id"],
            tenant="company", agent="assistant", recipient="owner",
        )["state"], "QUEUED")

    def test_call_me_api_is_disabled_without_host_configuration(self):
        alternate = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.registry),
        )
        t = threading.Thread(target=alternate.serve_forever, daemon=True)
        t.start()
        try:
            token = mint_identity_token(
                SECRET, "company", "owner", "human", now=self.now[0],
            )
            request = Request(
                f"http://127.0.0.1:{alternate.server_port}/v1/ip-calls/call-me",
                data=json.dumps({"idempotency_key": "unique-safe-0002"}).encode(),
                headers={"Authorization": "Bearer " + token},
                method="POST",
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)
            self.assertEqual(error.exception.code, 503)
        finally:
            alternate.shutdown()
            alternate.server_close()


if __name__ == "__main__":
    unittest.main()
