"""Real signed loopback HTTP and durable owner-only WhatsApp mirror tests."""
from __future__ import annotations

import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from porcelainberryme.contracts import InboundEvent
from porcelainberryme.ledger import IdempotencyConflict, Ledger
from porcelainberryme.server import handler_for, signature
from porcelainberryme.whatsapp_inbox import OwnerWhatsAppInbox
from porcelainberryme.whatsapp_mirror import OwnerWhatsAppMirror


SECRET = "test-only-original-intake-secret-0123456789"
MIRROR_SECRET = "test-only-independent-owner-secret-0123456789"


def event(**changes):
    props = {
        "tenant_id": "owner_tenant", "channel": "whatsapp",
        "external_id": "w01", "sender": "49151234567@c.us",
        "recipient": "verified_owner", "text": "Guten Tag",
        "conversation_ref": "conversation1", "kind": "text",
    }
    props.update(changes)
    return InboundEvent(**props)


class MirrorStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.location = Path(self.temp.name) / "events.sqlite3"
        self.ledger = Ledger(self.location)
        self.now = [1_700_000_000]
        self.mirror = OwnerWhatsAppMirror(
            str(self.location), tenant="owner_tenant",
            owner_destination="verified_owner", max_rows=100,
            clock=lambda: self.now[0],
        )
        self.inbox = OwnerWhatsAppInbox(
            database=str(self.location), tenant="owner_tenant",
            owner_destination="verified_owner",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_passive_inbox_does_not_create_any_claimable_agent_work(self):
        self.assertTrue(self.mirror.ingest(event())["new"])
        self.assertFalse(self.mirror.ingest(event())["new"])
        self.assertEqual(self.ledger.pending(tenant_id="owner_tenant"), [])
        with self.assertRaises(IdempotencyConflict):
            self.mirror.ingest(event(text="different"))
        projection = self.inbox.recent()
        self.assertEqual(projection["source"],
                         "existing_authenticated_owner_mirror")
        self.assertEqual(len(projection["messages"]), 1)
        self.assertEqual(projection["messages"][0]["text"], "Guten Tag")

    def test_history_cursor_skips_legacy_foreign_owner_rows(self):
        location = Path(self.temp.name) / "legacy-mixed.sqlite3"
        old = Ledger(location)
        old.ingest(event(external_id="authorized-old"))
        for n in range(4):
            old.ingest(event(
                external_id=f"foreign-{n}",
                recipient="different_owner",
            ))
        old.ingest(event(external_id="authorized-new"))
        inbox = OwnerWhatsAppInbox(
            database=str(location), tenant="owner_tenant",
            owner_destination="verified_owner",
        )
        first = inbox.recent(limit=1)
        self.assertEqual(
            [row["external_id"] for row in first["messages"]],
            ["authorized-new"],
        )
        self.assertGreater(first["previous_cursor"], 0)
        second = inbox.recent(before=first["previous_cursor"])
        self.assertEqual(
            [row["external_id"] for row in second["messages"]],
            ["authorized-old"],
        )

    def test_hard_tenant_owner_and_sender_scope(self):
        for props in (
            {"tenant_id": "another"},
            {"recipient": "another_owner"},
            {"channel": "email"},
            {"sender": "group@g.us"},
        ):
            with self.subTest(props=props):
                with self.assertRaisesRegex(PermissionError, "scope_rejected"):
                    self.mirror.ingest(event(**props))
        self.assertEqual(self.inbox.recent()["messages"], [])

    def test_bounded_retention_and_incremental_read(self):
        for n in range(100):
            self.mirror.ingest(event(external_id=f"w{n:03}"))
        with self.assertRaisesRegex(OverflowError, "capacity"):
            self.mirror.ingest(event(external_id="overcap"))
        view = self.inbox.recent(limit=10)
        self.assertEqual(len(view["messages"]), 10)
        self.assertEqual(view["messages"][-1]["external_id"], "w099")
        cursor = view["next_cursor"]
        self.assertEqual(self.inbox.recent(after=cursor)["messages"], [])
        self.now[0] += 31 * 86400
        self.assertTrue(self.mirror.ingest(event(external_id="after-retention"))["new"])
        self.assertEqual(
            [x["external_id"] for x in self.inbox.recent()["messages"]],
            ["after-retention"],
        )
        self.assertEqual(self.mirror.purge_expired(), 0)

    def test_history_pages_reach_all_older_messages_without_duplicates(self):
        for n in range(35):
            self.mirror.ingest(event(external_id=f"history-{n:03}"))
        first = self.inbox.recent(limit=10)
        self.assertEqual(
            [item["external_id"] for item in first["messages"]],
            [f"history-{n:03}" for n in range(25, 35)],
        )
        self.assertEqual(first["next_cursor"], 35)
        older = self.inbox.recent(before=first["previous_cursor"], limit=10)
        self.assertEqual(
            [item["external_id"] for item in older["messages"]],
            [f"history-{n:03}" for n in range(15, 25)],
        )
        oldest = self.inbox.recent(before=older["previous_cursor"], limit=30)
        self.assertEqual(
            [item["external_id"] for item in oldest["messages"]],
            [f"history-{n:03}" for n in range(15)],
        )
        self.assertEqual(
            self.inbox.recent(before=oldest["previous_cursor"])["messages"], [],
        )
        self.mirror.ingest(event(external_id="history-new"))
        fresh = self.inbox.recent(after=first["next_cursor"])
        self.assertEqual([item["external_id"] for item in fresh["messages"]],
                         ["history-new"])

    def test_inbox_rejects_ambiguous_or_invalid_history_cursors(self):
        for kwargs in (
            {"before": -1}, {"before": True},
            {"before": 1 << 63}, {"after": 5, "before": 3},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    self.inbox.recent(**kwargs)

    def test_legacy_inbound_ledger_stays_readable_if_mirror_table_absent(self):
        location = Path(self.temp.name) / "legacy.sqlite3"
        old = Ledger(location)
        old.ingest(event(external_id="legacy-message"))
        view = OwnerWhatsAppInbox(
            database=str(location), tenant="owner_tenant",
            owner_destination="verified_owner",
        ).recent()
        self.assertEqual(view["source"], "existing_authenticated_inbound_ledger")
        self.assertEqual(view["messages"][0]["external_id"], "legacy-message")


class MirrorLoopbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "events.sqlite3"
        self.ledger = Ledger(path)
        self.mirror = OwnerWhatsAppMirror(
            str(path), tenant="owner_tenant",
            owner_destination="verified_owner",
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            handler_for(self.ledger, secret=SECRET, mirror=self.mirror,
                        mirror_secret=MIRROR_SECRET),
        )
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.temp.cleanup()

    def post(self, payload, *, signed=True, endpoint="/v1/owner-whatsapp-mirror",
             key=MIRROR_SECRET):
        body = json.dumps(
            payload, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        stamp = str(int(time.time()))
        headers = {"Content-Type": "application/json"}
        if signed:
            headers["X-PB-Timestamp"] = stamp
            headers["X-PB-Signature"] = signature(key, stamp, body)
        return urlopen(Request(
            f"http://127.0.0.1:{self.server.server_port}{endpoint}",
            method="POST", data=body, headers=headers,
        ), timeout=2)

    def test_owner_mirror_has_distinct_readiness_from_generic_liveness(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        with urlopen(base + "/healthz", timeout=2) as response:
            self.assertEqual(response.status, 200)
        with urlopen(base + "/v1/owner-whatsapp-mirror/readyz",
                     timeout=2) as response:
            result = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(result, {
                "status": "ready", "scope": "owner_whatsapp_mirror",
            })

    def test_unreadable_owner_mirror_returns_503_not_a_false_ready(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        file_path = Path(self.mirror.database)
        file_path.chmod(0o644)
        try:
            with self.assertRaises(HTTPError) as response:
                urlopen(base + "/v1/owner-whatsapp-mirror/readyz",
                        timeout=2)
            self.assertEqual(response.exception.code, 503)
            self.assertEqual(json.loads(response.exception.read()),
                             {"status": "not_ready"})
            # Generic /healthz is deliberately only process liveness.
            with urlopen(base + "/healthz", timeout=2) as response:
                self.assertEqual(response.status, 200)
            with self.assertRaises(PermissionError):
                self.mirror.ingest(event())
        finally:
            file_path.chmod(0o600)

    def test_real_signed_http_idempotency_and_no_agent_queue(self):
        with self.post(event().to_dict()) as response:
            self.assertEqual(response.status, 202)
            self.assertEqual(json.loads(response.read())["state"], "MIRRORED")
        with self.post(event().to_dict()) as response:
            self.assertEqual(response.status, 200)
            self.assertTrue(json.loads(response.read())["duplicate"])
        self.assertEqual(self.ledger.pending(tenant_id="owner_tenant"), [])
        with self.assertRaises(HTTPError) as caught:
            self.post(event(text="changed").to_dict())
        self.assertEqual(caught.exception.code, 409)

    def test_distinct_secret_cannot_cross_into_actionable_inbound_queue(self):
        with self.assertRaises(HTTPError) as wrong_owner_key:
            self.post(event().to_dict(), key=SECRET)
        self.assertEqual(wrong_owner_key.exception.code, 401)
        with self.assertRaises(HTTPError) as wrong_inbound_key:
            self.post(event().to_dict(), endpoint="/v1/inbound", key=MIRROR_SECRET)
        self.assertEqual(wrong_inbound_key.exception.code, 401)
        self.assertEqual(self.ledger.pending(tenant_id="owner_tenant"), [])
        with self.assertRaisesRegex(ValueError, "distinct_signing_secret"):
            handler_for(self.ledger, secret=SECRET, mirror=self.mirror,
                        mirror_secret=SECRET)

    def test_bad_signature_and_foreign_owner_fail_closed(self):
        with self.assertRaises(HTTPError) as unauthorized:
            self.post(event().to_dict(), signed=False)
        self.assertEqual(unauthorized.exception.code, 401)
        with self.assertRaises(HTTPError) as forbidden:
            self.post(event(recipient="other_owner").to_dict())
        self.assertEqual(forbidden.exception.code, 403)
        self.assertEqual(self.ledger.pending(tenant_id="owner_tenant"), [])

    def test_mirror_endpoint_404_without_opt_in(self):
        alternate = ThreadingHTTPServer(
            ("127.0.0.1", 0), handler_for(self.ledger, secret=SECRET),
        )
        worker = threading.Thread(target=alternate.serve_forever, daemon=True)
        worker.start()
        try:
            body = event().to_dict()
            encoded = json.dumps(body, separators=(",", ":")).encode()
            stamp = str(int(time.time()))
            req = Request(
                f"http://127.0.0.1:{alternate.server_port}/v1/owner-whatsapp-mirror",
                method="POST", data=encoded,
                headers={
                    "X-PB-Timestamp": stamp,
                    "X-PB-Signature": signature(SECRET, stamp, encoded),
                },
            )
            with self.assertRaises(HTTPError) as denied:
                urlopen(req, timeout=2)
            self.assertEqual(denied.exception.code, 404)
            with urlopen(
                f"http://127.0.0.1:{alternate.server_port}/healthz",
                timeout=2,
            ) as alive:
                self.assertEqual(alive.status, 200)
            with self.assertRaises(HTTPError) as not_enabled:
                urlopen(
                    f"http://127.0.0.1:{alternate.server_port}"
                    "/v1/owner-whatsapp-mirror/readyz", timeout=2,
                )
            self.assertEqual(not_enabled.exception.code, 404)
        finally:
            alternate.shutdown()
            alternate.server_close()
            worker.join(timeout=2)



class MirrorMaintenanceTests(unittest.TestCase):
    def test_host_only_prune_respects_retention_and_owner_scope(self):
        import contextlib
        from io import StringIO
        import os
        import sqlite3
        import sys
        from unittest.mock import patch
        from porcelainberryme.whatsapp_mirror import main as maintenance

        with tempfile.TemporaryDirectory() as temporary:
            location = Path(temporary) / "private-mirror.sqlite3"
            Ledger(location)
            mine = OwnerWhatsAppMirror(
                str(location), tenant="owner_tenant",
                owner_destination="verified_owner",
            )
            other = OwnerWhatsAppMirror(
                str(location), tenant="another_tenant",
                owner_destination="different_owner",
            )
            mine.ingest(event(external_id="oldmine"))
            other.ingest(event(
                tenant_id="another_tenant", recipient="different_owner",
                external_id="otherowner",
            ))
            with sqlite3.connect(location) as db:
                db.execute("UPDATE whatsapp_owner_mirror SET received_at=0")
            env = {
                "PB_WHATSAPP_MIRROR_ENABLED": "1",
                "PORCELAINBERRY_DB": str(location),
                "PB_WHATSAPP_MIRROR_TENANT": "owner_tenant",
                "PB_WHATSAPP_MIRROR_DESTINATION": "verified_owner",
            }
            output = StringIO()
            with patch.dict(os.environ, env), patch.object(
                    sys, "argv", ["whatsapp_mirror", "prune"]), (
                    contextlib.redirect_stdout(output)):
                maintenance()
            result = json.loads(output.getvalue())
            self.assertEqual(result["purged"], 1)
            self.assertEqual(mine.purge_expired(), 0)
            with other._db() as db:
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM whatsapp_owner_mirror "
                    "WHERE tenant='another_tenant'"
                ).fetchone()[0], 1)

    def test_retention_command_refuses_disabled_and_world_readable_db(self):
        import os
        import sys
        from unittest.mock import patch
        from porcelainberryme.whatsapp_mirror import main as maintenance

        with tempfile.TemporaryDirectory() as temporary:
            location = Path(temporary) / "private-mirror.sqlite3"
            Ledger(location)
            settings = {
                "PORCELAINBERRY_DB": str(location),
                "PB_WHATSAPP_MIRROR_TENANT": "owner_tenant",
                "PB_WHATSAPP_MIRROR_DESTINATION": "verified_owner",
            }
            with patch.dict(os.environ, {
                    **settings, "PB_WHATSAPP_MIRROR_ENABLED": "0",
            }), patch.object(sys, "argv", ["whatsapp_mirror", "prune"]):
                with self.assertRaises(SystemExit):
                    maintenance()
            location.chmod(0o644)
            with patch.dict(os.environ, {
                    **settings, "PB_WHATSAPP_MIRROR_ENABLED": "1",
            }), patch.object(sys, "argv", ["whatsapp_mirror", "prune"]):
                with self.assertRaises(PermissionError):
                    maintenance()


if __name__ == "__main__":
    unittest.main()
