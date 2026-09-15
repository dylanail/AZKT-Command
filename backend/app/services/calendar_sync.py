"""Appointments on the business calendar (spec §5.3, §11.2, §12.3).

Dylan's decision, verbatim: *"create events from tasks on calendar — but only like meetings, call
people back, schedules, etc."* That is implemented literally in :func:`eligibility`:

| Task type     | On the calendar?                                                              |
|---------------|-------------------------------------------------------------------------------|
| `call`        | always, once it has a time — "call people back" is an appointment              |
| `meeting`     | always, once it has a time                                                     |
| `follow_up`   | only when it carries an explicit `start_at`/`end_at` block — "schedules"       |
| `operational` | never — shop work is a to-do, and a calendar full of to-dos is a useless calendar |

A task with no time at all is never an event: an appointment AZKT invented a time for would be a
fabricated commitment, not a schedule.

How it runs, and why it is built this way:

* **Event-driven, repaired by a sweep.** `@on_event("task.changed")` only decides whether the task
  could possibly need calendar work and enqueues a durable job; every provider call happens in the
  job, so a failure retries with backoff instead of poisoning the outbox, and `calendar.repair`
  re-queues anything a lost enqueue or a crashed worker left behind.
* **Idempotent at the provider, not only in our bookkeeping.** The event id is derived from the task
  id (`adapters.calendar.event_id_for`), so a replayed `task.changed` inserts the *same* id and
  Google itself refuses the duplicate, which AZKT then adopts. `calendar_synced_revision` and
  `calendar_synced_hash` on the task mean the common replay does not even reach the network.
* **A lost result is reconciled, never retried blind** (spec §11.5.4). `UnknownWriteResult` marks the
  task `unknown`; the next attempt looks the event up by its deterministic id and patches what it
  finds instead of creating a second entry.
* **Writes need two separate permissions.** A connected `google_calendar` account *and* the owner's
  explicit capability switch (spec §11.2: "calendar writes — separate explicit capability
  permission"). With either missing the task is marked `setup_blocked` with the provider's own
  reason and **nothing is written** — never a silent no-op that looks like success.
* **AZKT stays authoritative for reminders** (spec §5.4). The payload pins `reminders.useDefault` to
  false with no overrides and never carries attendees, so Google does not send a second nudge for an
  appointment AZKT already reminds about, and no customer is invited without an exact approval.
* **A clash is reported, not refused.** An overlapping entry is recorded on the task and raised as a
  notification; AZKT does not decide for the owner that the appointment is wrong.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters import calendar as cal
from ..core.errors import Blocked, NotFound, ProviderError, Unsupported, ValidationFailed
from ..core.ids import stable_hash
from ..core.time import PHOENIX, ensure_aware, fmt_local
from ..domain import jobs
from ..domain.commands import CommandContext, command, dispatch
from ..domain.events import on_event
from ..domain.jobs import sweep
from ..models import User
from ..models.comms import Connection
from ..models.tasks import Task
from . import connections as conn_svc
from . import email_templates, reminders
from . import settings_store

log = logging.getLogger("azkt.calendar")

PROVIDER = "google_calendar"
SETTING_KEY = "calendar"
# "only like meetings, call people back, schedules" — the owner's own words, encoded once.
ALWAYS_TYPES = ("call", "meeting")
WINDOW_TYPES = ("follow_up",)
NEVER_TYPES = ("operational",)
CLOSED_STATUSES = ("completed", "cancelled")
# How far back/forward the repair sweep looks. An appointment from last month needs no calendar entry
# any more, and one two months out is already written.
REPAIR_PAST_DAYS = 2
REPAIR_FUTURE_DAYS = 120
REPAIR_LIMIT = 200
REPAIR_SECONDS = 300
CONFLICT_MAX = 5


def now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _zone(tz: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz or PHOENIX)
    except Exception:  # noqa: BLE001  - a task saved with an unknown zone still has a real UTC instant
        return ZoneInfo(PHOENIX)


# ── configuration ───────────────────────────────────────────────────────────
async def config(db: AsyncSession) -> dict:
    """The owner's calendar settings, defaults merged in. A corrupt stored value never enables writes."""
    try:
        eff = await settings_store.get_effective(db, SETTING_KEY)
    except Exception:  # noqa: BLE001
        eff = settings_store.defaults_for(SETTING_KEY)
    return {
        "writes_enabled": bool(eff.get("writes_enabled", False)),
        "calendar_id": str(eff.get("calendar_id") or cal.PRIMARY),
        "conflict_check": bool(eff.get("conflict_check", True)),
        "minutes": {"call": int(eff.get("call_minutes") or 30),
                    "meeting": int(eff.get("meeting_minutes") or 60),
                    "follow_up": int(eff.get("follow_up_minutes") or 30)},
    }


async def connection(db: AsyncSession) -> Connection | None:
    return await conn_svc.get(db, PROVIDER)


async def adapter(db: AsyncSession, *, cfg: dict | None = None, write: bool = True):
    """The active calendar client, or `Unsupported` naming exactly which half of the setup is missing."""
    cfg = cfg if cfg is not None else await config(db)
    conn = await connection(db)
    return cal.adapter_for(db, conn, writes_enabled=cfg["writes_enabled"], write=write)


# ── eligibility and the appointment window ──────────────────────────────────
def eligibility(task: Task, cfg: dict | None = None) -> dict:
    """Is this task an appointment, and when?

    Returns `{eligible, reason, start, end}`. `reason` is written for a person: it is what Settings →
    Calendar and the task row show when an entry is deliberately absent.
    """
    minutes = (cfg or {}).get("minutes") or {}
    if task.type in NEVER_TYPES:
        return {"eligible": False, "reason": "operational work is a to-do, not an appointment",
                "start": None, "end": None}
    if task.type not in ALWAYS_TYPES + WINDOW_TYPES:
        return {"eligible": False, "reason": f"{task.type} tasks are not appointments", "start": None, "end": None}
    if task.is_suggestion:
        return {"eligible": False, "reason": "a suggested task is not a commitment yet", "start": None, "end": None}
    if task.status in CLOSED_STATUSES:
        return {"eligible": False, "reason": f"the task is {task.status}", "start": None, "end": None}
    start = ensure_aware(task.start_at) or ensure_aware(task.due_at)
    end = ensure_aware(task.end_at)
    if task.type in WINDOW_TYPES:
        # a follow-up only becomes an appointment when somebody scheduled an actual block for it
        if not (task.start_at and task.end_at):
            return {"eligible": False, "reason": "a follow-up is only on the calendar when it has a "
                                                 "scheduled start and end", "start": None, "end": None}
    if start is None:
        return {"eligible": False, "reason": "the task has no scheduled time", "start": None, "end": None}
    if end is None or ensure_aware(end) <= start:
        end = start + cal.default_window(task.type, minutes)
    return {"eligible": True, "reason": "", "start": start, "end": end}


def summary_for(task: Task) -> str:
    """The owner's own wording. AZKT does not prefix "Call:" onto a task already called "Call Tomás"."""
    return cal.clean_summary(task.title)


def description_for(task: Task, window: dict, owner_name: str | None = None) -> str:
    """What the entry says, including the deep link back to the exact task.

    The link opens an authenticated page (`core/config.PUBLIC_ORIGIN` via
    `email_templates.deep_link`); opening it never completes or snoozes anything (spec §5.4)."""
    kind = {"call": "Call", "meeting": "Meeting", "follow_up": "Scheduled follow-up"}.get(task.type, task.type)
    lines = [f"{kind} · AZKT task" + (f" · {owner_name}" if owner_name else "")]
    if task.notes:
        lines += ["", task.notes.strip()]
    if task.instructions:
        lines += ["", task.instructions.strip()]
    lines += ["", f"Open in AZKT: {email_templates.deep_link('task', task.id)}",
              "AZKT sends the reminder for this task; this entry is the appointment itself."]
    if (task.timezone or PHOENIX) != PHOENIX:
        lines.append(f"Scheduled in {task.timezone} · {fmt_local(window['start'], PHOENIX)} in Phoenix.")
    return "\n".join(lines)


def body_for(task: Task, window: dict, owner_name: str | None = None) -> dict:
    return cal.event_body_fields(
        summary=summary_for(task), description=description_for(task, window, owner_name),
        start=window["start"], end=window["end"], tz=task.timezone or PHOENIX,
        task_id=task.id, revision=int(task.schedule_revision or 1),
        source_url=email_templates.deep_link("task", task.id))


async def owner_name_of(db: AsyncSession, task: Task) -> str | None:
    """Whose appointment it is, for the entry's first line. An unassigned task simply says nothing."""
    if not task.owner_user_id:
        return None
    u = await db.get(User, task.owner_user_id)
    return (u.display_name or u.handle) if u is not None else None


def content_hash(task: Task, window: dict) -> str:
    """Everything a calendar entry shows. A title, notes or owner change moves this; a version bump
    that changed none of them does not, so an unrelated edit never causes a calendar write.

    The owner is hashed by id rather than by name because `plan_for` has to answer "does this need a
    write?" without a database round-trip, and the id changes exactly when the owner does."""
    return stable_hash({"summary": summary_for(task), "notes": task.notes or "",
                        "instructions": task.instructions or "", "type": task.type,
                        "owner": task.owner_user_id or "", "tz": task.timezone or PHOENIX,
                        "start": _iso(window["start"]), "end": _iso(window["end"])})


def plan_for(task: Task, cfg: dict | None = None) -> dict:
    """What the calendar should be made to look like for this task, before anything is called."""
    el = eligibility(task, cfg)
    has_entry = bool(task.calendar_event_id) or task.calendar_state == "unknown"
    if not el["eligible"]:
        if has_entry:
            return {"action": "cancel", "reason": el["reason"], **el}
        return {"action": "none", "reason": el["reason"], **el}
    window = {"start": el["start"], "end": el["end"]}
    digest = content_hash(task, window)
    if not has_entry:
        return {"action": "create", "reason": "not on the calendar yet", "hash": digest, **el}
    if task.calendar_state in ("unknown", "failed"):
        return {"action": "repair", "reason": f"last write ended {task.calendar_state}", "hash": digest, **el}
    if int(task.calendar_synced_revision or 0) != int(task.schedule_revision or 1):
        return {"action": "patch", "reason": "rescheduled", "hash": digest, "time_changed": True, **el}
    if task.calendar_synced_hash != digest:
        return {"action": "patch", "reason": "the appointment's text or owner changed", "hash": digest, **el}
    return {"action": "none", "reason": "already on the calendar at this revision", "hash": digest, **el}


# ── recording on the task ───────────────────────────────────────────────────
def _mark(task: Task, state: str | None, *, error: str | None = None, event_id: str | None = None,
          calendar_id: str | None = None, link: str | None = None, revision: int | None = None,
          digest: str | None = None, synced_at: datetime | None = None) -> None:
    """Write the sync bookkeeping onto the task.

    Deliberately **not** `ctx.touch()`: these columns are not business content, and bumping `version`
    would invalidate an `expected_version` the owner is holding in an open task form (spec §12.1
    invariant 4) because a background calendar write happened."""
    task.calendar_state = state
    task.calendar_error = (error or None) and str(error)[:2000]
    if event_id is not None:
        task.calendar_event_id = event_id or None
    if calendar_id is not None:
        task.calendar_id = calendar_id or None
    if link is not None:
        task.calendar_link = link or None
    if revision is not None:
        task.calendar_synced_revision = revision
    if digest is not None:
        task.calendar_synced_hash = digest
    if synced_at is not None:
        task.calendar_synced_at = synced_at


async def _report_conflicts(db: AsyncSession, task: Task, window: dict, rows: list[dict]) -> None:
    """Record an overlapping entry on the task and tell the people the task reminds.

    The double booking is a fact about the owner's day, not a reason to refuse the appointment he
    asked for: the entry is still written and the clash is surfaced (spec §11.3 — report the
    conflict, do not silently decide)."""
    task.calendar_conflicts = rows[:CONFLICT_MAX]
    if not rows:
        return
    first = rows[0]
    tz = task.timezone or PHOENIX
    body = (f"{summary_for(task)} at {fmt_local(window['start'], tz)} "
            f"overlaps “{first.get('summary') or 'an existing entry'}”"
            + (f" ({fmt_local(_parse(first.get('start')), tz)}–{fmt_local(_parse(first.get('end')), tz, with_zone=False)})"
               if first.get("start") else "")
            + (f" and {len(rows) - 1} more." if len(rows) > 1 else "."))
    for u in await reminders.recipients_for_task(db, task):
        await reminders.notify(
            db, user_id=u.id, kind="calendar_conflict", urgency="today",
            title=f"Calendar clash: {summary_for(task)}", body=body,
            entity_kind="task", entity_id=task.id, deep_link=email_templates.deep_link("task", task.id),
            group_key=f"task:{task.id}",
            dedupe=f"calendar_conflict:{task.id}:{task.schedule_revision or 1}:{u.id}",
            payload={"conflicts": rows[:CONFLICT_MAX]})


def _parse(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return ensure_aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


async def _find_conflicts(ad, calendar_id: str, window: dict, *, skip_event_id: str, task_id: str) -> list[dict]:
    rows = await ad.list_events(calendar_id, time_min=window["start"], time_max=window["end"], max_results=20)
    out: list[dict] = []
    for ev in rows:
        if ev.get("id") == skip_event_id or ev.get("azkt_task_id") == task_id:
            continue
        if ev.get("status") == "cancelled":
            continue
        if ev.get("transparency") == "transparent":
            continue        # the owner marked that entry "free": it is not a clash
        if not cal.overlaps(window["start"], window["end"], ev.get("start"), ev.get("end")):
            continue
        out.append({"event_id": ev.get("id"), "summary": ev.get("summary") or "Busy",
                    "start": _iso(ev.get("start")), "end": _iso(ev.get("end")),
                    "html_link": ev.get("html_link"), "seen_at": _iso(now())})
    return out


# ── the one write path ──────────────────────────────────────────────────────
async def sync_task(db: AsyncSession, task: Task, *, cfg: dict | None = None) -> dict:
    """Make the calendar match this task. Never commits — the job (or the caller) owns the transaction.

    Raises so the durable queue can retry with backoff, and always records the truthful state on the
    task *before* it raises so the retry knows what happened last time.
    """
    cfg = cfg if cfg is not None else await config(db)
    plan = plan_for(task, cfg)
    if plan["action"] == "none":
        if not plan["eligible"] and task.calendar_state not in (None, "skipped"):
            _mark(task, "skipped", error=None)
        return {"action": "none", "reason": plan["reason"], "written": False, "task_id": task.id}

    try:
        ad = await adapter(db, cfg=cfg, write=True)
    except Unsupported as e:
        # setup_blocked is reported on the task and nothing is written; it is never a silent no-op and
        # never a retry loop, because no amount of retrying connects an account.
        _mark(task, "setup_blocked", error=e.message)
        return {"action": plan["action"], "reason": plan["reason"], "written": False,
                "state": "setup_blocked", "setup_blocked": (e.detail or {}).get("setup_blocked") or "calendar",
                "message": e.message, "task_id": task.id}

    calendar_id = task.calendar_id or cfg["calendar_id"]
    event_id = task.calendar_event_id or cal.event_id_for(task.id)

    try:
        if plan["action"] == "cancel":
            return await _cancel(db, task, ad, calendar_id, event_id, plan)
        return await _upsert(db, task, ad, calendar_id, event_id, plan, cfg)
    except Blocked as e:
        # the non-production destination guard (H08). Retrying cannot change an allowlist, so this is
        # recorded like any other setup problem instead of burning the job's retry budget.
        _mark(task, "setup_blocked", error=e.message)
        return {"action": plan["action"], "reason": plan["reason"], "written": False,
                "state": "setup_blocked", "setup_blocked": "calendar.destination", "message": e.message,
                "task_id": task.id}


async def _cancel(db: AsyncSession, task: Task, ad, calendar_id: str, event_id: str, plan: dict) -> dict:
    try:
        res = await ad.delete_event(calendar_id, event_id)
    except cal.UnknownWriteResult as e:
        _mark(task, "unknown", error=str(e), event_id=event_id, calendar_id=calendar_id)
        raise
    except (ProviderError, Unsupported) as e:
        _mark(task, "failed", error=str(getattr(e, "message", e)), event_id=event_id, calendar_id=calendar_id)
        raise
    _mark(task, "cancelled", error=None, event_id="", link="", revision=int(task.schedule_revision or 1),
          digest="", synced_at=now())
    task.calendar_conflicts = []
    log.info("calendar entry removed for task %s (%s)", task.id, plan["reason"])
    return {"action": "cancel", "reason": plan["reason"], "written": True, "state": "cancelled",
            "event_id": event_id, "already_absent": bool(res.get("already_absent")), "task_id": task.id}


async def _upsert(db: AsyncSession, task: Task, ad, calendar_id: str, event_id: str, plan: dict,
                  cfg: dict) -> dict:
    window = {"start": plan["start"], "end": plan["end"]}
    body = body_for(task, window, await owner_name_of(db, task))
    digest = plan.get("hash") or content_hash(task, window)

    existing = None
    if plan["action"] in ("patch", "repair"):
        # `repair` follows an unknown or failed write: look the deterministic id up first, so a write
        # the calendar actually accepted is adopted instead of made twice (spec §11.5.4/5).
        try:
            existing = await ad.get_event(calendar_id, event_id)
        except (ProviderError, Unsupported) as e:
            if plan["action"] == "patch":
                raise
            log.warning("calendar reconciliation read failed for task %s: %s", task.id, e)
            existing = None
        if existing is not None and existing.get("status") == "cancelled":
            existing = None     # somebody deleted it in Google: create it again below

    if cfg["conflict_check"] and (plan["action"] == "create" or plan.get("time_changed") or existing is None):
        try:
            await _report_conflicts(db, task, window, await _find_conflicts(
                ad, calendar_id, window, skip_event_id=event_id, task_id=task.id))
        except (ProviderError, Unsupported) as e:
            # a clash we could not look for is not a reason to skip the appointment; say so instead
            log.info("conflict check unavailable for task %s: %s", task.id, e)
            task.calendar_conflicts = [{"unavailable": True, "reason": str(getattr(e, "message", e)),
                                        "seen_at": _iso(now())}]

    try:
        if existing is not None:
            ev = await ad.patch_event(calendar_id, event_id, body)
            action = "patch"
        else:
            try:
                ev = await ad.insert_event(calendar_id, body, event_id=event_id)
                action = "create"
            except ProviderError as e:
                if cal.error_kind(e) != "conflict":
                    raise
                # the id is already on the calendar — a replay, or a write whose answer we lost. The
                # entry that exists IS this task's entry, so it is adopted and brought up to date.
                ev = await ad.patch_event(calendar_id, event_id, body)
                action = "adopt"
    except cal.UnknownWriteResult as e:
        _mark(task, "unknown", error=str(e), event_id=e.provider_ref or event_id, calendar_id=calendar_id)
        raise
    except (ProviderError, Unsupported) as e:
        _mark(task, "failed", error=str(getattr(e, "message", e)), calendar_id=calendar_id)
        raise

    _mark(task, "synced", error=None, event_id=ev.get("id") or event_id, calendar_id=calendar_id,
          link=ev.get("html_link") or "", revision=int(task.schedule_revision or 1), digest=digest,
          synced_at=now())
    return {"action": action, "reason": plan["reason"], "written": True, "state": "synced",
            "event_id": task.calendar_event_id, "calendar_id": calendar_id,
            "conflicts": len(task.calendar_conflicts or []), "task_id": task.id}


# ── events: subscribe to task changes ───────────────────────────────────────
def needs_attention(task: Task, cfg: dict | None = None) -> bool:
    """Cheap, no-network test used before anything is queued: an operational to-do never enters the
    calendar queue at all, and an appointment already synced at this revision does not either."""
    return plan_for(task, cfg)["action"] != "none"


@on_event("task.changed")
async def _on_task_changed(db: AsyncSession, ev) -> None:
    """Queue calendar work for a task that changed.

    The handler does no provider IO: it runs inside the outbox dispatcher's savepoint, where a slow
    or failing network call would hold up every other handler for the same event. Everything it
    decides is re-derived from the Task row inside the job, so a replay is harmless."""
    task_id = (ev.payload or {}).get("task_id") or ev.aggregate_id
    if not task_id:
        return
    t = await db.get(Task, task_id)
    if t is None:
        return
    await enqueue_sync(db, t, reason=(ev.payload or {}).get("change") or "changed")


async def enqueue_sync(db: AsyncSession, task: Task, *, reason: str = "changed") -> bool:
    """One durable job per (task, version). A replayed event dedupes onto the queued job; a genuinely
    new change gets its own, because the task version moved."""
    cfg = await config(db)
    if not needs_attention(task, cfg):
        return False
    job = await jobs.enqueue(db, "calendar.sync", {"task_id": task.id, "reason": reason},
                             dedupe_key=f"calendar:sync:{task.id}:{task.version}")
    return job is not None


@jobs.job("calendar.sync")
async def _sync_job(jctx: jobs.JobContext, payload: dict) -> dict:
    db = jctx.db
    task = await db.get(Task, payload.get("task_id"))
    if task is None:
        return {"skipped": "no task"}
    try:
        res = await sync_task(db, task)
    except Exception:
        # keep the truthful state sync_task recorded (unknown / failed) before the queue retries it;
        # the job runner rolls the session back on the way out, which would otherwise erase it.
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            await db.rollback()
        raise
    await db.commit()
    return res


@sweep("calendar.repair", REPAIR_SECONDS)
async def repair_sweep(session_factory) -> dict:
    """Due-work query: appointments whose calendar entry is missing, stale or in an unknown state.

    This is what makes a lost enqueue, a crashed worker or a restored backup self-healing, and it is
    also where an unknown write result gets another reconciliation attempt."""
    async with session_factory() as db:
        cfg = await config(db)
        try:
            await adapter(db, cfg=cfg, write=True)
        except Unsupported as e:
            return {"setup_blocked": (e.detail or {}).get("setup_blocked") or "calendar", "message": e.message}
        horizon_past = now() - timedelta(days=REPAIR_PAST_DAYS)
        horizon_future = now() + timedelta(days=REPAIR_FUTURE_DAYS)
        scheduled = or_(Task.due_at.between(horizon_past, horizon_future),
                        Task.start_at.between(horizon_past, horizon_future))
        rows = (await db.execute(
            select(Task).where(
                Task.type.in_(ALWAYS_TYPES + WINDOW_TYPES),
                or_(scheduled, Task.calendar_state.in_(("unknown", "failed"))))
            .order_by(Task.due_at.nulls_last()).limit(REPAIR_LIMIT))).scalars().all()
        queued = 0
        for t in rows:
            try:
                if await enqueue_sync(db, t, reason="repair"):
                    queued += 1
            except Exception:  # noqa: BLE001
                log.exception("calendar repair could not queue task %s", t.id)
        await db.commit()
        return {"considered": len(rows), "queued": queued}


# ── reads for the API ───────────────────────────────────────────────────────
def serialize_task_entry(task: Task, cfg: dict | None = None) -> dict:
    el = eligibility(task, cfg)
    return {
        "task_id": task.id, "title": task.title, "type": task.type, "status": task.status,
        "owner_user_id": task.owner_user_id, "timezone": task.timezone,
        "start": _iso(el["start"]), "end": _iso(el["end"]),
        "start_local": fmt_local(el["start"], task.timezone or PHOENIX) if el["start"] else None,
        "eligible": el["eligible"], "not_on_calendar_because": el["reason"] or None,
        "calendar_event_id": task.calendar_event_id, "calendar_id": task.calendar_id,
        "calendar_state": task.calendar_state, "calendar_link": task.calendar_link,
        "synced_revision": task.calendar_synced_revision, "schedule_revision": task.schedule_revision,
        "synced_at": _iso(task.calendar_synced_at), "error": task.calendar_error,
        "conflicts": list(task.calendar_conflicts or []),
        "deep_link": email_templates.deep_link("task", task.id),
    }


def day_bounds(day: datetime | None, tz: str) -> tuple[datetime, datetime]:
    """A local calendar day → [from, to) as UTC instants, so a Phoenix day and a Tokyo day are both
    the day the person actually sees."""
    zone = _zone(tz)
    local = (day or now()).astimezone(zone)
    start = datetime.combine(local.date(), time.min, tzinfo=zone)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


async def schedule_for_day(db: AsyncSession, *, day: datetime | None = None, tz: str = PHOENIX,
                           visible_task_ids: set[str] | None = None, include_calendar: bool = True) -> dict:
    """Today's schedule: AZKT's own appointments for the day, plus what the calendar itself holds.

    AZKT's records are authoritative for the list; the calendar block is context and is reported as
    `setup_blocked` (never as an empty day) when the connection or scope is missing."""
    cfg = await config(db)
    frm, to = day_bounds(day, tz)
    rows = (await db.execute(
        select(Task).where(Task.type.in_(ALWAYS_TYPES + WINDOW_TYPES),
                           or_(Task.due_at.between(frm, to), Task.start_at.between(frm, to)))
        .order_by(Task.start_at.nulls_last(), Task.due_at.nulls_last()).limit(200))).scalars().all()
    if visible_task_ids is not None:
        rows = [t for t in rows if t.id in visible_task_ids]
    tasks = [serialize_task_entry(t, cfg) for t in rows]
    out = {"from": _iso(frm), "to": _iso(to), "timezone": tz, "tasks": tasks,
           "conflicts": sum(len(t["conflicts"]) for t in tasks),
           "calendar": {"available": False, "setup_blocked": None, "events": []}}
    if not include_calendar:
        return out
    try:
        ad = await adapter(db, cfg=cfg, write=False)
        events = await ad.list_events(cfg["calendar_id"], time_min=frm, time_max=to, max_results=50)
    except Unsupported as e:
        out["calendar"]["setup_blocked"] = e.message
        return out
    except Exception as e:  # noqa: BLE001 - a provider hiccup is context missing, not an empty day
        out["calendar"]["setup_blocked"] = f"the calendar could not be read: {getattr(e, 'message', e)}"
        return out
    out["calendar"] = {"available": True, "setup_blocked": None, "calendar_id": cfg["calendar_id"],
                       "events": [{**e, "start": _iso(e["start"]), "end": _iso(e["end"])} for e in events]}
    return out


async def status(db: AsyncSession) -> dict:
    """Connection + capability + what the sync is actually doing, for Settings → Calendar."""
    cfg = await config(db)
    conn = await connection(db)
    serialized = conn_svc.serialize(conn, PROVIDER)
    granted = list((conn.granted_scopes if conn else None) or [])
    write_blocked = None
    read_blocked = None
    try:
        await adapter(db, cfg=cfg, write=True)
    except Unsupported as e:
        write_blocked = {"reason": e.message, "setup_blocked": (e.detail or {}).get("setup_blocked") or "calendar"}
    try:
        await adapter(db, cfg=cfg, write=False)
    except Unsupported as e:
        read_blocked = {"reason": e.message, "setup_blocked": (e.detail or {}).get("setup_blocked") or "calendar"}
    counts = dict((s or "none", int(n)) for s, n in (await db.execute(
        select(Task.calendar_state, func.count()).where(Task.type.in_(ALWAYS_TYPES + WINDOW_TYPES))
        .group_by(Task.calendar_state))).all())
    problems = [serialize_task_entry(t, cfg) for t in (await db.execute(
        select(Task).where(Task.calendar_state.in_(("failed", "unknown", "setup_blocked")))
        .order_by(Task.calendar_synced_at.desc().nulls_last()).limit(20))).scalars().all()]
    entry = await settings_store.get_entry(db, SETTING_KEY)
    return {
        "connection": serialized,
        "connected": bool(conn and conn.status not in ("disconnected",)),
        "writes_enabled": cfg["writes_enabled"],
        "write_scope_granted": (cal.WRITE_SCOPE in granted) if granted else None,
        "read_scope_granted": (cal.READ_SCOPE in granted or cal.WRITE_SCOPE in granted) if granted else None,
        "calendar_id": cfg["calendar_id"], "conflict_check": cfg["conflict_check"],
        "durations": cfg["minutes"],
        "read_blocked": read_blocked, "write_blocked": write_blocked,
        "rules": {"always": list(ALWAYS_TYPES), "with_a_block": list(WINDOW_TYPES), "never": list(NEVER_TYPES)},
        "counts": counts, "problems": problems,
        "setting_version": entry["version"],
        "scopes": {"read": cal.READ_SCOPE, "write": cal.WRITE_SCOPE},
    }


# ── commands (owner-only: the capability, and a manual re-sync) ─────────────
class CalendarWritesIn(BaseModel):
    enabled: bool
    calendar_id: str | None = None
    conflict_check: bool | None = None
    expected_version: int | None = None


@command("calendar.set_writes", input=CalendarWritesIn, perm="settings", action_class="owner_only",
         approval_kind="other", summary=lambda p: f"{'Allow' if p.enabled else 'Stop'} AZKT creating calendar events",
         description="Owner capability switch for calendar writes (spec §11.2). Off by default: a connected "
                     "Google account alone never lets AZKT create an event.")
async def calendar_set_writes(ctx: CommandContext, inp: CalendarWritesIn) -> dict:
    patch: dict = {"writes_enabled": bool(inp.enabled)}
    if inp.calendar_id is not None:
        patch["calendar_id"] = inp.calendar_id.strip()
    if inp.conflict_check is not None:
        patch["conflict_check"] = bool(inp.conflict_check)
    if inp.enabled:
        # turning the capability on is pointless (and misleading in the UI) without an account to write
        # to: say which half is missing instead of storing a switch that does nothing.
        conn = await connection(ctx.db)
        if conn is None or conn.status == "disconnected":
            raise Blocked("connect the business Google Calendar first; the switch would have nothing to write to",
                          setup_blocked="calendar.oauth")
        granted = list(conn.granted_scopes or [])
        if granted and cal.WRITE_SCOPE not in granted:
            raise Blocked("this calendar connection was granted read access only; reconnect it and allow "
                          "AZKT to create events", setup_blocked="calendar.write_scope")
    res = await dispatch(ctx.child(), "settings.update",
                         {"key": SETTING_KEY, "value": patch, "expected_version": inp.expected_version},
                         commit=False)
    if res.status != "ok":
        return res.to_dict()
    ctx.record(f"{'Allowed' if inp.enabled else 'Stopped'} calendar event creation", entity_kind="setting",
               entity_id=SETTING_KEY, kind="system", state="enabled" if inp.enabled else "disabled",
               visibility="owner", details={"calendar_id": patch.get("calendar_id")})
    ctx.emit("calendar.capability_changed", aggregate_type="setting", aggregate_id=SETTING_KEY,
             payload={"writes_enabled": bool(inp.enabled), "calendar_id": patch.get("calendar_id")})
    return {"calendar": (res.data or {}).get("value") or {}, "version": (res.data or {}).get("version"),
            "writes_enabled": bool(inp.enabled)}


class CalendarResyncIn(BaseModel):
    task_id: str | None = None
    limit: int = Field(default=50, ge=1, le=REPAIR_LIMIT)


@command("calendar.resync", input=CalendarResyncIn, perm="settings", action_class="owner_only",
         approval_kind="other", summary=lambda p: "Re-sync the calendar",
         description="Queue calendar work for one task, or for every appointment that is out of sync. "
                     "Queues durable jobs; it never writes to the calendar inside the request.")
async def calendar_resync(ctx: CommandContext, inp: CalendarResyncIn) -> dict:
    cfg = await config(ctx.db)
    try:
        await adapter(ctx.db, cfg=cfg, write=True)
    except Unsupported as e:
        raise Blocked(e.message, setup_blocked=(e.detail or {}).get("setup_blocked") or "calendar")
    if inp.task_id:
        t = await ctx.db.get(Task, inp.task_id)
        if t is None:
            raise NotFound("task not found")
        el = eligibility(t, cfg)
        if not el["eligible"] and not t.calendar_event_id:
            raise ValidationFailed(f"this task is not an appointment: {el['reason']}")
        queued = 1 if await enqueue_sync(ctx.db, t, reason="owner re-sync") else 0
        return {"queued": queued, "task_id": t.id, "already_queued": queued == 0}
    horizon = now() - timedelta(days=REPAIR_PAST_DAYS)
    rows = (await ctx.db.execute(
        select(Task).where(Task.type.in_(ALWAYS_TYPES + WINDOW_TYPES),
                           or_(Task.due_at >= horizon, Task.start_at >= horizon,
                               Task.calendar_state.in_(("unknown", "failed", "setup_blocked"))))
        .order_by(Task.due_at.nulls_last()).limit(inp.limit))).scalars().all()
    queued = 0
    for t in rows:
        if await enqueue_sync(ctx.db, t, reason="owner re-sync"):
            queued += 1
    ctx.record(f"Queued a calendar re-sync ({queued} of {len(rows)} appointments)", entity_kind="setting",
               entity_id=SETTING_KEY, kind="system", state="queued", visibility="owner")
    return {"queued": queued, "considered": len(rows)}
