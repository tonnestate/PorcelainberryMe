"""Contract tests for the opt-in passive owner WhatsApp mirror."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
import importlib
import ast
import copy
from types import SimpleNamespace
from io import BytesIO
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
from types import ModuleType
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

# Import the two pure-stdlib components without loading the whole CRM Flask app.
# The real CRM package includes independent production-only dependencies.
SERVICES = Path(__file__).resolve().parents[1] / "app" / "services"
PACKAGE = "_pb_whatsapp_mirror_standalone_tests"
namespace = ModuleType(PACKAGE)
namespace.__path__ = [str(SERVICES)]
sys.modules[PACKAGE] = namespace
outbox = importlib.import_module(PACKAGE + ".porcelainberry_whatsapp_outbox")
mirror = importlib.import_module(PACKAGE + ".porcelainberry_whatsapp_mirror")


class Response:
    status = 202

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return b'{"accepted":true,"duplicate":false,"state":"MIRRORED"}'


class OwnerMirrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.secret_file = Path(self.temp.name) / "pb-secret"
        self.secret_file.write_text("z" * 48, encoding="utf-8")
        self.secret_file.chmod(0o600)
        self.outbox_path = str(Path(self.temp.name) / "outbox.sqlite3")
        self.env = patch.dict(os.environ, {
            "TE_PB_WHATSAPP_MIRROR_ENABLED": "1",
            "TE_PB_WHATSAPP_SIGNING_SECRET_FILE": str(self.secret_file),
            "TE_PB_WHATSAPP_OUTBOX_DB": self.outbox_path,
            "TE_PB_WHATSAPP_OWNER_TENANT": "verified_tenant",
            "TE_PB_WHATSAPP_OWNER_ID": "verified_owner",
        })
        self.env.start()
        self.payload = {
            "message_id": "provider-msg-001",
            "sender": "49151234567@c.us",
            "message": "Wie geht es?",
            "conversation_id": "provider-thread-01",
            "from_me": False,
            "tenant_id": "untrusted_tenant",
            "recipient": "untrusted_recipient",
        }

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def push(self, value=None):
        return mirror.mirror_owner_inbound(
            self.payload if value is None else value,
            tenant_id="verified_tenant", owner_id="verified_owner",
        )

    def test_valid_owner_message_is_signed_and_destination_bound(self):
        with patch.object(mirror, "urlopen", return_value=Response()) as send:
            self.assertTrue(self.push())
            send.assert_not_called()  # HTTP ingress does not contact PB.
            store = outbox.OwnerWhatsAppMirrorOutbox(
                self.outbox_path, tenant="verified_tenant",
                owner="verified_owner",
            )
            self.assertEqual(store.stats()["pending"], 1)
            self.assertEqual(store.drain_one(mirror._signed_push)["state"], "ACKED")
        self.assertEqual(send.call_count, 1)
        request = send.call_args.args[0]
        self.assertEqual(request.full_url,
                         "http://127.0.0.1:8873/v1/owner-whatsapp-mirror")
        self.assertEqual(send.call_args.kwargs["timeout"], 2)
        data = json.loads(request.data)
        self.assertEqual(data["tenant_id"], "verified_tenant")
        self.assertEqual(data["recipient"], "verified_owner")
        self.assertNotEqual(data["tenant_id"], self.payload["tenant_id"])
        self.assertNotEqual(data["recipient"], self.payload["recipient"])
        self.assertEqual(data["text"], "Wie geht es?")
        stamp = request.get_header("X-pb-timestamp")
        signature = request.get_header("X-pb-signature")
        expected = "sha256=" + hmac.new(
            ("z" * 48).encode(), stamp.encode("ascii") + b"." + request.data,
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(signature, expected)
        stats = outbox.OwnerWhatsAppMirrorOutbox(
            self.outbox_path, tenant="verified_tenant",
            owner="verified_owner",
        ).stats()
        self.assertEqual(stats["acknowledged"], 1)
        self.assertEqual(stats["pending"], 0)

    def test_opt_out_and_nonincoming_are_ignored_without_network_access(self):
        with patch.object(mirror, "urlopen") as send:
            with patch.dict(os.environ, {"TE_PB_WHATSAPP_MIRROR_ENABLED": "0"}):
                self.assertFalse(self.push())
            self.assertFalse(self.push({**self.payload, "from_me": True}))
            self.assertTrue(self.push({**self.payload, "from_me": None}))
            self.assertTrue(self.push({**self.payload, "from_me": "false"}))
            with self.assertRaisesRegex(ValueError, "incoming_event_invalid"):
                self.push({**self.payload, "sender": "group@g.us"})
            with self.assertRaisesRegex(ValueError, "incoming_event_invalid"):
                self.push({**self.payload, "message_id": ""})
            with self.assertRaisesRegex(ValueError, "incoming_event_invalid"):
                self.push({**self.payload, "message_id": "provider" + chr(10) + "bad"})
            with self.assertRaisesRegex(ValueError, "incoming_event_invalid"):
                self.push({**self.payload, "sender": "49151" + chr(13) + "bad@c.us"})
            with self.assertRaisesRegex(ValueError, "incoming_event_invalid"):
                self.push({**self.payload, "message": ""})
            send.assert_not_called()

    def test_missing_from_me_follows_existing_aurora_inbound_contract(self):
        incoming_without_flag = dict(self.payload)
        incoming_without_flag.pop("from_me")
        self.assertTrue(self.push(incoming_without_flag))
        store = outbox.OwnerWhatsAppMirrorOutbox(
            self.outbox_path, tenant="verified_tenant",
            owner="verified_owner",
        )
        self.assertEqual(store.stats()["pending"], 1)
        with patch.object(mirror, "urlopen", return_value=Response()) as network:
            self.assertEqual(
                store.drain_one(mirror._signed_push)["state"], "ACKED"
            )
            self.assertEqual(network.call_count, 1)

    def test_worker_owner_scope_mismatch_rejected_before_enqueuing(self):
        with patch.object(mirror, "urlopen") as outbound:
            for config in (
                {"TE_PB_WHATSAPP_OWNER_TENANT": ""},
                {"TE_PB_WHATSAPP_OWNER_ID": ""},
                {"TE_PB_WHATSAPP_OWNER_TENANT": "wrong_tenant"},
                {"TE_PB_WHATSAPP_OWNER_ID": "wrong_owner"},
            ):
                with self.subTest(config=config), patch.dict(os.environ, config):
                    with self.assertRaisesRegex(
                        PermissionError, "worker_scope_mismatch"
                    ):
                        self.push()
            outbound.assert_not_called()
        self.assertFalse(Path(self.outbox_path).exists())

    def test_same_owner_ingress_and_worker_share_pending_queue(self):
        self.assertTrue(self.push())
        store = outbox.OwnerWhatsAppMirrorOutbox(
            self.outbox_path,
            tenant=os.environ["TE_PB_WHATSAPP_OWNER_TENANT"],
            owner=os.environ["TE_PB_WHATSAPP_OWNER_ID"],
        )
        self.assertEqual(store.stats()["pending"], 1)
        self.assertEqual(store.stats()["acknowledged"], 0)

    def test_private_secret_missing_keeps_durable_message_for_later_retry(self):
        self.secret_file.chmod(0o644)
        with patch.object(mirror, "urlopen") as send:
            self.assertTrue(self.push())
            send.assert_not_called()
        stats = outbox.OwnerWhatsAppMirrorOutbox(
            self.outbox_path, tenant="verified_tenant",
            owner="verified_owner",
        ).stats()
        self.assertEqual(stats["pending"], 1)
        self.secret_file.chmod(0o600)
        restored = outbox.OwnerWhatsAppMirrorOutbox(
            self.outbox_path, tenant="verified_tenant",
            owner="verified_owner", clock=lambda: int(time.time()) + 1000,
        )
        with patch.object(mirror, "urlopen", return_value=Response()):
            self.assertEqual(restored.drain_one(mirror._signed_push)["state"], "ACKED")
        self.assertEqual(restored.stats()["pending"], 0)

    def test_failed_network_attempt_is_durably_recoverable_without_leaking(self):
        with patch.object(
            mirror, "urlopen", side_effect=RuntimeError(
                "sensitive-provider-msg 49151234567@c.us"
            )
        ) as send:
            self.assertTrue(self.push())
            send.assert_not_called()
            active = outbox.OwnerWhatsAppMirrorOutbox(
                self.outbox_path, tenant="verified_tenant",
                owner="verified_owner",
            )
            self.assertEqual(active.drain_one(mirror._signed_push)["state"], "PENDING")
            self.assertEqual(send.call_count, 1)
        store = outbox.OwnerWhatsAppMirrorOutbox(
            self.outbox_path, tenant="verified_tenant",
            owner="verified_owner",
        )
        self.assertEqual(store.stats()["pending"], 1)
        self.assertEqual(store.stats()["acknowledged"], 0)
        again = outbox.OwnerWhatsAppMirrorOutbox(
            self.outbox_path, tenant="verified_tenant",
            owner="verified_owner", clock=lambda: int(time.time()) + 1000,
        )
        with patch.object(mirror, "urlopen", return_value=Response()) as sender:
            self.assertEqual(again.drain_one(mirror._signed_push)["state"], "ACKED")
            self.assertEqual(sender.call_count, 1)

    def test_n8n_replay_keeps_original_provider_event_id(self):
        captured = []
        def send(request, timeout):
            captured.append(json.loads(request.data))
            return Response()
        with patch.object(mirror, "urlopen", side_effect=send):
            self.assertTrue(self.push())
            self.assertTrue(self.push())
            self.assertEqual(captured, [])
            active = outbox.OwnerWhatsAppMirrorOutbox(
                self.outbox_path, tenant="verified_tenant",
                owner="verified_owner",
            )
            self.assertEqual(active.drain_one(mirror._signed_push)["state"], "ACKED")
        self.assertEqual(
            [event["external_id"] for event in captured],
            ["provider-msg-001"],
        )
        store = outbox.OwnerWhatsAppMirrorOutbox(
            self.outbox_path, tenant="verified_tenant",
            owner="verified_owner",
        )
        self.assertEqual(store.stats()["acknowledged"], 1)

    def test_changed_same_provider_id_refused_even_after_first_ack(self):
        with patch.object(mirror, "urlopen", return_value=Response()):
            self.assertTrue(self.push())
        with self.assertRaisesRegex(ValueError, "outbox_id_conflict"):
            self.push({**self.payload, "message": "changed body"})

    def test_concurrent_drain_claims_once_and_crash_claim_is_recoverable(self):
        with patch.object(mirror, "urlopen", side_effect=TimeoutError()):
            self.assertTrue(self.push())
        store = outbox.OwnerWhatsAppMirrorOutbox(
            self.outbox_path, tenant="verified_tenant",
            owner="verified_owner", clock=lambda: int(time.time()) + 1000,
        )
        calls = []
        def dispatch(_payload):
            time.sleep(0.04)
            calls.append(1)
            return True
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(lambda _: store.drain_one(dispatch), range(8)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(sum(result is not None for result in outcomes), 1)
        self.assertEqual(store.stats()["acknowledged"], 1)

        second = {**self.payload, "message_id": "provider-crash-002"}
        with patch.object(mirror, "urlopen", return_value=Response()):
            self.assertTrue(self.push(second))
        with sqlite3.connect(self.outbox_path) as connection:
            connection.execute(
                "UPDATE owner_whatsapp_mirror_outbox "
                "SET state='INFLIGHT',lease_until=0,claim_token='stale' "
                "WHERE external_id='provider-crash-002'"
            )
        fresh = outbox.OwnerWhatsAppMirrorOutbox(
            self.outbox_path, tenant="verified_tenant",
            owner="verified_owner",
        )
        self.assertEqual(fresh.drain_one(lambda payload: True)["state"], "ACKED")

    def test_full_or_world_readable_outbox_rejected_and_scope_is_separate(self):
        with patch.object(mirror, "urlopen", return_value=Response()):
            self.assertTrue(self.push())
        path = Path(self.outbox_path)
        path.chmod(0o644)
        with self.assertRaisesRegex(PermissionError, "outbox_not_private"):
            self.push({**self.payload, "message_id": "provider-new-msg"})
        path.chmod(0o600)
        other = outbox.OwnerWhatsAppMirrorOutbox(
            self.outbox_path, tenant="foreign", owner="foreign",
        )
        self.assertEqual(other.stats()["acknowledged"], 0)
        self.assertEqual(other.stats()["pending"], 0)

    def test_pruning_removes_only_confirmed_records(self):
        with patch.object(mirror, "urlopen", return_value=Response()):
            self.assertTrue(self.push())
            self.assertEqual(
                outbox.OwnerWhatsAppMirrorOutbox(
                    self.outbox_path, tenant="verified_tenant",
                    owner="verified_owner",
                ).drain_one(mirror._signed_push)["state"], "ACKED"
            )
        with sqlite3.connect(self.outbox_path) as db:
            db.execute(
                "UPDATE owner_whatsapp_mirror_outbox SET updated_at=0 "
                "WHERE state='ACKED'"
            )
        store = outbox.OwnerWhatsAppMirrorOutbox(
            self.outbox_path, tenant="verified_tenant",
            owner="verified_owner",
        )
        self.assertEqual(store.prune_confirmed(), 1)
        self.assertEqual(store.stats()["acknowledged"], 0)

    def test_pending_outbox_staleness_is_reported_without_any_private_text(self):
        with patch.object(mirror, "urlopen", side_effect=TimeoutError()):
            self.assertTrue(self.push())
        old = outbox.OwnerWhatsAppMirrorOutbox(
            self.outbox_path, tenant="verified_tenant",
            owner="verified_owner", clock=lambda: int(time.time()) + 301,
        )
        metrics = old.stats()
        self.assertEqual(metrics["pending"], 1)
        self.assertGreater(metrics["oldest_pending_seconds"], 300)
        self.assertNotIn("Wie geht es?", json.dumps(metrics))

    def test_outbox_capacity_does_not_silently_evict_unconfirmed_entries(self):
        store = outbox.OwnerWhatsAppMirrorOutbox(
            self.outbox_path, tenant="verified_tenant",
            owner="verified_owner", capacity=100,
        )
        template = {
            "tenant_id": "verified_tenant", "channel": "whatsapp",
            "recipient": "verified_owner", "sender": "49151234567@c.us",
            "text": "Hello", "kind": "text", "conversation_ref": "one",
        }
        for item in range(100):
            store.enqueue({**template, "external_id": f"provider-{item:03}"})
        with self.assertRaisesRegex(OverflowError, "outbox_full"):
            store.enqueue({**template, "external_id": "provider-new"})
        self.assertEqual(store.stats()["pending"], 100)

    def test_new_ingress_prunes_only_aged_acknowledged_rows(self):
        with patch.object(mirror, "urlopen", return_value=Response()):
            self.assertTrue(self.push())
            self.assertEqual(
                outbox.OwnerWhatsAppMirrorOutbox(
                    self.outbox_path, tenant="verified_tenant",
                    owner="verified_owner",
                ).drain_one(mirror._signed_push)["state"], "ACKED"
            )
        with sqlite3.connect(self.outbox_path) as db:
            db.execute(
                "UPDATE owner_whatsapp_mirror_outbox SET updated_at=0 "
                "WHERE state='ACKED'"
            )
        another = {**self.payload, "message_id": "provider-new-after-retention"}
        with patch.object(mirror, "urlopen", side_effect=TimeoutError()):
            self.assertTrue(self.push(another))
        store = outbox.OwnerWhatsAppMirrorOutbox(
            self.outbox_path, tenant="verified_tenant",
            owner="verified_owner",
        )
        self.assertEqual(store.stats()["acknowledged"], 0)
        self.assertEqual(store.stats()["pending"], 1)

    def test_corrupt_json_is_quarantined_without_network_or_worker_crash(self):
        self.assertTrue(self.push())
        with sqlite3.connect(self.outbox_path) as db:
            db.execute(
                "UPDATE owner_whatsapp_mirror_outbox SET payload=? "
                "WHERE external_id='provider-msg-001'",
                ('{"incomplete":',),
            )
        store = outbox.OwnerWhatsAppMirrorOutbox(
            self.outbox_path, tenant="verified_tenant",
            owner="verified_owner",
        )
        with patch.object(mirror, "urlopen") as network:
            self.assertEqual(store.drain_one(mirror._signed_push),
                             {"state": "QUARANTINED", "acknowledged": False})
            network.assert_not_called()
        result = store.stats()
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["quarantined"], 1)
        self.assertNotIn("incomplete", json.dumps(result))
        self.assertIsNone(store.drain_one(mirror._signed_push))
        store.heartbeat()
        settings = {
            "TE_PB_WHATSAPP_MIRROR_ENABLED": "1",
            "TE_PB_WHATSAPP_OUTBOX_DB": self.outbox_path,
            "TE_PB_WHATSAPP_OWNER_TENANT": "verified_tenant",
            "TE_PB_WHATSAPP_OWNER_ID": "verified_owner",
        }
        with patch.dict(os.environ, settings), patch.object(
                sys, "argv", ["mirror-outbox", "check"]), (
                redirect_stdout(StringIO())):
            with self.assertRaises(SystemExit) as failed:
                outbox.main()
        self.assertEqual(failed.exception.code, 2)

    def test_recomputed_digest_cannot_forward_message_to_other_owner(self):
        self.assertTrue(self.push())
        with sqlite3.connect(self.outbox_path) as db:
            raw = db.execute(
                "SELECT payload FROM owner_whatsapp_mirror_outbox "
                "WHERE external_id='provider-msg-001'",
            ).fetchone()[0]
            altered = json.loads(raw)
            altered["recipient"] = "different_owner"
            encoded = json.dumps(
                altered, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            db.execute(
                "UPDATE owner_whatsapp_mirror_outbox "
                "SET payload=?,digest=? WHERE external_id='provider-msg-001'",
                (encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()),
            )
        store = outbox.OwnerWhatsAppMirrorOutbox(
            self.outbox_path, tenant="verified_tenant",
            owner="verified_owner",
        )
        with patch.object(mirror, "urlopen") as network:
            self.assertEqual(
                store.drain_one(mirror._signed_push)["state"], "QUARANTINED"
            )
            network.assert_not_called()

        new_payload = {**self.payload, "message_id": "provider-fresh-002"}
        self.assertTrue(self.push(new_payload))
        with patch.object(mirror, "urlopen", return_value=Response()) as network:
            self.assertEqual(
                store.drain_one(mirror._signed_push)["state"], "ACKED"
            )
            self.assertEqual(network.call_count, 1)
        self.assertEqual(store.stats()["quarantined"], 1)
        self.assertEqual(store.stats()["acknowledged"], 1)

    def test_idle_worker_health_absent_and_stale_even_when_queue_empty(self):
        now = [1_800_000_000]
        store = outbox.OwnerWhatsAppMirrorOutbox(
            self.outbox_path, tenant="verified_tenant",
            owner="verified_owner", clock=lambda: now[0],
        )
        before = store.stats()
        self.assertEqual(before["pending"], 0)
        self.assertIsNone(before["worker_heartbeat_age_seconds"])
        self.assertFalse(before["worker_healthy"])
        store.heartbeat()
        self.assertTrue(store.stats()["worker_healthy"])
        self.assertEqual(store.stats()["worker_heartbeat_age_seconds"], 0)
        now[0] += 61
        self.assertEqual(store.stats()["pending"], 0)
        self.assertEqual(store.stats()["worker_heartbeat_age_seconds"], 61)
        self.assertFalse(store.stats()["worker_healthy"])
        store.heartbeat()
        self.assertTrue(store.stats()["worker_healthy"])
        now[0] -= 120
        self.assertFalse(store.stats()["worker_healthy"])
        self.assertLess(store.stats()["worker_heartbeat_age_seconds"], 0)

    def test_worker_heartbeat_is_tenant_and_owner_scoped(self):
        store = outbox.OwnerWhatsAppMirrorOutbox(
            self.outbox_path, tenant="verified_tenant", owner="verified_owner",
        )
        other = outbox.OwnerWhatsAppMirrorOutbox(
            self.outbox_path, tenant="another_tenant", owner="another_owner",
        )
        self.assertFalse(store.stats()["worker_healthy"])
        self.assertFalse(other.stats()["worker_healthy"])
        store.heartbeat()
        self.assertTrue(store.stats()["worker_healthy"])
        self.assertFalse(other.stats()["worker_healthy"])

    def test_real_worker_health_cli_requires_fresh_heartbeat(self):
        settings = {
            "TE_PB_WHATSAPP_MIRROR_ENABLED": "1",
            "TE_PB_WHATSAPP_OUTBOX_DB": self.outbox_path,
            "TE_PB_WHATSAPP_OWNER_TENANT": "verified_tenant",
            "TE_PB_WHATSAPP_OWNER_ID": "verified_owner",
        }
        with patch.dict(os.environ, settings), patch.object(
                sys, "argv", ["mirror-outbox", "check"]), (
                redirect_stdout(StringIO())):
            with self.assertRaises(SystemExit) as absent:
                outbox.main()
        self.assertEqual(absent.exception.code, 2)

        store = outbox.OwnerWhatsAppMirrorOutbox(
            self.outbox_path, tenant="verified_tenant", owner="verified_owner",
        )
        store.heartbeat()
        output = StringIO()
        with patch.dict(os.environ, settings), patch.object(
                sys, "argv", ["mirror-outbox", "check"]), redirect_stdout(output):
            outbox.main()
        health = json.loads(output.getvalue())
        self.assertTrue(health["worker_healthy"])
        self.assertEqual(health["pending"], 0)

        with sqlite3.connect(self.outbox_path) as db:
            db.execute(
                "UPDATE owner_whatsapp_mirror_worker SET last_seen=0 "
                "WHERE tenant='verified_tenant' AND owner='verified_owner'",
            )
        with patch.dict(os.environ, settings), patch.object(
                sys, "argv", ["mirror-outbox", "check"]), (
                redirect_stdout(StringIO())):
            with self.assertRaises(SystemExit) as expired:
                outbox.main()
        self.assertEqual(expired.exception.code, 2)

    def _isolated_real_reply_handler(self, mirror_function, *, enabled=True):
        """Execute the real route body without importing Flask/SQLAlchemy.

        This keeps the semantics under test tied to the production function
        rather than a test-only reimplementation.
        """
        filename = Path(__file__).resolve().parents[1] / (
            "app/api/aurora_whatsapp_internal_api.py"
        )
        parsed = ast.parse(filename.read_text(encoding="utf-8"), filename=str(filename))
        original = next(
            fn for fn in parsed.body
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            and fn.name == "reply"
        )
        body = copy.deepcopy(original)
        body.decorator_list = []
        function_module = ast.fix_missing_locations(
            ast.Module(body=[body], type_ignores=[])
        )
        calls = {"gateway": 0, "mirror": 0, "logged": []}

        def mirror_call(*args, **kwargs):
            calls["mirror"] += 1
            return mirror_function(*args, **kwargs)

        def gateway():
            def handle(payload, **kwargs):
                calls["gateway"] += 1
                return {"send": True, "text": "mocked reply"}
            return SimpleNamespace(handle=handle)

        errors = []
        context = {
            "request": SimpleNamespace(get_json=lambda silent=True: {
                "message": "Hello",
                "from_me": False,
                "message_id": "provider-id",
                "sender": "49151234567@c.us",
            }),
            "_owner_scope": lambda: ("verified_tenant", "verified_owner", "personal"),
            "get_db": lambda: object(),
            "is_enabled": lambda *_args: enabled,
            "mirror_owner_inbound": mirror_call,
            "_gateway": gateway,
            "current_app": SimpleNamespace(logger=SimpleNamespace(
                error=lambda message: calls["logged"].append(message),
                exception=lambda message: calls["logged"].append(message),
            )),
            "success_response": lambda result: ("SUCCESS", result),
            "error_response": lambda code, message, **kwargs: (
                "ERROR", code, kwargs.get("status", 400), message,
            ),
        }
        exec(compile(function_module, str(filename), "exec"), context)
        return context["reply"], calls

    def test_actual_reply_handler_rejects_undurable_event_with_http_503(self):
        def impossible_to_persist(*_args, **_kwargs):
            raise OverflowError("private PII and path must not be logged")

        reply, calls = self._isolated_real_reply_handler(impossible_to_persist)
        result = reply()
        self.assertEqual(result[:3], (
            "ERROR", "WHATSAPP_MIRROR_PERSIST_FAILED", 503,
        ))
        self.assertEqual(calls["mirror"], 1)
        self.assertEqual(calls["gateway"], 0)
        self.assertEqual(
            calls["logged"], ["porcelainberry_whatsapp_mirror_persist_failed"]
        )
        self.assertNotIn("private PII", str(result))

    def test_actual_reply_handler_rejects_invalid_json_shape(self):
        reply, calls = self._isolated_real_reply_handler(
            lambda *_args, **_kwargs: True,
        )
        reply.__globals__["request"] = SimpleNamespace(
            get_json=lambda silent=True: ["not", "an", "object"],
        )
        result = reply()
        self.assertEqual(result[:3], ("ERROR", "VALIDATION_ERROR", 400))
        self.assertEqual(calls["mirror"], 0)
        self.assertEqual(calls["gateway"], 0)

    def test_actual_reply_handler_rejects_missing_incoming_id(self):
        def invalid_provider_event(*_args, **_kwargs):
            raise ValueError("pb_mirror_incoming_event_invalid")

        reply, calls = self._isolated_real_reply_handler(invalid_provider_event)
        self.assertEqual(
            reply()[:3], ("ERROR", "WHATSAPP_MIRROR_PERSIST_FAILED", 503)
        )
        self.assertEqual(calls["gateway"], 0)

    def test_actual_reply_handler_keeps_original_when_durable(self):
        reply, calls = self._isolated_real_reply_handler(
            lambda *_args, **_kwargs: True,
        )
        result = reply()
        self.assertEqual(result[0], "SUCCESS")
        self.assertEqual(result[1], {"send": True, "text": "mocked reply"})
        self.assertEqual(calls["mirror"], 1)
        self.assertEqual(calls["gateway"], 1)

    def test_actual_reply_handler_mirrors_even_with_autoreply_disabled(self):
        reply, calls = self._isolated_real_reply_handler(
            lambda *_args, **_kwargs: True, enabled=False,
        )
        self.assertEqual(
            reply(), ("SUCCESS", {"send": False, "text": "", "decision": "MASTER_OFF"})
        )
        self.assertEqual(calls["mirror"], 1)
        self.assertEqual(calls["gateway"], 0)

    def test_existing_reply_endpoint_calls_optional_mirror_after_owner_verification(self):
        source = (
            Path(__file__).resolve().parents[1] /
            "app/api/aurora_whatsapp_internal_api.py"
        ).read_text(encoding="utf-8")
        owner_check = source.index("tenant_id, owner_id, domain_id = _owner_scope()")
        mirror_call = source.index("            mirror_owner_inbound(")
        current_flow = source.index("        result = _gateway().handle(")
        self.assertLess(owner_check, mirror_call)
        master_off = source.index("        if not is_enabled(get_db(), tenant_id, owner_id):")
        self.assertLess(mirror_call, master_off)
        self.assertLess(master_off, current_flow)
        self.assertIn('"WHATSAPP_MIRROR_PERSIST_FAILED"', source)
        self.assertIn("status=503", source)
        self.assertIn(
            'current_app.logger.error("porcelainberry_whatsapp_mirror_persist_failed")',
            source,
        )


if __name__ == "__main__":
    unittest.main()
