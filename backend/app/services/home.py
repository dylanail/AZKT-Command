"""The Home page aggregate (spec §2.2, §2.4, H11, K05).

Every section is built from canonical rows. There is no model call anywhere in this module — a failed model
summary can never hide the deterministic approval / task / blocker lists (spec §2.2). Sections that the signed-in
person may not see are returned as an explicit `{"available": false, "reason": ...}` block rather than silently
omitted, and duplicate alerts about the same underlying problem are grouped.

Order (spec §2.2): status · business overview · needs decision · needs attention · today · vehicle timeline ·
in progress · completed. The period selector affects the business overview only — never today's approvals,
tasks or attention items.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import Denied, ValidationFailed
from ..core.time import PHOENIX, ensure_aware, fmt_local
from ..domain.access import can_see_costs, can_see_finance_status, visible_vehicle_ids
from ..domain.actors import Actor
from ..domain.policy import has_perm
from ..models.finance import Document, Sale
from ..models.listings import Publication
from ..models.runtime import ActivityEntry, Approval, Mission
from ..models.tasks import Case, Commitment, Task
from . import connections as conns
from . import reporting, timeline as tl

log = logging.getLogger("azkt.home")

ACTIVE_TASK_STATES = ("open", "in_progress", "blocked", "waiting", "awaiting_verification")
OPEN_APPROVAL_STATES = ("pending",)
IN_PROGRESS_MISSION_STATES = ("running", "waiting_approval", "waiting_external", "waiting_until", "needs_information", "open")
OPEN_CASE_STATES = ("open", "waiting", "blocked", "needs_owner")
# Sources Home depends on before it may say "nothing needs you".
REQUIRED_CONNECTIONS = ("gmail_business", "square", "sheets", "drive")
# `services.connections.all_clear_possible` owns the stale rule (warn / expired / degraded) and Home defers to it.
# It does not treat "never connected at all" as stale, so a first run with nothing connected would still allow an
# all-clear; Home has not seen the data in that case either, so `disconnected` is added here (spec §2.2, H11).
NEVER_SYNCED_STATE = "disconnected"


def _iso(dt: datetime | None) -> str | None:
    return ensure_aware(dt).isoformat() if dt else None


def _day_bounds(now: datetime, tz: str) -> tuple[datetime, datetime]:
    local = now.astimezone(ZoneInfo(tz))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def _scoped_tasks(q, actor: Actor, scope: set[str] | None):
    """Record scope for every task list on Home.

    A vehicle outside the person's grant is filtered by `scope`; a task with no vehicle carries no vehicle scope
    at all, so for an assigned-scope person it is limited to the tasks they own — the same rule the command layer
    enforces on the write side (policy.check_record_scope), rather than showing them somebody else's blocker."""
    if scope is not None:
        q = q.where(or_(Task.vehicle_id.is_(None), Task.vehicle_id.in_(list(scope) or ["-"])))
    if actor.scope == "assigned" and actor.user_id:
        q = q.where(Task.owner_user_id == actor.user_id)
    return q


# ── 1. status ────────────────────────────────────────────────────────────────
def unhealthy_connections(overview: list[dict]) -> list[dict]:
    """Required connections that cannot back an all-clear: behind, expired, degraded or never connected.

    The stale rule itself belongs to `connections.all_clear_possible`; calling it keeps the status line and the
    Settings page from ever disagreeing about which source is behind."""
    _, stale_labels = conns.all_clear_possible(overview)
    stale = set(stale_labels)
    return [c for c in overview if c["provider"] in REQUIRED_CONNECTIONS
            and (c["label"] in stale or c["freshness"]["state"] == NEVER_SYNCED_STATE)]


async def status(db: AsyncSession, actor: Actor, *, now: datetime, tz: str, counts: dict) -> dict:
    overview = await conns.overview(db)
    unhealthy = unhealthy_connections(overview)
    stale = [c["label"] for c in unhealthy]
    all_clear = not stale
    required = [c for c in overview if c["provider"] in REQUIRED_CONNECTIONS]
    parts = []
    if counts["needs_decision"]:
        parts.append(f"{counts['needs_decision']} waiting for your decision")
    if counts["needs_attention"]:
        parts.append(f"{counts['needs_attention']} need attention")
    if counts["today"]:
        parts.append(f"{counts['today']} due today")
    if parts:
        summary = "; ".join(parts) + "."
        if not all_clear:
            summary += f" {_join(stale)} needs attention."
    elif all_clear:
        summary = "No urgent items found in synced data."
    else:
        # H11 / spec §2.2: a stale integration never produces a false all-clear
        summary = f"No urgent items found in synced data; {_join(stale)} needs attention."
    return {
        "summary": summary, "all_clear": bool(all_clear and not parts), "all_clear_possible": all_clear,
        "stale_connections": stale, "counts": counts,
        "connections": [{"provider": c["provider"], "label": c["label"], "status": c["status"],
                         "freshness": c["freshness"], "last_success_at": c["last_success_at"],
                         "required": c["provider"] in REQUIRED_CONNECTIONS} for c in overview],
        "required_connections": [c["provider"] for c in required],
        "as_of": now.isoformat(), "timezone": tz, "model_used": False,
    }


def _join(labels: list[str]) -> str:
    if not labels:
        return "a connection"
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + f" and {labels[-1]}"


# ── 2. business overview ─────────────────────────────────────────────────────
async def business_overview(db: AsyncSession, actor: Actor, *, period: str, start, end, tz: str,
                            now: datetime) -> dict:
    if not reporting.can_read_metrics(actor):
        return {"available": False, "reason": "financial summaries need costs.read or finance.status",
                "money_hidden": True}
    try:
        m = await reporting.metrics(db, actor, period, start, end, tz, now=now)
    except Denied as e:
        return {"available": False, "reason": e.message, "money_hidden": True}
    v = m["values"]
    return {
        "available": True, "period": m["period"], "currency": m["currency"], "as_of": m["as_of"],
        "stale": m.get("stale", False), "served_from": m.get("served_from"),
        "stale_reason": m.get("stale_reason"), "money_hidden": m.get("money_hidden", False),
        "summary_row": [v["vehicles_sold"], v["vehicle_costs_sold_cohort"], v["gross_profit"], v["days_to_sale"]],
        "secondary_row": [v["unsold_inventory_cost"], v["projected_gross_profit"]],
        "values": v, "completeness": m["completeness"], "cohort": m["cohort"],
        "restatements": m["restatements"], "cash_flows": m["cash_flows"],
        "drilldown_metrics": list(reporting.DRILLDOWN_METRICS),
        "note": "the period selector changes this section only",
    }


# ── 3. needs your decision ───────────────────────────────────────────────────
async def needs_decision(db: AsyncSession, actor: Actor, *, now: datetime, tz: str) -> dict:
    if not has_perm(actor, "approve"):
        return {"available": False, "reason": "approvals are reviewed by the owner", "items": [], "total": 0}
    rows = (await db.execute(select(Approval).where(Approval.status.in_(OPEN_APPROVAL_STATES))
                             .order_by(Approval.expires_at.asc().nulls_last(), Approval.created_at))).scalars().all()
    show_money = can_see_costs(actor)
    items = []
    for a in rows:
        cons = dict(a.consequence or {})
        if not show_money:
            cons = {k: v for k, v in cons.items() if k not in ("amount", "currency")}
            cons["money_hidden"] = True
        items.append({
            "approval_id": a.id, "kind": a.kind, "action": a.command_name, "title": a.title,
            "related_record": {"kind": a.entity_kind, "id": a.entity_id},
            "consequence": cons, "targets": a.targets or {},
            "deadline": _iso(a.expires_at), "deadline_label": fmt_local(a.expires_at, tz) if a.expires_at else "No deadline",
            "expired": bool(a.expires_at and ensure_aware(a.expires_at) < now),
            "review_path": a.review_path or f"/approvals/{a.id}", "version": a.approval_version,
            "requested_by": (a.requested_by or {}).get("display_name"), "requested_at": _iso(a.created_at),
        })
    return {"available": True, "items": items, "total": len(items),
            "note": "approvals are never filtered by the reporting period"}


# ── 4. needs attention ───────────────────────────────────────────────────────
def _document_link(kind: str, eid: str) -> str:
    """Documents hang off vehicles, shipments, requests, sales and contacts; only some of those have pages.
    A sale's documents are reviewed on the vehicle's Sale tab; anything else lands on Finance (documents live there)."""
    if kind == "vehicle":
        return f"/vehicles/{eid}?tab=files"
    if kind in ("shipment", "request", "contact"):
        return f"/{kind}s/{eid}"
    if kind == "sale":
        return f"/sales?lead={eid}"
    return "/finance?tab=needs-matching"


async def needs_attention(db: AsyncSession, actor: Actor, *, now: datetime, tz: str) -> dict:
    """Blockers, missing documents, overdue commitments, unmatched messages/payments, failed publications and
    stale connections — grouped by the underlying problem, each with owner, next action and due/next check."""
    scope = await visible_vehicle_ids(db, actor)
    groups: list[dict] = []

    def add(problem: str, kind: str, title: str, *, detail: str = "", owner=None, next_action: str = "",
            due_at=None, items: list | None = None, link: str | None = None, severity: str = "normal") -> None:
        groups.append({"problem": problem, "kind": kind, "title": title, "detail": detail, "owner_user_id": owner,
                       "next_action": next_action, "due_at": _iso(due_at),
                       "due_label": fmt_local(due_at, tz) if due_at else None,
                       "count": len(items or []) or 1, "items": items or [], "link": link, "severity": severity})

    # blocked work, grouped per vehicle
    q = _scoped_tasks(select(Task).where(Task.status == "blocked"), actor, scope)
    if has_perm(actor, "tasks.read"):
        blocked = (await db.execute(q.order_by(Task.blocked_at.desc().nulls_last()))).scalars().all()
        by_vehicle: dict[str, list] = {}
        for t in blocked:
            by_vehicle.setdefault(t.vehicle_id or "unassigned", []).append(t)
        for vid, rows in by_vehicle.items():
            add(f"blocked_work:{vid}", "blocker", f"{len(rows)} blocked task(s)",
                detail="; ".join(filter(None, (t.block_reason for t in rows)))[:400],
                owner=rows[0].owner_user_id, next_action="Unblock or reassign the work",
                due_at=min((t.due_at for t in rows if t.due_at), default=None),
                items=[{"kind": "task", "id": t.id, "title": t.title, "reason": t.block_reason,
                        "vehicle_id": t.vehicle_id} for t in rows],
                link=f"/vehicles/{vid}" if vid != "unassigned" else "/tasks?bucket=blocked", severity="high")

    # missing / conflicted documents, grouped per record
    if has_perm(actor, "documents.read"):
        docs = (await db.execute(select(Document).where(Document.status.in_(("missing", "conflicted", "pending"))))).scalars().all()
        docs = [d for d in docs if scope is None or d.entity_kind != "vehicle" or d.entity_id in scope]
        by_entity: dict[tuple, list] = {}
        for d in docs:
            by_entity.setdefault((d.entity_kind, d.entity_id), []).append(d)
        for (kind, eid), rows in by_entity.items():
            worst = "conflicted" if any(d.status == "conflicted" for d in rows) else (
                "missing" if any(d.status == "missing" for d in rows) else "pending")
            add(f"documents:{kind}:{eid}", "missing_document", f"{len(rows)} document(s) {worst}",
                detail=", ".join(sorted({d.type for d in rows})), next_action="Attach or resolve the document",
                items=[{"kind": "document", "id": d.id, "type": d.type, "status": d.status} for d in rows],
                link=_document_link(kind, eid), severity="high" if worst == "conflicted" else "normal")

    # overdue commitments
    if has_perm(actor, "contacts.read") or has_perm(actor, "tasks.read"):
        promises = (await db.execute(select(Commitment).where(Commitment.status.in_(("open", "proposed")),
                                                              Commitment.due_at.is_not(None),
                                                              Commitment.due_at < now)
                                     .order_by(Commitment.due_at))).scalars().all()
        if promises:
            add("overdue_commitments", "overdue_commitment", f"{len(promises)} overdue promise(s)",
                detail="; ".join(p.text for p in promises)[:400], next_action="Deliver or renegotiate the promise",
                due_at=min(p.due_at for p in promises),
                items=[{"kind": "commitment", "id": p.id, "text": p.text, "contact_id": p.contact_id,
                        "due_at": _iso(p.due_at)} for p in promises], link="/contacts", severity="high")

    # overdue tasks (grouped, deduplicated against blocked work above)
    if has_perm(actor, "tasks.read"):
        qo = _scoped_tasks(select(Task).where(Task.status.in_(("open", "in_progress", "waiting")),
                                              Task.due_at.is_not(None), Task.due_at < now), actor, scope)
        overdue = (await db.execute(qo.order_by(Task.due_at))).scalars().all()
        if overdue:
            add("overdue_tasks", "overdue_task", f"{len(overdue)} overdue task(s)",
                next_action="Reschedule or complete", due_at=overdue[0].due_at,
                items=[{"kind": "task", "id": t.id, "title": t.title, "due_at": _iso(t.due_at),
                        "owner_user_id": t.owner_user_id} for t in overdue], link="/tasks?bucket=overdue")

    # unmatched important messages / payments (optional domains — lazily imported, absence is not an error)
    if can_see_finance_status(actor):
        try:
            from . import finance_queries as fq
            nm = await fq.needs_matching(db, scope)
            if nm["total"]:
                add("finance_needs_matching", "unmatched_payment", f"{nm['total']} finance item(s) need matching",
                    detail=", ".join(f"{k}: {v}" for k, v in nm["counts"].items() if v),
                    next_action="Match the evidence or confirm the allocation",
                    items=[{"kind": i.get("item_kind"), "id": i.get("id")} for i in nm["items"][:25]],
                    link="/finance?tab=needs-matching")
        except Exception as e:  # noqa: BLE001
            log.warning("finance needs-matching unavailable: %s", e)
    if has_perm(actor, "inbox.read"):
        try:
            from . import inbox as inbox_svc  # lazy: owned by the inbox slice
            unmatched = await inbox_svc.home_unmatched(db, actor) if hasattr(inbox_svc, "home_unmatched") else None
            if unmatched and unmatched.get("total"):
                add("inbox_unmatched", "unmatched_message", f"{unmatched['total']} message(s) not matched to a record",
                    next_action="Link the conversation to a contact or vehicle",
                    items=unmatched.get("items", [])[:25], link="/inbox?filter=unmatched")
        except Exception as e:  # noqa: BLE001
            log.debug("inbox summary unavailable: %s", e)

    # failed publications
    if has_perm(actor, "listings.read"):
        try:
            pubs = (await db.execute(select(Publication).where(Publication.state.in_(("failed", "mismatch"))))).scalars().all()
            pubs = [p for p in pubs if scope is None or p.vehicle_id in scope]
            if pubs:
                add("publications_failed", "failed_publication", f"{len(pubs)} publication(s) failed or mismatched",
                    detail="; ".join(filter(None, (p.error for p in pubs)))[:400],
                    next_action="Retry or verify the channel",
                    items=[{"kind": "publication", "id": p.id, "vehicle_id": p.vehicle_id, "channel": p.channel,
                            "state": p.state} for p in pubs], link="/vehicles?health=risk")
        except Exception as e:  # noqa: BLE001
            log.debug("publications unavailable: %s", e)

    # stale connections (same grouping and the same required-source rule as the status line, so the summary and
    # this group can never disagree; never a separate duplicate per workflow)
    overview = await conns.overview(db)
    stale_rows = unhealthy_connections(overview) + [
        c for c in overview if c["provider"] not in REQUIRED_CONNECTIONS
        and c["freshness"]["state"] in ("warn", "expired", "degraded")]
    if stale_rows:
        add("connections_stale", "stale_connection", f"{len(stale_rows)} connection(s) need attention",
            detail="; ".join(f"{c['label']}: {c['freshness']['label']}" for c in stale_rows),
            next_action="Reconnect or check the sync in Settings → Connections",
            items=[{"kind": "connection", "provider": c["provider"], "label": c["label"],
                    "state": c["freshness"]["state"], "last_success_at": c["last_success_at"]} for c in stale_rows],
            link="/settings/connections",
            severity="high" if any(c["freshness"]["state"] == "expired" for c in stale_rows) else "normal")

    order = {"high": 0, "normal": 1}
    groups.sort(key=lambda g: (order.get(g["severity"], 1), g["due_at"] or "9999"))
    return {"available": True, "groups": groups, "total": len(groups),
            "items_total": sum(g["count"] for g in groups),
            "note": "alerts about the same underlying problem are grouped; counts open the filtered queue"}


# ── 5. today ─────────────────────────────────────────────────────────────────
async def today(db: AsyncSession, actor: Actor, *, now: datetime, tz: str) -> dict:
    """Calls, meetings and timed follow-ups across both sales pipelines and operations.
    Never filtered by the reporting period (spec §2.4)."""
    if not has_perm(actor, "tasks.read"):
        return {"available": False, "reason": "tasks.read required", "items": [], "total": 0}
    a, b = _day_bounds(now, tz)
    scope = await visible_vehicle_ids(db, actor)
    q = _scoped_tasks(select(Task).where(Task.status.in_(ACTIVE_TASK_STATES),
                                         func.coalesce(Task.start_at, Task.due_at) >= a,
                                         func.coalesce(Task.start_at, Task.due_at) < b), actor, scope)
    rows = (await db.execute(q.order_by(func.coalesce(Task.start_at, Task.due_at)))).scalars().all()
    items = [{"kind": "task", "id": t.id, "title": t.title, "type": t.type, "status": t.status,
              "at": _iso(t.start_at or t.due_at), "at_label": fmt_local(t.start_at or t.due_at, tz),
              "owner_user_id": t.owner_user_id, "vehicle_id": t.vehicle_id, "contact_id": t.contact_id,
              "opportunity_id": t.opportunity_id, "pipeline": ("sales" if t.opportunity_id else "operations"),
              "priority": t.priority} for t in rows]
    # a delivery appointment carries the buyer, so it belongs to the sales pipeline reader only
    if has_perm(actor, "sales.read"):
        try:
            sales = (await db.execute(select(Sale).where(Sale.delivery_appointment_at.is_not(None),
                                                         Sale.delivery_appointment_at >= a,
                                                         Sale.delivery_appointment_at < b,
                                                         Sale.status.notin_(("cancelled", "expired"))))).scalars().all()
            for s in sales:
                if scope is not None and s.vehicle_id not in scope:
                    continue
                items.append({"kind": "delivery", "id": s.id, "title": "Delivery appointment", "type": "meeting",
                              "status": s.status, "at": _iso(s.delivery_appointment_at),
                              "at_label": fmt_local(s.delivery_appointment_at, tz), "owner_user_id": None,
                              "vehicle_id": s.vehicle_id, "contact_id": s.buyer_contact_id,
                              "opportunity_id": s.opportunity_id, "pipeline": "sales", "priority": "normal"})
        except Exception as e:  # noqa: BLE001
            log.debug("sale appointments unavailable: %s", e)
    items.sort(key=lambda i: i["at"] or "")
    return {"available": True, "items": items, "total": len(items), "day": {"from": a.isoformat(), "to": b.isoformat()},
            "timezone": tz, "note": "the reporting period never filters today's work"}


# ── 7. in progress ───────────────────────────────────────────────────────────
async def in_progress(db: AsyncSession, actor: Actor, *, now: datetime, tz: str) -> dict:
    """What AZKT or a person is handling, with the next checkpoint. Only real rows — no fabricated activity."""
    out: list[dict] = []
    scope = await visible_vehicle_ids(db, actor)
    try:
        missions = (await db.execute(select(Mission).where(Mission.status.in_(IN_PROGRESS_MISSION_STATES))
                                     .order_by(Mission.next_check_at.asc().nulls_last()).limit(25))).scalars().all()
        for m in missions:
            # record scope: a mission about a vehicle outside the person's grant is not theirs to see
            if scope is not None:
                refs = [r.get("id") for r in (m.entity_refs or []) if r.get("kind") == "vehicle" and r.get("id")]
                if refs and not (set(refs) & scope):
                    continue
            out.append({"kind": "mission", "id": m.id, "title": m.outcome[:200], "status": m.status,
                        "waiting_on": m.waiting_on, "role": m.role,
                        "next_check_at": _iso(m.next_check_at),
                        "next_check_label": fmt_local(m.next_check_at, tz) if m.next_check_at else "No checkpoint recorded",
                        "owner_user_id": m.responsible_user_id, "case_id": m.case_id})
    except Exception as e:  # noqa: BLE001
        log.debug("missions unavailable: %s", e)
    cases = (await db.execute(select(Case).where(Case.status.in_(OPEN_CASE_STATES))
                              .order_by(Case.next_check_at.asc().nulls_last()).limit(25))).scalars().all()
    for c in cases:
        if scope is not None and c.vehicle_id is not None and c.vehicle_id not in scope:
            continue
        out.append({"kind": "case", "id": c.id, "title": c.title, "status": c.status, "waiting_on": c.waiting_on,
                    "role": c.owner_role, "next_action": c.next_action, "next_check_at": _iso(c.next_check_at),
                    "next_check_label": fmt_local(c.next_check_at, tz) if c.next_check_at else "No checkpoint recorded",
                    "owner_user_id": c.owner_user_id, "vehicle_id": c.vehicle_id, "case_kind": c.kind})
    out.sort(key=lambda r: r["next_check_at"] or "9999")
    return {"available": True, "items": out, "total": len(out),
            "note": "only recorded missions and cases; no fabricated live activity"}


# ── 8. completed ─────────────────────────────────────────────────────────────
async def completed(db: AsyncSession, actor: Actor, *, now: datetime, tz: str, limit: int = 10, days: int = 7) -> dict:
    if not has_perm(actor, "activity.read"):
        return {"available": False, "reason": "activity.read required", "items": [], "total": 0, "collapsed": True}
    since = now - timedelta(days=days)
    visible = ["all"]
    if has_perm(actor, "costs.read") or has_perm(actor, "finance.status"):
        visible.append("finance")
    if actor.is_owner:
        visible.append("owner")
    rows = (await db.execute(select(ActivityEntry).where(ActivityEntry.at >= since,
                                                          ActivityEntry.visibility.in_(visible),
                                                          ActivityEntry.exception.is_(False))
                             .order_by(ActivityEntry.at.desc()).limit(limit))).scalars().all()
    return {"available": True, "collapsed": True, "since": since.isoformat(), "total": len(rows),
            "items": [{"id": r.id, "at": _iso(r.at), "at_label": fmt_local(r.at, tz), "what": r.what,
                       "kind": r.kind, "state": r.state, "entity_kind": r.entity_kind, "entity_id": r.entity_id,
                       "actor": (r.actor or {}).get("display_name"), "activity_path": f"/activity/{r.id}"}
                      for r in rows],
            "link": "/activity"}


# ── the page ─────────────────────────────────────────────────────────────────
async def _section(name: str, coro, *, empty: dict) -> dict:
    """One failing section degrades to an explicit unavailable block instead of taking the whole page down.

    A bad request (`ValidationFailed`, e.g. an impossible period) still propagates so the router answers 422
    rather than pretending the page rendered. Nothing here is ever silently omitted: a section the person may not
    see and a section that broke both come back as `available: false` with a reason (spec §2.2)."""
    try:
        return await coro
    except ValidationFailed:
        raise
    except Denied as e:
        return {**empty, "available": False, "reason": e.message}
    except Exception as e:  # noqa: BLE001
        log.exception("home section %s failed", name)
        return {**empty, "available": False, "reason": f"this section could not be loaded ({type(e).__name__})",
                "degraded": True}


async def home(db: AsyncSession, actor: Actor, *, period: str = "month", start=None, end=None, tz: str = PHOENIX,
               now: datetime | None = None, horizon_days: int = 7) -> dict:
    now = ensure_aware(now) or datetime.now(timezone.utc)
    decision = await _section("needs_decision", needs_decision(db, actor, now=now, tz=tz),
                              empty={"items": [], "total": 0})
    attention = await _section("needs_attention", needs_attention(db, actor, now=now, tz=tz),
                               empty={"groups": [], "total": 0, "items_total": 0})
    todays = await _section("today", today(db, actor, now=now, tz=tz), empty={"items": [], "total": 0})
    counts = {"needs_decision": decision["total"], "needs_attention": attention["total"], "today": todays["total"]}
    return {
        "as_of": now.isoformat(), "timezone": tz, "model_used": False,
        "status": await _section("status", status(db, actor, now=now, tz=tz, counts=counts),
                                 empty={"summary": "Connection status could not be read.", "all_clear": False,
                                        "all_clear_possible": False, "stale_connections": [], "counts": counts,
                                        "connections": [], "model_used": False}),
        "business_overview": await _section(
            "business_overview", business_overview(db, actor, period=period, start=start, end=end, tz=tz, now=now),
            empty={"money_hidden": True}),
        "needs_decision": decision,
        "needs_attention": attention,
        "today": todays,
        "vehicle_timeline": (await _section("vehicle_timeline",
                                            tl.compact(db, actor, horizon_days=horizon_days, tz=tz, now=now),
                                            empty={"items": [], "total": 0, "returned": 0})
                             if has_perm(actor, "vehicles.read")
                             else {"available": False, "reason": "vehicles.read required", "items": [], "total": 0}),
        "in_progress": await _section("in_progress", in_progress(db, actor, now=now, tz=tz),
                                      empty={"items": [], "total": 0}),
        "completed": await _section("completed", completed(db, actor, now=now, tz=tz),
                                    empty={"items": [], "total": 0, "collapsed": True}),
        "sections": ["status", "business_overview", "needs_decision", "needs_attention", "today",
                     "vehicle_timeline", "in_progress", "completed"],
    }
