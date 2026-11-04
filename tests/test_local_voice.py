"""Offline tests for the actual local PCM->text->reply->PCM control path.

No model weights, cloud calls, telephony account or media server used.
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import struct
import tempfile
import unittest
from unittest.mock import patch

from porcelainberryme.local_voice import (
    AudioPacket, EnergyTurnDetector, LocalFasterWhisper,
    LocalOllamaResponder, LocalPiperVoice, LocalVoiceUnavailable,
    VoiceTurnEngine, WebRTCTurnDetector,
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

    async def test_two_local_voice_failures_close_call_instead_of_silence(self):
        class FailingLocalModel:
            async def respond(self, text, history):
                raise RuntimeError("local model crashed")

        fatal = []
        stt = FakeSTT()
        speaker = FakeSpeaker()
        engine = VoiceTurnEngine(
            stt, FailingLocalModel(), FakeTTS(), speaker,
            on_unavailable=lambda: fatal.append("model_unavailable"),
        )
        for _ in range(2):
            await submit_phrase(engine)
            if engine._worker is not None:
                await engine._worker
        self.assertEqual(engine.turn_errors, 2)
        self.assertEqual(engine.turns_completed, 0)
        self.assertEqual(fatal, ["model_unavailable"])
        prior = stt.index
        await submit_phrase(engine)
        self.assertEqual(stt.index, prior)
        await engine.close()

    async def test_successful_reply_resets_consecutive_model_failures(self):
        class FlakyLocalModel:
            def __init__(self):
                self.calls = 0
            async def respond(self, text, history):
                self.calls += 1
                if self.calls in (1, 3):
                    raise RuntimeError("temporary model fault")
                return "Jetzt ist das Modell wieder verfügbar."

        fatal = []
        engine = VoiceTurnEngine(
            FakeSTT(), FlakyLocalModel(), FakeTTS(), FakeSpeaker(),
            on_unavailable=lambda: fatal.append("must_not_close"),
        )
        for _ in range(3):
            await submit_phrase(engine)
            if engine._worker is not None:
                await engine._worker
        self.assertEqual(engine.turn_errors, 2)
        self.assertEqual(engine.turns_completed, 1)
        self.assertEqual(fatal, [])
        await engine.close()

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

    async def test_local_ollama_streams_complete_sentences_before_final_event(self):
        sent = []
        events = [
            {"done": False, "message": {"content": "Guten "}},
            {"done": False, "message": {"content": "Tag. Wie "}},
            {"done": False, "message": {"content": "geht es"}},
            {"done": True, "message": {"content": "?"}},
        ]

        class Stream:
            def __aiter__(self):
                async def chunks():
                    for event in events:
                        yield (json.dumps(event) + "\\n").encode("utf-8")
                return chunks()

        class Response:
            status = 200
            content = Stream()
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        class Session:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def post(self, url, *, json):
                sent.append((url, json))
                return Response()

        fake_http = SimpleNamespace(
            ClientTimeout=lambda total: total,
            ClientSession=lambda timeout: Session(),
            ClientError=RuntimeError,
        )
        with patch.dict("sys.modules", {"aiohttp": fake_http}):
            responder = LocalOllamaResponder("already-installed:7b")
            stream = responder.stream_sentences("Hallo", [])
            first = await anext(stream)
            self.assertEqual(first, "Guten Tag.")
            second = await anext(stream)
            self.assertEqual(second, "Wie geht es?")
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)
        self.assertEqual(sent[0][0], "http://127.0.0.1:11434/api/chat")
        self.assertEqual(sent[0][1]["stream"], True)
        self.assertEqual(sent[0][1]["messages"][-1]["content"], "Hallo")

    async def test_local_ollama_stream_rejects_missing_completion(self):
        class Stream:
            def __aiter__(self):
                async def chunks():
                    yield (json.dumps({
                        "done": False, "message": {"content": "Teil."},
                    }) + "\\n").encode()
                return chunks()

        class Response:
            status = 200
            content = Stream()
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        class Session:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def post(self, url, *, json):
                return Response()

        fake_http = SimpleNamespace(
            ClientTimeout=lambda total: total,
            ClientSession=lambda timeout: Session(),
            ClientError=RuntimeError,
        )
        with patch.dict("sys.modules", {"aiohttp": fake_http}):
            stream = LocalOllamaResponder("local:qwen").stream_sentences("Hallo", [])
            self.assertEqual(await anext(stream), "Teil.")
            with self.assertRaisesRegex(
                LocalVoiceUnavailable, "local_ollama_stream_incomplete",
            ):
                await anext(stream)

    async def test_incremental_tts_commits_only_fully_heard_stream(self):
        class StreamingLLM:
            def __init__(self):
                self.delivered = []
            async def respond(self, text, history):
                raise AssertionError("legacy full-response method must not be called")
            async def stream_sentences(self, text, history):
                yield "Guten Tag."
                await asyncio.sleep(0)
                yield "Wie geht es Ihnen?"
            def on_turn_delivered(self, utterance, response):
                self.delivered.append((utterance, response))

        llm = StreamingLLM()
        speaker = FakeSpeaker()
        engine = VoiceTurnEngine(FakeSTT(), llm, FakeTTS(), speaker)
        await submit_phrase(engine)
        if engine._worker is not None:
            await engine._worker
        self.assertEqual(engine.turn_errors, 0)
        self.assertEqual(engine.turns_completed, 1)
        self.assertEqual(len(speaker.packets), 2)
        self.assertEqual(llm.delivered, [
            ("Satz 1", "Guten Tag. Wie geht es Ihnen?"),
        ])
        await engine.close()

    async def test_barge_in_discards_unfinished_streamed_response(self):
        first_paused = asyncio.Event()
        release = asyncio.Event()

        class StreamingLLM:
            async def stream_sentences(self, text, history):
                if text == "Satz 1":
                    yield "Erste Antwort."
                    first_paused.set()
                    await release.wait()
                    yield "Nicht mehr sprechen."
                else:
                    yield "Zweite Antwort."

        stt, speaker = FakeSTT(), FakeSpeaker()
        engine = VoiceTurnEngine(stt, StreamingLLM(), FakeTTS(), speaker)
        await submit_phrase(engine)
        await asyncio.wait_for(first_paused.wait(), 1)
        await submit_phrase(engine)
        release.set()
        await asyncio.wait_for(engine._worker, 1)
        self.assertEqual(engine.turns_completed, 1)
        self.assertEqual(engine.turn_errors, 0)
        self.assertEqual(engine.history, [
            {"role": "user", "content": "Satz 2"},
            {"role": "assistant", "content": "Zweite Antwort."},
        ])
        # The first sentence may have played before the new utterance,
        # but its interrupted continuation must never be delivered.
        self.assertEqual(len(speaker.packets), 2)
        await engine.close()

    async def test_opt_in_webrtc_vad_uses_20ms_frames_and_resets_state(self):
        created = []

        class NativeVad:
            def __init__(self, mode):
                self.mode = mode
                created.append(self)

            def is_speech(self, data, sample_rate):
                self.assert_frame(data, sample_rate)
                return data != audio_frame(0)

            @staticmethod
            def assert_frame(data, sample_rate):
                assert sample_rate == 16000
                assert len(data) == 640

        with patch.dict("sys.modules", {"webrtcvad": SimpleNamespace(Vad=NativeVad)}):
            detector = WebRTCTurnDetector(mode=1)
            utterance = None
            for _ in range(12):
                detector.push(audio_frame())
            for _ in range(35):
                _started, result = detector.push(audio_frame(0))
                if result is not None:
                    utterance = result
            self.assertIsNotNone(utterance)
            self.assertEqual(utterance, audio_frame() * 12)
            self.assertEqual(created[0].mode, 1)
            self.assertGreaterEqual(len(created), 2)
            with self.assertRaisesRegex(ValueError, "invalid_voice_frame_size"):
                detector.push(b"invalid")

    async def test_opt_in_vad_preserves_voice_turn_pipeline(self):
        class NativeVad:
            def __init__(self, mode):
                assert mode == 1

            def is_speech(self, data, sample_rate):
                return data != audio_frame(0)

        with patch.dict("sys.modules", {"webrtcvad": SimpleNamespace(Vad=NativeVad)}):
            stt, llm, tts, speaker = FakeSTT(), FakeLLM(), FakeTTS(), FakeSpeaker()
            engine = VoiceTurnEngine(
                stt, llm, tts, speaker, vad_backend="webrtc",
            )
            await submit_phrase(engine)
            if engine._worker is not None:
                await engine._worker
            self.assertIsInstance(engine.detector, WebRTCTurnDetector)
            self.assertEqual(engine.turns_completed, 1)
            self.assertEqual(engine.turn_errors, 0)
            self.assertEqual(len(speaker.packets), 1)
            await engine.close()

    async def test_opt_in_vad_fails_closed_if_not_installed(self):
        with patch.dict("sys.modules", {"webrtcvad": None}):
            with self.assertRaisesRegex(
                LocalVoiceUnavailable, "local_webrtc_vad_not_installed",
            ):
                WebRTCTurnDetector()
        with self.assertRaisesRegex(ValueError, "invalid_vad_backend"):
            VoiceTurnEngine(
                FakeSTT(), FakeLLM(), FakeTTS(), FakeSpeaker(),
                vad_backend="unknown",
            )
        with self.assertRaisesRegex(ValueError, "invalid_webrtc_vad_mode"):
            WebRTCTurnDetector(mode=4)


if __name__ == "__main__":
    unittest.main()
