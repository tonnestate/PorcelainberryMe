"""Offline native-completion -> authorized channel return flow."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from porcelainberryme.contracts import InboundEvent
from porcelainberryme.harness_ananasme import AnanasMeOwnerHarness
from porcelainberryme.harness_reply import NativeHarnessReplyDispatcher
from porcelainberryme.ledger import Ledger
from porcelainberryme.service import Gateway


def event(channel="whatsapp", sender="verified-owner"):
    return InboundEvent(
        tenant_id="owner-tenant", channel=channel,
        external_id="external-1", sender=sender,
        recipient="owner-identity", text="Bitte antworte",
    )


class FakeResponse:
    status = 200
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *args): return None
    def read(self, limit=32768):
        import json
        return json.dumps(self.payload).encode("utf-8")


class FakeChannelTransport:
    def __init__(self):
        self.messages = []
    def send(self, message):
        self.messages.append(message)
        return "accepted-provider-receipt"


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.thread = "a"*32
        self.mid = "b"*32
        self.harness = AnanasMeOwnerHarness(
            token="x"*32,
            verify_owner=lambda e: e.sender == "verified-owner",
            resolve_thread=lambda e: self.thread,
        )
        self.transport = FakeChannelTransport()
        self.gateway = Gateway(
            Ledger(Path(self.tmp.name) / "messages.sqlite3"),
            transports={c: self.transport for c in ("email","whatsapp","sms")},
            authorize=lambda m: m.tenant_id == "owner-tenant",
        )
        self.dispatcher = NativeHarnessReplyDispatcher(
            harness=self.harness, gateway=self.gateway,
            channel_sender=lambda e: "owner-identity",
            authorize_return=lambda e: e.sender == "verified-owner",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def response(self, state, reply=None, *, thread=None):
        return FakeResponse({
            "schema":"ananasme.assistant-result.v1",
            "message_id":self.mid,
            "thread_id":thread or self.thread,
            "state":state,
            "deliverable":state=="DONE",
            "reply":reply,
        })

    def test_no_send_before_authoritative_completion(self):
        for state in ("QUEUED","WORKING","HANDED_OFF","UNKNOWN","FAILED","REVIEW_UNVERIFIED"):
            with patch("porcelainberryme.harness_ananasme.urlopen",
                       return_value=self.response(state)):
                result = self.dispatcher.dispatch(event(), self.mid)
                self.assertEqual(result.state, "AWAITING_NATIVE_RESULT")
        self.assertEqual(self.transport.messages, [])

    def test_done_generates_once_to_original_verified_channel(self):
        for c in ("whatsapp","email","sms"):
            with patch("porcelainberryme.harness_ananasme.urlopen",
                       return_value=self.response("DONE","Native Aurora Antwort")):
                result = self.dispatcher.dispatch(event(c),self.mid)
                replay = self.dispatcher.dispatch(event(c),self.mid)
                self.assertEqual(result.state,"ACCEPTED")
                self.assertEqual(replay.state,"ACCEPTED")
        self.assertEqual(len(self.transport.messages),3)
        for msg in self.transport.messages:
            self.assertEqual(msg.text,"Native Aurora Antwort")
            self.assertEqual(msg.sender,"owner-identity")
            self.assertEqual(msg.recipient,"verified-owner")
        self.assertEqual(len({m.idempotency_key for m in self.transport.messages}),3)

    def test_owner_spoof_must_never_fetch_or_send(self):
        with patch("porcelainberryme.harness_ananasme.urlopen") as network:
            with self.assertRaises(PermissionError):
                self.dispatcher.dispatch(event(sender="customer"),self.mid)
            network.assert_not_called()
        self.assertEqual(self.transport.messages,[])

    def test_cross_thread_harness_result_rejected(self):
        with patch("porcelainberryme.harness_ananasme.urlopen",
                   return_value=self.response("DONE","Sensitive",thread="c"*32)):
            with self.assertRaisesRegex(RuntimeError,"identity_mismatch"):
                self.dispatcher.dispatch(event(),self.mid)
        self.assertEqual(self.transport.messages,[])

    def test_no_fake_empty_completed_reply(self):
        with patch("porcelainberryme.harness_ananasme.urlopen",
                   return_value=self.response("DONE","")):
            with self.assertRaisesRegex(RuntimeError,"final_reply_invalid"):
                self.dispatcher.dispatch(event(),self.mid)
        self.assertEqual(self.transport.messages,[])

    def test_phone_audio_not_dispatched_as_sms(self):
        with self.assertRaisesRegex(ValueError,"phone_reply_requires_sip_audio"):
            self.dispatcher.dispatch(event("phone"),self.mid)


if __name__ == "__main__":
    unittest.main()
