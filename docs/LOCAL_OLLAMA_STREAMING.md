# Local Ollama response streaming to WebRTC audio

Status: source integration and offline synthetic tests, **not a measured
production conversation or proof of lower first-audio latency on the VPS**.

## Implementation

The existing `LocalOllamaResponder` now exposes an async `stream_sentences`
method. The local `VoiceTurnEngine` uses it when the selected responder
supports streaming. Requests go exclusively to the existing loopback
`http://127.0.0.1:11434/api/chat` endpoint with `stream=true`.
Ollama's NDJSON reply chunks are bounded and checked for a final
`done=true` marker. Each complete sentence is synthesized immediately
with the already-configured Piper voice and played through the existing
LiveKit PCM speaker, without waiting for the rest of the LLM response.

Reference: https://docs.ollama.com/api

The free async HTTP client `aiohttp` is included in the existing optional
`voice-local` dependency group. Existing package-only installs do not
contact Ollama or install any speech/model weights.

## Progressive local Piper audio (additional opt-in)

The local Piper adapter can now stream 100-ms PCM packets while the
existing executable is still synthesizing the rest of the current sentence.
The streaming reader keeps at most a bounded subprocess buffer, enforces
the existing 8-MiB response limit, validates PCM sample alignment, and
kills/reaps its child process if the request is cancelled or interrupted.
A complete-response `synthesize()` API remains for legacy integrations.

This stage is OFF by default. To enable it on an existing, capacity-verified
host, set `PB_LOCAL_PIPER_STREAM=1` in the local AI-worker environment.
No extra service or speech model is downloaded. The shared
`VoiceTurnEngine` uses streaming only when the configured synthesizer
explicitly advertises it. Approved FAQ, appointment and handoff policies
still run before the supplied text reaches Piper.

When the caller starts talking during Ollama or Piper streaming, the
engine now cancels a waiting stream reader and closes the async generator,
rather than waiting for the next model/network byte. Stale speech never
creates a successful delivery callback. A partly audible answer cannot
be retracted from the person's ears or remote jitter buffer.

Isolated offline tests use a local synthetic Piper subprocess, exercising
pre-completion output, process exit, cancellation and chunk-level barge-in.
Real German speech synthesis timing, prosody, user microphone and server
capacity are still not certified.

## Safety, compatibility and limits

- Approved FAQ / document, calendar, staff-handoff and consent-gated
  responder wrappers remain authoritative. They currently expose only
  `respond()` and continue through the existing atomic full-response
  path; streaming never bypasses a policy or booking decision.
- A newly recognized human speech turn invalidates the old audio epoch,
  clears queued playback and prevents further chunks from being spoken.
- New speech segments are synthesized sequentially; no unbounded TTS
  batch or duplicate concurrent inference task is introduced.
- Only a **fully heard** response is added to in-memory dialogue history
  and passed to existing post-call consent logic. Interrupted or
  incomplete streamed responses never receive a successful delivery
  callback, even if an earlier sentence was audible.
- An invalid/truncated Ollama stream, empty reply, HTTP error or timeout
  fails the local speech turn; the existing two-failure call-shutdown
  policy remains unchanged.
- Stream responses are bounded to 500 generated characters; a sentence
  only begins after an explicit terminal punctuation boundary followed
  by whitespace, or at the final `done` event.
- Direct sentence streaming does not provide predictive VAD, concurrent
  TTS generation, natural prosody across chunks or real-time semantic
  end-of-turn detection. If the model emits no early sentence boundary,
  the first audio still waits for its complete output.

## Test evidence

Offline tests inject a deterministic fake `aiohttp` local transport to
verify NDJSON sentence emission, localhost-only requests and compulsory
stream completion. Other tests verify incremental TTS packets, history
commit exactly once, post-delivery callback and interruption between
partial sentences. These do not test a real running Ollama or Piper
process, human microphone, bandwidth, model capacity or actual latency.

Before enabling commercially: measure time to first audio, interruption
latency and voice quality with the locally installed German STT/LLM/TTS
under realistic concurrent calls and noisy real microphones.
