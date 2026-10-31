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
