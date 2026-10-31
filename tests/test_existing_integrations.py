"""Offline contract tests for existing TonnEstate API and Piper CLI source."""
from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

from porcelainberryme.contracts import OutboundMessage
from porcelainberryme.ledger import Ledger
from porcelainberryme.piper_existing import ExistingPiperVoice
from porcelainberryme.service import Gateway
from porcelainberryme.tonnestate_whatsapp import (
    TonnEstateWhatsAppTransport, from_existing_whatsapp_event,
)
from porcelainberryme.voice import PythonVoiceBridge, VoiceUnavailable


class ExistingPiperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.exe = root / "piper"
        self.exe.write_bytes(b"stub")
        self.exe.chmod(0o700)
        self.model = root / "de_DE-existing.onnx"
        self.model.write_bytes(b"existing model")
        (root / "de_DE-existing.onnx.json").write_text("{}", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _wave() -> bytes:
        buff = BytesIO()
        with wave.open(buff, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(22050)
            wav.writeframes(b"\x00\x00" * 400)
        return buff.getvalue()

    def test_no_voice_files_no_automatic_download(self):
        with self.assertRaisesRegex(VoiceUnavailable, "PIPER_EXISTING"):
            ExistingPiperVoice(executable=self.exe, model=self.model.with_name("missing.onnx"))

    def test_existing_python_voice_explicitly_bound(self):
        engine = ExistingPiperVoice(executable=self.exe, model=self.model)
        audio = self._wave()
        def installed_cli(args, **kwargs):
            self.assertEqual(args[:3], [str(self.exe), "--model", str(self.model)])
            self.assertEqual(args[3], "--output_file")
            Path(args[4]).write_bytes(audio)
            self.assertEqual(kwargs["input"], "Hallo")
            self.assertNotIn("shell", kwargs)
            return subprocess.CompletedProcess(args, 0, "", "")
        bridge = PythonVoiceBridge(engine.synthesize)
        with patch("porcelainberryme.piper_existing.subprocess.run",
                   side_effect=installed_cli) as invocation:
            result = bridge.speak("Hallo")
        self.assertEqual(result.mime_type, "audio/wav")
        self.assertEqual(result.audio, audio)
        self.assertEqual(invocation.call_count, 1)

    def test_invalid_wav_is_rejected(self):
        engine = ExistingPiperVoice(executable=self.exe, model=self.model)
        def broken_cli(args, **kwargs):
            Path(args[4]).write_bytes(b"invalid"*8)
            return subprocess.CompletedProcess(args, 0, "", "")
        with patch("porcelainberryme.piper_existing.subprocess.run",
                   side_effect=broken_cli):
            with self.assertRaisesRegex(VoiceUnavailable, "PIPER_WAV_INVALID"):
                engine.synthesize("Hallo")

    def test_nonzero_exit_does_not_expose_stderr(self):
        engine = ExistingPiperVoice(executable=self.exe, model=self.model)
        with patch("porcelainberryme.piper_existing.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 1, "", "secret")):
            with self.assertRaisesRegex(VoiceUnavailable, "^PIPER_GENERATION_FAILED$"):
                engine.synthesize("Hallo")


class ExistingWhatsAppTests(unittest.TestCase):
    def test_owner_scope_and_existing_tonnestate_api(self):
        sender = "configured-owner"
        message = OutboundMessage(
            tenant_id="tenant-1", idempotency_key="send-001",
            channel="whatsapp", sender=sender, recipient="49151234567@c.us",
            text="Guten Tag")
        transport = TonnEstateWhatsAppTransport(
            owner_sender=sender, api_token="private-test-token")
        class DummyResponse:
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self, limit): return (
                b'{"success":true,"data":{"status":"sent",'
                b'"sent_message_id":"wa-200"},"meta":{}}'
            )
        with tempfile.TemporaryDirectory() as temp:
            gateway = Gateway(Ledger(Path(temp)/"state.sqlite3"),
                              transports={"whatsapp": transport},
                              authorize=lambda msg: msg.tenant_id == "tenant-1")
            with patch("porcelainberryme.tonnestate_whatsapp.urlopen",
                       return_value=DummyResponse()) as patched:
                result = gateway.send(message)
                self.assertEqual(result.state, "ACCEPTED")
                req = patched.call_args.args[0]
                self.assertEqual(
                    req.full_url,
                    "http://127.0.0.1:5001/api/internal/v1/aurora/send"
                )
                self.assertEqual(json.loads(req.data), {
                    "recipient_ref": "49151234567@c.us",
                    "text": "Guten Tag", "allow_self": False
                })
                self.assertEqual(req.get_header("X-idempotency-key"), "send-001")
                self.assertNotIn("tenant_id", json.loads(req.data))
                self.assertEqual(gateway.send(message).state, "ACCEPTED")
                self.assertEqual(patched.call_count, 1)

    def test_fail_closed_and_inbound_normalization(self):
        with self.assertRaises(ValueError):
            from_existing_whatsapp_event({"from_me": True}, tenant_id="t", owner_destination="owner")
        event = from_existing_whatsapp_event({
            "from_me": False,
            "message_id": "unique-msg", "sender": "49151234567@c.us",
            "message": "Anfrage", "conversation_id": "chat-7",
        }, tenant_id="tenant-a", owner_destination="owner")
        self.assertEqual(event.external_id, "unique-msg")
        self.assertEqual(event.conversation_ref, "chat-7")
        self.assertEqual(event.channel, "whatsapp")


if __name__ == "__main__":
    unittest.main()
