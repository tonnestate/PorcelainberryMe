# PorcelainberryMe

Lightweight, Python-first communications infrastructure for FruitStack. The
library owns transport contracts, signed events and delivery receipts; **not**
Aurora/AVCOS cognition, AnanasMe UI, TonnEstate CRM records or new AI models.

**Status:** feature-branch prototype. Offline tests only; not deployed on VPS.

### Implemented

- Typed email, WhatsApp, SMS and phone events.
- Signed loopback HTTP intake, per-tenant/channel ID deduplication and durable,
  **capacity-limited** SQLite inbound queue (fails closed at 1,000 retained events by default).
- Atomically claimed, tenant-scoped inbound processing with a mandatory existing
  handler/policy, receipt checks and crash-safe UNKNOWN reconciliation.
- A signed local intake client for existing trusted n8n/WhatsApp integrations.
- Read-only server capacity and installed-Piper preflight (no model load).
- Explicitly authorized outbound gateway and provider acceptance/rejection/
  ambiguous failure ledger; ambiguous sends are not silently retried.
- Optional SMTP sender and basic MIME email normalization.
- Official WhatsApp Business Cloud text adapter (opt-in).
- **Existing TonnEstate/Aurora WhatsApp adapter:** delegates through the
  owner-scoped `127.0.0.1:5001/api/internal/v1/aurora/send` API; no new WhatsApp process.
- **Existing Piper voice adapter:** uses explicit local executable and ONNX
  file + config already present on disk; no model download or bundled voice.
  A caller must inject it into `PythonVoiceBridge` after verifying the
  intended voice and its licensing.

- **Optional SIP phone line:** real `baresip-python` 0.6.0 registration,
  authorization-gated incoming/outgoing SIP calls and direct transient PCM audio.
  No default SIP account, voice model or phone service is installed.

Not yet validated: real PSTN calling, live spoken AI conversations, SMS provider
transport, automatic mail polling, AnanasMe screens and VPS integration.

### Development

Python 3.11+; no mandatory third-party runtime dependencies. The optional SIP
extra is pinned and installed only on explicit request using `.[sip]`.

    python -m pip install -e .
    python -m unittest discover -s tests -v

The standalone server is **optional** and inbound-only. It listens exclusively
on loopback, refuses unsigned events and never invokes an agent itself.

    PORCELAINBERRY_SIGNING_SECRET=<private secret of at least 32 chars>
    PORCELAINBERRY_DB=/private/absolute/path/events.sqlite3
    porcelainberryme

POST a canonical InboundEvent JSON to 127.0.0.1:8873/v1/inbound. Sign the
literal body with `sha256=HMAC_SHA256(secret, ascii(unix_timestamp) + "." +
raw_body)` and supply `X-PB-Timestamp` and `X-PB-Signature`.

Never expose this local endpoint publicly. Existing authoritative connectors
must first authenticate their upstream provider. A trusted FruitStack worker can claim work via
`Ledger.claim_pending(tenant_id=...)` and acknowledge an authoritative
receipt through `Ledger.acknowledge_claim(...)`; it does not run automatically.

### Architectural constraints

- No provider credentials, private caller data or voice samples in Git.
- No new local voice-model weights and no duplicate FruitStack agent brain.
- No additional WhatsApp login, no parallel TonnEstate message consumer.
- External sending always requires an injected authorization policy.
- Provider-accepted does NOT mean human-delivered.
- No code merge or VPS installation implied by passing offline tests.

Technical details: [operator handoff](docs/OPERATIONS.md),
[architecture](docs/ARCHITECTURE.md),
[existing integrations](docs/INTEGRATIONS.md),
[voice recovery](docs/VOICE_RECOVERY.md),
[optional SIP telephony](docs/SIP_TELEPHONY.md) and
[donor audit](docs/DONOR_AUDIT.md).
