"""Offline LiveKit-voice adapter contracts.

The transport is exercised with a simulated WebRTC SDK; these tests do NOT
demonstrate latency or media connectivity on the actual VPS.
"""
import asyncio
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest

from porcelainberryme.call_notes import CallNotesStore, ConsentedNotesResponder
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
                SimpleNamespace(kind=1),
                SimpleNamespace(source=FakeRTC.TrackSource.SOURCE_MICROPHONE),
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

    async def test_hangup_during_slow_greeting_cancels_speech(self):
        rtc = SimpleNamespace(**{k: getattr(FakeRTC, k) for k in (
            "TrackKind", "TrackSource", "AudioFrame", "AudioSource",
            "LocalAudioTrack", "TrackPublishOptions", "RoomOptions",
            "AudioStream")})
        rooms = []
        class Room(FakeRTC.Room):
            pass

        def make_room():
            room = Room()
            rooms.append(room)
            return room
        rtc.Room = make_room

        class EndingControl(FakeControl):
            async def status(self, call_id):
                await asyncio.sleep(0.02)
                return {"state": "ENDED"}

        class SlowTTS(FakeTTS):
            def __init__(self):
                super().__init__()
                self.entered = asyncio.Event()
                self.cancelled = False

            async def synthesize(self, text):
                self.entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise

        tts = SlowTTS()
        worker = LiveKitAgentCall(
            client=EndingControl(), tenant="tenantA", callee="human1",
            tts=tts, stt=FakeSTT(), llm=FakeLLM(), rtc=rtc,
            greeting="Guten Tag, bitte hören Sie zu",
        )
        await asyncio.wait_for(worker.speak("b" * 32), timeout=1.0)
        self.assertTrue(tts.entered.is_set())
        self.assertTrue(tts.cancelled)
        self.assertEqual(len(rooms), 1)
        self.assertEqual(rooms[0].disconnect_count, 1)
        self.assertTrue(worker._source.closed)
        self.assertEqual(worker._source.frames, [])

    async def test_real_voice_engine_persists_notes_only_after_spoken_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = CallNotesStore(str(Path(tmp) / "call-notes.sqlite"))
            rtc = SimpleNamespace(**{k: getattr(FakeRTC, k) for k in (
                "TrackKind", "TrackSource", "AudioFrame", "AudioSource",
                "LocalAudioTrack", "TrackPublishOptions", "RoomOptions",
            )})

            class TwoTurnAudioStream:
                @staticmethod
                def from_track(track, sample_rate, num_channels,
                               frame_size_ms, capacity):
                    class Stream:
                        async def aclose(self):
                            pass
                        async def _gen(self):
                            for _turn in range(2):
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
                                await asyncio.sleep(0.07)
                            await asyncio.Event().wait()
                        def __aiter__(self):
                            return self._gen()
                    return Stream()

            rtc.AudioStream = TwoTurnAudioStream
            class Room(FakeRTC.Room):
                async def connect(self, url, token, options):
                    self.remote_identity = LiveKitAgentCall.participant_identity(
                        "e" * 32, "tenantA", "human1",
                    )
                    await super().connect(url, token, options)
            rtc.Room = Room

            class STT:
                count = 0
                async def transcribe(self, data):
                    self.count += 1
                    return (
                        "Ich stimme der Speicherung einer Gesprächsnotiz zu."
                        if self.count == 1 else
                        "Bitte kontaktieren Sie mich wegen des Liefertermins."
                    )

            class BaseResponder:
                async def respond(self, text, history):
                    return "Ich habe Ihre Frage gehört."

            class Control(FakeControl):
                async def status(self, call_id):
                    self.checks += 1
                    return {"state": "ACTIVE" if self.checks <= 2 else "ENDED"}

            llm = ConsentedNotesResponder(
                BaseResponder(), db,
                tenant="tenantA", assistant="assistant", caller="human1",
            )
            worker = LiveKitAgentCall(
                client=Control(), tenant="tenantA", callee="human1",
                tts=FakeTTS(), stt=STT(), llm=llm, rtc=rtc,
                greeting="Willkommen beim digitalen Empfang.",
            )
            await asyncio.wait_for(worker.speak("e" * 32), timeout=4.5)
            notes = db.list_notes("tenantA", "assistant")
            self.assertEqual(len(notes), 1)
            self.assertIn("Liefertermins", notes[0].text)
            self.assertNotIn("Ich stimme", notes[0].text)
            self.assertEqual(notes[0].excerpts, 1)

    async def test_microphone_track_resubscription_resets_partial_pcm_and_recovers(self):
        rtc = SimpleNamespace(**{k: getattr(FakeRTC, k) for k in (
            "TrackKind", "TrackSource", "AudioFrame", "AudioSource",
            "LocalAudioTrack", "TrackPublishOptions", "RoomOptions",
        )})
        tracks = []
        received = []

        class STT:
            async def transcribe(self, pcm_data):
                received.append(pcm_data)
                return "Neuer Anrufabschnitt"

        class Stream:
            def __init__(self, track):
                self.track = track
            async def aclose(self):
                pass
            async def _frames(self):
                # An interrupted old microphone stream has a partial turn
                # and must never contaminate the new subscription.
                length = 6 if self.track.name == "old" else 12
                for _ in range(length):
                    yield SimpleNamespace(frame=SimpleNamespace(
                        data=memoryview(pcm(1200)), sample_rate=16000,
                        num_channels=1,
                    ))
                if self.track.name != "old":
                    for _ in range(35):
                        yield SimpleNamespace(frame=SimpleNamespace(
                            data=memoryview(pcm(0)), sample_rate=16000,
                            num_channels=1,
                        ))
                await asyncio.Event().wait()
            def __aiter__(self):
                return self._frames()

        class AudioStream:
            @staticmethod
            def from_track(track, **kwargs):
                tracks.append(track.name)
                return Stream(track)

        rtc.AudioStream = AudioStream

        class ReconnectingRoom(FakeRTC.Room):
            async def connect(self, url, token, options):
                assert url == "ws://127.0.0.1:7880"
                identity = LiveKitAgentCall.participant_identity(
                    "d" * 32, "tenantA", "human1",
                )
                participant = SimpleNamespace(identity=identity)
                old = SimpleNamespace(kind=1, name="old")
                new = SimpleNamespace(kind=1, name="new")
                self.events["track_subscribed"](old, SimpleNamespace(source=2), participant)
                await asyncio.sleep(0.02)
                self.events["track_unsubscribed"](old, SimpleNamespace(), participant)
                # SDKs may also drop a remote participant without emitting
                # another unsubscribed event. Both paths must be idempotent.
                self.events["participant_disconnected"](participant)
                self.events["track_subscribed"](new, SimpleNamespace(source=2), participant)
                # Same publication may be emitted twice by the SDK: only
                # one audio stream must consume it.
                self.events["track_subscribed"](new, SimpleNamespace(source=2), participant)

        rooms = []
        def room_factory():
            room = ReconnectingRoom()
            rooms.append(room)
            return room
        rtc.Room = room_factory
        worker = LiveKitAgentCall(
            client=FakeControl(), tenant="tenantA", callee="human1",
            tts=FakeTTS(), stt=STT(), llm=FakeLLM(), rtc=rtc,
        )
        await asyncio.wait_for(worker.speak("d" * 32), timeout=3)
        self.assertEqual(tracks, ["old", "new"])
        self.assertEqual(worker._engine.turn_errors, 0)
        self.assertEqual(worker._engine.turns_completed, 1)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0], pcm(1200) * 12)
        self.assertEqual(rooms[0].disconnect_count, 1)

    async def test_staff_handoff_ignores_nonmicrophone_media_track(self):
        rtc = SimpleNamespace(**{k: getattr(FakeRTC, k) for k in (
            "TrackKind", "TrackSource", "AudioFrame", "AudioSource",
            "LocalAudioTrack", "TrackPublishOptions", "RoomOptions",
            "AudioStream",
        )})

        class HandoffControl(FakeControl):
            def __init__(self):
                super().__init__()
                self.completions = []

            async def status(self, call_id):
                self.checks += 1
                return {
                    "state": "ACTIVE" if self.checks == 1 else "ENDED",
                    "handoff": {"state": "ACCEPTED", "target": "staff1"},
                }

            async def change_handoff(self, call_id, action):
                self.completions.append((call_id, action))
                return {"state": "CONNECTED"}

        control = HandoffControl()
        call_id = "f" * 32

        class StaffRoom(FakeRTC.Room):
            async def connect(self, url, token, options):
                staff_identity = LiveKitAgentCall.participant_identity(
                    call_id, "tenantA", "staff1",
                )
                staff = SimpleNamespace(identity=staff_identity)
                track = SimpleNamespace(kind=FakeRTC.TrackKind.KIND_AUDIO)
                self.events["track_subscribed"](
                    track, SimpleNamespace(source="screen_audio"), staff,
                )
                await asyncio.sleep(0.01)
                self.assertions_before_mic = list(control.completions)
                self.events["track_subscribed"](
                    track, SimpleNamespace(
                        source=FakeRTC.TrackSource.SOURCE_MICROPHONE,
                    ), staff,
                )
                await asyncio.sleep(0.01)

        rooms = []
        def create_room():
            room = StaffRoom()
            rooms.append(room)
            return room
        rtc.Room = create_room

        worker = LiveKitAgentCall(
            client=control, tenant="tenantA", callee="human1",
            tts=FakeTTS(), stt=FakeSTT(), llm=FakeLLM(), rtc=rtc,
        )
        await asyncio.wait_for(worker.speak(call_id), timeout=3)
        self.assertEqual(rooms[0].assertions_before_mic, [])
        self.assertEqual(control.completions, [(call_id, "complete")])
        self.assertEqual(rooms[0].disconnect_count, 1)

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
        # When the caller starts speaking immediately, the greeting may
        # correctly be interrupted or suppressed. A reply must be generated.
        self.assertIn("Ja, ich höre dich.", tts.utterances)
        self.assertEqual(worker._engine.turns_completed, 1)
        self.assertGreaterEqual(control.checks, 2)


if __name__ == "__main__":
    unittest.main()
