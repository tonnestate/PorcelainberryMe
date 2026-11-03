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

## Mandatory parity gates

| Function | Fonio public claim | PorcelainberryMe as of this change | Release gate |
| --- | --- | --- | --- |
| Ordinary PSTN numbers (inbound/outbound) | Yes, number/SIP/carrier plans | NO PSTN access; only Internet identities | Carrier interconnect required; impossible to claim free PSTN from Internet alone |
| Real incoming calls | Yes | IP invitation and browser polling only, no sleeping handset notification | End-to-end iPhone/mobile and remote network tests |
| Outbound AI calls | Yes on supported plans | Agent invokes IP call API and local RTC worker; not public phone numbers | Live user acceptance, audio, calling policy, error handling |
| Low-latency natural conversation | Yes | PCM pipeline and whole-turn offline speech backends; no live quality proof | German latency/interruption field trials |
| Many voices/languages | 120+ voices, 60+ languages listed | One explicitly installed local Piper voice; German local STT setting | Model inventory, licenses, pronunciation, tests |
| Configurable assistants | Unlimited assistants listed | Basic per-tenant agent identity and now reviewed FAQ profile; no customer portal | Secure self-service enrollment, greeting, workflow, permissions |
| Business website/docs knowledge | Website content, self-learning knowledge and approvals | NEW: local approved text/Markdown document paragraphs plus exact FAQ, revision-specific approval and source title; no website crawling, PDF parser or semantic RAG | Automatic ingestion, multilingual semantic retrieval, evidence evaluation and review UI |
| Conditional call opening | Yes | NEW: per-assistant spoken greeting at call start; conditional start-rules absent | Per-agent condition engine and real listening test |
| Calendar/appointment actions | Yes | NEW: local calendar availability, concurrency-safe confirmed voice bookings, own-booking cancellation and ICS export; no external calendar sync or rescheduling | Real provider calendar adapters, reminders, team booking, flexible speech dates and end-to-end validation |
| Live handoff to a person | Yes | Ending an IP call works; verified transfer/handoff not implemented | Consent-aware transfer + agent context handoff |
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
