"""Real self-hosted RTC voice-agent acceptance with LOCAL FAKE speech modules.

Unlike Python unit mocks, both participants connect via the actual LiveKit
server and transport real PCM. Only the speech *content generators* are fakes:
STT/LLM/TTS APIs are exercised inside the actual VoiceTurnEngine and
LiveKitAgentCall. No model weights, paid APIs, microphone or PSTN carrier.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
import json
import math
import os
from pathlib import Path
import struct
import tempfile
import threading
import time

from porcelainberryme.ip_calls import (
    CallRegistry, make_handler, mint_identity_token,
    verify_identity_token,
)
from porcelainberryme.local_voice import AudioPacket
from porcelainberryme.voice_livekit_agent import CallControlClient, LiveKitAgentCall
from porcelainberryme.media_cleanup import LiveKitRoomTerminator
from http.server import ThreadingHTTPServer


class FakeLocalSTT:
    def __init__(self):
        self.called = asyncio.Event()
        self.transcripts = []

    async def transcribe(self, pcm: bytes) -> str:
        if len(pcm) < 16000:
            raise RuntimeError("speech_input_too_short")
        self.transcripts.append("Guten Tag. Ich habe eine Frage zum Service.")
        self.called.set()
        return self.transcripts[-1]


class FakeLocalLLM:
    def __init__(self):
        self.called = asyncio.Event()
        self.questions = []

    async def respond(self, text: str, history: list[dict]) -> str:
        self.questions.append(text)
        self.called.set()
        return "Ich habe Ihre Frage verstanden."


class FakeLocalTTS:
    sample_rate = 16000

    def __init__(self):
        self.utterances = []

    async def synthesize(self, text: str) -> AudioPacket:
        self.utterances.append(text)
        # 200ms ordinary speech-band PCM; the real LiveKit SDK encodes it
        # to Opus and transmits the resulting track to the human client.
        samples = [int(6000 * math.sin(2 * math.pi * 440 * i / self.sample_rate))
                   for i in range(self.sample_rate // 5)]
        return AudioPacket(struct.pack("<" + "h" * len(samples), *samples),
                           self.sample_rate)


async def feed_human_speech(rtc, room, agent_worker):
    source = rtc.AudioSource(sample_rate=16000, num_channels=1, queue_size_ms=200)
    try:
        track = rtc.LocalAudioTrack.create_audio_track("human-test-microphone", source)
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_MICROPHONE
        await room.local_participant.publish_track(track, options)
        # Wait for real agent microphone subscription before publishing.
        deadline = time.monotonic() + 6
        while agent_worker._stream_task is None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if agent_worker._stream_task is None:
            raise RuntimeError("agent_never_subscribed_to_human_audio")
        samples_per_frame = 320
        for index in range(110):
            # 70 speech frames + 40 silence to close the VAD turn.
            values = (
                [int(6000 * math.sin(2 * math.pi * 440 *
                                     (index * samples_per_frame + i) / 16000))
                 for i in range(samples_per_frame)]
                if index < 70 else [0] * samples_per_frame
            )
            frame = rtc.AudioFrame(
                data=struct.pack("<320h", *values),
                sample_rate=16000, num_channels=1,
                samples_per_channel=samples_per_frame,
            )
            await source.capture_frame(frame)
        await source.wait_for_playout()
    finally:
        await source.aclose()


async def remote_heard(rtc, track, observed: asyncio.Future):
    stream = rtc.AudioStream.from_track(
        track=track, sample_rate=16000, num_channels=1,
        frame_size_ms=20, capacity=100,
    )
    count = 0
    try:
        async for event in stream:
            if any(abs(sample) > 200
                   for (sample,) in struct.iter_unpack(
                       "<h", bytes(event.frame.data))):
                count += 1
            if count >= 5:
                if not observed.done():
                    observed.set_result(count)
                break
    except Exception as exc:
        if not observed.done():
            observed.set_exception(exc)
    finally:
        await stream.aclose()


async def main():
    try:
        from livekit import rtc
    except ImportError as exc:
        raise SystemExit("missing_open_source_livekit_rtc") from exc
    url = os.environ["LIVEKIT_URL"]
    api_key = os.environ["LIVEKIT_API_KEY"]
    secret = os.environ["LIVEKIT_API_SECRET"]
    identity_secret = os.environ["PB_CALL_IDENTITY_SECRET"]

    with tempfile.TemporaryDirectory(prefix="pb-real-agent-") as folder:
        registry = CallRegistry(
            str(Path(folder) / "calls.sqlite"),
            identity_secret=identity_secret,
            media_key=api_key, media_secret=secret, media_url=url,
        )
        registry.provision("smoke", "agent")
        registry.provision("smoke", "human", allow_agent_calls=True)
        now = int(time.time())
        agent_token = mint_identity_token(
            identity_secret, "smoke", "agent", "agent", now=now,
        )
        agent_claims = verify_identity_token(agent_token, identity_secret, now=now)
        human_claims = verify_identity_token(
            mint_identity_token(identity_secret, "smoke", "human", "human", now=now),
            identity_secret, now=now,
        )

        # Exercise the real HTTP control adapter, not a mocked client.
        http_server = ThreadingHTTPServer(
            ("127.0.0.1", 8874), make_handler(registry),
        )
        http_thread = threading.Thread(
            target=http_server.serve_forever, daemon=True,
        )
        http_thread.start()
        client = CallControlClient(agent_token)
        local_stt, local_llm, local_tts = (
            FakeLocalSTT(), FakeLocalLLM(), FakeLocalTTS(),
        )
        live_agent = LiveKitAgentCall(
            client=client, tenant="smoke", callee="human",
            stt=local_stt, llm=local_llm, tts=local_tts, rtc=rtc,
            greeting="Guten Tag vom lokalen Testassistenten.",
        )
        human = rtc.Room()
        observation = asyncio.get_running_loop().create_future()
        subscribed = asyncio.get_running_loop().create_future()
        receive_tasks = []
        agent_task = None
        call = None
        try:
            call = await client.originate("human", "smoke-agent-voice-0001")
            if call["state"] != "RINGING":
                raise RuntimeError("agent_control_did_not_ring")
            registry.decide(human_claims, call["id"], "accept")
            media = registry.media_credentials(human_claims, call["id"])
            expected_agent = registry.media_credentials(
                agent_claims, call["id"],
            )["participant"]

            @human.on("track_subscribed")
            def on_track(track, publication, participant):
                if (participant.identity == expected_agent
                        and track.kind == rtc.TrackKind.KIND_AUDIO):
                    if not subscribed.done():
                        subscribed.set_result(True)
                    receive_tasks.append(asyncio.create_task(
                        remote_heard(rtc, track, observation),
                    ))

            await asyncio.wait_for(human.connect(media["url"], media["token"]),
                                   timeout=8)
            agent_task = asyncio.create_task(live_agent.speak(call["id"]))
            await asyncio.wait_for(subscribed, timeout=6)
            await asyncio.wait_for(feed_human_speech(rtc, human, live_agent),
                                   timeout=12)
            await asyncio.wait_for(local_llm.called.wait(), timeout=8)
            await asyncio.wait_for(local_stt.called.wait(), timeout=8)
            frames = await asyncio.wait_for(observation, timeout=8)
            if len(local_tts.utterances) < 2:
                raise RuntimeError("agent_greeting_or_reply_not_synthesized")
            if not local_llm.questions:
                raise RuntimeError("agent_local_llm_not_invoked")
            print(json.dumps({
                "result": "REAL_LIVEKIT_VOICE_AGENT_TURN",
                "remote_nonzero_frames": frames,
                "stt_calls": len(local_stt.transcripts),
                "llm_calls": len(local_llm.questions),
                "tts_calls": len(local_tts.utterances),
                "paid_api_used": False, "pstn_used": False,
            }))
        finally:
            if call is not None:
                try:
                    registry.decide(agent_claims, call["id"], "hangup")
                except Exception:
                    pass
            if agent_task is not None:
                try:
                    await asyncio.wait_for(agent_task, timeout=4)
                except (TimeoutError, Exception):
                    agent_task.cancel()
                    await asyncio.gather(agent_task, return_exceptions=True)
            await human.disconnect()
            for task in receive_tasks:
                task.cancel()
            if receive_tasks:
                await asyncio.gather(*receive_tasks, return_exceptions=True)
            http_server.shutdown()
            http_server.server_close()
            http_thread.join(timeout=2)
            if call is not None:
                terminator = LiveKitRoomTerminator(url, api_key, secret)
                deadline = time.monotonic() + 25
                cleaned = False
                while time.monotonic() < deadline:
                    result = registry.reconcile_media(terminator.delete_room)
                    if result["completed"]:
                        cleaned = True
                        break
                    await asyncio.sleep(2)
                if not cleaned:
                    raise RuntimeError("agent_media_teardown_unconfirmed")


if __name__ == "__main__":
    asyncio.run(main())
