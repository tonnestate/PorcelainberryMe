# PorcelainberryMe

Independent, Python-first **headless AI communications platform** built
for stand-alone SaaS operation. FruitStack, TonnEstate, GreenGrapeMe, AnanasMe
and Aurora are optional integrations, never product runtime prerequisites.
The software owns its communication interfaces and AI-call orchestration;
it does not automatically provide an E.164 telephone carrier or a deployed
speech/media infrastructure.

**Status:** source implementation. Automated CI has verified synthetic WebRTC audio and the real agent/VoiceTurnEngine pipeline against a self-hosted LiveKit server using fake local STT/LLM/TTS. Production-hardened loopback ingress, a separate database readiness check, bounded SQL reporting and a read-only deployment release gate are now included. No user-VPS deployment, actual German model conversation, iPhone background ringing or PSTN interconnection is verified. See [production release gates](docs/PRODUCTION_READINESS.md) and [real media CI evidence](docs/REAL_MEDIA_EVIDENCE.md).

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
- **Opt-in owner WhatsApp MCP:** three host-scoped tools read already
  authenticated local WhatsApp events and submit only *exactly preapproved*
  messages via the existing TonnEstate/Aurora personal account. No second
  WhatsApp login, automatic resend or arbitrary model-selected recipient.
  This source integration is not yet a live VPS/ChatGPT account connection.
  See [owner WhatsApp MCP](docs/WHATSAPP_MCP.md).
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


### Headless Internet Call Control

The scope is now an **independently usable headless IP communications platform**, including **agent-initiated calls to registered Internet identities**, not merely a SIP/WhatsApp relay for FruitStack. The initial call-control implementation handles a verified agent's outbound call invitation, recipient-specific ring notifications, explicit answer/decline/hangup, tenant and identity isolation, stable idempotency, timeouts, and issuance of short-lived **self-hosted LiveKit** microphone-only room credentials after acceptance. It uses the standard library and existing SQLite; no paid API is required to run the control plane.

**Not yet a production phone service or commercial SaaS:** LiveKit media now passes real synthetic PCM transport tests and an end-to-end agent-pipeline test on an isolated CI server, but no full German local-model call, installed customer app or user-VPS deployment is established. Ordinary mobile/fixed-line numbers still require a PSTN interconnect. See [HEADLESS_IP_CALLS.md](docs/HEADLESS_IP_CALLS.md) and [real-media evidence](docs/REAL_MEDIA_EVIDENCE.md).


### Free local AI voice worker (real CI transport proven; local speech quality pending)

This feature branch now includes a concrete autonomous **Internet** voice-agent code path:
authenticated agent role -> originate a consented IP call -> wait for acceptance
-> join a self-hosted LiveKit microphone room -> receive real 20-ms PCM frames
-> segment human turns -> transcribe with an **already-installed**
faster-whisper model -> respond using **local Ollama** -> synthesize via an
**already-installed Piper voice** -> publish PCM back to the same WebRTC room.
Incoming speech interrupts the outgoing audio queue. No paid APIs, carrier
plans or new cloud subscriptions are dependencies.

**Status:** a new PR-only CI test has passed real WebRTC PCM transport and
real autonomous agent audio with local **fake** STT/LLM/TTS against a fresh
self-hosted LiveKit instance. No actual speech models, iPhone ringing, VPS
capacity or acceptable natural-language latency have been validated.
The energy detector and whole-turn STT/TTS need production improvements;
no models are installed by default. Synthetic evidence and boundaries:
[real media CI](docs/REAL_MEDIA_EVIDENCE.md). See [local agent execution and acceptance](docs/LOCAL_VOICE_AGENT.md) and
[headless call architecture](docs/HEADLESS_IP_CALLS.md). Public mobile/landline
numbers remain impossible without a separately funded PSTN interconnect.

### Verified business knowledge — first Fonio-parity capability

The independent AI voice agent now supports a private, tenant-scoped,
operator-approved company FAQ. Every new or edited answer remains a DRAFT
until a human explicitly approves that exact revision. Only approved
questions receive approved business answers. Unknown prompts fall back to
a safe configured sentence rather than an ungrounded corporate claim.
Unanswered question recording is disabled by default and can be explicitly
enabled with bounded retention and purge. No paid API is used.

To activate this in the existing IP voice worker, provision a local
knowledge profile and configure PB_AGENT_KNOWLEDGE_DB, following
[approved FAQ setup](docs/AGENT_KNOWLEDGE.md). This strict exact-match
knowledge capability does not yet provide semantic RAG or self-learning.
See the [Fonio feature-gap and release gates](docs/FONIO_PARITY.md).

Offline tests are required but do not prove deployed speech, real PSTN
reachability, a working iPhone background notification or commercial
readiness.

### Self-hosted appointments and autonomous-call safety

A local, independent calendar service now manages explicitly configured
appointment hours, time zones, daylight-saving-time ambiguity, available
slots, atomic/deduplicated booking, caller-owned cancellation and
privacy-preserving read-only iCalendar export. The existing German voice
worker can propose a date and only **after a specific second spoken
confirmation** book or cancel that caller's own slot. Unsupported speech
formats fail closed rather than asking a general LLM to invent a reservation.
No paid calendar account, new SaaS or phone carrier is required for this
internal calendar; it is **not** Google/Outlook two-way synchronization.

The IP calling control plane now enforces per-user Do Not Disturb, a
120-second cooldown against repeated agent-originated calls to the same
person, a 40-concurrent-session tenant safety ceiling, and bounded/aged
event retention. The local LiveKit worker monitors hangup concurrently
with the greeting, so slow local voice synthesis cannot outlive a call.

Technical configuration, booking CLI and honest remaining limitations:
[local calendar and voice booking](docs/APPOINTMENTS.md),
[Fonio feature-parity gaps](docs/FONIO_PARITY.md), and
[local WebRTC voice worker](docs/LOCAL_VOICE_AGENT.md).

**Real CI now verifies synthetic, self-hosted WebRTC transport and an actual
agent media session with fake speech adapters.** It does not prove natural
German conversation, iPhone push delivery, production VPS capacity,
commercial voice latency or public telephone-network connectivity.


### Approved business documents (source-level implementation)

The existing approved agent knowledge database also supports trusted local Markdown/plain-text imports with document-revision approval, bounded paragraph extraction, conservative lexical retrieval, source attribution and a safe no-match fallback. Only reviewed text belonging to the current tenant/assistant can be spoken. This is **not** semantic search or automatic website ingestion. See [document knowledge](docs/DOCUMENT_KNOWLEDGE.md) and [Fonio capability gaps](docs/FONIO_PARITY.md). No paid API or new service is required.


### Consent-gated local post-call notes

The existing self-hosted voice worker can store short, deterministic caller-request notes **only after explicit spoken opt-in in that call**. Revocation before hangup cancels note storage; no audio or full transcript is persisted by this feature. Notes contain up to four short post-consent user excerpts and expire after 14 days (hourly cleanup by the existing call server). No cloud summarizer or paid API. Setup and limitations: [post-call notes](docs/POST_CALL_NOTES.md). This is not an AI-generated summary or email/CRM automation.


### Autonomous human handoff (real RTC integration verified)

An operator may configure one approved employee per tenant/assistant. After an explicit customer spoken request, the AI offers a warm handoff. Staff must accept; their microphone must be observed in the **same** self-hosted LiveKit room before the AI disconnects. The staff/customer call continues without the AI. Declines, expiry, busy/DND and cancelled staff media tokens are handled with a durable, bounded removal journal. A real three-peer LiveKit CI test has passed without PSTN or a paid API. This is **not** a public-number/SIP carrier transfer or finished mobile phone service. See [staff handoff](docs/STAFF_HANDOFF.md) and [real-media evidence](docs/REAL_MEDIA_EVIDENCE.md).


### Optional Web Push for installed first-party PWAs

A new opt-in Web Push path queues incoming Internet call/staff handoff alerts durably in the existing SQLite call registry and delivers VAPID-signed, encrypted notifications to a user's enrolled first-party browser devices. The PWA service worker displays a visible alert and opens the same-origin app for explicit accept/decline. No paid notification vendor, telephone carrier or automatic microphone access is required. Apple Home Screen web apps can receive Web Push from iOS 16.4, but **neither reliable live iPhone delivery nor native CallKit ringing has been demonstrated**. See [PWA Web Push](docs/WEB_PUSH_CALLS.md).


### First-party, unstyled Internet call console

The existing browser SDK now ships a minimal embeddable **authenticated call console** (clients/browser/call_console.mjs). It implements verified incoming/outgoing calls, explicit acceptance/decline/hangup, accepted employee handoffs, self-hosted WebRTC microphone and remote audio, permission-aware reconnection, safe Web Push click resume, and click-only notification subscriptions. It does not store access tokens in URLs/localStorage or automatically answer a push. The host must supply a genuine signed in-memory identity token, locally bundled LiveKit browser SDK and already-authorized HTML root. The source does not contain a complete hosted login system or iOS deployment. See [call console integration](docs/BROWSER_CALL_CONSOLE.md).


### Autonomous incoming AI reception (Internet-only)

A tenant's trusted operator can now enable a **specific AI Internet identity to answer inbound calls** through the existing headless call registry. A long-running local receptionist worker authenticates to the same control API, atomically accepts RINGING invitations, joins the original self-hosted LiveKit room and replies using the same offline Whisper/approved company FAQ or Ollama/Piper voice pipeline as outbound AI. Exclusive 15-second worker leases, independent heartbeats from before media join, crash recovery and fenced hangup protect ongoing calls. Incoming conversations reuse reviewed knowledge, appointment actions, call-scoped note consent and existing live employee handoff. This is an **Internet-to-AI** calling implementation, not a traditional E.164 telephone number or a production-verified iPhone call. [Setup and exact safety boundaries](docs/INBOUND_AI_RECEPTION.md).
