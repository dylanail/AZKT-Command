"""Durable reminders: scheduling, claiming, delivery and truthful states (spec §5.4, §5.6, §12.4).

Scheduling is event-driven (`@on_event("task.changed")`) *and* repaired by a periodic sweep, so a lost
enqueue never loses a schedule. Delivery is a worker sweep that claims rows with
`FOR UPDATE SKIP LOCKED`, re-checks the current task state/revision/snooze, renders one of the four
designed emails (services/email_templates.py) or a Telegram message, and records a truthful state:

    scheduled -> claimed -> accepted   (provider took it; NOT proof Dylan saw it)
                         -> delivered  (only when the provider gives delivery evidence)
                         -> failed | unknown | cancelled | superseded | expired

Unique delivery key: ``{task}:{revision}:{kind}:{recipient}:{channel}`` (spec §5.6).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.errors import ProviderError, Unsupported
from ..core.ids import new_id
from ..core.time import PHOENIX, ensure_aware, fmt_local, reminder_fire_at, to_zone
from ..domain.events import on_event
from ..domain.jobs import sweep
from ..models import User
from ..models.legacy import Setting
from ..models.notify import Notification, ScheduledDelivery, TelegramPairing
from ..models.runtime import ActivityEntry, Approval
from ..models.tasks import Task
from . import email_templates
from .team import effective_notification_prefs

log = logging.getLogger("azkt.reminders")

ACTIONABLE = ("open", "in_progress", "blocked", "waiting")
CLOSED = ("completed", "cancelled")
CHANNELS_FOR_MODE = {"email_only": ("email",), "telegram_email": ("telegram", "email"),
                     "telegram_fallback_email": ("telegram",)}
DIGEST_WINDOW_MINUTES = 120           # send the digest inside this window after the configured local time
DIGEST_STATE_KEY = "reminders.digest_state"
CLAIM_BATCH = 50
LEASE_SECONDS = 120


def now() -> datetime:
    return datetime.now(timezone.utc)


# ── configuration ───────────────────────────────────────────────────────────
async def config(db: AsyncSession) -> dict:
    """Owner-editable reminder settings merged over the engineering defaults in core/config.py."""
    base = {"late_grace_minutes": settings.REMINDER_LATE_GRACE_MINUTES,
            "obsolete_after_hours": settings.REMINDER_OBSOLETE_HOURS,
            "overdue_delay_minutes": settings.OVERDUE_EMAIL_DELAY_MINUTES,
            "task_reminder_enabled": True, "overdue_enabled": True, "digest_enabled": True,
            "digest_local_time": settings.DIGEST_DEFAULT_LOCAL_TIME, "digest_timezone": PHOENIX,
            "digest_non_empty_only": True, "deposit_enabled": True, "employee_reminders_enabled": False,
            "channels": {}}
    try:
        from . import settings_store
        eff = await settings_store.get_effective(db, "reminders")
    except Exception:  # noqa: BLE001  - settings module absent or corrupt: engineering defaults still work
        return base
    base.update({
        "late_grace_minutes": int(eff.get("late_grace_minutes", base["late_grace_minutes"])),
        "obsolete_after_hours": int(eff.get("obsolete_after_hours", base["obsolete_after_hours"])),
        "overdue_delay_minutes": int((eff.get("overdue") or {}).get("delay_minutes", base["overdue_delay_minutes"])),
        "task_reminder_enabled": bool((eff.get("task_reminder") or {}).get("enabled", True)),
        "overdue_enabled": bool((eff.get("overdue") or {}).get("enabled", True)),
        "digest_enabled": bool((eff.get("digest") or {}).get("enabled", True)),
        "digest_local_time": (eff.get("digest") or {}).get("local_time", base["digest_local_time"]),
        "digest_timezone": (eff.get("digest") or {}).get("timezone", PHOENIX),
        "digest_non_empty_only": bool((eff.get("digest") or {}).get("non_empty_only", True)),
        "deposit_enabled": bool((eff.get("deposit_confirmed") or {}).get("enabled", True)),
        "employee_reminders_enabled": bool(eff.get("employee_reminders_enabled", False)),
        "channels": dict(eff.get("channels") or {}),
    })
    return base


def channels_for(user: User, kind: str, business_channels: dict | None = None) -> tuple[str, ...]:
    """Channels for one person and one reminder kind. The person's own preference wins over the
    business default; `telegram_fallback_email` means Telegram now and email only if Telegram fails."""
    prefs = effective_notification_prefs(user.notification_prefs)
    mode = (prefs.get("channels") or {}).get(kind) or (business_channels or {}).get(kind) or "email_only"
    out = list(CHANNELS_FOR_MODE.get(mode, ("email",)))
    if inapp_enabled(user, kind) and kind in ("case_update", "connection_issue", "deposit_confirmed"):
        out.append("inapp")
    return tuple(out)


def fallback_mode(user: User, kind: str) -> bool:
    prefs = effective_notification_prefs(user.notification_prefs)
    return (prefs.get("channels") or {}).get(kind) == "telegram_fallback_email"


def inapp_enabled(user: User, kind: str | None = None) -> bool:
    """In-app bell items. `notification_prefs.inapp` is honoured when the Settings UI starts writing it;
    until then in-app is on (it is the app's own surface, not an outbound send)."""
    prefs = user.notification_prefs or {}
    val = prefs.get("inapp")
    if isinstance(val, dict) and kind:
        return bool(val.get(kind, True))
    if isinstance(val, bool):
        return val
    return True


def quiet_hours_for(user: User) -> tuple[time, time] | None:
    qh = (effective_notification_prefs(user.notification_prefs) or {}).get("quiet_hours")
    if not qh:
        return None
    try:
        return (_parse_hhmm(qh["start"]), _parse_hhmm(qh["end"]))
    except Exception:  # noqa: BLE001
        return None


def _parse_hhmm(v: str) -> time:
    h, m = str(v).split(":")
    return time(int(h), int(m))


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or PHOENIX)
    except Exception:  # noqa: BLE001
        return ZoneInfo(PHOENIX)


def apply_quiet_hours(user: User, at: datetime, kind: str) -> datetime:
    """Quiet hours delay low-urgency deliveries only; a timed task reminder is never held back."""
    if kind not in ("digest", "connection_issue", "case_update"):
        return at
    window = quiet_hours_for(user)
    if not window:
        return at
    start, end = window
    tz = _zone(user.timezone)
    local = at.astimezone(tz)
    t = local.time()
    inside = (start <= t or t < end) if start > end else (start <= t < end)
    if not inside:
        return at
    target = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if target <= local:
        target = target + timedelta(days=1)
    return target.astimezone(timezone.utc)


# ── recipients ──────────────────────────────────────────────────────────────
async def owner_users(db: AsyncSession) -> list[User]:
    return list((await db.execute(select(User).where(User.role == "owner", User.status == "active")
                                  .order_by(User.created_at))).scalars().all())


def is_sales_task(t: Task) -> bool:
    return bool(t.opportunity_id or t.contact_id) or t.type in ("call", "meeting", "follow_up")


async def recipients_for_task(db: AsyncSession, t: Task) -> list[User]:
    """The task owner, plus Dylan (the business owner) for sales tasks — spec §5.4 sends reminders to
    the owner's configured destination. An unassigned task still reaches the owner."""
    out: dict[str, User] = {}
    if t.owner_user_id:
        u = await db.get(User, t.owner_user_id)
        if u is not None and u.status == "active":
            out[u.id] = u
    if not out or is_sales_task(t):
        for o in await owner_users(db):
            out.setdefault(o.id, o)
    return list(out.values())


def recipient_email(u: User) -> str | None:
    if u.role == "owner":
        return u.reminder_email or u.email or (settings.OWNER_REMINDER_EMAIL or None)
    return u.reminder_email or u.email


# ── scheduling ──────────────────────────────────────────────────────────────
def dedupe_key(task_id: str, revision: int, kind: str, recipient: str, channel: str) -> str:
    return f"{task_id}:{revision}:{kind}:{recipient}:{channel}"


async def _ensure_delivery(db: AsyncSession, *, dedupe: str, **kw) -> tuple[ScheduledDelivery | None, bool]:
    existing = (await db.execute(select(ScheduledDelivery).where(ScheduledDelivery.dedupe_key == dedupe))).scalar_one_or_none()
    if existing is not None:
        return existing, False
    row = ScheduledDelivery(dedupe_key=dedupe, state="scheduled", attempts=0, receipt={}, **kw)
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        try:
            db.expunge(row)
        except Exception:  # noqa: BLE001
            pass
        again = (await db.execute(select(ScheduledDelivery).where(ScheduledDelivery.dedupe_key == dedupe))).scalar_one_or_none()
        return again, False
    return row, True


async def _supersede(db: AsyncSession, task: Task, *, reason: str, all_revisions: bool = False) -> int:
    q = select(ScheduledDelivery).where(ScheduledDelivery.task_id == task.id,
                                        ScheduledDelivery.state.in_(("scheduled", "claimed")))
    if not all_revisions:
        q = q.where(or_(ScheduledDelivery.task_revision.is_(None),
                        ScheduledDelivery.task_revision != (task.schedule_revision or 1)))
    rows = (await db.execute(q)).scalars().all()
    for r in rows:
        r.state = "cancelled" if all_revisions else "superseded"
        r.cancel_reason = reason
        r.lease_token = None
    return len(rows)


async def schedule_for_task(db: AsyncSession, task: Task) -> dict:
    """(Re)compute the scheduled deliveries for one task. Idempotent: existing rows for the current
    revision are kept, older revisions are superseded, and a closed task cancels everything."""
    cfg = await config(db)
    created: list[str] = []
    revision = task.schedule_revision or 1
    if task.status in CLOSED:
        n = await _supersede(db, task, reason=f"task {task.status}", all_revisions=True)
        return {"task_id": task.id, "created": [], "cancelled": n, "reason": task.status}
    superseded = await _supersede(db, task, reason="superseded by a newer schedule revision")

    due = ensure_aware(task.due_at)
    ends = ensure_aware(task.end_at) or due
    right_now = now()
    recipients = await recipients_for_task(db, task)

    # 1 · task reminder at the chosen offset (or at the snoozed time)
    fire: datetime | None = None
    if due and task.reminder_kind and cfg["task_reminder_enabled"]:
        snooze = ensure_aware(task.snoozed_until)
        if snooze:
            fire = snooze
        else:
            fire = reminder_fire_at(due, task.reminder_kind, task.reminder_custom_minutes)
            if fire and fire < right_now:
                # the offset already passed but the appointment has not: remind now, not "in 15 minutes"
                fire = right_now if (ends or due) > right_now else None
        if fire is not None and (ends or due) <= right_now and not snooze:
            fire = None   # the call/meeting already ended: never a misleading "starts in 15 minutes"
    if fire is not None:
        for u in recipients:
            for ch in channels_for(u, "task_reminder", cfg["channels"]):
                key = dedupe_key(task.id, revision, "task_reminder", u.id, ch)
                row, made = await _ensure_delivery(
                    db, dedupe=key, kind="task_reminder", task_id=task.id, task_revision=revision,
                    entity_kind="task", entity_id=task.id, recipient_user_id=u.id, channel=ch,
                    deliver_at=apply_quiet_hours(u, fire, "task_reminder"),
                    payload={"title": task.title, "due_at": due.isoformat() if due else None,
                             "reminder_kind": task.reminder_kind, "snoozed": bool(task.snoozed_until)})
                if made:
                    created.append(key)

    # 2 · one overdue email per task revision, one hour after the time passes
    if due and cfg["overdue_enabled"] and task.status in ACTIONABLE:
        overdue_at = due + timedelta(minutes=cfg["overdue_delay_minutes"])
        for u in recipients:
            for ch in channels_for(u, "overdue", cfg["channels"]):
                key = dedupe_key(task.id, revision, "overdue", u.id, ch)
                row, made = await _ensure_delivery(
                    db, dedupe=key, kind="overdue", task_id=task.id, task_revision=revision,
                    entity_kind="task", entity_id=task.id, recipient_user_id=u.id, channel=ch,
                    deliver_at=overdue_at,
                    payload={"title": task.title, "due_at": due.isoformat()})
                if made:
                    created.append(key)
    return {"task_id": task.id, "created": created, "superseded": superseded, "revision": revision}


@on_event("task.changed")
async def _on_task_changed(db: AsyncSession, ev) -> None:
    task_id = (ev.payload or {}).get("task_id") or ev.aggregate_id
    if not task_id:
        return
    t = await db.get(Task, task_id)
    if t is None:
        return
    await schedule_for_task(db, t)


# ── periodic sweeps (registered with the foundation @sweep decorator) ────────
@sweep("reminders.repair", 300)
async def repair_missing(session_factory) -> dict:
    """Due-work query: re-create deliveries for actionable scheduled tasks whose rows are missing
    (a lost enqueue, a crashed worker, a restored backup). Idempotent by dedupe key."""
    async with session_factory() as db:
        cfg = await config(db)
        horizon_past = now() - timedelta(hours=max(cfg["obsolete_after_hours"], 24))
        rows = (await db.execute(
            select(Task).where(Task.status.in_(ACTIONABLE), Task.due_at.is_not(None),
                               Task.due_at >= horizon_past, Task.due_at <= now() + timedelta(days=60))
            .order_by(Task.due_at).limit(200))).scalars().all()
        made = 0
        for t in rows:
            try:
                res = await schedule_for_task(db, t)
                made += len(res.get("created") or [])
            except Exception:  # noqa: BLE001
                log.exception("repair_missing failed for task %s", t.id)
        await db.commit()
        return {"tasks": len(rows), "created": made}


@sweep("reminders.digest", 300)
async def digest_sweep(session_factory) -> dict:
    """Once a day, at the person's configured local time, when there is something to say."""
    async with session_factory() as db:
        cfg = await config(db)
        if not cfg["digest_enabled"]:
            return {"skipped": "digest disabled"}
        state_row = await db.get(Setting, DIGEST_STATE_KEY)
        state = dict((state_row.value or {}).get("last_sent") or {}) if state_row else {}
        candidates = await owner_users(db)
        if cfg["employee_reminders_enabled"]:
            others = (await db.execute(select(User).where(User.status == "active", User.role != "owner"))).scalars().all()
            candidates = candidates + list(others)
        sent = []
        for u in candidates:
            prefs = effective_notification_prefs(u.notification_prefs)
            local_time = prefs.get("digest_time") or cfg["digest_local_time"]
            tz = _zone(u.timezone or cfg["digest_timezone"])
            local = now().astimezone(tz)
            try:
                target = _parse_hhmm(local_time)
            except Exception:  # noqa: BLE001
                target = _parse_hhmm(cfg["digest_local_time"])
            start = local.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
            if not (start <= local < start + timedelta(minutes=DIGEST_WINDOW_MINUTES)):
                continue
            today_key = local.date().isoformat()
            if state.get(u.id) == today_key:
                continue
            content = await digest_content(db, u)
            if cfg["digest_non_empty_only"] and not _digest_has_content(content):
                continue
            for ch in channels_for(u, "digest", cfg["channels"]):
                key = f"digest:{u.id}:{today_key}:{ch}"
                row, made = await _ensure_delivery(
                    db, dedupe=key, kind="digest", task_id=None, task_revision=None, entity_kind="user",
                    entity_id=u.id, recipient_user_id=u.id, channel=ch,
                    deliver_at=apply_quiet_hours(u, now(), "digest"), payload={"date": today_key, **content})
                if made:
                    sent.append(key)
            state[u.id] = today_key
        if sent:
            if state_row is None:
                state_row = Setting(key=DIGEST_STATE_KEY, value={})
                db.add(state_row)
            state_row.value = {"last_sent": state}
            state_row.updated_at = now()
        await db.commit()
        return {"queued": sent}


def _digest_has_content(c: dict) -> bool:
    return bool(c.get("overdue") or c.get("today") or c.get("approvals"))


async def digest_content(db: AsyncSession, u: User) -> dict:
    """Overdue, today's work, waiting approvals and a tomorrow preview — all deterministic."""
    tz = _zone(u.timezone)
    local = now().astimezone(tz)
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    tomorrow_end = day_end + timedelta(days=1)
    q = select(Task).where(Task.status.in_(ACTIONABLE), Task.due_at.is_not(None))
    if u.role != "owner":
        q = q.where(Task.owner_user_id == u.id)
    rows = (await db.execute(q.order_by(Task.due_at).limit(200))).scalars().all()
    overdue, today, tomorrow = [], [], []
    for t in rows:
        due = ensure_aware(t.due_at)
        if due < now():
            overdue.append({"task_id": t.id, "text": f"{t.title} · was {fmt_local(due, t.timezone or PHOENIX)}"})
        elif day_start.astimezone(timezone.utc) <= due < day_end.astimezone(timezone.utc):
            today.append({"task_id": t.id, "time": to_zone(due, t.timezone or PHOENIX).strftime("%H:%M"),
                          "text": t.title})
        elif day_end.astimezone(timezone.utc) <= due < tomorrow_end.astimezone(timezone.utc):
            tomorrow.append({"task_id": t.id, "text": f"{t.title} · {fmt_local(due, t.timezone or PHOENIX)}"})
    approvals = []
    from ..domain.policy import ROLE_DEFAULTS
    can_approve = u.role == "owner" or bool((u.perms or {}).get("approve"))
    if can_approve:
        arows = (await db.execute(select(Approval).where(Approval.status == "pending")
                                  .order_by(Approval.created_at).limit(25))).scalars().all()
        for a in arows:
            when = ensure_aware(a.expires_at)
            approvals.append({"approval_id": a.id,
                              "text": a.title + (f" · expires {fmt_local(when, u.timezone or PHOENIX)}" if when else "")})
    return {"overdue": overdue[:10], "today": today[:10], "approvals": approvals[:10], "tomorrow": tomorrow[:5]}


@sweep("reminders.deliver_due", 15)
async def deliver_due(session_factory) -> dict:
    """Claim due rows (FOR UPDATE SKIP LOCKED), re-check the record, send, record the receipt."""
    claimed: list[str] = []
    async with session_factory() as db:
        rows = (await db.execute(
            select(ScheduledDelivery).where(ScheduledDelivery.state == "scheduled",
                                            ScheduledDelivery.deliver_at <= now())
            .order_by(ScheduledDelivery.deliver_at).limit(CLAIM_BATCH).with_for_update(skip_locked=True)
        )).scalars().all()
        token = new_id()
        for r in rows:
            r.state = "claimed"
            r.lease_token = token
            r.lease_until = now() + timedelta(seconds=LEASE_SECONDS)
            r.attempts = (r.attempts or 0) + 1
            claimed.append(r.id)
        await db.commit()
    out = {"claimed": len(claimed), "sent": 0, "cancelled": 0, "failed": 0, "collapsed": 0, "unknown": 0}
    for rid in claimed:
        async with session_factory() as db:
            try:
                res = await _deliver_one(db, rid)
                await db.commit()
            except Exception as e:  # noqa: BLE001
                await db.rollback()
                log.exception("delivery %s failed", rid)
                async with session_factory() as db2:
                    row = await db2.get(ScheduledDelivery, rid)
                    if row is not None and row.state == "claimed":
                        row.state = "failed"
                        row.last_error = f"{type(e).__name__}: {e}"[:1000]
                        row.lease_token = None
                        await db2.commit()
                res = "failed"
            out[res] = out.get(res, 0) + 1
    return out


async def _deliver_one(db: AsyncSession, delivery_id: str) -> str:
    row = (await db.execute(select(ScheduledDelivery).where(ScheduledDelivery.id == delivery_id)
                            .with_for_update())).scalar_one_or_none()
    if row is None or row.state != "claimed":
        return "cancelled"
    cfg = await config(db)
    right_now = now()
    user = await db.get(User, row.recipient_user_id)
    if user is None or user.status != "active":
        return _fail(row, "recipient is not an active person")

    task: Task | None = await db.get(Task, row.task_id) if row.task_id else None
    obsolete = _obsolete_reason(row, task, right_now)
    if obsolete:
        row.state = "cancelled"
        row.cancel_reason = obsolete
        row.lease_token = None
        return "cancelled"

    age_minutes = (right_now - ensure_aware(row.deliver_at)).total_seconds() / 60
    if age_minutes > cfg["obsolete_after_hours"] * 60:
        row.state = "expired"
        row.cancel_reason = "older than the obsolete window; collapsed into the next digest"
        row.lease_token = None
        return "collapsed"
    late = age_minutes > cfg["late_grace_minutes"]

    ctx = await build_context(db, row, task, user, late=late)
    if row.channel == "email":
        return await _send_email(db, row, user, ctx, late)
    if row.channel == "telegram":
        return await _send_telegram(db, row, user, ctx, task, late)
    if row.channel == "inapp":
        return await _send_inapp(db, row, user, ctx, late)
    return _fail(row, f"unknown channel {row.channel}")


def _obsolete_reason(row: ScheduledDelivery, task: Task | None, right_now: datetime) -> str | None:
    if row.task_id and task is None:
        return "task no longer exists"
    if task is None:
        return None
    if (task.schedule_revision or 1) != (row.task_revision or 1):
        return f"superseded: task is at revision {task.schedule_revision}"
    if task.status in CLOSED:
        return f"task is {task.status}"
    snooze = ensure_aware(task.snoozed_until)
    if snooze and snooze > right_now and row.kind == "task_reminder":
        return "snoozed to a later time"
    if row.kind == "overdue":
        due = ensure_aware(task.due_at)
        if due is None or due > right_now:
            return "task is no longer overdue"
        if task.status not in ACTIONABLE:
            return f"task is {task.status}"
    if row.kind == "task_reminder":
        end = ensure_aware(task.end_at) or ensure_aware(task.due_at)
        if end and end <= right_now and row.payload.get("reminder_kind") not in (None, "at"):
            return "the appointment already ended"
    return None


def _fail(row: ScheduledDelivery, reason: str) -> str:
    row.state = "failed"
    row.last_error = reason[:1000]
    row.lease_token = None
    return "failed"


# ── rendering context ───────────────────────────────────────────────────────
async def build_context(db: AsyncSession, row: ScheduledDelivery, task: Task | None, user: User,
                        *, late: bool = False) -> dict:
    ctx: dict = {"now": now(), "late": late, "owner": user.role == "owner",
                 "timezone": (task.timezone if task else None) or user.timezone or PHOENIX,
                 "kind": row.kind, "entity_kind": row.entity_kind, "entity_id": row.entity_id}
    if row.kind == "digest":
        ctx.update({k: row.payload.get(k) or [] for k in ("overdue", "today", "approvals", "tomorrow")})
        return ctx
    if row.kind == "deposit_confirmed":
        ctx.update(dict(row.payload or {}))
        ctx["now"] = now()
        ctx["owner"] = user.role == "owner"
        ctx["confirmed_at"] = _parse_dt(row.payload.get("confirmed_at"))
        ctx["link"] = email_templates.deep_link(row.entity_kind, row.entity_id)
        return ctx
    if row.kind == "connection_issue":
        ctx.update(dict(row.payload or {}))
        return ctx
    if task is None:
        ctx.update(dict(row.payload or {}))
        return ctx
    ctx.update({"task_id": task.id, "title": task.title, "due_at": ensure_aware(task.due_at),
                "note": (task.notes or "").strip().splitlines()[0][:160] if task.notes else None,
                "offset_note": _offset_note(task)})
    ctx["link"] = email_templates.deep_link("task", task.id)
    ctx["button_label"] = "Open task in AZKT"
    if task.owner_user_id and task.owner_user_id != user.id:
        owner_row = await db.get(User, task.owner_user_id)
        if owner_row is not None:
            ctx["owner_name"] = owner_row.display_name or owner_row.handle
    if task.opportunity_id:
        try:
            from ..models.sales import Opportunity
            o = await db.get(Opportunity, task.opportunity_id)
            if o is not None:
                ctx["pipeline"] = f"{'IRQ' if o.pipeline == 'irq' else 'Vehicle Sales'} · {o.stage.replace('_', ' ').title()}"
                ctx["looking_for"] = (o.enquiry or "").strip()[:160] or None
                ctx["amount"], ctx["currency"] = o.budget_amount, o.budget_currency
                ctx["link"] = email_templates.deep_link("opportunity", o.id)
                ctx["button_label"] = "Open lead in AZKT"
        except Exception:  # noqa: BLE001
            pass
    if task.vehicle_id:
        try:
            from ..models.vehicles import Vehicle
            from .vehicles import vehicle_title
            v = await db.get(Vehicle, task.vehicle_id)
            if v is not None:
                ctx["vehicle"] = f"{vehicle_title(v)} · {v.stock_no}" if v.stock_no else vehicle_title(v)
                if not task.opportunity_id:
                    ctx["link"] = email_templates.deep_link("vehicle", v.id, tab="sale")
                    ctx["button_label"] = "Open vehicle in AZKT"
        except Exception:  # noqa: BLE001
            pass
    if task.contact_id:
        try:
            from ..models.contacts import Contact
            c = await db.get(Contact, task.contact_id)
            if c is not None:
                ctx["contact"] = c.display_name or c.primary_phone or c.primary_email or None
        except Exception:  # noqa: BLE001
            pass
    return ctx


def _offset_note(task: Task) -> str:
    labels = {"at": "at the scheduled time", "15m": "15 minutes before", "1h": "1 hour before", "1d": "1 day before"}
    if task.reminder_kind == "custom":
        return f"You set this reminder for {task.reminder_custom_minutes or 30} minutes before. Change reminder timing in Sales › Tasks."
    if task.reminder_kind in labels:
        return f"You set this reminder for {labels[task.reminder_kind]}. Change reminder timing in Sales › Tasks."
    return "Change reminder timing in Sales › Tasks."


def _parse_dt(v) -> datetime | None:
    if isinstance(v, datetime):
        return ensure_aware(v)
    if isinstance(v, str) and v:
        try:
            return ensure_aware(datetime.fromisoformat(v.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


# ── channels ────────────────────────────────────────────────────────────────
async def _send_email(db: AsyncSession, row: ScheduledDelivery, user: User, ctx: dict, late: bool) -> str:
    from ..adapters import email as email_adapter
    to = recipient_email(user)
    if not to:
        return _fail(row, "setup_blocked: no verified reminder email configured for this person")
    template = row.kind if row.kind in email_templates.KINDS else "task_reminder"
    if row.kind == "connection_issue":
        subject = ctx.get("subject") or "A connection needs attention"
        text = ctx.get("body") or subject
        html = None
    else:
        subject, text, html = email_templates.render(template, ctx)
    msg = email_adapter.OutboundEmail(to=[to], subject=subject, text=text, html=html,
                                      headers={"X-AZKT-Delivery": row.id, "X-AZKT-Kind": row.kind,
                                               "Auto-Submitted": "auto-generated"})
    try:
        receipt = await email_adapter.send(msg)
    except Unsupported as e:
        return _fail(row, f"setup_blocked: {e}")
    except ProviderError as e:
        if getattr(e, "detail", {}).get("kind") == "transient" or "transient" in str(e):
            row.state = "unknown"
            row.last_error = f"send result unknown: {e}"[:1000]
            row.lease_token = None
            return "unknown"
        return _fail(row, f"{e}")
    except Exception as e:  # noqa: BLE001
        row.state = "unknown"
        row.last_error = f"send result unknown: {type(e).__name__}: {e}"[:1000]
        row.lease_token = None
        return "unknown"
    row.provider_ref = receipt.get("provider_ref")
    row.receipt = {**receipt, "subject": subject, "to": to}
    row.sent_at = now()
    row.late = late
    row.lease_token = None
    if not receipt.get("accepted"):
        row.state = "failed"
        row.last_error = receipt.get("note") or "transport did not accept the message"
        return "failed"
    row.state = "accepted"     # provider acceptance is not proof Dylan saw it (spec §5.4)
    return "sent"


async def _send_telegram(db: AsyncSession, row: ScheduledDelivery, user: User, ctx: dict, task: Task | None,
                         late: bool) -> str:
    from . import telegram_bot
    pairing = await telegram_bot.active_pairing(db, user.id)
    if pairing is None:
        await _telegram_fallback(db, row, user, "no active Telegram pairing")
        return _fail(row, "setup_blocked: no active Telegram pairing for this person")
    text = telegram_bot.compose_reminder(ctx)
    markup = telegram_bot.reminder_keyboard(task) if task is not None else None
    try:
        res = await telegram_bot.send(db, pairing, text, reply_markup=markup)
    except ProviderError as e:
        kind = (getattr(e, "detail", {}) or {}).get("kind", "unknown")
        await telegram_bot.record_failure(db, pairing, kind, str(e))
        if kind == "rate_limited":
            retry = int((getattr(e, "detail", {}) or {}).get("retry_after") or 30)
            row.state = "scheduled"
            row.deliver_at = now() + timedelta(seconds=retry)
            row.last_error = f"rate limited; retrying in {retry}s"
            row.lease_token = None
            return "cancelled"
        await _telegram_fallback(db, row, user, f"telegram {kind}: {e}")
        return _fail(row, f"telegram {kind}: {e}")
    except Unsupported as e:
        await _telegram_fallback(db, row, user, str(e))
        return _fail(row, f"setup_blocked: {e}")
    row.receipt = {"provider": "telegram", "chat_id": pairing.chat_id,
                   "message_id": (res.get("result") or {}).get("message_id")}
    row.provider_ref = str((res.get("result") or {}).get("message_id") or "")
    row.sent_at = now()
    row.late = late
    row.state = "accepted"     # bot API success is not proof the message was read
    row.lease_token = None
    return "sent"


async def _telegram_fallback(db: AsyncSession, row: ScheduledDelivery, user: User, reason: str) -> None:
    """`telegram_fallback_email`: email only when Telegram actually failed."""
    if not fallback_mode(user, row.kind):
        return
    key = f"{row.dedupe_key}:fallback"
    await _ensure_delivery(db, dedupe=key, kind=row.kind, task_id=row.task_id, task_revision=row.task_revision,
                           entity_kind=row.entity_kind, entity_id=row.entity_id, recipient_user_id=user.id,
                           channel="email", deliver_at=now(), fallback_of_id=row.id,
                           payload={**(row.payload or {}), "fallback_reason": reason[:300]})


async def _send_inapp(db: AsyncSession, row: ScheduledDelivery, user: User, ctx: dict, late: bool) -> str:
    if not inapp_enabled(user, row.kind):
        row.state = "cancelled"
        row.cancel_reason = "in-app notifications are off for this person"
        row.lease_token = None
        return "cancelled"
    title = ctx.get("title") or ctx.get("subject") or row.kind.replace("_", " ").title()
    note, _ = await notify(db, user_id=user.id, kind=row.kind,
                           urgency="high" if row.kind in ("overdue", "connection_issue") else "today",
                           title=title, body=ctx.get("body") or "", entity_kind=row.entity_kind,
                           entity_id=row.entity_id, deep_link=ctx.get("link"),
                           group_key=ctx.get("group_key") or f"{row.kind}:{row.entity_id}",
                           dedupe=f"inapp:{row.dedupe_key}", payload={"delivery_id": row.id})
    row.state = "delivered"    # an in-app row is genuinely delivered: it exists in Dylan's own app
    row.delivered_at = now()
    row.sent_at = now()
    row.late = late
    row.lease_token = None
    row.receipt = {"provider": "inapp", "notification_id": getattr(note, "id", None)}
    return "sent"


# ── notifications (one source for bell / Home / mobile) ─────────────────────
async def notify(db: AsyncSession, *, user_id: str, kind: str, urgency: str, title: str, body: str = "",
                 entity_kind: str | None = None, entity_id: str | None = None, deep_link: str | None = None,
                 group_key: str | None = None, dedupe: str, payload: dict | None = None) -> tuple[Notification, bool]:
    existing = (await db.execute(select(Notification).where(Notification.dedupe_key == dedupe))).scalar_one_or_none()
    if existing is not None:
        existing.occurrences = (existing.occurrences or 1) + 1
        existing.last_event_at = now()
        if existing.state in ("resolved",):
            existing.state = "unread"
        if body and body != existing.body:
            existing.body = body
        return existing, False
    row = Notification(user_id=user_id, kind=kind, urgency=urgency, title=title[:200], body=body or "",
                       entity_kind=entity_kind, entity_id=entity_id, deep_link=deep_link, group_key=group_key,
                       dedupe_key=dedupe, state="unread", occurrences=1, last_event_at=now(), payload=payload or {})
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        try:
            db.expunge(row)
        except Exception:  # noqa: BLE001
            pass
        again = (await db.execute(select(Notification).where(Notification.dedupe_key == dedupe))).scalar_one_or_none()
        return again, False
    return row, True


# ── deposit confirmed (owner only, once per payment) ────────────────────────
@on_event("deposit.confirmed")
async def _on_deposit_confirmed(db: AsyncSession, ev) -> None:
    cfg = await config(db)
    if not cfg["deposit_enabled"]:
        return
    p = dict(ev.payload or {})
    payment_id = p.get("payment_id") or p.get("deposit_payment_id") or f"{ev.aggregate_type}:{ev.aggregate_id}"
    entity_kind = ev.aggregate_type or ("invoice" if p.get("invoice_id") else "import_request")
    entity_id = ev.aggregate_id or p.get("invoice_id") or p.get("import_request_id")
    person, vehicle, request = None, None, None
    if p.get("contact_id"):
        try:
            from ..models.contacts import Contact
            c = await db.get(Contact, p["contact_id"])
            person = (c.display_name if c is not None else None) or None
        except Exception:  # noqa: BLE001
            person = None
    if p.get("vehicle_id"):
        try:
            from ..models.vehicles import Vehicle
            from .vehicles import vehicle_title
            v = await db.get(Vehicle, p["vehicle_id"])
            if v is not None:
                vehicle = f"{vehicle_title(v)} · {v.stock_no}" if v.stock_no else vehicle_title(v)
        except Exception:  # noqa: BLE001
            vehicle = None
    if p.get("import_request_id"):
        request = f"Import request {str(p['import_request_id'])[:8]}"
    handoff_kind = p.get("handoff_kind") or ("import_request" if p.get("import_request_id") else None)
    next_step = {"import_request": "Agreement · requirements · active search",
                 "sale": "Reservation agreement · pickup date · aftercare"}.get(handoff_kind or "",
                                                                                "Open the record for the next step")
    payload = {"person": person or "The buyer", "vehicle": vehicle, "request": request,
               "what": vehicle or request or "the deposit obligation",
               "amount": p.get("amount"), "currency": p.get("currency"),
               "receipt_ref": p.get("provider_receipt_ref") or p.get("source_ref"),
               "moved_to": "handed off to " + (handoff_kind or "the linked record") if handoff_kind else "recorded",
               "next_step": next_step, "cancelled_tasks": p.get("cancelled_tasks") or [],
               "confirmed_at": now().isoformat(), "payment_id": payment_id,
               "entity_kind": entity_kind, "entity_id": entity_id}
    for u in await owner_users(db):
        for ch in channels_for(u, "deposit_confirmed", cfg["channels"]):
            key = f"deposit:{payment_id}:{u.id}:{ch}"
            await _ensure_delivery(db, dedupe=key, kind="deposit_confirmed", task_id=None, task_revision=None,
                                   entity_kind=entity_kind, entity_id=entity_id, recipient_user_id=u.id,
                                   channel=ch, deliver_at=now(), payload=payload)
        await notify(db, user_id=u.id, kind="deposit_confirmed", urgency="today",
                     title=f"Deposit paid · {payload['person']}",
                     body=f"{payload['what']} — next: {next_step}", entity_kind=entity_kind, entity_id=entity_id,
                     deep_link=email_templates.deep_link(entity_kind, entity_id),
                     group_key=f"deposit:{payment_id}", dedupe=f"deposit:{payment_id}:{u.id}", payload=payload)


# ── connection issues (grouped by provider; one "needs attention" per incident) ──
@on_event("connection.degraded")
async def _on_connection_degraded(db: AsyncSession, ev) -> None:
    p = dict(ev.payload or {})
    provider = p.get("provider") or "connection"
    kind = p.get("kind") or "unknown"
    try:
        from .connections import PROVIDER_LABELS
        label = PROVIDER_LABELS.get(provider, provider)
    except Exception:  # noqa: BLE001
        label = provider
    title = f"{label} needs attention"
    body = (f"{label} has not completed a successful sync ({kind}). Lists and counts only cover data that did sync — "
            f"this is not an all-clear. {p.get('message', '')}").strip()
    urgent = kind in ("auth_expired", "permission_denied")
    for u in await owner_users(db):
        note, created = await notify(db, user_id=u.id, kind="connection_issue",
                                     urgency="high" if urgent else "later", title=title, body=body,
                                     entity_kind="connection", entity_id=ev.aggregate_id,
                                     deep_link=f"{email_templates.origin()}/settings/connections",
                                     group_key=f"connection:{provider}",
                                     dedupe=f"connection:{provider}:{kind}:{u.id}",
                                     payload={"provider": provider, "kind": kind, "message": p.get("message")})
        if created and urgent:
            cfg = await config(db)
            for ch in channels_for(u, "connection_issue", cfg["channels"]):
                await _ensure_delivery(db, dedupe=f"connissue:{provider}:{kind}:{note.id}:{ch}",
                                       kind="connection_issue", task_id=None, task_revision=None,
                                       entity_kind="connection", entity_id=ev.aggregate_id, recipient_user_id=u.id,
                                       channel=ch, deliver_at=apply_quiet_hours(u, now(), "connection_issue"),
                                       payload={"subject": title, "body": body, "title": title,
                                                "group_key": f"connection:{provider}",
                                                "link": f"{email_templates.origin()}/settings/connections"})


# ── read side used by the API ───────────────────────────────────────────────
def serialize_delivery(r: ScheduledDelivery) -> dict:
    return {"id": r.id, "kind": r.kind, "channel": r.channel, "state": r.state, "late": bool(r.late),
            "task_id": r.task_id, "task_revision": r.task_revision, "entity_kind": r.entity_kind,
            "entity_id": r.entity_id, "recipient_user_id": r.recipient_user_id,
            "deliver_at": r.deliver_at.isoformat() if r.deliver_at else None,
            "sent_at": r.sent_at.isoformat() if r.sent_at else None,
            "delivered_at": r.delivered_at.isoformat() if r.delivered_at else None,
            "attempts": r.attempts, "last_error": r.last_error, "cancel_reason": r.cancel_reason,
            "provider_ref": r.provider_ref, "fallback_of_id": r.fallback_of_id,
            "receipt": {k: v for k, v in (r.receipt or {}).items() if k not in ("text", "html")},
            "dedupe_key": r.dedupe_key,
            "state_label": STATE_LABELS.get(r.state, r.state)}


STATE_LABELS = {
    "scheduled": "Queued", "claimed": "Sending", "sent": "Sent", "accepted": "Provider accepted",
    "delivered": "Delivered", "failed": "Failed", "unknown": "Result unknown — not retried",
    "cancelled": "Cancelled", "superseded": "Superseded by a newer revision", "expired": "Collapsed into the digest",
}


async def deliveries_for_task(db: AsyncSession, task_id: str) -> list[dict]:
    rows = (await db.execute(select(ScheduledDelivery).where(ScheduledDelivery.task_id == task_id)
                             .order_by(ScheduledDelivery.deliver_at))).scalars().all()
    return [serialize_delivery(r) for r in rows]
