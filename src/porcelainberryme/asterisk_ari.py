"""Host-authorized Asterisk ARI bridge to existing slin16 voice media.

No SIP account creation, number assignment, outbound PSTN dialer or new
public listener. A trusted ARI/Stasis owner supplies the existing channel
identity and independently verifies permission before this module runs.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlencode
from uuid import UUID

from .asterisk_worker import serve_asterisk_media
from .voice_livekit_agent import prepare_local_voice

_CHANNEL = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_APP = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ARIProtocolError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ARIMediaBinding:
    caller_id: str
    bridge_id: str
    media_channel_id: str
    connection_id: str


class AsteriskARI:
    """Strict loopback-only ARI client, no arbitrary endpoints or credentials."""

    def __init__(self, *, user: str, password: str, port: int = 8088,
                 timeout: float = 5.0):
        if (not isinstance(user, str) or not user or len(user) > 128
                or not isinstance(password, str) or not password
                or len(password) > 256):
            raise ValueError("trusted_ari_credentials_required")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid_ari_local_port")
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 15:
            raise ValueError("invalid_ari_timeout")
        self.user = user
        self.password = password
        self.port = port
        self.timeout = float(timeout)

    async def _call(self, session, method: str, endpoint: str,
                    query: dict | None = None):
        # Endpoint is constructed exclusively from validated, host-owned
        # identifiers. Never accept a URL/tool string from an LLM.
        from aiohttp import BasicAuth, ClientError
        if method not in ("GET", "POST", "DELETE") or not endpoint.startswith("/"):
            raise ValueError("invalid_local_ari_operation")
        url = f"http://127.0.0.1:{self.port}/ari{endpoint}"
        if query:
            url += "?" + urlencode(query)
        try:
            async with session.request(
                    method, url, auth=BasicAuth(self.user, self.password),
                    timeout=self.timeout, allow_redirects=False) as response:
                if not 200 <= response.status < 300:
                    raise ARIProtocolError("asterisk_ari_request_unconfirmed")
                payload = await response.content.read(65537)
                if len(payload) > 65536:
                    raise ARIProtocolError("asterisk_ari_response_too_large")
                if not payload:
                    return None
                try:
                    return await response.json(content_type=None)
                except (ValueError, TypeError) as exc:
                    raise ARIProtocolError("asterisk_ari_invalid_json") from exc
        except (ClientError, TimeoutError, OSError) as exc:
            # Do NOT automatically retry: ARI may have already performed
            # channel/bridge effects despite a dropped HTTP acknowledgement.
            raise ARIProtocolError("asterisk_ari_outcome_unknown") from exc

    async def prepare(self, session, *, caller_channel_id: str,
                      stasis_app: str, authorize_channel) -> ARIMediaBinding:
        if not isinstance(caller_channel_id, str) or not _CHANNEL.fullmatch(caller_channel_id):
            raise ValueError("trusted_ari_caller_channel_required")
        if not isinstance(stasis_app, str) or not _APP.fullmatch(stasis_app):
            raise ValueError("trusted_ari_stasis_app_required")
        if not callable(authorize_channel):
            raise PermissionError("trusted_ari_caller_authorizer_required")
        caller = await self._call(session, "GET", "/channels/" + caller_channel_id)
        if (not isinstance(caller, dict) or caller.get("id") != caller_channel_id
                or authorize_channel(caller) is not True):
            raise PermissionError("asterisk_caller_not_authorized")

        # Only resources returned by this operation may be torn down later.
        # A partial/ambiguous ARI response is never transparently retried.
        bridge_id = media_id = None
        try:
            bridge = await self._call(
                session, "POST", "/bridges", {"type": "mixing"})
            if not isinstance(bridge, dict) or not _CHANNEL.fullmatch(
                    str(bridge.get("id") or "")):
                raise ARIProtocolError("asterisk_ari_bridge_unconfirmed")
            bridge_id = bridge["id"]
            await self._call(
                session, "POST", f"/bridges/{bridge_id}/addChannel",
                {"channel": caller_channel_id})
            media = await self._call(
                session, "POST", "/channels/externalMedia", {
                    "app": stasis_app, "external_host": "INCOMING",
                    "transport": "websocket", "encapsulation": "none",
                    "connection_type": "server", "format": "slin16",
                    "direction": "both", "transport_data": "f(json)",
                })
            if not isinstance(media, dict) or not _CHANNEL.fullmatch(
                    str(media.get("id") or "")):
                raise ARIProtocolError("asterisk_ari_media_unconfirmed")
            media_id = media["id"]
            await self._call(
                session, "POST", f"/bridges/{bridge_id}/addChannel",
                {"channel": media_id})
            variable = await self._call(
                session, "GET", f"/channels/{media_id}/variable",
                {"variable": "MEDIA_WEBSOCKET_CONNECTION_ID"})
            raw = variable.get("value") if isinstance(variable, dict) else None
            try:
                connection = UUID(raw)
            except (ValueError, TypeError, AttributeError) as exc:
                raise ARIProtocolError("asterisk_media_connection_id_missing") from exc
            if str(connection) != raw.lower():
                raise ARIProtocolError("asterisk_media_connection_id_invalid")
            return ARIMediaBinding(
                caller_id=caller_channel_id, bridge_id=bridge_id,
                media_channel_id=media_id, connection_id=raw,
            )
        except BaseException:
            # ARI cleanup is best-effort. The original caller is NEVER hung
            # up here: the authoritative Stasis host owns that call.
            await self._cleanup(session, media_id=media_id, bridge_id=bridge_id)
            raise

    async def _cleanup(self, session, *, media_id, bridge_id):
        for path in (
                f"/channels/{media_id}" if media_id else None,
                f"/bridges/{bridge_id}" if bridge_id else None):
            if path:
                try:
                    await self._call(session, "DELETE", path)
                except (ARIProtocolError, ValueError):
                    pass

    async def cleanup(self, session, binding: ARIMediaBinding):
        if not isinstance(binding, ARIMediaBinding):
            raise TypeError("trusted_ari_binding_required")
        await self._cleanup(
            session, media_id=binding.media_channel_id,
            bridge_id=binding.bridge_id,
        )


async def serve_authorized_ari_call(*, ari: AsteriskARI, session,
                                    caller_channel_id: str,
                                    stasis_app: str, authorize_channel,
                                    tenant: str, agent_id: str,
                                    whisper_model: str, ollama_model: str,
                                    piper_binary: str, piper_model: str):
    """Bridge exactly one authorized existing SIP call to local speech.

    Audio models are preflighted before Asterisk is asked to bridge media.
    SIP/PSTN access and Stasis event ownership must already exist in host.
    """
    resources = await prepare_local_voice(
        tenant=tenant, agent_id=agent_id, whisper_model=whisper_model,
        ollama_model=ollama_model, piper_binary=piper_binary,
        piper_model=piper_model,
    )
    binding = await ari.prepare(
        session, caller_channel_id=caller_channel_id,
        stasis_app=stasis_app, authorize_channel=authorize_channel,
    )
    try:
        await serve_asterisk_media(
            channel_id=binding.media_channel_id,
            connection_id=binding.connection_id,
            port=ari.port, tenant=tenant, agent_id=agent_id,
            whisper_model=whisper_model, ollama_model=ollama_model,
            piper_binary=piper_binary, piper_model=piper_model,
            local_resources=resources,
        )
    finally:
        await ari.cleanup(session, binding)
