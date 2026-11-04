"""Durable consent-gated outbound voice jobs, no real dialing."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from porcelainberryme.outbound_jobs import DurableCallJobs, worker_once

TENANT, AGENT, TARGET = "tenantA", "assistant", "approved_user"
SECRET = "signed-agent-identity-secret-01234567890123456789"


class DurableJobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "calls.sqlite"
        self.tick = [1800000000]
        self.store = DurableCallJobs(self.path, clock=lambda: self.tick[0])

    def approve(self, ttl=120):
        return self.store.approve(
            tenant=TENANT, agent=AGENT, recipient=TARGET,
            consent_reference="verified-consent-event-0001",
            ttl_seconds=ttl,
        )

    def request(self, key, **kwargs):
        scope = dict(tenant=TENANT, agent=AGENT, recipient=TARGET)
        scope.update(kwargs)
        return self.store.request(approval_id=key, **scope)

    def state(self, key):
        return self.store.status(
            approval_id=key, tenant=TENANT, agent=AGENT, recipient=TARGET,
        )["state"]

    async def test_restart_never_redials_ambiguous_call(self):
        key = self.approve()
        self.assertEqual(self.request(key)["state"], "QUEUED")
        restarted = DurableCallJobs(self.path, clock=lambda: self.tick[0])
        self.assertEqual(restarted.claim(tenant=TENANT, agent=AGENT)["approval_id"], key)
        self.assertIsNone(self.store.claim(tenant=TENANT, agent=AGENT))
        self.assertEqual(self.request(key)["state"], "DISPATCHING")
        self.tick[0] += 7201
        self.assertEqual(restarted.reconcile_stale(tenant=TENANT, agent=AGENT), 1)
        self.assertEqual(self.state(key), "UNKNOWN")
        self.assertIsNone(self.store.claim(tenant=TENANT, agent=AGENT))

    async def test_expired_consent_never_dials(self):
        key = self.approve(ttl=30)
        self.tick[0] += 31
        self.assertEqual(self.request(key)["state"], "EXPIRED")
        other = self.approve(ttl=30)
        self.request(other)
        self.tick[0] += 31
        self.assertIsNone(self.store.claim(tenant=TENANT, agent=AGENT))
        self.assertEqual(self.state(other), "EXPIRED")

    async def test_tenant_agent_and_recipient_are_fenced(self):
        key = self.approve()
        for scope in ({"tenant": "other"}, {"agent": "other"},
                      {"recipient": "other"}):
            with self.assertRaises(PermissionError):
                self.request(key, **scope)
        self.assertEqual(self.state(key), "APPROVED")

    async def test_parallel_worker_claims_exactly_one(self):
        key = self.approve()
        self.request(key)
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(
                lambda _: self.store.claim(tenant=TENANT, agent=AGENT),
                range(6),
            ))
        self.assertEqual(sum(x is not None for x in results), 1)

    async def test_worker_failure_is_fenced_and_not_redispatched(self):
        key = self.approve()
        self.request(key)
        attempts = []
        async def failure(**kwargs):
            attempts.append(kwargs)
            self.assertEqual(kwargs["target"], TARGET)
            self.assertEqual(kwargs["request_id"], key)
            self.assertEqual(self.state(key), "DISPATCHING")
            raise OSError("private connection error")
        args = dict(tenant=TENANT, agent=AGENT, identity_secret=SECRET,
                    voice_settings={}, voice_worker=failure)
        self.assertTrue(await worker_once(self.store, **args))
        self.assertEqual(self.state(key), "UNKNOWN")
        self.assertFalse(await worker_once(self.store, **args))
        self.assertEqual(len(attempts), 1)

    async def test_worker_completion_never_claims_human_answer(self):
        key = self.approve()
        self.request(key)
        async def no_answer(**kwargs):
            return None
        self.assertTrue(await worker_once(
            self.store, tenant=TENANT, agent=AGENT, identity_secret=SECRET,
            voice_settings={}, voice_worker=no_answer,
        ))
        result = self.store.status(
            approval_id=key, tenant=TENANT, agent=AGENT, recipient=TARGET,
        )
        self.assertEqual(result["state"], "FINISHED_UNVERIFIED")
        self.assertFalse(result["call_result_verified"])
        self.assertNotIn("recipient", result)

    async def test_approval_id_and_fingerprint_are_not_consent_text(self):
        import sqlite3
        import re
        key = self.approve()
        self.assertRegex(key, r"^[a-f0-9]{32}$")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT consent_fingerprint FROM outbound_call_jobs"
            ).fetchone()
        self.assertEqual(len(row[0]), 64)
        self.assertNotIn("verified-consent", row[0])

    async def test_retention_does_not_delete_recent_or_dispatching_jobs(self):
        old = self.approve(ttl=30)
        self.request(old)
        self.store.claim(tenant=TENANT, agent=AGENT)
        fresh = self.approve(ttl=30)
        self.tick[0] += 31
        self.assertEqual(self.store.prune_terminal(
            tenant=TENANT, agent=AGENT), 0)
        self.assertEqual(self.state(fresh), "EXPIRED")
        self.assertEqual(self.state(old), "DISPATCHING")
        self.tick[0] += 31 * 86400
        removed = self.store.prune_terminal(
            tenant=TENANT, agent=AGENT)
        self.assertEqual(removed, 1)
        self.assertEqual(self.state(old), "DISPATCHING")


    async def test_approved_durable_job_uses_existing_actual_call_orchestrator(self):
        import sys
        from types import ModuleType, SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from porcelainberryme.ip_calls import verify_identity_token
        key = self.approve()
        self.request(key)
        events = []
        fake_livekit = ModuleType("livekit")
        fake_livekit.rtc = SimpleNamespace()

        class Control:
            def __init__(self, token, *, token_factory=None):
                claims = verify_identity_token(token, SECRET)
                self.assert_token_factory = token_factory
                self.checks = 0
                self.claims = claims
                assert callable(token_factory)
                assert verify_identity_token(token_factory(), SECRET)["sub"] == AGENT

            async def whoami(self):
                return {"role": self.claims["role"],
                        "tenant": self.claims["tenant"],
                        "subject": self.claims["sub"]}

            async def originate(self, recipient, request_id):
                events.append(("originate", recipient, request_id))
                return {"id": "b" * 32, "agent_initiated": True}

            async def status(self, call_id):
                return {"state": "ACTIVE"}

            async def hangup(self, call_id):
                events.append(("hangup", call_id))

        class Session:
            def __init__(self, **kwargs):
                events.append(("session", kwargs["callee"]))

            async def speak(self, call_id):
                events.append(("voice", call_id))

        with patch.dict(sys.modules, {"livekit": fake_livekit}), (
                patch("porcelainberryme.voice_livekit_agent.LocalFasterWhisper",
                      return_value=object())), (
                patch("porcelainberryme.voice_livekit_agent.LocalPiperVoice",
                      return_value=object())), (
                patch("porcelainberryme.voice_livekit_agent.LocalOllamaResponder",
                      return_value=SimpleNamespace(preflight=AsyncMock()))), (
                patch("porcelainberryme.voice_livekit_agent.CallControlClient", Control)), (
                patch("porcelainberryme.voice_livekit_agent.LiveKitAgentCall", Session)):
            await worker_once(
                self.store, tenant=TENANT, agent=AGENT,
                identity_secret=SECRET,
                voice_settings=dict(whisper_model="existing",
                                    ollama_model="installed",
                                    piper_binary="existing", piper_model="existing"),
            )
        self.assertIn(("originate", TARGET, key), events)
        self.assertIn(("voice", "b" * 32), events)
        self.assertEqual(self.state(key), "FINISHED_UNVERIFIED")



if __name__ == "__main__":
    unittest.main()
