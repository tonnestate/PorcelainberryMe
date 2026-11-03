# PorcelainberryMe: Headless Internet Calls

## Binding product scope

PorcelainberryMe is an independently sellable headless communications platform. It must support incoming and outgoing real-time Internet calls, including calls that an authorized local AI agent originates towards a human user. TonnEstate, GreenGrapeMe, Agenqo, AnanasMe and other FruitStack systems are not required for its operation.

Constraints: no obligatory subscription, charged cloud model, additional paid telephony service, SIP trunk, purchased phone number, or new monthly hosting expense. Open-source infrastructure still consumes existing CPU, RAM and bandwidth. Do not silently deploy new components onto a capacity-constrained host.

This is a VOIP/IP communications provider **within its own reachable Internet users**, not a PSTN phone carrier. With Internet access alone and no PSTN interconnect it CANNOT ring an ordinary German mobile number, receive a native iPhone cellular call, or terminate a call to an ordinary public phone number. Do not label Internet identities as national telephone numbers.

## Architectural decision: separate four responsibilities

1. **Call control**: PorcelainberryMe owns authentication, tenant-scoped call invitations, agent origination, ringing, acceptance, rejection, hangup, timeouts and events. Basic abuse gates now enforce 120-second AI-call recipient cooldown, local DND and a default 40-active-call tenant capacity ceiling. Fine-grained production account/IP rate limiting remains open. Source: src/porcelainberryme/ip_calls.py.
2. **Media**: Self-hosted LiveKit (open-source Apache 2.0) transports real WebRTC audio. PR CI has now started LiveKit v1.13.11 on an isolated runner and verified two independent RTC participants exchange nonzero PCM, using the existing narrowly scoped tokens and Twirp teardown. No LiveKit Cloud account is required. Production VPS/TURN/TLS remain untested.
3. **Speech**: PorcelainberryMe now has a locally runnable VoiceTurnEngine/LiveKitAgentCall worker and optional offline Whisper/Ollama/Piper adapters. The real RTC worker was exercised in CI with local fake STT/LLM/TTS, including nonzero outbound PCM. Real German model weights, speech quality and streaming turn latency remain unverified; no second media server is required.
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

The headless browser adapter still has only source-level tests; no actual browser permission prompt, real iPhone notification, installable app or heterogeneous-network TURN test is verified. Separate Python-to-Python real WebRTC audio and an autonomous voice-agent path have passed against self-hosted LiveKit on a CI runner. See docs/REAL_MEDIA_EVIDENCE.md.

## Remaining production gates (main branch)

- Real isolated-CI LiveKit media transport and agent audio now PASS. Production host installation, real Whisper/Ollama/Piper speech, measured latency, browser-to-phone connection, TURN/TLS and load acceptance are still UNTESTED.
- The actual locally runnable VoiceTurnEngine and LiveKitAgentCall are wired and have passed a real-media CI test with fake local speech backends; this does not establish natural end-user phone conversations or actual installed model performance.
- **Implemented, offline-tested:** A durable LiveKit room-teardown outbox is queued on hangup and active-call timeout. The existing headless server starts a supervised 2-second reconciliation worker if self-hosted LiveKit is configured; it sends authenticated Twirp RoomService/DeleteRoom requests, which forcibly disconnect active participants. Failed requests are retried after persisted delays, including after process restart. LiveKit returns a typed not_found response when the room already does not exist; a generic HTTP 404 is never treated as success.
- **Residual revocation window:** livekit-server can create a room again if a still-valid join token is presented. Media tokens now expire after at most 12 seconds; successful room deletion repeats every 2 seconds through expiry plus 5 seconds of clock allowance. Therefore an ended call is not guaranteed to become silent instantly, and no strong zero-millisecond revocation guarantee is claimed. Actual synthetic two-party media, agent RTC and server room deletion now pass on an isolated self-hosted CI server. Production teardown timing, stale-token reconnection and network loss still require real deployment tests. The current endpoint returns call-control termination before remote media teardown is confirmed.
- Browser polling cannot ring a sleeping mobile handset. An independent consent-aware push and mobile client protocol is needed.
- Basic protection now exists: explicit agent-call consent, per-recipient DND, 120-second cooldown, default maximum 40 concurrent calls per tenant and per-recipient event caps (1,000 events, 30-day retention). Missing: authenticated public account management, configurable rate plans, per-IP/network rate limiting, device authorization, complete privacy rights and host-load acceptance.
- Offline tests cover identity, consent, tenant isolation, JWT, room teardown, retries and restart. Separate real-media CI now proves synthetic PCM delivery and the AI agent pipeline over an actual LiveKit server, with fake local STT/LLM/TTS. Neither test establishes a PSTN call or a human conversation with natural German speech.

This is an integrated **internet call-control and synthetic WebRTC voice implementation** on main, not a completed commercial telephone provider. Release gates above remain mandatory.

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
