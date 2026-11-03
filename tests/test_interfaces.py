import json
from email.message import EmailMessage
import unittest

from porcelainberryme.contracts import InboundEvent
from porcelainberryme.email_transport import parse_inbound_email
from porcelainberryme.server import authenticated, signature
from porcelainberryme.voice import PythonVoiceBridge, SpeechResult, VoiceUnavailable


class BoundaryTests(unittest.TestCase):
    def test_signed_ingress_and_replay_window(self):
        secret = "s" * 48
        data = json.dumps(InboundEvent(
            tenant_id="t", channel="sms", external_id="message-1",
            sender="+49111111111", recipient="+49222222222", text="Hi"
        ).to_dict()).encode()
        sig = signature(secret, "1793460000", data)
        self.assertTrue(authenticated(secret, "1793460000", sig, data, now=1793460000))
        self.assertFalse(authenticated(secret, "1793460000", sig, data + b"x", now=1793460000))
        self.assertFalse(authenticated(secret, "1793460000", sig, data, now=1793460900))
        self.assertFalse(authenticated(secret, "garbage", sig, data, now=1793460000))

    def test_email_conversion(self):
        msg = EmailMessage()
        msg["From"] = "sender@example.org"
        msg["To"] = "inbox@example.org"
        msg["Message-ID"] = "<unique@example.org>"
        msg.set_content("Hallo aus dem Postfach")
        event = parse_inbound_email(msg.as_bytes(), tenant_id="tenant", mailbox="inbox@example.org")
        self.assertEqual(event.channel, "email")
        self.assertEqual(event.text, "Hallo aus dem Postfach")
        self.assertEqual(event.external_id, "<unique@example.org>")

    def test_email_automated_reply_rejected(self):
        msg = EmailMessage()
        msg["From"] = "sender@example.org"
        msg["Message-ID"] = "<automated@example.org>"
        msg["Auto-Submitted"] = "auto-replied"
        msg.set_content("Automatisch")
        with self.assertRaises(ValueError):
            parse_inbound_email(msg.as_bytes(), tenant_id="tenant", mailbox="inbox@example.org")

    def test_voice_starts_unbound_and_accepts_injected_python_callable(self):
        voice = PythonVoiceBridge()
        self.assertFalse(voice.ready)
        with self.assertRaises(VoiceUnavailable):
            voice.speak("Hallo")
        approved = PythonVoiceBridge(lambda text: SpeechResult(b"RIFFaudio", "audio/wav"))
        self.assertTrue(approved.ready)
        self.assertEqual(approved.speak("Hallo").audio, b"RIFFaudio")


if __name__ == "__main__":
    unittest.main()
