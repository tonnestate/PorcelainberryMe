"""Agent-originated call orchestration contract: no real network or model imports."""
import asyncio
import os
from pathlib import Path
import tempfile
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

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
                   return_value=SimpleNamespace(preflight=AsyncMock())), \
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

    async def test_agent_voice_worker_attaches_calendar_without_losing_greeting(self):
        from porcelainberryme.appointments import AppointmentStore
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = str(Path(tmp) / "knowledge.sqlite")
            calendar = str(Path(tmp) / "appointments.sqlite")
            KnowledgeStore(knowledge).configure(
                "tenantA", "agentA", greeting="Herzlich willkommen.",
            )
            AppointmentStore(calendar).configure(
                "tenantA", "agentA", tz="Europe/Berlin",
                duration_min=30, weekly={"0": [["09:00", "17:00"]]},
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
                    return {"id": "c" * 32, "agent_initiated": True}

                async def status(self, call_id):
                    return {"state": "ACTIVE"}

                async def hangup(self, call_id):
                    observed.append("ended")

            class Session:
                def __init__(self, **kwargs):
                    self.reply = kwargs["llm"]
                    observed.append(("greeting", kwargs["greeting"]))
                    observed.append(("type", type(self.reply).__name__))

                async def speak(self, call_id):
                    observed.append(("fallback", await self.reply.respond(
                        "Was kostet Ihr Service?", [],
                    )))

            with patch.dict(sys.modules, {"livekit": media_module}), \
                 patch.dict(os.environ, {
                     "PB_AGENT_KNOWLEDGE_DB": knowledge,
                     "PB_APPOINTMENT_DB": calendar,
                 }), \
                 patch("porcelainberryme.voice_livekit_agent.LocalFasterWhisper",
                       return_value=object()), \
                 patch("porcelainberryme.voice_livekit_agent.LocalPiperVoice",
                       return_value=object()), \
                 patch("porcelainberryme.voice_livekit_agent.CallControlClient", Control), \
                 patch("porcelainberryme.voice_livekit_agent.LiveKitAgentCall", Session):
                await call_human(
                    target="humanA", tenant="tenantA", agent_id="agentA",
                    control_token="verified-token", request_id="approved-call-03",
                    whisper_model="/existing-whisper", ollama_model="",
                    piper_binary="/existing-piper", piper_model="/existing-voice",
                )
            self.assertIn(("greeting", "Herzlich willkommen."), observed)
            self.assertIn(("type", "AppointmentVoiceResponder"), observed)
            self.assertIn(("fallback",
                           "Dazu habe ich keine freigegebene Information. "
                           "Bitte wende dich an das zuständige Team."), observed)
            self.assertIn("ended", observed)

    async def test_completed_staff_handoff_does_not_hangup_customer(self):
        media = ModuleType("livekit")
        media.rtc = SimpleNamespace()
        events = []
        state = {"transferred": False}

        class Control:
            def __init__(self, token):
                self.token = token

            async def whoami(self):
                return {"role": "agent", "tenant": "tenantA", "subject": "agentA"}

            async def originate(self, target, request_id):
                events.append(("dial", target))
                return {"id": "h" * 32, "agent_initiated": True}

            async def status(self, call_id):
                return {
                    "state": "ACTIVE",
                    "handoff": {
                        "state": "CONNECTED" if state["transferred"] else "OFFERED",
                        "target": "staff_member",
                    },
                }

            async def offer_handoff(self, call_id, idem):
                events.append(("offered", call_id))
                return {"state": "OFFERED", "target": "staff_member"}

            async def hangup(self, call_id):
                events.append(("unexpected_hangup", call_id))

        class Session:
            def __init__(self, **kwargs):
                self.llm = kwargs["llm"]
                events.append(("responder", type(self.llm).__name__))

            async def speak(self, call_id):
                self_text = await self.llm.respond(
                    "Bitte verbinden Sie mich mit einem Mitarbeiter.", [],
                )
                events.append(("agent_reply", self_text))
                state["transferred"] = True

        with patch.dict(sys.modules, {"livekit": media}), \
             patch.dict(os.environ, {
                 "PB_ENABLE_STAFF_HANDOFF": "1",
                 "PB_AGENT_KNOWLEDGE_DB": "",
                 "PB_APPOINTMENT_DB": "",
                 "PB_CALL_NOTES_DB": "",
             }), \
             patch("porcelainberryme.voice_livekit_agent.LocalFasterWhisper",
                   return_value=object()), \
             patch("porcelainberryme.voice_livekit_agent.LocalPiperVoice",
                   return_value=object()), \
             patch("porcelainberryme.voice_livekit_agent.LocalOllamaResponder",
                   return_value=SimpleNamespace(preflight=AsyncMock())), \
             patch("porcelainberryme.voice_livekit_agent.CallControlClient", Control), \
             patch("porcelainberryme.voice_livekit_agent.LiveKitAgentCall", Session):
            await call_human(
                target="humanA", tenant="tenantA", agent_id="agentA",
                control_token="local-token", request_id="handoff-call-0001",
                whisper_model="/already-existing", ollama_model="local-qwen",
                piper_binary="/already-existing-piper",
                piper_model="/already-existing-voice",
            )
        self.assertIn(("responder", "HumanHandoffResponder"), events)
        self.assertIn(("offered", "h" * 32), events)
        self.assertNotIn(("unexpected_hangup", "h" * 32), events)

    async def test_absent_local_llm_never_triggers_agent_ringing(self):
        from porcelainberryme.local_voice import LocalVoiceUnavailable
        media_module = ModuleType("livekit")
        media_module.rtc = SimpleNamespace()
        initiated = []

        class MissingLocalModel:
            async def preflight(self):
                raise LocalVoiceUnavailable("local_ollama_model_not_installed")

        class Control:
            def __init__(self, token):
                initiated.append("control_client_started")
            async def originate(self, target, request_id):
                initiated.append("incorrectly_rang")
                raise AssertionError("must not ring")

        with patch.dict(sys.modules, {"livekit": media_module}), \
             patch.dict(os.environ, {
                 "PB_AGENT_KNOWLEDGE_DB": "",
                 "PB_APPOINTMENT_DB": "",
             }), \
             patch("porcelainberryme.voice_livekit_agent.LocalFasterWhisper",
                   return_value=object()), \
             patch("porcelainberryme.voice_livekit_agent.LocalPiperVoice",
                   return_value=object()), \
             patch("porcelainberryme.voice_livekit_agent.LocalOllamaResponder",
                   return_value=MissingLocalModel()), \
             patch("porcelainberryme.voice_livekit_agent.CallControlClient", Control):
            with self.assertRaisesRegex(LocalVoiceUnavailable, "model_not_installed"):
                await call_human(
                    target="humanA", tenant="tenantA", agent_id="agentA",
                    control_token="local-token", request_id="blocked-call-0001",
                    whisper_model="/already-installed-whisper",
                    ollama_model="not-installed",
                    piper_binary="/already-installed-piper",
                    piper_model="/already-installed-voice",
                )
        self.assertEqual(initiated, [])

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
                   return_value=SimpleNamespace(preflight=AsyncMock())), \
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
