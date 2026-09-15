"""Notification API: one feed for the bell, Home and the mobile sheet, plus truthful delivery states
(spec §2.2, §5.4). Reads apply record scope and hide money without `costs.read`; every write goes
through a command. Preference writes belong to the team domain (`me.update_prefs`)."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import command_context, current_actor
from ..auth.passkey import current_user
from ..db import get_db
from ..domain.access import assert_vehicle_visible
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..models.auth import User
from ..models.tasks import Task
from ..services import notifications_feed, reminders
from ..services import team as team_svc
from ..services import telegram_bot

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

ACTIONS = {"acknowledge": "notifications.acknowledge", "snooze": "notifications.snooze",
           "dismiss": "notifications.dismiss"}
_ACCESS_KEYS = {"role", "scope", "perms", "status", "manager_id", "grants", "session_version"}


@router.get("")
async def feed(actor: Actor = Depends(current_actor), db: AsyncSession = Depends(get_db)):
    """The single notification source. `status.message` never claims all-clear on stale sources (H11)."""
    return await notifications_feed.build(db, actor)


@router.get("/deliveries")
async def deliveries(task_id: str | None = Query(default=None), mine: bool = Query(default=False),
                     limit: int = Query(50, ge=1, le=50), actor: Actor = Depends(current_actor),
                     db: AsyncSession = Depends(get_db)):
    """Queued / provider accepted / delivered / failed / unknown per scheduled delivery.

    `mine=true` lists the caller's own recent deliveries instead of one task's."""
    if mine:
        if actor.kind != "user" or not actor.user_id:
            raise HTTPException(403, "not allowed")
        rows = await reminders.deliveries_for_user(db, actor.user_id, actor, limit=limit)
        return {"mine": True, "deliveries": rows, "total": len(rows),
                "note": "Provider acceptance is not proof the message was seen."}
    if not task_id:
        raise HTTPException(422, "task_id or mine=true is required")
    t = await db.get(Task, task_id)
    if t is None:
        raise HTTPException(404, "task not found")
    if t.vehicle_id:
        await assert_vehicle_visible(db, actor, t.vehicle_id)
    elif actor.kind in ("user", "agent") and (actor.scope == "assigned" or not actor.perms.get("vehicles.all", False)):
        if t.owner_user_id != actor.user_id:
            raise HTTPException(403, "record not accessible")
    rows = await reminders.deliveries_for_task(db, task_id, actor)
    return {"task_id": task_id, "deliveries": rows,
            "note": "Provider acceptance is not proof the message was seen."}


@router.get("/prefs")
async def prefs(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    cfg = await reminders.config(db)
    return {"prefs": team_svc.serialize_prefs(user),
            "business_defaults": {"channels": cfg["channels"], "digest_local_time": cfg["digest_local_time"],
                                  "late_grace_minutes": cfg["late_grace_minutes"],
                                  "obsolete_after_hours": cfg["obsolete_after_hours"],
                                  "overdue_delay_minutes": cfg["overdue_delay_minutes"],
                                  **reminders.owner_reminder_destination(reveal=user.role == "owner")},
            "telegram": await telegram_bot.serialize_pairing(db, user.id),
            "write_with": "PATCH /api/me/prefs"}


@router.patch("/prefs")
async def update_prefs(request: Request, ctx: CommandContext = Depends(command_context)):
    """Convenience alias; the write is the team domain's own command."""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(422, "expected an object")
    if _ACCESS_KEYS & set(body):
        raise HTTPException(403, "not allowed")
    return (await dispatch(ctx, "me.update_prefs", {"notification_prefs": body})).to_dict()


@router.post("/{notification_id}/{action}")
async def act(notification_id: str, action: str, payload: dict = Body(default={}),
              ctx: CommandContext = Depends(command_context)):
    name = ACTIONS.get(action)
    if name is None:
        raise HTTPException(404, f"unknown notification action {action!r}")
    body = dict(payload or {})
    body["notification_id"] = notification_id
    return (await dispatch(ctx, name, body)).to_dict()
