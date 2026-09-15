"""The private Telegram Manager channel (spec §5.5).

Authorization is the immutable paired `telegram_user_id` + private `chat_id` — never a username, never a
forwarded "owner" message, never a group. Inbound updates are stored durably first
(`events.record_provider_event`) and processed asynchronously by the `telegram.process_update` job, so a
duplicate update has no second effect. Consequential work (approvals, customer sends, publications) is
never executed from chat: the bot replies with the authenticated review deep link.
"""
from __future__ import annotations

import hmac
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from itsdangerous import BadSignature, Signer
from itsdangerous.encoding import want_bytes
from pydantic import BaseModel
from sqlalchemy import func, select, update
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
    """A Telegram `callback_data` value is at most 64 bytes, so the base64 HMAC is truncated to 16
    characters (96 bits) and compared in constant time against the same truncation. It is still a
    keyed signature over the exact payload — an unsigned or edited payload never verifies."""

    def get_signature(self, value):  # type: ignore[override]
        return super().get_signature(value)[:16]

    def verify_signature(self, value, sig):  # type: ignore[override]
        return hmac.compare_digest(want_bytes(sig), self.get_signature(value))


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
    elif ctx.get("snoozed"):
        lines.append("(snoozed reminder — the meeting time has not changed)")
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



# ── final message on revoke (outbox, so it never blocks the command transaction) ──
from ..domain.events import on_event  # noqa: E402


@on_event("telegram.pairing_changed")
async def _pairing_changed(db: AsyncSession, ev) -> None:
    p = dict(ev.payload or {})
    if p.get("status") != "revoked" or not p.get("notify") or not p.get("chat_id"):
        return
    tok = await bot_token(db)
    if not tok:
        return
    try:
        await client(tok).send_message(int(p["chat_id"]),
                                       "This chat is no longer linked to AZKT. Buttons from earlier messages "
                                       "will not work. Pair again from Settings if you need it back.")
    except (ProviderError, Unsupported) as e:  # a revoked chat that blocks us is not an error worth retrying
        log.info("could not send the final Telegram message: %s", e)


# ── webhook registration (owner-only, executed as a fenced external action) ──
class SetWebhookIn(BaseModel):
    url: str | None = None


@command("telegram.set_webhook", input=SetWebhookIn, perm="connections", action_class="owner_only",
         description="Register (or rotate) the Telegram webhook with the configured secret header.")
async def set_webhook(ctx: CommandContext, inp: SetWebhookIn) -> dict:
    if not settings.TELEGRAM_WEBHOOK_SECRET:
        raise Blocked("setup_blocked: TELEGRAM_WEBHOOK_SECRET is not configured; unauthenticated updates are refused")
    url = inp.url or f"{(settings.api_base or '').rstrip('/')}/api/telegram/webhook"
    if not url.startswith("https://"):
        raise ValidationFailed("the Telegram webhook URL must be https")
    act = await approvals_svc.intend_external_action(
        ctx, command_name="telegram.set_webhook", payload={"url": url}, provider="telegram",
        dedupe_key=f"telegram:setwebhook:{sha256_hex(url + settings.TELEGRAM_WEBHOOK_SECRET)[:16]}",
        entity_kind="connection", entity_id=None)
    return {"queued": True, "external_action_id": act.id, "url": url}


@approvals_svc.executor("telegram.set_webhook")
async def _exec_set_webhook(db: AsyncSession, action) -> dict:
    tok = await bot_token(db)
    if not tok:
        return {"sent": False, "handed_off": True, "error": "setup_blocked: no bot token configured"}
    res = await client(tok).set_webhook(action.payload["url"], settings.TELEGRAM_WEBHOOK_SECRET)
    conn = await connections.get(db, "telegram", create=True)
    await connections.mark_success(db, conn)
    return {"provider": "telegram", "provider_ref": action.payload["url"], "ok": bool(res.get("ok"))}


# ── inbound: durable store first, then this job ─────────────────────────────
@jobs.job("telegram.process_update")
async def process_update(jctx: jobs.JobContext, payload: dict) -> dict:
    db = jctx.db
    ev = await db.get(ProviderEvent, payload.get("provider_event_id"))
    if ev is None:
        return {"skipped": "provider event missing"}
    if ev.processed_at is not None:
        return {"skipped": "already processed"}          # duplicate update -> one intended effect (D03)
    replies: list[tuple] = []          # (chat_id, text, reply_markup[, chat_turn_id])
    answers: list[tuple[str, str]] = []
    try:
        note = await _handle_update(db, dict(ev.payload or {}), replies, answers)
        ev.error = None
    except DomainError as e:
        note = f"refused: {e.message}"
        ev.error = e.message[:2000]
    ev.processed_at = datetime.now(timezone.utc)
    await db.commit()
    # outbound only after the record change committed: no message claims an effect that did not happen
    tok = await bot_token(db)
    sent, receipts = 0, []
    for item in replies:
        chat_id, text, markup = item[0], item[1], item[2]
        turn_id = item[3] if len(item) > 3 else None
        if not tok:
            break
        try:
            res = await client(tok).send_message(chat_id, bound(text), reply_markup=markup, disable_preview=True)
            sent += 1
            mid = ((res or {}).get("result") or {}).get("message_id")
            if turn_id and mid:
                receipts.append((turn_id, int(mid)))
        except Exception as e:  # noqa: BLE001  - the record change is already committed; never re-run it
            log.info("telegram reply failed: %s", e)
    for cq_id, text in answers:
        if not tok:
            break
        try:
            await client(tok).answer_callback(cq_id, text)
        except Exception as e:  # noqa: BLE001
            log.info("telegram callback answer failed: %s", e)
    if receipts:
        # outbound message ids so the web and Telegram views cite the same message (spec §5.5)
        for turn_id, mid in receipts:
            await db.execute(update(ChatTurn).where(ChatTurn.id == turn_id).values(telegram_message_id=mid))
        await db.commit()
    return {"note": note, "replies": sent}


async def _actor_for(db: AsyncSession, user_id: str) -> Actor | None:
    u = await db.get(User, user_id)
    if u is None or u.status != "active":
        return None
    return Actor(kind="user", user_id=u.id, role=u.role, scope=u.scope,
                 perms=effective_perms(u.role, u.perms), display_name=u.display_name or u.handle)


def _ctx(db: AsyncSession, actor: Actor, *, request_id: str | None = None) -> CommandContext:
    return CommandContext(db=db, actor=actor, request_id=request_id, correlation_id=f"telegram:{new_id()[:8]}",
                          channel="telegram")


async def _handle_update(db: AsyncSession, update: dict, replies: list, answers: list) -> str:
    if "callback_query" in update:
        return await _handle_callback(db, update["callback_query"], replies, answers)
    msg = update.get("message") or update.get("edited_message")
    if not isinstance(msg, dict):
        return "ignored: unsupported update type"
    chat = msg.get("chat") or {}
    frm = msg.get("from") or {}
    if chat.get("type") != "private":
        return "ignored: not a private chat"          # groups never get business data (D02)
    tg_user_id, chat_id = frm.get("id"), chat.get("id")
    text = (msg.get("text") or msg.get("caption") or "").strip()

    if text.startswith("/start"):
        arg = text[len("/start"):].strip()
        return await _handle_start(db, arg, tg_user_id, chat_id, frm.get("username"), replies)

    pairing = await pairing_for_update(db, tg_user_id, chat_id)
    if pairing is None:
        # an unknown account, a matching username, or a revoked chat gets nothing at all
        return "ignored: no active pairing for this telegram user and chat"
    pairing.last_inbound_at = datetime.now(timezone.utc)
    actor = await _actor_for(db, pairing.user_id)
    if actor is None:
        return "ignored: paired person is not active"

    if msg.get("forward_origin") or msg.get("forward_from") or msg.get("forward_sender_name") or msg.get("forward_date"):
        await _store_turn(db, pairing, "user", text or "(forwarded content)", msg.get("message_id"))
        replies.append((chat_id, "Saved as a note. Forwarded content is information, not an instruction — "
                                 "tell me what you want done with it.", None))
        return "forwarded content stored as a note"

    if msg.get("voice") or msg.get("audio"):
        return await _handle_voice(db, pairing, actor, msg, replies)
    if msg.get("photo"):
        return await _handle_photos(db, pairing, actor, msg, replies)
    if not text:
        replies.append((chat_id, "I can read text, voice notes and photos here.", None))
        return "empty message"
    return await _handle_text(db, pairing, actor, text, msg, replies)


async def _handle_start(db: AsyncSession, arg: str, tg_user_id: int | None, chat_id: int | None,
                        username: str | None, replies: list) -> str:
    reject = ("That link is not valid any more. Open AZKT Settings › Telegram and generate a new one.")
    if not arg or tg_user_id is None or chat_id is None:
        if chat_id is not None:
            replies.append((chat_id, "Open AZKT Settings › Telegram to get your pairing link.", None))
        return "start without a token"
    p = (await db.execute(select(TelegramPairing).where(
        TelegramPairing.start_token_hash == sha256_hex(arg)).with_for_update())).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if p is None or p.status != "pending" or (p.token_expires_at and ensure_aware(p.token_expires_at) < now):
        # replay of a used token, an expired token, or another Telegram account trying it (D01)
        replies.append((chat_id, reject, None))
        return "start token rejected"
    if p.telegram_user_id and int(p.telegram_user_id) != int(tg_user_id):
        replies.append((chat_id, reject, None))
        return "start token already bound to another telegram account"
    p.telegram_user_id = int(tg_user_id)
    p.chat_id = int(chat_id)
    p.username_snapshot = username
    p.confirmed_in_chat_at = now
    p.token_used_at = now
    p.start_token_hash = None                      # one use only
    p.bump(p.user_id)
    replies.append((chat_id, "Thanks — this chat is linked to your AZKT account. Open AZKT Settings › Telegram and "
                             "confirm this account to finish. Until you confirm, I won't send or show anything.", None))
    return "start token bound; awaiting in-app confirmation"


# ── text commands ───────────────────────────────────────────────────────────
async def _handle_text(db: AsyncSession, pairing: TelegramPairing, actor: Actor, text: str, msg: dict,
                       replies: list) -> str:
    chat_id = pairing.chat_id
    await _store_turn(db, pairing, "user", text, msg.get("message_id"))
    low = text.lower().strip().rstrip("!.")
    cmd, _, rest = text.partition(" ")
    cmd = cmd.lower().strip()
    rest = rest.strip()

    if cmd in ("/help", "/commands"):
        out = ("I can: /tasks · /today · /done <task> · /snooze <minutes> · /vehicle <stock>. "
               "Approvals and customer sends always open the web review.")
    elif cmd == "/tasks":
        out = await _tasks_text(db, actor, scope="open")
    elif cmd == "/today":
        out = await _tasks_text(db, actor, scope="today")
    elif cmd == "/snooze":
        out = await _snooze_text(db, actor, rest, pairing)
    elif cmd == "/done":
        out = await _done_text(db, actor, rest, pairing)
    elif cmd == "/vehicle":
        out = await _vehicle_text(db, actor, rest, pairing)
    elif low in AFFIRMATIONS:
        out = await _unscoped_yes(db, actor)
    elif _needs_context(text) and not (pairing.context or {}).get("vehicle_id"):
        out = ("Which vehicle do you mean? Send /vehicle <stock number> first — I won't guess from an earlier "
               "conversation.")
    else:
        out = await _free_text(db, pairing, actor, text)
    turn = await _store_turn(db, pairing, "assistant", out, None)
    replies.append((chat_id, out, None, turn.id))
    return "text handled"


def _needs_context(text: str) -> bool:
    return bool(re.search(r"\b(this one|that one|it|the same one|same truck)\b", text, re.I))


async def _unscoped_yes(db: AsyncSession, actor: Actor) -> str:
    """An unscoped "yes" never approves anything (D05)."""
    from . import email_templates as et
    total = int(await db.scalar(select(func.count()).select_from(Approval).where(Approval.status == "pending")) or 0)
    rows = (await db.execute(select(Approval).where(Approval.status == "pending")
                             .order_by(Approval.created_at).limit(5))).scalars().all()
    if not rows:
        return "Nothing is waiting for approval right now."
    lines = [f"{total} item{'s' if total != 1 else ''} waiting. Open the review to approve — I can't approve "
             f"from chat:"]
    for a in rows:
        lines.append(f"• {a.title} → {et.origin()}{a.review_path or f'/approvals/{a.id}'}")
    if total > len(rows):
        lines.append(f"…and {total - len(rows)} more in Approvals.")
    return "\n".join(lines)


async def _tasks_text(db: AsyncSession, actor: Actor, scope: str) -> str:
    from . import reminders
    u = await db.get(User, actor.user_id)
    content = await reminders.digest_content(db, u)
    lines: list[str] = []
    for item in content["overdue"][:8]:
        lines.append(f"• Overdue — {item['text']}")
    if scope != "overdue":
        for item in content["today"][:8]:
            lines.append(f"• {item['time']} {item['text']}")
    if scope == "open":
        for item in content["tomorrow"][:5]:
            lines.append(f"• Tomorrow — {item['text']}")
    for item in content["approvals"][:5]:
        lines.append(f"• Waiting on you — {item['text']}")
    if not lines:
        return "Nothing scheduled and nothing overdue."
    return "\n".join(lines)


async def _find_task(db: AsyncSession, actor: Actor, ref: str) -> Task | None:
    ref = (ref or "").strip()
    if not ref:
        return None
    q = select(Task).where(Task.status.in_(("open", "in_progress", "blocked", "waiting")))
    if actor.role != "owner":
        q = q.where(Task.owner_user_id == actor.user_id)
    exact = (await db.execute(q.where(Task.id == ref))).scalars().first()
    if exact is not None:
        return exact
    rows = (await db.execute(q.order_by(Task.due_at.nulls_last()).limit(100))).scalars().all()
    ref_low = ref.lower()
    hits = [t for t in rows if t.id.startswith(ref) or ref_low in (t.title or "").lower()]
    return hits[0] if len(hits) == 1 else None


async def _done_text(db: AsyncSession, actor: Actor, ref: str, pairing: TelegramPairing) -> str:
    t = await _find_task(db, actor, ref)
    if t is None:
        return "I couldn't tell which task you mean. Send /tasks and use the exact title or id."
    try:
        res = await dispatch(_ctx(db, actor), "tasks.complete", {"task_id": t.id}, commit=False)
    except (Blocked, Denied, ValidationFailed) as e:
        return f"Not done: {e.message}"
    task = (res.data or {}).get("task") or {}
    return f"Marked done: {t.title} ({task.get('status', 'completed')})."


async def _snooze_text(db: AsyncSession, actor: Actor, rest: str, pairing: TelegramPairing) -> str:
    m = re.match(r"^(\d+)\s*(m|min|minutes|h|hour|hours)?\s*(.*)$", rest.strip(), re.I)
    if not m:
        return "Use /snooze <minutes> — for example /snooze 30."
    n = int(m.group(1))
    if (m.group(2) or "").lower().startswith("h"):
        n *= 60
    ref = (m.group(3) or "").strip()
    t = await _find_task(db, actor, ref) if ref else await _next_task(db, actor)
    if t is None:
        return "I couldn't tell which task to snooze. Send /tasks first."
    try:
        res = await dispatch(_ctx(db, actor), "tasks.snooze", {"task_id": t.id, "minutes": n}, commit=False)
    except (Blocked, Denied, ValidationFailed) as e:
        return f"Not snoozed: {e.message}"
    task = (res.data or {}).get("task") or {}
    return (f"Snoozed {n} min: {t.title}. The meeting time is unchanged "
            f"({fmt_local(ensure_aware(t.due_at), t.timezone or PHOENIX)}).")


async def _next_task(db: AsyncSession, actor: Actor) -> Task | None:
    q = select(Task).where(Task.status.in_(("open", "in_progress", "blocked", "waiting")), Task.due_at.is_not(None))
    if actor.role != "owner":
        q = q.where(Task.owner_user_id == actor.user_id)
    return (await db.execute(q.order_by(Task.due_at).limit(1))).scalars().first()


async def _vehicle_text(db: AsyncSession, actor: Actor, ref: str, pairing: TelegramPairing) -> str:
    from . import email_templates as et
    if not ref:
        ctxv = (pairing.context or {}).get("label")
        return f"Pinned vehicle: {ctxv}" if ctxv else "Send /vehicle <stock number>."
    try:
        from ..models.vehicles import Vehicle
        from .vehicles import vehicle_title
    except Exception:  # noqa: BLE001
        return "Vehicle records are not available here yet."
    rows = (await db.execute(select(Vehicle).where(func.lower(Vehicle.stock_no) == ref.lower()).limit(2))).scalars().all()
    if not rows:
        rows = (await db.execute(select(Vehicle).where(Vehicle.stock_no.ilike(f"%{ref}%")).limit(3))).scalars().all()
    if not rows:
        return f"No vehicle matches “{ref}”."
    if len(rows) > 1:
        return "Several vehicles match: " + ", ".join(v.stock_no or v.id[:8] for v in rows) + ". Use the exact stock number."
    v = rows[0]
    pairing.context = {"vehicle_id": v.id, "label": f"{vehicle_title(v)} · {v.stock_no}", "set_at":
                       datetime.now(timezone.utc).isoformat()}
    pairing.bump(pairing.user_id)
    return (f"{vehicle_title(v)} · {v.stock_no}\nRecon: {v.recon_state} · Logistics: {v.logistics_state}\n"
            f"{et.deep_link('vehicle', v.id, tab='overview')}\nPinned as “this one” for this chat.")


async def _free_text(db: AsyncSession, pairing: TelegramPairing, actor: Actor, text: str) -> str:
    """The Manager runtime handles free text when it exists; otherwise a deterministic status summary."""
    try:
        from ..agent import manager as manager_mod   # type: ignore
    except Exception:  # noqa: BLE001
        manager_mod = None
    handler = getattr(manager_mod, "handle_message", None) if manager_mod is not None else None
    if handler is not None:
        try:
            out = await handler(db, actor, text, channel="telegram", context=pairing.context or {})
            if isinstance(out, dict):
                out = out.get("text") or out.get("reply") or ""
            if out:
                return bound(str(out))
        except Exception:  # noqa: BLE001
            log.exception("manager.handle_message failed; falling back to the deterministic summary")
    summary = await _tasks_text(db, actor, scope="today")
    return ("I saved your message. The AI Manager isn't available right now, so here is the deterministic status:\n"
            + summary)


async def _store_turn(db: AsyncSession, pairing: TelegramPairing, role: str, content: str,
                      telegram_message_id: int | None) -> ChatTurn:
    turn = ChatTurn(thread_key=f"{pairing.user_id}:manager", role=role, content=(content or "")[:8000],
                    channel="telegram", telegram_message_id=telegram_message_id,
                    context=dict(pairing.context or {}), actor_user_id=pairing.user_id, agent_role="manager",
                    state="final", created_by=pairing.user_id)
    db.add(turn)
    await db.flush()
    return turn


# ── callbacks ───────────────────────────────────────────────────────────────
async def _handle_callback(db: AsyncSession, cq: dict, replies: list, answers: list) -> str:
    from . import email_templates as et
    cq_id = cq.get("id") or ""
    frm = cq.get("from") or {}
    message = cq.get("message") or {}
    chat = message.get("chat") or {}
    pairing = await pairing_for_update(db, frm.get("id"), chat.get("id"))
    if pairing is None:
        return "ignored: callback from an unpaired chat"     # revoked pairing invalidates pending callbacks (D06)
    parsed = verify_callback(cq.get("data"))
    if parsed is None:
        answers.append((cq_id, "This button is no longer valid."))
        return "callback signature rejected"
    actor = await _actor_for(db, pairing.user_id)
    if actor is None:
        return "ignored: paired person is not active"
    t = await db.get(Task, parsed["task_id"])
    if t is None:
        answers.append((cq_id, "That task no longer exists."))
        return "callback task missing"
    if (t.schedule_revision or 1) != parsed["revision"] or t.status in ("completed", "cancelled"):
        # a replayed or stale callback is harmless: it reports the current state and changes nothing (D03)
        answers.append((cq_id, f"Out of date — the task is now {t.status}. Open it to see the current version."))
        replies.append((pairing.chat_id, f"{t.title} is now {t.status}. {et.deep_link('task', t.id)}", None))
        return "callback stale; no effect"
    if parsed["action"] == "o":
        answers.append((cq_id, "Opening in AZKT."))
        replies.append((pairing.chat_id, et.deep_link("task", t.id), None))
        return "callback open"
    name, payload, label = ("tasks.complete", {"task_id": t.id}, "Marked done") if parsed["action"] == "d" else \
                           ("tasks.snooze", {"task_id": t.id, "minutes": 60}, "Snoozed 1 hour")
    try:
        await dispatch(_ctx(db, actor, request_id=f"tg-cb:{parsed['action']}:{t.id}:{parsed['revision']}"),
                       name, payload, commit=False)
    except (Blocked, Denied, ValidationFailed) as e:
        answers.append((cq_id, e.message[:180]))
        return f"callback refused: {e.message}"
    answers.append((cq_id, label))
    replies.append((pairing.chat_id, f"{label}: {t.title}", None))
    return f"callback {parsed['action']} applied"


# ── voice notes ─────────────────────────────────────────────────────────────
AMBIGUOUS_AMOUNT = re.compile(r"\b(about|around|roughly|maybe|approx\w*|~|or so|ish)\b[^.]{0,20}?\d", re.I)
BARE_MONEY = re.compile(r"\b\d{1,3}(?:[ ,]\d{3})+(?:\.\d+)?\b|\b\d+(?:\.\d+)?\s?(k|grand|thousand)\b", re.I)
DATE_NO_YEAR = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?!\s*,?\s*\d{4})\b|"
                          r"\b(the\s+)?\d{1,2}(st|nd|rd|th)\b|\b(next|this|last)\s+(week|month|monday|tuesday|wednesday|"
                          r"thursday|friday|saturday|sunday)\b", re.I)
FRAME_LIKE = re.compile(r"\b[A-Z][A-Z0-9]{2,6}-?\d{4,8}\b")


def ambiguity_flags(text: str) -> list[str]:
    """Deterministic checks. Uncertain amounts, dates without a year and frame-like tokens are confirmed
    with Dylan before anything acts on them (spec §5.5, D07)."""
    flags: list[str] = []
    if AMBIGUOUS_AMOUNT.search(text or ""):
        flags.append("an approximate amount")
    elif BARE_MONEY.search(text or ""):
        flags.append("an amount without a currency")
    if DATE_NO_YEAR.search(text or ""):
        flags.append("a date without a year")
    if FRAME_LIKE.search((text or "").upper()):
        flags.append("something that looks like a frame number")
    return flags


async def _store_bytes_as_asset(db: AsyncSession, actor: Actor, data: bytes, *, filename: str,
                                content_type: str, classification: str | None = None) -> dict:
    from .assets import part_key
    from .storage import storage
    prep = await dispatch(_ctx(db, actor), "assets.prepare_upload",
                          {"filename": filename, "content_type": content_type, "size_bytes": len(data),
                           "purpose": "intake"}, commit=False)
    upload_id = prep.data["upload"]["id"]
    storage().append_part(part_key(upload_id), data)
    await dispatch(_ctx(db, actor), "assets.record_chunk", {"upload_id": upload_id, "received_bytes": len(data)},
                   commit=False)
    fin = await dispatch(_ctx(db, actor), "assets.finalize_upload",
                         {"upload_id": upload_id, "source": "telegram",
                          **({"classification": classification} if classification else {})}, commit=False)
    return fin.data


async def _handle_voice(db: AsyncSession, pairing: TelegramPairing, actor: Actor, msg: dict, replies: list) -> str:
    from ..adapters import model as model_adapter
    voice = msg.get("voice") or msg.get("audio") or {}
    tok = await bot_token(db)
    if not tok:
        replies.append((pairing.chat_id, "I can't fetch voice notes yet — the Telegram bot token isn't configured.", None))
        return "setup_blocked: no bot token"
    try:
        data = await client(tok).get_file(voice.get("file_id"))
    except (ProviderError, Unsupported) as e:
        replies.append((pairing.chat_id, f"I couldn't download that voice note ({e}). Try again or type it.", None))
        return "voice download failed"
    ctype = voice.get("mime_type") or "audio/ogg"
    result = await _store_bytes_as_asset(db, actor, data, filename="voice-note", content_type=ctype,
                                         classification="voice_note")
    if result.get("status") != "ready":
        replies.append((pairing.chat_id, f"I couldn't save that voice note ({result.get('error')}). Please type it.", None))
        return "voice asset failed"
    asset_id = result["asset"]["id"]
    try:
        text = (await model_adapter.transcribe(data, ctype) or "").strip()
    except model_adapter.TranscriptionUnavailable:
        await _store_turn(db, pairing, "user", f"(voice note saved: asset {asset_id}; no transcript)",
                          msg.get("message_id"))
        replies.append((pairing.chat_id,
                        "I saved your voice note; transcription isn't configured — type what you said and I'll act on it.",
                        None))
        return "voice saved without transcription"
    except Exception as e:  # noqa: BLE001
        log.info("transcription failed: %s", e)
        replies.append((pairing.chat_id,
                        "I saved your voice note; transcription failed — type what you said and I'll act on it.", None))
        return "voice saved; transcription error"
    if not text:
        replies.append((pairing.chat_id, "I saved your voice note but the transcript came back empty — please type it.", None))
        return "voice saved; empty transcript"
    await dispatch(_ctx(db, actor), "assets.set_transcript",
                   {"asset_id": asset_id, "transcript": text, "source": "service"}, commit=False)
    await _store_turn(db, pairing, "user", text, msg.get("message_id"))
    flags = ambiguity_flags(text)
    if flags:
        out = ("Transcript: “" + bound(text, 400) + "”\nBefore I act: I heard " + ", ".join(flags)
               + ". Confirm the exact value and I'll continue.")
        turn = await _store_turn(db, pairing, "assistant", out, None)
        replies.append((pairing.chat_id, out, None, turn.id))
        return "voice transcript needs confirmation"
    out = "Transcript: “" + bound(text, 400) + "”\n" + await _free_text(db, pairing, actor, text)
    turn = await _store_turn(db, pairing, "assistant", out, None)
    replies.append((pairing.chat_id, out, None, turn.id))
    return "voice transcript handled"


# ── photos / albums -> one intake session per media group ───────────────────
async def _handle_photos(db: AsyncSession, pairing: TelegramPairing, actor: Actor, msg: dict, replies: list) -> str:
    if "intake.start" not in REGISTRY:
        replies.append((pairing.chat_id, "Photo intake isn't available yet.", None))
        return "intake unavailable"
    sizes = msg.get("photo") or []
    largest = sorted(sizes, key=lambda s: int(s.get("file_size") or s.get("width") or 0))[-1]
    group = msg.get("media_group_id")
    request_key = f"telegram:{group}" if group else f"telegram:msg:{msg.get('message_id')}"
    tok = await bot_token(db)
    if not tok:
        replies.append((pairing.chat_id, "I can't fetch photos yet — the Telegram bot token isn't configured.", None))
        return "setup_blocked: no bot token"
    ctxv = pairing.context or {}
    start = await dispatch(_ctx(db, actor), "intake.start",
                           {"target_mode": "existing" if ctxv.get("vehicle_id") else "find",
                            "vehicle_id": ctxv.get("vehicle_id"), "channel": "telegram",
                            "request_key": request_key, "telegram_media_group_id": group,
                            "text": (msg.get("caption") or "").strip() or None}, commit=False)
    intake = start.data["intake"]
    try:
        data = await client(tok).get_file(largest.get("file_id"))
    except (ProviderError, Unsupported) as e:
        replies.append((pairing.chat_id, f"I couldn't download that photo ({e}).", None))
        return "photo download failed"
    result = await _store_bytes_as_asset(db, actor, data, filename="telegram-photo", content_type="image/jpeg")
    if result.get("status") != "ready":
        replies.append((pairing.chat_id, f"That photo didn't save ({result.get('error')}). Send it again.", None))
        return "photo asset failed"
    added = await dispatch(_ctx(db, actor), "intake.add_assets",
                           {"intake_id": intake["id"], "asset_ids": [result["asset"]["id"]]}, commit=False)
    count = len((added.data.get("intake") or {}).get("asset_ids") or []) or 1
    if not start.data.get("created"):
        return f"photo added to intake {intake['id']} ({count})"
    from . import email_templates as et
    replies.append((pairing.chat_id,
                    f"Started an intake for these photos. Add more to the same album and I'll keep them together.\n"
                    f"{et.origin()}/intake/{intake['id']}", None))
    return f"intake {intake['id']} started from telegram"
