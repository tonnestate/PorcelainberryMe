"""MCP report tool returns bounded counters, never raw caller details."""
from __future__ import annotations

import json
import unittest

from porcelainberryme.mcp_server import MCPCallVisibility
from porcelainberryme.voice_livekit_agent import CallControlClient

COUNTERS = (
    "calls", "incoming", "outgoing", "agent_initiated",
    "accepted_confirmed", "declined", "timed_out", "ended",
    "ringing", "active", "transferred_confirmed",
    "duration_known_calls", "control_session_seconds",
    "acceptance_unverified",
)
DAY_COUNTERS = (
    "calls", "incoming", "outgoing", "accepted_confirmed", "declined",
    "timed_out", "transferred_confirmed", "control_session_seconds",
)


class Control(CallControlClient):
    def __init__(self):
        self.actor = {"tenant": "tenantA", "subject": "assistant", "role": "agent"}
        self.calls = []
        self.payload = {
            "schema": "porcelainberryme.reporting.calls.v1",
            "scope": "authenticated_participant_only",
            "timezone": "UTC",
            "metrics": {name: 1 for name in COUNTERS},
            "daily": [{"day": "2026-11-04", **{name: 1 for name in DAY_COUNTERS},
                       "peer": "do-not-share"}],
            "internal_private_token": "secret-not-for-model",
        }

    async def whoami(self):
        self.calls.append("whoami")
        return dict(self.actor)

    async def request(self, method, path, payload=None):
        self.calls.append((method, path))
        if (method, path) != ("GET", "/v1/ip-calls/report"):
            raise AssertionError("unexpected API")
        return self.payload


class MCPReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_kpis_only_with_whitelisted_day(self):
        client = Control()
        bridge = MCPCallVisibility(
            client, tenant="tenantA", subject="assistant", role="agent",
        )
        report = await bridge.call_report()
        self.assertEqual(client.calls, ["whoami", ("GET", "/v1/ip-calls/report")])
        self.assertEqual(report["metrics"]["calls"], 1)
        self.assertEqual(report["daily"][0]["day"], "2026-11-04")
        self.assertNotIn("peer", json.dumps(report))
        self.assertNotIn("secret-not-for-model", json.dumps(report))
        self.assertNotIn("accepted_at", json.dumps(report))
        self.assertEqual(report["scope"], "authenticated_participant_only")
        self.assertTrue((await bridge.capabilities())["read_only"])

    async def test_identity_mismatch_refuses_backend_query(self):
        client = Control()
        bridge = MCPCallVisibility(
            client, tenant="tenantA", subject="assistant", role="agent",
        )
        client.actor["tenant"] = "tenantB"
        with self.assertRaisesRegex(PermissionError, "identity_mismatch"):
            await bridge.call_report()
        self.assertEqual(client.calls, ["whoami"])

    async def test_malformed_or_pii_in_metrics_fails_closed(self):
        client = Control()
        bridge = MCPCallVisibility(
            client, tenant="tenantA", subject="assistant", role="agent",
        )
        client.payload["metrics"]["calls"] = "1"
        with self.assertRaisesRegex(RuntimeError, "contract_invalid"):
            await bridge.call_report()
        client.payload["metrics"]["calls"] = 1
        client.payload["daily"][0]["day"] = "2026-11-04'; DROP TABLE x"
        with self.assertRaisesRegex(RuntimeError, "contract_invalid"):
            await bridge.call_report()
        client.payload["daily"] = [client.payload["daily"][0]] * 32
        with self.assertRaisesRegex(RuntimeError, "contract_invalid"):
            await bridge.call_report()


if __name__ == "__main__":
    unittest.main()
