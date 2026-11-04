"""Read-only release checklist: verified config is not human call acceptance."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from porcelainberryme.ip_calls import CallRegistry
from porcelainberryme.outbound_jobs import DurableCallJobs
from porcelainberryme.release_gate import evaluate


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        calls = root / "calls.sqlite"
        CallRegistry(str(calls), identity_secret="a" * 36)
        calls.chmod(0o600)
        jobs = root / "jobs.sqlite"
        DurableCallJobs(jobs)

        whisper = root / "whisper"
        whisper.mkdir()
        (whisper / "model.bin").write_bytes(b"locally-installed-whisper-placeholder")
        piper = root / "piper"
        piper.write_text("#!/bin/sh\nexit 0\n")
        piper.chmod(0o700)
        voice = root / "voice.onnx"
        voice.write_bytes(b"installed-voice-placeholder")
        (root / "voice.onnx.json").write_text('{"audio":{"sample_rate":16000}}')
        token = root / "agent.jwt"
        token.write_text("placeholder-signature-token-for-test-only")
        token.chmod(0o600)
        self.env = {
            "PB_CALL_IDENTITY_SECRET": "a" * 36,
            "PB_TENANT": "tenantA",
            "PB_AGENT": "voiceagent",
            "PB_CALL_DB": str(calls),
            "LIVEKIT_API_KEY": "local-livekit",
            "LIVEKIT_API_SECRET": "b" * 36,
            "LIVEKIT_URL": "ws://127.0.0.1:7880",
            "PB_LOCAL_WHISPER_DIR": str(whisper),
            "PB_LOCAL_PIPER_BIN": str(piper),
            "PB_LOCAL_PIPER_MODEL": str(voice),
            "PB_LOCAL_OLLAMA_MODEL": "local-small:latest",
            "PB_CALL_JOBS_DB": str(jobs),
            "PB_MCP_CALL_TOKEN_FILE": str(token),
        }

    def test_config_is_valid_but_live_telephony_remains_unproven(self):
        report = evaluate(env=self.env, network=False, require_mobile_push=False)
        self.assertTrue(report["configuration_passed"])
        self.assertFalse(report["runtime_checks_passed"])
        self.assertFalse(report["production_accepted"])
        self.assertTrue(all(
            item["status"] == "NOT_TESTED" for item in report["checks"]
            if item["level"] == "runtime"
        ))
        self.assertNotIn("local-small:latest", str(report))
        self.assertNotIn(self.env["PB_CALL_IDENTITY_SECRET"], str(report))
        self.assertNotIn(str(self.temp.name), str(report))

    def test_real_runtime_probes_can_pass_without_false_product_claim(self):
        with patch("porcelainberryme.release_gate._local_call_control", return_value=True), (
                patch("porcelainberryme.release_gate._authenticated_agent_control", return_value=True)), (
                patch("porcelainberryme.release_gate._media_signaling", return_value=True)), (
                patch("porcelainberryme.release_gate._local_ollama_model", return_value=True)):
            report = evaluate(env=self.env, network=True,
                              require_mobile_push=False)
        self.assertTrue(report["configuration_passed"])
        self.assertTrue(report["runtime_checks_passed"])
        self.assertFalse(report["production_accepted"])
        self.assertIn("real_registered_smartphone_acceptance",
                      report["external_acceptance_outstanding"])

    def test_insecure_or_missing_sources_fail_closed(self):
        baseline = self.env.copy()
        for key, replacement in (
            ("PB_CALL_IDENTITY_SECRET", "bad"),
            ("LIVEKIT_URL", "ws://public.example.test"),
            ("PB_LOCAL_WHISPER_DIR", "/does/not/exist"),
            ("PB_CALL_JOBS_DB", "/does/not/exist"),
            ("PB_MCP_CALL_TOKEN_FILE", "/does/not/exist"),
        ):
            with self.subTest(key=key):
                broken = baseline.copy()
                broken[key] = replacement
                result = evaluate(env=broken, require_mobile_push=False)
                self.assertFalse(result["configuration_passed"])
                self.assertFalse(result["production_accepted"])
        calls = Path(self.env["PB_CALL_DB"])
        calls.chmod(0o644)
        self.assertFalse(evaluate(
            env=baseline, require_mobile_push=False,
        )["configuration_passed"])

    def test_actual_signed_whoami_handshake_is_verified_not_just_health(self):
        from porcelainberryme.ip_calls import verify_identity_token
        from porcelainberryme.release_gate import _authenticated_agent_control
        import json

        class SignedMockConnection:
            status = 200
            def __init__(self, host, port, timeout):
                self.asserted_host = (host, port)
                self.token = None

            def request(self, method, path, headers):
                self.token = headers["Authorization"].removeprefix("Bearer ")
                assert (method, path) == ("GET", "/v1/ip-calls/whoami")
                assert self.asserted_host == ("127.0.0.1", 8874)

            def getresponse(self):
                claims = verify_identity_token(
                    self.token, self_secret,
                )
                return type("Response", (), {
                    "status": self.status,
                    "read": lambda unused, size: json.dumps({
                        "tenant": claims["tenant"], "subject": claims["sub"],
                        "role": claims["role"],
                    }).encode("utf-8"),
                })()

            def close(self):
                pass

        self_secret = self.env["PB_CALL_IDENTITY_SECRET"]
        with patch("porcelainberryme.release_gate.http.client.HTTPConnection",
                   SignedMockConnection):
            self.assertTrue(_authenticated_agent_control(self.env))
            invalid = dict(self.env, PB_AGENT="not an identity")
            self.assertFalse(_authenticated_agent_control(invalid))

    def test_default_release_command_cannot_accidentally_be_used_for_deploy(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from porcelainberryme.release_gate import main
        from unittest.mock import patch
        report = {
            "configuration_passed": True, "runtime_checks_passed": True,
            "production_accepted": False,
        }
        with patch("porcelainberryme.release_gate.evaluate", return_value=report), (
                patch("sys.argv", ["porcelainberryme-release-gate", "--network"])), (
                redirect_stdout(StringIO())):
            self.assertEqual(main(), 3)
        with patch("porcelainberryme.release_gate.evaluate", return_value=report), (
                patch("sys.argv", ["porcelainberryme-release-gate",
                                   "--network", "--preflight-only"])), (
                redirect_stdout(StringIO())):
            self.assertEqual(main(), 0)

    def test_mobile_and_pstn_require_independent_acceptance(self):
        report = evaluate(env=self.env, network=False,
                          require_mobile_push=True, require_pstn=True)
        self.assertFalse(report["configuration_passed"])
        self.assertIn("real_ios_android_background_push_delivery",
                      report["external_acceptance_outstanding"])
        self.assertIn("licensed_provider_sip_trunk_and_real_carrier_call",
                      report["external_acceptance_outstanding"])
        self.assertEqual(report["mode"], "pstn")


if __name__ == "__main__":
    unittest.main()
