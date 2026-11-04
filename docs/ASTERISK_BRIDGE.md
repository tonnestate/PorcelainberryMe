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

## Authenticated ARI Stasis bridge (implemented in source)

The new asterisk_ari.py helper accepts an existing, independently
authorized ARI/Stasis caller channel. It checks the real channel, creates
a mixing bridge, attaches an incoming slin16 JSON WebSocket externalMedia
channel, obtains its MEDIA_WEBSOCKET_CONNECTION_ID, and runs the
existing AsteriskVoiceMedia/Piper/Whisper/Ollama worker. It cleans up
only media and bridge resources created by the helper, never the caller.
The dedicated `porcelainberryme-asterisk-stasis` worker now connects to
the authenticated loopback ARI `/ari/events` WebSocket itself, and matches
only the **exact configured Stasis app + dialplan context + extension**.
Before creating any media resources, it re-reads the original channel via
authenticated ARI GET, verifies the same route, and explicitly answers
a trusted caller still ringing. It rejects other apps, contexts, dialplan
extensions and duplicated StasisStart events. After normal media completion
or a voice failure it verifies the original caller again and returns it
to the next dialplan step using `/channels/{id}/continue`. `StasisEnd`
cancels the owned audio worker immediately; capacity-excess calls are
returned to the dialplan without answering, with a bounded return task
queue. The listener never force-hangs up another call.
A caller ID or a phone number is not a credential, and this route is
not an authorization for private FruitStack owner commands.

This ARI control path is tested against a real locally simulated ARI
HTTP server, including successful setup, denial, bounded responses,
error cleanup and strict media channel binding. It is NOT evidence of
a deployed Asterisk installation or working PSTN numbers.

## Explicitly unimplemented / deployment gates

- Real service installation on the user's Asterisk host; proof that the
  trusted dialplan route cannot be selected by unauthorized callers and
  that channel/media cleanup survives actual connection failures.
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

## Supervised Stasis event listener (new)

The same package now contains a persistent listener that watches the
**locally authenticated** Asterisk Stasis application. It starts up to
four (default one) preauthorized sessions and handles temporary ARI
WebSocket disconnects without replaying previous calls. Unknown
or differently routed Stasis events are rejected before creating a
media bridge. It never uses an incoming Caller-ID to unlock the private
FruitStack owner agent.

The existing host must define an Asterisk dialplan context that routes
only calls approved for this public-facing AI receptionist into the
configured Stasis app. Example **configuration template only**:

```asterisk
[pbx_ai_entry]
exten => s,1,NoOp(Approved public-facing AI receptionist route)
 same => n,Stasis(porcelain_voice)
 same => n,Hangup()
```

The following service environment settings must be provisioned through
a private, root-managed environment file (do not commit actual values):

```ini
PB_ASTERISK_STASIS_ENABLED=1
PB_ASTERISK_STASIS_APP=porcelain_voice
PB_ASTERISK_TRUSTED_CONTEXT=pbx_ai_entry
PB_ASTERISK_TRUSTED_EXTENSION=s
PB_ASTERISK_ARI_USER=HOST_CONFIGURED_ARI_USER
PB_ASTERISK_ARI_PASSWORD=HOST_CONFIGURED_ARI_PASSWORD
PB_ASTERISK_ARI_PORT=8088
PB_ASTERISK_TENANT=HOST_APPROVED_TENANT
PB_ASTERISK_ASSISTANT=HOST_APPROVED_AGENT
PB_ASTERISK_MAX_CALLS=1
PB_LOCAL_WHISPER_DIR=/preinstalled/whisper
PB_LOCAL_VOICE_CHAT_MODEL=preinstalled-fast-ollama-model
PB_LOCAL_PIPER_BIN=/preinstalled/piper
PB_LOCAL_PIPER_MODEL=/preinstalled/voice.onnx
```

The example is not a deployed service or a claim that these local
files/identities exist. To run on the actual host, install the project's
optional `voice-local` dependencies, verify local model availability and
restrict the ARI endpoint to 127.0.0.1. Then use the repository's
`deploy/systemd/porcelainberryme-asterisk-stasis.service.example`
as an **uninstalled** template for one supervised worker. Exposing ARI
or the unencrypted Asterisk media WebSocket on a public interface is not
supported by this integration.

Production acceptance of an actual telephone service must still prove
the intended dialplan routing, SIP-trunk connectivity (when PSTN is
required), call audio in both directions, ring/hangup behavior, memory
capacity, privacy, and recovery on the actual Asterisk host.

The release gate, when invoked with `--network --require-pstn`, checks
the configured fixed Stasis route and uses authenticated
`GET /ari/asterisk/info` **and**
`GET /ari/applications/<configured-app>` on 127.0.0.1.
Passing those HTTP checks confirms the ARI API and Stasis application
are visible, **not** that a carrier or real call works.

## Further supervision, interruption and first-response safeguards

The default single-call Stasis listener now preflights and holds one
installed speech model set **before** subscribing to new Stasis events.
It reuses these resources across sequential calls rather than loading
Whisper during an incoming ring. Hosts explicitly setting
`PB_ASTERISK_MAX_CALLS` above one retain independent per-call models to
avoid mixing mutable model/voice state between concurrent conversations.
No actual model inference latency is measured by synthetic CI.

`SIGTERM` and `SIGINT` are handled inside the listener event loop.
The worker stops accepting new events, cancels owned call/overload tasks,
and executes existing ARI media cleanup before the process exits.
A returned caller can generate `StasisEnd` *before* the
`/continue` HTTP acknowledgement; the return operation is now protected
from cancelling itself during that handshake. Other call hangups still
cancel ongoing media immediately.

The PCM speaker treats any failed `FLUSH_MEDIA` operation as an unsafe
playback state. It refuses new audio after that failure, rather than
risking speech from an interrupted turn leaking into the next turn.

On overload the listener now runs at most four concurrent
authenticated ARI return operations and holds up to 32 additional
channels in a FIFO (configurable through
`PB_ASTERISK_MAX_BUSY_PENDING`, allowed 1..256). A queued caller that
hangs up is removed without a late `/continue`. Graceful shutdown drains
this queue after cancelling active voice sessions. Each return has a
three-second deadline, never an unbounded retry. The counter
`overload_exhausted` records events beyond this finite queue; the
operator must enforce an ingress/concurrent channel ceiling in Asterisk
so calls beyond that tested ceiling cannot remain in Stasis. No finite
in-process queue is a substitute for SIP admission control.
The existing Piper subprocess also drains its discarded buffered stdout
after being killed on interruption; this prevents a deadlocked
`Process.wait()` when an OS pipe is full.

These invariants are covered by explicit cancellation/backpressure,
StasisEnd-during-continue, SIGTERM and prewarmed-model regression tests.
Deployment still requires actual Asterisk, local model and real device
verification, not a CI-only release declaration.

The CI test `tests/test_asterisk_stasis_e2e.py` covers, with actual
loopback HTTP/WebSocket transport and synthetic installed-model
substitutes, the complete authorized
`StasisStart -> answer -> mixing bridge -> externalMedia -> PCM -> STT
-> LLM -> TTS -> MARK_MEDIA acknowledgement -> cleanup -> continue`
lifecycle. Further tests cover replay, overload, hangup and denial.
This is strong source-level evidence but cannot validate an external
SIP carrier or an actual Asterisk installation.
