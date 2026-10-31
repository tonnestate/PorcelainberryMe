# Previously used Python/PyPI voice: evidence and recovery

Historical work retrieved from conversations establishes multiple distinct candidates, but no actual deployed model identifier or listening sample:

- February 20, 2023: Python voice synthesis with the PyPI package pyttsx3.
- July 20, 2026: a local Piper voice gateway with German profile de_DE, a service named tonnestate-voice-gateway.service and n8n WF-170 Generate TTS. Exact ONNX voice unknown.
- August 31, 2026: pip install chatterbox-tts was discussed for multilingual voice cloning. Evidence does not establish that the good earlier voice came from it.

Keep those candidates distinct; it would be false to label any unverified one the earlier good voice.

Existing PythonVoiceBridge accepts an injected callable. ExistingPiperVoice can reuse a previously installed local Piper CLI+ONNX+JSON config with no installation. Other installed PyPI sources must be located and verified on the actual host before binding.

Scientific and GitHub candidates for separate offline evaluation: Kyutai Pocket TTS (CPU, German, streaming); Chatterbox Multilingual V3; VoxCPM2 (German+Thai, GPU-intensive); Voicebox as a management/frontend donor. These are not installed here.

The production VPS is storage-constrained; no additional TTS model or PyTorch CUDA bundle should be installed without the read-only disk/RAM inventory. No ElevenLabs dependencies or fallback are allowed.

Streaming latency, barge-in, native German voice quality, commercial model-weight licensing and complete live SIP dialogue remain unverified.
