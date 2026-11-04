"""Read-only MCP projection of verified inbound events already stored locally.

No direct WhatsApp API, second consumer, login, or contact history scraping.
The trusted existing n8n/Aurora ingress must write an owner-scoped ledger.
"""
from __future__ import annotations

from contextlib import closing
import json
import re
import sqlite3
from urllib.parse import quote

from .contracts import InboundEvent
from .whatsapp_mcp import private_file


class OwnerWhatsAppInbox:
    def __init__(self, *, database: str, tenant: str,
                 owner_destination: str):
        if (not isinstance(tenant, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", tenant)
                or not isinstance(owner_destination, str)
                or not owner_destination.strip()
                or len(owner_destination) > 256):
            raise ValueError("whatsapp_owner_inbox_scope_invalid")
        self.database = private_file(database)
        self.tenant = tenant
        self.owner_destination = owner_destination

    def recent(self, *, after: int = 0, limit: int = 20) -> dict:
        if type(after) is not int or not 0 <= after < (1 << 63):
            raise ValueError("whatsapp_inbox_cursor_invalid")
        if type(limit) is not int or not 1 <= limit <= 30:
            raise ValueError("whatsapp_inbox_limit_invalid")
        # An existing file may have been replaced; never follow a symlink.
        private_file(self.database)
        uri = "file:" + quote(self.database, safe="/") + "?mode=ro"
        mirror_source = False
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
                db.execute("PRAGMA query_only=ON")
                mirror_source = db.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='whatsapp_owner_mirror'"
                ).fetchone() is not None
                if mirror_source:
                    source_table = "whatsapp_owner_mirror"
                    key_field = "seq"
                    scope = "tenant=? AND owner_destination=?"
                    parameters = (self.tenant, self.owner_destination)
                else:
                    source_table = "inbound"
                    key_field = "rowid"
                    scope = "tenant_id=? AND channel='whatsapp'"
                    parameters = (self.tenant,)
                # Table names and column names are fixed internal literals.
                if after:
                    rows = db.execute(
                        f"SELECT {key_field},payload FROM {source_table} "
                        f"WHERE {scope} AND {key_field}>? "
                        f"ORDER BY {key_field} ASC LIMIT 1000",
                        (*parameters, after),
                    ).fetchall()
                else:
                    rows = db.execute(
                        f"SELECT {key_field},payload FROM {source_table} "
                        f"WHERE {scope} ORDER BY {key_field} DESC LIMIT 1000",
                        parameters,
                    ).fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError("whatsapp_inbox_unavailable") from exc
        matches = []
        cursor = after
        for rowid, raw in rows:
            cursor = max(cursor, rowid)
            try:
                event = InboundEvent.from_dict(json.loads(raw))
            except (ValueError, TypeError):
                raise RuntimeError("whatsapp_inbox_event_invalid") from None
            if (event.tenant_id != self.tenant or event.channel != "whatsapp"
                    or event.kind != "text"
                    or event.recipient != self.owner_destination):
                continue
            matches.append({
                "cursor": rowid,
                "external_id": event.external_id,
                "sender": event.sender,
                "conversation_ref": event.conversation_ref,
                "text": event.text[:4000],
            })
            if len(matches) >= limit:
                break
        matches.sort(key=lambda row: row["cursor"])
        return {
            "schema": "porcelainberryme.mcp.whatsapp-inbox.v1",
            "source": ("existing_authenticated_owner_mirror" if mirror_source
                       else "existing_authenticated_inbound_ledger"),
            "messages": matches,
            "next_cursor": cursor,
            "truncated": len(matches) >= limit or len(rows) == 1000,
        }
