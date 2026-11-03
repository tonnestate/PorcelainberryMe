"""Privacy, authorization, reliability and SSRF tests for self-hosted Web Push."""
from __future__ import annotations

import base64
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from porcelainberryme.ip_calls import (
    CallError, CallRegistry, make_handler, mint_identity_token,
)
from porcelainberryme.web_push import (
    ExpiredPushEndpoint, validate_subscription, validate_public_key,
)


def key(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class PushIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = str(Path(self.folder.name) / "calls.sqlite")
        self.time = [1_800_000_000]
        self.secret = "test-identity-key-345678901234567890123"
        self.vapid = key(b"\x04" + bytes(range(1, 65)))
        self.db = CallRegistry(
            self.path, identity_secret=self.secret,
            push_public_key=self.vapid, clock=lambda: self.time[0],
        )
        self.agent = {"tenant": "shop_a", "sub": "ai", "role": "agent"}
        self.human = {"tenant": "shop_a", "sub": "buyer", "role": "human"}
        self.staff = {"tenant": "shop_a", "sub": "staff", "role": "human"}
        self.other = {"tenant": "shop_b", "sub": "buyer", "role": "human"}
        self.db.provision("shop_a", "ai")
        self.db.provision("shop_a", "buyer", allow_agent_calls=True)
        self.db.provision("shop_a", "staff")
        self.db.provision("shop_b", "buyer")

    def subscription(self, device="a" * 32, host="web.push.apple.com"):
        return {
            "device_id": device,
            "endpoint": f"https://{host}/v1/device/{device}",
            "keys": {
                "p256dh": key(b"\x04" + bytes(range(1, 65))),
                "auth": key(bytes(range(16))),
            },
        }

    def register(self, device="a" * 32):
        return self.db.register_push(self.human, self.subscription(device=device))

    def ring(self, recipient="buyer"):
        return self.db.originate(
            self.agent, recipient, "push-call-00001",
        )

    def test_explicit_registration_and_durable_queued_incoming_notification(self):
        self.register()
        created = self.ring()
        self.assertEqual(created["state"], "RINGING")
        delivered = []
        result = self.db.reconcile_push(
            lambda subscription, payload, ttl:
                delivered.append((subscription, payload, ttl))
        )
        self.assertEqual(result["accepted_by_push_service"], 1)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0][1], {
            "type": "incoming_ip_call", "call_id": created["id"],
            "kind": "RINGING",
        })
        self.assertLessEqual(delivered[0][2], 45)
        self.assertGreater(delivered[0][2], 0)
        self.assertNotIn("buyer", json.dumps(delivered[0][1]))
        self.assertNotIn("ai", json.dumps(delivered[0][1]))
        self.assertEqual(self.db.reconcile_push(lambda *args: None)["attempted"], 0)

    def test_registration_is_off_by_default_and_agent_cannot_subscribe(self):
        unconfigured = CallRegistry(
            str(Path(self.folder.name) / "unconfigured.sqlite"),
            identity_secret=self.secret,
        )
        unconfigured.provision("shop_a", "buyer", allow_agent_calls=True)
        with self.assertRaisesRegex(CallError, "webpush_not_configured"):
            unconfigured.register_push(self.human, self.subscription())
        with self.assertRaisesRegex(CallError, "human_push_only"):
            self.db.register_push(self.agent, self.subscription())
        self.ring()
        self.assertEqual(self.db.reconcile_push(lambda *args: None)["attempted"], 0)

    def test_cannot_register_endpoints_on_local_or_malicious_hosts(self):
        for host in (
            "127.0.0.1", "localhost", "169.254.169.254",
            "web.push.apple.com.attacker.example", "evil.push.apple.com.example",
            "example.com", "fcm.googleapis.com.attacker.com",
            "user@web.push.apple.com",
        ):
            info = self.subscription(host=host)
            with self.subTest(host=host):
                with self.assertRaises(CallError):
                    validate_subscription(info)
        info = self.subscription()
        info["endpoint"] = "http://web.push.apple.com/v1/device"
        with self.assertRaisesRegex(CallError, "unsupported_push_service"):
            validate_subscription(info)
        info = self.subscription()
        info["endpoint"] = "https://web.push.apple.com:8443/v1/device"
        with self.assertRaisesRegex(CallError, "unsupported_push_service"):
            validate_subscription(info)
        info = self.subscription()
        info["keys"]["p256dh"] = "not-a-curve"
        with self.assertRaisesRegex(CallError, "invalid_push_keys"):
            validate_subscription(info)

    def test_tenant_device_isolation_and_endpoint_ownership(self):
        self.register()
        with self.assertRaisesRegex(CallError, "push_endpoint_already_owned"):
            self.db.register_push(self.other, self.subscription())
        with self.assertRaisesRegex(CallError, "human_push_only"):
            self.db.revoke_push(self.agent, "a" * 32)
        self.assertEqual(
            self.db.revoke_push(self.other, "a" * 32)["removed"], False,
        )
        self.assertEqual(self.db.revoke_push(self.human, "a" * 32), {
            "enabled": False, "removed": True,
        })
        self.assertTrue(self.db.register_push(self.other, self.subscription())["enabled"])

    def test_no_push_after_decline_timeout_or_dnd(self):
        self.register()
        first = self.ring()
        self.db.decide(self.human, first["id"], "decline")
        self.assertEqual(self.db.reconcile_push(lambda *args: None)["attempted"], 0)
        self.time[0] += 121
        second = self.db.originate(self.agent, "buyer", "push-call-00002")
        self.time[0] += 46
        self.assertEqual(self.db.status(self.human, second["id"])["state"], "TIMED_OUT")
        self.assertEqual(self.db.reconcile_push(lambda *args: None)["attempted"], 0)
        self.db.set_do_not_disturb("shop_a", "buyer", True)
        with self.assertRaisesRegex(CallError, "recipient_do_not_disturb"):
            self.db.originate(self.agent, "buyer", "push-call-00003")

    def test_restart_preserves_subscription_and_pending_delivery(self):
        self.register()
        created = self.ring()
        restarted = CallRegistry(
            self.path, identity_secret=self.secret,
            push_public_key=self.vapid, clock=lambda: self.time[0],
        )
        sent = []
        self.assertEqual(restarted.reconcile_push(
            lambda subscription, payload, ttl: sent.append(payload),
        )["accepted_by_push_service"], 1)
        self.assertEqual(sent[0]["call_id"], created["id"])

    def test_410_removes_expired_subscription_without_disabling_call(self):
        self.register()
        created = self.ring()
        def gone(subscription, payload, ttl):
            raise ExpiredPushEndpoint()
        result = self.db.reconcile_push(gone)
        self.assertEqual(result["expired_subscriptions"], 1)
        self.assertEqual(self.db.status(self.human, created["id"])["state"], "RINGING")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM ip_push_subscriptions"
            ).fetchone()[0], 0)
        self.assertEqual(self.db.reconcile_push(gone)["attempted"], 0)

    def test_retries_bounded_and_no_retry_after_ring_expires(self):
        self.register()
        self.ring()
        messages = []
        def fail(subscription, payload, ttl):
            messages.append(ttl)
            raise OSError("local network down")
        self.assertEqual(self.db.reconcile_push(fail)["failures"], 1)
        self.assertEqual(self.db.reconcile_push(fail)["attempted"], 0)
        self.time[0] += 2
        self.assertEqual(self.db.reconcile_push(fail)["failures"], 1)
        self.time[0] += 46
        self.assertEqual(self.db.reconcile_push(fail)["attempted"], 0)
        self.assertEqual(messages, [45, 43])

    def test_registration_updates_same_device_and_does_not_replay_old_pending(self):
        self.register()
        self.ring()
        new = self.subscription()
        new["endpoint"] = new["endpoint"].replace("/device/", "/rotated/")
        self.db.register_push(self.human, new)
        self.assertEqual(self.db.reconcile_push(lambda *args: None)["attempted"], 0)
        self.assertEqual(self.db.revoke_push(self.human, "a" * 32)["removed"], True)

    def test_five_device_limit_and_idempotent_replay(self):
        for index in range(5):
            device = f"{index + 1:032x}"
            self.db.register_push(self.human, self.subscription(device))
        self.db.register_push(self.human, self.subscription(f"{1:032x}"))
        with self.assertRaisesRegex(CallError, "push_device_limit_reached"):
            self.db.register_push(
                self.human, self.subscription(f"{6:032x}"),
            )

    def test_handoff_to_staff_generates_staff_push_not_customer_details(self):
        self.db.register_push(self.staff, self.subscription())
        self.db.configure_handoff_route("shop_a", "ai", "staff")
        created = self.ring()
        self.db.decide(self.human, created["id"], "accept")
        self.db.offer_handoff(self.agent, created["id"],
                              idempotency_key="handoff-push-00001")
        sent = []
        self.assertEqual(self.db.reconcile_push(
            lambda sub, payload, ttl: sent.append(payload),
        )["accepted_by_push_service"], 1)
        self.assertEqual(sent[0]["kind"], "HANDOFF_OFFERED")
        self.assertEqual(sent[0]["call_id"], created["id"])
        self.assertNotIn("shop_a", json.dumps(sent[0]))
        self.assertNotIn("buyer", json.dumps(sent[0]))

    def test_no_push_for_accepted_staff_offer(self):
        self.db.register_push(self.staff, self.subscription())
        self.db.configure_handoff_route("shop_a", "ai", "staff")
        created = self.ring()
        self.db.decide(self.human, created["id"], "accept")
        self.db.offer_handoff(self.agent, created["id"],
                              idempotency_key="handoff-push-00002")
        self.db.decide_handoff(self.staff, created["id"], "accept")
        self.assertEqual(self.db.reconcile_push(lambda *args: None)["attempted"], 0)

    def test_authenticated_http_routes_and_no_auth_leak(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.db))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        root = f"http://127.0.0.1:{server.server_port}"

        def req(method, path, claims=None, body=None):
            headers = {}
            if claims is not None:
                headers["Authorization"] = "Bearer " + mint_identity_token(
                    self.secret, claims["tenant"], claims["sub"],
                    claims["role"], now=self.time[0],
                )
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            request = Request(root + path, headers=headers, data=data, method=method)
            try:
                with urlopen(request, timeout=2) as res:
                    return res.status, json.loads(res.read())
            except HTTPError as exc:
                return exc.code, json.loads(exc.read())

        self.assertEqual(req("GET", "/v1/ip-calls/push/config")[0], 401)
        self.assertEqual(req("GET", "/v1/ip-calls/push/config", self.human),
                         (200, {"vapid_public_key": self.vapid}))
        self.assertEqual(req(
            "POST", "/v1/ip-calls/push/subscriptions", self.human,
            self.subscription(),
        )[0], 201)
        self.assertEqual(req(
            "POST", "/v1/ip-calls/push/subscriptions", self.agent,
            self.subscription(),
        ), (403, {"error": "human_push_only"}))
        self.assertEqual(req(
            "DELETE", "/v1/ip-calls/push/subscriptions", self.other,
            {"device_id": "a" * 32},
        ), (200, {"enabled": False, "removed": False}))
        self.assertEqual(req(
            "DELETE", "/v1/ip-calls/push/subscriptions", self.human,
            {"device_id": "a" * 32},
        ), (200, {"enabled": False, "removed": True}))

    def test_invalid_public_key_rejected_on_startup(self):
        with self.assertRaisesRegex(ValueError, "invalid_vapid_public_key"):
            validate_public_key("not-real")
        with self.assertRaisesRegex(ValueError, "invalid_vapid_public_key"):
            CallRegistry(
                str(Path(self.folder.name) / "bad.sqlite"),
                identity_secret=self.secret,
                push_public_key="not-real",
            )


if __name__ == "__main__":
    unittest.main()
