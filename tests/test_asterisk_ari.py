"""Actual loopback ARI protocol checks; no SIP/PSTN provider needed."""
from __future__ import annotations
import unittest
from uuid import UUID

from porcelainberryme.asterisk_ari import AsteriskARI, ARIProtocolError

CHANNEL = "pbx.123.1"
CONNECTION = "01234567-89ab-4cde-8fab-123456789abc"


class ARILoopbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            from aiohttp import web
        except ImportError:
            self.skipTest("optional aiohttp not installed")
        self.web = web
        self.events = []
        self.media_fails = False
        app = web.Application()
        app.router.add_route("*", "/ari/{tail:.*}", self.handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.ari = AsteriskARI(user="agent", password="local-secret", port=port)

    async def asyncTearDown(self):
        if hasattr(self, "runner"):
            await self.runner.cleanup()

    async def handler(self, request):
        import base64
        path = request.path.removeprefix("/ari")
        self.events.append((request.method, path, dict(request.query)))
        auth = "Basic " + base64.b64encode(b"agent:local-secret").decode()
        if request.headers.get("Authorization") != auth:
            return self.web.Response(status=401)
        if request.method == "GET" and path == f"/channels/{CHANNEL}":
            return self.web.json_response({"id": CHANNEL, "state": "Up"})
        if request.method == "POST" and path == "/bridges":
            return self.web.json_response({"id": "bridge-1"})
        if request.method == "POST" and path == "/channels/externalMedia":
            if self.media_fails:
                return self.web.json_response({"error": "test"}, status=502)
            return self.web.json_response({"id": "media-1"})
        if request.method == "POST" and path == "/bridges/bridge-1/addChannel":
            return self.web.Response(status=204)
        if request.method == "GET" and path == "/channels/media-1/variable":
            return self.web.json_response({"value": CONNECTION})
        if request.method == "DELETE" and path in (
                "/channels/media-1", "/bridges/bridge-1"):
            return self.web.Response(status=204)
        return self.web.Response(status=404)

    async def test_trusted_existing_channel_creates_media_and_cleans_up(self):
        from aiohttp import ClientSession
        async with ClientSession() as session:
            bound = await self.ari.prepare(
                session, caller_channel_id=CHANNEL, stasis_app="voicebot",
                authorize_channel=lambda record: record["state"] == "Up",
            )
            self.assertEqual(bound.caller_id, CHANNEL)
            self.assertEqual(bound.connection_id, CONNECTION)
            self.assertEqual(bound.media_channel_id, "media-1")
            await self.ari.cleanup(session, bound)
        expected = [e for e in self.events if e[1] == "/channels/externalMedia"]
        self.assertEqual(len(expected), 1)
        self.assertEqual(expected[0][2]["transport"], "websocket")
        self.assertEqual(expected[0][2]["encapsulation"], "none")
        self.assertEqual(expected[0][2]["format"], "slin16")
        self.assertEqual(expected[0][2]["transport_data"], "f(json)")
        self.assertIn(("DELETE", "/channels/media-1", {}), self.events)
        self.assertIn(("DELETE", "/bridges/bridge-1", {}), self.events)
        self.assertFalse(any("DELETE" == method and CHANNEL in path
                             for method, path, _ in self.events))

    async def test_failed_permission_does_not_create_or_answer(self):
        from aiohttp import ClientSession
        async with ClientSession() as session:
            with self.assertRaises(PermissionError):
                await self.ari.prepare(
                    session, caller_channel_id=CHANNEL, stasis_app="voicebot",
                    authorize_channel=lambda _: False,
                )
        self.assertEqual(len(self.events), 1)

    async def test_media_failure_cleans_created_bridge_without_retrying(self):
        from aiohttp import ClientSession
        self.media_fails = True
        async with ClientSession() as session:
            with self.assertRaises(ARIProtocolError):
                await self.ari.prepare(
                    session, caller_channel_id=CHANNEL, stasis_app="voicebot",
                    authorize_channel=lambda _: True,
                )
        self.assertEqual(sum(e[1] == "/channels/externalMedia"
                             for e in self.events), 1)
        self.assertIn(("DELETE", "/bridges/bridge-1", {}), self.events)


if __name__ == "__main__":
    unittest.main()
