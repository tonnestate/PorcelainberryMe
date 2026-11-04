"""Connect approved Asterisk chan_websocket media to the existing voice engine.

Only a trusted ARI/SIP host may pass an already-created channel/connection.
Asterisk remains responsible for SIP, ring/answer, trunks and call policy.
This process neither dials a number nor exposes any public HTTP listener.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
from uuid import UUID

from .asterisk_ws import AsteriskVoiceMedia
from .voice_livekit_agent import prepare_local_voice

_TRUSTED_CHANNEL = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def validate_asterisk_binding(*, channel_id: str, connection_id: str,
                              port: int) -> str:
    if not isinstance(channel_id, str) or not _TRUSTED_CHANNEL.fullmatch(channel_id):
        raise ValueError("trusted_ari_channel_required")
    if not isinstance(connection_id, str):
        raise ValueError("trusted_asterisk_connection_id_required")
    try:
        parsed = UUID(connection_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("trusted_asterisk_connection_id_required") from exc
    if str(parsed) != connection_id.lower():
        raise ValueError("trusted_asterisk_connection_id_required")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid_asterisk_local_port")
    return f"ws://127.0.0.1:{port}/media/{connection_id}"


async def serve_asterisk_media(*, channel_id: str, connection_id: str,
                               tenant: str, agent_id: str,
                               whisper_model: str, ollama_model: str,
                               piper_binary: str, piper_model: str,
                               port: int = 8088, local_resources=None):
    """Run one Asterisk-provisioned channel with locally installed speech.

    An upstream authenticated host must have obtained channel and ephemeral
    media connection IDs from ARI. They are NOT supplied by MCP/LLM requests.
    """
    url = validate_asterisk_binding(
        channel_id=channel_id, connection_id=connection_id, port=port,
    )
    if not isinstance(tenant, str) or not _TRUSTED_CHANNEL.fullmatch(tenant):
        raise ValueError("trusted_asterisk_tenant_required")
    if not isinstance(agent_id, str) or not _TRUSTED_CHANNEL.fullmatch(agent_id):
        raise ValueError("trusted_asterisk_assistant_required")

    # Local model verification before connecting to media; no model download.
    if local_resources is None:
        stt, llm, tts = await prepare_local_voice(
            tenant=tenant, agent_id=agent_id, whisper_model=whisper_model,
            ollama_model=ollama_model, piper_binary=piper_binary,
            piper_model=piper_model,
        )
    else:
        stt, llm, tts = local_resources
    try:
        import aiohttp
    except ImportError as exc:
        raise RuntimeError("local_websocket_client_not_installed") from exc

    user = os.environ.get("PB_ASTERISK_WS_USER", "")
    password = os.environ.get("PB_ASTERISK_WS_PASSWORD", "")
    if bool(user) != bool(password):
        raise ValueError("asterisk_ws_auth_pair_required")
    auth = aiohttp.BasicAuth(user, password) if user else None
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(
                url, auth=auth, heartbeat=20, max_msg_size=65500) as ws:
            bridge = AsteriskVoiceMedia(
                ws, expected_channel_id=channel_id,
                expected_connection_id=connection_id,
                stt=stt, llm=llm, tts=tts,
            )
            await bridge.run()


def main():
    parser = argparse.ArgumentParser(
        description="Trusted precreated Asterisk slin16 media channel worker",
    )
    args = parser.parse_args()
    if os.environ.get("PB_ASTERISK_MEDIA_ENABLED") != "1":
        parser.error("PB_ASTERISK_MEDIA_ENABLED=1 required by trusted host")
    fields = {
        "channel_id": os.environ.get("PB_ASTERISK_CHANNEL_ID", ""),
        "connection_id": os.environ.get("PB_ASTERISK_CONNECTION_ID", ""),
        "tenant": os.environ.get("PB_ASTERISK_TENANT", ""),
        "agent_id": os.environ.get("PB_ASTERISK_ASSISTANT", ""),
        "whisper_model": os.environ.get("PB_LOCAL_WHISPER_DIR", ""),
        "ollama_model": os.environ.get("PB_LOCAL_OLLAMA_MODEL", ""),
        "piper_binary": os.environ.get("PB_LOCAL_PIPER_BIN", ""),
        "piper_model": os.environ.get("PB_LOCAL_PIPER_MODEL", ""),
    }
    if (any(not fields[k] for k in (
            "channel_id", "connection_id", "tenant", "agent_id",
            "whisper_model", "piper_binary", "piper_model"))
            or not (fields["ollama_model"]
                    or os.environ.get("PB_LOCAL_VOICE_CHAT_MODEL")
                    or os.environ.get("PB_AGENT_KNOWLEDGE_DB"))):
        parser.error("authenticated ARI channel and installed local voice resources required")
    try:
        port = int(os.environ.get("PB_ASTERISK_WS_PORT", "8088"))
        validate_asterisk_binding(
            channel_id=fields["channel_id"],
            connection_id=fields["connection_id"], port=port,
        )
    except (ValueError, TypeError) as exc:
        parser.error(str(exc))
    asyncio.run(serve_asterisk_media(**fields, port=port))


if __name__ == "__main__":
    main()
