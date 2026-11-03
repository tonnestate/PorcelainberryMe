# Autonomous incoming AI receptionist — self-hosted Internet calls

## Implemented topology

A verified, enrolled user can call a trusted AI identity over the existing
PorcelainberryMe Internet-call API. A local background worker monitoring that
identity's authenticated inbox automatically **accepts** the RINGING invite,
obtains a short-lived, self-hosted LiveKit room token, and speaks through the
existing local Whisper -> approved FAQ / Ollama -> Piper voice stack.

The customer and AI share one real WebRTC room. There is no new cloud
service, paid inference API, external telephone trunk, public mobile
number or second call/media architecture.

The inbound path also reuses the existing bounded audio-turn engine,
optional reviewed company knowledge, deterministic appointment booking,
per-call opt-in notes and approved employee warm handoff. Mutable consent
and booking-dialogue state are **re-created for each call**, even when
the locally installed speech engines are reused for efficiency.

## Operator configuration on the existing trusted server

An operator enrolls the AI identity through the existing local CLI:

    python -m porcelainberryme.ip_calls enroll --tenant exampleco --subject reception

Then enable answering for that exact tenant and identity:

    python -m porcelainberryme.ip_calls inbound-agent --tenant exampleco --assistant reception --enabled

To disable future automated acceptance:

    python -m porcelainberryme.ip_calls inbound-agent --tenant exampleco --assistant reception --disabled

This operation is deliberately NOT available on the public HTTP API.
Disabling it during a call prevents further lease renewals while allowing
the current worker to finish and close the conversation safely.

A human caller must be enrolled and hold a valid tenant-bound **human**
identity token from the host's existing verified login. The caller may
dial the enrolled "reception" **Internet identity** using the existing
browser SDK/console. This does NOT convert reception into a public
E.164 phone number or allow external cellular callers to reach it.

On the trusted local AI worker, configure the same credentials as its
existing, locally bound control server:

    export PB_CALL_IDENTITY_SECRET='<same-private-control-signing-secret>'
    export PB_LOCAL_WHISPER_DIR='/private/existing/whisper-model'
    export PB_LOCAL_OLLAMA_MODEL='already-installed-local-model'
    export PB_LOCAL_PIPER_BIN='/existing/piper'
    export PB_LOCAL_PIPER_MODEL='/private/existing/piper.onnx'

Alternatively, if PB_AGENT_KNOWLEDGE_DB is configured to an existing
provisioned approved-knowledge database, the general Ollama model may be
omitted. The operator must install models and ensure hardware capacity
**before** enabling service; the worker never downloads missing models.

Start the single inbound worker beside the existing call-control API and
self-hosted LiveKit:

    python -m porcelainberryme.voice_inbound_agent --tenant exampleco --agent reception

The worker creates short-lived agent-only JWTs locally to prevent a
long-running receptionist from failing when a fixed 1-hour token expires.
This requires access to the control server's powerful identity-signing
secret, so the process must run with the same private trust boundary,
protected environment/secret storage and no untrusted plugin code.
A least-privilege dedicated worker issuer is a future security hardening
task. The identity secret must never enter JavaScript, browser storage,
URLs, GitHub Actions logs or public interfaces.

A production operator still needs an OS process supervisor with automatic
restart, private file permissions, monitoring and resource limits. Those
deployment artifacts and VPS installation are not included in this PR.

## Call lifecycle and process crash recovery

1. A human initiates an authenticated Internet-identity call; existing
   enrollment, DND, busy and tenant call-capacity gates still apply.
2. Only an operator-enabled AI recipient can claim it. Direct agent
   accept/decline/hangup endpoints are rejected for inbound AI calls.
3. A single SQLite transaction changes RINGING -> ACTIVE and creates a
   random, 128-bit, 15-second exclusive lease for the AI process.
4. The worker receives a lease-fenced LiveKit token and enters the same
   room. The caller can now explicitly join audio through their existing
   UI. The assistant speaks its configured greeting and processes speech.
5. A heartbeat renews the worker lease every four seconds while the
   media session is active. The backend refuses media tokens, staff-transfer
   actions, renewals or termination by a worker with an expired/stale lease.
6. On a crash, another worker can reclaim an ACTIVE inbound call after
   lease expiry. The previous process can no longer end or control it.
   An active browser call and its room need not be deleted merely because
   the AI process restarted.
7. On normal end/error the current owner calls the fenced completion
   endpoint. This ends the call and schedules the existing durable
   LiveKit room teardown. When a verified staff takeover completed,
   completion leaves the customer+employee session ACTIVE instead.
8. The human can always hang up directly. Call and event retention reuse
   the existing 30-day closed-call policy.

**One concurrent call per AI identity** is currently enforced by the
existing endpoint-busy check; multi-receptionist queues, conferences
and fleet-level load balancing are not implemented.

The exclusive worker lease is a control-plane safety measure. An old
process's pre-issued LiveKit room JWT may technically remain usable for
its last few seconds even after its control lease expires. Connecting
a reclaimed worker with the same deterministic identity normally evicts
the old participant, but media-server-level fencing and production
network-split acceptance need independent testing. Do not claim
instantaneous zero-gap access revocation.

## Configuration and safety

The AI cannot call arbitrary public phone numbers and cannot claim
unreviewed customer facts as company policy. It does not record audio
by default. Optional short post-call notes still require explicit
per-call spoken consent and have 14-day retention.

Employee handoff can be enabled with the existing trusted operator staff
route and PB_ENABLE_STAFF_HANDOFF=1. The AI may invite only the enrolled,
configured staff member; staff explicitly accepts. The AI leaves only after
its real LiveKit subscription observes that employee's expected microphone
identity, and the original customer stays connected. Inbound handoff
actions are also protected by the current inbound worker lease.

No customer email, voicemail recording, external CRM write or
public-telephone-company functionality is added here.

## Tests and commercial boundaries

Offline Python regressions cover direct human->AI call acceptance, identity
isolation, tenant restrictions, disabled AI routing, lease contention,
heartbeat, crash recovery, stale process fencing, caller hangup,
same-room LiveKit token restrictions, staff transfer and API routes.
Worker tests cover startup model preflight, event filtering,
non-persistent per-call workflow wrappers and cleanup after errors.

A dedicated real LiveKit CI test joins a local customer and autonomous
AI worker, checks the authenticated control-plane acceptance, transmits
nonzero PCM both ways, calls synthetic STT/LLM/TTS adapters and checks
customer-driven hangup and room teardown. These are **synthetic-audio**
tests, not real Whisper/Ollama/Piper quality, natural voice latency,
human iPhone browser ringing or VPS capacity proof.

Next release gates: authenticated deployable frontend, real German
speech/audio hardware acceptance, TLS/TURN across networks, queue and
availability management, recovery under packet loss/long LLM inference,
real mobile Web Push acceptance, security/retention review and an actual
PSTN interconnect where public phone numbers are required. No external
provider can be eliminated by claiming a public number exists over
Internet-only WebRTC.
