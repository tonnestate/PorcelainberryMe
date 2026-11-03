"""Consent-gated Internet-call Web Push using the browser's own push service.

The system never posts call transcripts, caller names, phone numbers, auth
tokens or customer data to push services. All jobs originate in the same
SQLite transaction as the recipient-scoped RINGING/HANDOFF_OFFERED event.
The outbox and subscriptions persist through restarts. Browser grants and
delivery are NOT equivalent to native iOS CallKit, PSTN or guaranteed alerts.

Optional open source delivery dependency: pywebpush, not a paid API.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

MAX_PUSH_DEVICES = 5
MAX_PUSH_ENDPOINT = 2048
MAX_PENDING_JOBS = 10000
PUSH_RETRY_LIMIT = 4
PUSH_OFFER_SECONDS = 45
PUSH_JOB_RETENTION_SECONDS = 86400
HEX_DEVICE = re.compile(r"^[a-f0-9]{32}$")
BASE64URL = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
PUSH_HOST_SUFFIXES = (".push.apple.com", ".notify.windows.com",
                      ".push.services.mozilla.com")
PUSH_EXACT_HOSTS = frozenset({
    "fcm.googleapis.com", "updates.push.services.mozilla.com",
    "push.services.mozilla.com",
})


def _decode_url64(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def validate_public_key(public):
    if not isinstance(public, str) or not 80 <= len(public) <= 92:
        raise ValueError("invalid_vapid_public_key")
    try:
        raw = _decode_url64(public)
    except (ValueError, TypeError, base64.binascii.Error) as exc:
        raise ValueError("invalid_vapid_public_key") from exc
    if len(raw) != 65 or raw[0] != 4:
        raise ValueError("invalid_vapid_public_key")
    return public


def validate_subscription(subscription):
    from .ip_calls import CallError
    if not isinstance(subscription, dict):
        raise CallError("invalid_push_subscription")
    endpoint = subscription.get("endpoint")
    keys = subscription.get("keys")
    device = subscription.get("device_id")
    if not isinstance(device, str) or HEX_DEVICE.fullmatch(device) is None:
        raise CallError("invalid_push_device")
    if not isinstance(endpoint, str) or not 20 <= len(endpoint) <= MAX_PUSH_ENDPOINT:
        raise CallError("invalid_push_endpoint")
    parsed = urlsplit(endpoint)
    host = (parsed.hostname or "").lower()
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment or not parsed.path
            or host not in PUSH_EXACT_HOSTS
            and not any(host.endswith(suffix) for suffix in PUSH_HOST_SUFFIXES)):
        # Avoid SSRF: we only transmit to established browser push-service
        # hostnames. Never allow a caller supplied LAN/cloud metadata URL.
        raise CallError("unsupported_push_service")
    if not isinstance(keys, dict):
        raise CallError("invalid_push_keys")
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not isinstance(p256dh, str) or not isinstance(auth, str):
        raise CallError("invalid_push_keys")
    if not BASE64URL.fullmatch(p256dh) or not BASE64URL.fullmatch(auth):
        raise CallError("invalid_push_keys")
    try:
        pub = _decode_url64(p256dh)
        shared_secret = _decode_url64(auth)
    except (ValueError, TypeError, base64.binascii.Error) as exc:
        raise CallError("invalid_push_keys") from exc
    if len(pub) != 65 or pub[0] != 4 or len(shared_secret) != 16:
        raise CallError("invalid_push_keys")
    return device, endpoint, p256dh, auth


class ExpiredPushEndpoint(Exception):
    """Browser service explicitly returned 404/410 for a subscription."""


class PushMixin:
    def _init_push_schema(self, con):
        con.executescript("""
            CREATE TABLE IF NOT EXISTS ip_push_subscriptions (
              tenant TEXT NOT NULL,
              subject TEXT NOT NULL,
              device_id TEXT NOT NULL,
              endpoint TEXT NOT NULL UNIQUE,
              p256dh TEXT NOT NULL,
              auth TEXT NOT NULL,
              updated_at INTEGER NOT NULL,
              PRIMARY KEY(tenant,subject,device_id)
            );
            CREATE TABLE IF NOT EXISTS ip_push_jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_seq INTEGER NOT NULL,
              call_id TEXT NOT NULL,
              tenant TEXT NOT NULL, subject TEXT NOT NULL,
              device_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              created_at INTEGER NOT NULL,
              next_attempt INTEGER NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0,
              done INTEGER NOT NULL DEFAULT 0,
              UNIQUE(event_seq,tenant,subject,device_id)
            );
            CREATE INDEX IF NOT EXISTS ip_push_ready
              ON ip_push_jobs(done,next_attempt);
        """)

    def push_config(self):
        from .ip_calls import CallError
        if not self.push_public_key:
            raise CallError("webpush_not_configured", 503)
        return {"vapid_public_key": self.push_public_key}

    def register_push(self, claims, subscription):
        from .ip_calls import CallError
        if claims["role"] != "human":
            raise CallError("human_push_only", 403)
        self.push_config()
        device, endpoint, p256dh, auth = validate_subscription(subscription)
        with closing(self._open()) as db:
            with db:
                account = db.execute(
                    "SELECT 1 FROM ip_call_users WHERE tenant=? AND subject=?",
                    (claims["tenant"], claims["sub"]),
                ).fetchone()
                if account is None:
                    raise CallError("account_not_enrolled", 403)
                conflict = db.execute(
                    "SELECT tenant,subject,device_id FROM ip_push_subscriptions "
                    "WHERE endpoint=?", (endpoint,),
                ).fetchone()
                if conflict is not None and (
                    conflict["tenant"], conflict["subject"], conflict["device_id"],
                ) != (claims["tenant"], claims["sub"], device):
                    # A push subscription never silently transfers between
                    # accounts; old-device access must be revoked first.
                    raise CallError("push_endpoint_already_owned", 409)
                old = db.execute(
                    "SELECT endpoint FROM ip_push_subscriptions "
                    "WHERE tenant=? AND subject=? AND device_id=?",
                    (claims["tenant"], claims["sub"], device),
                ).fetchone()
                count = db.execute(
                    "SELECT COUNT(*) FROM ip_push_subscriptions "
                    "WHERE tenant=? AND subject=?",
                    (claims["tenant"], claims["sub"]),
                ).fetchone()[0]
                if old is None and count >= MAX_PUSH_DEVICES:
                    raise CallError("push_device_limit_reached", 429)
                if old and old["endpoint"] != endpoint:
                    db.execute(
                        "DELETE FROM ip_push_jobs WHERE tenant=? AND subject=? "
                        "AND device_id=?", (claims["tenant"], claims["sub"], device),
                    )
                db.execute(
                    "INSERT INTO ip_push_subscriptions "
                    "(tenant,subject,device_id,endpoint,p256dh,auth,updated_at) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(tenant,subject,device_id) "
                    "DO UPDATE SET endpoint=excluded.endpoint,"
                    "p256dh=excluded.p256dh,auth=excluded.auth,"
                    "updated_at=excluded.updated_at",
                    (claims["tenant"], claims["sub"], device, endpoint,
                     p256dh, auth, self._now()),
                )
        return {"enabled": True, "device_id": device}

    def revoke_push(self, claims, device_id):
        from .ip_calls import CallError
        if claims["role"] != "human":
            raise CallError("human_push_only", 403)
        if not isinstance(device_id, str) or HEX_DEVICE.fullmatch(device_id) is None:
            raise CallError("invalid_push_device")
        with closing(self._open()) as db:
            with db:
                db.execute(
                    "DELETE FROM ip_push_jobs WHERE tenant=? AND subject=? "
                    "AND device_id=?",
                    (claims["tenant"], claims["sub"], device_id),
                )
                removed = db.execute(
                    "DELETE FROM ip_push_subscriptions "
                    "WHERE tenant=? AND subject=? AND device_id=?",
                    (claims["tenant"], claims["sub"], device_id),
                )
                return {"enabled": False, "removed": removed.rowcount == 1}

    def _queue_event_push(self, db, call, receiver, kind, event_seq, now):
        if not self.push_public_key or kind not in ("RINGING", "HANDOFF_OFFERED"):
            return
        # A completed/expired/declined event never generates a notification.
        db.execute(
            "DELETE FROM ip_push_jobs WHERE done=1 AND created_at<?",
            (now - PUSH_JOB_RETENTION_SECONDS,),
        )
        count = db.execute(
            "SELECT COUNT(*) FROM ip_push_jobs WHERE done=0",
        ).fetchone()[0]
        if count >= MAX_PENDING_JOBS:
            return  # existing event inbox remains the fallback, no unbounded queue
        devices = db.execute(
            "SELECT device_id FROM ip_push_subscriptions "
            "WHERE tenant=? AND subject=?",
            (call["tenant"], receiver),
        ).fetchall()
        for row in devices[:MAX_PUSH_DEVICES]:
            db.execute(
                "INSERT OR IGNORE INTO ip_push_jobs "
                "(event_seq,call_id,tenant,subject,device_id,kind,"
                "created_at,next_attempt) VALUES(?,?,?,?,?,?,?,?)",
                (event_seq, call["id"], call["tenant"], receiver,
                 row["device_id"], kind, now, now),
            )

    def reconcile_push(self, send, *, limit=20):
        """One bounded batch; send() must return after the service accepts.

        A push-service 2xx is not proof that the user's phone notified.
        Network effects occur outside SQLite transactions and are retried
        safely only while the underlying invitation remains RINGING/OFFERED.
        """
        if not callable(send):
            raise ValueError("push_sender_required")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_push_batch_size")
        now = self._now()
        with closing(self._open()) as db:
            with db:
                db.execute(
                    "DELETE FROM ip_push_jobs WHERE done=1 AND created_at<?",
                    (now - PUSH_JOB_RETENTION_SECONDS,),
                )
                rows = db.execute(
                    "SELECT j.*,s.endpoint,s.p256dh,s.auth,"
                    "c.state AS call_state,c.expires_at AS call_exp,"
                    "h.state AS handoff_state,h.expires_at AS handoff_exp,"
                    "u.do_not_disturb "
                    "FROM ip_push_jobs j "
                    "LEFT JOIN ip_push_subscriptions s "
                    "ON j.tenant=s.tenant AND j.subject=s.subject "
                    "AND j.device_id=s.device_id "
                    "LEFT JOIN ip_calls c ON j.call_id=c.id "
                    "LEFT JOIN ip_call_handoffs h ON j.call_id=h.call_id "
                    "LEFT JOIN ip_call_users u ON j.tenant=u.tenant "
                    "AND j.subject=u.subject "
                    "WHERE j.done=0 AND j.next_attempt<=? "
                    "ORDER BY j.next_attempt,j.id LIMIT ?",
                    (now, limit),
                ).fetchall()
                jobs = []
                for row in rows:
                    expires = (row["call_exp"] if row["kind"] == "RINGING"
                               else row["handoff_exp"])
                    valid = (
                        row["endpoint"] is not None and expires is not None
                        and expires > now and not row["do_not_disturb"]
                        and ((row["kind"] == "RINGING"
                              and row["call_state"] == "RINGING")
                             or (row["kind"] == "HANDOFF_OFFERED"
                                 and row["call_state"] == "ACTIVE"
                                 and row["handoff_state"] == "OFFERED"))
                    )
                    if not valid:
                        db.execute(
                            "UPDATE ip_push_jobs SET done=1 WHERE id=?",
                            (row["id"],),
                        )
                        continue
                    db.execute(
                        "UPDATE ip_push_jobs SET next_attempt=? WHERE id=?",
                        (now + 30, row["id"]),
                    )
                    jobs.append((row, min(PUSH_OFFER_SECONDS, expires - now)))
        sent = stale = retry = 0
        for job, ttl in jobs:
            # Payload includes ONLY an opaque local call ID and event kind;
            # no customer text, credentials, tenant or caller identity.
            payload = {"type": "incoming_ip_call", "call_id": job["call_id"],
                       "kind": job["kind"]}
            failed = False
            gone = False
            try:
                send(
                    {"endpoint": job["endpoint"],
                     "keys": {"p256dh": job["p256dh"], "auth": job["auth"]}},
                    payload, ttl,
                )
            except ExpiredPushEndpoint:
                gone = True
            except Exception:
                failed = True
            stamp = self._now()
            with closing(self._conn()) as db:
                with db:
                    if gone:
                        db.execute(
                            "DELETE FROM ip_push_subscriptions "
                            "WHERE tenant=? AND subject=? AND device_id=? "
                            "AND endpoint=?",
                            (job["tenant"], job["subject"], job["device_id"],
                             job["endpoint"]),
                        )
                        db.execute(
                            "UPDATE ip_push_jobs SET done=1 WHERE tenant=? "
                            "AND subject=? AND device_id=?",
                            (job["tenant"], job["subject"], job["device_id"]),
                        )
                    elif failed and job["attempts"] + 1 < PUSH_RETRY_LIMIT:
                        db.execute(
                            "UPDATE ip_push_jobs SET attempts=attempts+1,"
                            "next_attempt=? WHERE id=?",
                            (stamp + min(15, 2 ** (job["attempts"] + 1)),
                             job["id"]),
                        )
                    else:
                        db.execute(
                            "UPDATE ip_push_jobs SET attempts=attempts+1,"
                            "done=1 WHERE id=?", (job["id"],),
                        )
            if gone:
                stale += 1
            elif failed:
                retry += 1
            else:
                sent += 1
        return {"attempted": len(jobs), "accepted_by_push_service": sent,
                "expired_subscriptions": stale, "failures": retry}


def make_web_push_sender(private_pem_path: str, contact: str):
    if not isinstance(private_pem_path, str) or not Path(private_pem_path).is_file():
        raise ValueError("vapid_private_key_file_missing")
    if (not isinstance(contact, str)
            or not (contact.startswith("mailto:") or contact.startswith("https://"))):
        raise ValueError("vapid_contact_required")
    try:
        from pywebpush import webpush, WebPushException
    except ImportError as exc:
        raise RuntimeError("install_optional_web_push_dependency") from exc

    def send(subscription, payload, ttl):
        # Fresh claims per request; pywebpush mutates the dict.
        try:
            webpush(
                subscription_info=subscription,
                data=json.dumps(payload, separators=(",", ":")),
                vapid_private_key=private_pem_path,
                vapid_claims={"sub": contact},
                ttl=max(1, int(ttl)),
                timeout=3.0,
            )
        except WebPushException as exc:
            if exc.status_code in (404, 410):
                raise ExpiredPushEndpoint() from exc
            raise
    return send


def main():
    parser = argparse.ArgumentParser(
        description="Generate local VAPID identity without a paid push provider"
    )
    sub = parser.add_subparsers(dest="action", required=True)
    generate = sub.add_parser("generate-vapid")
    generate.add_argument("--output", required=True, help="New private PEM path")
    args = parser.parse_args()
    if args.action == "generate-vapid":
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec
        except ImportError as exc:
            parser.error("install_optional_web_push_dependency")
        path = Path(args.output)
        if not path.is_absolute():
            parser.error("vapid_output_must_be_absolute")
        private = ec.generate_private_key(ec.SECP256R1())
        body = private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public = private.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(body)
        print("VAPID_PUBLIC_KEY=" + base64.urlsafe_b64encode(public).decode().rstrip("="))
        print("VAPID_PRIVATE_KEY_PATH=" + str(path))
        print("Never rotate keys without re-enrolling browser subscriptions.")


if __name__ == "__main__":
    main()
