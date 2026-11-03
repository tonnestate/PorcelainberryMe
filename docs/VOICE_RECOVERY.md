# Recovering the earlier Python voice — without another TTS installation

## Evidence from previous work

Our July 20, 2026 voice-gateway discussion named **Piper** (local Python TTS),
a German voice configuration `de_DE`, the service
`tonnestate-voice-gateway.service`, and n8n workflow
`WF-170 Generate TTS`. This is the concrete recovery trail, not proof that
these components still exist or work on the current host.

The October F10 voice named "Thorsten" was evaluated negatively and must not
be substituted for the older positively evaluated sample. The later Qwen3-TTS
experiment did not meet the user's quality expectations. The exact Piper
ONNX voice file, license, original Python script and old audio sample remain
unverified. No provider is automatically selected.

## Implemented in PorcelainberryMe

`src/porcelainberryme/piper_existing.py` has an opt-in adapter for the
**already-installed** Piper CLI and an explicit existing ONNX model path and
matching ONNX JSON config. A caller must inject
`ExistingPiperVoice(executable=..., model=...).synthesize` into the
`PythonVoiceBridge` after source and listening verification.

The adapter never installs a package, downloads a model, creates a background
service, or adds a runtime dependency. It writes a single temporary WAV,
validates it, returns the bytes, and removes the temporary file.

It does not guarantee German pronunciation, acceptable naturalness, real-time
concurrency, phone call suitability, or the identity of the *good* voice.

## Read-only VPS checklist (not executed)

1. Check the current service unit (if any) and the paths it references.
2. Check the n8n workflow **WF-170 Generate TTS** and any Python/CLI commands it calls.
3. Identify exact Piper binary, model ONNX + JSON config, Python script,
   any positive sample audio and model licensing.
4. Measure remaining storage and free memory before enabling any speech path.
5. Only after consent and actual sample comparison, bind the approved source.
6. Do not publish any caller/voice recordings or private paths on GitHub.

No VPS access or voice quality test was performed in this repository milestone.
