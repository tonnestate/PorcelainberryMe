"""Local, consent-gated, minimal post-call notes for PorcelainberryMe.

No always-on recording, no transcription archive, no cloud AI summarizer.
The assistant only persists bounded excerpts of *completed user turns after
a clear affirmative in-call opt-in*. Revocation before hangup discards all
notes. Each session starts with consent OFF; no cross-call inheritance.

This is a narrowly bounded first post-call workflow, not a complete
enterprise call-summary or legally certified consent management service.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
import time

from .agent_knowledge import _id

RETENTION_SECONDS = 14 * 86400
MAX_NOTES = 500
MAX_EXCERPTS = 4
EXCERPT_CHARS = 100
MAX_NOTE_CHARS = 600
CALL_ID = re.compile(r"^[0-9a-f]{32}$")
CONSENT = "ich stimme der speicherung einer gesprächsnotiz zu"
REVOKE = "ich widerrufe die speicherung der gesprächsnotiz"


class NoteError(ValueError):
    pass


def normalize_spoken(text: str) -> str:
    if not isinstance(text, str):
        return ""
    return re.sub(
        r"\s+", " ", re.sub(r"[^\w\s]", " ", text.casefold()),
    ).strip()


def build_minimal_note(turns: list[dict]) -> tuple[str, int] | None:
    # No model-generated content and no claims of interpretation.
    excerpts: list[str] = []
    for item in turns:
        if item.get("role") != "user" or not isinstance(item.get("content"), str):
            continue
        text = " ".join(item["content"].strip().split())
        if not text or normalize_spoken(text) in (CONSENT, REVOKE):
            continue
        if text not in excerpts:
            excerpts.append(text[:EXCERPT_CHARS])
        if len(excerpts) >= MAX_EXCERPTS:
            break
    if not excerpts:
        return None
    note = "Gesprochene Kundenanliegen (gekürzte Originaläußerungen): " + " | ".join(excerpts)
    return note[:MAX_NOTE_CHARS], len(excerpts)


@dataclass(frozen=True)
class SavedNote:
    call_id: str
    tenant: str
    assistant: str
    caller: str
    text: str
    excerpts: int
    created_at: int


class CallNotesStore:
    def __init__(self, path: str, *, clock=None):
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise NoteError("absolute_private_note_db_required")
        self.path = path
        self.clock = clock or time.time
        with closing(self._conn()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS pb_call_notes (
                    call_id TEXT PRIMARY KEY,
                    tenant TEXT NOT NULL,
                    assistant TEXT NOT NULL,
                    caller TEXT NOT NULL,
                    note TEXT NOT NULL,
                    excerpts INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS pb_call_notes_tenant
                  ON pb_call_notes(tenant,assistant,created_at);
            """)

    def _conn(self):
        con = sqlite3.connect(self.path, timeout=5)
        con.row_factory = sqlite3.Row
        return con

    def _prune(self, db):
        return db.execute(
            "DELETE FROM pb_call_notes WHERE created_at<?",
            (int(self.clock()) - RETENTION_SECONDS,),
        ).rowcount

    def prune_expired(self) -> int:
        """Trusted maintenance; may run hourly in the existing IP call server."""
        with closing(self._conn()) as db:
            with db:
                return self._prune(db)

    def save(self, *, call_id: str, tenant: str, assistant: str,
             caller: str, note: str, excerpts: int) -> bool:
        if not isinstance(call_id, str) or not CALL_ID.fullmatch(call_id):
            raise NoteError("invalid_call_id")
        _id(tenant)
        _id(assistant)
        _id(caller)
        if (not isinstance(note, str) or not 1 <= len(note) <= MAX_NOTE_CHARS
                or type(excerpts) is not int or not 1 <= excerpts <= MAX_EXCERPTS):
            raise NoteError("invalid_note_content")
        with closing(self._conn()) as db:
            db.execute("BEGIN IMMEDIATE")
            with db:
                self._prune(db)
                existing = db.execute(
                    "SELECT tenant,assistant,caller FROM pb_call_notes WHERE call_id=?",
                    (call_id,),
                ).fetchone()
                if existing:
                    if (existing["tenant"], existing["assistant"], existing["caller"]) != (
                        tenant, assistant, caller,
                    ):
                        raise NoteError("note_identity_conflict")
                    return False
                count = db.execute(
                    "SELECT COUNT(*) FROM pb_call_notes WHERE tenant=? AND assistant=?",
                    (tenant, assistant),
                ).fetchone()[0]
                if count >= MAX_NOTES:
                    # Idempotent replay was already handled above. Never
                    # evict another caller's data without an admin policy.
                    raise NoteError("tenant_call_note_limit_reached")
                db.execute(
                    "INSERT INTO pb_call_notes VALUES(?,?,?,?,?,?,?)",
                    (call_id, tenant, assistant, caller, note, excerpts,
                     int(self.clock())),
                )
                return True

    def list_notes(self, tenant: str, assistant: str,
                   *, caller: str | None = None) -> list[SavedNote]:
        _id(tenant)
        _id(assistant)
        if caller is not None:
            _id(caller)
        with closing(self._conn()) as db:
            with db:
                self._prune(db)
            if caller is None:
                rows = db.execute(
                    "SELECT * FROM pb_call_notes WHERE tenant=? AND assistant=? "
                    "ORDER BY created_at DESC LIMIT 100",
                    (tenant, assistant),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM pb_call_notes WHERE tenant=? AND assistant=? "
                    "AND caller=? ORDER BY created_at DESC LIMIT 100",
                    (tenant, assistant, caller),
                ).fetchall()
            return [
                SavedNote(r["call_id"], r["tenant"], r["assistant"],
                          r["caller"], r["note"], r["excerpts"], r["created_at"])
                for r in rows
            ]

    def delete_caller(self, tenant: str, assistant: str, caller: str) -> int:
        _id(tenant)
        _id(assistant)
        _id(caller)
        with closing(self._conn()) as db:
            with db:
                return db.execute(
                    "DELETE FROM pb_call_notes WHERE tenant=? AND assistant=? AND caller=?",
                    (tenant, assistant, caller),
                ).rowcount

    def delete_call(self, tenant: str, assistant: str, call_id: str) -> bool:
        _id(tenant)
        _id(assistant)
        if not isinstance(call_id, str) or not CALL_ID.fullmatch(call_id):
            raise NoteError("invalid_call_id")
        with closing(self._conn()) as db:
            with db:
                return db.execute(
                    "DELETE FROM pb_call_notes WHERE tenant=? AND assistant=? AND call_id=?",
                    (tenant, assistant, call_id),
                ).rowcount == 1


class ConsentedNotesResponder:
    """Existing Responder-protocol wrapper, not a second phone service."""

    def __init__(self, responder, store: CallNotesStore, *,
                 tenant: str, assistant: str, caller: str):
        self.responder = responder
        self.store = store
        self.tenant = _id(tenant)
        self.assistant = _id(assistant)
        self.caller = _id(caller)
        self.greeting = getattr(responder, "greeting", "Guten Tag. Wie kann ich Ihnen helfen?")
        self.consented = False
        self.start_index: int | None = None

    async def respond(self, text: str, history: list[dict]) -> str:
        command = normalize_spoken(text)
        if command == CONSENT:
            # Store ONLY subsequent turns, never those before the opt-in.
            self.consented = True
            self.start_index = len(history)
            return "Verstanden. Ich speichere nach diesem Gespräch eine kurze Notiz aus Ihren folgenden Anliegen."
        if command == REVOKE:
            self.consented = False
            self.start_index = None
            return "Verstanden. Ich speichere keine Gesprächsnotiz."
        return await self.responder.respond(text, history)

    async def persist_if_consented(self, call_id: str,
                                  history: list[dict]) -> bool:
        if not self.consented or self.start_index is None:
            return False
        minimal = build_minimal_note(history[self.start_index:])
        if minimal is None:
            return False
        text, number = minimal
        return await asyncio.to_thread(
            self.store.save, call_id=call_id,
            tenant=self.tenant, assistant=self.assistant, caller=self.caller,
            note=text, excerpts=number,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trusted local opt-in call notes, 14-day retention"
    )
    parser.add_argument("--db", required=True, help="Absolute private SQLite path")
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("list", "delete-caller", "delete-call", "prune-expired"):
        cmd = actions.add_parser(action)
        cmd.add_argument("--tenant", required=True)
        cmd.add_argument("--assistant", required=True)
        if action == "list":
            cmd.add_argument("--caller")
        if action == "delete-caller":
            cmd.add_argument("--caller", required=True)
        if action == "delete-call":
            cmd.add_argument("--call-id", required=True)
    args = parser.parse_args()
    store = CallNotesStore(args.db)
    if args.action == "prune-expired":
        print("expired_deleted", store.prune_expired())
    elif args.action == "list":
        for note in store.list_notes(args.tenant, args.assistant,
                                     caller=args.caller):
            print(json.dumps(note.__dict__, ensure_ascii=False))
    elif args.action == "delete-caller":
        print("deleted", store.delete_caller(
            args.tenant, args.assistant, args.caller,
        ))
    elif args.action == "delete-call":
        print("deleted", store.delete_call(
            args.tenant, args.assistant, args.call_id,
        ))


if __name__ == "__main__":
    main()
