"""Real incoming human->AI Internet call through a self-hosted LiveKit server.

Runs on the GitHub runner, not on a customer's VPS. The actual control API,
inbound lease, room JWTs, LiveKitAgentCall, audio VAD, publish/subscribe,
cleanup and two WebRTC participants are real. ONLY local speech model
contents are faked; no paid services or PSTN are used.
"""
from __future__ import annotations

import asyncio
from http.server import ThreadingHTTPServer
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
from porcelainberryme.voice_inbound_agent import serve_inbound
from porcelainberryme.voice_livekit_agent import (
    CallControlClient, LiveKitAgentCall,
)
from porcelainberryme.media_cleanup import LiveKitRoomTerminator
from voice_agent_network_smoke import (
    FakeLocalSTT, FakeLocalLLM, FakeLocalTTS, remote_heard,
)


async def transmit_test_microphone(rtc, room):
    source = rtc.AudioSource(
        sample_rate=16000, num_channels=1, queue_size_ms=200,
    )
    try:
        track = rtc.LocalAudioTrack.create_audio_track(
            "incoming-human-microphone", source,
        )
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_MICROPHONE
        await room.local_participant.publish_track(track, options)
        # The AI subscribed asynchronously to this published track.
        await asyncio.sleep(0.35)
        for frame_number in range(110):
            values = (
                [int(6000 * math.sin(2 * math.pi * 440 *
                                     (frame_number * 320 + i) / 16000))
                 for i in range(320)]
                if frame_number < 70 else [0] * 320
            )
            await source.capture_frame(rtc.AudioFrame(
                data=struct.pack("<320h", *values),
                sample_rate=16000, num_channels=1,
                samples_per_channel=320,
            ))
        await source.wait_for_playout()
    finally:
        await source.aclose()


async def main():
    from livekit import rtc
    media_url = os.environ["LIVEKIT_URL"]
    key = os.environ["LIVEKIT_API_KEY"]
    media_secret = os.environ["LIVEKIT_API_SECRET"]
    identity_secret = os.environ["PB_CALL_IDENTITY_SECRET"]
    tenant, agent_id, customer_id = "incoming_ci", "reception", "customer"

    with tempfile.TemporaryDirectory(prefix="pb-real-inbound-") as folder:
        registry = CallRegistry(
            str(Path(folder) / "ip-calls.sqlite"),
            identity_secret=identity_secret,
            media_key=key, media_secret=media_secret,
            media_url=media_url,
        )
        registry.provision(tenant, agent_id)
        registry.provision(tenant, customer_id)
        registry.configure_inbound_agent(tenant, agent_id, True)
        now = int(time.time())
        customer_token = mint_identity_token(
            identity_secret, tenant, customer_id, "human", now=now,
        )
        customer_claims = verify_identity_token(
            customer_token, identity_secret, now=now,
        )
        server = ThreadingHTTPServer(
            ("127.0.0.1", 8874), make_handler(registry),
        )
        server_thread = threading.Thread(
            target=server.serve_forever, daemon=True,
        )
        server_thread.start()

        customer = CallControlClient(customer_token)
        stt, llm, tts = FakeLocalSTT(), FakeLocalLLM(), FakeLocalTTS()
        room = rtc.Room()
        observation = asyncio.get_running_loop().create_future()
        subscribed = asyncio.get_running_loop().create_future()
        listen_tasks = []
        worker_task = None
        call = None
        try:
            call = await customer.originate(
                agent_id, "verified-incoming-ci-0001",
            )
            if call["state"] != "RINGING" or call["agent_initiated"]:
                raise RuntimeError("customer_did_not_originate_incoming_call")
            expected_ai = LiveKitAgentCall.participant_identity(
                call["id"], tenant, agent_id,
            )
            @room.on("track_subscribed")
            def on_track(track, publication, participant):
                if (participant.identity == expected_ai
                        and track.kind == rtc.TrackKind.KIND_AUDIO):
                    if not subscribed.done():
                        subscribed.set_result(True)
                    listen_tasks.append(asyncio.create_task(
                        remote_heard(rtc, track, observation),
                    ))

            worker_task = asyncio.create_task(serve_inbound(
                tenant=tenant, agent_id=agent_id,
                identity_secret=identity_secret,
                whisper_model="/not-used-by-fake",
                ollama_model="/not-used-by-fake",
                piper_binary="/not-used-by-fake",
                piper_model="/not-used-by-fake",
                rtc=rtc, local_resources=(stt, llm, tts),
                max_sessions=1, poll_seconds=0.1,
            ))
            deadline = time.monotonic() + 8
            while registry.status(customer_claims, call["id"])["state"] == "RINGING":
                if time.monotonic() >= deadline:
                    raise RuntimeError("inbound_ai_never_accepted")
                await asyncio.sleep(0.08)
            media = registry.media_credentials(customer_claims, call["id"])
            await asyncio.wait_for(room.connect(
                media["url"], media["token"],
            ), timeout=8)
            await asyncio.wait_for(subscribed, timeout=8)
            await asyncio.wait_for(
                transmit_test_microphone(rtc, room), timeout=12,
            )
            await asyncio.wait_for(stt.called.wait(), timeout=8)
            await asyncio.wait_for(llm.called.wait(), timeout=8)
            nonzero = await asyncio.wait_for(observation, timeout=8)
            if len(tts.utterances) < 2:
                raise RuntimeError("inbound_ai_greeting_or_reply_missing")
            if not llm.questions:
                raise RuntimeError("inbound_ai_llm_not_used")
            await customer.hangup(call["id"])
            worker_count = await asyncio.wait_for(worker_task, timeout=8)
            if worker_count != 1:
                raise RuntimeError("inbound_worker_did_not_finalize")
            if registry.status(customer_claims, call["id"])["state"] != "ENDED":
                raise RuntimeError("inbound_call_was_not_terminated")
            print(json.dumps({
                "result": "REAL_INBOUND_AI_WEBRTC_CALL",
                "ai_auto_answered": True,
                "remote_nonzero_audio_frames": nonzero,
                "stt_calls": len(stt.transcripts),
                "llm_calls": len(llm.questions),
                "tts_calls": len(tts.utterances),
                "human_ended_call": True,
                "paid_api_used": False, "pstn_used": False,
            }))
        finally:
            if call is not None:
                try:
                    registry.decide(customer_claims, call["id"], "hangup")
                except Exception:
                    pass
            if worker_task is not None and not worker_task.done():
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
            await room.disconnect()
            for job in listen_tasks:
                job.cancel()
            if listen_tasks:
                await asyncio.gather(*listen_tasks, return_exceptions=True)
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
            if call is not None:
                terminator = LiveKitRoomTerminator(
                    media_url, key, media_secret,
                )
                deadline = time.monotonic() + 25
                cleaned = False
                while time.monotonic() < deadline:
                    result = registry.reconcile_media(terminator.delete_room)
                    if result["completed"]:
                        cleaned = True
                        break
                    await asyncio.sleep(2)
                if not cleaned:
                    raise RuntimeError("inbound_media_teardown_unconfirmed")


if __name__ == "__main__":
    asyncio.run(main())
