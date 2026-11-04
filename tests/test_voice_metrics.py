"""PII-free bounded voice stage metrics from the real 20ms PCM turn engine."""
from __future__ import annotations
import asyncio
import json
import struct
import unittest

from porcelainberryme.local_voice import AudioPacket, VoiceTurnEngine


FRAME = struct.pack("<320h", *([1200] * 320))


class STT:
    async def transcribe(self, data):
        return "privat: konkrete Kundennummer 1234"


class LLM:
    async def respond(self, text, history):
        return "Vertrauliche Gesprächsantwort."


class TTS:
    async def synthesize(self, text):
        return AudioPacket(FRAME, 16000)


class Speaker:
    def interrupt(self):
        pass

    async def play(self, audio):
        return True


async def submit(engine):
    for _ in range(12):
        engine.receive(FRAME)
    for _ in range(35):
        engine.receive(bytes(640))
    if engine._worker is not None:
        await engine._worker


class MetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_latency_snapshot_is_bounded_and_has_no_transcript(self):
        engine = VoiceTurnEngine(STT(), LLM(), TTS(), Speaker())
        for _ in range(75):
            await submit(engine)
        report = engine.metrics_snapshot()
        self.assertEqual(report["schema"], "porcelainberryme.voice-metrics.v1")
        self.assertEqual(report["turns_completed"], 75)
        self.assertEqual(report["turn_errors"], 0)
        for name in ("stt_latency", "first_media_ack_latency",
                     "complete_turn_latency"):
            stat = report[name]
            self.assertEqual(stat["samples"], 64)
            self.assertIsInstance(stat["p50_ms"], int)
            self.assertIsInstance(stat["p95_ms"], int)
            self.assertLessEqual(stat["p50_ms"], stat["p95_ms"])
            self.assertLessEqual(stat["p95_ms"], stat["max_ms"])
        payload = json.dumps(report)
        self.assertNotIn("Kundennummer", payload)
        self.assertNotIn("Vertrauliche", payload)
        await engine.close()
        self.assertEqual(engine.metrics_snapshot()["turns_completed"], 75)

    async def test_silence_or_failed_audio_does_not_fake_media_ack(self):
        class DeniedSpeaker(Speaker):
            async def play(self, audio):
                return False
        engine = VoiceTurnEngine(STT(), LLM(), TTS(), DeniedSpeaker())
        for _ in range(50):
            engine.receive(bytes(640))
        self.assertEqual(engine.metrics_snapshot()["stt_latency"]["samples"], 0)
        await submit(engine)
        report = engine.metrics_snapshot()
        self.assertEqual(report["turns_completed"], 0)
        self.assertEqual(report["first_media_ack_latency"]["samples"], 0)
        self.assertEqual(report["stt_latency"]["samples"], 1)
        await engine.close()

    async def test_first_media_time_does_not_claim_human_heard_speech(self):
        engine = VoiceTurnEngine(STT(), LLM(), TTS(), Speaker())
        await submit(engine)
        report = engine.metrics_snapshot()
        self.assertEqual(report["first_media_ack_latency"]["samples"], 1)
        self.assertIn("not proof", report["measurement_note"])
        self.assertNotIn("text", report)
        await engine.close()


if __name__ == "__main__":
    unittest.main()
