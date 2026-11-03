"""No-network tests for local-only LiveKit configuration generation."""
from pathlib import Path
import os
import tempfile
import unittest

from scripts.generate_local_livekit_config import generate


class LocalLivekitConfigurationTests(unittest.TestCase):
    def test_generated_secrets_are_strong_and_match_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "private"
            config, env_file = generate(folder)
            self.assertEqual(config.name, "livekit-local.yaml")
            self.assertEqual(env_file.name, "local-media.env")
            self.assertFalse(config.is_symlink())
            values = dict(line.split("=", 1) for line in env_file.read_text().splitlines()
                          if line and not line.startswith("#"))
            self.assertEqual(values["LIVEKIT_URL"], "ws://127.0.0.1:7880")
            self.assertGreaterEqual(len(values["LIVEKIT_API_SECRET"]), 32)
            self.assertGreaterEqual(len(values["PB_CALL_IDENTITY_SECRET"]), 32)
            self.assertNotEqual(values["PB_CALL_IDENTITY_SECRET"],
                                values["LIVEKIT_API_SECRET"])
            self.assertIn(values["LIVEKIT_API_KEY"], config.read_text())
            self.assertIn(values["LIVEKIT_API_SECRET"], config.read_text())
            if os.name == "posix":
                self.assertEqual(config.stat().st_mode & 0o777, 0o600)
                self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)

    def test_credential_files_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "private"
            c, e = generate(folder)
            old_config, old_env = c.read_bytes(), e.read_bytes()
            with self.assertRaises(FileExistsError):
                generate(folder)
            self.assertEqual(c.read_bytes(), old_config)
            self.assertEqual(e.read_bytes(), old_env)


if __name__ == "__main__":
    unittest.main()
