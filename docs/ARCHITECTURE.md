# Architectural contract — v0.1

## Ownership boundaries

- PorcelainberryMe: channel normalization, signed ingress, transport adapters, delivery state and correlation references.
- Aurora: presentation and agent identity, not a second transport or model stack.
- AnanasMe: conversation and mission UI, not a carrier/telephony server.
- FruitStack / AVCOS: existing tool execution, cognitive decisions, budgets, safety and owner authorization.
- TonnEstate: canonical CRM contacts, communication events, consent, leads and mail desk.
- Existing WhatsApp bridge and n8n: keep in place pending actual runtime comparison. No parallel message consumer.

## Observed donor / integration surfaces

- Hermes: gateway/platforms/event.py, gateway/platforms/base.py, gateway/delivery_ledger.py, plugins/platforms/email/adapter.py, plugins/platforms/sms/adapter.py, plugins/platforms/whatsapp_cloud/adapter.py. The Hermes gateway couples adapter events to its own agent loop. We do not install that loop.
- Novu: cross-channel identity/delivery patterns; full self-hosted stack requires separate API, Redis, MongoDB, worker and websocket services. Not installed.
- TonnEstate: app/services/aurora_whatsapp_orchestrator.py, aurora_whatsapp_send_service.py, email_lead_intake_service.py, mail_desk_service.py, communication_policy_service.py, contact_comm_event_service.py and contact_consent_service.py.
- Aurora: integration/aurora_voice.py uses host-local faster-whisper and Qwen speech with complete WAV generation and word alignment. Not certified for real-time calls and not reused as a speech default.

No donor source code was copied into v0.1. All transport core code was authored for this repository.

## v0.1 message lifecycle

1. Existing trusted bridge verifies the platform-specific webhook.
2. It posts a canonical InboundEvent to loopback, signed and time-bounded.
3. The event is durably stored and deduplicated by (tenant, channel, external_id); storage is bounded and returns HTTP 507 instead of filling the host. Same ID with changed content conflicts.
4. An explicitly scoped and authorized existing integration uses an atomic claim (PENDING -> PROCESSING) across SQLite processes; a second consumer cannot claim it.
5. The existing authoritative FruitStack/CRM handler returns a persisted receipt to acknowledge HANDLED. On an ambiguous side effect or crash, UNKNOWN requires reconciliation and is never automatically replayed.
6. Outbound sends require a separate explicit authorizer callback. The delivery ledger records PENDING -> ATTEMPTING -> ACCEPTED / FAILED / UNKNOWN. UNKNOWN is never automatically retried.

Provider acceptance is not end-user delivery. Inbound and outbound exact-once externally are NOT claimed.

## Security constraints

- Loopback ingress; HMAC SHA-256 includes timestamp and raw body; five-minute replay window plus durable message ID dedup.
- No network-exposed send endpoint and no untrusted model calls.
- Secrets injected at deployment only; no environment dumps, no raw request logs.
- Tenant and permission scope must be enforced by existing authoritative callers; tenant_id in a signed envelope is not proof of business authorization.
- External events are untrusted input, never instruction authority.
- Call recording, WhatsApp business identity, SMS marketing and customer messages require channel-specific compliance and explicit policy.
- v0.1 stores inbound text in a local SQLite queue; deployment requires access restrictions and retention policy. It is NOT a replacement for TonnEstate master records.

## Deliberate non-goals / gaps

- No SIP/PSTN bridge or live audio stream.
- No bundled SMS carrier implementation or IMAP polling loop.
- No automated retry of ambiguous external sends.
- No blind installation of model weights or TTS engine.
- No provider-specific webhook publicly exposed.
- No VPS install, live call or production readiness claim.
- Read-only capacity and voice file preflight is available with `python -m porcelainberryme.preflight`.
- No live inbound connector was switched; TonnEstate remains the sole current WhatsApp consumer.

## Acceptance

Offline: typed contracts, tenant-scoped idempotency, signed ingress, safe outbound transitions, no autorun of tools, voice unbound. Integration: inspect VPS capacity; bind exactly one existing WhatsApp source and one existing CRM/email path, then execute a real end-to-end test in supervised staging. Later validate SIP telephony and the previously successful Python voice with recorded latency and an actual listening test.
