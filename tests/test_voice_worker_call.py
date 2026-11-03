"""Agent-originated call orchestration contract: no real network or model imports."""
import asyncio
import os
from pathlib import Path
import tempfile
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from porcelainberryme.voice_livekit_agent import call_human
from porcelainberryme.agent_knowledge import KnowledgeStore, ApprovedKnowledgeResponder


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

    async def test_approved_knowledge_is_used_in_the_real_voice_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "knowledge.sqlite")
            store = KnowledgeStore(path)
            store.configure("tenantA", "agentA",
                            greeting="Guten Tag vom Firmenassistenten.")
            store.propose(
                "tenantA", "agentA", key="opening",
                question="Wann ist geöffnet?", answer="Von neun bis fünf.",
            )
            store.review(
                "tenantA", "agentA", key="opening",
                expected_revision=1, approve=True,
            )
            observed = []
            media_module = ModuleType("livekit")
            media_module.rtc = SimpleNamespace()

            class Control:
                def __init__(self, token):
                    pass

                async def whoami(self):
                    return {"role": "agent", "tenant": "tenantA", "subject": "agentA"}

                async def originate(self, target, request_id):
                    observed.append(("dial", target))
                    return {"id": "f" * 32, "agent_initiated": True}

                async def status(self, call_id):
                    return {"state": "ACTIVE"}

                async def hangup(self, call_id):
                    observed.append(("hangup", call_id))

            class Session:
                def __init__(self, **kwargs):
                    self.llm = kwargs["llm"]
                    observed.append(("llm_type", type(self.llm).__name__))
                    observed.append(("greeting", kwargs["greeting"]))

                async def speak(self, call_id):
                    observed.append(("approved_answer", await self.llm.respond(
                        "Wann ist geöffnet?", [],
                    )))
                    observed.append(("unapproved_answer", await self.llm.respond(
                        "Was kostet es?", [],
                    )))

            with patch.dict(sys.modules, {"livekit": media_module}), \
                 patch.dict(os.environ, {"PB_AGENT_KNOWLEDGE_DB": path}), \
                 patch("porcelainberryme.voice_livekit_agent.LocalFasterWhisper",
                       return_value=object()), \
                 patch("porcelainberryme.voice_livekit_agent.LocalPiperVoice",
                       return_value=object()), \
                 patch("porcelainberryme.voice_livekit_agent.LocalOllamaResponder",
                       side_effect=AssertionError("unknown facts must not use LLM")), \
                 patch("porcelainberryme.voice_livekit_agent.CallControlClient", Control), \
                 patch("porcelainberryme.voice_livekit_agent.LiveKitAgentCall", Session):
                await call_human(
                    target="humanA", tenant="tenantA", agent_id="agentA",
                    control_token="valid-token", request_id="approved-call-02",
                    whisper_model="/existing-whisper", ollama_model="",
                    piper_binary="/existing-piper", piper_model="/existing-voice",
                )
            self.assertIn(("dial", "humanA"), observed)
            self.assertIn(("llm_type", "ApprovedKnowledgeResponder"), observed)
            self.assertIn(("greeting", "Guten Tag vom Firmenassistenten."), observed)
            self.assertIn(("approved_answer", "Von neun bis fünf."), observed)
            self.assertIn(("unapproved_answer",
                           "Dazu habe ich keine freigegebene Information. "
                           "Bitte wende dich an das zuständige Team."), observed)

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
