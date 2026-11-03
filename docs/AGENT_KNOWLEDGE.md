# Approved Knowledge for PorcelainberryMe Voice Agents

## Scope and implementation status

An independent, tenant-scoped, approved FAQ database is integrated into the
actual LiveKit agent voice path. This is a **first, strict business knowledge
mode**, not semantic RAG or the full self-learning Fonio knowledge system.

An operator must explicitly review each answer revision before it can be
spoken to a caller. The default unknown-answer behavior does not invoke
an LLM to invent company data. Neither a cloud API nor model download is
required for this functionality. No VPS deployment or live microphone test
has been completed. All tests are offline.

## Data model and safety boundaries

- Each knowledge profile belongs to one (tenant, assistant) pair. The worker
  loads only the profile matching the verified agent identity and tenant.
- Each FAQ has a stable operator-defined key, question, answer, status,
  revision, and optional approval time.
- Proposing or editing an entry sets DRAFT and invalidates the previous
  approval. An operator must approve exactly the newly returned revision.
- Only APPROVED entries are accessible to the runtime. Revocation changes
  the status to DISABLED immediately. No cross-tenant lookup fallback.
- Question matching is case-insensitive and Unicode-normalized, with
  punctuation ignored, but is **not** semantically fuzzy. No synonym
  inference, vector retrieval, generated citations, or fabricated answers.
  Operators can create additional approved question/answer entries for
  common verbal phrasings.
- Unknown questions are NOT recorded by default. An operator can explicitly
  enable a bounded unknown-question inbox **only where lawful consent and
  privacy notices are in place**. Stored unanswered text expires after
  fourteen days and can be purged on demand. This is not a call transcript
  store or a GDPR-complete data-subject deletion interface.
- No customer-facing HTTP administration is opened. Administrative
  operations are trusted local CLI operations. Ensure file permissions,
  secret management, user authorization and tenant isolation at the VPS
  boundary before exposing any management service.

## Local provisioning (existing host, no paid services)

Use a private absolute SQLite path in an existing secure directory:

    export PB_AGENT_KNOWLEDGE_DB='/private/existing/agents.sqlite3'

Create a profile with an independently configurable spoken greeting. Unless --retain-unknown is explicitly set, no unanswered
call questions are kept on disk:

    python -m porcelainberryme.agent_knowledge --db "$PB_AGENT_KNOWLEDGE_DB" configure --tenant company01 --assistant assistant01 --greeting 'Guten Tag. Sie sprechen mit dem digitalen Empfang.'

Submit a draft FAQ. The command returns its revision, initially 1:

    python -m porcelainberryme.agent_knowledge --db "$PB_AGENT_KNOWLEDGE_DB" propose --tenant company01 --assistant assistant01 --key opening --question 'Wann habt ihr geöffnet?' --answer 'Montag bis Freitag von neun bis siebzehn Uhr.'

A trusted operator confirms revision 1 after checking the response:

    python -m porcelainberryme.agent_knowledge --db "$PB_AGENT_KNOWLEDGE_DB" approve --tenant company01 --assistant assistant01 --key opening --revision 1

Corrections are applied with the propose command, which creates a new
DRAFT revision; the old approved content immediately stops being used.
An operator can revoke it without creating a replacement:

    python -m porcelainberryme.agent_knowledge --db "$PB_AGENT_KNOWLEDGE_DB" disable --tenant company01 --assistant assistant01 --key opening

Inspect entries and, if explicitly enabled, unknown questions:

    python -m porcelainberryme.agent_knowledge --db "$PB_AGENT_KNOWLEDGE_DB" list --tenant company01 --assistant assistant01
    python -m porcelainberryme.agent_knowledge --db "$PB_AGENT_KNOWLEDGE_DB" unanswered --tenant company01 --assistant assistant01

Permanent deletion of stored unknown questions for a profile:

    python -m porcelainberryme.agent_knowledge --db "$PB_AGENT_KNOWLEDGE_DB" purge-unanswered --tenant company01 --assistant assistant01

## How the real voice worker uses it

Configure the PB_AGENT_KNOWLEDGE_DB environment variable of the existing
PorcelainberryMe local voice worker. It validates that the private file
and verified (tenant, agent_id) profile actually exist before dialing.
The VoiceTurnEngine's responder is then an ApprovedKnowledgeResponder.
The worker synthesizes the approved profile greeting as real PCM when the
accepted call connects. If the recipient starts speaking during greeting
synthesis, the stale greeting is dropped; barge-in clears outgoing audio.
Only approved exact FAQ answers or the configured safe fallback reach Piper
speech synthesis. There is no unapproved Ollama fallback when this mode
is active, even if an Ollama model is installed. The local STT/Piper and
WebRTC media prerequisites from LOCAL_VOICE_AGENT.md still apply.

**Important limitation:** Strict fact lookup is not a natural FAQ dialogue
system. Speech recognition punctuation and paraphrases can reduce exact
matches. This feature closes an authorization/knowledge gap but does not
prove customer-grade knowledge comprehension, semantic document search,
automatic learning, conditional greeting rules or a voice picker,
scheduling or call transfer.

## Tests and remaining release gates

- Human review with optimistic revision locks; editing invalidates approval.
- Tenant and assistant separation, revocation and missing-profile failures.
- Unknown-question opt-in, limited retention and explicit purge.
- Actual VoiceTurnEngine wiring to a reviewed answer (synthetic PCM input).
- Actual autonomous call_human runtime chooses the knowledge responder
  when PB_AGENT_KNOWLEDGE_DB is set and does not call Ollama for unknown facts.

Next required steps: real voice E2E and latency measurement, semantic
retrieval with source/provenance and rejection thresholds, tenant onboarding
and admin UX, action-tool authorization, calendar integration, human
handoff and privacy/consent audit.
