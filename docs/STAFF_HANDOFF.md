# PorcelainberryMe: AI-to-human warm handoff (Internet only)

## Implemented behavior

The existing autonomous AI agent can invite an operator-provisioned human staff
member into the **same self-hosted LiveKit WebRTC room** when the customer
uses a clear German spoken request. This is an in-product IP handoff, not a
carrier transfer or a native cellular call. No added phone-number purchase,
paid voice API, LiveKit Cloud or subscription is required.

1. The customer has already explicitly accepted an AI-initiated call.
2. The customer says, for example, 'Bitte verbinden Sie mich mit einem Mitarbeiter'.
3. The signed AI identity requests an invitation only to its operator-approved
   tenant-specific staff route. Arbitrary LLM-selected recipients are disallowed.
4. The employee receives HANDOFF_OFFERED in the existing authenticated event
   stream, then explicitly accepts using the existing browser IPCallClient.
5. After acceptance, the employee obtains microphone-only, short-lived media
   credentials scoped to the same room and joins that room.
6. The originating AI sees that specific approved employee's actual RTC
   microphone track, reports HANDOFF_CONNECTED, and disconnects itself.
7. The customer and employee continue their original ACTIVE room together;
   the departed AI may no longer issue a media token or hang up that call.
8. The employee or customer ends the call normally; the existing durable
   media-room teardown handles actual media disconnection.

The original AI **does not leave** merely because a staff invitation was
accepted or a media token was issued. The RTC staff-microphone event is the
evidence gate. The backend trusts this report only from the signed
originating agent. It does not independently attest LiveKit participant
membership with a server-side ListParticipants call.

## Trusted local configuration

Provision staff with the existing local enrollment CLI:

    PYTHONPATH=src python -m porcelainberryme.ip_calls enroll --tenant company01 --subject staff01

Then configure the only allowed route for the assistant:

    PYTHONPATH=src python -m porcelainberryme.ip_calls handoff-route --tenant company01 --assistant assistant01 --staff staff01

The same trusted CLI can disable the route:

    PYTHONPATH=src python -m porcelainberryme.ip_calls handoff-route --tenant company01 --assistant assistant01 --disable

Use the same PB_CALL_DB and PB_CALL_IDENTITY_SECRET as the running local
headless call server. These commands do not expose public enrollment,
elevated identities or staff-routing writes over HTTP.

Opt into human handoff in the **existing** local AI voice-worker process:

    export PB_ENABLE_STAFF_HANDOFF=1

Without that flag, the autonomous agent does not interpret spoken requests
as control actions. Without the configured staff route, a requested transfer
fails safely and the AI tells the customer it cannot connect an employee.

## Browser staff acceptance

Use the existing clients/browser/ip_calls_client.mjs adapter as the
employee's authenticated Internet client:

    const handoff = await client.acceptHandoff(callId);
    await client.connect(callId);

To reject without ending the customer's active AI conversation:

    await client.declineHandoff(callId);

The host application must still present an incoming-call UI, obtain microphone
permission, attach remote audio and handle media errors. This module does
not provide a finished mobile app or suspended-iPhone push notifications.

## Safety, retry and retention

- A route is scoped by verified tenant and assistant; only enrolled staff
  identities can be configured. DND and busy staff block the initial offer.
- Staff cannot join media while the offer is merely OFFERED; signed staff
  acceptance is mandatory. Unrelated users receive no handoff credentials.
- An offer lasts up to 45 seconds. After staff accepts, up to 60 seconds
  are allowed for their microphone connection. Original customer media
  remains active if staff declines, cancels or times out.
- One target identity may be attempted per original call, preventing a
  cancelled staff member's old media-token revoke journal from being lost
  when a different target is selected. Reattempts require a new call.
- A 12-second-or-shorter staff JWT is minted only after acceptance. The
  actual expiration is also constrained by the staff join window.
- Self-hosted LiveKit RemoveParticipant does **not** invalidate already
  minted join JWTs. On cancellation or expiry after token issuance, a
  durable SQLite outbox repeatedly invokes the room-scoped, staff-only
  RemoveParticipant API until the last token expires plus clock allowance.
  The customer's room is never deleted merely because staff declined.
- On staff join, the agent leaves without sending original-call hangup.
  A completed human-to-human call is hangup-authorized only to the employee
  and original customer. Staff receives terminal call events.
- Event and call-history retention follows the existing 30-day policy.
  Incomplete media teardown remains in the journal across service restarts.

**Revocation is not instantaneous:** a client might briefly connect again
using a not-yet-expired JWT between staff removal attempts. This
implementation minimizes that window but does not claim atomic or zero-ms
revocation on a self-hosted media server.

## Verification and remaining release gates

Python and browser tests cover authorization, tenant separation, active
call state, staff DND/busy, expiry, JWT restrictions, correct RTC staff
identity, persistence after process restart, browser actions and retrying
staff removal after failures. A dedicated PR-only real LiveKit CI test
connects AI, customer and staff and requires audio to continue after the
AI agent leaves. Source: scripts/handoff_network_smoke.py.

This is not yet Fonio-equivalent PSTN transfer, a human-tested German
conversation, a finished customer-facing UI, a mobile call notification
system or an independent enterprise-privacy compliance certification.

LiveKit protocol sources:
- https://docs.livekit.io/reference/other/roomservice-api/
- https://docs.livekit.io/intro/basics/rooms-participants-tracks/participants/
- https://docs.livekit.io/frontends/reference/tokens-grants/
