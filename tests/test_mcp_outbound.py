"""MCP self-call admission: host-bound caller, fixed destination, no invented success."""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from types import ModuleType
from unittest.mock import patch

from porcelainberryme.mcp_server import MCPCallVisibility, build_server
from porcelainberryme.voice_livekit_agent import CallControlClient


class Control(CallControlClient):
    def __init__(self):
        self.identity = {"tenant": "tenantA", "subject": "voiceagent", "role": "agent"}
        self.checks = 0

    async def whoami(self):
        self.checks += 1
        return dict(self.identity)


class OutboundMCPTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def bridge(runner, control=None):
        return MCPCallVisibility(
            control or Control(), tenant="tenantA", subject="voiceagent",
            role="agent", call_target="approved_user",
            call_request_id="host-approved-event-0001", voice_launcher=runner,
        )

    async def test_one_job_even_after_repeat_and_completed(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def runner():
            calls.append("called")
            started.set()
            await release.wait()

        control = Control()
        bridge = self.bridge(runner, control)
        self.assertFalse((await bridge.capabilities())["read_only"])
        first = await bridge.call_me()
        self.assertEqual(first["state"], "IN_PROGRESS")
        self.assertFalse(first["call_result_verified"])
        await asyncio.wait_for(started.wait(), 1)
        second = await bridge.call_me()
        self.assertEqual(second["request_id"], "host-approved-event-0001")
        self.assertEqual(calls, ["called"])
        release.set()
        await asyncio.wait_for(bridge._launch_task, 1)
        self.assertEqual((await bridge.call_me_status())["state"], "FINISHED_UNVERIFIED")
        await bridge.call_me()
        self.assertEqual(calls, ["called"])

    async def test_rejected_identity_and_target(self):
        async def runner():
            pass

        control = Control()
        with self.assertRaisesRegex(PermissionError, "agent_identity_required"):
            MCPCallVisibility(control, tenant="tenantA", subject="voiceagent",
                              role="human", call_target="approved_user",
                              call_request_id="hostapproved0001", voice_launcher=runner)
        with self.assertRaisesRegex(ValueError, "approved_voice_job_required"):
            MCPCallVisibility(control, tenant="tenantA", subject="voiceagent",
                              role="agent", call_target="voiceagent",
                              call_request_id="hostapproved0001", voice_launcher=runner)
        with self.assertRaisesRegex(ValueError, "not_authorized"):
            MCPCallVisibility(control, tenant="tenantA", subject="voiceagent",
                              role="agent", call_target="approved_user")
        bridge = self.bridge(runner, control)
        control.identity["subject"] = "different"
        with self.assertRaisesRegex(PermissionError, "mcp_call_identity_mismatch"):
            await bridge.call_me()
        self.assertIsNone(bridge._launch_task)

    async def test_worker_failures_are_not_returned_as_success_or_retired(self):
        calls = []

        async def failing():
            calls.append(1)
            raise RuntimeError("private token cannot be leaked")

        bridge = self.bridge(failing)
        await bridge.call_me()
        await asyncio.sleep(0)
        status = await bridge.call_me_status()
        self.assertEqual(status["state"], "FAILED")
        self.assertNotIn("private", json.dumps(status))
        await bridge.call_me()
        self.assertEqual(calls, [1])

    async def test_only_opted_in_mcp_process_has_call_tool(self):
        class FakeMCP:
            def __init__(self, name):
                self.registered = {}

            def tool(self):
                def register(fn):
                    self.registered[fn.__name__] = fn
                    return fn
                return register

        root = ModuleType("mcp")
        server = ModuleType("mcp.server")
        server.MCPServer = FakeMCP

        async def runner():
            pass

        bridge = self.bridge(runner)
        with patch.dict(sys.modules, {"mcp": root, "mcp.server": server}):
            mcp = build_server(bridge)
        tool = mcp.registered["porcelainberryme_call_me"]
        self.assertEqual(tool.__code__.co_argcount, 0)
        self.assertIn("porcelainberryme_call_me_status", mcp.registered)
        result = json.loads(await tool())
        self.assertFalse(result["call_result_verified"])
        await bridge._launch_task


if __name__ == "__main__":
    unittest.main()
