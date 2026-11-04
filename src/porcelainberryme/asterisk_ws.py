"""Asterisk chan_websocket JSON/slin16 ↔ existing local VoiceTurnEngine.

This is an authenticated-host media adapter, not a SIP/PSTN dialer or ARI
controller. A trusted Asterisk/ARI owner must establish and authorize the
WebSocket channel first; caller-ID and MEDIA_START text are not credentials.
Requires chan_websocket JSON control mode (Asterisk 20.18+/22.8+/23.2+).
"""
from __future__ import annotations

import asyncio
import json
import re
from uuid import uuid4

from .local_voice import AudioPacket, FRAME_BYTES, VoiceTurnEngine

_CHANNEL = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_ALLOWED_EVENTS = {
    "MEDIA_START", "MEDIA_XOFF", "MEDIA_XON", "MEDIA_MARK_PROCESSED",
    "MEDIA_BUFFERING_COMPLETED", "QUEUE_DRAINED", "DTMF_END", "STATUS",
}
MAX_WS_MESSAGE_BYTES = 65500


class AsteriskMediaError(RuntimeError):
    pass


class AsteriskPcmSpeaker:
    """Playback proof requires Asterisk MARK_MEDIA_PROCESSED.

    AUDIO accepted by a media WebSocket is not proof that the caller heard it.
    Interrupts flush Asterisk's pending queue, not just local generation.
    """

    def __init__(self, ws, *, mark_timeout: float = 45):
        if not 1 <= mark_timeout <= 120:
            raise ValueError("invalid_asterisk_mark_timeout")
        self.ws = ws
        self.mark_timeout = mark_timeout
        self.ready = False
        self._closed = False
        self._revision = 0
        self._xoff = asyncio.Event()
        self._xoff.set()
        self._send_lock = asyncio.Lock()
        self._marks = {}
        self._flush_tasks = set()

    async def _text(self, command, **params):
        async with self._send_lock:
            await self.ws.send_json({"command": command, **params})

    async def _flush(self):
        if self.ready and not self._closed:
            await self._text("FLUSH_MEDIA")

    def interrupt(self):
        self._revision += 1
        self._xoff.set()
        for promise in tuple(self._marks.values()):
            if not promise.done():
                promise.set_result(False)
        if self.ready and not self._closed:
            task = asyncio.create_task(self._flush())
            self._flush_tasks.add(task)
            task.add_done_callback(self._flush_tasks.discard)

    async def play(self, packet: AudioPacket) -> bool:
        if not self.ready or self._closed:
            raise AsteriskMediaError("asterisk_media_not_started")
        if packet.sample_rate != 16000 or len(packet.pcm) % FRAME_BYTES:
            raise AsteriskMediaError("asterisk_pcm16_20ms_alignment_required")
        current = self._revision
        marker = uuid4().hex
        promise = asyncio.get_running_loop().create_future()
        self._marks[marker] = promise
        try:
            await self._text("START_MEDIA_BUFFERING")
            # Asterisk retimes uncompressed slin16; no RTP, no frame headers.
            # Bounded frame-aligned chunks well under its 65500-byte limit.
            for offset in range(0, len(packet.pcm), FRAME_BYTES * 20):
                await asyncio.wait_for(self._xoff.wait(), timeout=5)
                if current != self._revision or self._closed:
                    return False
                async with self._send_lock:
                    await self.ws.send_bytes(packet.pcm[offset:offset + FRAME_BYTES * 20])
            if current != self._revision or self._closed:
                return False
            await self._text("STOP_MEDIA_BUFFERING")
            await self._text("MARK_MEDIA", correlation_id=marker)
            verified = await asyncio.wait_for(promise, timeout=self.mark_timeout)
            return bool(verified and current == self._revision and not self._closed)
        except asyncio.TimeoutError as exc:
            raise AsteriskMediaError("asterisk_media_delivery_unverified") from exc
        finally:
            self._marks.pop(marker, None)

    async def aclose(self):
        self._closed = True
        self.interrupt()
        tasks = tuple(self._flush_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def event(self, name: str, data: dict):
        if name == "MEDIA_XOFF":
            self._xoff.clear()
        elif name == "MEDIA_XON":
            self._xoff.set()
        elif name == "MEDIA_MARK_PROCESSED":
            marker = data.get("correlation_id")
            promise = self._marks.get(marker) if isinstance(marker, str) else None
            if promise is not None and not promise.done():
                promise.set_result(True)


class AsteriskVoiceMedia:
    """Consume exactly one pre-authorized Asterisk WebSocket media channel.

    The expected channel ID MUST originate from the trusted host's ARI
    channel, never from a call's Caller-ID or an MCP/model argument.
    """

    def __init__(self, ws, *, expected_channel_id: str,
                 stt, llm, tts, max_seconds: int = 3600):
        if not isinstance(expected_channel_id, str) or not _CHANNEL.fullmatch(expected_channel_id):
            raise ValueError("trusted_asterisk_channel_required")
        if type(max_seconds) is not int or not 1 <= max_seconds <= 3600:
            raise ValueError("invalid_asterisk_call_limit")
        self.ws = ws
        self.expected_channel_id = expected_channel_id
        self.max_seconds = max_seconds
        self.speaker = AsteriskPcmSpeaker(ws)
        self.engine = VoiceTurnEngine(stt, llm, tts, self.speaker)
        self.started = False
        self._incoming = bytearray()

    def _text(self, text: str):
        if not isinstance(text, str) or len(text) > 16384:
            raise AsteriskMediaError("asterisk_invalid_control_event")
        try:
            event = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise AsteriskMediaError("asterisk_invalid_control_event") from exc
        if not isinstance(event, dict):
            raise AsteriskMediaError("asterisk_invalid_control_event")
        name = event.get("event")
        if name not in _ALLOWED_EVENTS:
            raise AsteriskMediaError("asterisk_unexpected_control_event")
        if event.get("channel_id") not in (None, self.expected_channel_id):
            raise AsteriskMediaError("asterisk_channel_identity_mismatch")
        if name == "MEDIA_START":
            if self.started:
                raise AsteriskMediaError("asterisk_duplicate_media_start")
            if (event.get("channel_id") != self.expected_channel_id
                    or event.get("format") != "slin16"
                    or type(event.get("optimal_frame_size")) is not int
                    or event["optimal_frame_size"] != FRAME_BYTES
                    or event.get("ptime") != 20):
                raise AsteriskMediaError("asterisk_pcm_media_contract_mismatch")
            self.started = True
            self.speaker.ready = True
        elif not self.started:
            raise AsteriskMediaError("asterisk_media_not_started")
        else:
            self.speaker.event(name, event)

    def _binary(self, pcm: bytes):
        if not self.started:
            raise AsteriskMediaError("asterisk_media_not_started")
        if (not isinstance(pcm, bytes) or not pcm
                or len(pcm) > MAX_WS_MESSAGE_BYTES or len(pcm) % 2):
            raise AsteriskMediaError("asterisk_invalid_pcm_frame")
        self._incoming.extend(pcm)
        while len(self._incoming) >= FRAME_BYTES:
            chunk = bytes(self._incoming[:FRAME_BYTES])
            del self._incoming[:FRAME_BYTES]
            self.engine.receive(chunk)

    async def run(self) -> None:
        """An existing trusted host supplies aiohttp WebSocketClientResponse.

        Never listens publicly or auto-answers a SIP call. The host must
        attach to the precreated ARI externalMedia websocket and arrange
        provider/trunk authorization outside this component.
        """
        try:
            async with asyncio.timeout(self.max_seconds):
                async for message in self.ws:
                    kind = getattr(getattr(message, "type", None), "name", "")
                    if kind == "TEXT":
                        self._text(message.data)
                    elif kind == "BINARY":
                        self._binary(message.data)
                    elif kind in ("CLOSE", "CLOSED", "CLOSING", "ERROR"):
                        break
                    else:
                        raise AsteriskMediaError("asterisk_invalid_websocket_message")
            if not self.started:
                raise AsteriskMediaError("asterisk_no_media_start")
        finally:
            await self.engine.close()
            await self.speaker.aclose()


__all__ = ["AsteriskMediaError", "AsteriskPcmSpeaker", "AsteriskVoiceMedia"]
