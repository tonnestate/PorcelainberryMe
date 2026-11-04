"""Asterisk Stasis route acceptance, bounded concurrency and real WS transport.

Does not use an external SIP provider, place a call or assert a remote phone
answered. The HTTP/WebSocket test binds a temporary local port only.
"""
from __future__ import annotations

import asyncio
import base64
from unittest.mock import patch
import unittest

from porcelainberryme.asterisk_ari import AsteriskARI
from porcelainberryme.asterisk_stasis import (
    AsteriskStasisListener, StasisListenerError, TrustedStasisRoute,
)

APP = "porcelain_voice"
CTX = "pbx_ai_entry"
EXT = "s"


def channel(channel_id="PJSIP-1234.1", *, context=CTX, extension=EXT,
            state="Up"):
    return {
        "id": channel_id, "state": state,
        "dialplan": {"context": context, "exten": extension},
        "caller": {"number": "+49123456789"},  # explicitly NOT an auth claim
    }


def stasis_event(item=None, *, application=APP, kind="StasisStart"):
    return {"type": kind, "application": application,
            "channel": channel() if item is None else item}


def listener(*, port=8088, max_calls=1):
    route = TrustedStasisRoute(app=APP, context=CTX, extension=EXT)
    ari = AsteriskARI(user="pbx-local", password="private-test-token", port=port)
    return AsteriskStasisListener(
        ari=ari, route=route, tenant="tenantA", assistant="assistant",
        voice_settings={
            "whisper_model": "/installed/whisper",
            "ollama_model": "local-model",
            "piper_binary": "/installed/piper",
            "piper_model": "/installed/voice.onnx",
        },
        max_calls=max_calls,
    )


class TrustedStasisTests(unittest.IsolatedAsyncioTestCase):
    async def test_untrusted_or_spoofed_route_denied_before_media(self):
        worker = listener()
        invocations = []
        async def fake_one(call_id):
            invocations.append(call_id)
        worker._serve_one = fake_one
        for invalid in (
            stasis_event(application="other_app"),
            stasis_event(kind="ChannelCreated"),
            stasis_event(channel(context="other_context")),
            stasis_event(channel(extension="wrong")),
            stasis_event(channel(state="Down")),
            stasis_event(channel("invalid/path")),
            {"type": "StasisStart", "application": APP,
             "channel": {"id": "PJSIP-1234.1", "state": "Up"}},
            {"type": "StasisStart", "application": APP, "channel": None},
        ):
            self.assertFalse(worker.accept_event(invalid))
        self.assertEqual(worker.dispatched, 0)
        self.assertEqual(invocations, [])
        self.assertTrue(worker.accept_event(stasis_event()))
        await asyncio.gather(*tuple(worker._calls.values()))
        self.assertEqual(invocations, ["PJSIP-1234.1"])
        # Same StasisStart delivered twice after a reconnect never redials.
        self.assertFalse(worker.accept_event(stasis_event()))
        self.assertEqual(worker.dispatched, 1)

    async def test_concurrency_gate_and_shutdown_cancel_owned_voice_jobs(self):
        worker = listener(max_calls=1)
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def long_call(call_id):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        worker._serve_one = long_call
        self.assertTrue(worker.accept_event(stasis_event()))
        await asyncio.wait_for(started.wait(), 1)
        self.assertFalse(worker.accept_event(stasis_event(channel("PJSIP-1234.2"))))
        self.assertEqual(worker.rejected, 1)
        await asyncio.wait_for(worker.close(), 2)
        self.assertTrue(stopped.is_set())
        self.assertEqual(worker.failed, 0)

    async def test_stasis_end_immediately_cancels_only_owned_channel(self):
        worker = listener()
        started = asyncio.Event()
        finished = asyncio.Event()

        async def ongoing(channel_id):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        worker._serve_one = ongoing
        self.assertTrue(worker.accept_event(stasis_event()))
        await asyncio.wait_for(started.wait(), 1)
        self.assertFalse(worker.accept_event({
            "type": "StasisEnd", "application": "other_app",
            "channel": channel(),
        }))
        self.assertFalse(worker.accept_event({
            "type": "StasisEnd", "application": APP,
            "channel": channel("PJSIP-not-ours.2"),
        }))
        self.assertFalse(finished.is_set())
        self.assertTrue(worker.accept_event({
            "type": "StasisEnd", "application": APP, "channel": channel(),
        }))
        await asyncio.wait_for(finished.wait(), 1)
        await worker.close()
        self.assertEqual(worker.failed, 0)

    async def test_double_check_with_real_ari_stasis_authorization_hook(self):
        try:
            import aiohttp
        except ImportError:
            self.skipTest("optional aiohttp not installed")
        worker = listener()
        observed = []

        async def examine(**kwargs):
            observed.append(kwargs["caller_channel_id"])
            self.assertTrue(kwargs["authorize_channel"](channel()))
            self.assertFalse(kwargs["authorize_channel"](
                channel(context="different")))
            self.assertFalse(kwargs["authorize_channel"](
                channel(channel_id="PJSIP-1234.2")))
            self.assertFalse(kwargs["authorize_channel"](
                channel(state="Down")))

        # Real _serve_one creates the session, but only the preflight/bridging
        # step is stubbed. A separate ARI test verifies real GET/POST/DELETE.
        async def completed_call(session, method, endpoint, query=None):
            if (method, endpoint) == ("GET", "/channels/PJSIP-1234.1"):
                return channel()
            if (method, endpoint) == ("POST", "/channels/PJSIP-1234.1/continue"):
                observed.append("continued")
                return None
            raise AssertionError("unapproved_ari_operation")

        with patch("porcelainberryme.asterisk_stasis.serve_authorized_ari_call",
                   new=examine), patch.object(
                       worker.ari, "_call", new=completed_call,
                   ):
            await worker._serve_one("PJSIP-1234.1")
        self.assertEqual(observed, ["PJSIP-1234.1", "continued"])

    async def test_failed_media_still_returns_verified_caller_to_dialplan(self):
        try:
            import aiohttp
        except ImportError:
            self.skipTest("optional aiohttp not installed")
        worker = listener()
        operations = []

        async def failed_media(**kwargs):
            raise RuntimeError("simulated_speech_failure")

        async def authorized_get(session, method, endpoint, query=None):
            operations.append((method, endpoint))
            if method == "GET":
                return channel()
            return None

        with patch("porcelainberryme.asterisk_stasis.serve_authorized_ari_call",
                   new=failed_media), patch.object(
                       worker.ari, "_call", new=authorized_get,
                   ):
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                await worker._serve_one("PJSIP-1234.1")
        self.assertIn(("POST", "/channels/PJSIP-1234.1/continue"), operations)

    async def test_untrusted_channel_state_never_gets_returned_by_our_host(self):
        try:
            import aiohttp
        except ImportError:
            self.skipTest("optional aiohttp not installed")
        worker = listener()
        operations = []

        async def denied(**kwargs):
            raise PermissionError("caller_not_authorized")

        async def wrong_get(session, method, endpoint, query=None):
            operations.append((method, endpoint))
            return channel(context="untrusted")

        with patch("porcelainberryme.asterisk_stasis.serve_authorized_ari_call",
                   new=denied), patch.object(
                       worker.ari, "_call", new=wrong_get,
                   ):
            with self.assertRaises(PermissionError):
                await worker._serve_one("PJSIP-1234.1")
        self.assertEqual(
            operations, [("GET", "/channels/PJSIP-1234.1")],
        )

    def test_invalid_route_or_limits_fail_closed(self):
        for kwargs in (
            {"app": "invalid/app", "context": CTX, "extension": EXT},
            {"app": APP, "context": "../secrets", "extension": EXT},
            {"app": APP, "context": CTX, "extension": "../admin"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    TrustedStasisRoute(**kwargs)
        with self.assertRaises(ValueError):
            listener(max_calls=5)


class NetworkStasisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            from aiohttp import web
        except ImportError:
            self.skipTest("optional aiohttp not installed")
        self.web = web
        self.events = []
        self.reject = False
        self.connections = 0
        app = web.Application()
        app.router.add_get("/ari/events", self.events_websocket)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        if hasattr(self, "runner"):
            await self.runner.cleanup()

    async def events_websocket(self, request):
        expected = "Basic " + base64.b64encode(
            b"pbx-local:private-test-token").decode()
        if (self.reject or request.headers.get("Authorization") != expected
                or request.query.get("app") != APP):
            return self.web.Response(status=401)
        ws = self.web.WebSocketResponse()
        await ws.prepare(request)
        self.connections += 1
        await ws.send_json(stasis_event(channel(context="untrusted")))
        await ws.send_json(stasis_event())
        await ws.send_json(stasis_event())
        try:
            async for _ in ws:
                pass
        finally:
            await ws.close()
        return ws

    async def test_real_local_websocket_delivers_only_authorized_route_once(self):
        worker = listener(port=self.port)
        stop = asyncio.Event()
        dispatched = []

        async def fake_serve(channel_id):
            dispatched.append(channel_id)
            stop.set()
        worker._serve_one = fake_serve
        await asyncio.wait_for(worker.serve(stop_event=stop), 5)
        self.assertEqual(dispatched, ["PJSIP-1234.1"])
        self.assertEqual(worker.dispatched, 1)
        self.assertEqual(worker.failed, 0)
        self.assertEqual(self.connections, 1)

    async def test_wrong_ari_auth_does_not_loop_as_temporary_outage(self):
        self.reject = True
        worker = listener(port=self.port)
        with self.assertRaisesRegex(StasisListenerError, "configuration_refused"):
            await asyncio.wait_for(worker.serve(), 4)
        self.assertEqual(worker.dispatched, 0)


if __name__ == "__main__":
    unittest.main()
