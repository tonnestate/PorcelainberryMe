"""Private SQLite ledger must never follow or chmod an unsafe existing file."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import unittest

from porcelainberryme.contracts import InboundEvent
from porcelainberryme.ledger import Ledger


def message(event_id: str) -> InboundEvent:
    return InboundEvent(
        tenant_id="ledger-owner", channel="whatsapp", external_id=event_id,
        sender="49151234567@c.us", recipient="owner", text="Test message",
    )


class LedgerPrivateFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_insecure_existing_db_is_rejected_not_silently_chmodded(self):
        path = self.directory / "preexisting.sqlite"
        path.write_bytes(b"not a private database")
        path.chmod(0o644)
        with self.assertRaisesRegex(PermissionError, "ledger_database_not_private"):
            Ledger(path)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        self.assertEqual(path.read_bytes(), b"not a private database")

    def test_existing_symlink_is_rejected_without_changing_target(self):
        real = self.directory / "real.sqlite"
        original = Ledger(real)
        self.assertTrue(original.ingest(message("safe-original"))["new"])
        alias = self.directory / "linked.sqlite"
        alias.symlink_to(real)
        with self.assertRaisesRegex(PermissionError, "ledger_database_not_private"):
            Ledger(alias)
        self.assertEqual(stat.S_IMODE(real.stat().st_mode), 0o600)
        self.assertEqual(len(original.pending(tenant_id="ledger-owner")), 1)

    def test_hardlinks_are_rejected_as_aliases_of_private_database(self):
        real = self.directory / "real.sqlite"
        original = Ledger(real)
        alias = self.directory / "hardlink.sqlite"
        os.link(real, alias)
        with self.assertRaisesRegex(PermissionError, "ledger_database_not_private"):
            Ledger(alias)
        # The original path also fails closed while its inode is shared.
        with self.assertRaisesRegex(PermissionError, "ledger_database_not_private"):
            original.ingest(message("blocked-hardlink"))
        alias.unlink()
        self.assertTrue(original.ingest(message("safe-after-unlink"))["new"])

    def test_running_ledger_rejects_permissions_changed_after_start(self):
        path = self.directory / "mutable.sqlite"
        ledger = Ledger(path)
        self.assertTrue(ledger.ingest(message("initial"))["new"])
        path.chmod(0o644)
        with self.assertRaisesRegex(PermissionError, "ledger_database_not_private"):
            ledger.ingest(message("not-written"))
        path.chmod(0o600)
        self.assertTrue(ledger.ingest(message("after-repair"))["new"])
        self.assertEqual(
            [x.external_id for x in ledger.pending(tenant_id="ledger-owner")],
            ["initial", "after-repair"],
        )

    def test_running_ledger_rejects_file_replaced_with_symlink(self):
        path = self.directory / "current.sqlite"
        ledger = Ledger(path)
        self.assertTrue(ledger.ingest(message("initial"))["new"])
        backup = self.directory / "original.sqlite"
        path.rename(backup)
        other = self.directory / "other.sqlite"
        alternate = Ledger(other)
        self.assertTrue(alternate.ingest(message("different-store"))["new"])
        path.symlink_to(other)
        with self.assertRaisesRegex(PermissionError, "ledger_database_not_private"):
            ledger.ingest(message("should-not-be-forwarded"))
        self.assertEqual(
            [x.external_id for x in alternate.pending(tenant_id="ledger-owner")],
            ["different-store"],
        )
        path.unlink()
        backup.rename(path)
        self.assertTrue(ledger.ingest(message("after-restore"))["new"])


if __name__ == "__main__":
    unittest.main()
