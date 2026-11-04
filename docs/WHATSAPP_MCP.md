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

## Exact MCP tools

- \`porcelainberryme_whatsapp_inbox(after=0, limit=20)\` reads at most 30
  previously authenticated personal WhatsApp *inbound* messages, scoped
  by configured tenant and owner destination. This is not a live account
  history reader: the trusted existing n8n/Aurora ingress must persist
  such events in the owner-only Ledger first. A separate message consumer
  must not be started.
- \`porcelainberryme_whatsapp_send(approval_id)\` atomically consumes an
  already-approved message and invokes the existing TonnEstate/Aurora
  owner-only sender once. It cannot select a new recipient or change text.
- \`porcelainberryme_whatsapp_send_status(approval_id)\` returns the bounded
  outcome without exposing the message text, target, bearer or provider
  receipt.

Host-only Python uses
\`OwnerWhatsAppApprovals(database).approve(tenant=..., agent=...,
sender=..., recipient=..., text=..., consent_reference=...)\` only after
verifying a real owner's explicit consent for that specific message and
recipient. Distinct messages need distinct consent references; replay
of a reference is idempotent and changed content is rejected.
\`revoke(...)\` is a host-only method and succeeds only before dispatch.

Successful TonnEstate acceptance is \`ACCEPTED\`, **not** confirmation of
delivery/read by the contact. Provider timeout, unclear HTTP status and
crash between dispatch and receipt are \`UNKNOWN\` and never blindly
retried. An attempt already claimed by another process likewise returns
\`UNKNOWN\` until its result can be reconciled. Definite rejection is
\`FAILED\`. Approval expiry defaults to 120 seconds, configurable by
the host to 30–300 seconds, and 64 simultaneous pending approvals are
the default ceiling per owner agent.

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
