# Optional SIP telephony — PorcelainberryMe v0.2 staging contract

## Current status

Source implementation and mocked offline tests, NOT a working PSTN call or
a production Asterisk/PBX system. No VPS changes, SIP registration, phone calls,
audio recordings, or local model downloads were performed.

The module uses the real public Python API of
https://github.com/daily-co/baresip-python (BSD-2-Clause, PyPI v0.6.0).
It is imported only when a trusted host explicitly opens a SipLine.
It is pinned as an optional Python extra. Existing FRITZ!Box/SIP registrar
or another authorized SIP account can be used subject to provider configuration.
Asterisk is NOT required for a simple SIP user agent, and none is installed.

## Capabilities implemented

- SIP account registration and clean teardown via Runtime, Account, UserAgent.
- One active call per SipLine, with additional callers denied instead of
  answered twice. Queue is bounded to eight pending call objects.
- Incoming SIP calls become a canonical InboundEvent with channel=phone and
  kind=call_started, before any answer. A trusted application must supply an
  authorize_incoming(InboundEvent) predicate that returns exactly True.
- Explicitly supervised outgoing SIP dial, subject to a separate
  authorize_outgoing(destination) predicate. Never a default autonomous action.
- Transient 16-bit PCM reads and writes via baresip's aumem audio driver.
- Optional play_wav() method: validates WAV before emitting PCM. Audio
  sample rate and channels MUST match the negotiated SIP TX format; it does
  not guess, transcode or resample.
- No call recording, no STT/TTS, no secret logs and no new background worker.
- No telephone number or SIP password stored in Git.

## Explicit configuration example (an application may adapt this)

The Python package is pure stdlib unless the optional SIP extra is installed:

    python -m pip install -e '.[sip]'

This package is not installed on the VPS by a GitHub commit.

Create SipCredentials using the real SIP registrar, username and
secret supplied securely at runtime. Do not hardcode credentials.
Create a SipLine with explicit tenant_id and two independent
authorization callbacks. Only a trusted service may invoke await line.open().

- Incoming: await line.next_incoming(timeout=...), inspect SipAudioSession.
- Outgoing: await line.dial("sip:destination@registrar.example"), after
  preexisting authorization and an authoritative idempotent task receipt.
- Audio: session.read_pcm(), session.write_pcm(), await session.play_wav(...)
- Close: await line.close() even on errors (async context manager supported).

No background listener is started by importing the module. Calling
next_incoming() does not choose a cognitive agent or authorize external callers.

## Important integration restrictions

1. The caller must route the resulting InboundEvent through the existing
   FruitStack/TonnEstate permission system; this module does not assign a
   CRM identity to an arbitrary SIP caller, and caller-ID is spoofable.
2. The call_id assigned here is process-local correlation, not a
   provider-stable SIP Call-ID or an external exactly-once guarantee.
3. The module does not implement durable outgoing call idempotency. Therefore
   it must NOT be exposed as an unauthenticated API endpoint or called by an
   unsupervised retrying background workflow.
4. The current PythonVoiceBridge / existing Piper adapter can only be connected
   once the originally successful voice implementation is verified. A WAV
   from that engine may need an explicit, quality-tested sample-rate converter
   before SIP transmission. There is no automatic substitute voice.
5. Natural conversational calling additionally needs authorized STT or a
   suitable realtime speech backend, turn detection and interruption control.
   These are NOT implemented by SipLine.
6. SIP accounts may incur carrier/PSTN charges; SIP-to-SIP availability and
   free calling depend on the real registrar and destination.
7. Verify TLS/SRTP, transport firewall behavior, DTMF, provider limits and
   human handover using the actual host/network before production acceptance.
8. Never record or transcribe non-public telephone calls without a lawful
   basis and appropriate disclosures/consents.

## Test criteria

Python 3.11/3.12 offline tests cover account lifecycle, unauthorized inbound
rejection, authorization errors, single-active-call protection,
outbound permission gating, concurrent dial suppression, PCM and WAV format
validation, and clean teardown — all with a simulated baresip backend.

What remains: real SIP account, IPv4/IPv6/NAT validation, audio latency,
jitter, German listening quality, caller hangup, transfers, and an end-to-end
conversation integrated with the existing FruitStack action receipt.

See https://github.com/daily-co/baresip-python/blob/main/examples/03_echo_aumem.py
and examples/04_answer_wav_bot.py for the upstream audio lifecycle.
