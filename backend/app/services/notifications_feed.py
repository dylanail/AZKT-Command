"""One notification source for the bell, Home and the mobile sheet (spec §2.2, §5.4).

`build(db, actor)` returns the same grouped payload for every surface:

    {"high": [...], "today": [...], "later": [...], "badge": {...}, "status": {...}}

Duplicate alerts about the same underlying problem collapse on `group_key`; a stale integration
produces "No urgent items found in synced data; <connection> needs attention" instead of a false
all-clear (H11). Money is hidden from anyone without `costs.read`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import Conflict, Denied, NotFound
from ..core.time import PHOENIX, ensure_aware, fmt_local
from ..domain.access import can_see_costs, visible_vehicle_ids
from ..domain.actors import Actor
from ..domain.commands import CommandContext, command
from ..domain.policy import has_perm
from ..models import User
from ..models.notify import Notification
from ..models.runtime import Approval
from ..models.tasks import Task
from . import email_templates
from .reminders import ACTIONABLE, inapp_enabled

log = logging.getLogger("azkt.notifications")
URGENCIES = ("high", "today", "later")


def now() -> datetime:
    return datetime.now(timezone.utc)


def _tz(actor_user: User | None) -> str:
    return (actor_user.timezone if actor_user else None) or PHOENIX


def _item(*, id: str, kind: str, urgency: str, title: str, body: str = "", group_key: str,
          entity_kind: str | None = None, entity_id: str | None = None, deep_link: str | None = None,
          when: datetime | None = None, source: str = "derived", state: str = "unread",
          occurrences: int = 1, extra: dict | None = None) -> dict:
    return {"id": id, "kind": kind, "urgency": urgency, "title": title, "body": body, "group_key": group_key,
            "entity_kind": entity_kind, "entity_id": entity_id, "deep_link": deep_link,
            "when": when.isoformat() if when else None, "source": source, "state": state,
            "occurrences": occurrences, **(extra or {})}


DERIVED_KINDS = ("task", "approval")


def derived_dedupe(user_id: str, ref: str) -> str:
    """Marker row for a feed item that has no Notification of its own. It carries the person's
    acknowledge/snooze/dismiss, never the feed content."""
    return f"feed:{user_id}:{ref}"


def _task_row(t: Task, tz: str, right_now: datetime) -> dict | None:
    """What the bell says about one task, or None when it says nothing."""
    due = ensure_aware(t.due_at)
    if t.status == "blocked":
        return {"kind": "task_blocked", "urgency": "high", "title": f"Blocked: {t.title}",
                "body": t.block_reason or "", "when": ensure_aware(t.blocked_at) or due}
    if due and due < right_now:
        return {"kind": "task_overdue", "urgency": "high", "title": f"Overdue: {t.title}",
                "body": f"Was due {fmt_local(due, t.timezone or tz)}", "when": due}
    if due and due <= right_now + timedelta(hours=24):
        return {"kind": "task_due", "urgency": "today", "title": t.title,
                "body": fmt_local(due, t.timezone or tz), "when": due}
    if t.status == "waiting":
        return {"kind": "task_waiting", "urgency": "later", "title": f"Waiting: {t.title}",
                "body": t.notes.splitlines()[0][:120] if t.notes else "", "when": due}
    return None


async def _derived_markers(db: AsyncSession, user_id: str | None) -> dict[str, Notification]:
    if not user_id:
        return {}
    rows = (await db.execute(select(Notification).where(
        Notification.user_id == user_id,
        Notification.dedupe_key.like(f"{derived_dedupe(user_id, '')}%")))).scalars().all()
    return {(n.payload or {}).get("derived_ref") or "": n for n in rows}


def _hidden_by_person(marker: Notification | None, right_now: datetime) -> bool:
    if marker is None:
        return False
    if marker.state in ("resolved", "acknowledged"):
        return True
    if marker.state == "snoozed":
        until = ensure_aware(marker.snoozed_until)
        return bool(until and until > right_now)
    return False


async def _visible_tasks(db: AsyncSession, actor: Actor) -> list[Task]:
    q = select(Task).where(Task.status.in_(ACTIONABLE))
    limited = actor.scope == "assigned" or not actor.perms.get("vehicles.all", False)
    if actor.kind in ("user", "agent") and limited:
        q = q.where(Task.owner_user_id == actor.user_id)
    else:
        ids = await visible_vehicle_ids(db, actor)
        if ids is not None:
            q = q.where(or_(Task.vehicle_id.is_(None), Task.vehicle_id.in_(list(ids) or [""])))
    return list((await db.execute(q.order_by(Task.due_at.nulls_last()).limit(300))).scalars().all())


async def build(db: AsyncSession, actor: Actor) -> dict:
    """The single feed. Identical data for the bell, Home 'Needs attention' and the mobile sheet."""
    user = await db.get(User, actor.user_id) if actor.user_id else None
    tz = _tz(user)
    right_now = now()
    buckets: dict[str, list[dict]] = {"high": [], "today": [], "later": []}
    seen: dict[str, dict] = {}

    def add(item: dict) -> None:
        prior = seen.get(item["group_key"])
        if prior is not None:
            prior["occurrences"] = prior.get("occurrences", 1) + item.get("occurrences", 1)
            if URGENCIES.index(item["urgency"]) < URGENCIES.index(prior["urgency"]):
                buckets[prior["urgency"]].remove(prior)
                prior["urgency"] = item["urgency"]
                buckets[prior["urgency"]].append(prior)
            return
        seen[item["group_key"]] = item
        buckets[item["urgency"]].append(item)

    # 1 · stored notifications (connection issues, deposits, case updates) — one row per problem
    if user is None or inapp_enabled(user):
        rows = (await db.execute(select(Notification).where(
            Notification.user_id == actor.user_id,
            Notification.dedupe_key.notlike(f"{derived_dedupe(actor.user_id or '', '')}%"),
            Notification.state.in_(("unread", "read", "snoozed"))).order_by(Notification.created_at.desc())
            .limit(100))).scalars().all()
        for n in rows:
            snoozed = ensure_aware(n.snoozed_until)
            if n.state == "snoozed" and snoozed and snoozed > right_now:
                continue
            add(_item(id=n.id, kind=n.kind, urgency=n.urgency if n.urgency in URGENCIES else "later",
                      title=n.title, body=n.body or "", group_key=n.group_key or f"notification:{n.id}",
                      entity_kind=n.entity_kind, entity_id=n.entity_id, deep_link=n.deep_link,
                      when=ensure_aware(n.last_event_at) or ensure_aware(n.created_at), source="notification",
                      state=n.state, occurrences=n.occurrences or 1))

    # 2 · tasks: overdue and blocked are attention; due within 24h is today
    markers = await _derived_markers(db, actor.user_id)
    for t in await _visible_tasks(db, actor):
        row = _task_row(t, tz, right_now)
        if row is None or _hidden_by_person(markers.get(f"task:{t.id}"), right_now):
            continue
        # several related blocker/overdue events about one case collapse into one row (spec §2.2, C07)
        gkey = f"case:{t.case_id}" if t.case_id else f"task:{t.id}"
        add(_item(id=f"task:{t.id}", kind=row["kind"], urgency=row["urgency"], title=row["title"],
                  body=row["body"], group_key=gkey, entity_kind="task", entity_id=t.id,
                  deep_link=email_templates.deep_link("task", t.id), when=row["when"]))

    # 3 · approvals waiting on this person
    if has_perm(actor, "approve"):
        arows = (await db.execute(select(Approval).where(Approval.status == "pending")
                                  .order_by(Approval.created_at).limit(50))).scalars().all()
        show_money = can_see_costs(actor)
        for a in arows:
            if _hidden_by_person(markers.get(f"approval:{a.id}"), right_now):
                continue
            cons = dict(a.consequence or {})
            if not show_money:
                cons = {k: (None if k in ("amount", "currency") else v) for k, v in cons.items()}
                cons["money_hidden"] = True
            expires = ensure_aware(a.expires_at)
            add(_item(id=a.id, kind="approval", urgency="today", title=a.title or a.command_name,
                      body=(f"Expires {fmt_local(expires, tz)}" if expires else "Waiting on your decision"),
                      group_key=f"approval:{a.id}", entity_kind="approval", entity_id=a.id,
                      deep_link=f"{email_templates.origin()}{a.review_path or f'/approvals/{a.id}'}",
                      when=expires, source="approval", extra={"consequence": cons}))

    for key in buckets:
        buckets[key].sort(key=lambda i: (i["when"] or "9999"))
    status = await feed_status(db, len(buckets["high"]))
    badge_count = len(buckets["high"]) + len(buckets["today"])
    badge = {"count": badge_count,
             "color": "red" if buckets["high"] else ("amber" if buckets["today"] else "neutral"),
             "high": len(buckets["high"]), "today": len(buckets["today"])}
    return {"high": buckets["high"], "today": buckets["today"], "later": buckets["later"],
            "badge": badge, "status": status, "generated_at": right_now.isoformat()}


async def feed_status(db: AsyncSession, high_count: int) -> dict:
    """Never claim all-clear while a required source is behind (spec §2.2, H11).

    A failure to *evaluate* freshness is itself a reason not to claim all-clear: an unchecked source is
    an unknown source, so the status says the check did not run instead of reporting a clean sweep."""
    checked = True
    try:
        from . import connections
        conns = await connections.overview(db)
        clear, stale = connections.all_clear_possible(conns)
    except Exception:  # noqa: BLE001
        log.exception("connection freshness could not be evaluated for the notification feed")
        clear, stale, checked = False, [], False
    head = f"{high_count} item{'s' if high_count != 1 else ''} need attention." if high_count else ""
    if not checked:
        message = (head + " Connection status could not be checked, so this is not an all-clear.").strip()
    elif high_count:
        message = head + (f" {', '.join(stale)} needs attention — this list only covers synced data." if stale else "")
    elif stale:
        message = f"No urgent items found in synced data; {', '.join(stale)} needs attention."
    else:
        message = "No urgent items found."
    return {"all_clear": bool(clear and not high_count), "stale_connections": stale, "message": message,
            "sources_complete": bool(clear), "sources_checked": checked}


# ── commands ────────────────────────────────────────────────────────────────
class NotificationRefIn(BaseModel):
    notification_id: str
    expected_version: int | None = None


class NotificationSnoozeIn(BaseModel):
    notification_id: str
    minutes: int = Field(default=60, ge=1, le=60 * 24 * 14)
    expected_version: int | None = None


async def _materialize(ctx: CommandContext, ref: str) -> Notification:
    """Give a derived feed item (`task:<id>`, `approval:<id>`) a real row for this person, so their
    acknowledge / snooze / dismiss is stored and audited like any other notification."""
    from .reminders import notify
    kind, _, rid = ref.partition(":")
    if ctx.actor.kind != "user" or not ctx.actor.user_id:
        raise Denied("only a signed-in person has a notification feed")
    if kind == "task":
        t = await ctx.db.get(Task, rid)
        if t is None:
            raise NotFound("task not found")
        limited = ctx.actor.scope == "assigned" or not ctx.actor.perms.get("vehicles.all", False)
        if limited and t.owner_user_id != ctx.actor.user_id:
            raise Denied("that task is not yours")
        if not limited and t.vehicle_id:
            from ..domain.access import assert_vehicle_visible
            await assert_vehicle_visible(ctx.db, ctx.actor, t.vehicle_id)
        user = await ctx.db.get(User, ctx.actor.user_id)
        row = _task_row(t, _tz(user), now()) or {"kind": "task_waiting", "urgency": "later",
                                                 "title": t.title, "body": ""}
        n, _created = await notify(ctx.db, user_id=ctx.actor.user_id, kind=row["kind"], urgency=row["urgency"],
                                   title=row["title"], body=row["body"], entity_kind="task", entity_id=t.id,
                                   deep_link=email_templates.deep_link("task", t.id),
                                   group_key=f"case:{t.case_id}" if t.case_id else f"task:{t.id}",
                                   dedupe=derived_dedupe(ctx.actor.user_id, ref), payload={"derived_ref": ref},
                                   reopen=False)
        return n
    if kind == "approval":
        if not has_perm(ctx.actor, "approve"):
            raise Denied("approvals are not yours to review")
        a = await ctx.db.get(Approval, rid)
        if a is None:
            raise NotFound("approval not found")
        n, _created = await notify(ctx.db, user_id=ctx.actor.user_id, kind="approval", urgency="today",
                                   title=a.title or a.command_name, body="Waiting on your decision",
                                   entity_kind="approval", entity_id=a.id,
                                   deep_link=f"{email_templates.origin()}{a.review_path or f'/approvals/{a.id}'}",
                                   group_key=f"approval:{a.id}",
                                   dedupe=derived_dedupe(ctx.actor.user_id, ref), payload={"derived_ref": ref},
                                   reopen=False)
        return n
    raise NotFound("notification not found")


async def _own(ctx: CommandContext, notification_id: str, expected_version: int | None = None) -> Notification:
    if notification_id.partition(":")[0] in DERIVED_KINDS and ":" in notification_id:
        n = await _materialize(ctx, notification_id)
    else:
        n = (await ctx.db.execute(select(Notification).where(Notification.id == notification_id)
                                  .with_for_update())).scalar_one_or_none()
    if n is None:
        raise NotFound("notification not found")
    if n.user_id != ctx.actor.user_id:
        raise Denied("that notification belongs to someone else")
    if expected_version is not None and n.version != expected_version:
        raise Conflict("notification changed since you loaded it", current_version=n.version)
    return n


@command("notifications.acknowledge", input=NotificationRefIn, perm=None, action_class="internal",
         description="Acknowledge one of your own notifications (acknowledged is a person's action, not a provider receipt).")
async def acknowledge(ctx: CommandContext, inp: NotificationRefIn) -> dict:
    n = await _own(ctx, inp.notification_id, inp.expected_version)
    n.state = "acknowledged"
    n.acknowledged_at = ctx.now
    n.snoozed_until = None
    ctx.touch(n, "notification")
    ctx.record(f"Acknowledged: {n.title}", entity_kind="notification", entity_id=n.id, kind="system",
               state="acknowledged", visibility="owner")
    return {"notification": serialize(n)}


@command("notifications.snooze", input=NotificationSnoozeIn, perm=None, action_class="internal",
         description="Hide a notification until later without resolving the underlying problem.")
async def snooze(ctx: CommandContext, inp: NotificationSnoozeIn) -> dict:
    n = await _own(ctx, inp.notification_id, inp.expected_version)
    n.state = "snoozed"
    n.snoozed_until = ctx.now + timedelta(minutes=inp.minutes)
    ctx.touch(n, "notification")
    ctx.record(f"Snoozed {inp.minutes} min: {n.title}", entity_kind="notification", entity_id=n.id, kind="system",
               state="snoozed", visibility="owner")
    return {"notification": serialize(n)}


@command("notifications.dismiss", input=NotificationRefIn, perm=None, action_class="internal",
         description="Dismiss a notification. The underlying record keeps its own state.")
async def dismiss(ctx: CommandContext, inp: NotificationRefIn) -> dict:
    n = await _own(ctx, inp.notification_id, inp.expected_version)
    n.state = "resolved"
    ctx.touch(n, "notification")
    ctx.record(f"Dismissed: {n.title}", entity_kind="notification", entity_id=n.id, kind="system", state="resolved",
               visibility="owner")
    return {"notification": serialize(n)}


def serialize(n: Notification) -> dict:
    return {"id": n.id, "version": n.version, "kind": n.kind, "urgency": n.urgency, "title": n.title,
            "body": n.body, "entity_kind": n.entity_kind, "entity_id": n.entity_id, "deep_link": n.deep_link,
            "group_key": n.group_key, "state": n.state, "occurrences": n.occurrences,
            "derived_ref": (n.payload or {}).get("derived_ref"),
            "snoozed_until": n.snoozed_until.isoformat() if n.snoozed_until else None,
            "acknowledged_at": n.acknowledged_at.isoformat() if n.acknowledged_at else None,
            "created_at": n.created_at.isoformat() if n.created_at else None}
