"""Route explicitly verified owner messages to the EXISTING AnanasMe harness.

AnanasMe keeps all Aurora, CherryMe and PlumMe cognition and its durable
request_id admission. This integration adds no agent loop, chat database or
automatic external response. Non-owner CRM contacts must use their approved
CRM handler instead of the privileged owner harness.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .contracts import InboundEvent

_THREAD = re.compile(r"^[a-f0-9]{32}$")
_MESSAGE = re.compile(r"^[a-f0-9]{32}$")


@dataclass(frozen=True, slots=True)
class HarnessAdmission:
    message_id: str
    state: str
    replayed: bool


class AnanasMeOwnerHarness:
    """Submission to the real owner chat; never to arbitrary customer threads.

    A trusted caller injects BOTH a verified owner check and an authoritative
    thread resolver. The external sender, text and conversation_ref grant no
    authority by themselves. No new thread is created by this adapter.
    """

    def __init__(
        self, *, token: str, port: int = 8765,
        verify_owner: Callable[[InboundEvent], bool],
        resolve_thread: Callable[[InboundEvent], str],
        timeout_seconds: int = 10,
    ):
        if not isinstance(token, str) or len(token) < 24 or "\r" in token or "\n" in token:
            raise ValueError("ananasme_owner_auth_token_required")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("invalid_harness_port")
        if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 30:
            raise ValueError("invalid_harness_timeout")
        if not callable(verify_owner) or not callable(resolve_thread):
            raise ValueError("owner_verifier_and_thread_resolver_required")
        self._token = token
        self._port = port
        self._verify_owner = verify_owner
        self._resolve_thread = resolve_thread
        self._timeout = timeout_seconds

    @staticmethod
    def request_id(event: InboundEvent) -> str:
        """Stable idempotency key across service crashes and ingress retries.

        Only source identity determines the key, never a transient claim
        token. AnanasMe itself checks content fingerprint on key reuse.
        """
        key = json.dumps(
            ["porcelainberryme", event.tenant_id, event.channel, event.external_id],
            separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(key).hexdigest()[:32]

    def submit_owner_message(self, event: InboundEvent) -> HarnessAdmission:
        if not isinstance(event, InboundEvent) or event.kind != "text":
            raise ValueError("only_text_events_admitted_to_harness")
        if not 1 <= len(event.text.strip()) <= 4000:
            raise ValueError("harness_text_limit")
        # This must be an upstream-authenticated owner identity / device.
        if self._verify_owner(event) is not True:
            raise PermissionError("not_verified_owner_channel")
        thread_id = self._resolve_thread(event)
        if not isinstance(thread_id, str) or not _THREAD.fullmatch(thread_id):
            raise PermissionError("no_authorized_owner_thread")
        request = {
            "text": event.text,
            "mode": "chat",
            "request_id": self.request_id(event),
        }
        payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        auth = base64.b64encode(("porcelainberryme:" + self._token).encode("utf-8")).decode("ascii")
        req = Request(
            f"http://127.0.0.1:{self._port}/api/threads/{thread_id}/messages",
            data=payload, method="POST",
            headers={"Authorization": "Basic " + auth,
                     "Content-Type": "application/json"},
        )
        try:
            with urlopen(req, timeout=self._timeout) as response:
                if response.status != 202:
                    raise RuntimeError("harness_admission_unconfirmed")
                result = json.loads(response.read(8192))
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise PermissionError("harness_auth_rejected") from exc
            if exc.code == 404:
                raise ValueError("approved_harness_thread_missing") from exc
            # Invalid/reused IDs, unavailable Aurora etc remain UNKNOWN in
            # InboundProcessor and must not be retried blindly.
            raise RuntimeError("harness_admission_unconfirmed") from exc
        if not isinstance(result, dict) or not _MESSAGE.fullmatch(str(result.get("message_id", ""))):
            raise RuntimeError("harness_receipt_invalid")
        state = result.get("state")
        if state not in ("QUEUED", "WORKING", "DONE", "HANDED_OFF",
                         "REVIEW_UNVERIFIED", "UNKNOWN", "FAILED"):
            raise RuntimeError("harness_state_unrecognized")
        return HarnessAdmission(
            message_id=result["message_id"],
            state=state,
            replayed=result.get("replayed") is True,
        )

    def observe_native_reply(self, event: InboundEvent, message_id: str) -> dict:
        """Read exactly one AnanasMe answer after native Aurora reconciliation.

        Neither a pending state nor an absent/failed result is a deliverable
        reply. The result's thread must match the approved owner thread.
        """
        if not isinstance(event, InboundEvent) or event.kind != "text":
            raise ValueError("only_text_events_admitted_to_harness")
        if self._verify_owner(event) is not True:
            raise PermissionError("not_verified_owner_channel")
        thread_id = self._resolve_thread(event)
        if not isinstance(thread_id, str) or not _THREAD.fullmatch(thread_id):
            raise PermissionError("no_authorized_owner_thread")
        if not isinstance(message_id, str) or not _MESSAGE.fullmatch(message_id):
            raise ValueError("invalid_harness_message_id")
        auth = base64.b64encode(("porcelainberryme:" + self._token).encode("utf-8")).decode("ascii")
        request = Request(
            f"http://127.0.0.1:{self._port}/api/messages/{message_id}/result",
            method="GET", headers={"Authorization": "Basic " + auth},
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                if response.status != 200:
                    raise RuntimeError("harness_result_unconfirmed")
                result = json.loads(response.read(32768))
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise PermissionError("harness_auth_rejected") from exc
            raise RuntimeError("harness_result_unconfirmed") from exc
        if (not isinstance(result, dict)
                or result.get("schema") != "ananasme.assistant-result.v1"
                or result.get("thread_id") != thread_id
                or result.get("message_id") != message_id):
            raise RuntimeError("harness_result_identity_mismatch")
        state = result.get("state")
        if state not in ("QUEUED", "WORKING", "DONE", "HANDED_OFF",
                         "REVIEW_UNVERIFIED", "UNKNOWN", "FAILED"):
            raise RuntimeError("harness_state_unrecognized")
        text = result.get("reply")
        if state == "DONE" and result.get("deliverable") is True:
            if not isinstance(text, str) or not text.strip() or len(text) > 16000:
                raise RuntimeError("harness_final_reply_invalid")
            return {"state": "DONE", "reply": text}
        return {"state": state, "reply": None}

    def receipt_handler(self, event: InboundEvent, claim_token: str) -> str:
        """Plug into InboundProcessor.handler after its own owner authorization.

        A valid message_id acknowledges durable *admission*, not the final
        Aurora reply nor external WhatsApp/email/SMS delivery.
        """
        if not isinstance(claim_token, str) or not claim_token:
            raise ValueError("claim_token_required")
        return self.submit_owner_message(event).message_id
