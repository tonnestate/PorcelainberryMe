"""Provider-free, per-call voice pipeline: local STT -> local LLM -> local TTS.

Neither this module nor importing it installs models, contacts external AI
services, or creates a duplicate FruitStack/AnanasMe privileged agent.
Voice data and conversation history stay in memory for one accepted call.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import aclosing
from dataclasses import dataclass
import http.client
import json
import math
from pathlib import Path
import os
import re
import shutil
import struct
import threading
import subprocess
import time
from typing import Callable, Protocol


SAMPLE_RATE = 16000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * 2
MAX_TURN_BYTES = SAMPLE_RATE * 2 * 16  # 16 s of mono PCM16
MAX_REPLY_BYTES = 8 * 1024 * 1024
DEFAULT_SYSTEM = (
    "Du bist ein sachlicher, hilfreicher deutschsprachiger Telefonassistent. "
    "Antworte präzise und kurz, maximal drei Sätze. "
    "Erfinde keine ausgeführten Handlungen, Termine oder Kontodaten."
)


class LocalVoiceUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AudioPacket:
    pcm: bytes
    sample_rate: int

    def __post_init__(self):
        if (not isinstance(self.pcm, bytes) or not 0 < len(self.pcm) <= MAX_REPLY_BYTES
                or len(self.pcm) % 2 or not 8000 <= self.sample_rate <= 48000):
            raise ValueError("invalid_local_pcm")


class Transcriber(Protocol):
    async def transcribe(self, pcm: bytes) -> str: ...


class Responder(Protocol):
    async def respond(self, text: str, history: list[dict]) -> str: ...


class Synthesizer(Protocol):
    async def synthesize(self, text: str) -> AudioPacket: ...


class Speaker(Protocol):
    async def play(self, packet: AudioPacket) -> bool: ...
    def interrupt(self) -> None: ...


class EnergyTurnDetector:
    """Bounded fixed-frame detector: one speaker, 16 kHz PCM16/20-ms frames.

    Not neural VAD. Quiet speech, music and noisy rooms require verification
    with real audio; do not use the detector as an identity check.
    """

    def __init__(self, *, threshold: int = 600, start_ms: int = 80,
                 silence_ms: int = 650, max_speech_ms: int = 14000):
        if not 100 <= threshold <= 30000:
            raise ValueError("invalid_energy_threshold")
        if not 20 <= start_ms <= 1000 or not 100 <= silence_ms <= 3000:
            raise ValueError("invalid_turn_boundaries")
        self.threshold = threshold
        self.start_frames = max(1, start_ms // FRAME_MS)
        self.silence_frames = max(1, silence_ms // FRAME_MS)
        self.max_frames = min(16000 // FRAME_MS, max_speech_ms // FRAME_MS)
        self.leading = deque(maxlen=12)
        self.recording = bytearray()
        self.consecutive_voice = 0
        self.quiet_frames = 0
        self.started = False

    def _is_speech(self, pcm: bytes) -> bool:
        return self.rms(pcm) >= self.threshold

    @staticmethod
    def rms(pcm: bytes) -> float:
        if len(pcm) != FRAME_BYTES:
            raise ValueError("invalid_voice_frame_size")
        # Python 3.13 no longer ships audioop; compute RMS without it.
        squares = 0
        for (sample,) in struct.iter_unpack("<h", pcm):
            squares += sample * sample
        return math.sqrt(squares / (len(pcm) // 2))

    def push(self, pcm: bytes) -> tuple[bool, bytes | None]:
        if len(pcm) != FRAME_BYTES:
            raise ValueError("invalid_voice_frame_size")
        voiced = self._is_speech(pcm)
        if not self.started:
            self.leading.append(pcm)
            self.consecutive_voice = self.consecutive_voice + 1 if voiced else 0
            if self.consecutive_voice < self.start_frames:
                return False, None
            self.started = True
            self.recording.extend(b"".join(self.leading))
            self.leading.clear()
            self.quiet_frames = 0
            return True, None

        self.recording.extend(pcm)
        self.quiet_frames = 0 if voiced else self.quiet_frames + 1
        capped = len(self.recording) >= MAX_TURN_BYTES or (
            len(self.recording) >= self.max_frames * FRAME_BYTES
        )
        if self.quiet_frames >= self.silence_frames or capped:
            # Remove trailing silence; retain pre-roll to help whisper.
            silence_bytes = self.quiet_frames * FRAME_BYTES
            utterance = bytes(self.recording[:-silence_bytes] if silence_bytes else self.recording)
            self.reset()
            return False, utterance if len(utterance) >= 8 * FRAME_BYTES else None
        return False, None

    def reset(self):
        self.leading.clear()
        self.recording.clear()
        self.consecutive_voice = 0
        self.quiet_frames = 0
        self.started = False


class WebRTCTurnDetector(EnergyTurnDetector):
    """Opt-in WebRTC VAD on the existing 16-kHz/20-ms PCM stream.

    Keeps the bounded pre-roll and end-of-turn state machine; only the
    speech/non-speech classification is delegated to the optional local
    webrtcvad-wheels binary. No model download, server or network request.
    """

    def __init__(self, *, mode: int = 1, **kwargs):
        if type(mode) is not int or mode not in (0, 1, 2, 3):
            raise ValueError("invalid_webrtc_vad_mode")
        try:
            import webrtcvad
        except ImportError as exc:
            raise LocalVoiceUnavailable("local_webrtc_vad_not_installed") from exc
        self._vad_factory = webrtcvad.Vad
        self._vad_mode = mode
        self._vad = self._vad_factory(mode)
        super().__init__(**kwargs)

    def _is_speech(self, pcm: bytes) -> bool:
        return bool(self._vad.is_speech(pcm, SAMPLE_RATE))

    def reset(self):
        # Reset the native VAD's acoustic state after a completed turn or
        # remote microphone replacement; never bleed it across tracks.
        super().reset()
        self._vad = self._vad_factory(self._vad_mode)


class LocalFasterWhisper:
    """Load an already-installed local CTranslate2 model, never a model ID."""

    def __init__(self, model_path: str, *, device: str = "cpu"):
        path = Path(model_path)
        if not path.is_dir() or not (path / "model.bin").is_file():
            raise LocalVoiceUnavailable("local_whisper_model_missing")
        if device not in ("cpu", "cuda"):
            raise ValueError("invalid_local_device")
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise LocalVoiceUnavailable("faster_whisper_not_installed") from exc
        self.model = WhisperModel(str(path), device=device,
                                  compute_type="int8" if device == "cpu" else "float16")
        # A cancelled asyncio.to_thread() does not stop its native CTranslate2
        # inference thread. Keep the slot until that real thread exits.
        self._inference_slot = threading.Lock()

    async def transcribe(self, pcm: bytes) -> str:
        if not 0 < len(pcm) <= MAX_TURN_BYTES or len(pcm) % 2:
            raise ValueError("invalid_transcription_pcm")

        def infer():
            # Never launch a second native inference on the same model while
            # an interrupted prior call still owns its actual worker thread.
            # Fail closed rather than pile up unbounded waiting threads.
            if not self._inference_slot.acquire(blocking=False):
                raise LocalVoiceUnavailable("local_whisper_inference_busy")
            try:
                try:
                    import numpy as np
                except ImportError as exc:
                    raise LocalVoiceUnavailable("numpy_not_installed") from exc
                audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
                segments, _ = self.model.transcribe(
                    audio, language="de", beam_size=1, vad_filter=False,
                    condition_on_previous_text=False,
                )
                return " ".join(segment.text.strip() for segment in segments).strip()[:1200]
            finally:
                self._inference_slot.release()

        return await asyncio.to_thread(infer)


class LocalOllamaResponder:
    """Localhost-only API; no external LLM network traffic or agent tools."""

    def __init__(self, model: str, *, system: str = DEFAULT_SYSTEM,
                 timeout: float = 30, max_tokens: int = 160,
                 disable_thinking: bool = False):
        if not model or len(model) > 128 or any(c.isspace() for c in model):
            raise ValueError("explicit_local_model_required")
        if not isinstance(system, str) or not 1 <= len(system) <= 3000:
            raise ValueError("invalid_system_instructions")
        if type(max_tokens) is not int or not 16 <= max_tokens <= 160:
            raise ValueError("local_voice_token_budget_out_of_range")
        if type(disable_thinking) is not bool:
            raise ValueError("local_voice_thinking_setting_invalid")
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 60:
            raise ValueError("local_voice_timeout_invalid")
        self.model = model
        self.system = system
        self.timeout = float(timeout)
        self.max_tokens = max_tokens
        self.disable_thinking = disable_thinking

    async def preflight(self) -> None:
        """Reject missing local models before dialing any recipient.

        Ollama's local model listing is checked without pulling a model.
        The endpoint is fixed to loopback, never a paid/cloud API.
        """
        def probe():
            conn = None
            try:
                conn = http.client.HTTPConnection(
                    "127.0.0.1", 11434, timeout=min(self.timeout, 5),
                )
                conn.request("GET", "/api/tags")
                response = conn.getresponse()
                data = response.read(65537)
                if response.status != 200 or len(data) > 65536:
                    raise LocalVoiceUnavailable("local_ollama_registry_unavailable")
                try:
                    registry = json.loads(data)
                    models = registry["models"]
                    if not isinstance(models, list):
                        raise ValueError("invalid_models_array")
                except (ValueError, KeyError, TypeError) as exc:
                    raise LocalVoiceUnavailable("local_ollama_registry_invalid") from exc
                installed = {
                    identifier
                    for item in models if isinstance(item, dict)
                    for identifier in (item.get("name"), item.get("model"))
                    if isinstance(identifier, str)
                }
                if self.model not in installed:
                    raise LocalVoiceUnavailable("local_ollama_model_not_installed")
            except (OSError, http.client.HTTPException) as exc:
                raise LocalVoiceUnavailable("local_ollama_unreachable") from exc
            finally:
                if conn is not None:
                    conn.close()
        await asyncio.to_thread(probe)

    async def stream_sentences(self, text: str, history: list[dict]):
        """Yield complete sentences from local Ollama NDJSON as they arrive.

        Uses an async, locally connected HTTP client so cancelled speech
        turns close the response instead of leaving a blocking inference
        reader behind. Each segment can be synthesized before model
        generation finishes. No remote providers, tools or model pulls.
        """
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1200:
            raise ValueError("invalid_transcribed_text")
        try:
            import aiohttp
        except ImportError as exc:
            raise LocalVoiceUnavailable("local_stream_http_not_installed") from exc

        request = {
            "model": self.model,
            "stream": True,
            "messages": [{"role": "system", "content": self.system}]
            + history[-12:]
            + [{"role": "user", "content": text}],
            "options": {"num_predict": self.max_tokens, "temperature": 0.5},
            **({"think": False} if self.disable_thinking else {}),
        }
        remaining = ""
        generated_chars = 0
        saw_done = False
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    "http://127.0.0.1:11434/api/chat", json=request,
                ) as response:
                    if response.status != 200:
                        raise LocalVoiceUnavailable("local_ollama_reply_failed")
                    async for raw in response.content:
                        if len(raw) > 8192:
                            raise LocalVoiceUnavailable("local_ollama_stream_line_too_long")
                        try:
                            event = json.loads(raw)
                            if not isinstance(event, dict) or type(event.get("done")) is not bool:
                                raise ValueError("invalid_stream_event")
                            if event.get("error"):
                                raise ValueError("ollama_stream_error")
                            message = event.get("message") or {}
                            piece = message.get("content", "")
                            if not isinstance(piece, str):
                                raise ValueError("invalid_stream_content")
                        except (ValueError, TypeError, AttributeError) as exc:
                            raise LocalVoiceUnavailable("local_ollama_stream_invalid") from exc
                        generated_chars += len(piece)
                        if generated_chars > 500:
                            raise LocalVoiceUnavailable("local_ollama_reply_too_long")
                        remaining += piece
                        # Only emit complete sentence boundaries. Sentence
                        # content stays bounded by the 500-character limit.
                        while True:
                            boundary = re.search(r"[.!?]\s+", remaining)
                            if boundary is None:
                                break
                            sentence = remaining[:boundary.start() + 1].strip()
                            remaining = remaining[boundary.end():]
                            if sentence:
                                yield sentence
                        if event["done"]:
                            saw_done = True
                            if remaining.strip():
                                yield remaining.strip()
                            break
                    if not saw_done or generated_chars == 0:
                        raise LocalVoiceUnavailable("local_ollama_stream_incomplete")
        except (OSError, asyncio.TimeoutError, aiohttp.ClientError) as exc:
            raise LocalVoiceUnavailable("local_ollama_stream_unreachable") from exc

    async def respond(self, text: str, history: list[dict]) -> str:
        if not 1 <= len(text.strip()) <= 1200:
            raise ValueError("invalid_transcribed_text")

        def infer():
            # This host is hard-coded loopback. Caller cannot configure remote
            # Ollama or arbitrary outbound URLs via a tenant prompt.
            conn = http.client.HTTPConnection("127.0.0.1", 11434, timeout=self.timeout)
            request = {
                "model": self.model,
                "stream": False,
                "messages": [{"role": "system", "content": self.system}]
                + history[-12:]
                + [{"role": "user", "content": text}],
                "options": {"num_predict": self.max_tokens, "temperature": 0.5},
            **({"think": False} if self.disable_thinking else {}),
            }
            try:
                conn.request("POST", "/api/chat",
                             json.dumps(request, ensure_ascii=False).encode(),
                             {"Content-Type": "application/json"})
                response = conn.getresponse()
                data = response.read(65537)
                if response.status != 200 or len(data) > 65536:
                    raise LocalVoiceUnavailable("local_ollama_reply_failed")
                result = json.loads(data)
                answer = result.get("message", {}).get("content")
                if not isinstance(answer, str) or not answer.strip():
                    raise LocalVoiceUnavailable("local_ollama_empty_answer")
                return answer.strip()[:500]
            finally:
                conn.close()

        return await asyncio.to_thread(infer)


class LocalPiperVoice:
    """Existing executable/ONNX only. Pipe PCM16 mono; never install/download."""

    def __init__(self, executable: str, model_path: str, *,
                 timeout: float = 30):
        exe = Path(executable)
        model = Path(model_path)
        if not exe.is_file() or not model.is_file():
            raise LocalVoiceUnavailable("local_piper_files_missing")
        config = Path(str(model) + ".json")
        if not config.is_file():
            raise LocalVoiceUnavailable("local_piper_config_missing")
        try:
            metadata = json.loads(config.read_text(encoding="utf-8"))
            rate = int(metadata["audio"]["sample_rate"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise LocalVoiceUnavailable("local_piper_rate_unknown") from exc
        if not 8000 <= rate <= 48000:
            raise LocalVoiceUnavailable("unsupported_local_piper_rate")
        self.executable = str(exe)
        self.model_path = str(model)
        self.sample_rate = rate
        self.timeout = timeout
        preference = os.environ.get("PB_LOCAL_PIPER_STREAM", "0")
        if preference not in ("0", "1"):
            raise ValueError("invalid_piper_stream_setting")
        self.streaming_enabled = preference == "1"

    async def stream_audio(self, text: str):
        """Stream bounded 100-ms PCM packets from local Piper stdout.

        A fully completed packet may be played before Piper finishes the
        remaining speech. The same local process is killed on cancellation,
        interruption or an invalid/oversized response.
        """
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 500:
            raise ValueError("invalid_speech_reply")
        if (not isinstance(self.timeout, (int, float))
                or not 0 < self.timeout <= 180):
            raise ValueError("local_piper_total_timeout_invalid")
        process = await asyncio.create_subprocess_exec(
            self.executable, "--model", self.model_path, "--output-raw",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # An absolute deadline also rejects a subprocess which dribbles
        # small valid packets forever, resetting a per-read timeout.
        clock = asyncio.get_running_loop()
        deadline = clock.time() + float(self.timeout)
        async def bounded(operation):
            remaining = deadline - clock.time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            return await asyncio.wait_for(operation(), timeout=remaining)
        packet_bytes = (self.sample_rate // 50) * 2 * 5
        pending = bytearray()
        total = 0
        emitted = False
        try:
            process.stdin.write((text.strip() + "\n").encode("utf-8"))
            await bounded(process.stdin.drain)
            process.stdin.close()
            while True:
                raw = await bounded(lambda: process.stdout.read(65536))
                if not raw:
                    break
                total += len(raw)
                if total > MAX_REPLY_BYTES:
                    raise LocalVoiceUnavailable("local_piper_output_too_large")
                pending.extend(raw)
                while len(pending) >= packet_bytes:
                    packet = bytes(pending[:packet_bytes])
                    del pending[:packet_bytes]
                    emitted = True
                    yield AudioPacket(packet, self.sample_rate)
            await bounded(process.wait)
            if process.returncode != 0:
                raise LocalVoiceUnavailable("local_piper_failed")
            if len(pending) % 2:
                raise LocalVoiceUnavailable("local_piper_invalid_pcm")
            if pending:
                emitted = True
                yield AudioPacket(bytes(pending), self.sample_rate)
            if not emitted:
                raise LocalVoiceUnavailable("local_piper_empty_output")
        except asyncio.TimeoutError as exc:
            raise LocalVoiceUnavailable("local_piper_timeout") from exc
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()

    async def synthesize(self, text: str) -> AudioPacket:
        # Preserve the complete-response adapter for existing callers and
        # for all installations without explicit progressive speech opt-in.
        chunks = []
        async with aclosing(self.stream_audio(text)) as stream:
            async for packet in stream:
                chunks.append(packet.pcm)
        return AudioPacket(b"".join(chunks), self.sample_rate)


class VoiceTurnEngine:
    """One bounded worker per active call; latest user turn wins.

    STT/LLM may be blocking local inference: old results are discarded after
    a barge-in, and the engine does not start concurrent inference jobs.
    Audio is never written to a file by this engine.
    """

    def __init__(self, stt: Transcriber, llm: Responder, tts: Synthesizer,
                 speaker: Speaker, *, threshold: int = 600,
                 on_unavailable: Callable[[], None] | None = None,
                 vad_backend: str = "energy"):
        if vad_backend == "energy":
            self.detector = EnergyTurnDetector(threshold=threshold)
        elif vad_backend == "webrtc":
            self.detector = WebRTCTurnDetector()
        else:
            raise ValueError("invalid_vad_backend")
        self.stt, self.llm, self.tts, self.speaker = stt, llm, tts, speaker
        self.history: list[dict] = []
        self._epoch = 0
        self._stream_interrupted = asyncio.Event()
        self._pending: bytes | None = None
        self._worker: asyncio.Task | None = None
        self._closed = False
        self.turns_completed = 0
        self.turn_errors = 0
        self._consecutive_failures = 0
        self.on_unavailable = on_unavailable
        # Strictly bounded timing samples. Never retain transcripts,
        # speaker identity, audio or model prompts in diagnostics.
        self._stt_ms = deque(maxlen=64)
        self._first_media_ack_ms = deque(maxlen=64)
        self._completed_turn_ms = deque(maxlen=64)
        self.input_interrupt_signals = 0
        self._metrics_epoch = None
        self._metrics_started = None
        self._metrics_first_ack_epoch = None

    @staticmethod
    def _duration_ms(start):
        return min(3600000, max(0, int((time.monotonic() - start) * 1000)))

    @staticmethod
    def _percentile_summary(samples):
        if not samples:
            return {"samples": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
        values = sorted(samples)
        return {
            "samples": len(values),
            "p50_ms": values[math.ceil(len(values) * 0.5) - 1],
            "p95_ms": values[math.ceil(len(values) * 0.95) - 1],
            "max_ms": values[-1],
        }

    def metrics_snapshot(self) -> dict:
        """Only bounded in-memory timing/quality counters, never PII."""
        return {
            "schema": "porcelainberryme.voice-metrics.v1",
            "turns_completed": self.turns_completed,
            "turn_errors": self.turn_errors,
            "input_interrupt_signals": self.input_interrupt_signals,
            "stt_latency": self._percentile_summary(self._stt_ms),
            "first_media_ack_latency": self._percentile_summary(
                self._first_media_ack_ms,
            ),
            "complete_turn_latency": self._percentile_summary(
                self._completed_turn_ms,
            ),
            "measurement_note": (
                "First-media timing ends at media-adapter acknowledgement, "
                "not proof that a human heard or understood audio."
            ),
        }

    async def _play_and_measure(self, packet: AudioPacket, epoch: int) -> bool:
        played = await self.speaker.play(packet)
        if (played and epoch == self._epoch
                and self._metrics_epoch == epoch
                and self._metrics_started is not None
                and self._metrics_first_ack_epoch != epoch):
            self._first_media_ack_ms.append(
                self._duration_ms(self._metrics_started)
            )
            self._metrics_first_ack_epoch = epoch
        return bool(played)

    def receive(self, pcm: bytes):
        if self._closed:
            return
        started, utterance = self.detector.push(pcm)
        if started:
            if self._worker is not None and not self._worker.done():
                self.input_interrupt_signals += 1
            self._epoch += 1
            self._stream_interrupted.set()
            self._stream_interrupted = asyncio.Event()
            self.speaker.interrupt()
        if utterance:
            self._pending = utterance
            if self._worker is None or self._worker.done():
                self._worker = asyncio.create_task(self._drain())

    def reset_input(self):
        """Discard interrupted microphone segments after track loss.

        A resubscribed microphone is a new acoustic input stream. Old
        partial PCM must never be concatenated with the new stream or
        produce a stale spoken reply/tool action after disconnection.
        """
        if self._closed:
            return
        if self._worker is not None and not self._worker.done():
            self.input_interrupt_signals += 1
        self._epoch += 1
        self._stream_interrupted.set()
        self._stream_interrupted = asyncio.Event()
        self._pending = None
        self.detector.reset()
        self.speaker.interrupt()

    @staticmethod
    async def _next_or_interrupted(iterator, interrupted: asyncio.Event):
        """Read one async chunk, or cancel its source on acoustic barge-in.

        An await on local TTS stdout or Ollama's HTTP stream must not hold
        the old conversation hostage until the next model byte arrives.
        The caller owns and closes the async generator on interruption.
        """
        if interrupted.is_set():
            return False, None
        reading = asyncio.ensure_future(anext(iterator))
        cancellation = asyncio.create_task(interrupted.wait())
        try:
            await asyncio.wait(
                (reading, cancellation), return_when=asyncio.FIRST_COMPLETED,
            )
            if interrupted.is_set():
                reading.cancel()
                await asyncio.gather(reading, return_exceptions=True)
                return False, None
            return True, reading.result()
        finally:
            if not reading.done():
                reading.cancel()
                await asyncio.gather(reading, return_exceptions=True)
            cancellation.cancel()
            await asyncio.gather(cancellation, return_exceptions=True)

    async def _speak_response(
        self, response: str, epoch: int, interrupted: asyncio.Event,
    ) -> bool:
        """Keep turn delivery fenced during progressive local speech."""
        stream_audio = getattr(self.tts, "stream_audio", None)
        if (getattr(self.tts, "streaming_enabled", False)
                and callable(stream_audio)):
            audible = False
            async with aclosing(stream_audio(response)) as packets:
                while True:
                    try:
                        available, packet = await self._next_or_interrupted(
                            packets, interrupted,
                        )
                    except StopAsyncIteration:
                        break
                    if not available or epoch != self._epoch or self._closed:
                        return False
                    if not await self._play_and_measure(packet, epoch):
                        return False
                    if epoch != self._epoch or self._closed:
                        return False
                    audible = True
            return audible and epoch == self._epoch and not self._closed
        audio = await self.tts.synthesize(response)
        if epoch != self._epoch or self._closed:
            return False
        return await self._play_and_measure(audio, epoch)

    async def _drain(self):
        while self._pending is not None and not self._closed:
            pcm, self._pending = self._pending, None
            epoch = self._epoch
            interrupted = self._stream_interrupted
            turn_started = time.monotonic()
            self._metrics_epoch = epoch
            self._metrics_started = turn_started
            self._metrics_first_ack_epoch = None
            try:
                stt_started = time.monotonic()
                utterance = (await self.stt.transcribe(pcm)).strip()
                if epoch == self._epoch and not self._closed:
                    self._stt_ms.append(self._duration_ms(stt_started))
                if epoch != self._epoch or self._closed or not utterance:
                    continue
                stream = getattr(self.llm, "stream_sentences", None)
                if stream is not None:
                    # Each sentence is synthesized and published before the
                    # complete LLM response arrives. Cancel/interrupt checks
                    # also stop subsequent speech chunks and close the HTTP
                    # stream without committing a speculative conversation.
                    spoken_parts = []
                    heard = True
                    async with aclosing(stream(utterance, self.history)) as sentences:
                        while True:
                            try:
                                available, sentence = await self._next_or_interrupted(
                                    sentences, interrupted,
                                )
                            except StopAsyncIteration:
                                break
                            if not available or epoch != self._epoch or self._closed:
                                heard = False
                                break
                            if not isinstance(sentence, str) or not sentence.strip():
                                raise LocalVoiceUnavailable("invalid_streamed_sentence")
                            if not await self._speak_response(
                                sentence.strip(), epoch, interrupted,
                            ):
                                heard = False
                                break
                            if epoch != self._epoch or self._closed:
                                heard = False
                                break
                            spoken_parts.append(sentence.strip())
                    response = " ".join(spoken_parts)
                else:
                    response = (await self.llm.respond(utterance, self.history)).strip()
                    if epoch != self._epoch or self._closed or not response:
                        continue
                    heard = await self._speak_response(response, epoch, interrupted)
                if heard and response and epoch == self._epoch and not self._closed:
                    # Consent-gated notes depend on turns the other party
                    # could actually hear, not on STT/LLM speculative work.
                    delivered = getattr(self.llm, "on_turn_delivered", None)
                    if delivered is not None:
                        delivered(utterance, response)
                    self.history.extend([
                        {"role": "user", "content": utterance},
                        {"role": "assistant", "content": response},
                    ])
                    self.history = self.history[-12:]
                    self.turns_completed += 1
                    self._completed_turn_ms.append(
                        self._duration_ms(turn_started)
                    )
                    self._consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                # No fabricated response and no hidden paid API fallback.
                # Repeated local speech failures must not leave the caller
                # in an indefinitely silent yet ACTIVE telephone session.
                self.turn_errors += 1
                self._consecutive_failures += 1
                if self._consecutive_failures >= 2:
                    self._closed = True
                    self._pending = None
                    try:
                        self.speaker.interrupt()
                    finally:
                        if self.on_unavailable is not None:
                            self.on_unavailable()

    async def close(self):
        self._closed = True
        self._stream_interrupted.set()
        self.detector.reset()
        self._pending = None
        self.speaker.interrupt()
        if self._worker is not None:
            # A detached synchronous inference thread may outlive cancel.
            # The process/session must not launch parallel inference as a result.
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        self.history.clear()
