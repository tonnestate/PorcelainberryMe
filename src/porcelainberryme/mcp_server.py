"""Read-only MCP visibility into existing authenticated PorcelainberryMe calls.

No new call controller, customer directory, owner-harness authority, media
session or network listener. The stdio MCP process inherits a trusted
single-identity control token from its supervised FruitStack host. Tools
never accept a token/tenant/subject from the LLM or expose raw payloads.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

from .voice_livekit_agent import CallControlClient

_CALL_ID = re.compile(r"^[a-f0-9]{32}$")
_SUBJECT = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
_STATES = frozenset({"RINGING", "ACTIVE", "ENDED", "DECLINED", "TIMED_OUT"})
_DIRECTIONS = frozenset({"incoming", "outgoing"})


class MCPCallVisibility:
    """Tenant/identity-fenced adapter to the existing loopback call server.

    The underlying call-control server is authoritative for participant,
    tenant, and employee handoff ACLs. Re-check the token's identity on
    every tool request; never trust a caller-selected tenant or claim.
    """

    def __init__(self, control: CallControlClient, *, tenant: str,
                 subject: str, role: str):
        if not isinstance(tenant, str) or not _SUBJECT.fullmatch(tenant):
            raise ValueError("mcp_trusted_tenant_required")
        if not isinstance(subject, str) or not _SUBJECT.fullmatch(subject):
            raise ValueError("mcp_trusted_subject_required")
        if role not in ("human", "agent"):
            raise ValueError("mcp_trusted_role_required")
        if not isinstance(control, CallControlClient):
            raise ValueError("mcp_authenticated_call_client_required")
        self._control = control
        self._scope = {"tenant": tenant, "subject": subject, "role": role}

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

    async def capabilities(self) -> dict:
        await self._verify_identity()
        return {
            "schema": "porcelainberryme.mcp.capabilities.v1",
            "tenant": self._scope["tenant"],
            "subject": self._scope["subject"],
            "role": self._scope["role"],
            "transport": "internet_webrtc",
            "read_only": True,
            "tools": ["porcelainberryme_capabilities",
                      "porcelainberryme_active_calls",
                      "porcelainberryme_call_status"],
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
    async def porcelainberryme_call_status(call_id: str) -> str:
        """Read one already-authorized Internet call without call media."""
        return json.dumps(await visibility.call_status(call_id), ensure_ascii=False)

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
    visibility = MCPCallVisibility(
        CallControlClient(token), tenant=tenant, subject=subject, role=role,
    )
    # Actual identity is verified on every async tool invocation, not
    # assumed from the environment or an MCP-provided argument.
    build_server(visibility).run()


if __name__ == "__main__":
    main()
