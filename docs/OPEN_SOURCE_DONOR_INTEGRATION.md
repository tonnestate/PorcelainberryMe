# PorcelainberryMe: open-source donor integration register

Verified source baseline: main, 2026-11-04. The named donor projects are
references, not automatically installed runtime dependencies. No vendor code
was copied into this repository in the changes documented below.

| Donor | Reused technical idea / implementation | State |
| --- | --- | --- |
| [LiveKit Agents](https://github.com/livekit/agents) (Apache-2.0) | Abort stale streamed turns on barge-in, preserve a single audio worker, reject stale speaker replies, bound native model overlap | Own Python voice-engine improvements implemented; no second Agents runtime |
| [Speaches](https://github.com/speaches-ai/speaches) (MIT) | Progressive local TTS output and asynchronous speech pipeline | Local Piper stdout can yield bounded 100-ms PCM; opt in with `PB_LOCAL_PIPER_STREAM=1`; no Speaches server installed |
| [Silero VAD](https://github.com/snakers4/silero-vad) (MIT) | Better-than-energy speech classification reference | NOT installed: torch/model-weight footprint unverified for the current VPS |
| [LiveKit React Starter](https://github.com/livekit-examples/agent-starter-react) (MIT) | End-user microphone state and media reconnection patterns | Existing first-party, unstyled call console now has mute/unmute and explicit reconnect; no Next.js deployment |
| [AI Native Call Center](https://github.com/rasonyang/ai-native-callcenter) (Apache-2.0) | Confirm a real staff microphone before completing a transfer | Handoff verifier rejects screen audio; already-existing same-room staff transfer preserved; no FreeSWITCH stack |
| [Bolna](https://github.com/bolna-ai/bolna) (MIT) | Agent workflows / persona configuration | NOT installed: independent orchestration would duplicate the existing controlled agent+calendar+knowledge+handoff path |
| [LiveKit SIP](https://github.com/livekit/sip) (Apache-2.0) | Standards-based SIP to RTC interconnection | NOT deployed: actual SIP account/trunk and carrier are not included in the no-extra-cost scope |
| [Pipecat](https://github.com/pipecat-ai/pipecat) (BSD-2-Clause) | Incremental audio pipeline, interruption and transport separation | Own engine uses incremental PCM and cancellation; Pipecat server/framework is not installed |
| [py-webrtcvad-wheels](https://github.com/daanzu/py-webrtcvad-wheels) (MIT wrapper / native WebRTC copyright) | Native speech/non-speech classifier on 16-kHz 20-ms frames | Actual optional dependency `.[vad]`, selected by `PB_LOCAL_VAD=webrtc`; existing energy VAD remains default |

These are narrow integrations and design transfers, **not a claim that every
listed donor's code has been incorporated**. Revisit third-party dependency
licenses and bundled model licenses before redistributing binaries.

## Additional production repairs

- Native Whisper inference lock remains held by the actual CTranslate2
  worker thread after asyncio cancellation; a second simultaneous inference
  on the same local model fails closed instead of overlapping stale work.
- Only published human microphone audio is eligible for local STT; screen
  audio does not enter the recognized utterance buffer.
- Explicit mute/unmute uses the same room and never creates a new call.
- Terminal LiveKit disconnect releases the stale browser media object;
  rejoin requires server-authorized, user-initiated action.
- Source-level Python/Node tests run for every push. The existing isolated
  self-hosted LiveKit WebRTC media workflow now runs on relevant changes to
  main as well as pull requests, rather than being bypassed by direct-main
  development.

## Not yet released

This work does not certify real German Whisper/Ollama/Piper speech latency,
customer hardware capacity, reliable suspended iOS notification, real
TLS/TURN deployment, a customer SaaS portal, public E.164 PSTN calling or
complete Fonio parity. External carriers remain optional and would have
their own costs. Do not claim a production deployment on the basis of CI.
