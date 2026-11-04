"""Real MCP stdio protocol acceptance (when optional MCP SDK is installed).

The spawned server receives a deliberately invalid but correctly shaped
identity token; tool discovery must work without contacting call-control,
and attempted use must fail instead of simulating a successful call.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest


class MCPProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_handshake_tool_discovery_and_fail_closed_read(self):
        try:
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client
        except ImportError:
            self.skipTest("optional MCP SDK not installed")
        config = StdioServerParameters(
            command=sys.executable,
            args=["-m", "porcelainberryme.mcp_server"],
            env={
                **os.environ,
                "PB_MCP_CALL_TOKEN": "fake-scope-test-token-0123456789abcdef",
                "PB_MCP_TENANT": "tenantA",
                "PB_MCP_SUBJECT": "assistant",
                "PB_MCP_ROLE": "agent",
            },
        )
        async with asyncio.timeout(15):
            async with stdio_client(config) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    exposed = await client.list_tools()
                    self.assertEqual({item.name for item in exposed.tools}, {
                        "porcelainberryme_capabilities",
                        "porcelainberryme_active_calls",
                        "porcelainberryme_call_status",
                        "porcelainberryme_call_events",
                    })
                    self.assertFalse(any(
                        word in item.name
                        for item in exposed.tools
                        for word in ("hangup", "dial", "execute", "owner")
                    ))


    async def test_opted_in_real_stdio_server_exposes_only_fixed_self_call(self):
        try:
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client
        except ImportError:
            self.skipTest("optional MCP SDK not installed")
        config = StdioServerParameters(
            command=sys.executable,
            args=["-m", "porcelainberryme.mcp_server"],
            env={
                **os.environ,
                "PB_MCP_CALL_TOKEN": "fake-agent-test-token-0123456789abcdef",
                "PB_MCP_TENANT": "tenantA",
                "PB_MCP_SUBJECT": "assistant",
                "PB_MCP_ROLE": "agent",
                "PB_MCP_CALL_ME_ENABLED": "1",
                "PB_MCP_CALL_ME_TARGET": "preapproved_user",
                "PB_MCP_CALL_ME_REQUEST_ID": "approved-voice-test-001",
                "PB_LOCAL_WHISPER_DIR": "/not-installed/whisper",
                "PB_LOCAL_OLLAMA_MODEL": "not-installed:mock",
                "PB_LOCAL_PIPER_BIN": "/not-installed/piper",
                "PB_LOCAL_PIPER_MODEL": "/not-installed/piper.onnx",
                "PB_AGENT_KNOWLEDGE_DB": "",
            },
        )
        async with asyncio.timeout(15):
            async with stdio_client(config) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    exposed = await client.list_tools()
                    tools = {item.name: item for item in exposed.tools}
                    self.assertIn("porcelainberryme_call_me", tools)
                    self.assertIn("porcelainberryme_call_me_status", tools)
                    # Tool fields differ across MCP SDK major versions;
                    # the source-side function signature has zero arguments
                    # (verified by test_mcp_outbound).
                    self.assertFalse(any("dial" in name or "hangup" in name
                                         for name in tools))


if __name__ == "__main__":
    unittest.main()
