"""Opt-in adapter for a pre-existing Piper Python/CLI voice installation.

The voice profile is never selected automatically: the caller must provide the
real installed executable AND the exact existing .onnx model file. No provider,
downloads, voice cloning, cache installation or background daemon is started.
"""
from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import tempfile
import wave

from .voice import SpeechResult, VoiceUnavailable


class ExistingPiperVoice:
    """Run the already installed Piper executable against an existing voice.

    Piper uses stdin for text and --model /absolute/path --output_file WAV.
    Installation and voice licensing are external operator responsibilities.
    """

    def __init__(self, *, executable: str | Path, model: str | Path,
                 timeout_seconds: int = 45, max_audio_bytes: int = 10_000_000):
        self.executable = Path(executable)
        self.model = Path(model)
        self.model_config = Path(str(self.model) + ".json")
        self.timeout_seconds = timeout_seconds
        self.max_audio_bytes = max_audio_bytes
        if (not self.executable.is_absolute() or not self.executable.is_file()
                or not os.access(self.executable, os.X_OK)
                or not self.model.is_absolute() or self.model.suffix.lower() != ".onnx"
                or not self.model.is_file() or not self.model_config.is_file()
                or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 120
                or not 4096 <= max_audio_bytes <= 10_000_000):
            raise VoiceUnavailable("PIPER_EXISTING_INSTALLATION_NOT_VERIFIED")

    def synthesize(self, text: str) -> SpeechResult:
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
            raise ValueError("invalid_voice_text")
        # Temporary WAV output: no persistent audio file, no shell, no external URL.
        with tempfile.TemporaryDirectory(prefix="pb-piper-") as directory:
            wav_path = Path(directory) / "speech.wav"
            command = [
                str(self.executable), "--model", str(self.model),
                "--output_file", str(wav_path)
            ]
            try:
                outcome = subprocess.run(
                    command, input=text, encoding="utf-8",
                    capture_output=True, timeout=self.timeout_seconds, check=False
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise VoiceUnavailable("PIPER_GENERATION_FAILED") from exc
            if outcome.returncode != 0:
                # stderr may include paths and private text; do not forward it.
                raise VoiceUnavailable("PIPER_GENERATION_FAILED")
            try:
                if not wav_path.is_file() or not 44 <= wav_path.stat().st_size <= self.max_audio_bytes:
                    raise VoiceUnavailable("PIPER_WAV_INVALID")
                audio = wav_path.read_bytes()
                with wave.open(io.BytesIO(audio), "rb") as stream:
                    if (stream.getnchannels() not in (1, 2)
                            or stream.getframerate() < 8000
                            or stream.getnframes() < 1
                            or stream.getsampwidth() not in (1, 2, 3, 4)):
                        raise VoiceUnavailable("PIPER_WAV_INVALID")
            except (OSError, EOFError, wave.Error) as exc:
                raise VoiceUnavailable("PIPER_WAV_INVALID") from exc
        return SpeechResult(audio=audio, mime_type="audio/wav")
