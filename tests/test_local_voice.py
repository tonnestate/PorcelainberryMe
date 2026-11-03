"""Offline tests for the actual local PCM->text->reply->PCM control path.

No model weights, cloud calls, telephony account or media server used.
"""
import asyncio
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from porcelainberryme.local_voice import (
    AudioPacket, EnergyTurnDetector, LocalFasterWhisper,
    LocalOllamaResponder, LocalPiperVoice, LocalVoiceUnavailable,
    VoiceTurnEngine,
)


def audio_frame(sample=1200):
    return struct.pack("<320h", *([sample] * 320))


async def submit_phrase(engine):
    for _ in range(12):
        engine.receive(audio_frame())
    for _ in range(35):
        engine.receive(audio_frame(0))
    await asyncio.sleep(0)


class FakeSTT:
    def __init__(self):
        self.index = 0

    async def transcribe(self, data):
        assert len(data) > 0
        self.index += 1
        return "Satz " + str(self.index)


class FakeLLM:
    def __init__(self):
        self.history_snapshots = []

    async def respond(self, text, history):
        self.history_snapshots.append(list(history))
        return "Antwort auf " + text


class FakeTTS:
    async def synthesize(self, text):
        return AudioPacket(audio_frame(), 16000)


class FakeSpeaker:
    def __init__(self):
        self.packets = []
        self.interruptions = 0

    def interrupt(self):
        self.interruptions += 1

    async def play(self, packet):
        self.packets.append(packet)
        return True


class LocalVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_pcm_turn_detector_and_memory_only_reply(self):
        stt, llm, tts, speaker = FakeSTT(), FakeLLM(), FakeTTS(), FakeSpeaker()
        engine = VoiceTurnEngine(stt, llm, tts, speaker)
        await submit_phrase(engine)
        if engine._worker is not None:
            await engine._worker
        self.assertEqual(engine.turn_errors, 0)
        self.assertEqual(engine.turns_completed, 1)
        self.assertEqual(stt.index, 1)
        self.assertEqual(len(speaker.packets), 1)
        self.assertEqual(engine.history, [
            {"role": "user", "content": "Satz 1"},
            {"role": "assistant", "content": "Antwort auf Satz 1"},
        ])
        self.assertEqual(llm.history_snapshots[0], [])
        await engine.close()
        self.assertEqual(engine.history, [])

    async def test_silent_audio_does_not_generate_calls(self):
        stt = FakeSTT()
        engine = VoiceTurnEngine(stt, FakeLLM(), FakeTTS(), FakeSpeaker())
        for _ in range(400):
            engine.receive(audio_frame(0))
        self.assertIsNone(engine._worker)
        self.assertEqual(stt.index, 0)
        await engine.close()

    async def test_barge_in_discards_old_model_result_without_overlap(self):
        first_answer_started = asyncio.Event()
        release_first = asyncio.Event()

        class SlowLLM(FakeLLM):
            async def respond(self, text, history):
                if text == "Satz 1":
                    first_answer_started.set()
                    await release_first.wait()
                return await super().respond(text, history)

        stt = FakeSTT()
        speaker = FakeSpeaker()
        engine = VoiceTurnEngine(stt, SlowLLM(), FakeTTS(), speaker)
        await submit_phrase(engine)
        await asyncio.wait_for(first_answer_started.wait(), 1)
        # A new real acoustic utterance starts while previous model is
        # thinking. The old output must never be emitted after barge-in.
        await submit_phrase(engine)
        release_first.set()
        await asyncio.wait_for(engine._worker, 1)
        self.assertEqual(engine.turns_completed, 1)
        self.assertEqual(stt.index, 2)
        self.assertEqual(len(speaker.packets), 1)
        self.assertEqual(engine.history[0]["content"], "Satz 2")
        self.assertEqual(engine.turn_errors, 0)
        await engine.close()

    async def test_bad_pcm_and_long_turn_are_bounded(self):
        detector = EnergyTurnDetector()
        with self.assertRaisesRegex(ValueError, "invalid_voice_frame_size"):
            detector.push(b"\x00\x00")
        _, recorded = False, None
        for _ in range(820):
            _, recorded = detector.push(audio_frame())
            if recorded:
                break
        self.assertIsNotNone(recorded)
        self.assertLessEqual(len(recorded), 16000 * 2 * 16)

    async def test_missing_model_fails_closed_no_download(self):
        with self.assertRaises(LocalVoiceUnavailable):
            LocalFasterWhisper("/definitely/no/model")
        with self.assertRaises(LocalVoiceUnavailable):
            LocalPiperVoice("/no/piper", "/no/model.onnx")

    async def test_installed_piper_uses_model_metadata_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "piper"
            model = Path(tmp) / "voice.onnx"
            exe.write_text("not executed")
            model.write_bytes(b"model")
            Path(str(model) + ".json").write_text(json.dumps({
                "audio": {"sample_rate": 22050},
            }), encoding="utf-8")
            piper = LocalPiperVoice(str(exe), str(model))
            self.assertEqual(piper.sample_rate, 22050)
            with self.assertRaisesRegex(ValueError, "invalid_speech_reply"):
                await piper.synthesize("")

    async def test_local_ollama_preflight_verifies_model_without_download(self):
        captured = []

        class Response:
            status = 200
            def __init__(self, body):
                self.body = body
            def read(self, size):
                return self.body

        class Connection:
            def __init__(self, host, port, timeout):
                captured.append((host, port, timeout))
            def request(self, method, path):
                captured.append((method, path))
            def getresponse(self):
                return Response(json.dumps({
                    "models": [{"name": "existing-qwen:7b", "model": "existing-qwen:7b"}],
                }).encode())
            def close(self):
                pass

        responder = LocalOllamaResponder("existing-qwen:7b")
        with patch("http.client.HTTPConnection", Connection):
            await responder.preflight()
        self.assertEqual(captured, [
            ("127.0.0.1", 11434, 5), ("GET", "/api/tags"),
        ])

        missing = LocalOllamaResponder("different-model:latest")
        with patch("http.client.HTTPConnection", Connection):
            with self.assertRaisesRegex(LocalVoiceUnavailable, "model_not_installed"):
                await missing.preflight()

    async def test_ollama_not_running_fails_before_agent_can_dial(self):
        def unreachable(host, port, timeout):
            raise ConnectionRefusedError("no local Ollama daemon")

        with patch("http.client.HTTPConnection", unreachable):
            with self.assertRaisesRegex(LocalVoiceUnavailable, "local_ollama_unreachable"):
                await LocalOllamaResponder("qwen:7b").preflight()

    async def test_corrupt_local_model_registry_fails_closed(self):
        class Connection:
            def __init__(self, host, port, timeout):
                pass
            def request(self, method, path):
                pass
            def getresponse(self):
                class Bad:
                    status = 200
                    def read(self, size):
                        return b"{invalid-json"
                return Bad()
            def close(self):
                pass

        with patch("http.client.HTTPConnection", Connection):
            with self.assertRaisesRegex(LocalVoiceUnavailable, "registry_invalid"):
                await LocalOllamaResponder("qwen:7b").preflight()

    async def test_local_ollama_is_loopback_only(self):
        recorded = []

        class Response:
            status = 200

            def read(self, length):
                return json.dumps({"message": {
                    "content": "Guten Tag."
                }}).encode()

        class Connection:
            def __init__(self, hostname, port, timeout):
                recorded.append((hostname, port))
            def request(self, method, path, body, headers):
                data = json.loads(body)
                self_body = data["messages"]
                assert self_body[-1] == {"role": "user", "content": "Hallo"}
                recorded.append((method, path, data["model"]))
            def getresponse(self):
                return Response()
            def close(self):
                pass

        backend = LocalOllamaResponder("existing-qwen")
        with patch("http.client.HTTPConnection", Connection):
            answer = await backend.respond("Hallo", [])
        self.assertEqual(answer, "Guten Tag.")
        self.assertEqual(recorded[0], ("127.0.0.1", 11434))
        self.assertEqual(recorded[1], ("POST", "/api/chat", "existing-qwen"))


if __name__ == "__main__":
    unittest.main()
