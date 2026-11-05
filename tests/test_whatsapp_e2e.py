"""Integrated signed WhatsApp inbound → MCP read → approved existing send.

No live provider credentials. The inbound path uses an actual loopback HTTP
server, the send path uses the real TonnEstate transport with the provider
HTTP boundary mocked, not a second WhatsApp client.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from porcelainberryme.contracts import InboundEvent
from porcelainberryme.ledger import Ledger
from porcelainberryme.mcp_server import MCPCallVisibility
from porcelainberryme.server import handler_for, signature
from porcelainberryme.voice_livekit_agent import CallControlClient
from porcelainberryme.whatsapp_inbox import OwnerWhatsAppInbox
from porcelainberryme.whatsapp_mcp import OwnerWhatsAppMCP
from porcelainberryme.whatsapp_mirror import OwnerWhatsAppMirror

_INBOUND_SECRET = "native-inbound-identity-strong-secret-" + "x" * 32
_MIRROR_SECRET = "independent-whatsapp-owner-secret-" + "z" * 32


class OwnerCallControl(CallControlClient):
    def __init__(self):
        self.who = {
            "tenant": "tenantA",
            "subject": "owner_agent",
            "role": "agent",
        }

    async def whoami(self):
        return dict(self.who)


class FakeHTTPResponse:
    status = 200

    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, *_args):
        return self._body


class OwnerWhatsAppEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = tempfile.TemporaryDirectory()
        folder = Path(self.root.name)
        self.inbound_db = str(folder / "inbound.sqlite3")
        self.approvals_db = str(folder / "approvals.sqlite3")
        self.token_path = folder / "owner-token"
        self.token_path.write_text("b" * 64, encoding="utf-8")
        self.token_path.chmod(0o600)
        self.ledger = Ledger(self.inbound_db)
        self.mirror = OwnerWhatsAppMirror(
            self.inbound_db, tenant="tenantA",
            owner_destination="owner_destination",
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), handler_for(
                self.ledger, secret=_INBOUND_SECRET, mirror=self.mirror,
                mirror_secret=_MIRROR_SECRET,
            ),
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True,
        )
        self.thread.start()
        self.control = OwnerCallControl()
        self.owner = OwnerWhatsAppMCP(
            tenant="tenantA", agent="owner_agent",
            sender="personal_owner", approvals_db=self.approvals_db,
            token_file=str(self.token_path),
        )
        self.bridge = MCPCallVisibility(
            self.control, tenant="tenantA", subject="owner_agent",
            role="agent", whatsapp=self.owner,
            whatsapp_inbox=OwnerWhatsAppInbox(
                database=self.inbound_db, tenant="tenantA",
                owner_destination="owner_destination",
            ),
        )

    async def asyncTearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.root.cleanup()

    def sign_inbound(self, event: InboundEvent, *, tamper=False):
        raw = json.dumps(
            event.to_dict(), separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        stamp = str(int(time.time()))
        signed = signature(_MIRROR_SECRET, stamp, raw)
        if tamper:
            signed = signature(_INBOUND_SECRET, stamp, raw)
        request = Request(
            f"http://127.0.0.1:{self.server.server_port}/v1/owner-whatsapp-mirror",
            method="POST", data=raw,
            headers={
                "Content-Type": "application/json",
                "X-PB-Timestamp": stamp, "X-PB-Signature": signed,
            },
        )
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())

    async def test_real_signed_ingress_then_mcp_read_and_explicit_approved_send(self):
        event = InboundEvent(
            tenant_id="tenantA", channel="whatsapp",
            external_id="wa-original-001", sender="49151234567@c.us",
            recipient="owner_destination",
            text="Bitte 1.234,56 € am 05.11.2026 um 18:30 – สวัสดี",
            conversation_ref="direct-chat-01",
        )
        status, response = await asyncio.to_thread(self.sign_inbound, event)
        self.assertEqual(status, 202)
        self.assertTrue(response["accepted"])
        duplicate_status, duplicate = await asyncio.to_thread(self.sign_inbound, event)
        self.assertEqual(duplicate_status, 200)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(self.ledger.pending(tenant_id="tenantA"), [])

        received = await self.bridge.whatsapp_messages()
        self.assertEqual(len(received["messages"]), 1)
        self.assertEqual(received["messages"][0]["text"], event.text)
        self.assertEqual(received["source"], "existing_authenticated_owner_mirror")
        self.assertEqual((await self.bridge.whatsapp_messages(
            after=received["next_cursor"],
        ))["messages"], [])

        outgoing = "Ja, ich melde mich um 18:30 – ขอบคุณ"
        draft = await self.bridge.whatsapp_prepare("49151234567@c.us", outgoing)
        self.assertEqual(draft["state"], "DRAFT")
        requests = []
        def provider(request, timeout):
            requests.append((request, timeout))
            return FakeHTTPResponse(json.dumps({
                "success": True, "data": {
                    "status": "sent", "sent_message_id": "accepted-remote-01",
                },
            }).encode("utf-8"))
        with patch("porcelainberryme.tonnestate_whatsapp.urlopen",
                   side_effect=provider):
            before = await self.bridge.whatsapp_send(draft["draft_id"])
            self.assertEqual(before["state"], "DRAFT")
            self.assertEqual(requests, [])
            human_review = self.owner.drafts.review(draft["draft_id"])
            self.owner.drafts.confirm(
                draft_id=draft["draft_id"],
                verified_recipient=human_review["recipient"],
                verified_text=human_review["text"],
            )
            confirmed = await self.bridge.whatsapp_send(draft["draft_id"])
            twice = await self.bridge.whatsapp_send(draft["draft_id"])

        self.assertEqual(confirmed["state"], "ACCEPTED")
        self.assertFalse(confirmed["human_delivered"])
        self.assertEqual(twice["state"], "ACCEPTED")
        self.assertEqual(len(requests), 1)
        request, timeout = requests[0]
        self.assertEqual(timeout, 20)
        self.assertEqual(request.full_url,
                         "http://127.0.0.1:5001/api/internal/v1/aurora/send")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + "b" * 64)
        self.assertEqual(request.get_header("X-idempotency-key"),
                         confirmed["approval_id"])
        content = json.loads(request.data)
        self.assertEqual(content["text"], outgoing)
        self.assertEqual(content["recipient_ref"], "49151234567@c.us")
        self.assertFalse(content["allow_self"])
        self.assertNotIn("tenant_id", content)
        self.assertEqual(
            (await self.bridge.whatsapp_send_status(draft["draft_id"]))["state"],
            "ACCEPTED",
        )

    async def test_mcp_token_subject_change_blocks_drafts_and_messages(self):
        self.control.who["subject"] = "foreign_agent"
        with self.assertRaisesRegex(PermissionError, "identity_mismatch"):
            await self.bridge.whatsapp_prepare("49151234567@c.us", "No")
        with self.assertRaisesRegex(PermissionError, "identity_mismatch"):
            await self.bridge.whatsapp_messages()


if __name__ == "__main__":
    unittest.main()
