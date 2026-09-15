"""The private Telegram Manager channel (spec §5.5).

Authorization is the immutable paired `telegram_user_id` + private `chat_id` — never a username, never a
forwarded "owner" message, never a group. Inbound updates are stored durably first
(`events.record_provider_event`) and processed asynchronously by the `telegram.process_update` job, so a
duplicate update has no second effect. Consequential work (approvals, customer sends, publications) is
never executed from chat: the bot replies with the authenticated review deep link.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from itsdangerous import BadSignature, Signer
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters.telegram import TelegramClient, deep_link
from ..core.config import settings
from ..core.errors import Blocked, Denied, DomainError, NotFound, ProviderError, Unsupported, ValidationFailed
from ..core.ids import new_id, sha256_hex, token as new_token
from ..core.time import PHOENIX, ensure_aware, fmt_local
from ..domain import jobs
from ..domain.actors import Actor
from ..domain.commands import CommandContext, REGISTRY, command, dispatch
from ..domain.policy import effective_perms
from ..models import User
from ..models.comms import ProviderEvent
from ..models.notify import TelegramPairing
from ..models.runtime import Approval, ChatTurn
from ..models.tasks import Task
from . import approvals as approvals_svc
from . import connections

log = logging.getLogger("azkt.telegram")

PAIRING_TTL = timedelta(minutes=10)
MAX_OUT = 1200
SALT = "azkt-telegram-callback"
AFFIRMATIONS = {"yes", "y", "yes please", "ok", "okay", "sure", "do it", "go ahead", "approve", "approved",
                "send it", "confirm", "yep", "yes do it", "please do"}


# ── client ──────────────────────────────────────────────────────────────────
_clients: dict[str, TelegramClient] = {}


def client(token: str | None = None) -> TelegramClient:
    tok = token or settings.TELEGRAM_BOT_TOKEN
    if tok not in _clients:
        _clients[tok] = TelegramClient(tok)
    return _clients[tok]


def reset_client() -> None:
    _clients.clear()


def recorded(token: str | None = None) -> list[dict]:
    """Messages the recording test transport captured (TelegramClient(token='test'))."""
    return client(token)._recorded


async def bot_token(db: AsyncSession) -> str:
    """Connection secret first, then the environment variable (never logged)."""
    try:
        conn = await connections.get(db, "telegram")
        if conn is not None:
            secret = connections.get_secret(conn)
            if secret.get("bot_token"):
                return secret["bot_token"]
    except Exception:  # noqa: BLE001
        pass
    return settings.TELEGRAM_BOT_TOKEN


# ── signed callback payloads ────────────────────────────────────────────────
class _ShortSigner(Signer):
    """A Telegram `callback_data` value is at most 64 bytes, so the HMAC is truncated consistently on
    both sides (still a keyed signature over the exact payload)."""

    def get_signature(self, value):  # type: ignore[override]
        return super().get_signature(value)[:16]


def _signer() -> _ShortSigner:
    return _ShortSigner(settings.SESSION_SECRET, salt=SALT, sep=b".")


def _compact(entity_id: str) -> str:
    try:
        return uuid.UUID(entity_id).hex
    except (ValueError, AttributeError, TypeError):
        return entity_id


def _expand(compact: str) -> str:
    if len(compact) == 32:
        try:
            return str(uuid.UUID(compact))
        except ValueError:
            return compact
    return compact


def sign_callback(action: str, task_id: str, revision: int, nonce: str | None = None) -> str:
    """{action, task_id, revision, nonce} signed with SESSION_SECRET."""
    nonce = nonce or new_id()[:4]
    raw = f"{action}:{_compact(task_id)}:{int(revision)}:{nonce}"
    return _signer().sign(raw.encode()).decode()


def verify_callback(data: str | None) -> dict | None:
    if not data:
        return None
    try:
        raw = _signer().unsign(data.encode()).decode()
    except (BadSignature, UnicodeDecodeError):
        return None
    parts = raw.split(":")
    if len(parts) != 4:
        return None
    action, tid, rev, nonce = parts
    if action not in ("o", "s", "d"):
        return None
    try:
        revision = int(rev)
    except ValueError:
        return None
    return {"action": action, "task_id": _expand(tid), "revision": revision, "nonce": nonce}


# ── pairing lookups ─────────────────────────────────────────────────────────
async def active_pairing(db: AsyncSession, user_id: str) -> TelegramPairing | None:
    return (await db.execute(select(TelegramPairing).where(
        TelegramPairing.user_id == user_id, TelegramPairing.status == "active",
        TelegramPairing.revoked_at.is_(None)).order_by(TelegramPairing.created_at.desc()))).scalars().first()


async def pairing_for_update(db: AsyncSession, telegram_user_id: int | None, chat_id: int | None) -> TelegramPairing | None:
    """Only an active pairing's immutable telegram_user_id **and** private chat_id authorize anything."""
    if telegram_user_id is None or chat_id is None:
        return None
    return (await db.execute(select(TelegramPairing).where(
        TelegramPairing.status == "active", TelegramPairing.revoked_at.is_(None),
        TelegramPairing.telegram_user_id == int(telegram_user_id),
        TelegramPairing.chat_id == int(chat_id)))).scalars().first()


async def serialize_pairing(db: AsyncSession, user_id: str) -> dict:
    rows = (await db.execute(select(TelegramPairing).where(TelegramPairing.user_id == user_id)
                             .order_by(TelegramPairing.created_at.desc()).limit(5))).scalars().all()
    def one(p: TelegramPairing) -> dict:
        return {"id": p.id, "status": p.status, "telegram_user_id": p.telegram_user_id, "chat_id": p.chat_id,
                "username": p.username_snapshot, "version": p.version,
                "confirmed_in_chat_at": p.confirmed_in_chat_at.isoformat() if p.confirmed_in_chat_at else None,
                "confirmed_in_app_at": p.confirmed_in_app_at.isoformat() if p.confirmed_in_app_at else None,
                "token_expires_at": p.token_expires_at.isoformat() if p.token_expires_at else None,
                "revoked_at": p.revoked_at.isoformat() if p.revoked_at else None, "revoke_reason": p.revoke_reason,
                "delivery_failures": p.delivery_failures, "blocked_at": p.blocked_at.isoformat() if p.blocked_at else None,
                "context": p.context or {}, "last_error": p.last_error}
    active = next((p for p in rows if p.status == "active"), None)
    return {"active": one(active) if active else None, "history": [one(p) for p in rows],
            "bot_username": settings.TELEGRAM_BOT_USERNAME or None,
            "webhook_secret_configured": bool(settings.TELEGRAM_WEBHOOK_SECRET)}


# ── outbound ────────────────────────────────────────────────────────────────
def bound(text: str, limit: int = MAX_OUT) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


async def send(db: AsyncSession, pairing: TelegramPairing, text: str, *, reply_markup: dict | None = None) -> dict:
    tok = await bot_token(db)
    if not tok:
        raise Unsupported("TELEGRAM_BOT_TOKEN not configured")
    res = await client(tok).send_message(pairing.chat_id, bound(text), reply_markup=reply_markup,
                                         disable_preview=True)
    pairing.last_outbound_at = datetime.now(timezone.utc)
    pairing.delivery_failures = 0
    pairing.last_error = None
    try:
        conn = await connections.get(db, "telegram", create=True)
        await connections.mark_success(db, conn)
    except Exception:  # noqa: BLE001
        pass
    return res


async def record_failure(db: AsyncSession, pairing: TelegramPairing | None, kind: str, message: str) -> None:
    if pairing is not None:
        pairing.delivery_failures = (pairing.delivery_failures or 0) + 1
        pairing.last_error = f"{kind}: {message}"[:500]
        if kind == "blocked":
            pairing.blocked_at = datetime.now(timezone.utc)
    try:
        conn = await connections.get(db, "telegram", create=True)
        await connections.mark_failure(db, conn, "auth_expired" if kind in ("auth_expired", "blocked") else kind,
                                       f"Telegram {kind}: {message}")
    except Exception:  # noqa: BLE001
        log.exception("could not record the telegram connection failure")


def compose_reminder(ctx: dict) -> str:
    """Concise, bounded, link at the end; previews are disabled by the client."""
    from . import email_templates as et
    tz = ctx.get("timezone") or PHOENIX
    kind = ctx.get("kind")
    lines: list[str] = []
    if kind == "overdue":
        lines.append(f"Overdue · {ctx.get('title', 'Task')}")
        lines.append(f"Was due {et.day_phrase(ctx.get('due_at'), tz, ctx.get('now'))} · {et.clock(ctx.get('due_at'), tz)}")
    elif kind == "digest":
        lines.append("Today")
        for item in (ctx.get("overdue") or [])[:5]:
            lines.append(f"• Overdue — {item.get('text', '')}")
        for item in (ctx.get("today") or [])[:5]:
            lines.append(f"• {item.get('time', '')} {item.get('text', '')}")
        for item in (ctx.get("approvals") or [])[:5]:
            lines.append(f"• Approval — {item.get('text', '')}")
    elif kind == "deposit_confirmed":
        lines.append(f"Deposit paid · {ctx.get('person', 'the buyer')}")
        lines.append(str(ctx.get("what") or ""))
        if ctx.get("owner") and ctx.get("amount"):
            lines.append(f"{et.money(ctx.get('amount'), ctx.get('currency'))}"
                         + (f" · {ctx['receipt_ref']}" if ctx.get("receipt_ref") else " · no provider receipt recorded"))
        lines.append(f"Next: {ctx.get('next_step', '')}")
    elif kind == "connection_issue":
        lines.append(str(ctx.get("title") or "A connection needs attention"))
        lines.append(str(ctx.get("body") or ""))
    else:
        lines.append(str(ctx.get("title") or "Task"))
        lead = et.lead_phrase(ctx.get("due_at"), ctx.get("now") or datetime.now(timezone.utc))
        lines.append(f"{et.day_phrase(ctx.get('due_at'), tz, ctx.get('now'))} · {et.clock(ctx.get('due_at'), tz)}"
                     + (f" · {lead}" if lead else ""))
        for label, key in (("Pipeline", "pipeline"), ("Vehicle", "vehicle"), ("Contact", "contact")):
            if ctx.get(key):
                lines.append(f"{label}: {ctx[key]}")
    if ctx.get("late"):
        lines.append("(late — delayed by a service interruption)")
    if ctx.get("link"):
        lines.append(str(ctx["link"]))
    return bound("\n".join(l for l in lines if l))


def reminder_keyboard(task: Task) -> dict:
    from . import email_templates as et
    rev = task.schedule_revision or 1
    return {"inline_keyboard": [[
        {"text": "Open", "url": et.deep_link("task", task.id)},
        {"text": "Snooze 1h", "callback_data": sign_callback("s", task.id, rev)},
        {"text": "Done", "callback_data": sign_callback("d", task.id, rev)},
    ]]}


# ── commands (pairing lifecycle) ────────────────────────────────────────────
class StartPairingIn(BaseModel):
    label: str | None = None


@command("telegram.start_pairing", input=StartPairingIn, perm="connections", action_class="owner_only",
         description="Create a one-use, 10-minute Telegram start token and the deep link that binds the private chat.")
async def start_pairing(ctx: CommandContext, inp: StartPairingIn) -> dict:
    if ctx.actor.kind != "user" or not ctx.actor.user_id:
        raise Denied("pairing starts from an authenticated Settings session")
    # one pending pairing at a time: a new link invalidates the previous, unused one
    pending = (await ctx.db.execute(select(TelegramPairing).where(
        TelegramPairing.user_id == ctx.actor.user_id, TelegramPairing.status == "pending"))).scalars().all()
    for p in pending:
        p.status = "expired"
        p.start_token_hash = None
        p.bump(ctx.actor.user_id)
    raw = new_token(24)
    p = TelegramPairing(user_id=ctx.actor.user_id, status="pending", start_token_hash=sha256_hex(raw),
                        token_expires_at=ctx.now + PAIRING_TTL, context={}, created_by=ctx.actor.user_id)
    ctx.db.add(p)
    await ctx.db.flush()
    ctx.changed.append({"kind": "telegram_pairing", "id": p.id, "version": p.version})
    ctx.record("Telegram pairing started", entity_kind="telegram_pairing", entity_id=p.id, kind="system",
               state="pending", visibility="owner")
    ctx.emit("telegram.pairing_changed", aggregate_type="telegram_pairing", aggregate_id=p.id,
             payload={"status": "pending", "user_id": p.user_id})
    link = deep_link(raw)
    return {"pairing_id": p.id, "status": "pending", "expires_at": p.token_expires_at.isoformat(),
            "deep_link": link or None, "start_token": raw,
            "setup_blocked": None if link else "TELEGRAM_BOT_USERNAME is not configured; the deep link cannot be built"}


class PairingRefIn(BaseModel):
    pairing_id: str
    expected_version: int | None = None
    reason: str | None = None


@command("telegram.confirm_pairing", input=PairingRefIn, perm="connections", action_class="owner_only",
         description="Owner confirms, in the web app, the Telegram identity that used the start link.")
async def confirm_pairing(ctx: CommandContext, inp: PairingRefIn) -> dict:
    p = (await ctx.db.execute(select(TelegramPairing).where(TelegramPairing.id == inp.pairing_id)
                              .with_for_update())).scalar_one_or_none()
    if p is None:
        raise NotFound("pairing not found")
    if p.user_id != ctx.actor.user_id:
        raise Denied("you can only confirm your own Telegram pairing")
    if inp.expected_version is not None and p.version != inp.expected_version:
        raise DomainError("pairing changed since you loaded it", code="conflict")
    if p.status != "pending":
        raise Blocked(f"pairing is {p.status}")
    if not p.telegram_user_id or not p.chat_id:
        raise Blocked("no Telegram account has used the link yet")
    for other in (await ctx.db.execute(select(TelegramPairing).where(
            TelegramPairing.user_id == p.user_id, TelegramPairing.status == "active"))).scalars().all():
        other.status = "revoked"
        other.revoked_at = ctx.now
        other.revoke_reason = "replaced by a newly confirmed pairing"
        other.bump(ctx.actor.user_id)
    p.status = "active"
    p.confirmed_in_app_at = ctx.now
    ctx.touch(p, "telegram_pairing")
    ctx.record(f"Telegram pairing confirmed: {p.username_snapshot or p.telegram_user_id}",
               entity_kind="telegram_pairing", entity_id=p.id, kind="system", state="active", visibility="owner",
               details={"telegram_user_id": p.telegram_user_id, "chat_id": p.chat_id})
    ctx.emit("telegram.pairing_changed", aggregate_type="telegram_pairing", aggregate_id=p.id,
             payload={"status": "active", "user_id": p.user_id})
    return {"pairing": {"id": p.id, "status": p.status, "telegram_user_id": p.telegram_user_id,
                        "chat_id": p.chat_id, "username": p.username_snapshot, "version": p.version}}


@command("telegram.revoke_pairing", input=PairingRefIn, perm="connections", action_class="owner_only",
         description="End Telegram access immediately: pending callbacks stop working and the chat is told.")
async def revoke_pairing(ctx: CommandContext, inp: PairingRefIn) -> dict:
    p = (await ctx.db.execute(select(TelegramPairing).where(TelegramPairing.id == inp.pairing_id)
                              .with_for_update())).scalar_one_or_none()
    if p is None:
        raise NotFound("pairing not found")
    if p.user_id != ctx.actor.user_id and ctx.actor.role != "owner":
        raise Denied("not your pairing")
    if p.status == "revoked":
        return {"pairing": {"id": p.id, "status": p.status}, "already": True}
    chat_id, was_active = p.chat_id, p.status == "active"
    p.status = "revoked"
    p.revoked_at = ctx.now
    p.revoke_reason = inp.reason or "revoked by the owner"
    p.start_token_hash = None
    ctx.touch(p, "telegram_pairing")
    ctx.record("Telegram pairing revoked", entity_kind="telegram_pairing", entity_id=p.id, kind="system",
               state="revoked", visibility="owner", details={"chat_id": chat_id})
    ctx.emit("telegram.pairing_changed", aggregate_type="telegram_pairing", aggregate_id=p.id,
             payload={"status": "revoked", "user_id": p.user_id, "chat_id": chat_id, "notify": was_active})
    return {"pairing": {"id": p.id, "status": p.status, "revoked_at": p.revoked_at.isoformat()}}


@on_event_placeholder := None  # noqa: E999
