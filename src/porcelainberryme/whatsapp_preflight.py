"""Read-only owner WhatsApp MCP release preflight for the real host.

Never sends WhatsApp messages, creates drafts, approves work or changes data.
A PASS is technical configuration/readiness only, never live acceptance.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote

from .whatsapp_mcp import private_file, owner_token_reader

_SCOPE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")


_REQUIRED_COLUMNS = {
    "whatsapp_mcp_approvals": {
        "approval_id", "tenant", "agent", "sender", "recipient",
        "body", "consent_hash", "expires_at", "state",
    },
    "whatsapp_mcp_drafts": {
        "draft_id", "tenant", "agent", "sender", "recipient", "body",
        "fingerprint", "expires_at", "state", "approval_id",
    },
    "whatsapp_owner_mirror": {
        "seq", "tenant", "owner_destination", "external_id",
        "digest", "payload", "received_at",
    },
}
_UNIQUE_KEYS = {
    "whatsapp_mcp_approvals": ("approval_id",),
    "whatsapp_mcp_drafts": ("draft_id",),
    "whatsapp_owner_mirror": ("tenant", "owner_destination", "external_id"),
}


def _sqlite_has_tables(path: str, tables: set[str]) -> bool:
    """Probe real owner schemas without creating or migrating a database.

    A table-name-only probe used to pass even if required columns or the
    deduplication constraint were missing; that falsely reported ready
    while MCP approval/ingest would fail at first use.
    """
    try:
        private_file(path)
        uri = "file:" + quote(str(Path(path)), safe="/") + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
            db.execute("PRAGMA query_only=ON")
            actual = {
                row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not tables.issubset(actual):
                return False
            for name in tables:
                required = _REQUIRED_COLUMNS.get(name)
                unique = _UNIQUE_KEYS.get(name)
                if required is None or unique is None:
                    return False  # Never interpolate unrecognized table names.
                present = {
                    row[1] for row in db.execute(f"PRAGMA table_info({name})")
                }
                if not required.issubset(present):
                    return False
                unique_indexes = [
                    row[1] for row in db.execute(f"PRAGMA index_list({name})")
                    if row[2] == 1
                ]
                if not any(tuple(
                    row[2] for row in db.execute(f"PRAGMA index_info({index})")
                ) == unique for index in unique_indexes):
                    return False
            return db.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
    except (OSError, PermissionError, ValueError, sqlite3.Error,
            TypeError, IndexError):
        return False

def _owner_token_file(path: str) -> bool:
    try:
        owner_token_reader(path)
        return True
    except (ValueError, PermissionError, OSError):
        return False


def _control_token(env) -> str | None:
    token_file = env.get("PB_MCP_CALL_TOKEN_FILE", "")
    if token_file:
        try:
            private_file(token_file)
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except (ValueError, OSError, PermissionError, UnicodeError):
            return None
    else:
        token = env.get("PB_MCP_CALL_TOKEN", "")
    if (not isinstance(token, str) or len(token) < 32
            or len(token) > 4096 or any(char.isspace() for char in token)):
        return None
    return token


def _probe_loopback(port: int, path: str, *, bearer: str = "") -> dict | None:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        headers = {"Authorization": "Bearer " + bearer} if bearer else {}
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        raw = response.read(4097)
        if response.status != 200 or len(raw) > 4096:
            return None
        result = json.loads(raw)
        return result if isinstance(result, dict) else None
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def evaluate(*, env=None, network: bool = False) -> dict:
    env = os.environ if env is None else env
    tenant = env.get("PB_MCP_TENANT", "")
    subject = env.get("PB_MCP_SUBJECT", "")
    role = env.get("PB_MCP_ROLE", "")
    checks = []

    def check(name: str, success: bool, *, live: bool = False):
        checks.append({
            "name": name,
            "status": "PASS" if success else "FAIL",
            "level": "runtime" if live else "configuration",
        })

    scoped = (
        bool(_SCOPE.fullmatch(tenant))
        and bool(_SCOPE.fullmatch(subject))
        and role == "agent"
        and env.get("PB_MCP_WHATSAPP_ENABLED") == "1"
        and env.get("PB_MCP_WHATSAPP_OWNER_TENANT") == tenant
        and env.get("PB_MCP_WHATSAPP_OWNER_AGENT") == subject
        and isinstance(env.get("PB_MCP_WHATSAPP_OWNER_SENDER"), str)
        and bool(env.get("PB_MCP_WHATSAPP_OWNER_SENDER", "").strip())
    )
    check("whatsapp_mcp_single_owner_binding", scoped)
    check("whatsapp_send_token_private",
          _owner_token_file(env.get("PB_MCP_WHATSAPP_TOKEN_FILE", "")))
    check("whatsapp_approval_and_draft_sqlite",
          _sqlite_has_tables(
              env.get("PB_MCP_WHATSAPP_APPROVALS_DB", ""),
              {"whatsapp_mcp_approvals", "whatsapp_mcp_drafts"},
          ))
    check("whatsapp_authenticated_mirror_sqlite",
          _sqlite_has_tables(
              env.get("PB_MCP_WHATSAPP_INBOX_DB", ""),
              {"whatsapp_owner_mirror"},
          ))
    destination = env.get("PB_MCP_WHATSAPP_OWNER_DESTINATION", "")
    check("whatsapp_owner_inbox_binding",
          isinstance(destination, str) and bool(destination.strip())
          and len(destination) <= 256)
    control_token = _control_token(env)
    check("whatsapp_signed_call_control_token", control_token is not None)

    if network:
        # Generic /healthz also succeeds when WhatsApp mirroring is OFF.
        # Release readiness must prove the opt-in mirror is enabled and
        # its private database/schema is actually readable.
        mirror = _probe_loopback(
            8873, "/v1/owner-whatsapp-mirror/readyz",
        )
        check("whatsapp_owner_mirror_readiness",
              mirror is not None and mirror.get("status") == "ready"
              and mirror.get("scope") == "owner_whatsapp_mirror", live=True)
        identity = (_probe_loopback(
            8874, "/v1/ip-calls/whoami", bearer=control_token,
        ) if control_token is not None else None)
        check("whatsapp_mcp_real_owner_identity",
              identity is not None
              and identity.get("tenant") == tenant
              and identity.get("subject") == subject
              and identity.get("role") == "agent", live=True)

    passed = all(item["status"] == "PASS" for item in checks)
    return {
        "schema": "porcelainberryme.whatsapp-release-preflight.v1",
        "configuration_and_requested_runtime_passed": passed,
        "production_accepted": False,
        "real_whatsapp_provider_delivery_verified": False,
        "tonnestate_replay_worker_verified": False,
        "chatgpt_custom_connector_verified": False,
        "checks": checks,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Non-mutating, owner-only WhatsApp MCP readiness checks",
    )
    parser.add_argument("--network", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    options = parser.parse_args()
    result = evaluate(network=options.network)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    if not result["configuration_and_requested_runtime_passed"]:
        raise SystemExit(2)
    if not options.preflight_only:
        # No preflight can prove a live customer's phone received a message.
        raise SystemExit(3)


if __name__ == "__main__":
    main()
