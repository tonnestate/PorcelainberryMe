"""Provider-free, per-call voice pipeline: local STT -> local LLM -> local TTS.

Neither this module nor importing it installs models, contacts external AI
services, or creates a duplicate FruitStack/AnanasMe privileged agent.
Voice data and conversation history stay in memory for one accepted call.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import http.client
import json
import math
from pathlib import Path
import shutil
import struct
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
        voiced = self.rms(pcm) >= self.threshold
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

    async def transcribe(self, pcm: bytes) -> str:
        if not 0 < len(pcm) <= MAX_TURN_BYTES or len(pcm) % 2:
            raise ValueError("invalid_transcription_pcm")

        def infer():
            try:
                import numpy as np
            except ImportError as exc:
                raise LocalVoiceUnavailable("numpy_not_installed") from exc
            audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            segments, _ = self.model.transcribe(
                audio, language="de", beam_size=1, vad_filter=False,
                condition_on_previous_text=False,
            )
            return " ".join(s.text.strip() for s in segments).strip()[:1200]

        return await asyncio.to_thread(infer)


class LocalOllamaResponder:
    """Localhost-only API; no external LLM network traffic or agent tools."""

    def __init__(self, model: str, *, system: str = DEFAULT_SYSTEM,
                 timeout: float = 30):
        if not model or len(model) > 128 or any(c.isspace() for c in model):
            raise ValueError("explicit_local_model_required")
        if not isinstance(system, str) or not 1 <= len(system) <= 3000:
            raise ValueError("invalid_system_instructions")
        self.model = model
        self.system = system
        self.timeout = timeout

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
                "options": {"num_predict": 160, "temperature": 0.5},
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

    async def synthesize(self, text: str) -> AudioPacket:
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 500:
            raise ValueError("invalid_speech_reply")
        process = await asyncio.create_subprocess_exec(
            self.executable, "--model", self.model_path, "--output-raw",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        async def bounded_capture():
            # communicate() would buffer unbounded stdout in RAM before the
            # AudioPacket length check. Drain in bounded chunks instead.
            process.stdin.write((text.strip() + "\n").encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
            chunks = []
            total = 0
            while True:
                chunk = await process.stdout.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_REPLY_BYTES:
                    raise LocalVoiceUnavailable("local_piper_output_too_large")
                chunks.append(chunk)
            await process.wait()
            if process.returncode != 0:
                raise LocalVoiceUnavailable("local_piper_failed")
            return b"".join(chunks)

        try:
            output = await asyncio.wait_for(
                bounded_capture(), timeout=self.timeout,
            )
            return AudioPacket(output, self.sample_rate)
        except asyncio.TimeoutError as exc:
            raise LocalVoiceUnavailable("local_piper_timeout") from exc
        finally:
            # The process must not continue using host CPU/RAM after a
            # cancelled speech turn or a failed size/time limit.
            if process.returncode is None:
                process.kill()
                await process.wait()


class VoiceTurnEngine:
    """One bounded worker per active call; latest user turn wins.

    STT/LLM may be blocking local inference: old results are discarded after
    a barge-in, and the engine does not start concurrent inference jobs.
    Audio is never written to a file by this engine.
    """

    def __init__(self, stt: Transcriber, llm: Responder, tts: Synthesizer,
                 speaker: Speaker, *, threshold: int = 600,
                 on_unavailable: Callable[[], None] | None = None):
        self.detector = EnergyTurnDetector(threshold=threshold)
        self.stt, self.llm, self.tts, self.speaker = stt, llm, tts, speaker
        self.history: list[dict] = []
        self._epoch = 0
        self._pending: bytes | None = None
        self._worker: asyncio.Task | None = None
        self._closed = False
        self.turns_completed = 0
        self.turn_errors = 0
        self._consecutive_failures = 0
        self.on_unavailable = on_unavailable

    def receive(self, pcm: bytes):
        if self._closed:
            return
        started, utterance = self.detector.push(pcm)
        if started:
            self._epoch += 1
            self.speaker.interrupt()
        if utterance:
            self._pending = utterance
            if self._worker is None or self._worker.done():
                self._worker = asyncio.create_task(self._drain())

    async def _drain(self):
        while self._pending is not None and not self._closed:
            pcm, self._pending = self._pending, None
            epoch = self._epoch
            try:
                utterance = (await self.stt.transcribe(pcm)).strip()
                if epoch != self._epoch or self._closed or not utterance:
                    continue
                response = (await self.llm.respond(utterance, self.history)).strip()
                if epoch != self._epoch or self._closed or not response:
                    continue
                audio = await self.tts.synthesize(response)
                if epoch != self._epoch or self._closed:
                    continue
                heard = await self.speaker.play(audio)
                if heard and epoch == self._epoch and not self._closed:
                    self.history.extend([
                        {"role": "user", "content": utterance},
                        {"role": "assistant", "content": response},
                    ])
                    self.history = self.history[-12:]
                    self.turns_completed += 1
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
