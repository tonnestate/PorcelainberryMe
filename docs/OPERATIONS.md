# PorcelainberryMe — safe operator handoff (not a production deployment guide)

## Current stage

Offline-tested source. NO installed VPS services, phone calls, external messages,
model downloads or modifications to Aurora, AnanasMe or TonnEstate have happened
as a consequence of commits in this repository.

## Read-only capacity and historical Piper voice check

The code includes a deterministic preflight with zero third-party dependencies:

    python -m porcelainberryme.preflight --disk / \
        --service tonnestate-voice-gateway.service

If the actual former Python/Piper paths are known, the operator can specify:

    python -m porcelainberryme.preflight --disk / \
        --piper-executable /absolute/path/to/piper \
        --piper-model /absolute/path/to/voice.onnx

The reported data contains disk total/used/free, system memory where supported,
explicit file existence/size, and basic systemd state. It does NOT enumerate
caches recursively, list credentials, alter services or check voice quality.

The July 2026 discussion mentioned Piper de_DE, a
tonnestate-voice-gateway.service and n8n WF-170 Generate TTS. These are search
clues, not verified host facts. Never enable any model until the original
pleasant-sounding sample and exact voice profile have been verified.

## Signed local ingress

The `LocalSignedIntake` helper posts canonical events to the loopback-only
`/v1/inbound` endpoint. It is for **existing authenticated and approved**
TonnEstate/n8n integrations. The inbound server has no public provider webhook
and no AI handler; writing into the queue never triggers an agent.

Existing TonnEstate WhatsApp remains the sole production consumer. A future
supervised handover must ensure the existing consumer is disabled, redirected
or intentionally replaced before calling `InboundProcessor.run_once()`.

## Tenant-scoped processing

An owning application injects an explicit `authorize` callback and an
authoritative `handler(event, claim_token) -> persisted_receipt`. These are
mandatory; there is no default LLM/agent handler. A verified claim is
transactionally exclusive across SQLite processes, not just threads.

- PENDING -> PROCESSING -> HANDLED with a nonempty authoritative receipt.
- On permission denial, uncertain delivery or handler exception, the event is
  UNKNOWN and **not retried automatically**.
- An unexpected process death leaves PROCESSING until the operator runs
  `recover_interrupted_inbound(tenant_id=...)`, marking it UNKNOWN for
  reconciliation rather than repeating a possible side effect.
- Only tenant-scoped `pending(tenant_id=...)` and
  `claim_pending(tenant_id=...)` are available in the supported path.
- The existing source event ID must be stable across retries. Content changes
  under the same ID are rejected, not silently overwritten.
- No external processing can be called "exactly once" merely because SQLite
  prevents duplicate local claims.

## Bounded disk behavior

Default inbound capacity is **1,000 retained events across the database**;
the `Ledger` constructor accepts a bounded override. At capacity,
new event IDs fail with HTTP 507 and the signed client raises
`inbound_capacity_exceeded`. Duplicates of previously ingested IDs are still
acknowledged. The system does not automatically delete or erase personal data;
the eventual retention process needs an approved policy and audit.

For a multi-tenant production design, capacity allocation and data retention
must be reviewed before scaling beyond one owner's proof of concept.

## Remaining blockers

1. No direct VPS shell/SSH connection was available for read-only inventory.
2. The real Python/Piper voice binary, ONNX model and licensing are unknown.
3. No inbound n8n/WhatsApp consumer switch was made; no production double-read.
4. No real CRM/AVCOS handler has been connected or approved.
5. No SIP real-time calls, WhatsApp voice calls or SMS carrier in use.
6. No production authentication, network, tenant or load acceptance completed.

The GitHub offline unit tests exercise concurrency, signature checks and
synthetic client/server roundtrips, not real carrier quality or deployment.
