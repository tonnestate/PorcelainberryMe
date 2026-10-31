# PorcelainberryMe

A lightweight, Python-first communication fabric for FruitStack. It unifies transport **without** replacing Aurora, CherryMe, PlumMe, AVCOS, AnanasMe or TonnEstate.

**Status: v0.1 implementation / offline verification pending. No VPS deployment.**

## Initial capabilities

- Typed inbound events for email, WhatsApp, SMS and phone.
- Signed, loopback-only HTTP intake from existing trusted webhooks/bridges.
- Durable SQLite event queue with per-tenant/channel/external-ID deduplication.
- Authorized outbound dispatch with a durable at-most-one automatic attempt, clear provider-accepted / failed / unknown states, and no blind retries.
- Optional standard-library SMTP outbound, MIME email intake and official WhatsApp Business Cloud API text sender.
- No SMS carrier, SIP trunk, call media bridge, mailbox poller, or provider secrets bundled.
- A Python speech hook with **no default engine**. Reconnect the earlier approved voice component only after its source and audio have been identified.

## Principles

Transport is not cognition. No duplicated FruitStack agent loop, no persistent local voice model, no autonomous outbound messaging, no duplicate CRM ownership, and no unverified provider switch. Reuse existing TonnEstate communication/consent APIs, mail desk, WhatsApp bridge and AnanasMe presentation.

No external action happens just by installing the package. Sending requires an injected authorization decision and a configured transport. Inbound HTTP never runs an agent.

## Offline test

    python -m pip install -e .
    python -m unittest discover -s tests -v

The package uses Python 3.11+ and has no required third-party runtime dependencies.

## Optional signed inbound bridge

The listening service is loopback-only. Configure a strong random secret and an absolute local database file path (private deployment configuration, never commit credentials):

    PORCELAINBERRY_SIGNING_SECRET=<private 32+ character value>
    PORCELAINBERRY_DB=/var/lib/porcelainberryme/events.sqlite3
    porcelainberryme

POST to http://127.0.0.1:8873/v1/inbound with JSON fields matching InboundEvent. Supply header X-PB-Timestamp (Unix seconds) and X-PB-Signature computed as sha256=HMAC_SHA256(secret, ascii(timestamp) + "." + raw_json_bytes). Timestamps outside five minutes are refused; external event IDs are idempotent. Use a trusted reverse proxy or existing internal bridge for upstream webhook verification; never expose the loopback listener directly to the internet.

The stored inbound events can be read via Ledger.pending(); the approved FruitStack integration is responsible for real processing and Ledger.mark_inbound(). No automatic AI inference is triggered.

## Current evidence boundary

Only code and source-level tests are being developed. Phone conversations, delivery to external recipients, SMTP authentication, WhatsApp API credentials, VPS capacity and voice quality are **not** verified. Accepted by a provider is not equivalent to delivered to the recipient.

See docs/ARCHITECTURE.md, docs/DONOR_AUDIT.md and docs/VOICE_RECOVERY.md.
