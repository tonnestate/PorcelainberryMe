# Integration contracts — existing infrastructure first

## 1. Existing TonnEstate owner WhatsApp bridge

Confirmed in `tonnestate/TonnEstate`:
- `tonnestate-crm/app/services/aurora_whatsapp_send_service.py`
- `tonnestate-crm/app/api/aurora_whatsapp_internal_api.py`
- `tonnestate-crm/app/services/aurora_whatsapp_orchestrator.py`

The **authorized owner path** is HTTP loopback
`127.0.0.1:5001/api/internal/v1/aurora/send`. The endpoint verifies
`TE_FLASK_API_TOKEN` bearer authorization, requires an `X-Idempotency-Key`
header, **rejects tenant_id** for the owner-only operation, checks owner
identity and forwards to the internal `:3015/send` bridge. The request body
has `recipient_ref`, `text`, `allow_self`. Success has outer `success=true`
with `data.status=sent` and `data.sent_message_id`.

Implemented in `tonnestate_whatsapp.py`:
- `TonnEstateWhatsAppTransport`: uses this **owner-authenticated** API; no additional
  WhatsApp process or linked device.
- `from_existing_whatsapp_event`: converts the already-authenticated
  n8n/Aurora payload (message_id, sender, message, conversation_id,
  from_me=false) into a typed event without side effects.

These objects are NOT activated by importing the library. The existing
TonnEstate orchestrator remains the live consumer until a deliberate, fully
observed handover. Running both consumers in parallel would cause duplicates.

A trusted owner-scoped caller must supply the actual tenant, owner sender
identity, destination, `TE_FLASK_API_TOKEN` and policy-authorizer. Outbound is denied by
default. Do not expose the owner bridge on a public listener. The adapter
does not broaden TonnEstate's owner-only WhatsApp authorization to CRM tenants.

## 2. Existing email and CRM

Existing `email_lead_intake_service.py`, `mail_desk_service.py`,
`contact_comm_event_service.py` and `communication_policy_service.py`
remain authoritative. PorcelainberryMe has no mailbox poller and does not
duplicate CRM contact records. The separately opt-in SMTP adapter has no
authorization until the existing policy callback is supplied. Do not run it
concurrently with an existing mail-desk send path.

## 3. Speech

A prior Python voice-gateway discussion named Piper `de_DE`,
`tonnestate-voice-gateway.service` and `WF-170 Generate TTS`.
The installed binaries and actual preferred voice are NOT confirmed.
`ExistingPiperVoice` connects to existing files only; no downloads.
See `VOICE_RECOVERY.md`.

## 4. Real integration acceptance (not yet passed)

- Read-only check of actual VPS services, disk and model directories.
- Decide which upstream message consumer is authoritative before wiring.
- Verify a real signed inbound message, authorized FruitStack processing,
  dispatch through the existing WhatsApp bridge, and exactly one platform
  acknowledgment.
- Verify permission and tenant isolation, duplicate input and ambiguous
  failures before any production activation.
- Verify an actual German voice recording and phone interruption latency
  before claiming live telephony.

Offline contract tests are *not* evidence of production availability.
