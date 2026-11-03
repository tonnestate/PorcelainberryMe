"""Actual three-party, provider-free warm handoff against self-hosted LiveKit.

Exercises a live AI RTC session that only leaves after the verified staff
microphone joins the original room. Staff and customer can then exchange
actual PCM without the AI. There is no cloud speech, phone number or PSTN.
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
    CallRegistry, mint_identity_token, verify_identity_token,
    make_handler,
)
from porcelainberryme.local_voice import AudioPacket
from porcelainberryme.media_cleanup import LiveKitRoomTerminator
from porcelainberryme.voice_livekit_agent import CallControlClient, LiveKitAgentCall


class LocalSTT:
    async def transcribe(self, pcm):
        return ""


class LocalLLM:
    async def respond(self, text, history):
        return "Die Mitarbeitenden übernehmen dieses Gespräch."


class LocalTTS:
    sample_rate = 16000

    async def synthesize(self, text):
        samples = [
            int(6000 * math.sin(2 * math.pi * 440 * i / 16000))
            for i in range(16000 // 5)
        ]
        return AudioPacket(struct.pack("<" + "h" * len(samples), *samples),
                           self.sample_rate)


async def capture_staff_pcm(rtc, track, received):
    audio = rtc.AudioStream.from_track(
        track=track, sample_rate=16000, num_channels=1,
        frame_size_ms=20, capacity=100,
    )
    found = 0
    try:
        async for sample in audio:
            raw = bytes(sample.frame.data)
            if any(abs(value) > 200 for (value,) in struct.iter_unpack("<h", raw)):
                found += 1
            if found >= 5:
                if not received.done():
                    received.set_result(found)
                return
    except Exception as exc:
        if not received.done():
            received.set_exception(exc)
    finally:
        await audio.aclose()


async def publish_staff_audio(rtc, room):
    source = rtc.AudioSource(sample_rate=16000, num_channels=1, queue_size_ms=200)
    try:
        track = rtc.LocalAudioTrack.create_audio_track(
            "staff-microphone", source,
        )
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_MICROPHONE
        await room.local_participant.publish_track(track, options)
        for frame_no in range(100):
            pcm = struct.pack(
                "<320h", *(
                    int(6000 * math.sin(
                        2 * math.pi * 440 * (frame_no * 320 + i) / 16000,
                    ))
                    for i in range(320)
                ),
            )
            await source.capture_frame(rtc.AudioFrame(
                data=pcm, sample_rate=16000, num_channels=1,
                samples_per_channel=320,
            ))
        await source.wait_for_playout()
    finally:
        await source.aclose()


async def main():
    from livekit import rtc

    identity_secret = os.environ["PB_CALL_IDENTITY_SECRET"]
    url = os.environ["LIVEKIT_URL"]
    key = os.environ["LIVEKIT_API_KEY"]
    secret = os.environ["LIVEKIT_API_SECRET"]

    with tempfile.TemporaryDirectory(prefix="pb-handoff-") as folder:
        registry = CallRegistry(
            str(Path(folder) / "call.sqlite"),
            identity_secret=identity_secret,
            media_key=key, media_secret=secret, media_url=url,
        )
        tenant, agent_id, customer, staff_id = (
            "handoff_ci", "ai_assistant", "customer", "staff",
        )
        for identity in (agent_id, customer, staff_id):
            registry.provision(
                tenant, identity, allow_agent_calls=identity == customer,
            )
        registry.configure_handoff_route(tenant, agent_id, staff_id)
        now = int(time.time())
        agent_token = mint_identity_token(
            identity_secret, tenant, agent_id, "agent", now=now,
        )
        agent_claims = verify_identity_token(agent_token, identity_secret, now=now)
        customer_claims = verify_identity_token(
            mint_identity_token(identity_secret, tenant, customer, "human", now=now),
            identity_secret, now=now,
        )
        staff_claims = verify_identity_token(
            mint_identity_token(identity_secret, tenant, staff_id, "human", now=now),
            identity_secret, now=now,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 8874), make_handler(registry))
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        client = CallControlClient(agent_token)
        customer_room, staff_room = rtc.Room(), rtc.Room()
        tasks = []
        agent_task = None
        call = None
        heard = asyncio.get_running_loop().create_future()
        staff_subscribed = asyncio.get_running_loop().create_future()
        try:
            call = await client.originate(customer, "handoff-ci-call-0001")
            assert call["state"] == "RINGING"
            registry.decide(customer_claims, call["id"], "accept")
            customer_media = registry.media_credentials(
                customer_claims, call["id"],
            )
            @customer_room.on("track_subscribed")
            def on_track(track, publication, participant):
                if (participant.identity == staff_participant[0]
                        and track.kind == rtc.TrackKind.KIND_AUDIO):
                    if not staff_subscribed.done():
                        staff_subscribed.set_result(True)
                    tasks.append(asyncio.create_task(
                        capture_staff_pcm(rtc, track, heard),
                    ))

            staff_participant = [None]
            await asyncio.wait_for(customer_room.connect(
                customer_media["url"], customer_media["token"],
            ), timeout=8)
            worker = LiveKitAgentCall(
                client=client, tenant=tenant, callee=customer,
                stt=LocalSTT(), llm=LocalLLM(), tts=LocalTTS(), rtc=rtc,
            )
            agent_task = asyncio.create_task(worker.speak(call["id"]))
            deadline = time.monotonic() + 6
            while (worker.room is None or
                   worker.room.connection_state != rtc.ConnectionState.CONN_CONNECTED):
                if time.monotonic() >= deadline:
                    raise RuntimeError("agent_room_did_not_connect")
                await asyncio.sleep(0.05)

            offered = await client.offer_handoff(
                call["id"], "handoff-ci-offer-0001",
            )
            assert offered["state"] == "OFFERED"
            assert registry.decide_handoff(
                staff_claims, call["id"], "accept",
            )["state"] == "ACCEPTED"
            staff_media = registry.media_credentials(staff_claims, call["id"])
            staff_participant[0] = staff_media["participant"]
            assert staff_media["room"] == customer_media["room"]
            await asyncio.wait_for(staff_room.connect(
                staff_media["url"], staff_media["token"],
            ), timeout=8)

            publish = asyncio.create_task(publish_staff_audio(rtc, staff_room))
            await asyncio.wait_for(staff_subscribed, timeout=8)
            await asyncio.wait_for(publish, timeout=12)
            nonzero = await asyncio.wait_for(heard, timeout=6)
            await asyncio.wait_for(agent_task, timeout=5)
            handoff = registry.status(customer_claims, call["id"])["handoff"]
            if handoff["state"] != "CONNECTED":
                raise RuntimeError("rtc_staff_handoff_not_confirmed")
            if registry.status(customer_claims, call["id"])["state"] != "ACTIVE":
                raise RuntimeError("customer_was_hung_up_during_handoff")
            if customer_room.connection_state != rtc.ConnectionState.CONN_CONNECTED:
                raise RuntimeError("customer_media_lost_on_ai_exit")
            if staff_room.connection_state != rtc.ConnectionState.CONN_CONNECTED:
                raise RuntimeError("staff_media_lost_on_ai_exit")
            print(json.dumps({
                "result": "REAL_HUMAN_HANDOFF_SAME_ROOM",
                "staff_audio_received": nonzero,
                "agent_exited_without_hangup": True,
                "customer_call_remains_active": True,
                "paid_api_used": False, "pstn_used": False,
            }))
        finally:
            if call is not None:
                try:
                    registry.decide(staff_claims, call["id"], "hangup")
                except Exception:
                    pass
            if agent_task is not None and not agent_task.done():
                agent_task.cancel()
                await asyncio.gather(agent_task, return_exceptions=True)
            await staff_room.disconnect()
            await customer_room.disconnect()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
            if call is not None:
                terminator = LiveKitRoomTerminator(url, key, secret)
                deadline = time.monotonic() + 25
                cleared = False
                while time.monotonic() < deadline:
                    result = registry.reconcile_media(terminator.delete_room)
                    if result["completed"] == 1:
                        cleared = True
                        break
                    await asyncio.sleep(2)
                if not cleared:
                    raise RuntimeError("handoff_room_teardown_unconfirmed")


if __name__ == "__main__":
    asyncio.run(main())
