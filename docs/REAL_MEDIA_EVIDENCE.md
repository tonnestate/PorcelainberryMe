# Verified real LiveKit WebRTC transport and agent pipeline

## Automated proof (2026-11-03)

GitHub Actions 'Self-hosted WebRTC media integration' runs on an isolated
Ubuntu 24.04 runner only when a relevant pull request changes media code.
Every job creates private local credentials, starts the actual open-source
LiveKit server v1.13.11 (no LiveKit Cloud), then connects real authenticated
Python RTC participants over WebRTC and requires nonzero received PCM.

Evidence: https://github.com/tonnestate/PorcelainberryMe/actions/runs/41699955717

The media test returned:

    {"result":"REAL_WEBRTC_PCM_RECEIVED","received_nonzero_frames":5,"network_transport":"self_hosted_livekit","pstn_used":false,"paid_api_used":false}

The second acceptance test exercised PorcelainberryMe's real HTTP call
control, LiveKitAgentCall, audio turn detector, speaker and real published
and received audio tracks through the same self-hosted media server.
Only the speech content adapters were in-process local fakes, so that
speech/model capacity was not conflated with the network integration.
It returned:

    {"result":"REAL_LIVEKIT_VOICE_AGENT_TURN","remote_nonzero_frames":5,"stt_calls":1,"llm_calls":1,"tts_calls":2,"paid_api_used":false,"pstn_used":false}

The local synthetic 440Hz signal replaced an earlier defective test pattern
at 8kHz (the Nyquist limit for 16kHz PCM), which Opus low-pass filtering
could suppress. The producer now waits for the actual remote subscription
event before sending audio; failing to receive PCM is always a failed CI job.

Test sources:
- .github/workflows/media-integration.yml
- scripts/generate_local_livekit_config.py
- scripts/livekit_audio_smoke.py
- scripts/voice_agent_network_smoke.py

## What this does and does not prove

PROVEN in an isolated CI environment:
- Real LiveKit server starts and accepts authenticated room-limited clients.
- Actual WebRTC signaling, ICE connectivity, publication, subscription and
  transfer of nonzero PCM between two independent participants.
- Real HTTP-driven PorcelainberryMe AI-originated call invitation/acceptance.
- Real LiveKitAgentCall executes its inbound audio frames through
  VoiceTurnEngine into a local STT adapter, a local LLM adapter and local
  TTS audio output received by the human-side RTC test participant.
- Media room teardown requests receive server acknowledgements, not mocks.

NOT PROVEN:
- Real German Whisper transcription, actual installed Ollama Qwen response
  quality/latency, natural Piper voice or interruption accuracy.
- A human browser/mobile microphone or background iOS call ringing.
- Remote production VPS capacity, TLS/TURN routing between heterogeneous
  networks, recovery under packet loss or autoscaling.
- Calling normal PSTN/mobile numbers; that requires carrier interconnection.
- Production readiness, SLA/security audit, data protection compliance.

GitHub Actions consumes runner time according to repository plan limits.
Nothing in this workflow deploys or purchases a third-party cloud service,
phone number, API or extra permanent server. The workflow only runs for
relevant pull requests; it is not a scheduled infrastructure bill.

Only label this milestone 'real, local, synthetic WebRTC media proof',
never 'complete Fonio-equivalent telephone system'.
