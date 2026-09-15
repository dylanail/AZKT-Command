"""Outbound email transport for owner reminders (spec §5.4). Transports: memory (tests), log (dev),
smtp (production via SMTP_*), gmail (send through the connected business Gmail when enabled).
Never sends to FORBIDDEN_RECIPIENTS. Returns a receipt or raises with a typed reason."""
from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid

from ..core.config import settings
from ..core.destinations import assert_destination_allowed
from ..core.errors import ProviderError, Unsupported
from ..services.forbidden_recipient import assert_allowed

log = logging.getLogger("azkt.email")


@dataclass
class OutboundEmail:
    to: list[str]
    subject: str
    text: str
    html: str | None = None
    headers: dict = field(default_factory=dict)
    from_addr: str | None = None


class MemoryTransport:
    sent: list[dict] = []

    async def send(self, msg: OutboundEmail) -> dict:
        mid = make_msgid(domain="azkt.test")
        rec = {"message_id": mid, "to": msg.to, "subject": msg.subject, "text": msg.text, "html": msg.html,
               "headers": msg.headers, "at": datetime.now(timezone.utc).isoformat()}
        MemoryTransport.sent.append(rec)
        return {"provider": "memory", "provider_ref": mid, "accepted": True}


class LogTransport:
    async def send(self, msg: OutboundEmail) -> dict:
        mid = make_msgid(domain="azkt.local")
        log.info("EMAIL (not sent; EMAIL_TRANSPORT=log) to=%s subject=%s id=%s", msg.to, msg.subject, mid)
        return {"provider": "log", "provider_ref": mid, "accepted": False, "note": "logged only; no transport configured"}


class SmtpTransport:
    async def send(self, msg: OutboundEmail) -> dict:
        if not settings.SMTP_HOST:
            raise Unsupported("SMTP_HOST not configured")
        em = EmailMessage()
        em["From"] = msg.from_addr or settings.REMINDER_FROM
        em["To"] = ", ".join(msg.to)
        em["Subject"] = msg.subject
        mid = make_msgid(domain=(settings.REMINDER_FROM.split("@")[-1].rstrip(">") if "@" in settings.REMINDER_FROM else "azkt.app"))
        em["Message-ID"] = mid
        for k, v in msg.headers.items():
            em[k] = v
        em.set_content(msg.text)
        if msg.html:
            em.add_alternative(msg.html, subtype="html")
        import asyncio
        def _send():
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30) as s:
                if settings.SMTP_STARTTLS:
                    s.starttls(context=ssl.create_default_context())
                if settings.SMTP_USERNAME:
                    s.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                s.send_message(em)
        try:
            await asyncio.to_thread(_send)
        except smtplib.SMTPResponseException as e:
            # a 4xx means the server refused this attempt and holds nothing: safe to try again.
            raise ProviderError(f"smtp error {e.smtp_code}: {e.smtp_error!r}",
                                kind="transient" if e.smtp_code >= 400 else "invalid_input",
                                retryable=e.smtp_code >= 400)
        except (smtplib.SMTPException, OSError) as e:
            raise ProviderError(f"smtp failure: {e}", kind="transient")
        return {"provider": "smtp", "provider_ref": mid, "accepted": True}


_transport = None


def transport():
    global _transport
    if _transport is None:
        kind = settings.EMAIL_TRANSPORT
        _transport = {"memory": MemoryTransport, "log": LogTransport, "smtp": SmtpTransport}.get(kind, LogTransport)()
    return _transport


def reset_transport() -> None:
    global _transport
    _transport = None
    MemoryTransport.sent.clear()


async def send(msg: OutboundEmail) -> dict:
    assert_allowed(*msg.to)
    if not msg.to:
        raise Unsupported("no recipient configured")
    t = transport()
    if not isinstance(t, (MemoryTransport, LogTransport)):
        # A delivering transport outside production may only reach allowlisted recipients (H08).
        assert_destination_allowed("email", *msg.to)
    return await t.send(msg)
