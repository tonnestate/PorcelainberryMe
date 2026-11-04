"""Set-based KPI accuracy under many unrelated calls; bounded result rows."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from porcelainberryme.ip_calls import CallRegistry
from porcelainberryme.reporting import call_report


class AggregateQueryTests(unittest.TestCase):
    def test_many_calls_in_other_tenants_and_contacts_do_not_bias_measures(self):
        with tempfile.TemporaryDirectory() as temp:
            now = 1800000000
            dbfile = str(Path(temp) / "calls.sqlite")
            registry = CallRegistry(
                dbfile, identity_secret="local-test-secret-very-long-and-private",
                clock=lambda: now,
            )
            records = []
            for group in range(5):
                for index in range(200):
                    own = group in (0, 1, 2)
                    tenant = "t1" if group != 4 else "t2"
                    caller = ("assistant" if group in (0, 1, 4)
                              else "customer")
                    callee = ("customer" if group in (0, 1, 4)
                              else "assistant" if group == 2 else "other")
                    if group == 3:
                        caller = "other"
                        callee = "customer"
                    accepted = group in (0, 2, 3, 4)
                    status = "ENDED" if accepted else "DECLINED"
                    start = now - 3600 - (index % 2) * 86400
                    ended = start + (70 if accepted else 12)
                    records.append((
                        f"{group*200+index+1:032x}", tenant, caller, callee,
                        int(caller == "assistant"), status, start, ended, ended,
                        start + 10 if accepted else None,
                        f"bulk-test-{group}-{index}",
                    ))
            with sqlite3.connect(dbfile) as db:
                db.executemany("""
                    INSERT INTO ip_calls(
                        id, tenant, caller, callee, agent, state,
                        created_at, expires_at, closed_at, accepted_at, idem
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """, records)
            claims = {"tenant": "t1", "sub": "assistant", "role": "agent"}
            result = call_report(registry, claims)
            metrics = result["metrics"]
            self.assertEqual(metrics["calls"], 600)
            self.assertEqual(metrics["outgoing"], 400)
            self.assertEqual(metrics["incoming"], 200)
            self.assertEqual(metrics["accepted_confirmed"], 400)
            self.assertEqual(metrics["declined"], 200)
            self.assertEqual(metrics["control_session_seconds"], 400 * 60)
            self.assertEqual(metrics["acceptance_unverified"], 0)
            self.assertEqual(len(result["daily"]), 2)
            self.assertEqual(sum(x["calls"] for x in result["daily"]), 600)
            self.assertNotIn("customer", str(result))
            with sqlite3.connect(dbfile) as db:
                indexes = {row[1] for row in db.execute(
                    "PRAGMA index_list('ip_calls')"
                )}
            self.assertIn("ip_call_reporting_caller", indexes)
            self.assertIn("ip_call_reporting_callee", indexes)


if __name__ == "__main__":
    unittest.main()
