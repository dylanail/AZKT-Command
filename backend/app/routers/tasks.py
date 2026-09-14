"""Tasks API (spec §2.3 Tasks / Employee task, §5.3). Reads apply record scope; writes dispatch
services/tasks.py commands. Times: ISO with offsets (or naive + IANA zone) in, UTC + zone out,
with Phoenix / zone / Tokyo display fields (C05)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import command_context, current_actor, require
from ..core.errors import NotFound, ValidationFailed
from ..core.time import PHOENIX, TOKYO, ensure_aware, fmt_local, parse_iso, to_zone
from ..db import get_db
from ..domain.access import visible_vehicle_ids
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..models import User
from ..models.tasks import TASK_STATUSES, Task
from ..services.tasks import serialize_task

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

ACTIVE = ("open", "in_progress", "blocked", "waiting", "awaiting_verification")
BUCKETS = ("upcoming", "overdue", "unassigned", "blocked", "waiting", "awaiting_verification", "completed")
COMMANDS = {
    "update": "tasks.update", "reschedule": "tasks.reschedule", "snooze": "tasks.snooze", "assign": "tasks.assign",
    "start": "tasks.start", "complete": "tasks.complete", "cancel": "tasks.cancel", "reopen": "tasks.reopen",
    "evidence": "tasks.attach_evidence", "block": "tasks.report_blocker", "verify": "tasks.verify",
    "reject": "tasks.reject_evidence",
}


# ── time helpers ─────────────────────────────────────────────────────────────
def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or PHOENIX)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationFailed(f"unknown timezone {name!r}; use an IANA name like America/Phoenix")


def to_utc(value, tz: str | None) -> datetime | None:
    """ISO string/datetime with an offset -> UTC; a naive value is interpreted in `tz` (IANA)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            raise ValidationFailed(f"invalid datetime {value!r}; use ISO 8601")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zone(tz))
    return dt.astimezone(timezone.utc)


def normalize_times(payload: dict, default_tz: str | None) -> dict:
    out = dict(payload)
    tz = out.get("timezone") or default_tz or PHOENIX
    _zone(tz)
    for k in ("due_at", "start_at", "end_at", "next_check_at"):
        if k in out and out[k] is not None:
            out[k] = to_utc(out[k], tz).isoformat()
    return out


def _display(dt: datetime | None, tz: str, jp: bool) -> dict:
    if not dt:
        return {"utc": None, "local": None, "phoenix": "Not scheduled", "zone": None, "tokyo": None}
    dt = ensure_aware(dt)
    d = {"utc": dt.isoformat(), "local": to_zone(dt, tz).isoformat(), "phoenix": fmt_local(dt, PHOENIX),
         "zone": fmt_local(dt, tz), "tokyo": None}
    if jp or tz == TOKYO:
        d["tokyo"] = fmt_local(dt, TOKYO)
    return d


def buckets_of(t: Task, now: datetime) -> list[str]:
    due = ensure_aware(t.due_at)
    out: list[str] = []
    if t.status in ("open", "in_progress", "waiting") and (due is None or due >= now):
        out.append("upcoming")
    if t.status in ("open", "in_progress", "blocked", "waiting") and due and due < now:
        out.append("overdue")
    if t.status in ACTIVE and not t.owner_user_id:
        out.append("unassigned")
    if t.status in ("blocked", "waiting", "awaiting_verification", "completed"):
        out.append(t.status)
    return out


def task_view(t: Task, *, jp: bool = False, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    d = serialize_task(t)
    tz = t.timezone or PHOENIX
    due = _display(t.due_at, tz, jp)
    d.update({
        "local_due": due["phoenix"], "zone_due": due["zone"], "tokyo_due": due["tokyo"], "due_local_iso": due["local"],
        "start": _display(t.start_at, tz, jp), "end": _display(t.end_at, tz, jp),
        "snoozed_until_local": fmt_local(ensure_aware(t.snoozed_until), PHOENIX) if t.snoozed_until else None,
        "buckets": buckets_of(t, now),
    })
    return d


# ── visibility ───────────────────────────────────────────────────────────────
async def visibility_clauses(db: AsyncSession, actor: Actor, view: str) -> list:
    """Mechanics (scope=assigned) see only their own tasks; managers see their own, their people's and
    unassigned work; the owner sees everything. `view=my` always narrows to the actor's own tasks."""
    clauses: list = []
    if view == "my":
        clauses.append(Task.owner_user_id == actor.user_id)
    if actor.kind in ("user", "agent") and actor.role != "owner":
        if actor.scope == "assigned":
            clauses.append(Task.owner_user_id == actor.user_id)
        else:
            reports = [r[0] for r in (await db.execute(select(User.id).where(User.manager_id == actor.user_id))).all()]
            clauses.append(or_(Task.owner_user_id.in_([actor.user_id, *reports]), Task.owner_user_id.is_(None)))
    limit = await visible_vehicle_ids(db, actor)
    if limit is not None and actor.kind == "external":
        clauses.append(or_(Task.vehicle_id.in_(list(limit)), Task.vehicle_id.is_(None)))
    return clauses


def bucket_clause(bucket: str, now: datetime):
    if bucket == "upcoming":
        return [Task.status.in_(("open", "in_progress", "waiting")), or_(Task.due_at.is_(None), Task.due_at >= now)]
    if bucket == "overdue":
        return [Task.status.in_(("open", "in_progress", "blocked", "waiting")), Task.due_at < now]
    if bucket == "unassigned":
        return [Task.status.in_(ACTIVE), Task.owner_user_id.is_(None)]
    if bucket in ("blocked", "waiting", "awaiting_verification", "completed"):
        return [Task.status == bucket]
    raise HTTPException(422, f"bucket must be one of {BUCKETS}")


async def _load_visible(db: AsyncSession, actor: Actor, task_id: str) -> Task:
    clauses = await visibility_clauses(db, actor, "all")
    t = (await db.execute(select(Task).where(Task.id == task_id, *clauses))).scalar_one_or_none()
    if t is None:
        raise NotFound("task not found")
    return t


async def _related(db: AsyncSession, t: Task) -> dict:
    out: dict = {"vehicle": None, "contact": None, "opportunity": None}
    if t.vehicle_id:
        from ..models.vehicles import Vehicle
        from ..services.sales import vehicle_title
        v = await db.get(Vehicle, t.vehicle_id)
        if v is not None:
            out["vehicle"] = {"id": v.id, "title": vehicle_title(v), "stock_no": v.stock_no, "location": v.location,
                              "hero_asset_id": v.hero_asset_id, "photo": "No photo yet" if not v.hero_asset_id else None}
    if t.contact_id:
        from ..models.contacts import Contact
        c = await db.get(Contact, t.contact_id)
        if c is not None:
            out["contact"] = {"id": c.id, "name": c.name, "company": c.company}
    if t.opportunity_id:
        from ..models.sales import Opportunity
        from ..services.sales import STAGE_LABELS
        o = await db.get(Opportunity, t.opportunity_id)
        if o is not None:
            out["opportunity"] = {"id": o.id, "pipeline": o.pipeline, "stage": o.stage,
                                  "stage_label": STAGE_LABELS.get(o.stage, o.stage)}
    return out


# ── reads ────────────────────────────────────────────────────────────────────
@router.get("")
async def list_tasks(view: str = Query("my"), bucket: str | None = None, vehicle_id: str | None = None,
                     opportunity_id: str | None = None, contact_id: str | None = None, owner: str | None = None,
                     status: str | None = None, type: str | None = None, include_closed: bool = False,
                     jp: bool = False, limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
                     actor: Actor = Depends(require("tasks.read")), db: AsyncSession = Depends(get_db)):
    if view not in ("my", "all"):
        raise HTTPException(422, "view must be my or all")
    now = datetime.now(timezone.utc)
    clauses = await visibility_clauses(db, actor, view)
    if bucket:
        clauses += bucket_clause(bucket, now)
    elif status:
        if status not in TASK_STATUSES:
            raise HTTPException(422, f"status must be one of {TASK_STATUSES}")
        clauses.append(Task.status == status)
    elif not include_closed:
        clauses.append(Task.status.in_(ACTIVE))
    if vehicle_id:
        clauses.append(Task.vehicle_id == vehicle_id)
    if opportunity_id:
        clauses.append(Task.opportunity_id == opportunity_id)
    if contact_id:
        clauses.append(Task.contact_id == contact_id)
    if owner:
        clauses.append(Task.owner_user_id.is_(None) if owner == "none" else Task.owner_user_id == owner)
    if type:
        clauses.append(Task.type == type)
    order = (Task.completed_at.desc(),) if bucket == "completed" else (Task.due_at.asc().nulls_last(), Task.created_at.asc())
    total = (await db.execute(select(func.count()).select_from(Task).where(*clauses))).scalar_one()
    rows = (await db.execute(select(Task).where(*clauses).order_by(*order).limit(limit).offset(offset))).scalars().all()
    # one row per task: a task lists every bucket it belongs to instead of appearing twice
    return {"items": [task_view(t, jp=jp, now=now) for t in rows], "total": int(total), "view": view, "bucket": bucket}


@router.get("/summary")
async def summary(tz: str = Query(PHOENIX), actor: Actor = Depends(require("tasks.read")),
                  db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    zone = _zone(tz)
    local_now = now.astimezone(zone)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    day_end = (local_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).astimezone(timezone.utc)
    base = await visibility_clauses(db, actor, "all")

    async def count(*extra) -> int:
        return int((await db.execute(select(func.count()).select_from(Task).where(*base, *extra))).scalar_one())

    return {
        "overdue": await count(*bucket_clause("overdue", now)),
        "today": await count(Task.status.in_(ACTIVE), Task.due_at >= day_start, Task.due_at < day_end),
        "unassigned": await count(*bucket_clause("unassigned", now)),
        "blocked": await count(Task.status == "blocked"),
        "awaiting_verification": await count(Task.status == "awaiting_verification"),
        "as_of": now.isoformat(), "timezone": tz,
    }


@router.get("/schedule")
async def schedule(from_: str | None = Query(None, alias="from"), to: str | None = None, tz: str = Query(PHOENIX),
                   view: str = Query("all"), jp: bool = False, actor: Actor = Depends(require("tasks.read")),
                   db: AsyncSession = Depends(get_db)):
    """Calendar-style view: scheduled tasks in [from, to), grouped by local day in `tz`."""
    zone = _zone(tz)
    now = datetime.now(timezone.utc)
    start = to_utc(from_, tz) if from_ else now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    end = to_utc(to, tz) if to else start + timedelta(days=7)
    if end <= start:
        raise HTTPException(422, "to must be after from")
    clauses = await visibility_clauses(db, actor, view if view in ("my", "all") else "all")
    clauses.append(Task.status.notin_(("cancelled",)))
    when = func.coalesce(Task.start_at, Task.due_at)
    clauses += [when >= start, when < end]
    rows = (await db.execute(select(Task).where(*clauses).order_by(when.asc(), Task.created_at))).scalars().all()
    items = []
    for t in rows:
        d = task_view(t, jp=jp, now=now)
        anchor = ensure_aware(t.start_at or t.due_at)
        d["day"] = anchor.astimezone(zone).date().isoformat() if anchor else None
        items.append(d)
    return {"items": items, "total": len(items), "from": start.isoformat(), "to": end.isoformat(), "timezone": tz}


@router.get("/{task_id}")
async def get_task(task_id: str, jp: bool = False, actor: Actor = Depends(require("tasks.read")),
                   db: AsyncSession = Depends(get_db)):
    t = await _load_visible(db, actor, task_id)
    return {"task": task_view(t, jp=jp), "related": await _related(db, t)}


# ── writes (all through commands) ───────────────────────────────────────────
async def _result_with_view(ctx: CommandContext, res, jp: bool = False) -> dict:
    d = res.to_dict()
    data = d.get("data")
    if isinstance(data, dict) and isinstance(data.get("task"), dict):
        t = await ctx.db.get(Task, data["task"]["id"])
        if t is not None:
            data["task"] = task_view(t, jp=jp)
    return d


@router.post("")
async def create_task(payload: dict = Body(...), jp: bool = False, ctx: CommandContext = Depends(command_context)):
    res = await dispatch(ctx, "tasks.create", normalize_times(payload, payload.get("timezone")))
    return await _result_with_view(ctx, res, jp)


@router.post("/{task_id}/{action}")
async def task_action(task_id: str, action: str, payload: dict = Body(default={}), jp: bool = False,
                      ctx: CommandContext = Depends(command_context)):
    name = COMMANDS.get(action)
    if name is None:
        raise HTTPException(404, f"unknown task action {action!r}")
    body = dict(payload or {})
    body["task_id"] = task_id
    if action in ("reschedule", "update"):
        current = await ctx.db.get(Task, task_id)
        body = normalize_times(body, (current.timezone if current else None) or body.get("timezone"))
    res = await dispatch(ctx, name, body)
    return await _result_with_view(ctx, res, jp)
