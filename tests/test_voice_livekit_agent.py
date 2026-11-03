"""Offline LiveKit-voice adapter contracts.

The transport is exercised with a simulated WebRTC SDK; these tests do NOT
demonstrate latency or media connectivity on the actual VPS.
"""
import asyncio
import struct
from types import SimpleNamespace
import unittest

from porcelainberryme.local_voice import AudioPacket
from porcelainberryme.voice_livekit_agent import (
    LiveKitAgentCall, LiveKitPcmSpeaker,
)


def pcm(value=1200):
    return struct.pack("<320h", *([value] * 320))


class FakeAudioSource:
    def __init__(self, *args, **kwargs):
        self.frames = []
        self.clears = 0
        self.closed = False

    async def capture_frame(self, frame):
        self.frames.append(frame)

    async def wait_for_playout(self):
        await asyncio.sleep(0)

    def clear_queue(self):
        self.clears += 1

    async def aclose(self):
        self.closed = True


class FakeRTC:
    TrackKind = SimpleNamespace(KIND_AUDIO=1)
    TrackSource = SimpleNamespace(SOURCE_MICROPHONE=2)
    AudioFrame = SimpleNamespace
    AudioSource = FakeAudioSource

    class LocalAudioTrack:
        @staticmethod
        def create_audio_track(name, source):
            assert name == "agent-microphone"
            return SimpleNamespace(name=name, source=source)

    class TrackPublishOptions:
        def __init__(self):
            self.source = None

    class RoomOptions:
        def __init__(self, auto_subscribe):
            self.auto_subscribe = auto_subscribe

    class AudioStream:
        @staticmethod
        def from_track(track, sample_rate, num_channels, frame_size_ms, capacity):
            assert sample_rate == 16000
            assert num_channels == 1
            assert frame_size_ms == 20
            assert capacity == 100

            class Stream:
                async def aclose(self):
                    self.closed = True

                async def _frames(self):
                    for _ in range(12):
                        yield SimpleNamespace(frame=SimpleNamespace(
                            data=memoryview(pcm()), sample_rate=16000,
                            num_channels=1,
                        ))
                    for _ in range(35):
                        yield SimpleNamespace(frame=SimpleNamespace(
                            data=memoryview(pcm(0)), sample_rate=16000,
                            num_channels=1,
                        ))
                    # Keep subscribed track alive until session cancellation.
                    await asyncio.Event().wait()

                def __aiter__(self):
                    return self._frames()

            return Stream()

    class Room:
        def __init__(self):
            self.events = {}
            self.remote_identity = None
            self.published = False
            self.disconnect_count = 0
            self.local_participant = SimpleNamespace(
                publish_track=self._publish,
            )

        def on(self, event):
            def register(fn):
                self.events[event] = fn
                return fn
            return register

        async def _publish(self, track, options):
            assert options.source == 2
            self.published = True

        async def connect(self, url, token, options):
            assert url == "ws://127.0.0.1:7880"
            assert token == "scoped-media-jwt"
            assert options.auto_subscribe is True
            participant = SimpleNamespace(identity=self.remote_identity)
            self.events["track_subscribed"](
                SimpleNamespace(kind=1), SimpleNamespace(),
                participant,
            )

        async def disconnect(self):
            self.disconnect_count += 1


class FakeSTT:
    async def transcribe(self, data):
        return "Hallo, hörst du mich?"


class FakeLLM:
    async def respond(self, text, history):
        return "Ja, ich höre dich."


class FakeTTS:
    sample_rate = 16000

    def __init__(self):
        self.utterances = []

    async def synthesize(self, text):
        self.utterances.append(text)
        return AudioPacket(pcm(), self.sample_rate)


class FakeControl:
    def __init__(self):
        self.checks = 0

    async def media_token(self, call_id):
        return {"url": "ws://127.0.0.1:7880",
                "token": "scoped-media-jwt"}

    async def status(self, call_id):
        self.checks += 1
        return {"state": "ACTIVE" if self.checks == 1 else "ENDED"}


class VoiceLiveKitTests(unittest.IsolatedAsyncioTestCase):
    async def test_pcm_publisher_builds_real_20ms_lk_frames(self):
        source = FakeAudioSource()
        speaker = LiveKitPcmSpeaker(source, FakeRTC, sample_rate=16000)
        sent = await speaker.play(AudioPacket(pcm() + pcm(), 16000))
        self.assertTrue(sent)
        self.assertEqual(len(source.frames), 2)
        self.assertEqual(source.frames[0].samples_per_channel, 320)
        self.assertEqual(source.frames[0].sample_rate, 16000)
        speaker.interrupt()
        self.assertEqual(source.clears, 1)

    async def test_only_expected_remote_identity_is_subscribed(self):
        rtc = SimpleNamespace(**{k: getattr(FakeRTC, k) for k in (
            "TrackKind", "TrackSource", "AudioFrame", "AudioSource",
            "LocalAudioTrack", "TrackPublishOptions", "RoomOptions",
            "AudioStream")})
        rooms = []

        class FakeRoom(FakeRTC.Room):
            async def connect(self, url, token, options):
                # Deliberate first attempt to inject an unrelated participant.
                self.events["track_subscribed"](
                    SimpleNamespace(kind=1), SimpleNamespace(),
                    SimpleNamespace(identity="ip_unapproved"),
                )
                self.remote_identity = LiveKitAgentCall.participant_identity(
                    "a" * 32, "tenantA", "human1",
                )
                await super().connect(url, token, options)

        def create_room():
            room = FakeRoom()
            rooms.append(room)
            return room

        rtc.Room = create_room
        control = FakeControl()
        tts = FakeTTS()
        worker = LiveKitAgentCall(
            client=control, tenant="tenantA", callee="human1",
            tts=tts, stt=FakeSTT(), llm=FakeLLM(), rtc=rtc,
            greeting="Guten Tag. Sie sprechen mit dem digitalen Empfang.",
        )
        await asyncio.wait_for(worker.speak("a" * 32), 3)
        self.assertEqual(len(rooms), 1)
        self.assertTrue(rooms[0].published)
        self.assertEqual(rooms[0].disconnect_count, 1)
        self.assertTrue(worker._source.closed)
        self.assertEqual(tts.utterances[0],
                         "Guten Tag. Sie sprechen mit dem digitalen Empfang.")
        self.assertEqual(worker._engine.turns_completed, 1)
        self.assertGreaterEqual(control.checks, 2)


if __name__ == "__main__":
    unittest.main()
