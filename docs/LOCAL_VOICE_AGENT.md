# Standalone local AI voice agent — first executable E2E path

## Status (2026-11-03)

The code on feature branch feat/headless-ip-calls now connects **an AI-initiated, explicitly accepted Internet call** to actual LiveKit RTC microphone frames and locally installed speech backends. Source-level, offline tests prove its wiring. **There has NOT been a live call on the user's server**, nor a verified German listening test, CPU/memory benchmark, NAT/TURN assessment or phone-style iOS background notification. This is not yet a production phone service.

This worker is **PorcelainberryMe itself**, not a dependency on AnanasMe, Aurora, GreenGrapeMe or TonnEstate. External apps may trigger its call-control API but are never required for it to run. It does not contact a paid AI provider, does not request an OpenAI key, and does not connect to PSTN or a SIP carrier.

## Actual code path

1. A trusted operator provisions the AI identity and the human account using the loopback-only PorcelainberryMe call API. The human opts in to AI-initiated calls.
2. The operator or authorized local workflow invokes the worker with a stable request ID and a previously issued agent bearer token.
3. The worker verifies its own agent role, subject and tenant using GET /v1/ip-calls/whoami and only then originates the call invitation via POST /v1/ip-calls.
4. The human's *Internet call client* sees RINGING and explicitly accepts. The agent waits up to 50 seconds, then requests a short-lived media token and joins a self-hosted LiveKit room.
5. Only the deterministic participant identity of the intended callee is eligible to supply microphone tracks. The LiveKit RTC Python SDK subscribes to that track and delivers 16 kHz, mono, 20 ms PCM16 frames. No raw audio is recorded by PorcelainberryMe.
6. A capped energy-based speech detector segments the remote microphone into user turns. A new user turn interrupts queued outgoing audio. The recognizer loads an existing, on-disk faster-whisper/CTranslate2 model and transcribes locally in German. It does not download a model ID.
7. Only the loopback Ollama endpoint at 127.0.0.1:11434 is used for the spoken reply. There is no remote OpenAI model, and no tool use or access to the owner harness.
8. An existing Piper executable and ONNX model synthesize local 16-bit mono audio. The worker publishes 20 ms PCM frames via LiveKit's AudioSource and LocalAudioTrack. Successful completed turns are kept as a bounded in-memory conversational history for that call only.
9. The worker observes call-control termination and disconnects. The call server's durable room teardown outbox ensures asynchronous media cleanup.

The above makes real WebRTC audio **possible when all dependencies and models are actually installed**, but does not prove the live deployment or acceptable natural language latency. The current Piper path synthesizes each answer before playback; it is *not* token-by-token streaming TTS. The energy detector is a minimal first pass, not a certified conversational VAD.

## Zero-licensing-fee prerequisites, NOT automatically installed

On the **existing and capacity-tested host only**:

- The already implemented PorcelainberryMe call control service from HEADLESS_IP_CALLS.md.
- A self-hosted LiveKit server that accepts and serves the room-scoped JWTs from that same call-control instance. LiveKit Cloud is neither used nor required. Verify TURN/NAT, valid TLS for remote browsers and bandwidth.
- The optional free RTC/faster-whisper/Python numerical dependencies. Deliberately opt in with python -m pip install -e '.[voice-local]' only after approval. No weights are bundled.
- A previously downloaded local faster-whisper CTranslate2 model DIRECTORY containing model.bin (not a model name requiring auto-download), with RAM/GPU capacity verified.
- A functioning locally installed Ollama server with an explicitly installed model (e.g. the operator's already provisioned Qwen). The worker does NOT start Ollama or pull a model.
- An already-installed Piper binary, a reviewed ONNX voice and its adjacent configuration JSON declaring audio.sample_rate. The originally preferred voice, pronunciation and voice licensing have not been recovered or approved.
- A human user's compatible authenticated WebRTC client with microphone enabled. The browser SDK source adapter exists, but this repository does not currently ship a finished installable phone UI.

There is no requirement for SIP credentials or paid phone number subscriptions. Normal mobile and fixed-line numbers are **not callable under the zero-PSTN-cost, Internet-only constraint**.

## Trusted operator invocation

The commands below are examples of integration with a previously approved, installed local environment, not a claim that these programs/models exist on the user's VPS.

Start self-hosted LiveKit and call control using the same LIVEKIT_API_KEY/LIVEKIT_API_SECRET, as detailed in HEADLESS_IP_CALLS.md.

Enroll both identities:

    PYTHONPATH=src python -m porcelainberryme.ip_calls enroll --tenant example --subject assistant
    PYTHONPATH=src python -m porcelainberryme.ip_calls enroll --tenant example --subject human --allow-agent-calls

A trusted operator creates an agent token locally and passes it in the environment, not via a URL or repository configuration:

    PYTHONPATH=src python -m porcelainberryme.ip_calls token --tenant example --subject assistant --role agent

Provide the actual operator-approved local paths and model name:

    export PB_CALL_AGENT_TOKEN='<bearer-token-created-locally>'
    export PB_LOCAL_WHISPER_DIR='/existing/private/path/to/ctranslate2-model'
    export PB_LOCAL_OLLAMA_MODEL='<explicitly-installed-local-model-name>'
    export PB_LOCAL_PIPER_BIN='/existing/path/to/piper'
    export PB_LOCAL_PIPER_MODEL='/existing/path/to/approved-voice.onnx'

Initiate the call **only after verifying the recipient's permission**:

    PYTHONPATH=src python -m porcelainberryme.voice_livekit_agent \
        --tenant example --agent assistant --to human --request-id approved-call-0001

The worker fails closed if the model directory/executable/voice model is absent or if the authenticated agent identity differs from the supplied subject or tenant. It does not silently substitute an external vendor.

## Safety and operational gates before customer rollout

- **Real audio test**: incoming human microphone captured, German STT recognized correctly, local LLM reply coherent, Piper speech received at the human device, interruption audio stops and room teardown completes. Measure first audio latency and percentiles under CPU load. Test multiple network topologies.
- **Agent initiation**: a model/authorized scheduler may request an outbound call through the call-control API, but a signed agent identity does not create permission to call arbitrary people. Maintain opt-in and prevent repeated unwanted calls. The current worker is a command-line on-demand caller; no unsupervised automated campaign runner has been installed.
- **Barge-in limitations**: energy-based VAD is noise-sensitive, and the old response may remain audible briefly in a remote jitter buffer after clearing the local AudioSource; it is not guaranteed instantaneous.
- **Model latency**: faster-whisper inference, local Ollama completion and full Piper synthesis are sequential and may introduce unacceptable pauses; real listening and timing is the release gate. No model was installed/downloaded or benchmarked in this PR.
- **Resilience**: inference cancellation cannot reliably terminate an already running Python worker thread in faster-whisper or Ollama; cap per-call workers and monitor resources. On remote track errors the call fails closed. Explicit server concurrency quotas, reconnection and telemetry are still needed.
- **iPhone**: an idle/suspended phone app does not start ringing merely because the server queues RINGING. Push notification and foreground/session recovery need dedicated integration and acceptance testing.
- **Multi-tenant SaaS**: signup/device authentication, tenant configuration, retention policies, abuse rate limits, quotas, billing and end-user experience are incomplete. No production readiness claim.

## Source and test evidence

- src/porcelainberryme/local_voice.py
- src/porcelainberryme/voice_livekit_agent.py
- tests/test_local_voice.py
- tests/test_voice_livekit_agent.py
- clients/browser/ip_calls_client.mjs
- docs/HEADLESS_IP_CALLS.md

Run Python 3.11/3.12 offline tests using the repository workflow. Real media remains unverified because no actual self-hosted LiveKit server, models or microphone are attached to the test run.

Technical donor references (source/licensing must be reviewed in the target deployment):

- LiveKit Python RTC SDK: https://docs.livekit.io/reference/python/livekit/rtc/index.html
- LiveKit audio frame: https://docs.livekit.io/reference/python/livekit/rtc/audio_frame.html
- faster-whisper: https://github.com/SYSTRAN/faster-whisper
- Ollama API: https://github.com/ollama/ollama/blob/main/docs/api.md
- Piper CLI (local raw speech): https://github.com/rhasspy/piper

## Real WebRTC media acceptance test (no paid service)

The manual test in scripts/livekit_audio_smoke.py is NOT mocked: it joins
two distinct authenticated RTC endpoints to an already running self-hosted
LiveKit server. It creates and accepts a tenant-scoped AI call in a
temporary SQLite database, sends synthetic PCM via the real media server,
and requires at least five nonzero PCM frames at the other endpoint.
The test also reconciles media termination through the join-token expiry.
No public telephone number, microphone recording or paid API is used.

The test is not automatically run in CI and has not been executed on the
user VPS. Offline Python/Node CI passing is not proof of live audio.

Provide an already running self-hosted LiveKit instance with private key
and secret, on a capacity-tested machine. Then:

    export LIVEKIT_URL='ws://127.0.0.1:7880'
    export LIVEKIT_API_KEY='<self-hosted-key>'
    export LIVEKIT_API_SECRET='<self-hosted-secret-at-least-32-characters>'
    export PB_CALL_IDENTITY_SECRET='<different-private-secret-at-least-32-characters>'
    PYTHONPATH=src python scripts/livekit_audio_smoke.py

A successful output must include REAL_WEBRTC_PCM_RECEIVED and a count of
nonzero received frames. Missing dependencies, zero audio, network errors
and unconfirmed teardown are failures, never silently accepted.

The default LiveKit --dev secret is too short for our required media signing
secret, so the operator must configure strong private self-hosted credentials.
References: https://docs.livekit.io/transport/self-hosting/local/
and https://docs.livekit.io/transport/self-hosting/deployment/.

### Creating an isolated free LiveKit test configuration

Generate a strong local API key, media secret, identity token key and
a private LiveKit YAML configuration using the Python standard library:

    python scripts/generate_local_livekit_config.py --output ./local-private

The command refuses to overwrite existing secrets and writes private files.
Do not commit local-private/ to Git. On a host where the open-source
livekit-server binary is ALREADY installed, point it to the generated YAML
with a loopback-only bind and keep that local server process running:

    livekit-server --config ./local-private/livekit-local.yaml --bind 127.0.0.1

Load the generated local-media.env into the trusted shell (never in
a browser), then run the previously documented livekit_audio_smoke.py.
Do not expose this local-only test instance to the public Internet.

The generator does not install software, buy a telephone number,
download a model, start LiveKit, or use a cloud account. A fully external
WebRTC deployment requires separate firewall/TURN/TLS configuration
and on-host resource verification.
