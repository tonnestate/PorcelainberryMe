# PorcelainberryMe

Lightweight, Python-first communications infrastructure for FruitStack. The
library owns transport contracts, signed events and delivery receipts; **not**
Aurora/AVCOS cognition, AnanasMe UI, TonnEstate CRM records or new AI models.

**Status:** feature-branch prototype. Offline tests only; not deployed on VPS.

### Implemented

- Typed email, WhatsApp, SMS and phone events.
- Signed loopback HTTP intake, per-tenant/channel ID deduplication and durable
  SQLite inbound queue.
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

Not yet built: SMS provider transport, SIP/PSTN calls, real-time media,
mailbox polling, AnanasMe screens and production/tenant-specific integration.

### Development

Python 3.11+; no mandatory third-party runtime dependencies.

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
must first authenticate their upstream provider. A trusted FruitStack worker
can read `Ledger.pending()` and acknowledge verified outcomes; it does not
run automatically.

### Architectural constraints

- No provider credentials, private caller data or voice samples in Git.
- No new local voice-model weights and no duplicate FruitStack agent brain.
- No additional WhatsApp login, no parallel TonnEstate message consumer.
- External sending always requires an injected authorization policy.
- Provider-accepted does NOT mean human-delivered.
- No code merge or VPS installation implied by passing offline tests.

Technical details: [architecture](docs/ARCHITECTURE.md),
[existing integrations](docs/INTEGRATIONS.md),
[voice recovery](docs/VOICE_RECOVERY.md) and
[donor audit](docs/DONOR_AUDIT.md).
