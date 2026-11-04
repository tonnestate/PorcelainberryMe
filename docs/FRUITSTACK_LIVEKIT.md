# Native FruitStack owner voice over the existing LiveKit call

Status: **merged source integration and offline contract tests**. No production
FruitStack host, customer SaaS tenant, real Whisper/Piper models or authenticated
owner phone call has been exercised or deployed by these commits.

## Dependency now merged in AnanasMe

AnanasMe PR #33 was fixed and merged to main on 2026-11-04:
https://github.com/tonnestate/AnanasMe/pull/33

Its native `POST /api/threads/{thread_id}/messages` 202 admission and
`GET /api/messages/{message_id}/result` terminal-state contract remain the
authority. This does **not** deploy or start AnanasMe on the existing host.

## Who can use this path

**Only an independently verified personal FruitStack owner.**

The existing signed IP call registry establishes an enrolled Internet
participant and the current call ID. Enrollment alone is NOT sufficient
authority to use Aurora, CherryMe, PlumMe, AVCoS or the AnanasMe owner
harness. A trusted first-party host must supply an independent owner
authorizer bound to its authenticated session/device and explicit owner
entitlement; do not compare a caller-supplied phone number or voice claim.

The trust-scoped host injects an instance of
`FruitStackOwnerVoiceResponder`, constructed with:
- The verified `tenant`, `assistant` and `caller` identities;
- An `AnanasMeOwnerHarness` carrying the **existing** loopback-only native
  API token, an owner verifier, and authoritative existing thread resolver;
- A **separate persistent Ledger SQLite file for this one signed call**.
  Neither the global customer CRM event ledger nor a fresh temporary
  file on each restart meets the durable, call-exclusive contract;
- An independent `authorize_owner(event)` callback from that trusted host.
  It must fail closed if the session or permission has been revoked.

A host can call `serve_inbound(..., owner_voice_factory=trusted_factory)`
from its trusted Python process. The factory receives the verified
`(tenant, assistant, caller, call_id)` (positional arguments) and returns
the call-specific `FruitStackOwnerVoiceResponder` only for authorized
owner callers, or `None` for ordinary customer calls. This hook is **not**
an HTTP field, voice command, open CLI switch or global customer opt-in.

The returned backend is validated against the actual tenant, assistant and
caller, passed into the existing `make_call_responder()` stack, and bound to
the signed control-plane call ID using `set_call_id()`. Calendar, consented
notes and human handoff wrappers preserve that binding; final results
are acknowledged only by the voice engine's audible-delivery callback.

## Request lifecycle

1. Whisper produces an utterance on a verified, accepted WebRTC microphone.
2. The owner-authorizer is rechecked. The signed call ID is used for the
   monotonic source event key `livekit:<call-id>:<turn-number>`.
3. The **existing** `Ledger` ingests a hashed, bounded, call-scoped
   `InboundEvent(channel="phone")`; `InboundProcessor` claims it atomically
   and the already-existing `AnanasMeOwnerHarness` submits to the original
   owner thread. No second agent or FruitStack mission planner is created.
4. The authoritative AnanasMe assistant `message_id` is durably
   associated with the source event as an `OWNER_HARNESS` admission receipt.
   `QUEUED`, `WORKING` and `HANDED_OFF` are not completed replies.
5. Only `DONE` from the precise message and verified owner thread is
   eligible to become speech. Other states and uncertain network errors
   produce explicit, non-success status speech; no blind resubmission.
6. A later user turn during an unfinished or interrupted mission polls
   ONLY the existing receipt. Another owner mission is fenced until its
   final result has actually been spoken by the existing Piper/LiveKit stack.
7. The ordinary customer voice backend remains unchanged and cannot select
   this owner pathway via its conversation, payload or phone number.

The voice responder never writes arbitrary CRM records, authorizes outside
contacts, impersonates owner sessions or sends an external WhatsApp/SMS.
External CRM/work execution requires separate tenant-scoped host connectors.

## Operational gates still open

- Authoritative first-party host integration of signed owner authentication
  and persistent per-call ledgers; exact installed runtime paths/permissions.
- Supervisor-managed AnanasMe process on existing FruitStack, including
  loopback, token management and lifecycle health.
- Actual verified owner-to-FruitStack-to-voice conversation on the deployed
  hardware, real German audio latency, barge-in, multi-call policy and
  recovery after an ambiguous native request.
- Specific customer CRM record mapping, consent, effect receipts and audit
  for TonnEstate and GreenGrapeMe; **not implemented by owner voice**.
- Standard PSTN telephone numbers are still not available without a
  separate carrier/trunk. Internet WebRTC calls remain the supported path.

Testing uses the native `Ledger` and `InboundProcessor` with mock
AnanasMe HTTP responses plus existing voice-worker regressions. It is
not proof of productive customer contacts or installed VPS services.
