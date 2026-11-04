# Durable FruitStack/Hermes calls: host integration

**Implementation:** GitHub source and CI. The user's actual FruitStack VPS,
registered phone, SIP trunk, Hermes installation and custom ChatGPT connector
have NOT been accessed, installed or verified by this development.

## Architecture

- PorcelainberryMe's existing signed call control stays authoritative for
  enrollment, call consent, DND, cooldown, status, JWT and WebRTC rooms.
- A host-owned approval is stored once in the private SQLite outbound ledger,
  expires in 30-300 seconds and binds tenant, agent, fixed recipient and
  a hashed consent reference.
- Hermes/FruitStack MCP only changes an existing approval to QUEUED by
  approval ID; it cannot approve a call or choose an arbitrary phone number.
- A separately supervised service atomically changes QUEUED to DISPATCHING
  before any attempt to call LiveKit. On restart it does NOT retry an
  ambiguous dial, and records UNKNOWN rather than falsely claiming failure.
- The current fast local voice option reuses installed Whisper, Ollama,
  Piper and the existing VoiceTurnEngine, not a second Hermes AI runtime.
- Asterisk is optional and requires a host-operated ARI/Stasis application
  plus a real SIP trunk for telephone numbers.

## Run the independent outbound worker

The restricted host service user creates an existing private data folder and
sets these environment variables:

  PB_CALL_JOBS_DB=/ABSOLUTE/PRIVATE/outbound.sqlite
  PB_CALL_IDENTITY_SECRET=<EXISTING_CALL_CONTROL_SIGNING_SECRET>
  PB_LOCAL_WHISPER_DIR=<INSTALLED_WHISPER_MODEL>
  PB_LOCAL_PIPER_BIN=<INSTALLED_PIPER_BINARY>
  PB_LOCAL_PIPER_MODEL=<INSTALLED_PIPER_VOICE>
  PB_LOCAL_OLLAMA_MODEL=<INSTALLED_OLLAMA_MODEL>

Alternatively use PB_LOCAL_VOICE_CHAT_MODEL for a separate preinstalled fast
model. This does not download models or require a paid inference API.

The trusted supervisor launches (using the real existing Python interpreter):

  python -m porcelainberryme.outbound_jobs work --tenant TENANT --agent AGENT

This is a persistent service, independent of the Hermes MCP process.
Use the existing systemd environment-file and restart process; do not
expose the agent's signing secret to an MCP client or public server.
More than one worker cannot claim the same queued item. Resource quotas
for concurrent local models still require host acceptance.

## Admit independently verified consent

Only an authenticated FIRST-PARTY host can verify that a real user agreed to
receive a specific call. Its trusted Python workflow invokes
DurableCallJobs.approve(tenant, agent, recipient, consent_reference) or
the corresponding **local operator** command, with the approved consent
source and correct tenant context:

  PB_CALL_APPROVAL_HOST_ENABLED=1 python -m porcelainberryme.outbound_jobs approve --tenant TENANT --agent AGENT --recipient INTERNET_USER --consent-reference VERIFIED_INTENT_REFERENCE

This prints a 32-character random approval ID, valid for 120 seconds unless
an explicitly bounded TTL is selected. The evidence reference is hashed,
not placed into the response. Merely typing an arbitrary consent reference
is NOT verification; the host must authenticate and prove the consent first.
No approval-creation MCP tool exists.

Every genuinely new user-authorized call needs a distinct, immutable
consent_reference from the trusted host. Retrying the SAME reference for
the same tenant, agent and recipient reuses the exact prior approval ID
even if its status is now DISPATCHING, UNKNOWN or finished. Trying to
reuse one reference for another agent/recipient in the same tenant fails
closed. This prevents duplicate ringing after an uncertain approval
response or a host retry.

The approval ledger also enforces 64 unconsumed APPROVED/QUEUED jobs
per tenant/agent by default and at most 10,000 retained records per
tenant/agent. Expired approvals release pending capacity, but the
retained ceiling requires actual retention maintenance. These values
can be narrowed by the trusted host's DurableCallJobs constructor.
Capacity failures do not issue fresh authorizations.

Repeated consumption of the same approved ID is idempotent, including
across MCP/worker restarts. The worker reuses that ID as the existing
call-control idempotency key.

## Stop an approved call before the worker dials

The authenticated first-party host may withdraw an approved or QUEUED
call authorization atomically. The operation is **not** available as a
model-visible MCP write tool. The trusted host invokes
DurableCallJobs.revoke(approval_id, tenant, agent, recipient), or the
local CLI with explicit host-only permission:

  PB_CALL_REVOCATION_HOST_ENABLED=1 python -m porcelainberryme.outbound_jobs revoke --tenant TENANT --agent AGENT --recipient INTERNET_USER --approval-id APPROVED_REQUEST_ID

The result is REVOKED only when the SQLite transaction confirmed that
the worker had NOT yet claimed the job. The entire approval record remains
available under normal retention; re-submitting the prior approval
cannot create a new invitation. The same operation is idempotent across
restarts. Existing ledger files are migrated in place with a
nullable revoked_at column, without inventing new outcomes for
historic records.

If the worker reached DISPATCHING first, revocation reports
DISPATCHING with revocation_confirmed=false. It cannot claim to
cancel an already ringing or accepted WebRTC call. The authenticated
host must instead use the existing per-call control API/hangup and
verify the real terminal state. The concurrent claim/revoke race is
covered by regression tests.

## Long-lived Hermes MCP process

Configure an MCP stdio server in the existing Hermes configuration with
these host-owned environment values:

  PB_MCP_TENANT=TENANT
  PB_MCP_SUBJECT=AGENT
  PB_MCP_ROLE=agent
  PB_MCP_CALL_ME_ENABLED=1
  PB_MCP_CALL_ME_TARGET=FIXED_ENROLLED_INTERNET_RECIPIENT
  PB_CALL_JOBS_DB=/ABSOLUTE/PRIVATE/outbound.sqlite
  PB_MCP_CALL_TOKEN_FILE=/ABSOLUTE/PRIVATE/rotating-agent-bearer

The host atomically rotates the already-issued, 0600-permissioned agent
bearer file. The MCP process rereads it for every API action and never
receives PB_CALL_IDENTITY_SECRET. Do not set PB_MCP_CALL_TOKEN alongside
PB_MCP_CALL_TOKEN_FILE. If using legacy one-shot MCP, omit
PB_CALL_JOBS_DB; legacy calls still depend on the MCP process lifetime.

Allowlist only the required tool names in Hermes:

- porcelainberryme_call_me(approval_id): returns QUEUED or current state.
- porcelainberryme_call_me_status(approval_id): returns durable state.
- porcelainberryme_active_calls: authorized live participant snapshot.
- porcelainberryme_call_status: authoritative per-call visibility.

No model-visible tool may select the recipient or create consent.
The host must provide the approval ID from its authenticated user action.
The MCP server validates the same tenant and agent identity via the
existing call-control whoami operation before each request.

## Asterisk ARI

Asterisk must support JSON chan_websocket (Asterisk 20.18+, 22.8+, 23.2+
or newer compatible versions). The implemented AsteriskARI.prepare()
accepts an existing, independently authorized Stasis channel, creates a
mixing bridge, attaches a slin16 WebSocket externalMedia channel, obtains
the ephemeral MEDIA_WEBSOCKET_CONNECTION_ID and validates it again when
the WebSocket MEDIA_START event arrives. It cleans up only its own
bridge and media channel, not the original caller.

The implemented `porcelainberryme-asterisk-stasis` command owns the
local authenticated Stasis event subscription, applies an exact
application/context/extension allowlist, rechecks the actual channel
through ARI before performing side effects, answers authorized ringing
callers and attaches the existing voice engine. A `StasisEnd` cancels the
active worker; finished calls and bounded rejected overload calls return
to the original dialplan via `/continue` only after ARI reauthorization.
The original caller is not arbitrarily hung up by this bridge.

An installed Asterisk service with a host-owned trusted dialplan route,
valid local ARI credentials and a real SIP trunk when public phone
numbers are needed remain deployment prerequisites. Nothing in this
repository creates free calls to the public telephone network.

## Verified release gates that still require the real host

- Successful Web Push/PWA reception and human acceptance on a real phone.
- Real installed Whisper/Ollama/Piper latency and barge-in measurements.
- Restart Hermes during a call; the independent worker must continue.
- Crash a dialing worker, restart it and prove no ambiguous redial.
- Existing Stasis service, Asterisk version, SIP trunk and actual carrier call.
- Real FruitStack contact matching, CRM writes, authorization and receipts.
- Private file ownership, model CPU/RAM, backups, retention/deletion, TLS/TURN.
- Connecting a supported write-enabled custom MCP app to the user's
  ChatGPT account. A GitHub push does not connect an app to this chat.

GitHub tests are engineering evidence, never proof of live deployment.
