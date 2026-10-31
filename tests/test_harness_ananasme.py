"""Existing AnanasMe /api/threads/<id>/messages contract, mocked HTTP only."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from porcelainberryme.contracts import InboundEvent
from porcelainberryme.harness_ananasme import AnanasMeOwnerHarness
from porcelainberryme.inbound import InboundProcessor
from porcelainberryme.ledger import Ledger


def sample(channel="whatsapp", text="Guten Morgen", event_id="wamid-1"):
    return InboundEvent(
        tenant_id="owner-tenant", channel=channel, external_id=event_id,
        sender="registered-device", recipient="my-owner-account",
        text=text, conversation_ref="conversation-1",
    )


class FakeResponse:
    status = 202
    def __init__(self, payload):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return None
    def read(self, limit=8192):
        return json.dumps(self.payload).encode()


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.thread = "a" * 32
        self.messages = []
        self.verified = lambda event: event.sender == "registered-device"
        self.adapter = AnanasMeOwnerHarness(
            token="x" * 32,
            verify_owner=self.verified,
            resolve_thread=lambda event: self.thread,
        )

    def test_all_four_channels_can_use_same_native_harness(self):
        for channel in ("whatsapp", "email", "sms", "phone"):
            event = sample(channel, event_id="same-upstream-id")
            reply = FakeResponse({"message_id": "b"*32, "state": "QUEUED", "replayed": False})
            with patch("porcelainberryme.harness_ananasme.urlopen", return_value=reply) as send:
                outcome = self.adapter.submit_owner_message(event)
                self.assertEqual(outcome.message_id, "b"*32)
                request = send.call_args.args[0]
                self.assertEqual(request.full_url,
                                 "http://127.0.0.1:8765/api/threads/" + self.thread + "/messages")
                body = json.loads(request.data)
                self.assertEqual(body["mode"], "chat")
                self.assertEqual(body["text"], event.text)
                self.assertEqual(len(body["request_id"]), 32)
                self.assertEqual(request.get_method(), "POST")
        self.assertEqual(len({self.adapter.request_id(sample(channel, event_id="same-upstream-id"))
                              for channel in ("whatsapp", "email", "sms", "phone")}), 4)

    def test_request_id_stable_across_reprocessing(self):
        self.assertEqual(self.adapter.request_id(sample()), self.adapter.request_id(sample()))
        self.assertEqual(self.adapter.request_id(sample()),
                         self.adapter.request_id(sample(text="Changed")))
        self.assertNotEqual(self.adapter.request_id(sample()),
                            self.adapter.request_id(sample(event_id="wamid-2")))

    def test_unverified_customer_cannot_invoke_owner_controls(self):
        external = InboundEvent(
            tenant_id="owner-tenant", channel="whatsapp", external_id="external-1",
            sender="customer", recipient="my-owner-account", text="stop"
        )
        with patch("porcelainberryme.harness_ananasme.urlopen") as send:
            with self.assertRaisesRegex(PermissionError, "not_verified_owner"):
                self.adapter.submit_owner_message(external)
            send.assert_not_called()

    def test_wrong_thread_and_invalid_tokens_fail_closed(self):
        with self.assertRaises(ValueError):
            AnanasMeOwnerHarness(token="", verify_owner=self.verified,
                                resolve_thread=lambda event: self.thread)
        adapter = AnanasMeOwnerHarness(
            token="x"*32, verify_owner=self.verified,
            resolve_thread=lambda event: "not-a-valid-thread-id",
        )
        with patch("porcelainberryme.harness_ananasme.urlopen") as send:
            with self.assertRaisesRegex(PermissionError, "no_authorized_owner_thread"):
                adapter.submit_owner_message(sample())
            send.assert_not_called()

    def test_harness_acceptance_is_not_claimed_as_delivered(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "messages.sqlite3")
            event = sample()
            ledger.ingest(event)
            handler = InboundProcessor(
                ledger, tenant_id="owner-tenant",
                authorize=self.verified,
                handler=self.adapter.receipt_handler,
                handler_kind="OWNER_HARNESS",
            )
            reply = FakeResponse({"message_id": "c"*32, "state": "QUEUED", "replayed": False})
            with patch("porcelainberryme.harness_ananasme.urlopen", return_value=reply) as send:
                result = handler.run_once()
                self.assertEqual(result.state, "HANDLED")
                self.assertEqual(result.receipt, "c"*32)
                self.assertEqual(send.call_count, 1)
                self.assertIsNone(handler.run_once())
                self.assertEqual(ledger.inbound_status(
                    tenant_id="owner-tenant", channel="whatsapp", external_id="wamid-1"
                )["processing_receipt"], "c"*32)

    def test_unavailable_harness_never_retries_without_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "messages.sqlite3")
            ledger.ingest(sample())
            processor = InboundProcessor(
                ledger, tenant_id="owner-tenant", authorize=self.verified,
                handler=self.adapter.receipt_handler,
            )
            with patch("porcelainberryme.harness_ananasme.urlopen",
                       side_effect=TimeoutError("network response lost")) as send:
                result = processor.run_once()
                self.assertEqual(result.state, "UNKNOWN")
                self.assertIsNone(processor.run_once())
                self.assertEqual(send.call_count, 1)

    def test_tampered_or_missing_harness_receipt_rejected(self):
        for response in ({"state":"QUEUED"}, {"message_id":"b"*32,"state":"invented"}):
            with patch("porcelainberryme.harness_ananasme.urlopen",
                       return_value=FakeResponse(response)):
                with self.assertRaises(RuntimeError):
                    self.adapter.submit_owner_message(sample())

    def test_harness_permission_refusal(self):
        with patch("porcelainberryme.harness_ananasme.urlopen",
                   side_effect=HTTPError("http://127.0.0.1",401,"Unauthorized",{},None)):
            with self.assertRaises(PermissionError):
                self.adapter.submit_owner_message(sample())


if __name__ == "__main__":
    unittest.main()
