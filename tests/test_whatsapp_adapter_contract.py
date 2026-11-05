"""TonnEstate/Aurora authenticated inbound event compatibility regression."""
from __future__ import annotations

import unittest

from porcelainberryme.tonnestate_whatsapp import from_existing_whatsapp_event


class ExistingWhatsAppAdapterContractTests(unittest.TestCase):
    def setUp(self):
        self.message = {
            "message_id": "original-001",
            "sender": "49151234567@c.us",
            "message": "1.234,56 € am 05.11.2026, 18:30 – สวัสดี",
            "conversation_id": "direct-provider-chat",
            "tenant_id": "untrusted-tenant",
            "recipient": "untrusted-owner",
        }

    def inbound(self, **changes):
        return from_existing_whatsapp_event(
            {**self.message, **changes},
            tenant_id="verified_tenant",
            owner_destination="verified_owner",
        )

    def test_missing_and_null_from_me_follow_existing_aurora_contract(self):
        for options in (
            {},
            {"from_me": False},
            {"from_me": None},
        ):
            with self.subTest(options=options):
                result = self.inbound(**options)
                self.assertEqual(result.channel, "whatsapp")
                self.assertEqual(result.external_id, "original-001")
                self.assertEqual(result.tenant_id, "verified_tenant")
                self.assertEqual(result.recipient, "verified_owner")
                self.assertEqual(
                    result.text, "1.234,56 € am 05.11.2026, 18:30 – สวัสดี",
                )

    def test_explicit_owner_message_and_groups_never_mirrored(self):
        with self.assertRaisesRegex(ValueError, "outgoing_whatsapp_message"):
            self.inbound(from_me=True)
        for sender in ("12345@g.us", "", "fake\n@c.us"):
            with self.subTest(sender=sender):
                with self.assertRaisesRegex(ValueError, "direct_whatsapp_sender"):
                    self.inbound(sender=sender)

    def test_payload_cannot_claim_tenant_or_owner(self):
        result = self.inbound(tenant_id="FORGED", recipient="FORGED_OWNER")
        self.assertEqual(result.tenant_id, "verified_tenant")
        self.assertEqual(result.recipient, "verified_owner")


if __name__ == "__main__":
    unittest.main()
