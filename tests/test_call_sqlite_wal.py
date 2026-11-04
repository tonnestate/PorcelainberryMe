"""Control-plane WAL and private-file enforcement during live call load."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from porcelainberryme.ip_calls import CallRegistry, main

SECRET = "unique-test-signing-key-01234567890123456789"


class CallDatabaseSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / "calls.sqlite"
        self.registry = CallRegistry(str(self.file), identity_secret=SECRET)

    def test_journal_mode_is_wal_and_provides_concurrent_read_write(self):
        with sqlite3.connect(self.file) as db:
            self.assertEqual(
                db.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal",
            )
        reader = sqlite3.connect(self.file, timeout=2)
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM ip_calls").fetchone()
            def write_during_reader():
                with sqlite3.connect(self.file, timeout=2) as writer:
                    writer.execute(
                        "INSERT INTO ip_call_users(tenant,subject) VALUES(?,?)",
                        ("tenantA", "someone"),
                    )
                    writer.commit()
            with ThreadPoolExecutor(max_workers=1) as executor:
                completed = executor.submit(write_during_reader)
                try:
                    completed.result(timeout=3)
                finally:
                    reader.rollback()
        finally:
            reader.close()
        with sqlite3.connect(self.file) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM ip_call_users").fetchone()[0],
                1,
            )

    def _run_main(self, path: Path):
        with patch.dict(os.environ, {
            "PB_CALL_IDENTITY_SECRET": SECRET, "PB_CALL_DB": str(path),
        }), patch.object(sys, "argv", [
            "porcelainberryme-ip-calls", "serve", "--port", "0",
        ]):
            with self.assertRaises(SystemExit) as ex:
                main()
        self.assertEqual(ex.exception.code, 2)

    def test_live_entrypoint_refuses_world_readable_existing_database(self):
        self.file.chmod(0o644)
        self._run_main(self.file)
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o644)

    def test_live_entrypoint_refuses_symlink_redirection(self):
        self.file.chmod(0o600)
        link = Path(self.temp.name) / "redirect.sqlite"
        link.symlink_to(self.file)
        self._run_main(link)


if __name__ == "__main__":
    unittest.main()
