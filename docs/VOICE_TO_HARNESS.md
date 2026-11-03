# Python/PyPI voice ↔ SIP ↔ existing AnanasMe harness

## Implemented source, not live telephony

Two optional modules extend the existing communications core:

- `voice_stream.py` accepts an **explicitly approved and installed** asynchronous Python/PyPI PCM16LE generator through `InstalledPyPIStreamVoice`. It submits bounded ~20 ms frames to the existing SIP audio channel. It does not select or import a TTS package, download a model or start an agent.
- `ExistingPythonWavVoice` bridges the existing `PythonVoiceBridge` and installed Piper/other Python callable. Its whole WAV is synthesized **before** the first PCM frame. Chunked playback is NOT streaming inference; an interrupted synchronous generation can continue using CPU until it finishes. Concurrent legacy inference is therefore refused until the original generator terminates.
- `SipVoiceTurn` enforces caller-injected playback authorization and one active voice turn. `interrupt()` cancels future frame writes. It cannot guarantee the carrier/hardware has flushed audio already accepted into its jitter buffer.
- `harness_sip_voice.py` binds a verified phone transcript and its durable AnanasMe admission receipt to the **same** authorized SIP session. It polls AnanasMe's exact native assistant result, plays only `DONE`, and records each speech attempt in the existing outbound ledger. Interrupted or uncertain speech is not blindly replayed.

## Audio contract

An existing approved Python/PyPI engine may supply PCM16LE chunks of at most 65,536 bytes each, sample-aligned, with sample rate and channel count exactly equal to negotiated SIP transmission. Total audio is limited to 10 MiB per turn. No implicit resampling, language detection, speaker cloning or format conversion occurs.

A legacy Python voice must return an actual valid 16-bit WAV matching the negotiated SIP format. For sample-rate mismatches, it fails closed and does not play a lower-quality inferred conversion.

## Integration chain (supervised, not auto-activated)

1. Existing SIP provider authenticates/authorizes a caller and creates the `SipAudioSession`.
2. Existing approved speech recognition provides an already-transcribed utterance. Its trusted caller supplies a stable `turn_id`; `event_from_verified_sip_transcript(...)` constructs a canonical phone text event. No STT provider is installed by this component.
3. The signed/authorized existing inbound handler stores the event in the shared Ledger and submits it to the native AnanasMe owner thread using `InboundProcessor` with `handler_kind=OWNER_HARNESS`. The returned 32-character assistant ID proves **admission**, not completion.
4. `NativeSipHarnessReply.speak_ready(event, assistant_id)` verifies stored source content, exact assistant ID, SIP session identity and owner permission, then reads the native AnanasMe result from PR #33.
5. Only a `DONE` native reply may create an outbound SIP speech attempt. `SipVoiceTurn.play` receives that text and uses the explicitly installed approved PCM/WAV voice.
6. After PCM was submitted, the ledger records `ACCEPTED` as SIP adapter acceptance (not proof that the other caller heard it). If interrupted or uncertain, the result is `UNKNOWN` and requires explicit reconciliation; it is never replayed automatically.

## Scope and known limitations

- This is an **owner-verified phone** path. Unverified customers, caller ID alone or an email/WhatsApp From header do not gain access to privileged Aurora controls. General contact conversations still require TonnEstate's CRM-specific policy and a separate authorized conversation contract.
- No AVCoS/CherryMe agent loop, model selection, training, background monitoring or additional PBX is introduced.
- The good previously used PyPI voice remains unidentified. pyttsx3, Piper and chatterbox-tts are historical leads only; no source substitution is made.
- No ElevenLabs dependencies are permitted.
- Real carrier streaming, audio latency, jitter handling, VAD, barge-in detection, actual SIP interruption buffer flushing, host RAM/disk suitability, local STT and voice listening tests remain unverified.
- AnanasMe PR #33 must be deployed with PorcelainberryMe PR #1 for the native-result API. AnanasMe's GitHub-runner failures remain independent from these offline unit tests.

Tests simulate streaming sources, WAV adaptation, interruption, one-at-a-time inference, verified caller binding, durable assistant IDs and no repeated phone speech. They do not place real calls.
