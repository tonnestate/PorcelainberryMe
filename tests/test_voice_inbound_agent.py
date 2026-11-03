"""Inbound worker orchestrator: no live server/model weights required."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from porcelainberryme.voice_inbound_agent import (
    is_inbound_invitation, serve_inbound,
)


CALL1 = "1" * 32
CALL2 = "2" * 32
SECRET = "trusted-local-signing-secret-very-long-key-xyz"


def event(call_id, cursor, *, kind="RINGING", direction="incoming",
          ai_originated=False):
    return {"kind": kind, "cursor": cursor, "call": {
        "id": call_id, "direction": direction,
        "agent_initiated": ai_originated,
    }}


class Control:
    def __init__(self, events=None):
        self.events = events or [event(CALL1, 1)]
        self.claims = []
        self.finishes = []
        self.heartbeat_calls = []
        self.cursor_history = []
        self.inbound_lease = None
        self.held = 0
        self.identity = {"tenant": "tenantA", "subject": "assistant",
                         "role": "agent"}

    async def whoami(self):
        return self.identity

    async def inbox(self, after):
        self.cursor_history.append(after)
        return {"events": [x for x in self.events if x["cursor"] > after][:100]}

    async def claim_inbound(self, call_id):
        if self.held:
            self.held -= 1
            raise RuntimeError("call_control_inbound_lease_held")
        self.claims.append(call_id)
        return {"id": call_id, "lease": "f" * 32, "peer": "customer",
                "state": "ACTIVE", "recovered": False}

    async def heartbeat_inbound(self, call_id, lease):
        self.heartbeat_calls.append((call_id, lease))
        return {"active": True}

    async def finish_inbound(self, call_id, lease):
        self.finishes.append((call_id, lease))
        return {"state": "ENDED", "transferred": False}


class Session:
    logs = []
    fails = False
    def __init__(self, **kwargs):
        self.data = kwargs
        Session.logs.append(("constructed", kwargs["tenant"], kwargs["callee"]))

    async def speak(self, call_id, *, heartbeat=None):
        Session.logs.append(("spoken", call_id))
        Session.logs.append(("responder", type(self.data["llm"]).__name__))
        Session.logs.append(("greeting", self.data["greeting"]))
        Session.logs.append(("lease", self.data["client"].inbound_lease))
        # Give the independent lease keeper its own event-loop turn,
        # even when this synthetic media session finishes very quickly.
        await asyncio.sleep(0.05)
        if Session.fails:
            raise OSError("mock_media_disconnected")


class InboundWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Session.logs.clear()
        Session.fails = False
        self.resources = (object(), SimpleNamespace(greeting="Welcome inbound"), object())

    async def run_worker(self, client, *, max_sessions=1):
        with patch("porcelainberryme.voice_inbound_agent.LiveKitAgentCall", Session):
            return await serve_inbound(
                tenant="tenantA", agent_id="assistant",
                identity_secret=SECRET, whisper_model="/existing-whisper",
                ollama_model="existing-qwen", piper_binary="/existing-piper",
                piper_model="/existing-voice", client=client,
                rtc=object(), local_resources=self.resources,
                max_sessions=max_sessions, poll_seconds=0.1,
            )

    async def test_human_inbound_auto_answer_and_fenced_finalization(self):
        client = Control()
        count = await self.run_worker(client)
        self.assertEqual(count, 1)
        self.assertEqual(client.claims, [CALL1])
        self.assertEqual(client.finishes, [(CALL1, "f" * 32)])
        self.assertEqual(client.heartbeat_calls, [(CALL1, "f" * 32)])
        self.assertIn(("constructed", "tenantA", "customer"), Session.logs)
        self.assertIn(("spoken", CALL1), Session.logs)
        self.assertIn(("lease", "f" * 32), Session.logs)
        self.assertEqual(client.inbound_lease, None)

    async def test_unrelated_agent_originated_events_do_not_trigger_auto_answer(self):
        client = Control([
            event(CALL1, 1, kind="ACTIVE"),
            event(CALL1, 2, direction="outgoing"),
            event(CALL1, 3, ai_originated=True),
            event(CALL2, 4),
        ])
        await self.run_worker(client)
        self.assertEqual(client.claims, [CALL2])

    async def test_held_lease_keeps_cursor_for_crash_recovery(self):
        client = Control()
        client.held = 2
        count = await self.run_worker(client)
        self.assertEqual(count, 1)
        self.assertGreaterEqual(client.cursor_history.count(0), 3)
        self.assertEqual(client.claims, [CALL1])

    async def test_lease_loss_cancels_media_before_a_second_worker_reclaims(self):
        from porcelainberryme.voice_inbound_agent import run_fenced_voice_session
        class FailedLease:
            async def heartbeat_inbound(self, call_id, lease):
                raise RuntimeError("call_control_inbound_lease_lost")
        class HangingSession:
            def __init__(self):
                self.started = asyncio.Event()
                self.cancelled = False
            async def speak(self, call_id):
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
        session = HangingSession()
        with self.assertRaisesRegex(RuntimeError, "inbound_lease_lost"):
            await asyncio.wait_for(run_fenced_voice_session(
                session, FailedLease(), CALL1, "f" * 32,
            ), timeout=1)
        self.assertTrue(session.started.is_set())
        self.assertTrue(session.cancelled)

    async def test_bad_identity_never_claims_or_loads_model(self):
        client = Control()
        client.identity = {"tenant": "tenantB", "subject": "assistant", "role": "agent"}
        with self.assertRaisesRegex(PermissionError, "identity_mismatch"):
            await self.run_worker(client)
        self.assertEqual(client.claims, [])

    async def test_media_failure_fences_and_finishes_incoming_call(self):
        client = Control()
        Session.fails = True
        with self.assertRaisesRegex(OSError, "mock_media_disconnected"):
            await self.run_worker(client)
        self.assertEqual(client.finishes, [(CALL1, "f" * 32)])
        self.assertIsNone(client.inbound_lease)

    async def test_real_model_preflight_failure_never_claims_call(self):
        client = Control()
        with patch("porcelainberryme.voice_inbound_agent.prepare_local_voice",
                   side_effect=RuntimeError("local_voice_missing")):
            with self.assertRaisesRegex(RuntimeError, "local_voice_missing"):
                await serve_inbound(
                    tenant="tenantA", agent_id="assistant",
                    identity_secret=SECRET,
                    whisper_model="/missing", ollama_model="/missing",
                    piper_binary="/missing", piper_model="/missing",
                    client=client, rtc=object(), max_sessions=1,
                )
        self.assertEqual(client.claims, [])

    async def test_invalid_invites_are_skipped_without_preventing_future_call(self):
        client = Control([
            event("../bad", 1), event(CALL1, 2, kind="ENDED"),
            event(CALL2, 3),
        ])
        self.assertEqual(await self.run_worker(client), 1)
        self.assertEqual(client.claims, [CALL2])

    async def test_explicit_warm_transfer_can_wrap_inbound_responder(self):
        client = Control()
        with patch.dict("os.environ", {"PB_ENABLE_STAFF_HANDOFF": "1",
                                       "PB_APPOINTMENT_DB": "",
                                       "PB_CALL_NOTES_DB": ""}):
            await self.run_worker(client)
        self.assertIn(("responder", "HumanHandoffResponder"), Session.logs)

    def test_inbound_filter_fails_closed(self):
        for item in (None, "bad", {"kind": "RINGING"}, event(CALL1, 1, ai_originated=True),
                     event(CALL1, 2, direction="outgoing"), event(CALL1, 3, kind="ENDED")):
            self.assertFalse(is_inbound_invitation(item))
        self.assertTrue(is_inbound_invitation(event(CALL1, 5)))


if __name__ == "__main__":
    unittest.main()
