# Personal WhatsApp via PorcelainberryMe MCP

Status: optional source implementation on a supervised FruitStack/Hermes MCP
process. Source/CI acceptance is distinct from a deployed connection to an
individual ChatGPT account or the live WhatsApp provider.

## Architecture

The existing TonnEstate/Aurora owner bridge remains the *only* WhatsApp
sender; it routes owner-authenticated POST requests to the already-running
local bridge at 127.0.0.1:3015. The new MCP path does **not** create a
WhatsApp login, session, webhook, polling worker or additional consumer.

The owner is authenticated twice: PorcelainberryMe's existing signed
call-control /whoami must match the trusted tenant/subject/agent role on
every MCP call, and the host must explicitly bind those same values to
the owner WhatsApp capability at MCP startup. The TonnEstate server also
checks its original owner-only API bearer and authoritative owner user.
Never attach this account to a shared/customer-visible MCP process.

An LLM cannot supply a telephone number, recipient, message body or
arbitrary URL to a WhatsApp send tool. **Only the trusted first-party
host** may create an approval bound to the exact recipient, exact text,
sender, tenant, agent, and a verified consent reference. The MCP tool
then takes only the opaque approval ID.

## Host-owned installation

Install the existing project with its optional MCP SDK in the existing
trusted Python environment. The host configures a dedicated
\`porcelainberryme-mcp\` stdio process with:

| Environment variable | Host-controlled meaning |
| --- | --- |
| \`PB_MCP_TENANT\`, \`PB_MCP_SUBJECT\`, \`PB_MCP_ROLE=agent\` | Exact trusted call-control identity |
| \`PB_MCP_CALL_TOKEN_FILE\` | Existing short-lived, rotating, owner-restricted call-control token |
| \`PB_MCP_WHATSAPP_ENABLED=1\` | Explicit WhatsApp opt-in |
| \`PB_MCP_WHATSAPP_OWNER_TENANT\` | Must equal \`PB_MCP_TENANT\` |
| \`PB_MCP_WHATSAPP_OWNER_AGENT\` | Must equal \`PB_MCP_SUBJECT\` |
| \`PB_MCP_WHATSAPP_OWNER_SENDER\` | Existing TonnEstate owner sender identity |
| \`PB_MCP_WHATSAPP_APPROVALS_DB\` | Absolute path to persistent private SQLite approvals |
| \`PB_MCP_WHATSAPP_TOKEN_FILE\` | Absolute path to existing owner TonnEstate API bearer, owned and mode 0600 |
| \`PB_MCP_WHATSAPP_INBOX_DB\` | Optional absolute private existing PorcelainberryMe inbound Ledger SQLite |
| \`PB_MCP_WHATSAPP_OWNER_DESTINATION\` | Required with inbox DB; trusted owner destination used when normalizing events |

No model is allowed to supply these environment values. The host must
create the private data directory with service-user ownership, mount the
original TonnEstate bearer through the existing secret manager, and
continue to use the existing upstream WhatsApp authentication. The API
token file is re-read for each attempted send and never returned in MCP.
No token/credential/account contact is committed to Git.

## Exact MCP tools and mandatory human consent

- `porcelainberryme_whatsapp_inbox(after=0, limit=20, before=0)`
  reads only previously authenticated owner inbound events. With
  `after` it returns **newer** events, with `before` it retrieves
  **older** pages, and with both cursors zero it shows the newest
  messages. The optional `previous_cursor` returned from a page is
  passed to the next call as `before`; `next_cursor` is used for
  new-event polling through `after`. Both directions cannot be used
  simultaneously. This remains a private, locally mirrored inbox,
  not a live remote WhatsApp-history reader.
- `porcelainberryme_whatsapp_prepare(recipient, text)` creates a
  **non-sending** draft with a random 32-character `draft_id`. The
  recipient must be a direct person contact `123...@c.us`, never a
  group; body length is at most 3,000 Unicode characters. It remains
  unapproved for at most 600 seconds.
- `porcelainberryme_whatsapp_send(approval_id)` accepts either a
  previously host-approved legacy approval ID or the prepared
  `draft_id`. An unapproved, rejected or expired draft **never
  reaches** TonnEstate. An approved draft maps atomically to the
  immutable original recipient/body and makes one delivery attempt
  through the existing owner-only transport.
- `porcelainberryme_whatsapp_send_status(approval_id)` returns the
  actual approval/dispatch state, not a delivery or read receipt.

### Trusted FruitStack/Hermes operator review

**Neither a model nor an MCP tool can approve a draft.** The owner
authenticates independently to the existing first-party host, whose
service-user filesystem permissions protect the private 0600 SQLite
approvals database.

1. Hermes invokes `porcelainberryme_whatsapp_prepare` and receives
   `draft_id`, state `DRAFT`; no send or network call occurs.
2. The authenticated owner opens a dedicated local operator terminal
   on that trusted host, with the same `PB_MCP_WHATSAPP_ENABLED=1`,
   `PB_MCP_WHATSAPP_OWNER_TENANT`,
   `PB_MCP_WHATSAPP_OWNER_AGENT`,
   `PB_MCP_WHATSAPP_OWNER_SENDER` and
   `PB_MCP_WHATSAPP_APPROVALS_DB` values as the supervised MCP
   owner agent. The operator runs
   `python -m porcelainberryme.whatsapp_owner_consent approve DRAFT_ID`.
3. The host displays the **exact** recipient, Unicode message,
   content fingerprint and expiry. The owner types
   `APPROVE DRAFT_ID` in the real interactive terminal. Non-interactive
   stdin/stdout, invalid identity or an expired draft are rejected.
   The approval and draft-to-approval mapping commit in one SQLite
   transaction. The operator can instead use `reject DRAFT_ID`.
   `review DRAFT_ID` is available only on a local, interactive terminal.
4. Hermes calls `porcelainberryme_whatsapp_send(draft_id)` only after
   explicit approval. Provider acceptance reports `ACCEPTED`, not
   recipient delivery. An ambiguous provider outcome is `UNKNOWN`,
   never automatically resent. A second send with the same draft ID
   does **not** send again.

The host may integrate the same `OwnerWhatsAppDrafts.review` and
`confirm` methods into an existing independently authenticated
consent UI, but must preserve explicit owner action and the exact
recipient/body comparison. Do not expose these host-only methods to
MCP tools or accept a model's claimed consent as authority.

Run `python -m porcelainberryme.whatsapp_owner_consent prune` from
the trusted host's existing maintenance scheduler to remove expired
drafts and old terminal approvals after 30 days. UNKNOWN/ambiguous
outbound approvals are not automatically deleted. The older host-only
`OwnerWhatsAppApprovals.approve` API remains supported for previously
authenticated first-party workflows.

## Host-only production preflight

The owner host can verify local configuration and optionally its loopback
control-plane endpoints **without** sending a WhatsApp message:

```sh
python -m porcelainberryme.whatsapp_preflight --preflight-only
python -m porcelainberryme.whatsapp_preflight --network --preflight-only
```

The preflight inspects real owner scope, 0600 API/control tokens, private
approval/draft SQLite schema, private mirror SQLite schema, the existing
`127.0.0.1:8873/v1/owner-whatsapp-mirror/readyz` endpoint, and the signed control-plane
`127.0.0.1:8874/v1/ip-calls/whoami` identity. No PII, secrets, message
bodies, token values, database filenames or recipient contacts are written
to its JSON output. The dedicated mirror-ready endpoint returns
HTTP 404 when mirroring is disabled and HTTP 503 when its private
SQLite file/schema is unavailable. The general `/healthz` endpoint
is deliberately not sufficient, because the process can run with
WhatsApp mirroring disabled. A missing check exits with status 2. A successful
`--preflight-only` exits 0, but that does **not** establish real production
acceptance. Without `--preflight-only`, even a successful configuration
check exits with status 3, because live provider/phone acceptance remains
unverified.

Run the actual TonnEstate replay-worker `status` and `check` commands
on its own host identity; this PB preflight cannot read another
service-user's protected outbox or certify that n8n retries HTTP 503.
In production, enable both the separately documented TonnEstate
outbox systemd worker/health timer and the existing PorcelainberryMe
inbound server before an end-to-end test.

The host-only draft pruning command has supervised
`deploy/systemd/porcelainberryme-whatsapp-consent-prune.service.example`
and corresponding `.timer.example` templates. They must be reviewed,
installed and enabled by the existing operator with the same private
`/etc/porcelainberryme/owner-mcp.env` scope as the owner MCP process.

### Supervised private inbound listener (host installation required)

The source package now contains
`deploy/systemd/porcelainberryme-inbound.service.example`.
It is a **review-only deployment template**, not an installed unit.
Install a reviewed copy as `porcelainberryme-inbound.service` through the
existing host deployment procedure. Unlike WhatsApp provider clients,
this unit starts **only** the existing signed loopback HTTP intake
(`python -m porcelainberryme.server`) bound in source to
`127.0.0.1:8873`. The signed HTTP listener is now capped at **64 concurrent
connections** (configurable constructor bound 1–256), uses a **10-second
per-socket timeout**, and immediately returns HTTP 503 at capacity. This
preserves the upstream TonnEstate retry/backpressure semantics; it does not
silently acknowledge an event that the server could not process. Do not start a second WhatsApp consumer.

Run the listener as the existing `porcelainberryme` service user. Provision
`/var/lib/porcelainberryme` with the private SQLite file and exclusive
service-user access, and supply the required
`/etc/porcelainberryme/voice.env` environment file using the trusted host
secret manager. That file must provide the existing
`PORCELAINBERRY_DB` and `PORCELAINBERRY_SIGNING_SECRET`, plus
`PB_WHATSAPP_MIRROR_ENABLED=1`,
`PB_WHATSAPP_MIRROR_SIGNING_SECRET`,
`PB_WHATSAPP_MIRROR_TENANT` and
`PB_WHATSAPP_MIRROR_DESTINATION`. The mirror signing secret must differ
from the normal inbound HMAC secret. The unit has
`ProtectSystem=strict`, restricts persistent writes to
`/var/lib/porcelainberryme`, and deliberately requires the environment
file rather than silently skipping startup when it is absent.

After installing the reviewed unit, reload systemd, enable and start
`porcelainberryme-inbound.service`, and inspect its active state. Verify
`http://127.0.0.1:8873/v1/owner-whatsapp-mirror/readyz` on the
**host**: HTTP 200 with owner-mirror readiness is required. HTTP 404
means opt-in disabled; HTTP 503 means the mirror database is unavailable.
Separately run the existing
`python -m porcelainberryme.whatsapp_preflight --network --preflight-only`
as the authorized owner-MCP service user. All these checks are local,
non-sending readiness checks; none proves an actual WhatsApp delivery.

Review the TonnEstate replay worker unit and matching owner environment,
including the original n8n HTTP-503 retry contract, **before** opening
the mirror to real incoming events. Supervision, database integrity and
preflight results alone are not a production acceptance.

**Required live acceptance before use:** Verify a real authenticated
n8n direct-contact incoming event, exactly-one durable outbox replay,
MCP inbox visibility and subject isolation, explicit human approval of
an outbound draft, one original WhatsApp-bridge submission, verified
provider receipt, restart recovery, secret-file permissions, backlog
alarm and a deliberately injected outage. Do not assert actual contact
delivery or read receipts without authoritative provider evidence.
The live account was not contacted by the offline GitHub tests.

## Passive WhatsApp ingress from the existing TonnEstate consumer

The owner account now has a separate opt-in signed ingestion endpoint
`POST /v1/owner-whatsapp-mirror` on the **existing loopback** 
`127.0.0.1:8873` listener. This endpoint does not claim/process
actionable messages in the original `Ledger.inbound` table: it writes a
dedicated, bounded `whatsapp_owner_mirror` store. This prevents a
second consumer and avoids filling the actionable agent work queue.
The MCP inbox automatically prefers this new passive table; legacy
inbound records remain readable when the mirror table does not exist.

Both services require independently trusted host configuration:

| PorcelainberryMe process | Value |
| --- | --- |
| `PORCELAINBERRY_DB` | Existing private 0600 inbound SQLite file |
| `PORCELAINBERRY_SIGNING_SECRET` | Original signed-inbound secret |
| `PB_WHATSAPP_MIRROR_ENABLED=1` | Explicitly enable passive mirror |
| `PB_WHATSAPP_MIRROR_SIGNING_SECRET` | Different random secret, 32+ characters, dedicated to mirror |
| `PB_WHATSAPP_MIRROR_TENANT` | Verified owner tenant ID, server-side |
| `PB_WHATSAPP_MIRROR_DESTINATION` | Verified owner user ID, server-side |

The original TonnEstate/Aurora authenticated `/api/internal/v1/aurora/reply`
handler optionally invokes a signed mirror client only *after* existing
n8n bearer authentication and database-derived owner identity:

| TonnEstate owner process | Value |
| --- | --- |
| `TE_PB_WHATSAPP_MIRROR_ENABLED=1` | Explicitly enable owner mirroring |
| `TE_PB_WHATSAPP_SIGNING_SECRET_FILE` | Owner-only 0600 file holding the **mirror** signing secret |

The mirror tenant and destination must match the authenticated user
derived from `_owner_scope()`, **not** values in the provider payload.
The old Aurora reply path remains the sole processing/sending authority.
Incoming `from_me=false` direct-contact messages are mirrored
idempotently by original provider message ID; groups and outgoing messages
are excluded. A signed request to the mirror endpoint cannot authorize
the ordinary actionable `/v1/inbound` route (and vice versa).

`PB_MCP_WHATSAPP_INBOX_DB` must reference the existing `PORCELAINBERRY_DB`;
`PB_MCP_WHATSAPP_OWNER_DESTINATION` must match the mirror destination.
The passive table is limited to 10,000 owner messages by default;
entries older than 30 days are purged on each newly inserted event and
can be purged explicitly via host-only
`OwnerWhatsAppMirror.purge_expired()`. No background garbage
collector, message API polling, alternate WhatsApp login, or provider
access is added.

**Runtime database failures:** If the existing private SQLite file becomes
unreadable or a required table disappears, the signed loopback POST returns
HTTP 503 with only the static marker `inbound_storage_unavailable`, never
contact data, message text or exception details. The upstream TonnEstate
outbox must retain the original event and retry after the host repairs
storage. A signed event with the wrong owner scope remains HTTP 403; a
full bounded mirror remains HTTP 507. The live owner-mirror readiness
probe now rejects missing unique owner/message constraints as well as
missing columns or a broken SQLite integrity check.

**Durable source recovery:** TonnEstate's complementary
`porcelainberry_whatsapp_outbox.py` now writes each authorized inbound
observation to its separate, private SQLite queue **before** any network
I/O. If this listener is temporarily unavailable, its supervised
TonnEstate worker retries the identical event with exponential backoff.
The mirror accepts it at most once by content-bound provider message ID.
An `ACKED` outbox record confirms remote mirror persistence, not a
WhatsApp response or AI processing. The 10,000-record source capacity
and source ingest outage remain explicit operational limits; if the
queue fills or its disk is unavailable, TonnEstate must raise/alert a
static failure and does not silently discard pending stored entries.
Absolute source completeness still depends on verified upstream replay
and monitoring for the capacity-exhaustion case.

**Retention operations:** The new
`python -m porcelainberryme.whatsapp_mirror prune` command performs
host-only, tenant/owner-scoped cleanup after the configured 30-day
mirror retention. The repository includes the corresponding hourly
`deploy/systemd/porcelainberryme-whatsapp-mirror-prune.service.example`
and `.timer.example` templates. The existing private 0600
`PORCELAINBERRY_DB` must be used; the command cannot run when mirroring
is disabled or when that file is not private. The separate TonnEstate
outbox automatically prunes old **acknowledged** records during ingress,
while never evicting unacknowledged messages. Operators must schedule
maintenance and monitor any failed unit, rather than rely on opportunistic
cleanup alone.

The new endpoint and owner read view are covered by actual loopback
HTTP, signature isolation, tenant fencing, replay, capacity, retention,
and no-duplicate-consumer tests. Live TonnEstate/VPS deployment and
real personal-account E2E receipt remain separate acceptance gates.

## Explicit limits / acceptance

1. The TonnEstate baseline currently contains a signed owner-send API,
   **not** an authenticated owner history-read API. Consequently the
   inbox covers events already forwarded into the local private Ledger,
   not all WhatsApp messages or historical chats.
2. An authenticated first-party FruitStack/Hermes workflow must still
   create an approval from a real user action. The MCP model itself
   cannot invent an approval. This is intentional.
3. A local stdio MCP server cannot be invoked from an ordinary ChatGPT
   session merely because its source was committed. A supported ChatGPT
   custom remote connector, with the relevant user permissions and a
   supervised authenticated gateway, is a separate installation.
4. No production WhatsApp account or live provider was contacted in
   GitHub CI. Tests use a mocked existing sender, private SQLite,
   concurrency and failure checks, and actual stdio MCP discovery where
   the optional SDK is installed.
5. The owner must define a retention/deletion schedule for the local
   inbox and content-bearing approvals, validate backups, and complete
   the real host security/consent audit before enabling the feature.
