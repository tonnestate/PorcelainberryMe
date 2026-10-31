"""Offline synthetic voice-to-SIP tests; never import or install a TTS model."""
import asyncio
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from porcelainberryme.voice import PythonVoiceBridge, SpeechResult, VoiceUnavailable
from porcelainberryme.voice_stream import (
    ExistingPythonWavVoice, InstalledPyPIStreamVoice, PcmSpec, SipVoiceTurn,
)


def wav_data(rate=8000, channels=1):
    out = BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x01\x02" * 160 * channels)
    return out.getvalue()


class FakeSip:
    def __init__(self, rate=8000, channels=1):
        self.audio_info = SimpleNamespace(tx_sample_rate=rate, tx_channels=channels)
        self.frames = []

    def write_pcm(self, data):
        self.frames.append(data)
        return len(data)


class VoiceToSipTests(unittest.IsolatedAsyncioTestCase):
    async def test_injected_pypi_stream_writes_two_paced_frames(self):
        seen = []
        async def installed(text, rate, channels):
            seen.append((text, rate, channels))
            yield b"\x01\x02" * 160
            yield b"\x03\x04" * 160
        sip = FakeSip()
        turn = SipVoiceTurn(sip, InstalledPyPIStreamVoice(installed),
                            authorize=lambda session: True)
        result = await turn.play("Guten Morgen")
        self.assertEqual(result.state, "COMPLETED")
        self.assertEqual(result.bytes_written, 640)
        self.assertEqual(len(sip.frames), 2)
        self.assertEqual(seen, [("Guten Morgen", 8000, 1)])

    async def test_unverified_voice_and_denied_policy_never_play(self):
        sip = FakeSip()
        turn = SipVoiceTurn(sip, InstalledPyPIStreamVoice(),
                            authorize=lambda session: False)
        with self.assertRaises(PermissionError):
            await turn.play("Hallo")
        turn = SipVoiceTurn(sip, InstalledPyPIStreamVoice(),
                            authorize=lambda session: True)
        with self.assertRaisesRegex(VoiceUnavailable, "NOT_BOUND"):
            await turn.play("Hallo")
        self.assertEqual(sip.frames, [])

    async def test_barge_in_cancels_remaining_chunks(self):
        entered = asyncio.Event()
        async def source(text, rate, channels):
            entered.set()
            for _ in range(30):
                yield b"\x00\x01" * 160
        sip = FakeSip()
        turn = SipVoiceTurn(sip, InstalledPyPIStreamVoice(source),
                            authorize=lambda session: True)
        task = asyncio.create_task(turn.play("Diese Antwort unterbrechen"))
        await asyncio.wait_for(entered.wait(), 2)
        self.assertTrue(await turn.interrupt())
        result = await task
        self.assertEqual(result.state, "INTERRUPTED")
        self.assertLess(result.bytes_written, 30*320)
        self.assertFalse(await turn.interrupt())
        # The canceled turn must release its slot for the next turn.
        followup = await turn.play("Neue Antwort")
        self.assertEqual(followup.state, "COMPLETED")

    async def test_concurrent_turn_refused(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        async def source(text, rate, channels):
            entered.set()
            await release.wait()
            yield b"\x00\x01" * 160
        turn = SipVoiceTurn(FakeSip(), InstalledPyPIStreamVoice(source),
                            authorize=lambda session: True)
        task = asyncio.create_task(turn.play("Erste Antwort"))
        await asyncio.wait_for(entered.wait(), 2)
        with self.assertRaisesRegex(RuntimeError, "voice_turn_already_running"):
            await turn.play("Zweite Antwort")
        release.set()
        self.assertEqual((await task).state, "COMPLETED")

    async def test_reject_unaligned_and_excess_pcm(self):
        async def unaligned(text, rate, channels):
            yield b"\x00"
        sip = FakeSip()
        turn = SipVoiceTurn(sip, InstalledPyPIStreamVoice(unaligned),
                            authorize=lambda session: True)
        with self.assertRaisesRegex(VoiceUnavailable, "PCM_CHUNK_INVALID"):
            await turn.play("Hallo")
        self.assertEqual(sip.frames, [])

        async def oversized(text, rate, channels):
            yield b"\x00\x01" * 80
            yield b"\x00\x01" * 80
        with patch("porcelainberryme.voice_stream.MAX_AUDIO_BYTES", 200):
            turn = SipVoiceTurn(FakeSip(), InstalledPyPIStreamVoice(oversized),
                                authorize=lambda session: True)
            with self.assertRaisesRegex(VoiceUnavailable, "PCM_AUDIO_LIMIT"):
                await turn.play("Hallo")

    async def test_existing_python_pypi_voice_wav_adapter(self):
        bridge = PythonVoiceBridge(lambda text: SpeechResult(wav_data(), "audio/wav"))
        sip = FakeSip()
        turn = SipVoiceTurn(sip, ExistingPythonWavVoice(bridge),
                            authorize=lambda session: True)
        result = await turn.play("Vorlesen")
        self.assertEqual(result.state, "COMPLETED")
        self.assertEqual(result.bytes_written, 320)
        self.assertEqual(sip.frames, [b"\x01\x02" * 160])

    async def test_legacy_wav_format_mismatch_no_audio(self):
        bridge = PythonVoiceBridge(
            lambda text: SpeechResult(wav_data(rate=22050), "audio/wav"))
        sip = FakeSip()
        turn = SipVoiceTurn(sip, ExistingPythonWavVoice(bridge),
                            authorize=lambda session: True)
        with self.assertRaisesRegex(VoiceUnavailable, "VOICE_SIP_FORMAT_MISMATCH"):
            await turn.play("Hallo")
        self.assertEqual(sip.frames, [])

    async def test_no_provider_autoload_and_format_constraints(self):
        with self.assertRaisesRegex(VoiceUnavailable, "PYTHON_VOICE_NOT_BOUND"):
            ExistingPythonWavVoice(PythonVoiceBridge())
        self.assertFalse(InstalledPyPIStreamVoice().ready)
        with self.assertRaises(ValueError):
            PcmSpec(8000, 3)
        with self.assertRaises(ValueError):
            PcmSpec(0, 1)
        with self.assertRaises(ValueError):
            PcmSpec(True, 1)


if __name__ == "__main__":
    unittest.main()
