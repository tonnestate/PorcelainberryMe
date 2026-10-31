"""Standard-library SMTP transport and MIME-to-InboundEvent converter.

Email sending is opt-in through Gateway.authorize. This module does not poll
mailboxes, replace TonnEstate mail-desk, or own CRM identities.
"""
from __future__ import annotations

from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import make_msgid, parseaddr
import smtplib
import ssl

from .contracts import InboundEvent, OutboundMessage
from .service import DeliveryRejected


def _safe_email(value: str) -> str:
    if not isinstance(value, str) or "\r" in value or "\n" in value:
        raise ValueError("invalid_email")
    _, address = parseaddr(value)
    if not address or address != value.strip() or "@" not in address:
        raise ValueError("invalid_email")
    return address


class SMTPTransport:
    def __init__(self, *, host: str, port: int, sender: str, username: str,
                 password: str, implicit_tls: bool = False):
        if not host or not 1 <= port <= 65535 or not username or not password:
            raise ValueError("smtp_configuration_incomplete")
        self.host, self.port = host, port
        self.sender = _safe_email(sender)
        self.username, self.password = username, password
        self.implicit_tls = implicit_tls

    def send(self, message: OutboundMessage) -> str:
        if message.channel != "email":
            raise ValueError("wrong_transport")
        if _safe_email(message.sender) != self.sender:
            raise PermissionError("smtp_sender_mismatch")
        recipient = _safe_email(message.recipient)
        mail = EmailMessage()
        mail["From"] = self.sender
        mail["To"] = recipient
        mail["Subject"] = "Nachricht"
        mail["Message-ID"] = make_msgid()
        mail.set_content(message.text)
        context = ssl.create_default_context()
        try:
            if self.implicit_tls:
                smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=15, context=context)
            else:
                smtp = smtplib.SMTP(self.host, self.port, timeout=15)
            with smtp:
                if not self.implicit_tls:
                    smtp.ehlo()
                    smtp.starttls(context=context)
                    smtp.ehlo()
                smtp.login(self.username, self.password)
                refused = smtp.send_message(mail)
                if refused:
                    raise DeliveryRejected("smtp_recipient_refused")
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
            raise DeliveryRejected("smtp_address_refused") from exc
        # SMTP success acknowledges relay acceptance, not recipient delivery.
        return str(mail["Message-ID"])


def parse_inbound_email(raw: bytes, *, tenant_id: str, mailbox: str) -> InboundEvent:
    if not isinstance(raw, bytes) or not raw or len(raw) > 256 * 1024:
        raise ValueError("invalid_email_size")
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    if parsed.get("Auto-Submitted", "no").lower() != "no":
        raise ValueError("automated_email_ignored")
    sender = _safe_email(str(parsed.get("From", "")))
    external_id = str(parsed.get("Message-ID", "")).strip()
    if not external_id or len(external_id) > 256:
        raise ValueError("email_message_id_required")
    part = parsed.get_body(preferencelist=("plain",))
    if part is None:
        raise ValueError("email_plain_text_required")
    content = part.get_content()
    if not isinstance(content, str):
        raise ValueError("invalid_email_body")
    return InboundEvent(
        tenant_id=tenant_id, channel="email", external_id=external_id,
        sender=sender, recipient=_safe_email(mailbox),
        text=content.strip(),
        conversation_ref=str(parsed.get("In-Reply-To", ""))[:512],
    )
