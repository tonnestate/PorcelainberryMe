# Hermes Agent → PorcelainberryMe: controlled MCP Internet voice

**Source implementation exists; deployment not performed.** The Hermes Agent
docs confirm stdio MCP servers under `~/.hermes/config.yaml`, an `env`
mapping for subprocesses, explicit `tools.include` allowlists, and
`/reload-mcp` / `hermes mcp test <name>`. Hermes itself is not installed
or copied into PorcelainberryMe, avoiding a second cognitive core.

Upstream documentation:
- https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/mcp.md
- https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/voice-mode.md

## Example (template only, never commit actual credentials)

A trusted owner/operator generates a **short-lived, single-use** Hermes MCP
process configuration for an individually authorized call. The following
is a fragment to merge under `~/.hermes/config.yaml` on the existing
FruitStack host, not a complete deployable file:

```yaml
mcp_servers:
  porcelainberryme_owner_voice:
    command: "/ABSOLUTE/PATH/TO/EXISTING/PYTHON"
    args: ["-m", "porcelainberryme.mcp_server"]
    enabled: true
    supports_parallel_tool_calls: false
    timeout: 120
    env:
      PB_MCP_CALL_TOKEN: "HOST_ISSUED_AGENT_BEARER_DO_NOT_COMMIT"
      PB_MCP_TENANT: "AUTHENTICATED_TENANT"
      PB_MCP_SUBJECT: "ENROLLED_AGENT_ID"
      PB_MCP_ROLE: "agent"
      PB_MCP_CALL_ME_ENABLED: "1"
      PB_MCP_CALL_ME_TARGET: "ENROLLED_OPTED_IN_RECIPIENT"
      PB_MCP_CALL_ME_REQUEST_ID: "UNIQUE_APPROVED_REQUEST_0001"
      PB_LOCAL_WHISPER_DIR: "/EXISTING/WHISPER"
      PB_LOCAL_OLLAMA_MODEL: "EXISTING_OLLAMA_MODEL"
      PB_LOCAL_VOICE_CHAT_MODEL: "EXISTING_FAST_OLLAMA_MODEL"
      PB_LOCAL_PIPER_BIN: "/EXISTING/PIPER"
      PB_LOCAL_PIPER_MODEL: "/EXISTING/PIPER_VOICE.onnx"
    tools:
      include:
        - porcelainberryme_call_me
        - porcelainberryme_call_me_status
        - porcelainberryme_call_status
        - porcelainberryme_active_calls
      resources: false
      prompts: false
```

**Do not reuse the above illustrative request ID in production.** The host
must generate a new idempotency key for a separately approved future call,
and create a fresh supervised process/configuration. The present MCP tool
does not dial E.164/PSTN numbers, cannot override the fixed target and
does not claim the invite was accepted. The actual outbound process invokes
PorcelainberryMe's existing `call_human` and returns an honest job state.

The Hermes configuration file contains sensitive secrets if an agent
bearer is included; only the service-user account may read it. Prefer the
existing restricted host secret manager or per-instance supervisor-managed
secret injection. Do not paste a real token into chat or source control.

The active MCP process must remain connected throughout the call. Hermes
supports per-server idle/lifetime recycling; **do not enable aggressive
recycling** for an active voice call, or it may terminate mid-conversation.
One process is scoped to exactly one authorized request and target.
Run `hermes mcp test porcelainberryme_owner_voice` and inspect tool
visibility after loading configuration. Installed models, WebRTC host and
target registration must independently pass acceptance.

For a permanently available "call me whenever I explicitly request it"
feature, a trusted host-controlled authorization queue and durable
outbound job supervisor are still required. The one-request-per-process
version deliberately does not let a model invent infinite outbound jobs.
