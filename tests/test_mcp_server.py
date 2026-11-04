"""Read-only FruitStack MCP bridge contracts.

No network, authority elevation, new tenant or actual call is created.
The fake server tests the tool registration shape without installing MCP.
"""
from __future__ import annotations

import json
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

from porcelainberryme.mcp_server import MCPCallVisibility, build_server, main
from porcelainberryme.voice_livekit_agent import CallControlClient


FIRST = "a" * 32
SECOND = "b" * 32


class FakeControl(CallControlClient):
    def __init__(self):
        self.identity = {
            "tenant": "tenantA", "subject": "assistant", "role": "agent",
        }
        self.calls = []
        self.statuses = {
            FIRST: {
                "id": FIRST, "state": "ACTIVE", "direction": "outgoing",
                "peer": "confidential-caller", "media_token": "secret",
                "handoff": {"state": "OFFERED", "target": "private-employee"},
            },
            SECOND: {
                "id": SECOND, "state": "RINGING", "direction": "incoming",
                "peer": "another-secret", "tenant": "tenantA",
            },
        }
        self.allowed = {FIRST, SECOND}

    async def whoami(self):
        self.calls.append(("whoami",))
        return dict(self.identity)

    async def request(self, method, path, payload=None):
        self.calls.append((method, path))
        if method == "GET" and path == "/v1/ip-calls/active":
            return {"calls": list(self.statuses.values()), "server_time": 1}
        raise AssertionError("unexpected_api_path")

    async def inbox(self, after=0):
        self.calls.append(("inbox", after))
        return {"events": [
            {"cursor": after + i + 1, "kind": "ACTIVE", "call": {
                **self.statuses[FIRST], "peer": "private-person",
            }}
            for i in range(4)
        ], "next_cursor": after + 4}

    async def status(self, call_id):
        self.calls.append(("status", call_id))
        if call_id not in self.allowed:
            raise PermissionError("not_authorized")
        return dict(self.statuses[call_id])


def visibility(control):
    return MCPCallVisibility(
        control, tenant="tenantA", subject="assistant", role="agent",
    )


class MCPCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_identity_and_bounded_redacted_active_calls(self):
        client = FakeControl()
        bridge = visibility(client)
        capabilities = await bridge.capabilities()
        self.assertEqual(capabilities["tenant"], "tenantA")
        self.assertEqual(capabilities["role"], "agent")
        self.assertTrue(capabilities["read_only"])
        self.assertEqual(len(capabilities["tools"]), 4)
        out = await bridge.active_calls(1)
        self.assertTrue(out["truncated"])
        self.assertEqual(out["calls"], [{
            "id": FIRST, "state": "ACTIVE", "direction": "outgoing",
            "handoff_state": "OFFERED",
        }])
        serialized = json.dumps(out)
        for secret in ("confidential-caller", "private-employee", "media_token",
                       "secret", "another-secret"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(client.calls, [
            ("whoami",), ("whoami",), ("GET", "/v1/ip-calls/active"),
        ])

    async def test_exact_authorized_call_and_cross_tenant_fail_closed(self):
        client = FakeControl()
        bridge = visibility(client)
        result = await bridge.call_status(FIRST)
        self.assertEqual(result["call"]["id"], FIRST)
        self.assertNotIn("peer", result["call"])
        client.identity["tenant"] = "tenantB"
        before = len(client.calls)
        with self.assertRaisesRegex(PermissionError, "mcp_call_identity_mismatch"):
            await bridge.active_calls()
        self.assertEqual(client.calls[before:], [("whoami",)])
        with self.assertRaisesRegex(PermissionError, "mcp_call_identity_mismatch"):
            await bridge.call_status(FIRST)

    async def test_event_cursor_is_returned_only_after_last_exposed_event(self):
        client = FakeControl()
        bridge = visibility(client)
        reply = await bridge.call_events(after=100, limit=2)
        self.assertEqual(reply["next_cursor"], 102)
        self.assertTrue(reply["truncated"])
        self.assertEqual([item["cursor"] for item in reply["events"]], [101, 102])
        self.assertEqual(client.calls, [("whoami",), ("inbox", 100)])
        self.assertNotIn("private-person", json.dumps(reply))
        with self.assertRaisesRegex(ValueError, "mcp_invalid_event_cursor"):
            await bridge.call_events(after=-1)
        with self.assertRaisesRegex(ValueError, "mcp_invalid_event_limit"):
            await bridge.call_events(limit=100)
        self.assertEqual(len(client.calls), 2)

    async def test_unrelated_participant_is_not_visible(self):
        client = FakeControl()
        client.allowed = {SECOND}
        bridge = visibility(client)
        with self.assertRaisesRegex(PermissionError, "not_authorized"):
            await bridge.call_status(FIRST)

    async def test_invalid_arguments_and_upstream_contracts_are_rejected(self):
        client = FakeControl()
        bridge = visibility(client)
        for limit in (0, -1, 31, True, "20"):
            with self.subTest(limit=limit):
                with self.assertRaisesRegex(ValueError, "mcp_invalid_call_limit"):
                    await bridge.active_calls(limit)
        for call_id in ("bad", FIRST + "/hangup", "https://example.test",
                        "C" * 32):
            with self.subTest(call_id=call_id):
                with self.assertRaisesRegex(ValueError, "mcp_invalid_call_id"):
                    await bridge.call_status(call_id)
        self.assertEqual(client.calls, [])
        client.statuses[FIRST]["id"] = SECOND
        with self.assertRaisesRegex(RuntimeError, "response_mismatch"):
            await bridge.call_status(FIRST)
        client.statuses[SECOND]["state"] = "unverified"
        with self.assertRaisesRegex(RuntimeError, "mcp_call_contract_invalid"):
            await bridge.active_calls()

    async def test_schema_has_no_arbitrary_mcp_tool_dispatch(self):
        class FakeMCPServer:
            def __init__(self, name):
                self.name = name
                self.registered = {}

            def tool(self):
                def register(fn):
                    self.registered[fn.__name__] = fn
                    return fn
                return register

        top = ModuleType("mcp")
        server_package = ModuleType("mcp.server")
        server_package.MCPServer = FakeMCPServer
        client = FakeControl()
        with patch.dict(sys.modules, {"mcp": top, "mcp.server": server_package}):
            server = build_server(visibility(client))
            self.assertEqual(server.name, "PorcelainberryMe")
            self.assertEqual(set(server.registered), {
                "porcelainberryme_capabilities",
                "porcelainberryme_active_calls",
                "porcelainberryme_call_status",
                "porcelainberryme_call_events",
            })
            output = await server.registered["porcelainberryme_call_status"](FIRST)
        self.assertEqual(json.loads(output)["call"]["state"], "ACTIVE")

    def test_bad_host_scope_is_never_allowed(self):
        client = FakeControl()
        for kw, expected in [
            ({"tenant": "tenant/X"}, "mcp_trusted_tenant_required"),
            ({"subject": "untrusted owner"}, "mcp_trusted_subject_required"),
            ({"role": "owner"}, "mcp_trusted_role_required"),
        ]:
            config = dict(tenant="tenantA", subject="assistant", role="agent")
            config.update(kw)
            with self.assertRaisesRegex(ValueError, expected):
                MCPCallVisibility(client, **config)
        with self.assertRaisesRegex(ValueError, "mcp_authenticated_call_client_required"):
            MCPCallVisibility(object(), tenant="tenantA",
                              subject="assistant", role="agent")
        with patch.dict("os.environ", {"PB_MCP_CALL_TOKEN": "too-short"}):
            with self.assertRaisesRegex(ValueError, "mcp_host_scoped_call_token_required"):
                main()


if __name__ == "__main__":
    unittest.main()
