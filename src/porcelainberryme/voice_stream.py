"""Stream approved Python/PyPI TTS audio to existing SIP PCM; no model install.

Both true async-PCM sources and old full-WAV Python callables are supported.
A legacy WAV is synthesized in full before the first output chunk, so it
must never be advertised as real-time streaming synthesis.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from io import BytesIO
from typing import AsyncIterator, Callable
import wave

from .voice import PythonVoiceBridge, VoiceUnavailable

MAX_AUDIO_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PcmSpec:
    sample_rate: int
    channels: int

    def __post_init__(self):
        if (type(self.sample_rate) is not int or not 8000 <= self.sample_rate <= 48000
                or type(self.channels) is not int or self.channels not in (1, 2)):
            raise ValueError("invalid_pcm_spec")

    @property
    def alignment(self):
        return 2 * self.channels

    @property
    def frame_bytes(self):
        return (self.sample_rate // 50) * self.alignment


def validated_text(text):
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
        raise ValueError("invalid_voice_text")
    return text


class InstalledPyPIStreamVoice:
    """Injected installed-engine source: (text, rate, channels) -> async PCM16LE iterator."""

    def __init__(self, produce: Callable[[str, int, int], AsyncIterator[bytes]] | None = None):
        self._produce = produce

    @property
    def ready(self):
        return callable(self._produce)

    def frames(self, text: str, spec: PcmSpec) -> AsyncIterator[bytes]:
        validated_text(text)
        if not self.ready:
            raise VoiceUnavailable("PYPI_STREAM_VOICE_NOT_BOUND")
        iterator = self._produce(text, spec.sample_rate, spec.channels)
        if not hasattr(iterator, "__aiter__"):
            raise VoiceUnavailable("PYPI_STREAM_VOICE_INVALID_SOURCE")
        return iterator


class ExistingPythonWavVoice:
    """Bridge the existing verified synchronous WAV callable, without provider selection.

    Synchronous Python inference continues in its thread if playback is
    interrupted; it cannot be forcibly killed by asyncio cancellation.
    """

    def __init__(self, voice: PythonVoiceBridge):
        if not isinstance(voice, PythonVoiceBridge) or not voice.ready:
            raise VoiceUnavailable("PYTHON_VOICE_NOT_BOUND")
        self.voice = voice
        self._inference: asyncio.Task | None = None

    def _clear_inference(self, task: asyncio.Task) -> None:
        # Consume failures from a thread that finishes after caller barge-in.
        # It is essential not to start a second expensive TTS while the first
        # thread is still using the existing installed engine/model.
        try:
            task.exception()
        except asyncio.CancelledError:
            pass
        if self._inference is task:
            self._inference = None

    async def frames(self, text: str, spec: PcmSpec) -> AsyncIterator[bytes]:
        validated_text(text)
        if self._inference is not None:
            raise VoiceUnavailable("PYTHON_VOICE_INFERENCE_BUSY")
        task = asyncio.create_task(asyncio.to_thread(self.voice.speak, text))
        self._inference = task
        task.add_done_callback(self._clear_inference)
        # Preserve the synchronous worker after playback cancellation, so
        # all subsequent turns fail closed until that worker has finished.
        result = await asyncio.shield(task)
        if result.mime_type != "audio/wav":
            raise VoiceUnavailable("VOICE_WAV_REQUIRED")
        try:
            with wave.open(BytesIO(result.audio), "rb") as reader:
                if (reader.getcomptype() != "NONE" or reader.getsampwidth() != 2
                        or reader.getframerate() != spec.sample_rate
                        or reader.getnchannels() != spec.channels):
                    raise VoiceUnavailable("VOICE_SIP_FORMAT_MISMATCH")
                total = 0
                while True:
                    pcm = reader.readframes(max(1, spec.sample_rate // 50))
                    if not pcm:
                        break
                    total += len(pcm)
                    if total > MAX_AUDIO_BYTES:
                        raise VoiceUnavailable("VOICE_TOO_LARGE")
                    yield pcm
                    await asyncio.sleep(0)
        except (wave.Error, EOFError) as exc:
            raise VoiceUnavailable("VOICE_WAV_INVALID") from exc


@dataclass(frozen=True, slots=True)
class PlaybackReceipt:
    state: str
    bytes_written: int


class SipVoiceTurn:
    """One authorized active voice turn, cancellable between PCM writes.

    No STT/VAD, model inference, voicemail, call recording or autonomous calls.
    Already buffered provider-side audio cannot be guaranteed to flush.
    """

    def __init__(self, session, voice, *, authorize: Callable[[object], bool]):
        if not callable(authorize):
            raise ValueError("playback_authorization_required")
        if not hasattr(voice, "frames"):
            raise ValueError("streaming_voice_source_required")
        if not callable(getattr(session, "write_pcm", None)):
            raise ValueError("sip_audio_session_required")
        self.session = session
        self.voice = voice
        self.authorize = authorize
        self._task: asyncio.Task | None = None
        self._written = 0

    async def play(self, text: str) -> PlaybackReceipt:
        validated_text(text)
        if self._task is not None:
            raise RuntimeError("voice_turn_already_running")
        if self.authorize(self.session) is not True:
            raise PermissionError("sip_voice_playback_not_authorized")
        info = self.session.audio_info
        spec = PcmSpec(info.tx_sample_rate, info.tx_channels)
        self._written = 0
        task = asyncio.create_task(self._emit(text, spec))
        self._task = task
        try:
            try:
                await task
            except asyncio.CancelledError:
                return PlaybackReceipt("INTERRUPTED", self._written)
            return PlaybackReceipt("COMPLETED", self._written)
        finally:
            if self._task is task:
                self._task = None

    async def _emit(self, text: str, spec: PcmSpec) -> None:
        pending = bytearray()
        total = 0
        async for chunk in self.voice.frames(text, spec):
            if (not isinstance(chunk, bytes) or not chunk
                    or len(chunk) > 65536 or len(chunk) % spec.alignment):
                raise VoiceUnavailable("PCM_CHUNK_INVALID")
            total += len(chunk)
            if total > MAX_AUDIO_BYTES:
                raise VoiceUnavailable("PCM_AUDIO_LIMIT")
            pending.extend(chunk)
            while len(pending) >= spec.frame_bytes:
                payload = bytes(pending[:spec.frame_bytes])
                del pending[:spec.frame_bytes]
                self._send(payload)
                await asyncio.sleep(0.02)
        if pending:
            self._send(bytes(pending))

    def _send(self, payload: bytes):
        written = self.session.write_pcm(payload)
        if written != len(payload):
            raise RuntimeError("sip_audio_backpressure")
        self._written += written

    async def interrupt(self) -> bool:
        task = self._task
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True
