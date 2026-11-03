# Reviewed business document knowledge

## Product behavior

PorcelainberryMe can now read **operator-approved .txt/.md business documents**
from the same private SQLite database as the existing approved FAQ entries.
Source text is split into short, complete reviewable paragraphs; an operator
must approve each document revision before any part may be spoken in a call.
Editing immediately withdraws the previous approval. The former exact FAQ
answer takes precedence when both an FAQ and a document are relevant.

Retrieval is **conservative lexical paragraph matching**, NOT embeddings,
semantic search, or Fonio's advertised self-learning knowledge base.
It requires at least two nontrivial query words, 80% coverage of query
terms and one unambiguous best paragraph. Missing or competing matches
fail closed to the configured safe FAQ fallback. The voice worker speaks
a verbatim reviewed paragraph prefixed with its document title; it never
asks an LLM to write factual assertions about business documents.
Only already-approved data belonging to the verified tenant and assistant
is used; no cross-customer fallback or automatic web crawling.

## Local operator setup

First create the agent knowledge profile with the existing command from
docs/AGENT_KNOWLEDGE.md. No new SaaS server or background process is required.
Provide an absolute, private SQLite path as PB_AGENT_KNOWLEDGE_DB in the
existing voice worker configuration. The document store is initialized
in the same database; approved document retrieval is used automatically.

Example local operator commands (not public HTTP):

    python -m porcelainberryme.document_knowledge --db "$PB_AGENT_KNOWLEDGE_DB" propose --tenant company01 --assistant assistant01 --key customer_terms --title 'Kundenbedingungen' --file /private/review/customer_terms.md

Review the local source text AND the generated reviewable passages before approval.
Only a checked revision may become available:

    python -m porcelainberryme.document_knowledge --db "$PB_AGENT_KNOWLEDGE_DB" approve --tenant company01 --assistant assistant01 --key customer_terms --revision 1

Later editing increments the revision and automatically changes the status
back to DRAFT. A disabled document stops supplying answers immediately:

    python -m porcelainberryme.document_knowledge --db "$PB_AGENT_KNOWLEDGE_DB" disable --tenant company01 --assistant assistant01 --key customer_terms

    python -m porcelainberryme.document_knowledge --db "$PB_AGENT_KNOWLEDGE_DB" list --tenant company01 --assistant assistant01

Input constraints: local UTF-8 text or Markdown only, <=64 KiB on disk,
<=40,000 Unicode characters, at most 200 reviewable paragraphs and maximum
350 characters per paragraph/sentence. Sentences that cannot be safely
split are rejected rather than arbitrarily truncated. Text is never
downloaded from a third-party URL. PDFs, website ingestion and OCR
are NOT implemented by this milestone.

## Verified tests

Offline tests exercise document approval and revision locks, revocation,
tenant isolation, duplicate/conflicting content, ambiguous retrieval,
unknown-question recording, traceable answers, persistence and actual
ApprovedKnowledgeResponder integration used by the voice worker.
Source-level CI is not a live WebRTC listening acceptance. Lexical matching
will miss paraphrases; it can also match overlapping wording incorrectly.
Production use requires corpus-specific evaluations, adversarial tests,
approved privacy and retention policies and a customer-facing review UI.

## Remaining Fonio parity gaps

Smart website ingestion, PDF/Office extraction, semantic retrieval with
supporting citations, automated answer suggestions with human review,
adaptive dialog, real-time external CRM actions, actual SIP/PSTN, low-latency
natural speech, phone-style iOS ringing and verified call post-processing.
See docs/FONIO_PARITY.md.
