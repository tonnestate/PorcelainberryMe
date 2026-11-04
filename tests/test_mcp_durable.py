"""MCP cannot mint consent or choose a telephone number or tenant."""
from __future__ import annotations
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from types import ModuleType
import sys
import json

from porcelainberryme.outbound_jobs import DurableCallJobs
from porcelainberryme.mcp_server import MCPCallVisibility, build_server
from porcelainberryme.voice_livekit_agent import CallControlClient


class FakeControl(CallControlClient):
    def __init__(self):
        self.actor = {"tenant": "tenantA", "subject": "agent", "role": "agent"}
        self.checked = 0

    async def whoami(self):
        self.checked += 1
        return dict(self.actor)


class MCPDurableTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = DurableCallJobs(Path(temp.name) / "calls.sqlite")
        self.client = FakeControl()
        self.bridge = MCPCallVisibility(
            self.client, tenant="tenantA", subject="agent", role="agent",
            call_target="owner", job_store=self.store,
        )

    def approve(self):
        return self.store.approve(
            tenant="tenantA", agent="agent", recipient="owner",
            consent_reference="trusted-host-approved-001",
        )

    async def test_repeated_approved_calls_same_mcp_process(self):
        first, second = self.approve(), self.approve()
        self.assertNotEqual(first, second)
        self.assertFalse((await self.bridge.capabilities())["read_only"])
        self.assertEqual((await self.bridge.call_me(first))["state"], "QUEUED")
        self.assertEqual((await self.bridge.call_me(second))["state"], "QUEUED")
        self.assertEqual((await self.bridge.call_me(first))["state"], "QUEUED")
        self.assertEqual((await self.bridge.call_me_status(second))["state"], "QUEUED")
        self.assertGreaterEqual(self.client.checked, 5)

    async def test_invalid_or_cross_scoped_approval_not_authorized(self):
        with self.assertRaises(PermissionError):
            await self.bridge.call_me("f" * 32)
        wrong = self.store.approve(
            tenant="tenantB", agent="agent", recipient="owner",
            consent_reference="trusted-host-approved-001",
        )
        with self.assertRaises(PermissionError):
            await self.bridge.call_me(wrong)
        good = self.approve()
        self.client.actor["tenant"] = "tenantB"
        with self.assertRaises(PermissionError):
            await self.bridge.call_me(good)
        self.client.actor["tenant"] = "tenantA"
        self.client.actor["role"] = "human"
        with self.assertRaises(PermissionError):
            await self.bridge.call_me_status(good)

    async def test_tools_expose_only_approval_id_not_target_or_token(self):
        root, server_module = ModuleType("mcp"), ModuleType("mcp.server")
        class FakeMCP:
            def __init__(self, name):
                self.tools = {}
            def tool(self):
                def decorate(fn):
                    self.tools[fn.__name__] = fn
                    return fn
                return decorate
        server_module.MCPServer = FakeMCP
        with patch.dict(sys.modules, {"mcp": root, "mcp.server": server_module}):
            server = build_server(self.bridge)
        tool = server.tools["porcelainberryme_call_me"]
        self.assertEqual(tool.__code__.co_varnames[:tool.__code__.co_argcount],
                         ("approval_id",))
        key = self.approve()
        result = json.loads(await tool(key))
        self.assertEqual(result["state"], "QUEUED")
        self.assertNotIn("recipient", result)
        self.assertNotIn("token", result)


if __name__ == "__main__":
    unittest.main()
