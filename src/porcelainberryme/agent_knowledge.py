"""Approved, tenant-isolated company answers for independent PorcelainberryMe agents.

This is an intentionally conservative knowledge layer: an AI voice call may
state a business answer only when a human operator has approved that exact
question/answer revision. Unknown answers fail closed instead of asking an
LLM to fabricate company facts. No embeddings, vendor APIs or model weights.

Question collection is OFF by default and optional; calls/audio/transcripts
are never stored here. All administration is local CLI, not public HTTP.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sqlite3
import time
import unicodedata

ID = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")
MAX_QUESTION = 350
MAX_ANSWER = 500
MAX_UNKNOWN = 500
RETENTION_SECONDS = 14 * 86400
DEFAULT_UNKNOWN = "Dazu habe ich keine freigegebene Information. Bitte wende dich an das zuständige Team."
DEFAULT_GREETING = "Guten Tag. Wie kann ich Ihnen helfen?"


class KnowledgeError(ValueError):
    pass


def _id(value: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise KnowledgeError("invalid_tenant_or_assistant_id")
    return value


def _plain(value: str, limit: int, code: str) -> str:
    if not isinstance(value, str):
        raise KnowledgeError(code)
    value = value.strip()
    if not 1 <= len(value) <= limit or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise KnowledgeError(code)
    return value


def normalize_question(value: str) -> str:
    question = _plain(value, MAX_QUESTION, "invalid_question")
    question = unicodedata.normalize("NFKC", question).casefold()
    words = re.findall(r"[^\W_]+", question, flags=re.UNICODE)
    if not words:
        raise KnowledgeError("invalid_question")
    return " ".join(words)


@dataclass(frozen=True, slots=True)
class AgentProfile:
    tenant: str
    assistant: str
    greeting: str
    fallback: str
    retain_unknown: bool


@dataclass(frozen=True, slots=True)
class KnowledgeAnswer:
    response: str
    approved: bool
    knowledge_id: str | None = None


class KnowledgeStore:
    """SQLite KB with human review, revision-bound approval and tenant isolation."""

    def __init__(self, path: str, *, clock=None):
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise KnowledgeError("knowledge_database_requires_absolute_path")
        self.path = path
        self.clock = clock or time.time
        with closing(self._connect()) as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS pb_kb_agents (
                    tenant TEXT NOT NULL, assistant TEXT NOT NULL,
                    greeting TEXT NOT NULL DEFAULT 'Guten Tag. Wie kann ich Ihnen helfen?',
                    fallback TEXT NOT NULL, retain_unknown INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (tenant, assistant)
                );
                CREATE TABLE IF NOT EXISTS pb_kb_entries (
                    tenant TEXT NOT NULL, assistant TEXT NOT NULL,
                    id TEXT NOT NULL, question TEXT NOT NULL,
                    normalized TEXT NOT NULL, answer TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('DRAFT','APPROVED','DISABLED')),
                    revision INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    approved_at INTEGER,
                    PRIMARY KEY (tenant, assistant, id),
                    UNIQUE(tenant, assistant, normalized),
                    FOREIGN KEY (tenant, assistant) REFERENCES pb_kb_agents(tenant, assistant)
                );
                CREATE TABLE IF NOT EXISTS pb_kb_unknown (
                    tenant TEXT NOT NULL, assistant TEXT NOT NULL,
                    normalized TEXT NOT NULL, question TEXT NOT NULL,
                    first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
                    count INTEGER NOT NULL,
                    PRIMARY KEY (tenant, assistant, normalized)
                );
                CREATE INDEX IF NOT EXISTS pb_kb_unknown_retention
                  ON pb_kb_unknown(last_seen);
            """)
            # Backwards-compatible local SQLite migration for any operator
            # who created a profile before greeting configuration existed.
            columns = {row["name"] for row in con.execute("PRAGMA table_info(pb_kb_agents)")}
            if "greeting" not in columns:
                con.execute(
                    "ALTER TABLE pb_kb_agents ADD COLUMN greeting TEXT NOT NULL "
                    "DEFAULT 'Guten Tag. Wie kann ich Ihnen helfen?'"
                )

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def _profile(self, con, tenant, assistant):
        profile = con.execute(
            "SELECT * FROM pb_kb_agents WHERE tenant=? AND assistant=?",
            (tenant, assistant),
        ).fetchone()
        if profile is None:
            raise KnowledgeError("agent_knowledge_profile_missing")
        return profile

    def configure(self, tenant: str, assistant: str, *, greeting: str = DEFAULT_GREETING,
                  fallback: str = DEFAULT_UNKNOWN,
                  retain_unknown: bool = False) -> AgentProfile:
        _id(tenant)
        _id(assistant)
        greeting = _plain(greeting, MAX_ANSWER, "invalid_greeting")
        fallback = _plain(fallback, MAX_ANSWER, "invalid_fallback")
        if type(retain_unknown) is not bool:
            raise KnowledgeError("invalid_unknown_retention_policy")
        with closing(self._connect()) as con:
            with con:
                con.execute(
                    "INSERT INTO pb_kb_agents(tenant,assistant,greeting,fallback,retain_unknown) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(tenant,assistant) DO UPDATE SET "
                    "greeting=excluded.greeting,fallback=excluded.fallback,"
                    "retain_unknown=excluded.retain_unknown",
                    (tenant, assistant, greeting, fallback, int(retain_unknown)),
                )
                # Turning retention OFF must actively purge previously stored questions.
                if not retain_unknown:
                    con.execute(
                        "DELETE FROM pb_kb_unknown WHERE tenant=? AND assistant=?",
                        (tenant, assistant),
                    )
        return AgentProfile(tenant, assistant, greeting, fallback, retain_unknown)

    def profile(self, tenant: str, assistant: str) -> AgentProfile:
        _id(tenant)
        _id(assistant)
        with closing(self._connect()) as con:
            p = self._profile(con, tenant, assistant)
            return AgentProfile(tenant, assistant, p["greeting"], p["fallback"],
                                bool(p["retain_unknown"]))

    def propose(self, tenant: str, assistant: str, *, key: str,
                question: str, answer: str) -> int:
        _id(tenant)
        _id(assistant)
        _id(key)
        question = _plain(question, MAX_QUESTION, "invalid_question")
        normalized = normalize_question(question)
        answer = _plain(answer, MAX_ANSWER, "invalid_answer")
        with closing(self._connect()) as con:
            with con:
                con.execute("BEGIN IMMEDIATE")
                self._profile(con, tenant, assistant)
                existing = con.execute(
                    "SELECT id,revision FROM pb_kb_entries "
                    "WHERE tenant=? AND assistant=? AND (id=? OR normalized=?)",
                    (tenant, assistant, key, normalized),
                ).fetchone()
                if existing is not None and existing["id"] != key:
                    raise KnowledgeError("question_already_exists_for_another_key")
                now = int(self.clock())
                if existing is None:
                    con.execute(
                        "INSERT INTO pb_kb_entries("
                        "tenant,assistant,id,question,normalized,answer,status,revision,updated_at) "
                        "VALUES(?,?,?,?,?,?,'DRAFT',1,?)",
                        (tenant, assistant, key, question, normalized, answer, now),
                    )
                    return 1
                revision = existing["revision"] + 1
                con.execute(
                    "UPDATE pb_kb_entries SET question=?,normalized=?,answer=?,"
                    "status='DRAFT',revision=?,updated_at=?,approved_at=NULL "
                    "WHERE tenant=? AND assistant=? AND id=?",
                    (question, normalized, answer, revision, now, tenant, assistant, key),
                )
                return revision

    def review(self, tenant: str, assistant: str, *, key: str,
               expected_revision: int, approve: bool) -> bool:
        _id(tenant)
        _id(assistant)
        _id(key)
        if type(expected_revision) is not int or expected_revision < 1:
            raise KnowledgeError("expected_revision_required")
        if type(approve) is not bool:
            raise KnowledgeError("invalid_review_decision")
        with closing(self._connect()) as con:
            with con:
                con.execute("BEGIN IMMEDIATE")
                self._profile(con, tenant, assistant)
                updated = con.execute(
                    "UPDATE pb_kb_entries SET status=?,approved_at=? "
                    "WHERE tenant=? AND assistant=? AND id=? "
                    "AND status='DRAFT' AND revision=?",
                    ("APPROVED" if approve else "DISABLED",
                     int(self.clock()) if approve else None,
                     tenant, assistant, key, expected_revision),
                )
                return updated.rowcount == 1

    def disable(self, tenant: str, assistant: str, key: str) -> bool:
        _id(tenant)
        _id(assistant)
        _id(key)
        with closing(self._connect()) as con:
            with con:
                updated = con.execute(
                    "UPDATE pb_kb_entries SET status='DISABLED',approved_at=NULL "
                    "WHERE tenant=? AND assistant=? AND id=? AND status='APPROVED'",
                    (tenant, assistant, key),
                )
                return updated.rowcount == 1

    def entries(self, tenant: str, assistant: str, status: str | None = None) -> list[dict]:
        _id(tenant)
        _id(assistant)
        if status is not None and status not in ("DRAFT", "APPROVED", "DISABLED"):
            raise KnowledgeError("invalid_knowledge_status")
        with closing(self._connect()) as con:
            self._profile(con, tenant, assistant)
            if status:
                rows = con.execute(
                    "SELECT id,question,answer,status,revision FROM pb_kb_entries "
                    "WHERE tenant=? AND assistant=? AND status=? ORDER BY id LIMIT 1000",
                    (tenant, assistant, status),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT id,question,answer,status,revision FROM pb_kb_entries "
                    "WHERE tenant=? AND assistant=? ORDER BY id LIMIT 1000",
                    (tenant, assistant),
                ).fetchall()
            return [dict(x) for x in rows]

    def answer(self, tenant: str, assistant: str, question: str, *,
               record_unknown: bool = True) -> KnowledgeAnswer:
        _id(tenant)
        _id(assistant)
        normalized = normalize_question(question)
        with closing(self._connect()) as con:
            with con:
                p = self._profile(con, tenant, assistant)
                entry = con.execute(
                    "SELECT id,answer FROM pb_kb_entries "
                    "WHERE tenant=? AND assistant=? AND normalized=? AND status='APPROVED'",
                    (tenant, assistant, normalized),
                ).fetchone()
                if entry:
                    return KnowledgeAnswer(entry["answer"], True, entry["id"])
                if p["retain_unknown"] and record_unknown:
                    now = int(self.clock())
                    con.execute(
                        "DELETE FROM pb_kb_unknown WHERE tenant=? AND assistant=? "
                        "AND last_seen<?",
                        (tenant, assistant, now - RETENTION_SECONDS),
                    )
                    row = con.execute(
                        "SELECT 1 FROM pb_kb_unknown WHERE tenant=? AND assistant=? "
                        "AND normalized=?",
                        (tenant, assistant, normalized),
                    ).fetchone()
                    if row is not None:
                        con.execute(
                            "UPDATE pb_kb_unknown SET last_seen=?,count=count+1 "
                            "WHERE tenant=? AND assistant=? AND normalized=?",
                            (now, tenant, assistant, normalized),
                        )
                    else:
                        count = con.execute(
                            "SELECT COUNT(*) FROM pb_kb_unknown "
                            "WHERE tenant=? AND assistant=?",
                            (tenant, assistant),
                        ).fetchone()[0]
                        if count < MAX_UNKNOWN:
                            con.execute(
                                "INSERT INTO pb_kb_unknown VALUES(?,?,?,?,?,?,?)",
                                (tenant, assistant, normalized, question.strip(), now, now, 1),
                            )
                return KnowledgeAnswer(p["fallback"], False, None)

    def unanswered(self, tenant: str, assistant: str) -> list[dict]:
        _id(tenant)
        _id(assistant)
        with closing(self._connect()) as con:
            p = self._profile(con, tenant, assistant)
            if not p["retain_unknown"]:
                return []
            rows = con.execute(
                "SELECT question,count,first_seen,last_seen FROM pb_kb_unknown "
                "WHERE tenant=? AND assistant=? AND last_seen>=? "
                "ORDER BY last_seen DESC LIMIT 100",
                (tenant, assistant, int(self.clock()) - RETENTION_SECONDS),
            ).fetchall()
            return [dict(x) for x in rows]

    def purge_unanswered(self, tenant: str, assistant: str) -> int:
        _id(tenant)
        _id(assistant)
        with closing(self._connect()) as con:
            with con:
                self._profile(con, tenant, assistant)
                return con.execute(
                    "DELETE FROM pb_kb_unknown WHERE tenant=? AND assistant=?",
                    (tenant, assistant),
                ).rowcount


class ApprovedKnowledgeResponder:
    """Implements existing Responder protocol used by VoiceTurnEngine.

    Always speaks operator-approved exact FAQ answers or a configured safe
    fallback; this layer never invokes a paid service or ungrounded LLM.
    """

    def __init__(self, path: str, tenant: str, assistant: str):
        # Runtime must not create an empty database and then pretend company
        # knowledge was provisioned successfully.
        if not isinstance(path, str) or not Path(path).is_file():
            raise KnowledgeError("approved_knowledge_database_missing")
        self.store = KnowledgeStore(path)
        # Reviewed FAQ and documents share the same local SQLite identity,
        # tenant profile and approval boundary. No separate SaaS service.
        from .document_knowledge import DocumentKnowledgeStore
        self.documents = DocumentKnowledgeStore(path)
        self.tenant, self.assistant = _id(tenant), _id(assistant)
        self.greeting = self.store.profile(tenant, assistant).greeting
        # Missing profiles fail before an outbound call is even initiated.

    def _resolve(self, text: str) -> str:
        # A document match must not first be recorded as an unanswered FAQ.
        exact = self.store.answer(
            self.tenant, self.assistant, text, record_unknown=False,
        )
        if exact.approved:
            return exact.response
        hit = self.documents.find_document(self.tenant, self.assistant, text)
        if hit is not None:
            # Speak only verbatim human-approved text with named provenance.
            # No free-form LLM interpretation or factual extrapolation.
            return f"Laut Dokument {hit.title}: {hit.text}"
        return self.store.answer(self.tenant, self.assistant, text).response

    async def respond(self, text: str, history: list[dict]) -> str:
        return await asyncio.to_thread(self._resolve, text)


def main():
    parser = argparse.ArgumentParser(description="Local, approved per-tenant agent FAQ")
    parser.add_argument("--db", required=True, help="Absolute private SQLite path")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("configure", "propose", "approve", "reject", "disable",
                 "list", "unanswered", "purge-unanswered"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--tenant", required=True)
        cmd.add_argument("--assistant", required=True)
        if name == "configure":
            cmd.add_argument("--greeting", default=DEFAULT_GREETING)
            cmd.add_argument("--fallback", default=DEFAULT_UNKNOWN)
            cmd.add_argument("--retain-unknown", action="store_true")
        if name == "propose":
            cmd.add_argument("--key", required=True)
            cmd.add_argument("--question", required=True)
            cmd.add_argument("--answer", required=True)
        if name in ("approve", "reject"):
            cmd.add_argument("--key", required=True)
            cmd.add_argument("--revision", type=int, required=True)
        if name == "disable":
            cmd.add_argument("--key", required=True)
        if name == "list":
            cmd.add_argument("--status", choices=["DRAFT", "APPROVED", "DISABLED"])
    args = parser.parse_args()
    os.umask(0o077)
    store = KnowledgeStore(args.db)
    t, a = args.tenant, args.assistant
    if args.command == "configure":
        print(store.configure(t, a, greeting=args.greeting,
                              fallback=args.fallback,
                              retain_unknown=args.retain_unknown))
    elif args.command == "propose":
        print("DRAFT revision", store.propose(
            t, a, key=args.key, question=args.question, answer=args.answer,
        ))
    elif args.command in ("approve", "reject"):
        accepted = store.review(
            t, a, key=args.key, expected_revision=args.revision,
            approve=args.command == "approve",
        )
        if not accepted:
            parser.error("entry absent, modified or already reviewed")
        print(args.command, "confirmed")
    elif args.command == "disable":
        if not store.disable(t, a, args.key):
            parser.error("approved entry not found")
        print("disabled")
    elif args.command == "list":
        for item in store.entries(t, a, getattr(args, "status", None)):
            print(item)
    elif args.command == "unanswered":
        for item in store.unanswered(t, a):
            print(item)
    elif args.command == "purge-unanswered":
        print("deleted", store.purge_unanswered(t, a))


if __name__ == "__main__":
    main()
