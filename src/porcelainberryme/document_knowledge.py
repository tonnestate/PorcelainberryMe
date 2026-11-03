"""Approved plain-text business documents for the independent voice assistant.

Strict lexical retrieval of *verbatim reviewed paragraphs*: no model-generated
business claims, no scraper, embedding server, cloud AI or auto-download.
Documents are scoped by (tenant,assistant); a revision is usable only after
a trusted operator explicitly approves that same revision.

A document hit requires >=2 nontrivial question terms, >=80% coverage and
an unambiguous best paragraph. This is NOT semantic RAG.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata

from .agent_knowledge import KnowledgeError, KnowledgeStore, _id, _plain

MAX_DOCUMENT_BYTES = 64 * 1024
MAX_DOCUMENT_CHARS = 40000
MAX_CHUNK = 350
MAX_CHUNKS = 200
MAX_TITLE = 100
STOP = frozenset({
    "der", "die", "das", "des", "dem", "den", "ein", "eine", "einer", "eines",
    "einem", "einen", "und", "oder", "mit", "von", "für", "bei", "auf",
    "in", "im", "ins", "am", "an", "aus", "zu", "zum", "zur", "über",
    "was", "wie", "wo", "wer", "wen", "wann", "welche", "welcher",
    "welches", "sind", "ist", "hat", "haben", "kann", "können",
    "ich", "du", "ihr", "wir", "sie", "mir", "mich", "uns",
    "bitte", "möchte", "wissen", "erfahren", "gibt", "es",
    "lautet", "bedeutet", "eigentlich", "genau", "dazu", "jetzt",
    "mein", "meine", "dein", "unser", "unsere",
})


def terms(text: str) -> frozenset[str]:
    cleaned = unicodedata.normalize("NFKC", text).casefold()
    return frozenset(
        term for term in re.findall(r"[^\W_]+", cleaned, flags=re.UNICODE)
        if len(term) >= 4 and term not in STOP and not term.isdecimal()
    )


def split_reviewable(text: str) -> list[str]:
    if not isinstance(text, str) or not 1 <= len(text) <= MAX_DOCUMENT_CHARS:
        raise KnowledgeError("invalid_document_size")
    if any(ord(ch) == 0 for ch in text):
        raise KnowledgeError("invalid_document_binary_data")
    # Preserve sentence boundaries; never truncate verified content silently.
    paragraphs = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    chunks: list[str] = []
    for paragraph in paragraphs:
        paragraph = " ".join(paragraph.strip().split())
        if not paragraph:
            continue
        if len(paragraph) <= MAX_CHUNK:
            chunks.append(paragraph)
            continue
        sentences = re.split(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9])", paragraph)
        buf = ""
        for sentence in sentences:
            if len(sentence) > MAX_CHUNK:
                # Do not divide an approved sentence arbitrarily: its
                # isolated fragment could lose essential qualifications.
                raise KnowledgeError("document_sentence_too_long")
            if buf and len(buf) + 1 + len(sentence) > MAX_CHUNK:
                chunks.append(buf)
                buf = sentence
            else:
                buf = (buf + " " + sentence).strip()
        if buf:
            chunks.append(buf)
    if not chunks or len(chunks) > MAX_CHUNKS:
        raise KnowledgeError("document_chunk_count_out_of_bounds")
    return chunks


@dataclass(frozen=True)
class DocumentHit:
    text: str
    title: str
    document_id: str
    revision: int
    coverage: float


class DocumentKnowledgeStore(KnowledgeStore):
    def __init__(self, path: str, *, clock=None):
        super().__init__(path, clock=clock)
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS pb_kb_documents (
                    tenant TEXT NOT NULL, assistant TEXT NOT NULL,
                    id TEXT NOT NULL, title TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL
                      CHECK(status IN ('DRAFT','APPROVED','DISABLED')),
                    updated_at INTEGER NOT NULL,
                    approved_at INTEGER,
                    PRIMARY KEY(tenant,assistant,id),
                    FOREIGN KEY(tenant,assistant)
                      REFERENCES pb_kb_agents(tenant,assistant)
                );
                CREATE TABLE IF NOT EXISTS pb_kb_document_chunks (
                    tenant TEXT NOT NULL, assistant TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL, text TEXT NOT NULL,
                    PRIMARY KEY(tenant,assistant,document_id,ordinal),
                    FOREIGN KEY(tenant,assistant,document_id)
                      REFERENCES pb_kb_documents(tenant,assistant,id)
                      ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS pb_kb_document_approved
                  ON pb_kb_documents(tenant,assistant,status);
            """)

    def propose_document(self, tenant: str, assistant: str, *, key: str,
                         title: str, text: str) -> int:
        _id(tenant)
        _id(assistant)
        _id(key)
        title = _plain(title, MAX_TITLE, "invalid_document_title")
        chunks = split_reviewable(text)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            with db:
                self._profile(db, tenant, assistant)
                old = db.execute(
                    "SELECT revision FROM pb_kb_documents "
                    "WHERE tenant=? AND assistant=? AND id=?",
                    (tenant, assistant, key),
                ).fetchone()
                rev = old["revision"] + 1 if old else 1
                now = int(self.clock())
                if old:
                    db.execute(
                        "UPDATE pb_kb_documents SET title=?,revision=?,"
                        "status='DRAFT',updated_at=?,approved_at=NULL "
                        "WHERE tenant=? AND assistant=? AND id=?",
                        (title, rev, now, tenant, assistant, key),
                    )
                    db.execute(
                        "DELETE FROM pb_kb_document_chunks "
                        "WHERE tenant=? AND assistant=? AND document_id=?",
                        (tenant, assistant, key),
                    )
                else:
                    db.execute(
                        "INSERT INTO pb_kb_documents VALUES(?,?,?,?,?,'DRAFT',?,NULL)",
                        (tenant, assistant, key, title, rev, now),
                    )
                db.executemany(
                    "INSERT INTO pb_kb_document_chunks VALUES(?,?,?,?,?)",
                    [(tenant, assistant, key, i, chunk)
                     for i, chunk in enumerate(chunks)],
                )
                return rev

    def review_document(self, tenant: str, assistant: str, *, key: str,
                        revision: int, approve: bool) -> bool:
        _id(tenant)
        _id(assistant)
        _id(key)
        if type(revision) is not int or revision <= 0 or type(approve) is not bool:
            raise KnowledgeError("invalid_document_review")
        with closing(self._connect()) as db:
            with db:
                result = db.execute(
                    "UPDATE pb_kb_documents SET status=?,approved_at=? "
                    "WHERE tenant=? AND assistant=? AND id=? "
                    "AND revision=? AND status='DRAFT'",
                    ("APPROVED" if approve else "DISABLED",
                     int(self.clock()) if approve else None,
                     tenant, assistant, key, revision),
                )
                return result.rowcount == 1

    def disable_document(self, tenant: str, assistant: str, key: str) -> bool:
        _id(tenant)
        _id(assistant)
        _id(key)
        with closing(self._connect()) as db:
            with db:
                result = db.execute(
                    "UPDATE pb_kb_documents SET status='DISABLED',approved_at=NULL "
                    "WHERE tenant=? AND assistant=? AND id=? "
                    "AND status='APPROVED'", (tenant, assistant, key),
                )
                return result.rowcount == 1

    def list_documents(self, tenant: str, assistant: str) -> list[dict]:
        _id(tenant)
        _id(assistant)
        with closing(self._connect()) as db:
            self._profile(db, tenant, assistant)
            return [dict(r) for r in db.execute(
                "SELECT id,title,revision,status FROM pb_kb_documents "
                "WHERE tenant=? AND assistant=? ORDER BY id LIMIT 1000",
                (tenant, assistant),
            )]

    def find_document(self, tenant: str, assistant: str,
                      question: str) -> DocumentHit | None:
        _id(tenant)
        _id(assistant)
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 350:
            raise KnowledgeError("invalid_question")
        tokens = terms(question)
        if len(tokens) < 2:
            return None
        with closing(self._connect()) as db:
            self._profile(db, tenant, assistant)
            rows = db.execute(
                "SELECT d.id,d.title,d.revision,c.text "
                "FROM pb_kb_documents d JOIN pb_kb_document_chunks c "
                "ON c.tenant=d.tenant AND c.assistant=d.assistant "
                "AND c.document_id=d.id "
                "WHERE d.tenant=? AND d.assistant=? AND d.status='APPROVED' "
                "ORDER BY d.id,c.ordinal LIMIT 3000",
                (tenant, assistant),
            )
            candidates = []
            for row in rows:
                hits = tokens & terms(row["text"])
                coverage = len(hits) / len(tokens)
                if len(hits) >= 2 and coverage >= 0.8:
                    candidates.append((coverage, len(hits), row))
            if not candidates:
                return None
            candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
            best = candidates[0]
            # Same-strength contradictory paragraphs are ambiguous; do not
            # choose one arbitrarily or manufacture an answer.
            if len(candidates) > 1 and best[:2] == candidates[1][:2]:
                return None
            row = best[2]
            return DocumentHit(
                row["text"], row["title"], row["id"], row["revision"], best[0],
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Local reviewable business documents; no paid service"
    )
    parser.add_argument("--db", required=True, help="Absolute existing knowledge database")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("propose", "approve", "reject", "disable", "list"):
        cmd = sub.add_parser(action)
        cmd.add_argument("--tenant", required=True)
        cmd.add_argument("--assistant", required=True)
        if action in ("propose", "approve", "reject", "disable"):
            cmd.add_argument("--key", required=True)
        if action == "propose":
            cmd.add_argument("--title", required=True)
            cmd.add_argument("--file", required=True,
                             help="Existing UTF-8 .txt or .md, no URL downloads")
        if action in ("approve", "reject"):
            cmd.add_argument("--revision", required=True, type=int)
    args = parser.parse_args()
    store = DocumentKnowledgeStore(args.db)
    if args.action == "propose":
        file = Path(args.file)
        if file.suffix.lower() not in (".txt", ".md"):
            parser.error("only_reviewable_plain_text_or_markdown_supported")
        if not file.is_file() or file.stat().st_size > MAX_DOCUMENT_BYTES:
            parser.error("file_not_found_or_too_large")
        try:
            content = file.read_text(encoding="utf-8")
        except (UnicodeError, OSError):
            parser.error("invalid_utf8_document")
        print("DRAFT revision", store.propose_document(
            args.tenant, args.assistant,
            key=args.key, title=args.title, text=content,
        ))
    elif args.action in ("approve", "reject"):
        if not store.review_document(
            args.tenant, args.assistant, key=args.key,
            revision=args.revision, approve=args.action == "approve",
        ):
            parser.error("document_missing_modified_or_already_reviewed")
        print(args.action, "confirmed")
    elif args.action == "disable":
        if not store.disable_document(args.tenant, args.assistant, args.key):
            parser.error("document_not_approved")
        print("disabled")
    elif args.action == "list":
        for item in store.list_documents(args.tenant, args.assistant):
            print(item)


if __name__ == "__main__":
    main()
