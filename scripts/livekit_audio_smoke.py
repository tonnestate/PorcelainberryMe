"""Manual, cost-free LiveKit *network* smoke test for PorcelainberryMe.

Runs against an ALREADY STARTED, self-hosted LiveKit server. Creates a real
authorized IP call, joins two different RTC participants, publishes 16 kHz
synthetic PCM and waits for actual received PCM over WebRTC. Never downloads
models, requests paid services, or calls the public telephone network.

This is not a speech/LLM test. No human audio or contact data is recorded.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import struct
import tempfile
import time

from porcelainberryme.ip_calls import CallRegistry, mint_identity_token, verify_identity_token
from porcelainberryme.media_cleanup import LiveKitRoomTerminator


def env_required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"Missing {name}; use an already running, self-hosted LiveKit instance.")
    return value


async def wait_for_pcm(rtc, track, received: asyncio.Future, sample_rate: int):
    stream = rtc.AudioStream.from_track(
        track=track, sample_rate=sample_rate, num_channels=1,
        frame_size_ms=20, capacity=100,
    )
    nonzero_frames = 0
    try:
        async for event in stream:
            data = bytes(event.frame.data)
            if len(data) < 2:
                continue
            if any(sample > 200 or sample < -200
                   for (sample,) in struct.iter_unpack("<h", data)):
                nonzero_frames += 1
            if nonzero_frames >= 5:
                if not received.done():
                    received.set_result(nonzero_frames)
                break
    except Exception as exc:
        if not received.done():
            received.set_exception(exc)
    finally:
        await stream.aclose()


async def publish_probe(rtc, room, *, rate: int = 16000):
    source = rtc.AudioSource(sample_rate=rate, num_channels=1, queue_size_ms=200)
    try:
        track = rtc.LocalAudioTrack.create_audio_track("probe", source)
        opts = rtc.TrackPublishOptions()
        opts.source = rtc.TrackSource.SOURCE_MICROPHONE
        await room.local_participant.publish_track(track, opts)
        # 100 x 20 ms frames of non-zero PCM, no private microphone.
        samples = rate // 50
        sine = struct.pack("<" + "h" * samples, *(
            3000 if i % 2 else -3000 for i in range(samples)
        ))
        for _ in range(100):
            frame = rtc.AudioFrame(
                data=sine, sample_rate=rate,
                num_channels=1, samples_per_channel=samples,
            )
            await source.capture_frame(frame)
        await source.wait_for_playout()
    finally:
        await source.aclose()


async def main():
    media_url = env_required("LIVEKIT_URL")
    media_key = env_required("LIVEKIT_API_KEY")
    media_secret = env_required("LIVEKIT_API_SECRET")
    identity_secret = os.environ.get("PB_CALL_IDENTITY_SECRET") or (
        "manual-smoke-local-identity-secret-not-for-production"
    )
    if len(media_secret) < 32 or len(identity_secret) < 32:
        raise SystemExit("The distinct LiveKit and identity secrets must each have >=32 characters.")
    if media_secret == identity_secret:
        raise SystemExit("LiveKit media and user identity signing secrets must differ.")
    if not media_url.startswith(("ws://127.0.0.1:", "ws://localhost:", "wss://")):
        raise SystemExit("Use local self-hosted ws:// or TLS-secured wss:// LiveKit only.")

    try:
        from livekit import rtc
    except ImportError as exc:
        raise SystemExit("Install the optional open-source voice-local extra first.") from exc

    # Only a temporary SQLite database: no changes to the real tenant ledger.
    with tempfile.TemporaryDirectory(prefix="pb-ip-audio-smoke-") as directory:
        registry = CallRegistry(
            str(Path(directory) / "calls.sqlite"),
            identity_secret=identity_secret, media_key=media_key,
            media_secret=media_secret, media_url=media_url,
        )
        tenant, agent, human = "smoke_only", "smoke_agent", "smoke_human"
        registry.provision(tenant, agent)
        registry.provision(tenant, human, allow_agent_calls=True)
        now = int(time.time())
        agent_claims = verify_identity_token(
            mint_identity_token(identity_secret, tenant, agent, "agent", now=now),
            identity_secret, now=now,
        )
        human_claims = verify_identity_token(
            mint_identity_token(identity_secret, tenant, human, "human", now=now),
            identity_secret, now=now,
        )
        call = registry.originate(agent_claims, human, "smoke-call-request-0001")
        if call["state"] != "RINGING":
            raise RuntimeError("unexpected_call_state")
        events = registry.inbox(human_claims)
        if not events["events"] or events["events"][-1]["kind"] != "RINGING":
            raise RuntimeError("callee_did_not_receive_ring_event")
        registry.decide(human_claims, call["id"], "accept")
        agent_media = registry.media_credentials(agent_claims, call["id"])
        human_media = registry.media_credentials(human_claims, call["id"])
        if agent_media["participant"] == human_media["participant"]:
            raise RuntimeError("media_identity_collision")

        publisher, subscriber = rtc.Room(), rtc.Room()
        tasks = []
        received = asyncio.get_running_loop().create_future()

        @subscriber.on("track_subscribed")
        def on_track(track, publication, participant):
            if participant.identity == agent_media["participant"] and (
                track.kind == rtc.TrackKind.KIND_AUDIO
            ):
                tasks.append(asyncio.create_task(wait_for_pcm(
                    rtc, track, received, sample_rate=16000,
                )))

        try:
            await asyncio.wait_for(
                subscriber.connect(human_media["url"], human_media["token"]),
                timeout=8,
            )
            await asyncio.wait_for(
                publisher.connect(agent_media["url"], agent_media["token"]),
                timeout=8,
            )
            # Allow remote subscriber to bind before sending nonzero PCM.
            await asyncio.sleep(0.25)
            await asyncio.wait_for(publish_probe(rtc, publisher), timeout=12)
            frames = await asyncio.wait_for(received, timeout=12)
            print(json.dumps({
                "result": "REAL_WEBRTC_PCM_RECEIVED",
                "received_nonzero_frames": frames,
                "network_transport": "self_hosted_livekit",
                "pstn_used": False, "paid_api_used": False,
            }))
        finally:
            registry.decide(agent_claims, call["id"], "hangup")
            await publisher.disconnect()
            await subscriber.disconnect()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            terminator = LiveKitRoomTerminator(
                media_url, media_key, media_secret,
            )
            # Repeat cleanup until the last short-lived join token is
            # expired, so a stale token cannot re-create the deleted room.
            done = False
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                result = registry.reconcile_media(terminator.delete_room)
                if result["completed"] == 1:
                    done = True
                    break
                await asyncio.sleep(2.0)
            if not done:
                raise RuntimeError("media_teardown_not_confirmed")


if __name__ == "__main__":
    asyncio.run(main())
