"""Autonomous incoming Internet-call receptionist, reusing PorcelainberryMe RTC.

Run as a trusted local process beside the existing call-control API and the
self-hosted LiveKit server. The worker does NOT originate any calls. It
monitors only its signed agent identity's incoming invitations and accepts
them atomically under a durable fenced 15-second lease, then speaks using
the SAME locally installed Whisper/Ollama(or approved FAQ)/Piper stack as
the existing outbound worker.

The worker holds the local call-signing secret to mint fresh, short-lived
agent-only identity JWTs; do not expose that secret to a public browser or
remote customer. No phone number, PSTN, cloud AI or paid API is involved.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from .ip_calls import CALL_ID, mint_identity_token
from .voice_livekit_agent import (
    CallControlClient, LiveKitAgentCall, make_call_responder,
    prepare_local_voice,
)


def is_inbound_invitation(event) -> bool:
    if not isinstance(event, dict) or event.get("kind") != "RINGING":
        return False
    call = event.get("call")
    return (
        isinstance(call, dict)
        and isinstance(call.get("id"), str)
        and CALL_ID.fullmatch(call["id"]) is not None
        and call.get("direction") == "incoming"
        and call.get("agent_initiated") is False
    )


def _held(exc) -> bool:
    return (
        isinstance(exc, RuntimeError)
        and str(exc) == "call_control_inbound_lease_held"
    )


async def serve_inbound(*, tenant: str, agent_id: str,
                        identity_secret: str,
                        whisper_model: str, ollama_model: str,
                        piper_binary: str, piper_model: str,
                        client=None, rtc=None, local_resources=None,
                        stop_event: asyncio.Event | None = None,
                        max_sessions: int | None = None,
                        poll_seconds: float = 0.5):
    """Process bound incoming invitations; one call/agent identity at a time.

    Optional client, rtc and local_resources are test injection points only:
    production always uses real loopback HTTP, LiveKit and local models.
    """
    if not tenant or not agent_id or not isinstance(identity_secret, str) or len(identity_secret) < 32:
        raise ValueError("trusted_inbound_agent_identity_required")
    if type(max_sessions) is not int and max_sessions is not None:
        raise ValueError("invalid_max_sessions")
    if max_sessions is not None and max_sessions < 1:
        raise ValueError("invalid_max_sessions")
    if not 0.1 <= poll_seconds <= 5:
        raise ValueError("invalid_poll_seconds")

    if client is None:
        # Restricted to this configured AI identity. A fresh signed token
        # per local HTTP action avoids a stale token on a long-lived daemon.
        client = CallControlClient(
            "", token_factory=lambda: mint_identity_token(
                identity_secret, tenant, agent_id, "agent", lifetime=600,
            ),
        )
    who = await client.whoami()
    if (who.get("role") != "agent" or who.get("tenant") != tenant
            or who.get("subject") != agent_id):
        raise PermissionError("verified_inbound_agent_identity_mismatch")
    if rtc is None:
        from livekit import rtc as rtc_module
        rtc = rtc_module

    # Fail before accepting any incoming invitation if model files or the
    # locally installed inference service are absent. No auto-download.
    if local_resources is None:
        stt, base_llm, tts = await prepare_local_voice(
            tenant=tenant, agent_id=agent_id,
            whisper_model=whisper_model, ollama_model=ollama_model,
            piper_binary=piper_binary, piper_model=piper_model,
        )
    else:
        stt, base_llm, tts = local_resources

    cursor = 0
    sessions = 0
    errors = 0
    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        try:
            data = await client.inbox(cursor)
            errors = 0
        except (OSError, RuntimeError, TimeoutError):
            # A temporary control-plane outage must never invent an answer
            # or silently accept an unverified call. Continue with backoff.
            errors += 1
            await asyncio.sleep(min(10.0, 2 ** min(errors - 1, 4)))
            continue
        events = data.get("events", [])
        if not isinstance(events, list):
            raise RuntimeError("malformed_inbound_event_inbox")
        blocked = False
        for event in events:
            seq = event.get("cursor", cursor)
            if type(seq) is not int or seq <= cursor:
                raise RuntimeError("invalid_inbound_event_cursor")
            if is_inbound_invitation(event):
                call_id = event["call"]["id"]
                try:
                    claimed = await client.claim_inbound(call_id)
                except Exception as exc:
                    if _held(exc):
                        # Do NOT advance cursor. After the crashed worker's
                        # lease expires we must retry this exact invitation.
                        blocked = True
                        break
                    if isinstance(exc, RuntimeError) and str(exc).startswith(
                        "call_control_"
                    ):
                        # Unavailable, rejected, stale or disabled invite.
                        # These are controlled server-side state decisions.
                        cursor = seq
                        continue
                    raise
                lease = claimed["lease"]
                caller = claimed["peer"]
                if not isinstance(lease, str) or len(lease) != 32:
                    raise RuntimeError("server_returned_invalid_inbound_lease")
                # Fresh mutable appointment / notes / consent / handoff
                # state for EACH call. No consent is inherited across calls.
                try:
                    client.inbound_lease = lease
                    llm = make_call_responder(
                        base_llm=base_llm, tenant=tenant, agent_id=agent_id,
                        caller=caller, client=client,
                    )
                    bind = getattr(llm, "set_call_id", None)
                    if bind is not None:
                        bind(call_id)
                    session = LiveKitAgentCall(
                        client=client, tenant=tenant, callee=caller,
                        stt=stt, llm=llm, tts=tts, rtc=rtc,
                        greeting=getattr(llm, "greeting", None)
                        or "Guten Tag. Wie kann ich Ihnen helfen?",
                    )
                    await session.speak(
                        call_id,
                        heartbeat=lambda: client.heartbeat_inbound(
                            call_id, lease,
                        ),
                    )
                except Exception as exc:
                    if max_sessions is not None:
                        raise
                    # Never log a call ID, transcript, person or bearer token.
                    print("inbound_voice_session_error:" +
                          type(exc).__name__, file=sys.stderr)
                finally:
                    client.inbound_lease = None
                    # Fenced completion cannot end a conversation taken over
                    # by a newer worker or successfully handed to staff.
                    try:
                        await client.finish_inbound(call_id, lease)
                    except Exception as exc:
                        print("inbound_voice_finish_error:" +
                              type(exc).__name__, file=sys.stderr)
                sessions += 1
                cursor = seq
                if max_sessions is not None and sessions >= max_sessions:
                    return sessions
            else:
                cursor = seq
        if blocked or not events:
            await asyncio.sleep(poll_seconds)
    return sessions


def main():
    parser = argparse.ArgumentParser(
        description="Self-hosted inbound AI receptionist for Internet identities"
    )
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--agent", required=True)
    args = parser.parse_args()
    secret = os.environ.get("PB_CALL_IDENTITY_SECRET", "")
    settings = {
        "whisper_model": os.environ.get("PB_LOCAL_WHISPER_DIR", ""),
        "ollama_model": os.environ.get("PB_LOCAL_OLLAMA_MODEL", ""),
        "piper_binary": os.environ.get("PB_LOCAL_PIPER_BIN", ""),
        "piper_model": os.environ.get("PB_LOCAL_PIPER_MODEL", ""),
    }
    if len(secret) < 32:
        parser.error("PB_CALL_IDENTITY_SECRET required in trusted local worker")
    if any(not settings[k] for k in ("whisper_model", "piper_binary", "piper_model")):
        parser.error("locally installed Whisper and Piper models are required")
    if not settings["ollama_model"] and not os.environ.get("PB_AGENT_KNOWLEDGE_DB"):
        parser.error("local Ollama model or approved knowledge DB required")
    asyncio.run(serve_inbound(
        tenant=args.tenant, agent_id=args.agent,
        identity_secret=secret, **settings,
    ))


if __name__ == "__main__":
    main()
