# Optional native WebRTC voice activity detection

Status: integrated source and offline regression tests, **not validated as a
German live-call listening improvement**.

## Why this exists

The first PorcelainberryMe voice engine detects speech with a fixed RMS
energy threshold. Background sounds can trigger a false start and soft
speech can be missed. The free `webrtcvad-wheels` project provides the
WebRTC project's lightweight native voice/nonvoice classifier without an
STT model, new media server or cloud subscription:

- Upstream: https://github.com/daanzu/py-webrtcvad-wheels
- Distribution: https://pypi.org/project/webrtcvad-wheels/
- License: Python wrapper MIT; bundled native WebRTC code BSD-style.
  Retain the distribution's own copyright/license notices.
- Frames: our already-used mono PCM16, 16 kHz, 20 ms (640 bytes) fit its
  supported 8/16/32/48-kHz, 10/20/30-ms contract exactly.

This is a genuine optional donor **dependency**, not copied vendor source.

## Enable only on a capacity-reviewed existing host

By default, `PB_LOCAL_VAD=energy` retains existing behavior with no
third-party VAD dependency. To opt in:

```sh
python -m pip install -e '.[voice-local,vad]'
export PB_LOCAL_VAD=webrtc
```

Both incoming receptionist and outgoing local voice agent use the same
`VoiceTurnEngine` path and read this environment variable. Their startup
preflight refuses unrecognized values and detects missing native VAD before
ringing a user or accepting calls. This component operates in-process; it
does not download weights, start services or use paid APIs.

WebRTC VAD runs with fixed aggressiveness mode 1. The existing turn
state machine still provides the original 80-ms speech start, 650-ms
silence boundary, 16-second audio limit, bounded pre-roll and interruption
logic. The native detector is recreated after a completed utterance or
a microphone reset, preventing acoustic state from carrying over a
disconnected/reconnected track.

No changed REST API, database schema, authentication, room credentials,
carrier access or PWA behavior is involved.

## Acceptance and limitations

Offline tests use an injected native-VAD-compatible fake to verify 20-ms
classification, utterance segmentation, state reset, opt-in selection,
required dependency errors and the existing STT->LLM->TTS delivery path.
The ordinary energy detector remains the default for backward compatibility.
No ML model or actual microphone is invoked in these tests.

Before setting `PB_LOCAL_VAD=webrtc` for customers, compare German quiet
speech, accented speech, overlaps, noise, echo and utterance interruption
rates on real host hardware. Measure false triggers, missed speech, time
to first audible response and CPU under concurrent calls. WebRTC VAD
classifies speech; it does **not** perform semantic end-of-turn detection,
speaker diarization, echo cancellation, streaming LLM or streaming TTS.
