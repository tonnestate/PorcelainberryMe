"""Read-only MCP visibility into existing authenticated PorcelainberryMe calls.

No new call controller, customer directory, owner-harness authority, media
session or network listener. The stdio MCP process inherits a trusted
single-identity control token from its supervised FruitStack host. Tools
never accept a token/tenant/subject from the LLM or expose raw payloads.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import os
import re
from typing import Any

from .voice_livekit_agent import CallControlClient

_CALL_ID = re.compile(r"^[a-f0-9]{32}$")
_SUBJECT = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
_STATES = frozenset({"RINGING", "ACTIVE", "ENDED", "DECLINED", "TIMED_OUT", "HANDOFF_REVOKED"})
_DIRECTIONS = frozenset({"incoming", "outgoing"})


class MCPCallVisibility:
    """Tenant/identity-fenced adapter to the existing loopback call server.

    The underlying call-control server is authoritative for participant,
    tenant, and employee handoff ACLs. Re-check the token's identity on
    every tool request; never trust a caller-selected tenant or claim.
    """

    def __init__(self, control: CallControlClient, *, tenant: str,
                 subject: str, role: str, call_target: str = "",
                 call_request_id: str = "",
                 voice_launcher: Callable[[], Awaitable[None]] | None = None):
        if not isinstance(tenant, str) or not _SUBJECT.fullmatch(tenant):
            raise ValueError("mcp_trusted_tenant_required")
        if not isinstance(subject, str) or not _SUBJECT.fullmatch(subject):
            raise ValueError("mcp_trusted_subject_required")
        if role not in ("human", "agent"):
            raise ValueError("mcp_trusted_role_required")
        if not isinstance(control, CallControlClient):
            raise ValueError("mcp_authenticated_call_client_required")
        if voice_launcher is not None:
            if role != "agent":
                raise PermissionError("mcp_voice_job_agent_identity_required")
            if (not callable(voice_launcher)
                    or not _SUBJECT.fullmatch(call_target) or call_target == subject
                    or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", call_request_id)):
                raise ValueError("mcp_host_approved_voice_job_required")
        elif call_target or call_request_id:
            raise ValueError("mcp_outbound_voice_job_not_authorized")
        self._control = control
        self._scope = {"tenant": tenant, "subject": subject, "role": role}
        self._voice_launcher = voice_launcher
        self._call_request_id = call_request_id
        self._launch_task: asyncio.Task | None = None

    async def _verify_identity(self):
        identity = await self._control.whoami()
        if not isinstance(identity, dict) or any(
                identity.get(field) != expected
                for field, expected in self._scope.items()):
            raise PermissionError("mcp_call_identity_mismatch")

    @staticmethod
    def _visible(call: Any) -> dict:
        if not isinstance(call, dict):
            raise RuntimeError("mcp_call_contract_invalid")
        call_id, state = call.get("id"), call.get("state")
        if (not isinstance(call_id, str) or not _CALL_ID.fullmatch(call_id)
                or state not in _STATES):
            raise RuntimeError("mcp_call_contract_invalid")
        # No peer identity, private invitation, bearer, media credential,
        # transcript, note, contact data or internal employee reference.
        view = {"id": call_id, "state": state}
        if call.get("direction") in _DIRECTIONS:
            view["direction"] = call["direction"]
        handoff = call.get("handoff")
        if isinstance(handoff, dict):
            handoff_state = handoff.get("state")
            if handoff_state in ("OFFERED", "ACCEPTED", "CONNECTED",
                                 "CANCELLED", "DECLINED", "EXPIRED"):
                view["handoff_state"] = handoff_state
        return view

    @property
    def can_place_calls(self) -> bool:
        return self._voice_launcher is not None

    def _job_state(self) -> str:
        task = self._launch_task
        if task is None:
            return "NOT_REQUESTED"
        if not task.done():
            return "IN_PROGRESS"
        if task.cancelled():
            return "CANCELLED"
        if task.exception() is not None:
            return "FAILED"
        # Worker exit can also mean declined or unanswered; not delivery proof.
        return "FINISHED_UNVERIFIED"

    async def call_me(self) -> dict:
        """Start exactly one host-approved real voice worker; never accept a target from the LLM."""
        if not self.can_place_calls:
            raise PermissionError("mcp_outbound_voice_job_not_authorized")
        await self._verify_identity()
        if self._launch_task is None:
            task = asyncio.create_task(self._voice_launcher())
            # Consume errors without putting credentials or transcripts in logs.
            task.add_done_callback(
                lambda finished: None if finished.cancelled() else finished.exception()
            )
            self._launch_task = task
        return {
            "schema": "porcelainberryme.mcp.voice-job.v1",
            "request_id": self._call_request_id,
            "state": self._job_state(),
            "call_result_verified": False,
        }

    async def call_me_status(self) -> dict:
        if not self.can_place_calls:
            raise PermissionError("mcp_outbound_voice_job_not_authorized")
        await self._verify_identity()
        return {
            "schema": "porcelainberryme.mcp.voice-job.v1",
            "request_id": self._call_request_id,
            "state": self._job_state(),
            "call_result_verified": False,
        }

    async def capabilities(self) -> dict:
        await self._verify_identity()
        tools = ["porcelainberryme_capabilities",
                 "porcelainberryme_active_calls",
                 "porcelainberryme_call_status",
                 "porcelainberryme_call_events"]
        if self.can_place_calls:
            tools += ["porcelainberryme_call_me",
                      "porcelainberryme_call_me_status"]
        return {
            "schema": "porcelainberryme.mcp.capabilities.v1",
            "tenant": self._scope["tenant"],
            "subject": self._scope["subject"],
            "role": self._scope["role"],
            "transport": "internet_webrtc",
            "read_only": not self.can_place_calls,
            "tools": tools,
        }

    async def active_calls(self, limit: int = 20) -> dict:
        if type(limit) is not int or not 1 <= limit <= 30:
            raise ValueError("mcp_invalid_call_limit")
        await self._verify_identity()
        snapshot = await self._control.request("GET", "/v1/ip-calls/active")
        calls = snapshot.get("calls") if isinstance(snapshot, dict) else None
        if not isinstance(calls, list) or len(calls) > 100:
            raise RuntimeError("mcp_active_calls_contract_invalid")
        return {
            "schema": "porcelainberryme.mcp.calls.v1",
            "calls": [self._visible(item) for item in calls[:limit]],
            "truncated": len(calls) > limit,
        }

    async def call_events(self, after: int = 0, limit: int = 30) -> dict:
        """Read bounded, ACL-filtered events without losing an event cursor.

        The source API fetches up to 100 events. If the MCP client asks for
        fewer, report the cursor from the last *returned* event, not the
        API's later cursor, so follow-up reads never skip unread events.
        """
        if type(after) is not int or not 0 <= after < (1 << 63):
            raise ValueError("mcp_invalid_event_cursor")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("mcp_invalid_event_limit")
        await self._verify_identity()
        result = await self._control.inbox(after)
        events = result.get("events") if isinstance(result, dict) else None
        if not isinstance(events, list) or len(events) > 100:
            raise RuntimeError("mcp_call_events_contract_invalid")
        entries = []
        previous = after
        for event in events[:limit]:
            if not isinstance(event, dict):
                raise RuntimeError("mcp_call_event_invalid")
            cursor, kind = event.get("cursor"), event.get("kind")
            if (type(cursor) is not int or not previous < cursor < (1 << 63)
                    or not isinstance(kind, str)
                    or not re.fullmatch(r"[A-Z_]{1,48}", kind)):
                raise RuntimeError("mcp_call_event_invalid")
            entries.append({
                "cursor": cursor,
                "kind": kind,
                "call": self._visible(event.get("call")),
            })
            previous = cursor
        return {
            "schema": "porcelainberryme.mcp.events.v1",
            "events": entries,
            "next_cursor": previous,
            "truncated": len(events) > limit,
        }

    async def call_status(self, call_id: str) -> dict:
        if not isinstance(call_id, str) or not _CALL_ID.fullmatch(call_id):
            raise ValueError("mcp_invalid_call_id")
        await self._verify_identity()
        call = await self._control.status(call_id)
        result = self._visible(call)
        if result["id"] != call_id:
            raise RuntimeError("mcp_call_identity_response_mismatch")
        return {"schema": "porcelainberryme.mcp.call.v1", "call": result}


def build_server(visibility: MCPCallVisibility):
    """Register only explicitly defined, side-effect-free tools."""
    if not isinstance(visibility, MCPCallVisibility):
        raise TypeError("mcp_trusted_visibility_required")
    try:
        from mcp.server import MCPServer as Server
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP as Server
        except ImportError as exc:
            raise RuntimeError("optional_mcp_sdk_not_installed") from exc
    mcp = Server("PorcelainberryMe")

    @mcp.tool()
    async def porcelainberryme_capabilities() -> str:
        """Read capabilities for this preauthenticated FruitStack identity."""
        return json.dumps(await visibility.capabilities(), ensure_ascii=False)

    @mcp.tool()
    async def porcelainberryme_active_calls(limit: int = 20) -> str:
        """List bounded call state only for this authenticated participant."""
        return json.dumps(await visibility.active_calls(limit), ensure_ascii=False)

    @mcp.tool()
    async def porcelainberryme_call_events(after: int = 0, limit: int = 30) -> str:
        """Poll authorized call events without transcripts or skipped cursors."""
        return json.dumps(await visibility.call_events(after, limit), ensure_ascii=False)

    @mcp.tool()
    async def porcelainberryme_call_status(call_id: str) -> str:
        """Read one already-authorized Internet call without call media."""
        return json.dumps(await visibility.call_status(call_id), ensure_ascii=False)

    if visibility.can_place_calls:
        @mcp.tool()
        async def porcelainberryme_call_me() -> str:
            """Start one host-approved Internet voice worker; no destination argument."""
            return json.dumps(await visibility.call_me(), ensure_ascii=False)

        @mcp.tool()
        async def porcelainberryme_call_me_status() -> str:
            """Check actual worker state; exit is not evidence of a conversation."""
            return json.dumps(await visibility.call_me_status(), ensure_ascii=False)

    return mcp


def main() -> None:
    # An MCP client is untrusted for identity. Configure these values in a
    # supervised process environment and pass no CLI token/owner override.
    tenant = os.environ.get("PB_MCP_TENANT", "")
    subject = os.environ.get("PB_MCP_SUBJECT", "")
    role = os.environ.get("PB_MCP_ROLE", "")
    token = os.environ.get("PB_MCP_CALL_TOKEN", "")
    if len(token) < 32:
        raise ValueError("mcp_host_scoped_call_token_required")
    voice_options = {}
    enabled = os.environ.get("PB_MCP_CALL_ME_ENABLED", "")
    if enabled not in ("", "0", "1"):
        raise ValueError("mcp_voice_job_enable_flag_invalid")
    if enabled == "1":
        if role != "agent":
            raise PermissionError("mcp_voice_job_agent_identity_required")
        target = os.environ.get("PB_MCP_CALL_ME_TARGET", "")
        request_id = os.environ.get("PB_MCP_CALL_ME_REQUEST_ID", "")
        settings = {
            "whisper_model": os.environ.get("PB_LOCAL_WHISPER_DIR", ""),
            "ollama_model": os.environ.get("PB_LOCAL_OLLAMA_MODEL", ""),
            "piper_binary": os.environ.get("PB_LOCAL_PIPER_BIN", ""),
            "piper_model": os.environ.get("PB_LOCAL_PIPER_MODEL", ""),
        }
        if (any(not settings[key] for key in
                ("whisper_model", "piper_binary", "piper_model"))
                or not (settings["ollama_model"] or
                        os.environ.get("PB_AGENT_KNOWLEDGE_DB", ""))):
            raise ValueError("mcp_approved_local_voice_runtime_required")
        from .voice_livekit_agent import call_human
        async def start_verified_call() -> None:
            await call_human(target=target, tenant=tenant, agent_id=subject,
                             control_token=token, request_id=request_id,
                             **settings)
        voice_options = {
            "call_target": target,
            "call_request_id": request_id,
            "voice_launcher": start_verified_call,
        }
    visibility = MCPCallVisibility(
        CallControlClient(token), tenant=tenant, subject=subject, role=role,
        **voice_options,
    )
    # Actual identity is verified on every async tool invocation, not
    # assumed from the environment or an MCP-provided argument.
    build_server(visibility).run()


if __name__ == "__main__":
    main()
