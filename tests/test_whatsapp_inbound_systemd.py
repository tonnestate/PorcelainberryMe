"""Deployment contract for the private signed WhatsApp mirror listener.

These offline tests validate the packaged systemd source, not VPS activation.
Never start a second WhatsApp login or remote provider client.
"""
from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path
import inspect
import unittest

from porcelainberryme import server


ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy/systemd/porcelainberryme-inbound.service.example"


class WhatsAppInboundSystemdTests(unittest.TestCase):
    def setUp(self):
        self.unit = ConfigParser(interpolation=None)
        with UNIT.open(encoding="utf-8") as stream:
            self.unit.read_file(stream)
        self.service = self.unit["Service"]

    def test_supervised_original_signed_listener_only(self):
        self.assertEqual(
            self.service["ExecStart"],
            "/opt/porcelainberryme/venv/bin/python -m porcelainberryme.server",
        )
        self.assertEqual(self.service["User"], "porcelainberryme")
        self.assertEqual(self.service["Group"], "porcelainberryme")
        self.assertEqual(self.service["Restart"], "on-failure")
        self.assertEqual(self.service["WorkingDirectory"], "/opt/porcelainberryme")
        # Exact socket binding is in the invoked module, never in a shell shim.
        self.assertIn('("127.0.0.1", 8873)', inspect.getsource(server.main))
        self.assertNotIn("whatsapp-web", self.service["ExecStart"].lower())
        self.assertNotIn("whatsapp_cloud", self.service["ExecStart"].lower())

    def test_required_config_and_private_persistent_storage(self):
        self.assertEqual(
            self.service["EnvironmentFile"], "/etc/porcelainberryme/voice.env",
        )
        self.assertFalse(self.service["EnvironmentFile"].startswith("-"))
        self.assertEqual(self.service["StateDirectory"], "porcelainberryme")
        self.assertEqual(self.service["StateDirectoryMode"], "0700")
        self.assertEqual(self.service["UMask"], "0077")
        self.assertEqual(self.service["ReadWritePaths"], "/var/lib/porcelainberryme")
        self.assertEqual(self.service["ProtectSystem"], "strict")
        self.assertEqual(self.service["ProtectHome"], "yes")
        self.assertEqual(self.service["NoNewPrivileges"], "yes")
        self.assertEqual(self.service["PrivateTmp"], "yes")
        self.assertEqual(self.service["CapabilityBoundingSet"], "")
        self.assertNotIn("ConditionPathExists", self.unit["Unit"])


if __name__ == "__main__":
    unittest.main()
