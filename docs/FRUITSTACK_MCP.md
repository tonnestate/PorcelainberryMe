# FruitStack ↔ PorcelainberryMe: authenticated, read-only MCP bridge

Status: implemented MCP **server** source and offline tests. No claim that
FruitStack has installed/started/attached the process on the actual VPS.
This is the first authenticated control-plane MCP surface, not a customer
CRM/work execution connector.

## Why one stdio server instead of eight donor runtimes

The existing MangoMe, BlackberryMe and other FruitStack packages already
own their canonical MCP tools. PorcelainberryMe owns Internet call identity,
RINGING/ACTIVE state and LiveKit media. FruitStack should query that source of
truth instead of copying call ledgers into every SaaS or exposing another
HTTP listener. The present adapter only uses the already-authenticated,
loopback IP-call service; no audio, transcription or tokens enter the MCP
result. It does not add a general LLM tool-execution endpoint.

## Install and run through trusted host only

The host must install the optional local MCP dependency *in the existing
Python environment* where PorcelainberryMe is already available:

```sh
python -m pip install -e '.[mcp]'
```

Run the supervised stdio command `porcelainberryme-mcp` only from an
existing trusted FruitStack MCP client with environment secrets supplied by
the operator, never from model/tool arguments:

- `PB_MCP_CALL_TOKEN`: already-issued, signed IPCall identity bearer,
  minimum 32 characters; short and expired tokens fail.
- `PB_MCP_TENANT`: exact authorized tenant (no broad tenant wildcard).
- `PB_MCP_SUBJECT`: exact enrolled agent or human subject.
- `PB_MCP_ROLE`: only `human` or `agent`; not an arbitrary `owner`.

The MCP process is a **single-identity connector**. It checks the actual
`GET /v1/ip-calls/whoami` response against all three trusted environment
fields on *every tool call*. A provider-side token expiry or identity
mismatch fails closed. Existing call-control tenant and participant ACLs
apply independently to the subsequent call query. Neither tools nor
callers can set an auth token, tenant, subject or role per request.

A model-facing host MUST restrict which authenticated user is allowed to
attach this MCP process in the first place. In particular, do not mount an
owner/agent bearer token into a shared untrusted SaaS tenant MCP connection.
The installed supervised host owns rotation of the short-lived bearer and
the lifecycle of this child process.

## Read-only tools

| MCP tool | Input | Source of truth | Result |
| --- | --- | --- | --- |
| `porcelainberryme_capabilities` | none | authenticated `whoami` | scoped tenant, subject, role and declared read-only tool list |
| `porcelainberryme_active_calls` | integer `limit` 1..30 | `GET /v1/ip-calls/active` | bounded, own authorized call IDs and states |
| `porcelainberryme_call_status` | 32-digit lowercase hexadecimal `call_id` | `GET /v1/ip-calls/<id>` | verified status of one call the token is allowed to see |
| `porcelainberryme_call_events` | previous cursor `after`, `limit` 1..50 | `GET /v1/ip-calls/events` | bounded, ACL-filtered call events; next cursor equals the final **returned** event, preventing skipped messages |

All results have a versioned JSON schema. Peer/customer identity,
employee target, tenant directory, JWT, raw provider responses, media URL,
audio, notes and transcripts are never returned. There is no arbitrary
HTTP proxy, generic MCP tool call, call origination, hangup, appointment
write or privileged AnanasMe command in this server. The existing native
call-control and owner-harness authorization boundaries remain unchanged.

Tests use a fake authenticated `CallControlClient` and a fake MCP decorator
to assert the exact four registered tools, per-request identity checks,
redacted output, cross-tenant refusal, invalid arguments and disallowed
read-only scope escalation. They do not start an installed MCP SDK or host.

## Customer call / CRM integration: still open

This **does not** mean PorcelainberryMe already executes GreenGrapeMe,
TonnEstate, MangoMe or PomeloMe actions. A separate customer voice bridge
must obtain tenant and participant identity from a trusted host; resolve an
existing authorized CRM contact without trusting caller ID; read
minimal per-call context; allowlist exact FruitStack tool names and input
schemas; require the underlying MangoMe/PomeloMe effect authority for
writes, stable idempotency keys and reconciliation; and only speak an
externally confirmed result. Arbitrary tool discovery and execution by a
voice LLM would bypass those boundaries and is forbidden.

MangoMe/BlackberryMe already have MCP servers. PorcelainberryMe should
reuse their installed MCP clients/session contracts through the trusted
FruitStack host rather than install a second cognitive runtime. On
current source evidence, that inbound MCP **client** binding, real CRM
contact context and end-to-end effect receipts are NOT implemented.
A production installation still needs configuration and runtime acceptance.

## Opt-in outgoing Internet voice from a trusted Hermes/FruitStack MCP host

The default MCP process remains read-only. A dedicated, supervised MCP stdio
instance can start **one fixed, previously approved** LiveKit voice job when:
- `PB_MCP_CALL_ME_ENABLED=1`
- `PB_MCP_CALL_ME_TARGET` is the already enrolled Internet participant ID,
  obtained from the trusted host, never extracted from an LLM prompt
- `PB_MCP_CALL_ME_REQUEST_ID` is a unique, stable, host-generated idempotency
  key for **this one approved user action** (8..128 safe characters)
- The MCP token authenticates an `agent` identity matching the configured
  tenant and subject; a customer/human identity cannot enable calling
- The preinstalled Whisper, Piper and local Ollama or approved knowledge
  resources are configured using the existing `PB_LOCAL_*` environment.

Only that opted-in process registers the additional tools:
- `porcelainberryme_call_me()`: **no arguments**; schedules the existing
  outbound `call_human` worker which preflights models before ringing and
  handles real LiveKit audio after acceptance.
- `porcelainberryme_call_me_status()`: reports the in-process task state.
  `IN_PROGRESS`, `FAILED`, `CANCELLED` and `FINISHED_UNVERIFIED` are
  truthful execution states, **not evidence of an answered call**.

One MCP process never starts this approved request more than once. Repeated
calls reuse the same task, and the server-side call registry deduplicates
the same stable request ID across process restarts. A subsequent explicit
call needs a newly approved request ID and new supervised MCP process.
There is no arbitrary phone number, dialer, SMS fallback or owner-harness
action tool exposed to the model.

**Important lifecycle limitation:** the worker is a task in the MCP stdio
process. The Hermes/FruitStack host must keep the process alive for the
duration of the call; disconnecting/terminating it can interrupt the audio
worker. Run the existing `voice_livekit_agent.call_human` CLI under normal
host service supervision if calls must outlive an MCP session. No additional
paid model service or separate agent is required.

Custom write-enabled MCP support inside a ChatGPT account is a separate
product capability and connection step: publishing this source does not
automatically add a callable tool to this particular ChatGPT conversation.
