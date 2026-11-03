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
- **AnanasMe native harness integration:** verified owner-originated messages
  from all four channels are submitted through the existing persistent
  AnanasMe request_id and Aurora/CherryMe routing; no second cognition.
- **Native reply gate:** WhatsApp/email/SMS returns are permitted only after
  the exact AnanasMe assistant message reports DONE, then pass the existing
  authorized Gateway/idempotency ledger; phone speech uses SIP separately.
  A caller must inject it into `PythonVoiceBridge` after verifying the
  intended voice and its licensing.

- **Python/PyPI voice streaming to SIP:** explicit installed-engine PCM16LE
  source or existing Python WAV bridge, paced small frames, cancellable
  playback and single-inference protection for legacy synchronous voices.
- **Native SIP harness speech:** an authorized, already-transcribed phone turn
  is tied to the original SIP session and the exact durable AnanasMe assistant
  receipt. Only confirmed native DONE answers can be played; ambiguous or
  interrupted attempts are not repeated automatically.
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
[prior PyPI voice investigation](docs/PYPI_VOICE_RECOVERY.md),
[native AnanasMe harness link](docs/HARNESS_INTEGRATION.md),
[optional SIP telephony](docs/SIP_TELEPHONY.md),
[voice-to-harness integration](docs/VOICE_TO_HARNESS.md) and
[donor audit](docs/DONOR_AUDIT.md).


### Headless Internet Call Control — new feature branch slice

The scope is now an **independently usable headless IP communications platform**, including **agent-initiated calls to registered Internet identities**, not merely a SIP/WhatsApp relay for FruitStack. The initial call-control implementation handles a verified agent's outbound call invitation, recipient-specific ring notifications, explicit answer/decline/hangup, tenant and identity isolation, stable idempotency, timeouts, and issuance of short-lived **self-hosted LiveKit** microphone-only room credentials after acceptance. It uses the standard library and existing SQLite; no paid API is required to run the control plane.

**Not yet a working voice phone or commercial SaaS:** LiveKit media has not been deployed, no independent user app or local AI speech agent is running, and no real WebRTC audio call has passed. Native phone numbers and ordinary cellular calls require a separate PSTN interconnect and cannot be reached over Internet alone at zero provider cost. See [HEADLESS_IP_CALLS.md](docs/HEADLESS_IP_CALLS.md) for the source-only invocation, architecture and remaining hard release gates. The earlier experimental baresip SIP module is not the default media path.


### Free local AI voice worker (in development)

This feature branch now includes a concrete autonomous **Internet** voice-agent code path:
authenticated agent role -> originate a consented IP call -> wait for acceptance
-> join a self-hosted LiveKit microphone room -> receive real 20-ms PCM frames
-> segment human turns -> transcribe with an **already-installed**
faster-whisper model -> respond using **local Ollama** -> synthesize via an
**already-installed Piper voice** -> publish PCM back to the same WebRTC room.
Incoming speech interrupts the outgoing audio queue. No paid APIs, carrier
plans or new cloud subscriptions are dependencies.

**Status:** all tests so far are offline/mocked; no real deployment, installed
models, LiveKit audio acceptance, iPhone background ringing or acceptable
natural speech latency are established. The first energy-based detector and
whole-utterance STT/TTS are not production-ready conversational turn-taking.
The free Python optional extras are deliberately not installed by default.
See [local agent execution and acceptance](docs/LOCAL_VOICE_AGENT.md) and
[headless call architecture](docs/HEADLESS_IP_CALLS.md). Public mobile/landline
numbers remain impossible without a separately funded PSTN interconnect.
