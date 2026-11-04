"""Consent withdrawal must be atomic with worker claiming a pending call."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from porcelainberryme.outbound_jobs import DurableCallJobs, main


class RevocationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "calls.sqlite"
        self.now = [1800000000]
        self.store = DurableCallJobs(self.db, clock=lambda: self.now[0])
        self.args = dict(tenant="tenantA", agent="assistant", recipient="owner")
        self.sequence = 0

    def approve(self):
        self.sequence += 1
        return self.store.approve(
            **self.args,
            consent_reference=f"separately-approved-request-{self.sequence:03d}",
        )

    def test_revoke_approved_or_queued_never_dials(self):
        approval = self.approve()
        cancelled = self.store.revoke(approval_id=approval, **self.args)
        self.assertEqual(cancelled["state"], "REVOKED")
        self.assertTrue(cancelled["revocation_confirmed"])
        self.assertEqual(self.store.request(
            approval_id=approval, **self.args)["state"], "REVOKED")
        self.assertIsNone(self.store.claim(
            tenant="tenantA", agent="assistant"))
        self.assertEqual(
            self.store.revoke(approval_id=approval, **self.args)["state"],
            "REVOKED",
        )
        queued = self.approve()
        self.store.request(approval_id=queued, **self.args)
        self.assertEqual(
            self.store.revoke(approval_id=queued, **self.args)["state"],
            "REVOKED",
        )
        self.assertIsNone(self.store.claim(
            tenant="tenantA", agent="assistant"))
        restarted = DurableCallJobs(self.db, clock=lambda: self.now[0])
        self.assertEqual(
            restarted.status(approval_id=queued, **self.args)["state"],
            "REVOKED",
        )

    def test_revoke_after_dispatch_never_claims_to_stop_remote_call(self):
        approval = self.approve()
        self.store.request(approval_id=approval, **self.args)
        self.store.claim(tenant="tenantA", agent="assistant")
        result = self.store.revoke(approval_id=approval, **self.args)
        self.assertEqual(result["state"], "DISPATCHING")
        self.assertFalse(result["revocation_confirmed"])

    def test_revoke_is_authorized_to_exact_agent_recipient_and_tenant(self):
        approval = self.approve()
        for scope in (
            dict(tenant="tenantB", agent="assistant", recipient="owner"),
            dict(tenant="tenantA", agent="other", recipient="owner"),
            dict(tenant="tenantA", agent="assistant", recipient="other"),
        ):
            with self.subTest(scope=scope), self.assertRaises(PermissionError):
                self.store.revoke(approval_id=approval, **scope)
        self.assertEqual(
            self.store.status(approval_id=approval, **self.args)["state"],
            "APPROVED",
        )

    def test_claim_and_revoke_race_has_no_false_success(self):
        for _ in range(15):
            approval = self.approve()
            self.store.request(approval_id=approval, **self.args)
            with ThreadPoolExecutor(max_workers=2) as pool:
                a = pool.submit(
                    self.store.claim, tenant="tenantA", agent="assistant",
                )
                b = pool.submit(
                    self.store.revoke, approval_id=approval, **self.args,
                )
                claimed, cancelled = a.result(), b.result()
            if claimed is None:
                self.assertEqual(cancelled["state"], "REVOKED")
                self.assertTrue(cancelled["revocation_confirmed"])
            else:
                self.assertEqual(claimed["approval_id"], approval)
                self.assertEqual(cancelled["state"], "DISPATCHING")
                self.assertFalse(cancelled["revocation_confirmed"])
                self.store.finish(
                    approval_id=approval, tenant="tenantA",
                    agent="assistant", state="UNKNOWN",
                )

    def test_trusted_revoke_cli_disabled_by_default(self):
        key = self.approve()
        with patch.dict(os.environ, {
            "PB_CALL_JOBS_DB": str(self.db),
            "PB_CALL_REVOCATION_HOST_ENABLED": "0",
        }), patch.object(sys, "argv", [
            "porcelainberryme-call-worker", "revoke",
            "--tenant", "tenantA", "--agent", "assistant",
            "--recipient", "owner", "--approval-id", key,
        ]):
            with self.assertRaises(SystemExit) as ctx:
                main()
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(self.store.status(
            approval_id=key, **self.args)["state"], "APPROVED")

    def test_existing_sqlite_schema_migrates_revocation_column(self):
        old = Path(self.db.parent) / "pre-revoke.sqlite"
        with sqlite3.connect(old) as db:
            db.execute("""
                CREATE TABLE outbound_call_jobs(
                  approval_id TEXT PRIMARY KEY, tenant TEXT NOT NULL,
                  agent TEXT NOT NULL, recipient TEXT NOT NULL,
                  consent_fingerprint TEXT NOT NULL,
                  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                  state TEXT NOT NULL, claimed_at INTEGER, completed_at INTEGER
                )
            """)
        old.chmod(0o600)
        store = DurableCallJobs(old)
        with sqlite3.connect(old) as db:
            columns = {
                row[1] for row in db.execute(
                    "PRAGMA table_info(outbound_call_jobs)"
                )
            }
        self.assertIn("revoked_at", columns)
        approval = store.approve(
            **self.args, consent_reference="old-db-approved-request",
        )
        self.assertEqual(
            store.revoke(approval_id=approval, **self.args)["state"],
            "REVOKED",
        )


if __name__ == "__main__":
    unittest.main()
