# PorcelainberryMe versus Fonio — capability-gated product scope

Reference comparison: 2026-11-03. Only functionalities publicly described
on Fonio's own site are treated as claimed capabilities; this document
does not allege independent hands-on verification of Fonio's service.

Sources:
- https://www.fonio.ai/de/preise/
- https://www.fonio.ai/de/integrationen/
- https://www.fonio.ai/de/use-cases/ki-telefonservice/
- https://www.fonio.ai/de/news-cool-stuff/inbound-webhook-kundendaten-und-dynamische-informationen-bei-anrufen-abrufen/
- https://app.fonio.ai/api/docs

## Reporting milestone (2026-11-04)

PorcelainberryMe now has a signed-participant-scoped reporting API, a
read-only MCP KPI tool, a PWA call-report client and a numeric-only
daily CSV export. Accepted-call timestamps persist for both outbound
recipient acceptance and inbound autonomous agent claims, with
existing-data migration and 30-day retention. See
[REPORTING.md](REPORTING.md).

This is **NOT Fonio analytics parity**. Fonio currently offers a
finished workspace dashboard, call outcomes, transcript/summary,
recordings, campaign reporting and minute costs; none of these can be
substituted by guessed AI results. The present stats are valid only for
participant-authorized call-control outcomes, not full tenant/customer
business intelligence or PSTN billing.

## Mandatory parity gates

| Function | Fonio public claim | PorcelainberryMe as of this change | Release gate |
| --- | --- | --- | --- |
| Ordinary PSTN numbers (inbound/outbound) | Yes, number/SIP/carrier plans | NO PSTN access; only Internet identities | Carrier interconnect required; impossible to claim free PSTN from Internet alone |
| Real incoming calls | Yes | NEW: trusted inbound AI Internet identity autoaccepts human-originated calls under a crash-fenced lease and enters the original self-hosted LiveKit room; first-party browser controls and opt-in PWA Web Push already included | Real Whisper/Ollama/Piper listening, installed iPhone acceptance, TLS/TURN production, public PSTN interconnect |
| Outbound AI calls | Yes on supported plans | Real media CI verifies IP origination, acceptance and actual LiveKit voice-agent PCM with local fake speech providers; no PSTN numbers | Installed real models, human call quality, outbound consent UX, caller abuse testing |
| Low-latency natural conversation | Yes | Real self-hosted PCM transport and full agent pipeline proven with synthetic audio; natural model latency and turn quality remain unverified | German Whisper/Ollama/Piper audio and interruption measurements |
| Many voices/languages | 120+ voices, 60+ languages listed | One explicitly installed local Piper voice; German local STT setting | Model inventory, licenses, pronunciation, tests |
| Configurable assistants | Unlimited assistants listed | Basic per-tenant agent identity and now reviewed FAQ profile; no customer portal | Secure self-service enrollment, greeting, workflow, permissions |
| Business website/docs knowledge | Website content, self-learning knowledge and approvals | NEW: local approved text/Markdown document paragraphs plus exact FAQ, revision-specific approval and source title; no website crawling, PDF parser or semantic RAG | Automatic ingestion, multilingual semantic retrieval, evidence evaluation and review UI |
| Conditional call opening | Yes | NEW: per-assistant spoken greeting at call start; conditional start-rules absent | Per-agent condition engine and real listening test |
| Calendar/appointment actions | Yes | NEW: local calendar availability, concurrency-safe confirmed voice bookings, own-booking cancellation and ICS export; no external calendar sync or rescheduling | Real provider calendar adapters, reminders, team booking, flexible speech dates and end-to-end validation |
| Live handoff to a person | Yes | NEW: approved tenant-scoped employee warm handoff inside the SAME self-hosted LiveKit room; employee accepts, real microphone identity gates agent departure and customer remains connected | PSTN transfer, customer UI, real natural speech quality, multi-route queues, staff context & production host acceptance |
| Conversation summary/transcript | Yes | NEW: per-call opt-in short post-consent user excerpts stored locally with 14-day TTL; no recording, complete transcript or AI summary | Real consent UX, structured extraction, verified local summarization, delivery receipts and external notifications |
| CRM/data integrations | 30+ claimed | Some standalone adapters and owner integrations, not generic audited customer tool execution | Per-tenant scoped tools, webhooks, CRM/approval workflow |
| Incoming caller-specific context | Inbound webhook | Not implemented for independent customer calls | Authenticated tenant-bound context provider with timeout and data minimization |
| Call accounting/concurrency | Plans and limits | Per-tenant hard cap of 40 simultaneous control sessions, AI-call 120s cooldown, per-user DND; no metering or billing | Tenant-configurable quotas, observed capacity, resource ceilings, billing |
| Customer-facing SaaS | Self-service dashboard, team users | No complete customer UI/onboarding/payments | RBAC, portal, settings, subscriptions, audit |
| Privacy and retention | Optional privacy controls | Call audio not persisted by voice engine; 30-day closed-call history pruning after confirmed media cleanup; optional unanswered-question inbox | Tenant-specific GDPR review, deletion rights, configurable retention and consent proof |

## Current concrete additions (2026-11-03)

See docs/AGENT_KNOWLEDGE.md. PorcelainberryMe now contains a local
reviewed FAQ system with explicit human approval, a spoken greeting, revision-invalidating
edits, tenant/assistant isolation, on-demand disabling, safe unknown-answer
fallback and opt-in question inbox with bounded retention. The autonomous
WebRTC voice worker actually selects that responder when configured.
Current FAQ matching is normalized exact-question lookup; document excerpts now use conservative lexical paragraph retrieval with approval, not semantic RAG;
do not advertise it as full Fonio knowledge parity or self-learning.

The new autonomous appointment integration is documented in docs/APPOINTMENTS.md.
It offers an internal SQLite calendar with explicit opening hours, atomic
booking/cancellation, safe confirmation phrases and iCalendar export, but
is NOT synchronized with customer Google/Microsoft calendars. New call
safety gates in ip_calls.py include per-recipient DND, agent cooldown,
tenant-level concurrent control capacity and bounded event retention.
The opening greeting now runs concurrently with call-state monitoring.

## Differentiation beyond Fonio (goals, not unproven claims)

1. Fully headless, open local speech and media components with no required
   billable LLM/voice API; enterprise runtime capacity still costs resources.
2. Agent-driven outbound *Internet* communication with documented consent
   and deterministic local identity rather than a hard product dependency
   on the CRM or a particular FruitStack owner harness.
3. Revision-specific human approval before a company fact can be spoken;
   no use of unreviewed draft content.
4. Multi-tenant local data isolation with configurable no-storage default
   for unanswered utterances.

None of the above establishes Fonio feature parity or permits a claim that
normal mobile/fixed-line telephone numbers are freely reachable. A genuine
telecommunications carrier offering public E.164 service requires licensed
interconnection, number allocation, operational controls and related costs.

## Implementation priority

P0: Production-grade, measured self-hosted WebRTC voice interaction
(inbound, outbound, reconnect, interruptions, safe audio teardown, iOS
background notification) plus capacity/abuse controls.
P1: Business assistant setup, knowledge documents/retrieval/review,
appointment booking with verifiable calendar write receipts, authorized
handoff and call summaries.
P2: Per-tenant generic action tooling, independent customer portal/RBAC,
analytics, billing, audit, privacy and wider language/voice coverage.
P3: Optional customer-funded lawful PSTN/SIP carrier integration. Public
number calling cannot be included in an absolute zero-carrier-cost claim.

Do not mark any feature production-ready solely because offline CI passes.


## Reviewed documents milestone

Reviewable UTF-8 TXT/Markdown paragraphs, document revision approvals, source-title provenance, tenant isolation and fail-closed ambiguity checks are implemented through docs/DOCUMENT_KNOWLEDGE.md. They are integrated into the existing autonomous voice responder, but no website/PDF ingestion or semantic citations are claimed.


## Opt-in follow-up notes milestone

The voice agent now supports call-scoped spoken consent/revocation and a bounded, local SQLite note of user utterances spoken after consent. It does not claim Fonio's full call transcription, AI summaries, email or CRM workflow. The existing IP-call server can prune expired notes hourly without a new daemon or paid provider. Details: docs/POST_CALL_NOTES.md.


## Real WebRTC synthetic media proof

On 2026-11-03, isolated GitHub PR CI passed both two-peer real WebRTC PCM transport and actual autonomous agent audio pipeline, using self-hosted LiveKit v1.13.11 and free local fake STT/LLM/TTS. Evidence: docs/REAL_MEDIA_EVIDENCE.md. Do **not** extrapolate this to natural German speech, actual VPS capacity, iOS/PSTN calling or Fonio product parity.


## Real employee warm handoff milestone

The existing autonomous Internet agent now supports an explicit customer request for a configured, enrolled human operator. The employee accepts/declines via the browser IPCallClient, joins the same LiveKit room and the agent leaves only after the employee's actual published microphone identity is observed. A real three-participant LiveKit PR test delivered nonzero employee-to-customer audio after the AI left, while the original call remained ACTIVE. Cancellation/expiry causes durable room-scoped staff removal attempts until the last self-hosted JWT expires. See docs/STAFF_HANDOFF.md. This does NOT support traditional PSTN transfers, emergency calling or suspended-phone push notifications.


## Browser push notifications milestone

PorcelainberryMe now has an optional self-hosted VAPID Web Push sender, durable pending-call notifications, tenant/device-scoped subscription API, and first-party PWA service worker. Apple supports Web Push for Home Screen apps on iOS 16.4+ with a direct user permission gesture, but a submitted push request is **not evidence of ringing or a delivered phone call**. The service is not a native CallKit/PSTN phone provider. See docs/WEB_PUSH_CALLS.md.


## Browser call console milestone

The existing IPCallClient has a minimal first-party UI controller (clients/browser/call_console.mjs) that requires an authenticated host-provided identity token. It explicitly accepts/rejects incoming calls and staff handoffs, starts outbound Internet-identity calls, connects microphone/remote audio to LiveKit only after a user action, supports manual reconnection and verifies every incoming push call ID against current server state. The component is deliberately unstyled and does not create another app, an unsafe demo login or token storage. See docs/BROWSER_CALL_CONSOLE.md. A complete, deployed login/signup/PWA package and real mobile acceptance remain OPEN.


## Autonomous inbound AI reception

A verified human may now dial an operator-enabled AI Internet identity and be
answered by the existing local WebRTC voice engine. The backend uses a
transactional fenced 15-second AI worker lease with crash recovery and
server-authorized media tokens; the self-hosted AI worker continuously
renews it before and during RTC media connection. Approved company
knowledge, local appointments, per-call notes and employee takeover
are reused for both outgoing and **incoming** AI conversations. Real
isolated LiveKit CI tests use synthetic STT/LLM/TTS outputs; there is
no proven true German phone call quality or E.164 number access.
See docs/INBOUND_AI_RECEPTION.md.
