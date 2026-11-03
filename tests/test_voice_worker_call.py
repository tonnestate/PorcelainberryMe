"""Agent-originated call orchestration contract: no real network or model imports."""
import asyncio
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from porcelainberryme.voice_livekit_agent import call_human


class OutboundAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_agent_can_start_and_finish_a_local_ip_call(self):
        logs = []
        media_module = ModuleType("livekit")
        media_module.rtc = SimpleNamespace()
        class FakeControl:
            def __init__(self, token):
                logs.append(("token", token))
                self.checks = 0

            async def whoami(self):
                return {"role": "agent", "tenant": "tenantA", "subject": "agentA"}

            async def originate(self, target, request_id):
                logs.append(("originate", target, request_id))
                return {"id": "a" * 32, "agent_initiated": True}

            async def status(self, call_id):
                self.checks += 1
                return {"state": "ACTIVE"}

            async def hangup(self, call_id):
                logs.append(("hangup", call_id))

        class FakeSession:
            def __init__(self, **kwargs):
                logs.append(("session", kwargs["tenant"], kwargs["callee"]))

            async def speak(self, call_id):
                logs.append(("voice", call_id))

        async def mocked_load(*args, **kwargs):
            return object()

        with patch.dict(sys.modules, {"livekit": media_module}), \
             patch("porcelainberryme.voice_livekit_agent.LocalFasterWhisper",
                   return_value=object()), \
             patch("porcelainberryme.voice_livekit_agent.LocalPiperVoice",
                   return_value=object()), \
             patch("porcelainberryme.voice_livekit_agent.LocalOllamaResponder",
                   return_value=object()), \
             patch("porcelainberryme.voice_livekit_agent.CallControlClient", FakeControl), \
             patch("porcelainberryme.voice_livekit_agent.LiveKitAgentCall", FakeSession):
            await call_human(
                target="humanA", tenant="tenantA", agent_id="agentA",
                control_token="verified-token", request_id="approved-call-01",
                whisper_model="/existing-whisper", ollama_model="existing-qwen",
                piper_binary="/existing-piper", piper_model="/existing-voice",
            )
        self.assertIn(("originate", "humanA", "approved-call-01"), logs)
        self.assertIn(("voice", "a" * 32), logs)
        self.assertIn(("hangup", "a" * 32), logs)

    async def test_user_issued_token_cannot_run_as_agent(self):
        logs = []
        media_module = ModuleType("livekit")
        media_module.rtc = SimpleNamespace()
        class FakeControl:
            def __init__(self, token):
                pass

            async def whoami(self):
                return {"role": "human", "tenant": "tenantA", "subject": "humanA"}

            async def originate(self, target, request_id):
                logs.append("incorrectly_called")
                raise AssertionError("should never originate")

        with patch.dict(sys.modules, {"livekit": media_module}), \
             patch("porcelainberryme.voice_livekit_agent.LocalFasterWhisper",
                   return_value=object()), \
             patch("porcelainberryme.voice_livekit_agent.LocalPiperVoice",
                   return_value=object()), \
             patch("porcelainberryme.voice_livekit_agent.LocalOllamaResponder",
                   return_value=object()), \
             patch("porcelainberryme.voice_livekit_agent.CallControlClient", FakeControl):
            with self.assertRaisesRegex(PermissionError, "identity_mismatch"):
                await call_human(
                    target="humanA", tenant="tenantA", agent_id="agentA",
                    control_token="wrong-role-token", request_id="approved-call-01",
                    whisper_model="/existing-whisper", ollama_model="existing-qwen",
                    piper_binary="/existing-piper", piper_model="/existing-voice",
                )
        self.assertEqual(logs, [])

    async def test_no_public_mobile_number_without_pstn(self):
        with self.assertRaisesRegex(ValueError, "not_public_phone_number"):
            await call_human(
                target="+491234567890", tenant="tenantA", agent_id="agentA",
                control_token="agent-token", request_id="approved-call-01",
                whisper_model="/existing-whisper", ollama_model="existing-qwen",
                piper_binary="/existing-piper", piper_model="/existing-voice",
            )


if __name__ == "__main__":
    unittest.main()
