"""Authenticated local Asterisk WebSocket transport acceptance.

Uses a real loopback WebSocket server when aiohttp is installed. Asterisk,
SIP trunk, public mobile number and provider configuration are not faked
as production services.
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from porcelainberryme.asterisk_worker import (
    main, serve_asterisk_media, validate_asterisk_binding,
)
from porcelainberryme.local_voice import AudioPacket


CHANNEL = "pbx1-123456789.999"
CONNECTION = "01234567-89ab-4cde-8fab-123456789abc"
FRAME = struct.pack("<320h", *([1200] * 320))


class LocalMediaWorkerTests(unittest.IsolatedAsyncioTestCase):
    def test_ari_connection_and_channel_are_host_bound(self):
        self.assertEqual(validate_asterisk_binding(
            channel_id=CHANNEL, connection_id=CONNECTION, port=8088,
        ), f"ws://127.0.0.1:8088/media/{CONNECTION}")
        for bad in ("", "not-a-uuid", "../media/secret", "A" * 100):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    validate_asterisk_binding(
                        channel_id=CHANNEL, connection_id=bad, port=8088,
                    )
        for bad_channel in ("", "../wrong", "bad channel", "@" * 64):
            with self.subTest(channel=bad_channel):
                with self.assertRaises(ValueError):
                    validate_asterisk_binding(
                        channel_id=bad_channel, connection_id=CONNECTION, port=8088,
                    )
        with self.assertRaisesRegex(ValueError, "invalid_asterisk_local_port"):
            validate_asterisk_binding(
                channel_id=CHANNEL, connection_id=CONNECTION, port=0,
            )

    def test_unarmed_worker_refuses_connection(self):
        with patch.dict(os.environ, {"PB_ASTERISK_MEDIA_ENABLED": "0"}), (
                patch.object(sys, "argv", ["porcelainberryme-asterisk"])):
            with self.assertRaises(SystemExit) as context:
                main()
        self.assertEqual(context.exception.code, 2)

    async def test_network_websocket_pcm_and_playback_acknowledgement(self):
        try:
            from aiohttp import web, WSMsgType
        except ImportError:
            self.skipTest("optional aiohttp is not installed")

        class STT:
            async def transcribe(self, pcm):
                return "Kannst du mich hören?"

        class LLM:
            async def respond(self, text, history):
                return "Ja, ich höre dich."

        class TTS:
            async def synthesize(self, text):
                return AudioPacket(FRAME, 16000)

        received = []
        connected = asyncio.Event()
        async def asterisk(request):
            self.assertEqual(request.match_info["connection"], CONNECTION)
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            connected.set()
            await ws.send_json({
                "event": "MEDIA_START", "channel_id": CHANNEL,
                "format": "slin16", "optimal_frame_size": 640, "ptime": 20,
            })
            for _ in range(12):
                await ws.send_bytes(FRAME)
            for _ in range(35):
                await ws.send_bytes(bytes(640))
            async for incoming in ws:
                if incoming.type == WSMsgType.TEXT:
                    payload = json.loads(incoming.data)
                    received.append(payload)
                    if payload["command"] == "MARK_MEDIA":
                        await ws.send_json({
                            "event": "MEDIA_MARK_PROCESSED",
                            "channel_id": CHANNEL,
                            "correlation_id": payload["correlation_id"],
                        })
                        await asyncio.sleep(0.15)
                        await ws.close()
                elif incoming.type == WSMsgType.BINARY:
                    received.append(bytes(incoming.data))
            return ws

        app = web.Application()
        app.router.add_get("/media/{connection}", asterisk)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            with patch.dict(os.environ, {
                "PB_ASTERISK_WS_USER": "", "PB_ASTERISK_WS_PASSWORD": "",
            }):
                await asyncio.wait_for(serve_asterisk_media(
                    channel_id=CHANNEL, connection_id=CONNECTION,
                    tenant="tenantA", agent_id="assistant",
                    whisper_model="", ollama_model="", piper_binary="",
                    piper_model="", port=port,
                    local_resources=(STT(), LLM(), TTS()),
                ), 5)
            self.assertTrue(connected.is_set())
            commands = [item["command"] for item in received
                        if isinstance(item, dict)]
            self.assertIn("MARK_MEDIA", commands)
            self.assertIn("FLUSH_MEDIA", commands)
            self.assertIn(FRAME, received)
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
