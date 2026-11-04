"""Asterisk chan_websocket JSON/slin16 local audio pipeline acceptance."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import struct
import unittest

from porcelainberryme.asterisk_ws import (
    AsteriskMediaError, AsteriskPcmSpeaker, AsteriskVoiceMedia,
)
from porcelainberryme.local_voice import AudioPacket

CHANNEL = "pbx1-123456789.999"
FRAME = struct.pack("<320h", *([1200] * 320))
SILENCE = bytes(640)


def msg(name, data):
    return SimpleNamespace(type=SimpleNamespace(name=name), data=data)


class FakeWS:
    def __init__(self, *, auto_mark=True):
        self.incoming = asyncio.Queue()
        self.outbound = []
        self.auto_mark = auto_mark

    def feed_event(self, event, **kwargs):
        self.incoming.put_nowait(msg("TEXT", json.dumps({
            "event": event, "channel_id": CHANNEL, **kwargs,
        })))

    def feed_pcm(self, pcm):
        self.incoming.put_nowait(msg("BINARY", pcm))

    def close(self):
        self.incoming.put_nowait(msg("CLOSED", ""))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.incoming.get()

    async def send_json(self, payload):
        self.outbound.append(payload)
        if payload["command"] == "MARK_MEDIA" and self.auto_mark:
            self.feed_event("MEDIA_MARK_PROCESSED",
                            correlation_id=payload["correlation_id"])

    async def send_bytes(self, pcm):
        self.outbound.append(pcm)


class STT:
    async def transcribe(self, pcm):
        return "Wie ist der Status?"


class LLM:
    async def respond(self, text, history):
        return "Alles überprüft."


class TTS:
    async def synthesize(self, text):
        return AudioPacket(FRAME, 16000)


class AsteriskBridgeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def bridge(ws):
        return AsteriskVoiceMedia(
            ws, expected_channel_id=CHANNEL,
            stt=STT(), llm=LLM(), tts=TTS(), max_seconds=5,
        )

    async def test_full_real_voice_engine_pcm_to_pcm_and_mark_confirmation(self):
        ws = FakeWS()
        session = self.bridge(ws)
        task = asyncio.create_task(session.run())
        ws.feed_event("MEDIA_START", format="slin16",
                      optimal_frame_size=640, ptime=20)
        for _ in range(12):
            ws.feed_pcm(FRAME)
        for _ in range(35):
            ws.feed_pcm(SILENCE)
        async def completed():
            while session.engine.turns_completed == 0:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(completed(), 3)
        self.assertEqual(session.engine.turn_errors, 0)
        commands = [x["command"] for x in ws.outbound if isinstance(x, dict)]
        # VoiceTurnEngine flushes stale playback as speech begins; this is
        # intentional. Confirm the final marked playback commands in order.
        self.assertEqual(commands[-3:], [
            "START_MEDIA_BUFFERING", "STOP_MEDIA_BUFFERING", "MARK_MEDIA",
        ])
        self.assertEqual([x for x in ws.outbound if isinstance(x, bytes)], [FRAME])
        ws.close()
        await asyncio.wait_for(task, 2)

    async def test_identity_or_codec_mismatch_denies_audio(self):
        for altered in (
            {"channel_id": "different", "format": "slin16",
             "optimal_frame_size": 640, "ptime": 20},
            {"channel_id": CHANNEL, "format": "ulaw",
             "optimal_frame_size": 160, "ptime": 20},
            {"channel_id": CHANNEL, "format": "slin16",
             "optimal_frame_size": 642, "ptime": 20},
        ):
            with self.subTest(altered=altered):
                ws = FakeWS()
                bridge = self.bridge(ws)
                with self.assertRaises(AsteriskMediaError):
                    bridge._text(json.dumps({"event": "MEDIA_START", **altered}))
                self.assertFalse(bridge.started)
                await bridge.engine.close()

    async def test_audio_before_handshake_denied_and_partial_frame_alignment(self):
        ws = FakeWS()
        bridge = self.bridge(ws)
        with self.assertRaisesRegex(AsteriskMediaError, "not_started"):
            bridge._binary(FRAME)
        bridge._text(json.dumps({
            "event": "MEDIA_START", "channel_id": CHANNEL,
            "format": "slin16", "optimal_frame_size": 640, "ptime": 20,
        }))
        bridge._binary(FRAME[:500])
        bridge._binary(FRAME[500:] + SILENCE)
        self.assertEqual(len(bridge._incoming), 0)
        with self.assertRaisesRegex(AsteriskMediaError, "invalid_pcm_frame"):
            bridge._binary(b"odd")
        await bridge.engine.close()

    async def test_media_start_rejects_different_ari_connection_uuid(self):
        ws = FakeWS()
        session = AsteriskVoiceMedia(
            ws, expected_channel_id=CHANNEL,
            expected_connection_id="01234567-89ab-4cde-8fab-123456789abc",
            stt=STT(), llm=LLM(), tts=TTS(),
        )
        with self.assertRaisesRegex(AsteriskMediaError, "connection_identity_mismatch"):
            session._text(json.dumps({
                "event": "MEDIA_START", "channel_id": CHANNEL,
                "connection_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                "format": "slin16", "optimal_frame_size": 640, "ptime": 20,
            }))
        self.assertFalse(session.started)
        await session.engine.close()

    async def test_backpressure_interrupt_and_asterisk_flush(self):
        ws = FakeWS(auto_mark=False)
        speaker = AsteriskPcmSpeaker(ws, mark_timeout=1)
        speaker.ready = True
        speaker.event("MEDIA_XOFF", {})
        pending = asyncio.create_task(speaker.play(AudioPacket(FRAME, 16000)))
        await asyncio.sleep(0)
        self.assertFalse(any(isinstance(x, bytes) for x in ws.outbound))
        speaker.interrupt()
        self.assertFalse(await asyncio.wait_for(pending, 1))
        await asyncio.sleep(0)
        self.assertIn({"command": "FLUSH_MEDIA"}, ws.outbound)
        speaker.event("MEDIA_XON", {})
        await speaker.aclose()

    async def test_no_unsupported_codec_or_wrong_audio_rate(self):
        ws = FakeWS()
        speaker = AsteriskPcmSpeaker(ws)
        speaker.ready = True
        with self.assertRaisesRegex(AsteriskMediaError, "alignment"):
            await speaker.play(AudioPacket(b"\x01\x00" * 10, 16000))
        with self.assertRaisesRegex(AsteriskMediaError, "alignment"):
            await speaker.play(AudioPacket(FRAME, 8000))
        await speaker.aclose()


if __name__ == "__main__":
    unittest.main()
