"""Optional SIP telephone endpoint backed by baresip-python 0.6.0.

One registered SIP user agent, no PBX process or new AI/voice runtime.
Importing this module NEVER registers or dials. Opening is explicit and
requires a configured provider account. Incoming calls require an injected
authorization predicate; external dialling requires a separate permission
predicate. Audio is transient PCM; nothing is recorded or persisted.

baresip-python is BSD-2 licensed and is an *optional* install extra.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from io import BytesIO
import re
from typing import Callable
from uuid import uuid4
import wave

from .contracts import InboundEvent

_SIP_URI = re.compile(r"^sips?:[^@\s]+@[^@\s]+$", re.IGNORECASE)
_MAX_WAV = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SipCredentials:
    user: str
    domain: str
    password: str = field(repr=False)

    def __post_init__(self):
        if (not self.user or not self.domain or not self.password
                or any(ch.isspace() for ch in self.user + self.domain)
                or "@" in self.user or "://" in self.domain):
            raise ValueError("invalid_sip_credentials")


@dataclass(frozen=True, slots=True)
class SipCallIdentity:
    tenant_id: str
    call_id: str
    peer: str
    local_address: str
    direction: str


class SipAudioSession:
    """A thin, transient wrapper over an established baresip Call.

    Audio is 16-bit little-endian PCM at the negotiated rates. No automatic
    recording, sample rate guesses, STT, TTS or interruption semantics.
    """

    def __init__(self, call, identity: SipCallIdentity):
        self._call = call
        self.identity = identity

    @property
    def audio_info(self):
        return self._call.audio.info()

    def read_pcm(self, max_bytes: int = 4096) -> bytes:
        if not isinstance(max_bytes, int) or not 2 <= max_bytes <= 65536 or max_bytes % 2:
            raise ValueError("invalid_pcm_read_size")
        data = self._call.audio.read(max_bytes)
        if not isinstance(data, bytes) or len(data) > max_bytes:
            raise RuntimeError("sip_invalid_audio_frame")
        return data

    def write_pcm(self, pcm: bytes) -> int:
        if not isinstance(pcm, bytes) or not 0 < len(pcm) <= 65536 or len(pcm) % 2:
            raise ValueError("invalid_pcm_frame")
        return self._call.audio.write(pcm)

    async def play_wav(self, wav_data: bytes) -> int:
        """Send a pre-approved, 16-bit WAV only if rates/channels match SIP.

        Transcoding is NOT guessed. The caller must configure the voice output
        to match the negotiated SIP format or provide an approved converter.
        """
        if not isinstance(wav_data, bytes) or not 44 <= len(wav_data) <= _MAX_WAV:
            raise ValueError("invalid_wav_size")
        try:
            with wave.open(BytesIO(wav_data), "rb") as reader:
                info = self.audio_info
                if (reader.getcomptype() != "NONE" or reader.getsampwidth() != 2
                        or reader.getframerate() != info.tx_sample_rate
                        or reader.getnchannels() != info.tx_channels):
                    raise ValueError("sip_wav_format_mismatch")
                channels = reader.getnchannels()
                frames_per_chunk = max(1, reader.getframerate() // 50)  # 20 ms
                total = 0
                while True:
                    pcm = reader.readframes(frames_per_chunk)
                    if not pcm:
                        break
                    if len(pcm) % (2 * channels):
                        raise ValueError("invalid_wav_frame_alignment")
                    written = self.write_pcm(pcm)
                    if written != len(pcm):
                        raise RuntimeError("sip_audio_backpressure")
                    total += written
                    await asyncio.sleep(0.02)
                return total
        except (wave.Error, EOFError) as exc:
            raise ValueError("invalid_wav") from exc

    async def hangup(self):
        await self._call.hangup()


class SipLine:
    """Explicitly open/close one SIP account, with a single active call.

    The endpoint does NOT create an autonomous agent. Applications own the
    authorized handler for each returned SipAudioSession, and remain responsible
    for consent, caller verification, the conversation and any CRM operations.
    """

    def __init__(
        self,
        credentials: SipCredentials,
        *,
        tenant_id: str,
        authorize_incoming: Callable[[InboundEvent], bool],
        authorize_outgoing: Callable[[str], bool] | None = None,
        backend=None,
    ):
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_scope_required")
        if not callable(authorize_incoming):
            raise ValueError("incoming_authorization_required")
        self.credentials = credentials
        self.tenant_id = tenant_id
        self.authorize_incoming = authorize_incoming
        self.authorize_outgoing = authorize_outgoing
        self._backend = backend
        self._runtime = None
        self._ua = None
        self._incoming = asyncio.Queue(maxsize=8)
        self._active = None

    async def open(self):
        if self._runtime is not None:
            raise RuntimeError("sip_line_already_started")
        if self._backend is None:
            try:
                import baresip  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError("sip_optional_dependency_missing") from exc
            if getattr(baresip, "__version__", None) != "0.6.0":
                raise RuntimeError("sip_backend_version_unverified")
            self._backend = baresip
        backend = self._backend
        runtime = backend.Runtime()
        self._runtime = runtime
        try:
            await runtime.start(backend.Config())
            account = backend.Account(
                user=self.credentials.user,
                password=self.credentials.password,
                domain=self.credentials.domain,
            )
            self._ua = await backend.UserAgent.create(runtime, account)
            self._ua.on_incoming(self._enqueue_incoming)
            await self._ua.register()
        except Exception:
            await self.close()
            raise
        return self

    def _enqueue_incoming(self, call):
        try:
            self._incoming.put_nowait(call)
        except asyncio.QueueFull:
            # Callback cannot block. No implicit accepted call or unbounded queue.
            asyncio.create_task(call.hangup())

    async def next_incoming(self, *, timeout: float | None = None) -> SipAudioSession | None:
        if self._ua is None:
            raise RuntimeError("sip_line_not_started")
        if timeout is not None and (timeout <= 0 or timeout > 3600):
            raise ValueError("invalid_sip_wait")
        call = await asyncio.wait_for(self._incoming.get(), timeout)
        identity = SipCallIdentity(
            tenant_id=self.tenant_id,
            call_id="sip-" + uuid4().hex,
            peer=str(call.peer),
            local_address=self.credentials.user + "@" + self.credentials.domain,
            direction="incoming",
        )
        event = InboundEvent(
            tenant_id=identity.tenant_id,
            channel="phone",
            external_id=identity.call_id,
            sender=identity.peer,
            recipient=identity.local_address,
            text="",
            kind="call_started",
        )
        if self._active is not None or not self.authorize_incoming(event):
            await call.hangup()
            return None
        self._active = call
        self._watch_close(call)
        try:
            await call.answer()
            await call.wait_established()
        except Exception:
            self._active = None
            try:
                await call.hangup()
            except Exception:
                pass
            raise
        return SipAudioSession(call, identity)

    async def dial(self, destination: str) -> SipAudioSession:
        """Explicit supervised outbound call; NOT an autonomous retry endpoint.

        Does not promise durable outbound exactly-once across process crashes.
        Call only from an existing permissioned, idempotent FruitStack action.
        """
        if self._ua is None:
            raise RuntimeError("sip_line_not_started")
        if not isinstance(destination, str) or not _SIP_URI.fullmatch(destination):
            raise ValueError("invalid_sip_destination")
        if self.authorize_outgoing is None or not self.authorize_outgoing(destination):
            raise PermissionError("sip_outbound_not_authorized")
        if self._active is not None:
            raise RuntimeError("sip_line_busy")
        call = await self._ua.dial(destination)
        self._active = call
        self._watch_close(call)
        try:
            await call.wait_established()
        except Exception:
            self._active = None
            try:
                await call.hangup()
            except Exception:
                pass
            raise
        identity = SipCallIdentity(
            tenant_id=self.tenant_id,
            call_id="sip-" + uuid4().hex,
            peer=destination,
            local_address=self.credentials.user + "@" + self.credentials.domain,
            direction="outgoing",
        )
        return SipAudioSession(call, identity)

    def _watch_close(self, call):
        def closed(event):
            if event.event is self._backend.Event.CALL_CLOSED and self._active is call:
                self._active = None
        call.on(closed)

    async def close(self):
        active = self._active
        self._active = None
        if active is not None:
            try:
                await active.hangup()
            except Exception:
                pass
        if self._ua is not None:
            try:
                await self._ua.unregister()
            except Exception:
                pass
            self._ua = None
        if self._runtime is not None:
            await self._runtime.close()
            self._runtime = None

    async def __aenter__(self):
        return await self.open()

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()
