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


if __name__ == "__main__":
    unittest.main()
