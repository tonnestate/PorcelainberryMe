# First-party Internet call console

## Status and scope

The existing authenticated browser IPCallClient now has a minimal, unstyled,
interactive call console. This is not a second application or a new hosted
SaaS. The component mounts in a trusted existing customer or employee
web page, including a first-party installed PWA.

Implemented actions:

- Receive and verify inbound Internet calls, explicitly accept or decline
- Receive and accept/reject an AI-to-human handoff offer
- Place an outbound Internet-identity call and wait for acceptance
- Establish self-hosted LiveKit microphone and remote-audio playback
- Hang up explicitly or disconnect on a terminal call event
- Reconnect a previously accepted call after a browser media failure
- Handle Web Push clicks/PWA URL resume with fresh authenticated call status
- Enable/disable browser notifications only after an explicit user click

The UI never automatically answers or requests microphone permission after
a push, stores no identity tokens in localStorage/cookies/URLs, does not
invent authentication or create paid API/service dependencies. It adds no
CSS framework, card styles, spacing, wrapping layout or rounding.

## Embed in the existing authenticated customer/employee application

The host application MUST provide a valid, in-memory, tenant-scoped
identityToken obtained through its existing authorized login/session flow.
The token cannot be created or requested anonymously in the public PWA.
The backend intentionally has no public identity-token mint endpoint.

In the host's existing bundled web application, import:

    import { IPCallClient } from './ip_calls_client.mjs';
    import { mountCallConsole } from './call_console.mjs';

    // The existing host provides and verifies hostAuth.
    const identityToken = await hostAuth.getIdentityToken();
    const client = new IPCallClient({
      apiBase: window.location.origin,
      identityToken,
    });
    const consoleUI = mountCallConsole({
      root: document.getElementById('calls'),
      client,
      location: window.location,
    });
    await consoleUI.ready;

The calls element must already exist in the trusted web page. The host
owns authentication, account enrollment, responsive layout, color/style,
design system, accessibility review and deployment. The component generates
only semantic buttons, headings, paragraphs, sections, a recipient field
and a remote audio container.

Build dependency: IPCallClient.connect() dynamically imports the open source
livekit-client browser package when no roomFactory is passed. The host
must bundle that dependency locally into its existing first-party app or
supply a working self-hosted LiveKit roomFactory. This repository does not
yet ship a production browser bundle or full self-service login. It cannot
be honestly sold as a completed standalone phone app without these pieces.

## Incoming calls and Web Push interaction

The console polls the existing authenticated IP call event API every
1.2 seconds while the page is open. Polling starts BEFORE optional PWA
service worker setup; slow or unsupported Web Push cannot block live calls.

RINGING events present explicit Annehmen and Ablehnen buttons. For staff,
only an active server-confirmed HANDOFF_OFFERED targeted at the logged-in
employee allows an employee takeover. The original AI remains connected
until that staff microphone is observed in actual LiveKit media.

After a Web Push click, the service worker opens the first-party PWA
with an opaque incoming_call query parameter. The controller fetches
the current call state from the authenticated server before offering an
action. Unauthorized or expired IDs do not produce a connection. An
already active or pending conversation cannot be replaced without the
current user ending or rejecting it.

Push opt-in must originate directly in a user click, with the public VAPID
key and service worker prepared beforehand. The browser SDK now calls
pushManager.subscribe() synchronously inside the user event handler:
awaiting getSubscription() first can lose transient activation on WebKit.

## Retry safety and temporary mobile disconnects

The browser client now permits HTTPS endpoints and local HTTP loopback only;
it rejects URL userinfo and redirects. Requests have a 10-second default
deadline (configurable within 1–30 seconds), and empty HTTP 503 responses
from the bounded call-control server are interpreted as overload rather
than JSON syntax failures. A timed-out POST is explicitly classified as
uncertain: it may already have been committed to SQLite.

The first-party CallConsoleController retains ONE logical outbound
idempotency key after an uncertain POST. It will use the exact same key
when the user retries the same Internet recipient. When sessionStorage
is available, only the recipient ID and random request key are stored for
the current browser tab, so accidental PWA reloads do not generate a
second dial authority. No JWT, bearer, transcript, microphone recording or
contact data beyond that chosen Internet recipient ID is stored. A
different recipient is blocked until the previous uncertain dial is
reconciled. Terminal replay cannot ring the same call again.

The pending key is erased only after a verified RINGING, ACTIVE or
terminal server response, or a definitive non-ambiguous 4xx policy denial.
A malformed success body never clears this safety fence.
Browser privacy modes without sessionStorage retain this protection only
in the live controller's memory; a new tab may start a new logical
user request, which the host must additionally police if the same human
action can be submitted from multiple devices.

## Media errors and reconnection

The call UI does not auto-connect the caller microphone merely because
a remote party accepted an outbound invitation. It displays Audio verbinden
and the initiating user explicitly activates the microphone. If media
connection fails after successful server acceptance, the UI maintains
the ACTIVE call and offers a new explicit connection attempt. No silent
hangup, recording or background microphone activation occurs.

The remote-audio callback uses LiveKit track.attach() and normal browser
playback. Autoplay may still be blocked. Bluetooth routing, device-specific
permissions, packet loss and call latency need actual hardware testing.

## Microphone control and real media disconnects

The unstyled call console now includes an accessible mute/unmute button
only while an explicitly connected LiveKit call is active. It calls
`IPCallClient.setMicrophoneMuted(boolean)`, which delegates directly to
`room.localParticipant.setMicrophoneEnabled(!muted)`. It never obtains
new credentials, joins a new room or grants background microphone access.
A completed hangup or reconnect resets the displayed muted state.

The browser SDK now watches LiveKit `reconnecting`, `reconnected` and
`disconnected` events. Temporary recovery does not force a second
microphone request; a terminal media disconnect releases the defunct
room object and the console offers an explicit reconnect button for the
still-authorized ACTIVE call. Stale events from an old room cannot clear
the current replacement media room. Manual reconnect rechecks server call
status, obtains fresh room credentials and requires a user interaction.

Node tests cover event delivery, manual recovery, stale room callbacks,
mute/unmute, pre-connect error paths and accessible control state.
These are not actual iOS network-switch, Safari audio permission, or
background push delivery acceptance tests.

## Release gates

Node regression tests exercise verified incoming/outgoing events, employee
handoff, stale/forged push URLs, user consent, media reconnection, call
overlap prevention, browser push permission and minimal accessible controls.

Python backend tests and existing real self-hosted LiveKit media integration
are separate CI gates. No actual iPhone/Android browser call was measured.

Still open for Fonio-level commercial service: customer sign-in and session
renewal, built/deployed first-party PWA, team administration, real iOS
background push delivery, production speech quality/latency, TURN/TLS on
different networks, high availability, payments and telephone PSTN access.

See WEB_PUSH_CALLS.md, STAFF_HANDOFF.md and FONIO_PARITY.md.
