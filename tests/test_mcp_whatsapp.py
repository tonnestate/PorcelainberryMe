"""Owner WhatsApp MCP regression and optional real stdio discovery tests."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from porcelainberryme.contracts import InboundEvent
from porcelainberryme.ledger import Ledger
from porcelainberryme.mcp_server import MCPCallVisibility, build_server
from porcelainberryme.service import DeliveryRejected
from porcelainberryme.voice_livekit_agent import CallControlClient
from porcelainberryme.whatsapp_mcp import (
    OwnerWhatsAppApprovals, OwnerWhatsAppMCP,
)
from porcelainberryme.whatsapp_inbox import OwnerWhatsAppInbox


class Control(CallControlClient):
    def __init__(self):
        self.identity = {
            "tenant": "tenantA", "subject": "owner_agent", "role": "agent",
        }
        self.checked = 0

    async def whoami(self):
        self.checked += 1
        return dict(self.identity)


class OwnerMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.token = root / "private-token"
        self.token.write_text("a" * 48, encoding="utf-8")
        self.token.chmod(0o600)
        self.approval_db = str(root / "approvals.sqlite3")
        self.inbox_db = str(root / "inbound.sqlite3")
        self.sender = "personal_owner"
        self.store = OwnerWhatsAppApprovals(self.approval_db)
        self.inbound = Ledger(self.inbox_db)
        self.adapter = OwnerWhatsAppMCP(
            tenant="tenantA", agent="owner_agent", sender=self.sender,
            approvals_db=self.approval_db, token_file=str(self.token),
        )
        self.inbox = OwnerWhatsAppInbox(
            database=self.inbox_db, tenant="tenantA",
            owner_destination="owner_inbox",
        )
        self.control = Control()
        self.bridge = MCPCallVisibility(
            self.control, tenant="tenantA", subject="owner_agent", role="agent",
            whatsapp=self.adapter, whatsapp_inbox=self.inbox,
        )

    def tearDown(self):
        self.temp.cleanup()

    def approve(self, text="Hallo", recipient="49151234567@c.us",
                reference="verified-consent-001"):
        return self.store.approve(
            tenant="tenantA", agent="owner_agent", sender=self.sender,
            recipient=recipient, text=text, consent_reference=reference,
        )

    async def test_mcp_registration_requires_explicit_owner_configuration(self):
        class FakeMCP:
            def __init__(self, name):
                self.registered = {}

            def tool(self):
                def decorate(fn):
                    self.registered[fn.__name__] = fn
                    return fn
                return decorate

        root = ModuleType("mcp")
        server = ModuleType("mcp.server")
        server.MCPServer = FakeMCP
        with patch.dict(sys.modules, {"mcp": root, "mcp.server": server}):
            instance = build_server(self.bridge)
            default = build_server(MCPCallVisibility(
                self.control, tenant="tenantA", subject="owner_agent",
                role="agent",
            ))
        self.assertEqual(
            set(instance.registered) - set(default.registered), {
                "porcelainberryme_whatsapp_inbox",
                "porcelainberryme_whatsapp_send",
                "porcelainberryme_whatsapp_send_status",
            },
        )
        self.assertEqual(len(default.registered), 5)
        self.assertEqual(
            instance.registered["porcelainberryme_whatsapp_send"].__code__.co_argcount,
            1,
        )
        self.assertEqual(
            instance.registered["porcelainberryme_whatsapp_inbox"].__code__.co_argcount,
            2,
        )
        capabilities = await self.bridge.capabilities()
        self.assertFalse(capabilities["read_only"])
        self.assertEqual(len(capabilities["tools"]), 8)

    async def test_one_approved_send_and_repeated_id_returns_no_second_transport(self):
        approval = self.approve()
        captured = []

        def fake_send(transport, msg):
            captured.append((transport.owner_sender, msg.to_dict()))
            return "wa-provider-receipt"

        with patch("porcelainberryme.whatsapp_mcp.TonnEstateWhatsAppTransport.send",
                   fake_send):
            first = await self.bridge.whatsapp_send(approval)
            second = await self.bridge.whatsapp_send(approval)
        self.assertEqual(first["state"], "ACCEPTED")
        self.assertTrue(first["provider_accepted"])
        self.assertFalse(first["human_delivered"])
        self.assertEqual(second, first)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][1]["recipient"], "49151234567@c.us")
        self.assertEqual(captured[0][1]["text"], "Hallo")
        self.assertEqual(captured[0][1]["idempotency_key"], approval)
        self.assertNotIn("recipient", json.dumps(first))
        self.assertNotIn("Hallo", json.dumps(first))

    async def test_concurrent_send_is_atomic_and_no_duplicate(self):
        approval = self.approve(reference="verified-concurrent-001")
        calls = []

        def fake_send(_transport, _message):
            import time
            calls.append(1)
            time.sleep(0.04)
            return "provider-receipt"

        with patch("porcelainberryme.whatsapp_mcp.TonnEstateWhatsAppTransport.send",
                   fake_send):
            results = await asyncio.gather(
                *[self.bridge.whatsapp_send(approval) for _ in range(12)]
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            (await self.bridge.whatsapp_send_status(approval))["state"],
            "ACCEPTED",
        )
        self.assertTrue(all(item["state"] in ("UNKNOWN", "ACCEPTED")
                            for item in results))

    async def test_timeout_unknown_and_definite_rejection_do_not_retry(self):
        approval = self.approve(reference="verified-timeout-001")
        with patch("porcelainberryme.whatsapp_mcp.TonnEstateWhatsAppTransport.send",
                   side_effect=TimeoutError("private data")) as send:
            self.assertEqual(
                (await self.bridge.whatsapp_send(approval))["state"], "UNKNOWN"
            )
            self.assertEqual(
                (await self.bridge.whatsapp_send(approval))["state"], "UNKNOWN"
            )
            self.assertEqual(send.call_count, 1)
        refused = self.approve(reference="verified-refusal-001")
        with patch("porcelainberryme.whatsapp_mcp.TonnEstateWhatsAppTransport.send",
                   side_effect=DeliveryRejected("denied")):
            self.assertEqual(
                (await self.bridge.whatsapp_send(refused))["state"], "FAILED"
            )

    async def test_host_only_revoke_and_expiration(self):
        revoked = self.approve(reference="verified-revoke-001")
        self.assertEqual(
            self.store.revoke(
                approval_id=revoked, tenant="tenantA",
                agent="owner_agent", sender=self.sender,
            )["state"], "REVOKED",
        )
        with patch("porcelainberryme.whatsapp_mcp.TonnEstateWhatsAppTransport.send") as send:
            self.assertEqual(
                (await self.bridge.whatsapp_send(revoked))["state"], "REVOKED"
            )
            send.assert_not_called()
        expired = self.approve(reference="verified-expire-001")
        with sqlite3.connect(self.approval_db) as db:
            db.execute(
                "UPDATE whatsapp_mcp_approvals SET expires_at=0 "
                "WHERE approval_id=?", (expired,),
            )
        self.assertEqual(
            (await self.bridge.whatsapp_send_status(expired))["state"], "EXPIRED"
        )

    async def test_identity_consent_and_token_rejection_fail_closed(self):
        approval = self.approve()
        with self.assertRaisesRegex(PermissionError, "consent_conflict"):
            self.approve(text="changed", reference="verified-consent-001")
        with self.assertRaisesRegex(PermissionError, "not_authorized"):
            self.store.status(
                approval_id=approval, tenant="tenantB",
                agent="owner_agent", sender=self.sender,
            )
        self.control.identity["subject"] = "other"
        with self.assertRaisesRegex(PermissionError, "identity_mismatch"):
            await self.bridge.whatsapp_send(approval)
        self.control.identity["subject"] = "owner_agent"
        self.token.chmod(0o644)
        with self.assertRaisesRegex(PermissionError, "private_file_permissions|token_permissions"):
            await self.bridge.whatsapp_send(approval)
        self.token.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "recipient_invalid"):
            self.approve(recipient="group@g.us", reference="verified-badgroup-01")
        with self.assertRaisesRegex(PermissionError, "owner_identity_required"):
            MCPCallVisibility(
                self.control, tenant="tenantA", subject="owner_agent",
                role="human", whatsapp=self.adapter,
            )

    async def test_inbox_is_read_only_owner_scoped_and_cursor_bounded(self):
        for tenant, recipient, message_id, text in (
            ("tenantA", "owner_inbox", "msg01", "Hello"),
            ("tenantA", "other_mailbox", "msg02", "PRIVATE_OTHER"),
            ("tenantB", "owner_inbox", "msg03", "PRIVATE_TENANT"),
            ("tenantA", "owner_inbox", "msg04", "Second"),
        ):
            self.inbound.ingest(InboundEvent(
                tenant_id=tenant, channel="whatsapp",
                external_id=message_id, sender="49151234567@c.us",
                recipient=recipient, text=text,
            ))
        messages = await self.bridge.whatsapp_messages(limit=10)
        self.assertEqual(
            [x["text"] for x in messages["messages"]],
            ["Hello", "Second"],
        )
        serialized = json.dumps(messages)
        self.assertNotIn("PRIVATE_OTHER", serialized)
        self.assertNotIn("PRIVATE_TENANT", serialized)
        self.assertEqual(messages["source"],
                         "existing_authenticated_inbound_ledger")
        self.assertEqual(
            (await self.bridge.whatsapp_messages(
                after=messages["next_cursor"],
            ))["messages"], [],
        )
        for after, limit in ((-1, 1), (0, 0), (True, 10), (0, 31)):
            with self.assertRaises(ValueError):
                await self.bridge.whatsapp_messages(after, limit)
        self.control.identity["tenant"] = "tenantB"
        with self.assertRaisesRegex(PermissionError, "identity_mismatch"):
            await self.bridge.whatsapp_messages()
        self.assertEqual(self.inbound.pending(tenant_id="tenantA", limit=10)[0].text,
                         "Hello")


class RealWhatsAppMCPProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_discovery_opt_in_only(self):
        try:
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client
        except ImportError:
            self.skipTest("optional MCP SDK not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = root / "owner-token"
            token.write_text("a" * 40, encoding="utf-8")
            token.chmod(0o600)
            approvals_db = str(root / "approvals.sqlite")
            inbox_db = str(root / "inbound.sqlite")
            OwnerWhatsAppApprovals(approvals_db)
            Ledger(inbox_db)
            config = StdioServerParameters(
                command=sys.executable,
                args=["-m", "porcelainberryme.mcp_server"],
                env={
                    **os.environ,
                    "PB_MCP_CALL_TOKEN": "fake-agent-token-0123456789abcdef",
                    "PB_MCP_CALL_TOKEN_FILE": "",
                    "PB_MCP_TENANT": "tenantA",
                    "PB_MCP_SUBJECT": "owner_agent",
                    "PB_MCP_ROLE": "agent",
                    "PB_MCP_CALL_ME_ENABLED": "0",
                    "PB_MCP_WHATSAPP_ENABLED": "1",
                    "PB_MCP_WHATSAPP_OWNER_TENANT": "tenantA",
                    "PB_MCP_WHATSAPP_OWNER_AGENT": "owner_agent",
                    "PB_MCP_WHATSAPP_OWNER_SENDER": "personal_owner",
                    "PB_MCP_WHATSAPP_APPROVALS_DB": approvals_db,
                    "PB_MCP_WHATSAPP_TOKEN_FILE": str(token),
                    "PB_MCP_WHATSAPP_INBOX_DB": inbox_db,
                    "PB_MCP_WHATSAPP_OWNER_DESTINATION": "owner_inbox",
                },
            )
            async with asyncio.timeout(15):
                async with stdio_client(config) as (reader, writer):
                    async with ClientSession(reader, writer) as client:
                        await client.initialize()
                        tools = await client.list_tools()
                        names = {tool.name for tool in tools.tools}
                        self.assertEqual(len(names), 8)
                        self.assertIn("porcelainberryme_whatsapp_send", names)
                        self.assertIn("porcelainberryme_whatsapp_send_status", names)
                        self.assertIn("porcelainberryme_whatsapp_inbox", names)
                        self.assertNotIn("porcelainberryme_whatsapp_approve", names)


if __name__ == "__main__":
    unittest.main()
