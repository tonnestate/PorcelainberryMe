"""Interactive owner-only WhatsApp consent for the existing FruitStack host.

Run locally by an authenticated operator with access to the private owner
SQLite file. NOT an MCP tool, HTTP endpoint, or model-executable command.
"""
from __future__ import annotations

import argparse
import os
import sys

from .whatsapp_consent import OwnerWhatsAppDrafts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Review and approve one exact personal WhatsApp draft",
    )
    parser.add_argument("action", choices=("review", "approve", "reject", "prune"))
    parser.add_argument("draft_id", nargs="?")
    args = parser.parse_args()

    if os.environ.get("PB_MCP_WHATSAPP_ENABLED") != "1":
        parser.error("whatsapp_owner_consent_not_enabled")
    tenant = os.environ.get("PB_MCP_WHATSAPP_OWNER_TENANT", "")
    agent = os.environ.get("PB_MCP_WHATSAPP_OWNER_AGENT", "")
    sender = os.environ.get("PB_MCP_WHATSAPP_OWNER_SENDER", "")
    database = os.environ.get("PB_MCP_WHATSAPP_APPROVALS_DB", "")
    store = OwnerWhatsAppDrafts(
        database, tenant=tenant, agent=agent, sender=sender,
    )
    if args.action == "prune":
        if args.draft_id:
            parser.error("prune_does_not_accept_draft_id")
        print("pruned", store.prune())
        print("pruned send approvals", store.approvals.prune_terminal(
            tenant=tenant, agent=agent, sender=sender,
        ))
        return

    if args.draft_id is None:
        parser.error("draft_id_required")

    if args.action in ("approve", "reject") and (
            not sys.stdin.isatty() or not sys.stdout.isatty()):
        parser.error("interactive_authenticated_owner_terminal_required")

    if args.action == "review":
        # Requires local filesystem permission, but revealing message text to
        # unattended logs is forbidden: review only in an operator terminal.
        if not sys.stdout.isatty():
            parser.error("private_review_terminal_required")

    review = store.review(args.draft_id)
    print("Draft ID:", review["draft_id"])
    print("Recipient:", review["recipient"])
    print("Message (exact UTF-8):")
    print(review["text"])
    print("Content fingerprint:", review["fingerprint"])
    print("Expires at (Unix seconds):", review["expires_at"])

    if args.action == "review":
        return
    if args.action == "reject":
        phrase = "REJECT " + args.draft_id
        if input("Type " + phrase + " to reject: ").strip() != phrase:
            parser.error("whatsapp_explicit_rejection_missing")
        print("state:", store.revoke(args.draft_id)["state"])
        return

    phrase = "APPROVE " + args.draft_id
    if input("Type " + phrase + " to authorize this exact send: ").strip() != phrase:
        parser.error("whatsapp_explicit_owner_consent_missing")
    state = store.confirm(
        draft_id=args.draft_id,
        verified_recipient=review["recipient"],
        verified_text=review["text"],
    )
    print("state:", state["state"])
    print("Draft ID to send from authorized MCP:", args.draft_id)


if __name__ == "__main__":
    main()
