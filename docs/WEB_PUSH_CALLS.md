# Internet-call Web Push (self-hosted sender, no provider subscription)

## Product scope

The existing PorcelainberryMe headless IP-call control server now offers
opt-in encrypted Web Push for incoming call and staff-handoff events.
Registered browsers receive time-limited, visible user notifications and
can open the first-party PWA to explicitly accept or reject the live call.
Push uses the browser manufacturer's standards-based delivery endpoints
(Apple/Google/Mozilla) and self-hosted VAPID signing; **no paid push
provider account, telephone carrier, LiveKit Cloud or AI API** is required.
Internet traffic and existing server capacity are still consumed.

**Not a native telephone system**: an ordinary cellular phone number
is not reachable; the iPhone does not show a CallKit phone-call interface
and native sustained ringtone cannot be guaranteed. Notification delivery
and a user's device behavior depend on operating system, Focus, network,
notification permissions and browser push service. The API only knows
that a push provider *accepted* the request, never that the handset rang.

Apple documents Web Push support in iOS/iPadOS **16.4 and later for
installed Home Screen web apps**. The user must add the PWA to Home Screen
and grant permission from a direct interaction inside that web app.
Safari does not allow silent push; the supplied service worker always
shows a user-visible notification for each valid incoming call push.
Official developer docs:
https://developer.apple.com/documentation/usernotifications/sending-web-push-notifications-in-web-apps-and-browsers
https://webkit.org/blog/13878/

## Configure on existing infrastructure

Install only the optional free sender dependency:

    python -m pip install '.[web-push]'

Create a locally stored VAPID P-256 keypair. Do not commit it:

    python -m porcelainberryme.web_push generate-vapid --output /private/pb-vapid.pem

Record the printed VAPID_PUBLIC_KEY. This public key may safely be
served to verified clients; **never serve the private key**.

Configure the existing IP-call server (not a second SaaS):

    export PB_WEBPUSH_PUBLIC_KEY='<printed-P-256-public-key>'
    export PB_WEBPUSH_PRIVATE_KEY='/private/pb-vapid.pem'
    export PB_WEBPUSH_CONTACT='mailto:operations@example.org'

Set a real operator-controlled VAPID contact, not the illustrative
example.org email. Keep PB_CALL_DB and PB_CALL_IDENTITY_SECRET and the
existing self-hosted LiveKit settings as documented in HEADLESS_IP_CALLS.md.
The existing server runs its push outbox worker every 2 seconds alongside
media cleanup. If the push configuration is absent, the feature is OFF,
and call-control polling continues to work unchanged.

Currently the server binds to loopback. A customer-owned HTTPS reverse
proxy is still necessary before any remote browser can register or open
the API. The host application must already manage user login and issue
valid tenant-scoped signed identity tokens; there is no public mint API.

## First-party PWA integration

Serve the source assets from the existing HTTPS web origin:

    clients/browser/pwa/manifest.webmanifest -> /manifest.webmanifest
    clients/browser/pwa/push-sw.js          -> /push-sw.js

The manifest and service worker must be served with suitable types and
registration scope for that first-party web app. A previously existing
app must retain its own routing, icon, UI, and authentication. Do not
replace its root or use an arbitrary third-party origin.

From a user interaction inside the installed PWA:

    const { vapid_public_key } = await client.pushConfig();
    // Cache the public key beforehand; call the following directly
    // inside the opt-in button click/tap handler:
    const registration = await navigator.serviceWorker.register('/push-sw.js');
    await client.enableCallPush({ registration, vapidPublicKey: vapid_public_key });

The registration should generally happen during app startup, and the
permission/subscribe call **must** be inside the click handler, not after
an unrelated asynchronous network fetch, to preserve browser user activation.
Disable from a user-controlled action:

    await client.disableCallPush({ registration });

The server stores only a specific device's browser subscription endpoint
and per-device encryption key, scoped to the authenticated tenant/subject.
The subscription is stored until revoked, replaced or returned expired
by its push service. Maximum 5 per enrolled human. An authenticated
device owner may remove only its own registration; the endpoint cannot
silently be reassigned to a different tenant or account.

On a valid push, the service worker displays 'Eingehender Anruf' or
'Gesprächsübernahme'. The payload contains ONLY a call ID and event kind,
never caller details, conversation text, auth tokens or customer names.
On click it focuses an existing first-party app or opens the PWA route
with an incoming_call parameter. The host app must re-fetch the current
call state before presenting an Accept/Decline control; an invitation
may already have expired. Microphone permission and WebRTC joining
still require an explicit user action and a verified media token.

## Outbox, quotas, security and tests

Outgoing RINGING and HANDOFF_OFFERED events are persisted with push jobs
in the *same database transaction*, surviving restarts and preventing
missing notifications due to process failure. The existing event inbox
still works if push is unavailable. Outbox delivery only occurs while
the actual invitation is pending and not expired, declined or DND.
Messages have a maximum time-to-live of 45 seconds and fewer than four
attempts. Expired browser subscriptions are removed on a verified 404/410
from the external browser push service; bounded queues avoid unlimited
accumulation. Non-sensitive event data is never sent to a paid AI API.

Subscription endpoints are restricted to known TLS-only browser push
service domains to limit SSRF; credential and token material never appears
in client responses or server logs. A public application still needs
complete authorization, IP rate limiting, audit/deletion policies and
an ordinary network security review.

Automated Python and Node tests validate tenant/device ownership,
no-consent behavior, DND, invite TTL, persistence, service-worker display,
click-to-resume, same-origin navigation, bounded retry, 410 cleanup and
API authorization. These source-level tests are NOT proof of real
iPhone background delivery, PWA install, or browser notification timing.

## Release gaps versus Fonio

Still missing: a finished installable first-party customer interface,
live iOS/Android push acceptance, notification cancellation update,
realtime authenticated browser identity provisioning, Contact/CallKit
UI where an actual native app exists, real Whisper/Piper voice latency,
multi-network TURN/TLS production acceptance and PSTN interconnection.
