"""Owner WhatsApp production preflight: read-only, fail-closed, no live sends."""
from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from porcelainberryme.ledger import Ledger
from porcelainberryme.whatsapp_consent import OwnerWhatsAppDrafts
from porcelainberryme.whatsapp_mirror import OwnerWhatsAppMirror
from porcelainberryme import whatsapp_preflight


class WhatsAppPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.token = base / "token"
        self.token.write_text("valid-API-token-" + "t" * 64, encoding="utf-8")
        self.token.chmod(0o600)
        self.control = base / "control"
        self.control.write_text("valid-control-token-" + "c" * 64, encoding="utf-8")
        self.control.chmod(0o600)
        self.approvals = str(base / "approvals.db")
        self.inbox = str(base / "inbound.db")
        OwnerWhatsAppDrafts(
            self.approvals, tenant="tenantA", agent="owner_agent",
            sender="personal_owner",
        )
        Ledger(self.inbox)
        OwnerWhatsAppMirror(
            self.inbox, tenant="tenantA",
            owner_destination="owner_destination",
        )
        self.env = {
            "PB_MCP_TENANT": "tenantA",
            "PB_MCP_SUBJECT": "owner_agent",
            "PB_MCP_ROLE": "agent",
            "PB_MCP_WHATSAPP_ENABLED": "1",
            "PB_MCP_WHATSAPP_OWNER_TENANT": "tenantA",
            "PB_MCP_WHATSAPP_OWNER_AGENT": "owner_agent",
            "PB_MCP_WHATSAPP_OWNER_SENDER": "personal_owner",
            "PB_MCP_WHATSAPP_OWNER_DESTINATION": "owner_destination",
            "PB_MCP_WHATSAPP_TOKEN_FILE": str(self.token),
            "PB_MCP_WHATSAPP_APPROVALS_DB": self.approvals,
            "PB_MCP_WHATSAPP_INBOX_DB": self.inbox,
            "PB_MCP_CALL_TOKEN_FILE": str(self.control),
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_private_owner_configuration_is_recognized_but_not_live_accepted(self):
        snapshot = whatsapp_preflight.evaluate(env=self.env)
        self.assertTrue(snapshot["configuration_and_requested_runtime_passed"])
        self.assertFalse(snapshot["production_accepted"])
        self.assertFalse(snapshot["real_whatsapp_provider_delivery_verified"])
        self.assertFalse(snapshot["tonnestate_replay_worker_verified"])
        self.assertFalse(snapshot["chatgpt_custom_connector_verified"])
        self.assertEqual({x["status"] for x in snapshot["checks"]}, {"PASS"})
        rendered = json.dumps(snapshot)
        self.assertNotIn("valid-API-token-", rendered)
        self.assertNotIn("valid-control-token-", rendered)
        self.assertNotIn("personal_owner", rendered)
        self.assertNotIn("owner_destination", rendered)

    def test_missing_or_insecure_secrets_and_databases_fail(self):
        for key, value in (
            ("PB_MCP_WHATSAPP_ENABLED", "0"),
            ("PB_MCP_WHATSAPP_OWNER_TENANT", "tenantB"),
            ("PB_MCP_WHATSAPP_OWNER_AGENT", "wrong_agent"),
            ("PB_MCP_ROLE", "human"),
            ("PB_MCP_WHATSAPP_TOKEN_FILE", "/tmp/never-create-owner-token"),
            ("PB_MCP_WHATSAPP_INBOX_DB", "/tmp/never-create-owner-inbox"),
            ("PB_MCP_CALL_TOKEN_FILE", "/tmp/never-create-owner-control"),
        ):
            with self.subTest(key=key):
                env = {**self.env, key: value}
                self.assertFalse(
                    whatsapp_preflight.evaluate(env=env)[
                        "configuration_and_requested_runtime_passed"
                    ]
                )
        self.token.chmod(0o644)
        self.assertFalse(
            whatsapp_preflight.evaluate(env=self.env)[
                "configuration_and_requested_runtime_passed"
            ]
        )
        self.token.chmod(0o600)

    def test_missing_sqlite_draft_schema_fails_old_install(self):
        legacy = str(Path(self.temp.name) / "legacy.db")
        Ledger(legacy)
        self.assertFalse(
            whatsapp_preflight.evaluate(env={
                **self.env, "PB_MCP_WHATSAPP_APPROVALS_DB": legacy,
            })["configuration_and_requested_runtime_passed"]
        )

    def test_network_is_read_only_and_requires_owner_control(self):
        probes = []
        def probe(port, path, *, bearer=""):
            probes.append((port, path, bool(bearer)))
            if port == 8873:
                return {"status": "ready", "scope": "owner_whatsapp_mirror"}
            return {
                "tenant": "tenantA", "subject": "owner_agent",
                "role": "agent",
            }
        with patch.object(whatsapp_preflight, "_probe_loopback",
                          side_effect=probe):
            snapshot = whatsapp_preflight.evaluate(env=self.env, network=True)
        self.assertTrue(snapshot["configuration_and_requested_runtime_passed"])
        self.assertEqual(probes, [
            (8873, "/v1/owner-whatsapp-mirror/readyz", False),
            (8874, "/v1/ip-calls/whoami", True),
        ])
        with patch.object(
            whatsapp_preflight, "_probe_loopback",
            side_effect=lambda port, path, **kwargs: (
                {"status": "ready", "scope": "owner_whatsapp_mirror"}
                if port == 8873 else
                {"tenant": "ANOTHER", "subject": "owner_agent", "role": "agent"}
            ),
        ):
            failed = whatsapp_preflight.evaluate(env=self.env, network=True)
        self.assertFalse(failed["configuration_and_requested_runtime_passed"])

    def test_generic_http_liveness_does_not_pass_owner_mirror_readiness(self):
        with patch.object(whatsapp_preflight, "_probe_loopback",
                          side_effect=lambda port, path, **kwargs: (
                              {"status": "up", "mode": "inbound_only"}
                              if port == 8873 else
                              {"tenant": "tenantA", "subject": "owner_agent",
                               "role": "agent"}
                          )):
            result = whatsapp_preflight.evaluate(env=self.env, network=True)
        self.assertFalse(result["configuration_and_requested_runtime_passed"])
        mirror_check = next(item for item in result["checks"]
                            if item["name"] == "whatsapp_owner_mirror_readiness")
        self.assertEqual(mirror_check["status"], "FAIL")

    def test_cli_explicit_preflight_only_exit_semantics(self):
        output = StringIO()
        with patch.dict(os.environ, self.env), patch.object(
                sys, "argv", ["whatsapp-preflight", "--preflight-only"]), (
                redirect_stdout(output)):
            whatsapp_preflight.main()
        self.assertFalse(json.loads(output.getvalue())["production_accepted"])

        with patch.dict(os.environ, self.env), patch.object(
                sys, "argv", ["whatsapp-preflight"]), (
                redirect_stdout(StringIO())):
            with self.assertRaises(SystemExit) as not_live:
                whatsapp_preflight.main()
        self.assertEqual(not_live.exception.code, 3)


if __name__ == "__main__":
    unittest.main()
