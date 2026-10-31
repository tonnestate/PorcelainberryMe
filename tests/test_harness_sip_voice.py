"""Simulated SIP -> existing AnanasMe -> PCM, no calls or downloaded voices."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from porcelainberryme.harness_ananasme import AnanasMeOwnerHarness
from porcelainberryme.harness_sip_voice import (
    NativeSipHarnessReply, event_from_verified_sip_transcript,
)
from porcelainberryme.ledger import Ledger
from porcelainberryme.voice_stream import InstalledPyPIStreamVoice, SipVoiceTurn


class FakeSipSession:
    def __init__(self):
        self.identity = SimpleNamespace(
            tenant_id="owner-tenant",
            call_id="sip-call-123",
            peer="sip:verified-owner@example.test",
            local_address="1000@example.test",
            direction="incoming",
        )
        self.audio_info = SimpleNamespace(tx_sample_rate=8000, tx_channels=1)
        self.frames = []

    def write_pcm(self, data):
        self.frames.append(data)
        return len(data)


async def approved_voice(text, rate, channels):
    yield b"\x01\x02" * 160


class NativeSipBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.temp.name)/"state.sqlite3")
        self.sip = FakeSipSession()
        self.harness = AnanasMeOwnerHarness(
            token="z"*32,
            verify_owner=lambda event: event.sender == "sip:verified-owner@example.test",
            resolve_thread=lambda event: "b"*32,
        )
        self.voice = SipVoiceTurn(
            self.sip, InstalledPyPIStreamVoice(approved_voice),
            authorize=lambda session: True,
        )
        self.bridge = NativeSipHarnessReply(
            ledger=self.ledger, harness=self.harness, playback=self.voice,
            authorize_phone=lambda event: event.sender == "sip:verified-owner@example.test",
        )
        self.event = event_from_verified_sip_transcript(
            self.sip, text="Wie ist der Stand?", turn_id="utterance-1"
        )
        self.assistant = "a"*32
        self.ledger.ingest(self.event)
        claim = self.ledger.claim_pending(tenant_id=self.event.tenant_id)
        self.assertTrue(self.ledger.acknowledge_claim(
            claim, state="HANDLED", receipt=self.assistant,
            handler_kind="OWNER_HARNESS"
        ))

    def tearDown(self):
        self.temp.cleanup()

    async def test_only_final_native_answer_enters_original_sip_session(self):
        with patch.object(self.harness, "observe_native_reply",
                          return_value={"state":"HANDED_OFF", "reply":None}):
            result = await self.bridge.speak_ready(self.event, self.assistant)
        self.assertEqual(result.state, "AWAITING_NATIVE_RESULT")
        self.assertEqual(self.sip.frames, [])
        with patch.object(self.harness, "observe_native_reply",
                          return_value={"state":"DONE", "reply":"Ergebnis bestätigt."}) as observed:
            result = await self.bridge.speak_ready(self.event, self.assistant)
            self.assertEqual(result.state, "PCM_SUBMITTED")
            self.assertEqual(result.bytes_submitted, 320)
            observed.assert_called_once()
        self.assertEqual(len(self.sip.frames), 1)
        with patch.object(self.harness, "observe_native_reply",
                          return_value={"state":"DONE", "reply":"Ergebnis bestätigt."}):
            repeat = await self.bridge.speak_ready(self.event, self.assistant)
        self.assertEqual(repeat.state, "ALREADY_ATTEMPTED")
        self.assertEqual(len(self.sip.frames), 1)

    async def test_different_sip_call_cannot_replay_earlier_answer(self):
        self.sip.identity.call_id = "another-call"
        with patch.object(self.harness, "observe_native_reply") as native:
            with self.assertRaisesRegex(PermissionError, "sip_event_session_mismatch"):
                await self.bridge.speak_ready(self.event, self.assistant)
            native.assert_not_called()
        self.assertEqual(self.sip.frames, [])

    async def test_unknown_harness_assistant_or_modified_transcript_rejected(self):
        changed = event_from_verified_sip_transcript(
            self.sip, text="Anderer Inhalt", turn_id="utterance-1"
        )
        with patch.object(self.harness, "observe_native_reply") as native:
            with self.assertRaisesRegex(PermissionError, "not_bound_to_inbound_event"):
                await self.bridge.speak_ready(changed, self.assistant)
            with self.assertRaisesRegex(PermissionError, "not_bound_to_inbound_event"):
                await self.bridge.speak_ready(self.event, "c"*32)
            native.assert_not_called()
        self.assertEqual(self.sip.frames, [])

    async def test_revoked_owner_permission_never_speaks(self):
        self.bridge.authorize_phone = lambda event: False
        with patch.object(self.harness, "observe_native_reply") as native:
            with self.assertRaisesRegex(PermissionError, "sip_owner_not_authorized"):
                await self.bridge.speak_ready(self.event, self.assistant)
            native.assert_not_called()
        self.assertEqual(self.sip.frames, [])

    async def test_interrupted_audio_never_replayed(self):
        entered = asyncio.Event()
        async def long_voice(text, rate, channels):
            entered.set()
            for _ in range(10):
                yield b"\x01\x02" * 160
        self.voice.voice = InstalledPyPIStreamVoice(long_voice)
        with patch.object(self.harness, "observe_native_reply",
                          return_value={"state":"DONE", "reply":"Längere Antwort"}):
            task = asyncio.create_task(self.bridge.speak_ready(self.event, self.assistant))
            await asyncio.wait_for(entered.wait(), 2)
            self.assertTrue(await self.voice.interrupt())
            result = await task
            self.assertEqual(result.state, "INTERRUPTED")
            before = len(self.sip.frames)
            reread = await self.bridge.speak_ready(self.event, self.assistant)
        self.assertEqual(reread.state, "ALREADY_ATTEMPTED")
        self.assertEqual(len(self.sip.frames), before)

    def test_stable_turn_id_is_required(self):
        first = event_from_verified_sip_transcript(
            self.sip, text="Hallo", turn_id="known-1"
        )
        again = event_from_verified_sip_transcript(
            self.sip, text="Hallo", turn_id="known-1"
        )
        self.assertEqual(first.external_id, again.external_id)
        with self.assertRaises(ValueError):
            event_from_verified_sip_transcript(
                self.sip, text="Hallo", turn_id="../../bad"
            )


if __name__ == "__main__":
    unittest.main()
