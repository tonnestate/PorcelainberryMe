# Opt-in post-call notes (standalone, local)

## Feature and privacy boundary

PorcelainberryMe's autonomous Internet call can now produce a short,
deterministic customer-request note. **Off by default**: neither a note
database nor ordinary conversation history is persisted unless an operator
sets PB_CALL_NOTES_DB AND the human explicitly opts in during that call.
The permission applies only to the current accepted call, never to future
calls. A spoken revocation before the end prevents storage.

Even after consent, only up to four short, successfully completed user
utterances AFTER opt-in are used. No audio, full transcript, pre-consent
dialogue, assistant text, AI-generated facts or externally fetched data
are written by this feature. The note is a bounded excerpt collection,
**not an abstractive AI summary** and not a complete Fonio-equivalent
postprocessing, transcription or notification workflow.

## Operator setup

Configure a private, absolute SQLite file path in the EXISTING
PorcelainberryMe IP-call server and local voice worker processes:

    export PB_CALL_NOTES_DB='/private/existing/call_notes.sqlite'

With no such variable, the conversation engine keeps its default transient
in-memory history and does not instantiate this store.
With the variable, the existing server performs an hourly retention sweep
and note reads/saves also clean up old records. No external worker,
AI/cloud vendor, paid software, new VPS or carrier is required.

## Spoken opt-in and revocation

The following explicit German sentence is the only affirmative command:

    Ich stimme der Speicherung einer Gesprächsnotiz zu.

PorcelainberryMe acknowledges the opt-in and only includes later turns.
Generic utterances such as 'Ja', 'Ich stimme zu' or questions about notes
do not create permission. Revocation is a separate exact phrase:

    Ich widerrufe die Speicherung der Gesprächsnotiz.

The responder acknowledges revocation and discards the permission and
prior note candidates. A new opt-in within the same call permits only
the turns spoken after that later opt-in. These are exact, deterministic
command phrases, not general semantic consent classification. Speech
recognition inaccuracies must be evaluated in real call conditions.

## Storage, retention and operator access

After verified call termination, the active voice engine snapshots only
successfully played dialogue turns and clears the in-memory history.
Only if the final per-call permission remains active does it write the
bounded note to SQLite. The database is tenant/assistant/caller isolated,
with an immutable idempotent call ID and hard size caps.

The default retention is **14 days**, with a sweep at least hourly while
the configured headless control server is running. Read/save operations
also prune expired entries. If the process is offline, expired entries
remain on disk until it starts again or an operator runs the purge.
At most 500 live notes per tenant/assistant are retained; reaching the
limit fails closed rather than silently deleting other people's notes.

Trusted CLI commands (no public administration HTTP endpoints):

    python -m porcelainberryme.call_notes --db "$PB_CALL_NOTES_DB" list --tenant company01 --assistant assistant01

    python -m porcelainberryme.call_notes --db "$PB_CALL_NOTES_DB" list --tenant company01 --assistant assistant01 --caller human01

    python -m porcelainberryme.call_notes --db "$PB_CALL_NOTES_DB" delete-caller --tenant company01 --assistant assistant01 --caller human01

    python -m porcelainberryme.call_notes --db "$PB_CALL_NOTES_DB" delete-call --tenant company01 --assistant assistant01 --call-id 00000000000000000000000000000001

    python -m porcelainberryme.call_notes --db "$PB_CALL_NOTES_DB" prune-expired --tenant company01 --assistant assistant01

The local CLI is intended for a trusted operator. Account/role controls,
data subject deletion beyond this new note store, encryption-at-rest,
consent evidence and a complete GDPR review remain unimplemented.

## Tests and release gates

Offline tests cover no-consent/no-save, pre-consent data exclusion,
revocation and re-consent, per-call isolation, tenant boundaries,
durable idempotency, capped excerpts, TTL cleanup and the synthetic
WebRTC-to-VoiceTurnEngine-to-SQLite lifecycle.
These are source-level tests, not a live, recorded user call.
Automatic email delivery, CRM synchronization, dynamic recipients,
speech-quality validation and user-accessible deletion remain open.

External product comparison: https://www.fonio.ai/de/preise/
