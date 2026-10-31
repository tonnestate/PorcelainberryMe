# PorcelainberryMe ↔ AnanasMe native harness

AnanasMe is the *existing* Aurora/CherryMe/PlumMe harness. PorcelainberryMe owns communication transport and source-side idempotency, never an additional agent brain.

## Source-verified API contracts

- POST /api/threads/{thread_id}/messages accepts a text, mode=chat and 32-character request_id. It returns HTTP 202 and the durable assistant message_id.
- GET /api/messages/{message_id}/result (AnanasMe PR #33) exposes ONLY the exact native assistant state, answer if DONE, thread_id and whether it is deliverable. It reconciles native Aurora mission receipts first.
- GET /api/communications/status (AnanasMe PR #33) is optional, read-only, and distinguishes reachable ingress from verified channel readiness.
- All endpoints use the existing AnanasMe host authentication. This integration connects via literal loopback, never a public URL.

## Direct communication routing

1. A pre-existing provider bridge authenticates WhatsApp/email/SMS/phone and identifies the owner outside the payload.
2. An explicit owner verifier and an authoritative existing AnanasMe thread resolver are injected into AnanasMeOwnerHarness. No arbitrary thread creation.
3. Stable request_id is derived from source tenant, channel and external message ID, not the transient worker token.
4. InboundProcessor claims one event atomically and stores the assistant message_id as an *admission* receipt, not proof of completion.
   The owner-harness handler is explicitly marked OWNER_HARNESS in the ledger; a generic CRM receipt is never sufficient.
5. NativeHarnessReplyDispatcher checks GET /api/messages/{message_id}/result. It sends nothing until an actual DONE reply is observed.
6. An independently authorized return policy and Gateway delivery ledger control WhatsApp/email/SMS output; duplicates and ambiguous outcomes are not automatically resent.
7. Phone audio remains with the SIP PCM path. A text reply must not be sent back as an SMS-style phone message; streaming TTS needs separate quality and cancellation verification.

## Absolute boundaries

- Customer/lead messages go to the existing TonnEstate permissioned CRM service, NEVER the privileged AnanasMe owner chat.
- Caller-ID, From headers and the event's sender/tenant fields do not independently prove owner identity.
- A harness QUEUED, WORKING, HANDED_OFF, UNKNOWN, FAILED or REVIEW_UNVERIFIED result is **not** an external reply.
- A Gateway ACCEPTED receipt proves provider acceptance, not delivery to the human.
- No second WhatsApp consumer is enabled and nothing is deployed to the VPS by this branch.
- PorcelainberryMe reply dispatch depends on the additional result endpoint from AnanasMe PR #33; both branches must pass review and be deployed together in a supervised, authorized staging integration.

Source: https://github.com/tonnestate/AnanasMe/pull/33
Source: https://github.com/tonnestate/PorcelainberryMe/pull/1

## Supervised restart-safe pipeline (implemented)

`HarnessChannelPipeline` composes the existing `Ledger`, `InboundProcessor`,
`AnanasMeOwnerHarness`, `NativeHarnessReplyDispatcher` and `Gateway`.
It creates **no** new agent loop, HTTP server, systemd service or
background poller. A trusted owner-scoped process invokes:

- `pipeline.ledger.ingest(event)` only for events verified by the existing
  provider connector, with authoritative tenant identity.
- `pipeline.admit_next(channel=...)` to claim exactly one event and durably
  record its owner-harness `message_id` admission, never model completion.
- `pipeline.deliver_ready(limit=50)` to inspect **only** explicitly
  OWNER_HARNESS-tagged, durably admitted messages from the selected tenant.
  It retrieves the exact AnanasMe result, returns pending for unfinished
  native execution, and authorizes sender/recipient through the existing
  Gateway before any real delivery attempt.

The pipeline requires the **same** SQLite ledger for admission and return
delivery, so restart recovery retains the exact source event and native
message ID. Any different text, recipient, sender or conversation reference
for the same original event ID fails the stored-payload fingerprint check.
A forged/unrelated AnanasMe `message_id` within the same thread cannot
be sent to the caller. Other CRM handlers with generic receipts cannot
enter this owner-specific return path.

Delivery stats distinguish `provider_accepted` from human delivery.
Recorded ATTEMPTING/FAILED/UNKNOWN/ACCEPTED outbound states are never
automatically replayed. A status GET to AnanasMe may be repeated for
pending execution because it never restarts a mission.

No existing TonnEstate or n8n consumer was switched. The operator must
disable/redirect the old ingestion path before running this pipeline
against the same live platform. Only an explicitly accepted staging
handover may attach a real provider listener and sender.

Tests: cross-process durable claim, exact event+message ID binding,
spoofed sender/cross-thread rejection, restart recovery, repeated polling
and exactly one mocked outbound provider acceptance.
