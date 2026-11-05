"""Optional passive owner WhatsApp mirror into PorcelainberryMe's signed inbox.

Called from TonnEstate's existing authenticated n8n/Aurora reply route.
Never logs in to WhatsApp, triggers a second consumer, or sends replies.
The trusted caller supplies owner tenant and owner ID from the database,
not from the untrusted WhatsApp payload.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import time
from urllib.request import Request, urlopen

from .porcelainberry_whatsapp_outbox import OwnerWhatsAppMirrorOutbox


_MIRROR_URL = "http://127.0.0.1:8873/v1/owner-whatsapp-mirror"


def _read_signing_secret():
    value = os.environ.get("TE_PB_WHATSAPP_SIGNING_SECRET_FILE", "")
    if not value or not Path(value).is_absolute():
        raise PermissionError("pb_whatsapp_mirror_secret_file_required")
    fd = os.open(value, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_uid != os.geteuid()):
            raise PermissionError("pb_whatsapp_mirror_secret_not_private")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            secret = stream.read(4097).strip()
    finally:
        if fd >= 0:
            os.close(fd)
    if not 32 <= len(secret) <= 4096 or any(ch.isspace() for ch in secret):
        raise PermissionError("pb_whatsapp_mirror_secret_invalid")
    return secret


def mirror_owner_inbound(payload, *, tenant_id, owner_id):
    """Durably queue authenticated owner messages before network I/O.

    True means persisted locally (not remote acknowledged). The scheduled
    outbox drain reconciles failures using PB's original-ID deduplication.
    """

    if os.environ.get("TE_PB_WHATSAPP_MIRROR_ENABLED", "0") != "1":
        return False
    # The ingress and the independently supervised replay worker MUST
    # address exactly the same, database-authorized owner. Otherwise an
    # incoming message can be stranded in a different SQLite owner queue
    # while the configured worker reports an empty, healthy backlog.
    expected_tenant = os.environ.get("TE_PB_WHATSAPP_OWNER_TENANT", "")
    expected_owner = os.environ.get("TE_PB_WHATSAPP_OWNER_ID", "")
    if (not expected_tenant or not expected_owner
            or tenant_id != expected_tenant or owner_id != expected_owner):
        raise PermissionError("pb_whatsapp_mirror_worker_scope_mismatch")
    if not isinstance(payload, dict):
        raise ValueError("pb_mirror_incoming_event_invalid")
    # Match AuroraWhatsAppOrchestrator._handle exactly: only boolean
    # True represents a message sent by the owner. The existing Aurora
    # reply path treats absent, null and other values as inbound. Mirror
    # the same authenticated input to avoid dropping valid conversations;
    # provider-side normalization must be fixed separately if needed.
    if payload.get("from_me") is True:
        return False
    event_id = payload.get("message_id")
    sender = payload.get("sender")
    message = payload.get("message")
    conversation = payload.get("conversation_id", "")
    if (not isinstance(event_id, str) or not 1 <= len(event_id) <= 256
            or not event_id.strip() or "\r" in event_id or "\n" in event_id
            or not isinstance(sender, str) or not sender.endswith("@c.us")
            or len(sender) > 256 or "\r" in sender or "\n" in sender
            or not isinstance(message, str) or not message.strip()
            or len(message) > 16000
            or not isinstance(conversation, str) or len(conversation) > 512
            or not isinstance(tenant_id, str) or not tenant_id.strip()
            or len(tenant_id) > 256
            or not isinstance(owner_id, str) or not owner_id.strip()
            or len(owner_id) > 256):
        # An explicitly incoming event must not be silently acknowledged
        # when the stable provider ID or authenticated identity is missing.
        raise ValueError("pb_mirror_incoming_event_invalid")
    event = {
        "tenant_id": tenant_id,
        "channel": "whatsapp",
        "external_id": event_id,
        "sender": sender,
        "recipient": owner_id,
        "text": message,
        "conversation_ref": conversation,
        "kind": "text",
    }
    outbox = OwnerWhatsAppMirrorOutbox(
        os.environ.get("TE_PB_WHATSAPP_OUTBOX_DB", ""),
        tenant=tenant_id, owner=owner_id,
    )
    # The authenticated request only persists the event. Delivery is owned
    # exclusively by the independently supervised outbox worker; otherwise
    # a provider outage adds blocking network latency to Aurora replies.
    outbox.enqueue(event)
    return True


def _signed_push(event: dict) -> bool:
    """One bounded, signed attempt to the existing local PB mirror.

    This callback is also used by the supervised outbox replay worker.
    """
    encoded = json.dumps(
        event, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    timestamp = str(int(time.time()))
    secret = _read_signing_secret()
    signature = "sha256=" + hmac.new(
        secret.encode("utf-8"), timestamp.encode("ascii") + b"." + encoded,
        hashlib.sha256,
    ).hexdigest()
    request = Request(
        _MIRROR_URL, data=encoded, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-PB-Timestamp": timestamp,
            "X-PB-Signature": signature,
        },
    )
    try:
        with urlopen(request, timeout=2) as response:
            if response.status not in (200, 202):
                raise RuntimeError("pb_whatsapp_mirror_unconfirmed")
            report = json.loads(response.read(4096))
            if (not isinstance(report, dict)
                    or report.get("accepted") is not True
                    or report.get("state") != "MIRRORED"):
                raise RuntimeError("pb_whatsapp_mirror_unconfirmed")
            return True
    except Exception:
        # Never include provider exception text, message, sender or URL.
        raise RuntimeError("pb_whatsapp_mirror_unavailable") from None
