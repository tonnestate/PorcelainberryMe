"""LiveKit WebRTC voice agent: initiate an accepted Internet call and speak.

Optional runtime. No carrier account, no paid model API, no AnanasMe owner
harness. All model files/services must already exist on the host.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import http.client
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

from .local_voice import (
    AudioPacket, LocalFasterWhisper, LocalOllamaResponder,
    LocalPiperVoice, LocalVoiceUnavailable, VoiceTurnEngine,
)


class CallControlClient:
    """Authenticate to PorcelainberryMe's loopback-only headless API."""

    def __init__(self, token: str, *, timeout: float = 5):
        if not isinstance(token, str) or not token:
            raise ValueError("agent_control_token_required")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, payload=None):
        conn = http.client.HTTPConnection("127.0.0.1", 8874, timeout=self.timeout)
        try:
            body = None if payload is None else json.dumps(payload).encode()
            headers = {"Authorization": "Bearer " + self.token}
            if body is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read(65537)
            if len(raw) > 65536:
                raise RuntimeError("call_control_response_too_large")
            data = json.loads(raw)
            if response.status not in (200, 201):
                raise RuntimeError("call_control_" + str(data.get("error", "failed")))
            return data
        finally:
            conn.close()

    async def request(self, method: str, path: str, payload=None):
        return await asyncio.to_thread(self._request, method, path, payload)

    async def whoami(self):
        return await self.request("GET", "/v1/ip-calls/whoami")

    async def originate(self, target: str, request_id: str):
        return await self.request("POST", "/v1/ip-calls", {
            "to": target, "idempotency_key": request_id,
        })

    async def status(self, call_id: str):
        return await self.request("GET", "/v1/ip-calls/" + call_id)

    async def media_token(self, call_id: str):
        return await self.request("POST", "/v1/ip-calls/" + call_id + "/media-token")

    async def hangup(self, call_id: str):
        return await self.request("POST", "/v1/ip-calls/" + call_id + "/hangup")


class LiveKitPcmSpeaker:
    """Output real PCM frames through a LiveKit rtc.AudioSource."""

    def __init__(self, source, rtc, *, sample_rate: int):
        self.source = source
        self.rtc = rtc
        self.sample_rate = sample_rate
        self._revision = 0

    def interrupt(self):
        self._revision += 1
        self.source.clear_queue()

    async def play(self, packet: AudioPacket) -> bool:
        if packet.sample_rate != self.sample_rate:
            raise LocalVoiceUnavailable("tts_publisher_sample_rate_mismatch")
        revision = self._revision
        # 20ms PCM16 frames, naturally paced by AudioSource's bounded queue.
        frame_samples = self.sample_rate // 50
        frame_bytes = frame_samples * 2
        for offset in range(0, len(packet.pcm), frame_bytes):
            if revision != self._revision:
                return False
            chunk = packet.pcm[offset:offset + frame_bytes]
            if len(chunk) < frame_bytes:
                chunk += b"\x00" * (frame_bytes - len(chunk))
            frame = self.rtc.AudioFrame(
                data=chunk, sample_rate=self.sample_rate,
                num_channels=1, samples_per_channel=frame_samples,
            )
            await self.source.capture_frame(frame)
        await self.source.wait_for_playout()
        return revision == self._revision


class LiveKitAgentCall:
    """One-to-one voice assistant for an already accepted Internet call."""

    def __init__(self, *, client: CallControlClient, tenant: str,
                 callee: str, tts, stt, llm, rtc):
        self.client = client
        self.tenant = tenant
        self.callee = callee
        self.tts, self.stt, self.llm, self.rtc = tts, stt, llm, rtc
        self.room = None
        self._stream_task = None
        self._closed = asyncio.Event()
        self._engine = None
        self._source = None
        self._expected_participant = None

    @staticmethod
    def participant_identity(call_id, tenant, user):
        return "ip_" + hashlib.sha256(
            (call_id + ":" + tenant + ":" + user).encode()
        ).hexdigest()[:24]

    async def _consume_audio(self, track):
        # SDK performs real audio resampling to 16k mono. This is not a
        # mocked speech transcript: the frames come from the remote track.
        stream = self.rtc.AudioStream.from_track(
            track=track, sample_rate=16000, num_channels=1,
            frame_size_ms=20, capacity=100,
        )
        try:
            async for event in stream:
                frame = event.frame
                if frame.sample_rate != 16000 or frame.num_channels != 1:
                    raise LocalVoiceUnavailable("unexpected_inbound_audio_format")
                raw = bytes(frame.data.cast("B"))
                if len(raw) != 640:
                    # 20ms framing is part of the turn detector contract.
                    raise LocalVoiceUnavailable("unexpected_inbound_frame_size")
                self._engine.receive(raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Audio ingestion failure must not leave the other party
            # listening indefinitely to an otherwise silent agent.
            self._closed.set()
            raise
        finally:
            await stream.aclose()

    async def _watch_call(self, call_id):
        while not self._closed.is_set():
            status = await self.client.status(call_id)
            if status["state"] != "ACTIVE":
                self._closed.set()
                return
            await asyncio.sleep(1.0)

    async def speak(self, call_id):
        media = await self.client.media_token(call_id)
        self._expected_participant = self.participant_identity(
            call_id, self.tenant, self.callee,
        )
        room = self.rtc.Room()
        self.room = room
        self._source = self.rtc.AudioSource(
            sample_rate=self.tts.sample_rate, num_channels=1,
            queue_size_ms=250,
        )
        speaker = LiveKitPcmSpeaker(
            self._source, self.rtc, sample_rate=self.tts.sample_rate,
        )
        self._engine = VoiceTurnEngine(self.stt, self.llm, self.tts, speaker)

        @room.on("track_subscribed")
        def track_subscribed(track, publication, participant):
            if (track.kind == self.rtc.TrackKind.KIND_AUDIO
                    and participant.identity == self._expected_participant
                    and self._stream_task is None):
                self._stream_task = asyncio.create_task(self._consume_audio(track))

        @room.on("disconnected")
        def room_disconnected(*args):
            self._closed.set()

        try:
            await room.connect(
                media["url"], media["token"],
                options=self.rtc.RoomOptions(auto_subscribe=True),
            )
            track = self.rtc.LocalAudioTrack.create_audio_track(
                "agent-microphone", self._source,
            )
            options = self.rtc.TrackPublishOptions()
            options.source = self.rtc.TrackSource.SOURCE_MICROPHONE
            await room.local_participant.publish_track(track, options)
            watcher = asyncio.create_task(self._watch_call(call_id))
            waiter = asyncio.create_task(self._closed.wait())
            try:
                done, pending = await asyncio.wait(
                    [watcher, waiter], return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    task.result()
            finally:
                for task in (watcher, waiter):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(watcher, waiter, return_exceptions=True)
        finally:
            self._closed.set()
            if self._stream_task is not None:
                self._stream_task.cancel()
                await asyncio.gather(self._stream_task, return_exceptions=True)
            await self._engine.close()
            await room.disconnect()
            await self._source.aclose()


async def call_human(*, target: str, tenant: str, control_token: str,
                     whisper_model: str, ollama_model: str,
                     piper_binary: str, piper_model: str,
                     agent_id: str, request_id: str):
    """Call a registered, opted-in Internet user, never an E.164 number."""
    if not target or target.startswith("+") or not tenant or not agent_id:
        raise ValueError("internet_identity_required_not_public_phone_number")
    if not request_id or len(request_id) < 8:
        raise ValueError("stable_request_id_required")
    from livekit import rtc  # optional installed open-source Python SDK

    # Fail before dialing if local speech components are absent.
    stt = await asyncio.to_thread(LocalFasterWhisper, whisper_model)
    approved_db = os.environ.get("PB_AGENT_KNOWLEDGE_DB", "").strip()
    if approved_db:
        # Trusted per-agent corporate answers override general-purpose LLM.
        # Unapproved or unknown company facts never reach the local LLM.
        from .agent_knowledge import ApprovedKnowledgeResponder

        llm = ApprovedKnowledgeResponder(approved_db, tenant, agent_id)
    else:
        llm = LocalOllamaResponder(ollama_model)
    tts = LocalPiperVoice(piper_binary, piper_model)
    client = CallControlClient(control_token)
    identity = await client.whoami()
    if (identity.get("role") != "agent"
            or identity.get("tenant") != tenant
            or identity.get("subject") != agent_id):
        raise PermissionError("verified_voice_agent_identity_mismatch")
    call = await client.originate(target, request_id)
    call_id = call["id"]
    if not call["agent_initiated"]:
        raise RuntimeError("non_agent_identity_cannot_originate_agent_call")
    try:
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            status = await client.status(call_id)
            if status["state"] == "ACTIVE":
                await LiveKitAgentCall(
                    client=client, tenant=tenant, callee=target,
                    tts=tts, stt=stt, llm=llm, rtc=rtc,
                ).speak(call_id)
                return
            if status["state"] != "RINGING":
                return
            await asyncio.sleep(0.5)
    finally:
        # A caller may already have ended; repeated hangup is not silently
        # retried by this worker, and cleanup stays in PorcelainberryMe.
        try:
            status = await client.status(call_id)
            if status["state"] in ("RINGING", "ACTIVE"):
                await client.hangup(call_id)
        except Exception:
            # Caller cannot claim a successful network hangup here.
            pass


def main():
    parser = argparse.ArgumentParser(description="Self-hosted Internet voice agent")
    parser.add_argument("--to", required=True, help="Enrolled Internet subject ID, not phone number")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--request-id", required=True,
                        help="Stable ID from the authorized originating job")
    args = parser.parse_args()
    settings = {
        "control_token": os.environ.get("PB_CALL_AGENT_TOKEN", ""),
        "whisper_model": os.environ.get("PB_LOCAL_WHISPER_DIR", ""),
        "ollama_model": os.environ.get("PB_LOCAL_OLLAMA_MODEL", ""),
        "piper_binary": os.environ.get("PB_LOCAL_PIPER_BIN", ""),
        "piper_model": os.environ.get("PB_LOCAL_PIPER_MODEL", ""),
    }
    required = ("control_token", "whisper_model", "piper_binary", "piper_model")
    if any(not settings[name] for name in required):
        parser.error("local speech files and agent identity token are required")
    if not settings["ollama_model"] and not os.environ.get("PB_AGENT_KNOWLEDGE_DB"):
        parser.error("local Ollama model or approved agent knowledge DB is required")
    asyncio.run(call_human(
        target=args.to, tenant=args.tenant,
        agent_id=args.agent, request_id=args.request_id, **settings,
    ))


if __name__ == "__main__":
    main()
