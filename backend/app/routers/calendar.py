"""Calendar read API and the two owner switches (spec §5.3, §11.2).

    GET  /api/calendar/status   — connection, the owner capability, the rules, what is out of sync
    GET  /api/calendar/today    — a day's appointments from AZKT plus what the calendar itself holds
    POST /api/calendar/writes   — owner: turn event creation on/off, choose the calendar
    POST /api/calendar/resync   — owner: queue calendar work for one task or for everything stale
    POST /api/calendar/connect  — owner: start the Google consent flow with the calendar scopes

GETs have no side effects: `/today` reads the calendar, it never creates or repairs anything. Both
writes go through commands, and both report `setup_blocked` with the missing half named rather than
pretending to have worked (spec §12.3).
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters import google_oauth
from ..auth.deps import command_context, current_actor, require
from ..core.config import settings
from ..core.time import PHOENIX, parse_iso
from ..db import get_db
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..domain.policy import has_perm
from ..models import User
from ..models.tasks import Task
from ..services import calendar_sync as svc
from .tasks import visibility_clauses

router = APIRouter(prefix="/api/calendar", tags=["calendar"])


@router.get("/status")
async def status(actor: Actor = Depends(require("connections")), db: AsyncSession = Depends(get_db)) -> dict:
    return await svc.status(db)


@router.get("/today")
async def today(day: str | None = Query(default=None, description="YYYY-MM-DD or an ISO instant; default today"),
                tz: str | None = Query(default=None, description="IANA zone the day is read in"),
                include_calendar: bool = Query(default=True),
                actor: Actor = Depends(require("tasks.read")), db: AsyncSession = Depends(get_db)) -> dict:
    """The schedule for one local day. AZKT's own appointments are the list; the Google entries are
    context, and are reported as setup-blocked rather than as an empty day when the calendar cannot
    be read."""
    when: datetime | None = None
    if day:
        try:
            when = parse_iso(day if "T" in day else f"{day}T12:00:00+00:00")
        except ValueError:
            raise HTTPException(422, "day must be YYYY-MM-DD or an ISO instant")
    zone = tz or (await _user_timezone(db, actor)) or PHOENIX
    # the calendar block is the business account's, so only someone who may see connections gets it
    show_calendar = include_calendar and has_perm(actor, "connections")
    clauses = await visibility_clauses(db, actor, "all")
    visible = None
    if clauses:
        visible = {r[0] for r in (await db.execute(select(Task.id).where(*clauses))).all()}
    return await svc.schedule_for_day(db, day=when, tz=zone, visible_task_ids=visible,
                                      include_calendar=show_calendar)


async def _user_timezone(db: AsyncSession, actor: Actor) -> str | None:
    if actor.kind != "user" or not actor.user_id:
        return None
    u = await db.get(User, actor.user_id)
    return (u.timezone if u else None) or None


@router.post("/writes")
async def set_writes(body: svc.CalendarWritesIn, ctx: CommandContext = Depends(command_context)) -> dict:
    return (await dispatch(ctx, "calendar.set_writes", body)).to_dict()


@router.post("/resync")
async def resync(body: svc.CalendarResyncIn | None = Body(default=None),
                 ctx: CommandContext = Depends(command_context)) -> dict:
    return (await dispatch(ctx, "calendar.resync", body or svc.CalendarResyncIn())).to_dict()


@router.post("/connect")
async def connect(body: dict = Body(default={}), actor: Actor = Depends(require("connections")),
                  db: AsyncSession = Depends(get_db)) -> dict:
    """Start Google consent for the business calendar.

    `enable_write` asks for the event scope as well; it does **not** switch writes on — that is the
    separate owner capability (`POST /api/calendar/writes`), exactly as spec §11.2 requires."""
    from ..services import connections as conn_svc
    await conn_svc.get(db, svc.PROVIDER, create=True)
    await db.commit()
    extra = list(google_oauth.SCOPES["google_calendar_write"]) if (body or {}).get("enable_write") else []
    res = google_oauth.start(svc.PROVIDER, actor.user_id, extra)
    return {"url": res["url"], "scopes": res["scopes"], "redirect_uri": google_oauth.redirect_uri(),
            "expected_identity": settings.BUSINESS_EMAIL}


@router.get("/tasks/{task_id}")
async def task_entry(task_id: str, actor: Actor = Depends(current_actor),
                     db: AsyncSession = Depends(get_db)) -> dict:
    """One task's calendar state: whether it belongs on the calendar, and what happened to it."""
    if not has_perm(actor, "tasks.read"):
        raise HTTPException(403, "missing permission tasks.read")
    clauses = await visibility_clauses(db, actor, "all")
    t = (await db.execute(select(Task).where(Task.id == task_id, *clauses))).scalar_one_or_none()
    if t is None:
        raise HTTPException(404, "task not found")
    return svc.serialize_task_entry(t, await svc.config(db))
