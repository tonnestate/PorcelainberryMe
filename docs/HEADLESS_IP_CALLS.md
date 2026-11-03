# PorcelainberryMe: Headless Internet Calls

## Binding product scope

PorcelainberryMe is an independently sellable headless communications platform. It must support incoming and outgoing real-time Internet calls, including calls that an authorized local AI agent originates towards a human user. TonnEstate, GreenGrapeMe, Agenqo, AnanasMe and other FruitStack systems are not required for its operation.

Constraints: no obligatory subscription, charged cloud model, additional paid telephony service, SIP trunk, purchased phone number, or new monthly hosting expense. Open-source infrastructure still consumes existing CPU, RAM and bandwidth. Do not silently deploy new components onto a capacity-constrained host.

This is a VOIP/IP communications provider **within its own reachable Internet users**, not a PSTN phone carrier. With Internet access alone and no PSTN interconnect it CANNOT ring an ordinary German mobile number, receive a native iPhone cellular call, or terminate a call to an ordinary public phone number. Do not label Internet identities as national telephone numbers.

## Architectural decision: separate four responsibilities

1. **Call control**: PorcelainberryMe owns authentication, tenant-scoped call invitations, agent origination, ringing, acceptance, rejection, hangup, timeouts and events. Basic abuse gates now enforce 120-second AI-call recipient cooldown, local DND and a default 40-active-call tenant capacity ceiling. Fine-grained production account/IP rate limiting remains open. Source: src/porcelainberryme/ip_calls.py.
2. **Media**: Self-hosted LiveKit (open-source Apache 2.0) is the donor for WebRTC media transport, secure rooms, participant audio and NAT/ICE connectivity. This repository mints a narrowly scoped LiveKit room token, but has NOT installed LiveKit or tested live audio. Unlike a proprietary provider, no LiveKit Cloud account is required.
3. **Speech**: A separate, locally running voice worker must listen, use offline STT/LLM/TTS and publish natural speech in the same room. No provider, worker, model weights, streaming performance or acceptable voice quality have been verified or implemented for this path. Pipecat can be evaluated as the speech orchestration donor; it is NOT a second media-server requirement.
4. **Client and notification**: An authenticated browser/mobile client displays incoming invites and joins the LiveKit room after human acceptance. The first API has authenticated cursor-based polling only; it cannot ring a suspended iOS app in the background. Native push/call UI requires separate platform work.

The existing experimental baresip SIP adapter and the privileged AnanasMe owner-harness remain separate. Neither is on the critical path for Internet calls. No code from the donor was copied.

## Current headless API (loopback-only proof of control plane)

Bearer identity tokens are signed by a trusted backend with subject, tenant, role (human or agent), expiry and an audience. The public API does not issue them. Roles are not permission to harass an end user; callers need a lawful basis and opt-in/policy before any external product use.

- POST /v1/ip-calls with {"to":"user_01","idempotency_key":"stable-request-01"}: initiate ringing
- GET /v1/ip-calls/events?after=0: recipient-scoped event inbox
- GET /v1/ip-calls/{id}: participants' call status
- POST /v1/ip-calls/{id}/accept: callee accepts
- POST /v1/ip-calls/{id}/decline: callee declines
- POST /v1/ip-calls/{id}/hangup: caller or callee terminates control state
- POST /v1/ip-calls/{id}/media-token: participants get short-lived, microphone-only LiveKit room grants only AFTER acceptance and only if media is configured

All participating accounts must be provisioned by a trusted operator. An AI agent can only ring an account whose agent-call consent flag is explicitly enabled. One ringing/active call per actor per tenant. Ring timeout: 45 seconds. Max accepted call: 60 minutes. Duplicate origination with identical caller+key yields identical call. Different destination reusing that key fails closed. Call data and events live in a SQLite file; no real carrier or vendor call is placed.

## Local source-only operation

The control API can run without a media server. Configure the call identity and database first; only set the three LIVEKIT variables when a self-hosted media service is ready. Never commit credentials:

    export PB_CALL_IDENTITY_SECRET='replace-with-private-32-plus-character-identity-secret'
    export PB_CALL_DB='/private/absolute/path/ip_calls.sqlite'
    export LIVEKIT_API_KEY='self-hosted-livekit-api-key'
    export LIVEKIT_API_SECRET='replace-with-different-private-32-plus-character-media-secret'
    export LIVEKIT_URL='ws://127.0.0.1:7880'
    PYTHONPATH=src python -m porcelainberryme.ip_calls serve --port 8874

Create role credentials LOCALLY, not through HTTP:

    PYTHONPATH=src python -m porcelainberryme.ip_calls enroll --tenant demo --subject agent_01
    PYTHONPATH=src python -m porcelainberryme.ip_calls enroll --tenant demo --subject user_01 --allow-agent-calls
    PYTHONPATH=src python -m porcelainberryme.ip_calls token --tenant demo --subject agent_01 --role agent
    PYTHONPATH=src python -m porcelainberryme.ip_calls token --tenant demo --subject user_01 --role human

Use the trusted local CLI for DND (never issue this command from a public HTTP request):

    PYTHONPATH=src python -m porcelainberryme.ip_calls dnd --tenant demo --subject user_01 --enabled
    PYTHONPATH=src python -m porcelainberryme.ip_calls dnd --tenant demo --subject user_01 --disabled

The API binds ONLY to 127.0.0.1:8874 and is not accessible from remote browsers until a separately secured HTTPS reverse proxy is configured. If LIVEKIT variables are unset, call setup and consent work but /media-token returns 503 media_backend_not_configured instead of inventing a functioning media server. LIVEKIT_URL specifies a separate independently operated instance: merely providing a URL does not start a media server.

## Optional browser adapter for a future end-user app

The headless JavaScript adapter in clients/browser/ip_calls_client.mjs can be embedded in a browser/PWA or packaged mobile front end without requiring a particular UI design. It supports agent-initiated invite polling, explicit accept/decline, hangup, and a LiveKit microphone session after acceptance. Its onCallEvent and onRemoteAudio callbacks are owned by the consuming application, not a hidden FruitStack or CRM UI.

It does not ship a UI, place a call to the public telephone network or install a hosted provider. Install the open-source livekit-client NPM library in the consuming app when actual media is needed; the adapter loads it only at connection time. A test client can inject a fake roomFactory for offline tests. The caller must use its own playback element for remote tracks and obtain browser microphone permission.

The code and Node built-in tests are source-level integration checks; no live audio, browser permission prompts, NAT traversal, suspended iPhone background ringing or app installation has been verified.

## Production gating: do not misrepresent this branch

- Real self-hosted LiveKit deployment, TURN where needed, two-way audio and observed latency remain UNTESTED. A valid token is not proof of media connectivity.
- A locally executing AI voice agent using this API is NOT YET WIRED. An agent can trigger a call invitation through the existing API, but cannot yet speak autonomously through it.
- **Implemented, offline-tested:** A durable LiveKit room-teardown outbox is queued on hangup and active-call timeout. The existing headless server starts a supervised 2-second reconciliation worker if self-hosted LiveKit is configured; it sends authenticated Twirp RoomService/DeleteRoom requests, which forcibly disconnect active participants. Failed requests are retried after persisted delays, including after process restart. LiveKit returns a typed not_found response when the room already does not exist; a generic HTTP 404 is never treated as success.
- **Residual revocation window:** livekit-server can create a room again if a still-valid join token is presented. Media tokens now expire after at most 12 seconds; successful room deletion repeats every 2 seconds through expiry plus 5 seconds of clock allowance. Therefore an ended call is not guaranteed to become silent instantly, and no strong zero-millisecond revocation guarantee is claimed. Live media behavior, server connectivity and forced audio disconnection still require a real two-party end-to-end test on self-hosted infrastructure. The current endpoint returns call-control termination before remote media teardown is confirmed.
- Browser polling cannot ring a sleeping mobile handset. An independent consent-aware push and mobile client protocol is needed.
- Basic protection now exists: explicit agent-call consent, per-recipient DND, 120-second cooldown, default maximum 40 concurrent calls per tenant and per-recipient event caps (1,000 events, 30-day retention). Missing: authenticated public account management, configurable rate plans, per-IP/network rate limiting, device authorization, complete privacy rights and host-load acceptance.
- The expanded offline tests cover state transitions, identity and tenant isolation, explicit enrollment/opt-in, AI-originated ringing, JWT issuance, token expiry, actual LiveKit Twirp protocol shape via an injected test HTTP adapter, durable teardown on hangup and timeout, retries after network failure, and reconciliation after database restart. It does NOT establish a working PSTN call or live WebRTC audio.

This is a first actual **internet call control implementation**, not a completed telephone provider, and must remain a feature branch until its production gates are satisfied.

## Verified upstream technical sources

- https://github.com/livekit/livekit
- https://docs.livekit.io/home/server/generating-tokens
- https://docs.livekit.io/deploy/custom/deployments/
- https://github.com/livekit/sip (optional SIP bridge; does not provide free PSTN termination)


## Free self-hosted media teardown contract

With LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET configured to a server under your control, PorcelainberryMe uses no additional cloud account or paid SDK for media cleanup. Its own server runs a dedicated bounded reconciliation thread. The LiveKit admin credential is kept server-side and is **never** returned to browser clients. The only elevated VideoGrant used by cleanup is roomCreate, required by DeleteRoom.

Closed call records (including caller/callee IDs and idempotency data) now have an explicit `closed_at` timestamp. The default 30-day call-history retention expires completed records and their related event/media-journal rows, but **never** purges a call with unfinished LiveKit media teardown. Existing SQLite databases are migrated in place. Since old idempotency keys expire with their records, callers must not replay an authorization request after the retention period. This is the application data default, not a substitute for a customer retention agreement or GDPR assessment.

The call API remains loopback-only. The HTTP response to hangup confirms the **call-control** transition, not that media has already been disconnected. The cleanup journal persists outstanding work in SQLite, with lease recovery and retries after process restart. Inspect server logs for ip_media_reconcile_error and monitor the database in staging before production.

The implementation deliberately avoids assuming an ordinary phone number or an incoming PSTN trunk. Media cleanup does not confer carrier connectivity.

Verified protocol: https://docs.livekit.io/reference/other/roomservice-api/
