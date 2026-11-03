# PorcelainberryMe: Headless Internet Calls

## Binding product scope

PorcelainberryMe is an independently sellable headless communications platform. It must support incoming and outgoing real-time Internet calls, including calls that an authorized local AI agent originates towards a human user. TonnEstate, GreenGrapeMe, Agenqo, AnanasMe and other FruitStack systems are not required for its operation.

Constraints: no obligatory subscription, charged cloud model, additional paid telephony service, SIP trunk, purchased phone number, or new monthly hosting expense. Open-source infrastructure still consumes existing CPU, RAM and bandwidth. Do not silently deploy new components onto a capacity-constrained host.

This is a VOIP/IP communications provider **within its own reachable Internet users**, not a PSTN phone carrier. With Internet access alone and no PSTN interconnect it CANNOT ring an ordinary German mobile number, receive a native iPhone cellular call, or terminate a call to an ordinary public phone number. Do not label Internet identities as national telephone numbers.

## Architectural decision: separate four responsibilities

1. **Call control**: PorcelainberryMe owns authentication, tenant-scoped call invitations, agent origination, ringing, acceptance, rejection, hangup, timeouts, events and rate limiting. The first bounded implementation is in src/porcelainberryme/ip_calls.py.
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
- POST /v1/ip-calls/{id}/media-token: participants get short-lived, microphone-only LiveKit room grants only AFTER acceptance

One ringing/active call per actor per tenant. Ring timeout: 45 seconds. Max accepted call: 60 minutes. Duplicate origination with identical caller+key yields identical call. Different destination reusing that key fails closed. Call data and events live in a SQLite file; no real carrier or vendor call is placed.

## Local source-only operation

Generate three distinct secret values; never commit them. Configure:

    export PB_CALL_IDENTITY_SECRET='replace-with-private-32-plus-character-identity-secret'
    export PB_CALL_DB='/private/absolute/path/ip_calls.sqlite'
    export LIVEKIT_API_KEY='self-hosted-livekit-api-key'
    export LIVEKIT_API_SECRET='replace-with-different-private-32-plus-character-media-secret'
    export LIVEKIT_URL='ws://127.0.0.1:7880'
    PYTHONPATH=src python -m porcelainberryme.ip_calls serve --port 8874

Create role credentials LOCALLY, not through HTTP:

    PYTHONPATH=src python -m porcelainberryme.ip_calls token --tenant demo --subject agent_01 --role agent
    PYTHONPATH=src python -m porcelainberryme.ip_calls token --tenant demo --subject user_01 --role human

The API binds ONLY to 127.0.0.1:8874 and is not accessible from remote browsers until a separately secured HTTPS reverse proxy is configured. LIVEKIT_URL specifies a separate independently operated instance: merely providing a URL does not start a media server.

## Production gating: do not misrepresent this branch

- Real self-hosted LiveKit deployment, TURN where needed, two-way audio and observed latency remain UNTESTED. A valid token is not proof of media connectivity.
- A locally executing AI voice agent using this API is NOT YET WIRED. An agent can trigger a call invitation through the existing API, but cannot yet speak autonomously through it.
- Accept/hangup updates the call-control state; it does NOT presently eject already connected participants from the LiveKit room. Add verified room teardown/revocation and recovery before exposing this externally.
- Browser polling cannot ring a sleeping mobile handset. An independent consent-aware push and mobile client protocol is needed.
- Rate limits, global concurrency ceilings, account enrolment, device authentication, event retention, explicit consent/DND policies, privacy audits, abuse prevention, host load tests and crash-recovery behavior are missing.
- An offline test suite covers state transitions, tenant isolation, agent origination, JWT permissions, duplicate rejection, expiration and API handling. It does NOT establish a working PSTN call or live WebRTC audio.

This is a first actual **internet call control implementation**, not a completed telephone provider, and must remain a feature branch until its production gates are satisfied.

## Verified upstream technical sources

- https://github.com/livekit/livekit
- https://docs.livekit.io/home/server/generating-tokens
- https://docs.livekit.io/deploy/custom/deployments/
- https://github.com/livekit/sip (optional SIP bridge; does not provide free PSTN termination)
