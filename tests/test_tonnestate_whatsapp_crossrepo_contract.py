"""Pinned TonnEstate c96b9516 <-> real PorcelainberryMe loopback contract.

Temporary independent CI acceptance branch; the production repos do not
need a second WhatsApp client or imported private CRM service.
"""
from __future__ import annotations

from contextlib import closing
from http.server import ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import ModuleType
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from porcelainberryme.ledger import Ledger
from porcelainberryme.server import handler_for
from porcelainberryme.whatsapp_mirror import OwnerWhatsAppMirror
from porcelainberryme.whatsapp_inbox import OwnerWhatsAppInbox


# Import only the real, pinned stdlib TonnEstate modules staged in /app/services.
repo = Path(__file__).resolve().parents[1]
package = "_te_real_source_crosscontract"
root = ModuleType(package)
root.__path__ = [str(repo / "app" / "services")]
sys.modules[package] = root
te_outbox = importlib.import_module(package + ".porcelainberry_whatsapp_outbox")
te_mirror = importlib.import_module(package + ".porcelainberry_whatsapp_mirror")

INGRESS_SECRET = "different-inbound-signing-secret-" + "i" * 48
MIRROR_SECRET = "mirror-independent-signing-secret-" + "m" * 48


class CrossRepositoryWhatsAppContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.pb_file = str(root / "pb.sqlite")
        self.te_file = str(root / "te.sqlite")
        self.key_file = root / "private-key"
        self.key_file.write_text(MIRROR_SECRET)
        self.key_file.chmod(0o600)

        self.pb_ledger = Ledger(self.pb_file)
        self.pb_mirror = OwnerWhatsAppMirror(
            self.pb_file, tenant="tenant1", owner_destination="owner1",
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            handler_for(
                self.pb_ledger, secret=INGRESS_SECRET,
                mirror=self.pb_mirror, mirror_secret=MIRROR_SECRET,
            ),
        )
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.env = patch.dict(os.environ, {
            "TE_PB_WHATSAPP_MIRROR_ENABLED": "1",
            "TE_PB_WHATSAPP_OUTBOX_DB": self.te_file,
            "TE_PB_WHATSAPP_OWNER_TENANT": "tenant1",
            "TE_PB_WHATSAPP_OWNER_ID": "owner1",
            "TE_PB_WHATSAPP_SIGNING_SECRET_FILE": str(self.key_file),
        })
        self.env.start()
        self.url = patch.object(
            te_mirror, "_MIRROR_URL",
            f"http://127.0.0.1:{self.server.server_port}/v1/owner-whatsapp-mirror",
        )
        self.url.start()
        self.body = {
            "message_id": "real-provider-stable-id-01",
            "sender": "49151234567@c.us",
            "message": "Hallo über den Bridge-Vertrag.",
            "conversation_id": "thread-01",
            "from_me": False,
        }

    def tearDown(self):
        self.url.stop()
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.temp.cleanup()

    def spool(self, *, changes=None):
        return te_mirror.mirror_owner_inbound(
            {**self.body, **(changes or {})},
            tenant_id="tenant1", owner_id="owner1",
        )

    def store(self, *, clock=None):
        return te_outbox.OwnerWhatsAppMirrorOutbox(
            self.te_file, tenant="tenant1", owner="owner1", clock=clock,
        )

    def inbox(self):
        return OwnerWhatsAppInbox(
            database=self.pb_file, tenant="tenant1",
            owner_destination="owner1",
        ).recent()

    def test_verified_real_http_end_to_end_queued_then_visible_without_agent_work(self):
        with patch.object(te_mirror, "urlopen", wraps=urlopen) as network:
            self.assertTrue(self.spool())
            network.assert_not_called()
            self.assertEqual(self.store().stats()["pending"], 1)
            self.assertEqual(self.inbox()["messages"], [])
            self.assertEqual(self.store().drain_one(te_mirror._signed_push)["state"], "ACKED")
        self.assertEqual(network.call_count, 1)
        self.assertEqual(self.store().stats()["acknowledged"], 1)
        messages = self.inbox()["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["text"], self.body["message"])
        self.assertEqual(messages[0]["external_id"], self.body["message_id"])
        self.assertEqual(self.pb_ledger.pending(tenant_id="tenant1"), [])
        self.assertTrue(self.spool())
        self.assertEqual(self.store().drain_one(te_mirror._signed_push), None)
        self.assertEqual(len(self.inbox()["messages"]), 1)

    def test_ambiguous_lost_reply_is_reconciled_without_double_message(self):
        self.assertTrue(self.spool())
        def lose_receipt(request, timeout=2):
            with urlopen(request, timeout=timeout) as response:
                self.assertEqual(response.status, 202)
                response.read()
            raise TimeoutError("remote commit succeeded but ack got lost")

        with patch.object(te_mirror, "urlopen", side_effect=lose_receipt):
            self.assertEqual(self.store().drain_one(te_mirror._signed_push)["state"], "PENDING")
        self.assertEqual(len(self.inbox()["messages"]), 1)
        future = self.store(clock=lambda: int(time.time()) + 400)
        self.assertEqual(future.drain_one(te_mirror._signed_push)["state"], "ACKED")
        self.assertEqual(len(self.inbox()["messages"]), 1)
        self.assertEqual(self.pb_ledger.pending(tenant_id="tenant1"), [])

    def test_wrong_credential_cannot_access_mirror_or_agent_inbound(self):
        self.assertTrue(self.spool())
        self.key_file.write_text("wrong-signed-mirror-secret-" + "x" * 40)
        with patch.object(te_mirror, "urlopen", wraps=urlopen):
            self.assertEqual(self.store().drain_one(te_mirror._signed_push)["state"], "PENDING")
        self.assertEqual(self.inbox()["messages"], [])
        self.assertEqual(self.pb_ledger.pending(tenant_id="tenant1"), [])

    def test_sender_scope_and_backpressure_remain_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "incoming_event_invalid"):
            self.spool(changes={"sender": "example@g.us", "message_id": "bad-group"})
        self.assertEqual(self.store().stats()["pending"], 0)
        self.assertFalse(Path(self.pb_file).is_symlink())

    def test_full_real_mirror_returns_http_507_and_outbox_retains_then_recovers(self):
        from porcelainberryme.contracts import InboundEvent

        # Exercise the real lower-bound capacity (100 messages), not
        # a slow 10,000-transaction load test in the PR test matrix.
        self.pb_mirror.max_rows = 100
        # Directly fill the current private owner mirror to its bounded
        # capacity. This is the receiving service's real SQLite store.
        for i in range(self.pb_mirror.max_rows):
            self.pb_mirror.ingest(InboundEvent(
                tenant_id="tenant1", channel="whatsapp",
                external_id=f"older-{i:05d}", sender="49151234567@c.us",
                recipient="owner1", text=f"older-{i:05d}",
            ))
        self.assertTrue(self.spool())
        outcome = self.store().drain_one(te_mirror._signed_push)
        self.assertEqual(outcome["state"], "PENDING")
        self.assertEqual(self.store().stats()["pending"], 1)
        self.assertEqual(
            [x["external_id"] for x in self.inbox()["messages"]
             if x["external_id"] == self.body["message_id"]], [],
        )

        # An operator or the retention scheduler safely frees space in
        # the receiving store; the original TonnEstate event must replay.
        with closing(sqlite3.connect(self.pb_file)) as db:
            db.execute(
                "DELETE FROM whatsapp_owner_mirror "
                "WHERE external_id='older-00000'"
            )
        future = self.store(clock=lambda: int(time.time()) + 400)
        self.assertEqual(
            future.drain_one(te_mirror._signed_push)["state"], "ACKED"
        )
        self.assertEqual(self.store().stats()["pending"], 0)
        self.assertEqual(self.store().stats()["acknowledged"], 1)
        self.assertEqual(
            [x["external_id"] for x in self.inbox()["messages"]][-1],
            self.body["message_id"],
        )
        self.assertEqual(self.pb_ledger.pending(tenant_id="tenant1"), [])

    def test_private_mirror_readiness_reports_actual_schema(self):
        self.assertTrue(self.pb_mirror.ready())
        with closing(sqlite3.connect(self.pb_file)) as db:
            db.execute("DROP TABLE whatsapp_owner_mirror")
        self.assertFalse(self.pb_mirror.ready())


if __name__ == "__main__":
    unittest.main()
