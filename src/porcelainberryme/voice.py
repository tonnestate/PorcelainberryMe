"""Optional bridge to the previously approved Python voice source.

No speech engine is selected, installed, downloaded, or silently substituted.
Bind the actual reviewed callable only after source and sample verification.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


class VoiceUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SpeechResult:
    audio: bytes
    mime_type: str


class PythonVoiceBridge:
    def __init__(self, synthesize: Callable[[str], SpeechResult] | None = None):
        self._synthesize = synthesize

    @property
    def ready(self) -> bool:
        return self._synthesize is not None

    def speak(self, text: str) -> SpeechResult:
        if self._synthesize is None:
            raise VoiceUnavailable("VOICE_SOURCE_NOT_VERIFIED")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
            raise ValueError("invalid_voice_text")
        result = self._synthesize(text)
        if (not isinstance(result, SpeechResult) or
                not isinstance(result.audio, bytes) or
                not 0 < len(result.audio) <= 10 * 1024 * 1024 or
                result.mime_type not in {"audio/wav", "audio/ogg", "audio/mpeg"}):
            raise VoiceUnavailable("VOICE_PROVIDER_INVALID_OUTPUT")
        return result
