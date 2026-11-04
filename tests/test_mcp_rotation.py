"""Long-lived MCP credentials rotate without sharing signing secrets."""
from __future__ import annotations
import os
from pathlib import Path
import tempfile
import unittest

from porcelainberryme.mcp_server import rotating_identity_token


class RotationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.file = self.root / "identity.jwt"
        self.file.write_text("first-token-abcdefghijklmnopqrstuvwxyz")
        self.file.chmod(0o600)

    def test_reloaded_every_call_and_requires_private_file(self):
        read = rotating_identity_token(str(self.file))
        self.assertEqual(read(), "first-token-abcdefghijklmnopqrstuvwxyz")
        updated = self.root / "updated.jwt"
        updated.write_text("second-token-abcdefghijklmnopqrstuvwxyz")
        updated.chmod(0o600)
        os.replace(updated, self.file)
        self.assertEqual(read(), "second-token-abcdefghijklmnopqrstuvwxyz")
        self.file.chmod(0o644)
        with self.assertRaises(PermissionError):
            read()

    def test_symlink_cannot_redirect_bearer_to_another_file(self):
        link = self.root / "redirect"
        link.symlink_to(self.file)
        with self.assertRaises(OSError):
            rotating_identity_token(str(link))

    def test_relative_and_invalid_token_rejected(self):
        with self.assertRaises(ValueError):
            rotating_identity_token("relative-token")
        self.file.write_text("too-short")
        with self.assertRaises(PermissionError):
            rotating_identity_token(str(self.file))


if __name__ == "__main__":
    unittest.main()
