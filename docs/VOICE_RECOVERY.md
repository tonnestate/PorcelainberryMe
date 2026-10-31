# Recovery of the previously successful Python voice

User requirement: reuse the earlier Python-developed, well-sounding voice if the exact implementation can be recovered. The previous Qwen3 voice quality was unsatisfactory. Recent Aurora speech may be silent.

## Verified / unverified

Verified current sources:
- Aurora/integration/aurora_voice.py implements slower non-streaming Qwen3 synthesis and faster-whisper transcription.
- Aurora/requirements-private-voice.txt lists qwen-tts, faster-whisper and soundfile.
- Previous desktop voice_helper/F9/F10 discussions mention local Whisper and a voice called 'Thorsten', which the user described as unpleasant.

NOT VERIFIED:
- The source path, package, model, voice preset or sample audio of the *good* earlier Python voice.
- Any working phone conversation via Aurora.
- Current VPS speech engine and free disk/RAM.

## Implementation constraint

src/porcelainberryme/voice.py defines a PythonVoiceBridge into an already approved callable that returns SpeechResult(audio, mime_type). The default is deliberately UNBOUND and fails with VOICE_SOURCE_NOT_VERIFIED.

No voice model download, no copy of unknown sample recordings, no silent substitution. Before enabling: locate the original Python source, retrieve its actual package/version and voice selection, verify permission to use that voice, compare a known audio output, then measure cold/warm latency and CPU/RAM on the VPS.

## Safe follow-up (read-only)

Inspect actual VPS process mounts, Python scripts, application services, installed TTS packages and reference WAV paths without deleting or replacing assets. Keep personal reference recordings and credentials outside GitHub. Classify problems as runtime configuration, missing dependency, implementation drift or missing capability rather than guessing.
