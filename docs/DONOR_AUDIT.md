# Donor audit (source review 2026-10-31)

Primary external repositories:
- https://github.com/NousResearch/hermes-agent — MIT root license; messaging adapters, message events, durable delivery ledger and separate voice paths. Source files reviewed directly via GitHub connector. Do not run Hermes AIAgent as a second FruitStack brain.
- https://github.com/novuhq/novu — reference for agent communication infrastructure, channel identity and replies. Full community Docker composition includes Redis, MongoDB, API and workers; not installed and no source copied.
- https://github.com/pipecat-ai/pipecat — real-time audio pipeline candidate for later telephone stage, not an immediate dependency.

Existing internal source reviewed:
- https://github.com/tonnestate/AnanasMe (README, private voice adapter/test).
- https://github.com/tonnestate/Aurora (integration/aurora_voice.py and docs/ANANASME_PRIVATE_VOICE.md).
- https://github.com/tonnestate/TonnEstate (communication, email, consent and WhatsApp service modules).

Decisions:
- No new reasoning model, independent session brain or voice model installed in v0.1.
- New Python contracts are independent of both Hermes and Novu runtime dependencies.
- SMS, WhatsApp and phone are separate provider adapters with explicit consent/call scope; no universal carrier assumed.
- Existing Aurora and TonnEstate code is not modified by this repo.
- Commercial use: recheck individual module assets/licensing if code is copied later, not just the root license.
