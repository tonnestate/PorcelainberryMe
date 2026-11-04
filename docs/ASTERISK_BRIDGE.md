# Asterisk ↔ PorcelainberryMe: local chan_websocket PCM transport

Status: media protocol, authenticated-host worker and simulated/real-loopback
WebSocket protocol tests implemented on `main`. **Not deployed against a
real installed Asterisk/PSTN trunk**. No telephone number, carrier, trunk
credentials or provider billing is included.

Official Asterisk media protocol:
https://docs.asterisk.org/Configuration/Channel-Drivers/WebSocket/

## Scope and supported Asterisk

`src/porcelainberryme/asterisk_ws.py` binds a previously authorized Asterisk
channel to the existing `VoiceTurnEngine`. It accepts JSON `MEDIA_START`
with the exact host-provided channel ID, `slin16` / PCM16LE mono at 16 kHz,
20-ms frames and Asterisk's expected 640-byte optimal frame size.
Inbound BINARY WebSocket messages become the same STT/VAD input as LiveKit.
Reply PCM uses `START_MEDIA_BUFFERING`, bounded binary writes,
`STOP_MEDIA_BUFFERING` and `MARK_MEDIA`. Only
`MEDIA_MARK_PROCESSED` confirms a positive playback result. Barge-in
sends `FLUSH_MEDIA`; XOFF/XON provide outgoing backpressure.
This is not a recording or a second model/runtime.

Asterisk `chan_websocket` was introduced in Asterisk 20.16.0,
21.11.0, 22.6.0 and 23.0.0. This implementation requires the **JSON**
control protocol introduced in 20.18.0, 22.8.0 and 23.2.0, or a newer
compatible release. Older plain-text control messages fail closed.

Asterisk dialplan illustrates the expected codec and JSON format, but
is **not** an automatically installed configuration:

```asterisk
[porcelainberryme-ai]
exten => s,1,NoOp(PorcelainberryMe media via existing approved Asterisk call)
 same => n,Dial(WebSocket/INCOMING/c(slin16)f(json))
 same => n,Hangup()
```

Asterisk creates `MEDIA_WEBSOCKET_CONNECTION_ID` for this incoming
media channel. The trusted host must obtain that ephemeral ID and
the ARI channel ID from its **authenticated call-control process**, then
launch one worker bound to that exact call. A caller's CLI text, Caller-ID,
dialplan extension number or voice transcript never grants authorization.

## One-channel worker (existing host Python environment)

Install the existing optional local voice dependencies and locally verified
Whisper/Piper/Ollama assets before accepting a call. The worker does not
install models, listen on public ports, dial another party or alter SIP.

The host launches `porcelainberryme-asterisk` with restricted environment:

```sh
export PB_ASTERISK_MEDIA_ENABLED=1
export PB_ASTERISK_CHANNEL_ID='pbx1-123456789.999'
export PB_ASTERISK_CONNECTION_ID='01234567-89ab-4cde-8fab-123456789abc'
export PB_ASTERISK_TENANT='approved_tenant'
export PB_ASTERISK_ASSISTANT='approved_assistant'
export PB_ASTERISK_WS_PORT='8088'
export PB_LOCAL_WHISPER_DIR='/preinstalled/whisper'
export PB_LOCAL_OLLAMA_MODEL='preinstalled-ollama-model'
export PB_LOCAL_PIPER_BIN='/preinstalled/piper'
export PB_LOCAL_PIPER_MODEL='/preinstalled/voice.onnx'
porcelainberryme-asterisk
```

The IDs and file paths above are *illustrative placeholders*, not a
claim that files or that channel exist. The host must supply real values.
The WebSocket connector is intentionally fixed to local loopback
`ws://127.0.0.1:<port>/media/<connection-id>`. If the existing Asterisk
HTTP endpoint requires Basic authentication, supply both
`PB_ASTERISK_WS_USER` and `PB_ASTERISK_WS_PASSWORD` in the process
environment. Never put credentials in model-visible MCP arguments,
URLs, application logs or repository files.

The legacy `PB_AGENT_KNOWLEDGE_DB` FAQ policy and optional fast local
voice model are reused. Owner-only AnanasMe tools and LiveKit-specific
staff transfer are *not* automatically enabled for an incoming SIP call:
an inbound telephone number is not an authenticated FruitStack owner.

## Explicitly unimplemented / deployment gates

- The trusted ARI/dialplan listener that allocates and passes the real
  channel/media IDs, verifies callers and supervises one worker per call.
- An installed, configured SIP trunk and permission to originate/receive
  real public E.164 calls. Asterisk is free software, not a free connection
  to the national mobile network.
- Real Asterisk 22.8+/23.2+ interoperability, audio codecs/device
  acceptance, hangup propagation and interruption latency under network
  congestion; GitHub CI uses a local protocol-faithful WebSocket simulator.
- Cross-channel CRM contact authorization, call summaries, customer
  handoff and persisted effect receipts on the deployed FruitStack host.
- HA/TLS, firewalling, process quotas, resource capacity and retention.

Do not advertise an ordinary phone call as working before an actual trunk
and Asterisk host have passed their independent real-call acceptance tests.

Tests: `tests/test_asterisk_ws.py` (PCM/mark/FLUSH/XON) and
`tests/test_asterisk_worker.py` (a real aiohttp WebSocket loopback
handshake, PCM path, and signed-channel binding semantics).
