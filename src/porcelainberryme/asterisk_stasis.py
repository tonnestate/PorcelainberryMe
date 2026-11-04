"""Supervised local ARI Stasis listener for preauthorized SIP voice routes.

No public REST listener, dialer, trunk registration or model-created phone
numbers. The trusted Asterisk dialplan must explicitly route selected calls
into the configured Stasis application/context/extension. An ARI GET of the
original channel rechecks that route before any bridge/media side effect.
Caller-ID is NEVER treated as permission to use private FruitStack tools.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
from collections import deque

from .asterisk_ari import AsteriskARI, serve_authorized_ari_call
from .voice_livekit_agent import prepare_local_voice

_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_CONTEXT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_EXTENSION = re.compile(r"^[A-Za-z0-9_+*#.-]{1,64}$")


class StasisListenerError(RuntimeError):
    pass


class TrustedStasisRoute:
    """Permit only an explicitly configured, Asterisk-controlled route."""

    def __init__(self, *, app: str, context: str, extension: str):
        if not isinstance(app, str) or not _CONTEXT.fullmatch(app):
            raise ValueError("stasis_app_required")
        if not isinstance(context, str) or not _CONTEXT.fullmatch(context):
            raise ValueError("trusted_dialplan_context_required")
        if not isinstance(extension, str) or not _EXTENSION.fullmatch(extension):
            raise ValueError("trusted_dialplan_extension_required")
        self.app, self.context, self.extension = app, context, extension

    def allows(self, channel, *, expected_id=None):
        if not isinstance(channel, dict):
            return False
        identity = channel.get("id")
        dialplan = channel.get("dialplan")
        return (
            isinstance(identity, str) and bool(_ID.fullmatch(identity))
            and (expected_id is None or identity == expected_id)
            and channel.get("state") in ("Ring", "Ringing", "Up")
            and isinstance(dialplan, dict)
            and dialplan.get("context") == self.context
            and dialplan.get("exten") == self.extension
        )

    def inbound_channel(self, event):
        if (not isinstance(event, dict)
                or event.get("type") != "StasisStart"
                or event.get("application") != self.app):
            return None
        channel = event.get("channel")
        if self.allows(channel):
            return channel["id"]
        return None


class AsteriskStasisListener:
    """Bounded ARI listener; each event is reauthorized via ARI GET.

    A reconnect only subscribes to new events. It NEVER retries a call that
    may already be in progress. On shutdown all owned media tasks are
    cancelled, allowing their finally blocks to release ARI resources.
    """

    def __init__(self, *, ari: AsteriskARI, route: TrustedStasisRoute,
                 tenant: str, assistant: str, voice_settings: dict,
                 max_calls: int = 1):
        if not isinstance(ari, AsteriskARI):
            raise TypeError("trusted_asterisk_ari_required")
        if not isinstance(route, TrustedStasisRoute):
            raise TypeError("trusted_stasis_route_required")
        if type(max_calls) is not int or not 1 <= max_calls <= 4:
            raise ValueError("stasis_max_calls_out_of_range")
        if not isinstance(tenant, str) or not _ID.fullmatch(tenant):
            raise ValueError("stasis_trusted_tenant_required")
        if not isinstance(assistant, str) or not _ID.fullmatch(assistant):
            raise ValueError("stasis_trusted_assistant_required")
        if not isinstance(voice_settings, dict) or set(voice_settings) != {
            "whisper_model", "ollama_model", "piper_binary", "piper_model",
        }:
            raise ValueError("stasis_voice_settings_required")
        self.ari, self.route = ari, route
        self.tenant, self.assistant = tenant, assistant
        self.voice_settings = dict(voice_settings)
        self.max_calls = max_calls
        self._calls: dict[str, asyncio.Task] = {}
        self._busy_returns: dict[str, asyncio.Task] = {}
        # An acknowledged /continue itself produces StasisEnd. Do not
        # cancel the task carrying that HTTP result or release may be lost.
        self._returning: set[str] = set()
        # A single subscribed channel can safely reuse one locally loaded
        # STT/LLM/TTS set, avoiding cold-start after each incoming ring.
        # Multi-call hosts retain per-call resources to prevent shared model
        # state from leaking between simultaneously active conversations.
        self._prepared_resources = None
        self._seen: deque[str] = deque(maxlen=4096)
        self.dispatched = 0
        self.rejected = 0
        self.failed = 0

    def _finished(self, channel_id, task):
        self._calls.pop(channel_id, None)
        if not task.cancelled():
            try:
                task.result()
            except Exception:
                self.failed += 1
                # No channel, Caller-ID, token, transcript or remote response.
                print("asterisk_stasis_session_failed", file=sys.stderr)

    async def _return_caller(self, session, channel_id) -> bool:
        """Return an exact verified Stasis channel to its trusted dialplan.

        This performs no arbitrary hangup and does not retry ambiguous ARI
        effects. The host dialplan determines its next step.
        """
        # Lock the *entire* GET-and-continue sequence against StasisEnd.
        # The event means the caller left the app, which is exactly what
        # this authoritative continuation is trying to accomplish.
        self._returning.add(channel_id)
        try:
            current = await self.ari._call(
                session, "GET", f"/channels/{channel_id}",
            )
            if not self.route.allows(current, expected_id=channel_id):
                return False
            await self.ari._call(
                session, "POST", f"/channels/{channel_id}/continue",
            )
            return True
        except Exception:
            # Disconnected/unverified channels are not force-hung-up.
            print("asterisk_stasis_return_unconfirmed", file=sys.stderr)
            return False
        finally:
            self._returning.discard(channel_id)

    async def _reject_busy(self, channel_id):
        """Release only an independently reverified capacity-excess call."""
        try:
            import aiohttp
        except ImportError as exc:
            raise StasisListenerError("asterisk_aiohttp_not_installed") from exc
        async with aiohttp.ClientSession() as session:
            await self._return_caller(session, channel_id)

    def _busy_return_finished(self, channel_id, task):
        self._busy_returns.pop(channel_id, None)
        if not task.cancelled():
            try:
                task.result()
            except Exception:
                print("asterisk_stasis_overload_return_failed",
                      file=sys.stderr)

    async def _prepare_resources_once(self):
        """Fail before subscribing or ringing if local voice is unavailable.

        Default max_calls=1: load once, then reuse sequentially. The
        explicit multi-call mode continues using isolated per-call models.
        """
        if self.max_calls == 1 and self._prepared_resources is None:
            self._prepared_resources = await prepare_local_voice(
                tenant=self.tenant, agent_id=self.assistant,
                **self.voice_settings,
            )

    async def _serve_one(self, channel_id):
        try:
            import aiohttp
        except ImportError as exc:
            raise StasisListenerError("asterisk_aiohttp_not_installed") from exc
        # Per-call HTTP session isolates timeouts and cleanup. The existing
        # ARI adapter owns media bridges and never terminates the caller.
        async with aiohttp.ClientSession() as session:
            try:
                await serve_authorized_ari_call(
                    ari=self.ari, session=session, caller_channel_id=channel_id,
                    stasis_app=self.route.app,
                    authorize_channel=lambda channel: self.route.allows(
                        channel, expected_id=channel_id),
                    tenant=self.tenant, agent_id=self.assistant,
                    local_resources=self._prepared_resources
                    if self.max_calls == 1 else None,
                    **self.voice_settings,
                )
            finally:
                # Returning to the next trusted dialplan step prevents
                # answered callers from becoming stuck after media cleanup.
                await self._return_caller(session, channel_id)

    def accept_event(self, event: dict) -> bool:
        """Bounded call admission and immediate peer-hangup cancellation.

        StasisEnd is authoritative only for an already owned channel in
        this subscribed application; it cannot redirect unrelated calls.
        """
        if (isinstance(event, dict)
                and event.get("type") == "StasisEnd"
                and event.get("application") == self.route.app):
            ended = event.get("channel")
            channel_id = ended.get("id") if isinstance(ended, dict) else None
            # Returning a caller to the dialplan is expected to emit
            # StasisEnd *before* the /continue response is delivered.
            # Cancelling here can corrupt the cleanup completion receipt.
            if isinstance(channel_id, str) and channel_id in self._returning:
                return False
            task = self._calls.get(channel_id) if isinstance(channel_id, str) else None
            if task is None and isinstance(channel_id, str):
                task = self._busy_returns.get(channel_id)
            if task is not None and not task.done():
                task.cancel()
                return True
            return False
        channel_id = self.route.inbound_channel(event)
        if channel_id is None:
            return False
        if channel_id in self._calls or channel_id in self._seen:
            self.rejected += 1
            return False
        self._seen.append(channel_id)
        if len(self._calls) >= self.max_calls:
            self.rejected += 1
            # Never answer or bridge an overload call. Release it via the
            # verified dialplan continuation, which can reject/fallback.
            # Bound cleanup tasks too, so a flood cannot exhaust the host.
            if len(self._busy_returns) < 4:
                task = asyncio.create_task(self._reject_busy(channel_id))
                self._busy_returns[channel_id] = task
                task.add_done_callback(
                    lambda done: self._busy_return_finished(channel_id, done),
                )
            return False
        task = asyncio.create_task(self._serve_one(channel_id))
        self._calls[channel_id] = task
        task.add_done_callback(
            lambda finished: self._finished(channel_id, finished),
        )
        self.dispatched += 1
        return True

    async def close(self):
        tasks = tuple(self._calls.values()) + tuple(self._busy_returns.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def serve(self, *, stop_event: asyncio.Event | None = None):
        """Connect only to local ARI. Reconnect without replaying events.

        A permanently wrong ARI password/app is a configuration error and
        fails closed. Temporary network disconnects wait before reconnect.
        """
        try:
            import aiohttp
        except ImportError as exc:
            raise StasisListenerError("asterisk_aiohttp_not_installed") from exc
        stop = stop_event if stop_event is not None else asyncio.Event()
        if stop.is_set():
            return
        # Preflight once before the Stasis websocket can admit calls.
        await self._prepare_resources_once()
        if stop.is_set():
            return
        url = f"ws://127.0.0.1:{self.ari.port}/ari/events"
        auth = aiohttp.BasicAuth(self.ari.user, self.ari.password)
        try:
            while not stop.is_set():
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.ws_connect(
                            url, params={"app": self.route.app},
                            auth=auth, heartbeat=20, max_msg_size=65536,
                        ) as ws:
                            while not stop.is_set():
                                try:
                                    message = await asyncio.wait_for(
                                        ws.receive(), timeout=1,
                                    )
                                except asyncio.TimeoutError:
                                    continue
                                if message.type == aiohttp.WSMsgType.TEXT:
                                    if len(message.data) > 65536:
                                        raise StasisListenerError("oversized_stasis_event")
                                    try:
                                        event = json.loads(message.data)
                                    except (ValueError, UnicodeError) as exc:
                                        raise StasisListenerError("invalid_stasis_event") from exc
                                    self.accept_event(event)
                                elif message.type in (
                                    aiohttp.WSMsgType.CLOSE,
                                    aiohttp.WSMsgType.CLOSED,
                                    aiohttp.WSMsgType.CLOSING,
                                    aiohttp.WSMsgType.ERROR,
                                ):
                                    break
                                else:
                                    raise StasisListenerError("unexpected_stasis_frame")
                except aiohttp.WSServerHandshakeError as exc:
                    if 400 <= exc.status < 500:
                        raise StasisListenerError("stasis_ari_configuration_refused") from exc
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
                    # A dropped ARI event stream is not authority to dial
                    # anything again; in-flight media work remains fenced.
                    pass
                if not stop.is_set():
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        pass
        finally:
            await self.close()


async def serve_with_signals(listener: AsteriskStasisListener) -> None:
    """Drain media sessions on systemd SIGTERM, not abrupt process death.

    Never expose a network shutdown endpoint. The existing listener owns
    only its own ARI channels and finishes them through its normal cleanup
    and trusted dialplan handoff. Windows event loops without signal
    support keep their standard KeyboardInterrupt handling.
    """
    if not isinstance(listener, AsteriskStasisListener):
        raise TypeError("trusted_stasis_listener_required")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop.set)
            registered.append(signum)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await listener.serve(stop_event=stop)
    finally:
        for signum in registered:
            loop.remove_signal_handler(signum)


def main():
    parser = argparse.ArgumentParser(
        description="Host-only Asterisk Stasis voice route listener",
    )
    args = parser.parse_args()
    if os.environ.get("PB_ASTERISK_STASIS_ENABLED") != "1":
        parser.error("PB_ASTERISK_STASIS_ENABLED=1 required")
    keys = {
        "user": os.environ.get("PB_ASTERISK_ARI_USER", ""),
        "password": os.environ.get("PB_ASTERISK_ARI_PASSWORD", ""),
        "app": os.environ.get("PB_ASTERISK_STASIS_APP", ""),
        "context": os.environ.get("PB_ASTERISK_TRUSTED_CONTEXT", ""),
        "extension": os.environ.get("PB_ASTERISK_TRUSTED_EXTENSION", ""),
        "tenant": os.environ.get("PB_ASTERISK_TENANT", ""),
        "assistant": os.environ.get("PB_ASTERISK_ASSISTANT", ""),
        "whisper_model": os.environ.get("PB_LOCAL_WHISPER_DIR", ""),
        "ollama_model": os.environ.get("PB_LOCAL_OLLAMA_MODEL", ""),
        "piper_binary": os.environ.get("PB_LOCAL_PIPER_BIN", ""),
        "piper_model": os.environ.get("PB_LOCAL_PIPER_MODEL", ""),
    }
    needed = ("user", "password", "app", "context", "extension",
              "tenant", "assistant", "whisper_model", "piper_binary",
              "piper_model")
    if (any(not keys[k] for k in needed)
            or not (keys["ollama_model"] or
                    os.environ.get("PB_LOCAL_VOICE_CHAT_MODEL") or
                    os.environ.get("PB_AGENT_KNOWLEDGE_DB"))):
        parser.error("trusted Stasis route and local voice configuration required")
    try:
        port = int(os.environ.get("PB_ASTERISK_ARI_PORT", "8088"))
        max_calls = int(os.environ.get("PB_ASTERISK_MAX_CALLS", "1"))
        ari = AsteriskARI(user=keys["user"], password=keys["password"], port=port)
        route = TrustedStasisRoute(
            app=keys["app"], context=keys["context"], extension=keys["extension"],
        )
        listener = AsteriskStasisListener(
            ari=ari, route=route, tenant=keys["tenant"],
            assistant=keys["assistant"],
            voice_settings={k: keys[k] for k in (
                "whisper_model", "ollama_model", "piper_binary", "piper_model",
            )},
            max_calls=max_calls,
        )
    except (ValueError, TypeError) as exc:
        parser.error(str(exc))
    try:
        asyncio.run(serve_with_signals(listener))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
