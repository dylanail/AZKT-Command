"""Inbox API (spec §2.3 Inbox, §4.3, §12.3).

Reads apply record scope and hide owner-only money; every write dispatches a command. Personal-mailbox
threads are visible to the owner only. No GET has a side effect (invariant 11).
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import command_context, require
from ..core.errors import NotFound, ValidationFailed
from ..db import get_db
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..models.comms import Draft
from ..services import inbox as inbox_svc
from ..services.reply import serialize_draft_live

router = APIRouter(tags=["inbox"])

ACTIONS = {
    "prepare": "reply.prepare",
    "edit": "reply.edit",
    "submit": "reply.submit_for_approval",
    "take-over": "inbox.take_over",
    "resume": "inbox.resume",
    "link": "inbox.link_record",
    "unlink": "inbox.unlink_record",
    "classify": "inbox.classify_override",
    "spam": "inbox.mark_spam",
    "not-spam": "inbox.not_spam",
    "archive": "inbox.archive",
    "manual-reply": "inbox.manual_reply_recorded",
    "reconcile": "reply.reconcile_unknown",
}
CONVERSATION_KEY = {"reply.prepare": "conversation_id", "inbox.take_over": "conversation_id",
                    "inbox.resume": "conversation_id", "inbox.link_record": "conversation_id",
                    "inbox.unlink_record": "conversation_id", "inbox.classify_override": "conversation_id",
                    "inbox.mark_spam": "conversation_id", "inbox.not_spam": "conversation_id",
                    "inbox.archive": "conversation_id", "inbox.manual_reply_recorded": "conversation_id"}


@router.get("/api/inbox/threads")
async def list_threads(filter: str = Query("needs_reply"), account: str | None = None, q: str | None = None,
                       limit: int = Query(50, le=200), offset: int = 0,
                       actor: Actor = Depends(require("inbox.read")), db: AsyncSession = Depends(get_db)):
    return await inbox_svc.list_threads(db, actor, filter=filter, account=account, limit=limit, offset=offset, q=q)


@router.get("/api/inbox/counts")
async def thread_counts(account: str | None = None, q: str | None = None,
                        actor: Actor = Depends(require("inbox.read")), db: AsyncSession = Depends(get_db)):
    """{filter: n} for the bell/tab headers, under the caller's own record scope."""
    return await inbox_svc.counts(db, actor, account=account, q=q)


@router.get("/api/inbox/coverage")
async def coverage(actor: Actor = Depends(require("inbox.read")), db: AsyncSession = Depends(get_db)):
    """Per account: connected, freshness, coverage window, unresolved gaps, excluded counts (F5/H11)."""
    return await inbox_svc.coverage(db, actor)


@router.get("/api/inbox/threads/{conversation_id}")
async def thread_detail(conversation_id: str, actor: Actor = Depends(require("inbox.read")),
                        db: AsyncSession = Depends(get_db)):
    return await inbox_svc.thread_detail(db, actor, conversation_id)


@router.get("/api/drafts/{draft_id}/versions")
async def draft_versions(draft_id: str, actor: Actor = Depends(require("inbox.read")),
                         db: AsyncSession = Depends(get_db)):
    d = await db.get(Draft, draft_id)
    if d is None:
        raise NotFound("draft not found")
    # record scope: the thread guard is the same one the detail view applies
    await inbox_svc.thread_detail(db, actor, d.conversation_id)
    rows = (await db.execute(select(Draft).where(Draft.conversation_id == d.conversation_id)
                             .order_by(Draft.draft_version.desc()))).scalars().all()
    return {"items": [await serialize_draft_live(db, r, actor=actor) for r in rows], "total": len(rows),
            "conversation_id": d.conversation_id, "current": await serialize_draft_live(db, d, actor=actor)}


@router.post("/api/inbox/threads/{conversation_id}/{action}")
async def thread_action(conversation_id: str, action: str, body: dict = Body(default_factory=dict),
                        ctx: CommandContext = Depends(command_context)):
    name = ACTIONS.get(action)
    if name is None:
        raise ValidationFailed(f"unknown action {action}; use one of {sorted(ACTIONS)}")
    payload = dict(body or {})
    key = CONVERSATION_KEY.get(name)
    if key:
        payload[key] = conversation_id
    elif name in ("reply.edit", "reply.submit_for_approval", "reply.reconcile_unknown"):
        # the path names the thread: a draft id from another thread is a mistake, not a shortcut
        if not payload.get("draft_id"):
            raise ValidationFailed("draft_id is required for this action")
        d = await ctx.db.get(Draft, payload["draft_id"])
        if d is None:
            raise NotFound("draft not found")
        if d.conversation_id != conversation_id:
            raise ValidationFailed("this draft belongs to a different thread")
    res = await dispatch(ctx, name, payload)
    return res.to_dict()


@router.post("/api/inbox/paste")
async def paste_thread(body: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    """Setup fallback: enter a thread by hand so drafting works before Gmail is connected."""
    res = await dispatch(ctx, "inbox.paste_thread", body)
    return res.to_dict()


@router.get("/api/inbox/threads/{conversation_id}/drafts")
async def thread_drafts(conversation_id: str, actor: Actor = Depends(require("inbox.read")),
                        db: AsyncSession = Depends(get_db)):
    detail = await inbox_svc.thread_detail(db, actor, conversation_id)
    return {"items": detail["drafts"], "total": len(detail["drafts"])}
