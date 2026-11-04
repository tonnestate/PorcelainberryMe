"""Hermes-style separate local voice model; no paid LLM or model downloads."""
from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from porcelainberryme.local_voice import LocalOllamaResponder
from porcelainberryme.voice_livekit_agent import prepare_local_voice


class FastVoiceModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_and_fast_model_selection(self):
        with patch.dict(os.environ, {
            "PB_LOCAL_VAD": "energy", "PB_AGENT_KNOWLEDGE_DB": "",
            "PB_LOCAL_VOICE_CHAT_MODEL": "",
        }), patch("porcelainberryme.voice_livekit_agent.LocalFasterWhisper"), (
                patch("porcelainberryme.voice_livekit_agent.LocalPiperVoice")), (
                patch.object(LocalOllamaResponder, "preflight", new_callable=AsyncMock)):
            _, normal, _ = await prepare_local_voice(
                tenant="t", agent_id="a", whisper_model="whisper",
                ollama_model="main:latest", piper_binary="piper",
                piper_model="de.onnx",
            )
            self.assertEqual(normal.model, "main:latest")
            self.assertEqual(normal.max_tokens, 160)
            self.assertFalse(normal.disable_thinking)
            with patch.dict(os.environ, {
                "PB_LOCAL_VOICE_CHAT_MODEL": "fast:latest",
                "PB_LOCAL_VOICE_CHAT_MAX_TOKENS": "48",
            }):
                _, fast, _ = await prepare_local_voice(
                    tenant="t", agent_id="a", whisper_model="whisper",
                    ollama_model="main:latest", piper_binary="piper",
                    piper_model="de.onnx",
                )
            self.assertEqual(fast.model, "fast:latest")
            self.assertEqual(fast.timeout, 15)
            self.assertEqual(fast.max_tokens, 48)
            self.assertTrue(fast.disable_thinking)

    async def test_fast_model_needs_no_separately_installed_heavy_model(self):
        with patch.dict(os.environ, {
            "PB_LOCAL_VAD": "energy", "PB_AGENT_KNOWLEDGE_DB": "",
            "PB_LOCAL_VOICE_CHAT_MODEL": "fast:local",
            "PB_LOCAL_VOICE_CHAT_MAX_TOKENS": "72",
        }), patch("porcelainberryme.voice_livekit_agent.LocalFasterWhisper"), (
                patch("porcelainberryme.voice_livekit_agent.LocalPiperVoice")), (
                patch.object(LocalOllamaResponder, "preflight", new_callable=AsyncMock)):
            _, responder, _ = await prepare_local_voice(
                tenant="t", agent_id="a", whisper_model="whisper",
                ollama_model="", piper_binary="piper", piper_model="de.onnx",
            )
        self.assertEqual(responder.model, "fast:local")
        self.assertEqual(responder.max_tokens, 72)

    async def test_invalid_fast_budget_fails_closed(self):
        with patch.dict(os.environ, {
            "PB_LOCAL_VOICE_CHAT_MODEL": "fast:latest",
            "PB_LOCAL_VOICE_CHAT_MAX_TOKENS": "nonsense",
            "PB_AGENT_KNOWLEDGE_DB": "",
        }), patch("porcelainberryme.voice_livekit_agent.LocalFasterWhisper"), (
                patch("porcelainberryme.voice_livekit_agent.LocalPiperVoice")):
            with self.assertRaisesRegex(ValueError, "token_budget"):
                await prepare_local_voice(
                    tenant="t", agent_id="a", whisper_model="whisper",
                    ollama_model="main", piper_binary="piper", piper_model="de.onnx",
                )

    def test_fast_llm_rejects_nonbounded_options(self):
        for kwargs in ({"max_tokens": 500}, {"max_tokens": True},
                       {"timeout": 0}, {"disable_thinking": "no"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    LocalOllamaResponder("fast:latest", **kwargs)


if __name__ == "__main__":
    unittest.main()
