"""Read-only release gate for the actual PorcelainberryMe host.

Checks factual configuration and local socket health; never dials, records,
installs packages, downloads weights or prints credentials/tenant identities.
A passing configuration check is NOT live smartphone/PSTN acceptance.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
from urllib.parse import quote, urlsplit

from .ip_calls import validate_livekit_url


def _private_regular_file(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = Path(value)
    try:
        if not path.is_absolute() or path.is_symlink():
            return False
        info = path.stat()
        return stat.S_ISREG(info.st_mode) and not (stat.S_IMODE(info.st_mode) & 0o077)
    except OSError:
        return False


def _sqlite_schema(value: str, required: set[str]) -> bool:
    if not _private_regular_file(value):
        return False
    path = Path(value)
    try:
        with closing(sqlite3.connect(
                "file:" + quote(str(path), safe="/") + "?mode=ro",
                uri=True, timeout=1,
        )) as db:
            db.execute("PRAGMA query_only=ON")
            actual = {
                row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            return required.issubset(actual) and (
                db.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
            )
    except (OSError, sqlite3.Error, TypeError, IndexError):
        return False


def _voice_files(env) -> bool:
    whisper = Path(env.get("PB_LOCAL_WHISPER_DIR") or "")
    piper = Path(env.get("PB_LOCAL_PIPER_BIN") or "")
    voice = Path(env.get("PB_LOCAL_PIPER_MODEL") or "")
    conf = Path(str(voice) + ".json")
    if (not whisper.is_absolute() or not piper.is_absolute()
            or not voice.is_absolute()):
        return False
    if (not (whisper / "model.bin").is_file()
            or not piper.is_file() or not os.access(piper, os.X_OK)
            or not voice.is_file() or not conf.is_file()):
        return False
    try:
        if any(link.is_symlink() for link in (whisper, piper, voice, conf)):
            return False
        data = conf.read_bytes()
        if len(data) > 65536:
            return False
        config = json.loads(data)
        rate = config["audio"]["sample_rate"]
        return type(rate) is int and 8000 <= rate <= 48000
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _model_name(env):
    return (env.get("PB_LOCAL_VOICE_CHAT_MODEL") or
            env.get("PB_LOCAL_OLLAMA_MODEL") or "").strip()


def _local_ollama_model(model: str) -> bool:
    if not model or len(model) > 128 or any(c.isspace() for c in model):
        return False
    conn = http.client.HTTPConnection("127.0.0.1", 11434, timeout=2)
    try:
        conn.request("GET", "/api/tags")
        response = conn.getresponse()
        data = response.read(65537)
        if response.status != 200 or len(data) > 65536:
            return False
        models = json.loads(data)["models"]
        if not isinstance(models, list):
            return False
        return any(isinstance(item, dict) and model in
                   (item.get("name"), item.get("model")) for item in models)
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def _local_call_control() -> bool:
    conn = http.client.HTTPConnection("127.0.0.1", 8874, timeout=2)
    try:
        conn.request("GET", "/readyz")
        response = conn.getresponse()
        data = response.read(4097)
        if len(data) > 4096 or response.status != 200:
            return False
        report = json.loads(data)
        return (report.get("status") == "ready"
                and report.get("scope") == "internet_calls_control_only"
                and report.get("media_configured") is True)
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def _authenticated_agent_control(env) -> bool:
    """A real signed agent token must be accepted by the running controller.

    The only request is GET /whoami to 127.0.0.1:8874. The signing
    secret/issued JWT never appears in logs, return values or stdout.
    """
    from .ip_calls import IDENTITY, mint_identity_token
    tenant = env.get("PB_TENANT", "")
    agent = env.get("PB_AGENT", "")
    secret = env.get("PB_CALL_IDENTITY_SECRET", "")
    if (not isinstance(tenant, str) or not IDENTITY.fullmatch(tenant)
            or not isinstance(agent, str) or not IDENTITY.fullmatch(agent)
            or not isinstance(secret, str) or len(secret) < 32):
        return False
    conn = http.client.HTTPConnection("127.0.0.1", 8874, timeout=2)
    try:
        bearer = mint_identity_token(secret, tenant, agent, "agent")
        conn.request("GET", "/v1/ip-calls/whoami",
                     headers={"Authorization": "Bearer " + bearer})
        response = conn.getresponse()
        body = response.read(4097)
        if response.status != 200 or len(body) > 4096:
            return False
        verified = json.loads(body)
        return (isinstance(verified, dict)
                and verified.get("role") == "agent"
                and verified.get("tenant") == tenant
                and verified.get("subject") == agent)
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def _media_signaling(url: str) -> bool:
    try:
        validate_livekit_url(url)
        target = urlsplit(url)
        port = target.port or (443 if target.scheme == "wss" else 80)
        with socket.create_connection((target.hostname, port), timeout=2):
            return True
    except (ValueError, OSError, OverflowError):
        return False


def evaluate(*, env=None, network: bool = False,
             require_mobile_push: bool = True,
             require_pstn: bool = False) -> dict:
    """Inspect, never mutate. The CI environment and real host are distinct."""
    env = os.environ if env is None else env
    checks = []

    def add(key, passed, *, level="configuration"):
        checks.append({
            "key": key, "status": "PASS" if passed else "FAIL",
            "level": level,
        })

    identity = env.get("PB_CALL_IDENTITY_SECRET", "")
    media_secret = env.get("LIVEKIT_API_SECRET", "")
    add("call_identity_signing_key",
        isinstance(identity, str) and len(identity) >= 32)
    from .ip_calls import IDENTITY
    add("trusted_agent_binding",
        isinstance(env.get("PB_TENANT"), str)
        and bool(IDENTITY.fullmatch(env["PB_TENANT"]))
        and isinstance(env.get("PB_AGENT"), str)
        and bool(IDENTITY.fullmatch(env["PB_AGENT"])))
    add("call_ledger_private_and_healthy",
        _sqlite_schema(env.get("PB_CALL_DB", ""), {
            "ip_calls", "ip_call_users", "ip_call_events", "ip_media_teardown",
        }))
    try:
        media_url = validate_livekit_url(env.get("LIVEKIT_URL", ""))
        valid_media = True
    except (ValueError, TypeError):
        media_url = ""
        valid_media = False
    add("self_hosted_media_config",
        valid_media
        and bool(env.get("LIVEKIT_API_KEY"))
        and isinstance(media_secret, str) and len(media_secret) >= 32
        and identity != media_secret)
    add("installed_whisper_piper", _voice_files(env))

    approved_db = env.get("PB_AGENT_KNOWLEDGE_DB", "")
    llm_model = _model_name(env)
    if approved_db:
        add("voice_knowledge_configuration",
            _sqlite_schema(approved_db, {
                "pb_kb_agents", "pb_kb_entries",
            }))
    else:
        add("local_voice_model_name",
            bool(llm_model) and len(llm_model) <= 128
            and not any(c.isspace() for c in llm_model))

    add("durable_outbound_ledger_private",
        _sqlite_schema(env.get("PB_CALL_JOBS_DB", ""), {
            "outbound_call_jobs",
        }))
    add("mcp_rotatable_token_private",
        _private_regular_file(env.get("PB_MCP_CALL_TOKEN_FILE", "")))

    if require_mobile_push:
        add("mobile_push_config_present",
            all(isinstance(env.get(key), str) and bool(env.get(key)) for key in (
                "PB_WEBPUSH_PUBLIC_KEY",
                "PB_WEBPUSH_PRIVATE_KEY",
                "PB_WEBPUSH_CONTACT",
            )))

    configuration_passed = all(
        check["status"] == "PASS" for check in checks
    )
    network_results = [
        ("running_call_control_with_media", _local_call_control),
        ("signed_agent_identity_accepted",
         lambda: _authenticated_agent_control(env)),
        ("livekit_signaling_socket", lambda: _media_signaling(media_url)),
    ]
    if not approved_db:
        network_results.append((
            "installed_local_ollama_model",
            lambda: _local_ollama_model(llm_model),
        ))
    for key, probe in network_results:
        checks.append({
            "key": key,
            "status": ("PASS" if probe() else "FAIL") if network else "NOT_TESTED",
            "level": "runtime",
        })
    network_passed = network and all(
        check["status"] == "PASS" for check in checks
        if check["level"] == "runtime"
    )
    external_proofs = [
        "real_registered_smartphone_acceptance",
        "actual_installed_german_stt_tts_latency_and_barge_in",
        "live_restart_and_idempotency_reconciliation",
        "documented_tenant_privacy_and_restore_acceptance",
    ]
    if require_mobile_push:
        external_proofs.append("real_ios_android_background_push_delivery")
    if require_pstn:
        external_proofs += [
            "configured_authorized_asterisk_stasis_host",
            "licensed_provider_sip_trunk_and_real_carrier_call",
        ]
    return {
        "schema": "porcelainberryme.release-gate.v1",
        "mode": "pstn" if require_pstn else "internet_voice",
        "read_only": True,
        "configuration_passed": configuration_passed,
        "runtime_checks_passed": network_passed,
        # An engineer cannot prove real PSTN/audio quality with repo tests.
        "production_accepted": False,
        "checks": checks,
        "external_acceptance_outstanding": external_proofs,
        "interpretation": (
            "A green local check means ready for real host acceptance, "
            "not proof of production operation."
        ),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Read-only local PorcelainberryMe release gate; no phone calls",
    )
    parser.add_argument("--network", action="store_true",
                        help="Probe only configured LiveKit TCP and local control/Ollama")
    parser.add_argument("--no-mobile-push", action="store_true",
                        help="For explicitly web-only deployment without mobile alerts")
    parser.add_argument("--require-pstn", action="store_true",
                        help="List separate carrier/Asterisk acceptance gates")
    args = parser.parse_args()
    report = evaluate(
        network=args.network,
        require_mobile_push=not args.no_mobile_push,
        require_pstn=args.require_pstn,
    )
    print(json.dumps(report, sort_keys=True, indent=2))
    # A successful exit proves only static/network preflight, never release.
    return 0 if (report["configuration_passed"]
                 and report["runtime_checks_passed"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
