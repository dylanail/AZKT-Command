"""Telegram webhook and pairing API (spec §5.5).

The webhook verifies `X-Telegram-Bot-Api-Secret-Token` **before** anything is stored, then records the
update durably (`events.record_provider_event`, deduplicated by `update_id`) and acknowledges. All
interpretation happens asynchronously in the `telegram.process_update` job.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters.telegram import webhook_secret_ok
from ..auth.deps import command_context, require_owner
from ..auth.passkey import current_user
from ..db import get_db
from ..domain import jobs
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..domain.events import record_provider_event
from ..models.auth import User
from ..services import telegram_bot

log = logging.getLogger("azkt.telegram")
router = APIRouter(prefix="/api/telegram", tags=["telegram"])
CONNECTION_KEY = "bot"


@router.post("/webhook")
async def webhook(request: Request, db: AsyncSession = Depends(get_db),
                  x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if not webhook_secret_ok(x_telegram_bot_api_secret_token):
        # a spoofed update is refused before it is stored or interpreted
        raise HTTPException(403, "invalid webhook secret")
    try:
        update = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "expected a JSON update")
    if not isinstance(update, dict) or update.get("update_id") is None:
        raise HTTPException(400, "update_id is required")
    row, is_new = await record_provider_event(db, "telegram", CONNECTION_KEY, str(update["update_id"]), update,
                                              event_type=next((k for k in ("message", "edited_message", "callback_query")
                                                               if k in update), None), signature_ok=True)
    if is_new:
        await jobs.enqueue(db, "telegram.process_update", {"provider_event_id": row.id},
                           dedupe_key=f"telegram:update:{update['update_id']}")
    await db.commit()
    return {"ok": True, "stored": True, "duplicate": not is_new}


@router.get("/status")
async def status(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await telegram_bot.serialize_pairing(db, user.id)


@router.post("/pair")
async def pair(payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context),
               _: Actor = Depends(require_owner())):
    return (await dispatch(ctx, "telegram.start_pairing", payload or {})).to_dict()


@router.post("/set-webhook")
async def set_webhook(payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context),
                      _: Actor = Depends(require_owner())):
    return (await dispatch(ctx, "telegram.set_webhook", payload or {})).to_dict()


@router.post("/pairings/{pairing_id}/{action}")
async def pairing_action(pairing_id: str, action: str, payload: dict = Body(default={}),
                         ctx: CommandContext = Depends(command_context), _: Actor = Depends(require_owner())):
    name = {"confirm": "telegram.confirm_pairing", "revoke": "telegram.revoke_pairing"}.get(action)
    if name is None:
        raise HTTPException(404, f"unknown pairing action {action!r}")
    body = dict(payload or {})
    body["pairing_id"] = pairing_id
    return (await dispatch(ctx, name, body)).to_dict()
