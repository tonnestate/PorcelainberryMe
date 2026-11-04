"""Full local SIP-core acceptance: ARI Stasis -> ARI bridge -> PCM WebSocket.

Uses the production listener/ARI/media engine implementations and a real
loopback HTTP/WebSocket server with deterministic synthetic speech objects.
No external carrier, paid model, deployed Asterisk or real phone is implied.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import unittest
from unittest.mock import AsyncMock, patch

from porcelainberryme.asterisk_ari import AsteriskARI
from porcelainberryme.asterisk_stasis import AsteriskStasisListener, TrustedStasisRoute
from porcelainberryme.local_voice import AudioPacket

CALLER = "PJSIP-1234.1"
MEDIA = "media-1"
BRIDGE = "bridge-1"
UUID = "01234567-89ab-4cde-8fab-123456789abc"
APP = "porcelain_voice"
CONTEXT = "pbx_ai_entry"
EXT = "s"
PCM = struct.pack("<320h", *([1200] * 320))


class STT:
    async def transcribe(self, pcm):
        return "Ist das Sprachsystem erreichbar?"


class LLM:
    async def respond(self, text, history):
        return "Ja, die Antwort wurde erzeugt."


class TTS:
    async def synthesize(self, text):
        return AudioPacket(PCM, 16000)


class StasisThroughMediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            from aiohttp import web
        except ImportError:
            self.skipTest("optional aiohttp not installed")
        self.web = web
        self.ari_requests = []
        self.media_frames = []
        self.media_commands = []
        self.ari_cleanup = asyncio.Event()
        self.ari_continued = asyncio.Event()
        self.media_mark_received = asyncio.Event()
        self.caller_state = "Ringing"
        application = web.Application()
        application.router.add_route("*", "/ari/{tail:.*}", self.ari_handler)
        application.router.add_get("/media/{connection}", self.media_handler)
        self.runner = web.AppRunner(application)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        if hasattr(self, "runner"):
            await self.runner.cleanup()

    @staticmethod
    def channel():
        return {
            "id": CALLER,
            "state": "Up",
            "dialplan": {"context": CONTEXT, "exten": EXT},
            "caller": {"number": "+490000000"},  # never used as authority
        }

    async def ari_handler(self, request):
        from aiohttp import WSMsgType
        auth = "Basic " + base64.b64encode(
            b"pbx-local:private-test-token").decode()
        if request.headers.get("Authorization") != auth:
            return self.web.Response(status=401)
        path = request.path.removeprefix("/ari")
        self.ari_requests.append((request.method, path, dict(request.query)))
        if path == "/events":
            if request.query.get("app") != APP:
                return self.web.Response(status=403)
            ws = self.web.WebSocketResponse()
            await ws.prepare(request)
            # First event is from an arbitrary, untrusted route; no media
            # bridge is created until the exact approved context appears.
            wrong = dict(self.channel())
            wrong["dialplan"] = {"context": "untrusted", "exten": EXT}
            await ws.send_json({
                "type": "StasisStart", "application": APP, "channel": wrong,
            })
            await ws.send_json({
                "type": "StasisStart", "application": APP,
                "channel": self.channel(),
            })
            async for _ in ws:
                pass
            return ws
        if request.method == "POST" and path == f"/channels/{CALLER}/continue":
            self.ari_continued.set()
            return self.web.Response(status=204)
        if request.method == "GET" and path == f"/channels/{CALLER}":
            record = self.channel()
            record["state"] = self.caller_state
            return self.web.json_response(record)
        if request.method == "POST" and path == f"/channels/{CALLER}/answer":
            self.caller_state = "Up"
            return self.web.Response(status=204)
        if request.method == "POST" and path == "/bridges":
            return self.web.json_response({"id": BRIDGE})
        if request.method == "POST" and path == "/channels/externalMedia":
            query = request.query
            if (query.get("connection_type") != "server"
                    or query.get("format") != "slin16"
                    or query.get("transport") != "websocket"
                    or query.get("transport_data") != "f(json)"):
                return self.web.Response(status=400)
            return self.web.json_response({"id": MEDIA})
        if request.method == "POST" and path == f"/bridges/{BRIDGE}/addChannel":
            return self.web.Response(status=204)
        if request.method == "GET" and path == f"/channels/{MEDIA}/variable":
            if request.query.get("variable") != "MEDIA_WEBSOCKET_CONNECTION_ID":
                return self.web.Response(status=400)
            return self.web.json_response({"value": UUID})
        if request.method == "DELETE" and path == f"/channels/{MEDIA}":
            return self.web.Response(status=204)
        if request.method == "DELETE" and path == f"/bridges/{BRIDGE}":
            self.ari_cleanup.set()
            return self.web.Response(status=204)
        return self.web.Response(status=404)

    async def media_handler(self, request):
        from aiohttp import WSMsgType
        if request.match_info["connection"] != UUID:
            return self.web.Response(status=404)
        ws = self.web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({
            "event": "MEDIA_START", "connection_id": UUID,
            "channel_id": MEDIA, "format": "slin16",
            "optimal_frame_size": 640, "ptime": 20,
        })
        for _ in range(12):
            await ws.send_bytes(PCM)
        for _ in range(35):
            await ws.send_bytes(bytes(640))
        async for inbound in ws:
            if inbound.type == WSMsgType.TEXT:
                payload = json.loads(inbound.data)
                self.media_commands.append(payload["command"])
                if payload["command"] == "MARK_MEDIA":
                    await ws.send_json({
                        "event": "MEDIA_MARK_PROCESSED", "channel_id": MEDIA,
                        "correlation_id": payload["correlation_id"],
                    })
                    self.media_mark_received.set()
                    await asyncio.sleep(0.2)
                    await ws.close()
            elif inbound.type == WSMsgType.BINARY:
                self.media_frames.append(bytes(inbound.data))
        return ws

    async def test_call_from_authorized_stasis_event_to_confirmed_media_then_cleanup(self):
        worker = AsteriskStasisListener(
            ari=AsteriskARI(
                user="pbx-local", password="private-test-token", port=self.port,
            ),
            route=TrustedStasisRoute(
                app=APP, context=CONTEXT, extension=EXT,
            ),
            tenant="tenantA", assistant="assistant",
            voice_settings={
                "whisper_model": "/installed/whisper",
                "ollama_model": "local-model",
                "piper_binary": "/installed/piper",
                "piper_model": "/installed/voice.onnx",
            },
        )
        stop = asyncio.Event()
        original_finish = worker._finished

        def completed(channel_id, task):
            original_finish(channel_id, task)
            stop.set()

        worker._finished = completed
        with patch.dict(os.environ, {
            "PB_ASTERISK_WS_USER": "", "PB_ASTERISK_WS_PASSWORD": "",
        }), patch(
            "porcelainberryme.asterisk_stasis.prepare_local_voice",
            new_callable=AsyncMock,
            return_value=(STT(), LLM(), TTS()),
        ) as preflight, patch(
            "porcelainberryme.asterisk_ari.prepare_local_voice",
            new_callable=AsyncMock,
        ) as cold_reload:
            await asyncio.wait_for(worker.serve(stop_event=stop), 10)
            preflight.assert_awaited_once()
            cold_reload.assert_not_awaited()

        self.assertEqual(worker.dispatched, 1)
        self.assertEqual(worker.failed, 0)
        self.assertTrue(self.media_mark_received.is_set())
        self.assertTrue(self.ari_cleanup.is_set())
        self.assertTrue(self.ari_continued.is_set())
        self.assertEqual(self.media_frames, [PCM])
        self.assertIn("FLUSH_MEDIA", self.media_commands)
        self.assertIn("START_MEDIA_BUFFERING", self.media_commands)
        self.assertIn("STOP_MEDIA_BUFFERING", self.media_commands)
        self.assertIn("MARK_MEDIA", self.media_commands)
        self.assertIn(("GET", f"/channels/{CALLER}", {}), self.ari_requests)
        self.assertIn(("POST", f"/channels/{CALLER}/answer", {}), self.ari_requests)
        operations = [(method, path) for method, path, _ in self.ari_requests]
        self.assertLess(
            operations.index(("POST", f"/channels/{CALLER}/answer")),
            operations.index(("POST", "/bridges")),
        )
        self.assertIn(("DELETE", f"/channels/{MEDIA}", {}), self.ari_requests)
        self.assertIn(("DELETE", f"/bridges/{BRIDGE}", {}), self.ari_requests)
        self.assertIn(("POST", f"/channels/{CALLER}/continue", {}),
                      self.ari_requests)
        operations = [(method, path) for method, path, _ in self.ari_requests]
        self.assertLess(
            operations.index(("DELETE", f"/bridges/{BRIDGE}")),
            operations.index(("POST", f"/channels/{CALLER}/continue")),
        )
        self.assertFalse(any(
            method == "DELETE" and path == f"/channels/{CALLER}"
            for method, path, _ in self.ari_requests
        ))


if __name__ == "__main__":
    unittest.main()
