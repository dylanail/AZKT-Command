"""Vehicle milestone timeline projection (spec §2.4, K04).

Projected from canonical rows — `vehicle_milestones` plus the shipment milestones bridged onto them — never
invented. Rules kept here:

  * Planned / estimated / completed stay distinct; a milestone with no record reads "Not recorded".
  * Actual events carry their saved source (kind + ref) and actor; upcoming events are either sourced dates or
    clearly labelled internal targets. No invented ETA, no completion percentage.
  * The independent logistics / recon / commercial / document states are all shown: a sold truck still shows its
    open shipment.
  * Turnaround durations exclude records with a missing endpoint and report the excluded count.
  * A historical period view inspects milestones completed in a past period without hiding current urgent work.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import ValidationFailed
from ..core.time import PHOENIX, ensure_aware, fmt_local
from ..domain.access import visible_vehicle_ids
from ..domain.actors import Actor
from ..models.finance import Sale
from ..models.shipping import Shipment, ShipmentMilestone
from ..models.tasks import Task
from ..models.vehicles import MILESTONE_KINDS, ReconIssue, Vehicle, VehicleMilestone

NOT_RECORDED = "Not recorded"
ACTIVE_TASK_STATES = ("open", "in_progress", "blocked", "waiting", "awaiting_verification")
OPEN_SHIPMENT_STATUSES = ("planned", "in_transit", "at_port", "released", "domestic", "exception")
STAGE_DIMENSIONS = ("logistics", "recon", "commercial", "documents")
MILESTONE_LABELS = {
    "purchased": "Purchased", "export_cleared": "Export cleared", "on_vessel": "On vessel",
    "arrived_port": "Arrived at port", "released": "Released", "received": "Received", "inspected": "Inspected",
    "recon_started": "Recon started", "ready": "Ready for sale", "listed": "Listed", "reserved": "Reserved",
    "sold": "Sold", "delivered": "Delivered",
}
# shipment milestone kind -> the vehicle milestone it feeds (same map the shipping domain bridges with)
SHIPMENT_TO_VEHICLE = {"vessel_departed": "on_vessel", "vessel_arrival": "arrived_port", "discharge": "arrived_port",
                       "release": "released", "received": "received"}
VIEW_STATES = {
    "sourcing": ("candidate",),
    "shipping": ("purchased", "export_pending", "on_vessel", "at_port", "released", "domestic_transit"),
}


def _iso(dt: datetime | None) -> str | None:
    return ensure_aware(dt).isoformat() if dt else None


def _at(dt: datetime | None, tz: str) -> dict:
    """Every date is shown with its label so a missing one is honest rather than blank."""
    if dt is None:
        return {"at": None, "at_label": NOT_RECORDED, "recorded": False}
    return {"at": _iso(dt), "at_label": fmt_local(dt, tz), "recorded": True}


def _days_between(a: datetime | None, b: datetime | None) -> int | None:
    a, b = ensure_aware(a), ensure_aware(b)
    if a is None or b is None:
        return None
    return (b - a).days


# ── milestones ───────────────────────────────────────────────────────────────
def _milestone_row(kind: str, m: VehicleMilestone | None, tz: str, shipment_rows: list[ShipmentMilestone]) -> dict:
    """One line of the timeline. `status` is planned | estimated | completed | not_recorded."""
    if m is not None:
        row = {"kind": kind, "label": MILESTONE_LABELS.get(kind, kind), "status": m.status,
               "actual": m.status == "completed", **_at(m.at, tz),
               "source": {"kind": m.source_kind, "ref": m.source_ref, "shipment_id": m.shipment_id},
               "actor_id": m.actor_id, "note": m.note, "milestone_id": m.id, "supersedes_id": m.supersedes_id,
               "origin": "shipment" if m.shipment_id else "vehicle"}
    else:
        row = {"kind": kind, "label": MILESTONE_LABELS.get(kind, kind), "status": "not_recorded", "actual": False,
               "at": None, "at_label": NOT_RECORDED, "recorded": False,
               "source": {"kind": None, "ref": None, "shipment_id": None}, "actor_id": None, "note": None,
               "milestone_id": None, "supersedes_id": None, "origin": None}
    # an open shipment notice that has not yet been bridged (planned/estimated) is shown as the shipment's own view
    pending = [s for s in shipment_rows if SHIPMENT_TO_VEHICLE.get(s.kind) == kind and s.status in ("planned", "estimated")]
    if pending:
        s = pending[-1]
        row["shipment_expectation"] = {"status": s.status, "at": _iso(s.at), "at_label": fmt_local(s.at, tz) if s.at else NOT_RECORDED,
                                       "source": {"kind": s.source_kind, "ref": s.source_ref}, "scope": s.applies_to,
                                       "shipment_id": s.shipment_id}
    return row


def _stage(v: Vehicle, dimension: str, now: datetime) -> dict:
    state = {"logistics": v.logistics_state, "recon": v.recon_state, "commercial": v.commercial_state,
             "documents": v.documents_state}[dimension]
    since, source = None, None
    for h in reversed(v.state_history or []):
        if h.get("dimension") == dimension and h.get("to") == state and h.get("at"):
            try:
                since = ensure_aware(datetime.fromisoformat(h["at"]))
                source = "state change"
            except ValueError:
                since = None
            break
    if since is None:
        since, source = ensure_aware(v.created_at), "record created (no state change recorded)"
    return {"dimension": dimension, "state": state, "since": _iso(since), "since_source": source,
            "days_in_stage": _days_between(since, now)}


async def _vehicle_item(db: AsyncSession, v: Vehicle, *, now: datetime, tz: str, horizon_days: int,
                        period: tuple[datetime, datetime] | None, milestones: list[VehicleMilestone],
                        shipments: list[Shipment], ship_milestones: list[ShipmentMilestone],
                        tasks: list[Task], issues: list[ReconIssue], sale: Sale | None) -> dict:
    current = {m.kind: m for m in milestones if m.is_current}
    history = [{"kind": m.kind, "status": m.status, "at": _iso(m.at), "source": {"kind": m.source_kind, "ref": m.source_ref},
                "actor_id": m.actor_id, "note": m.note, "milestone_id": m.id, "superseded": True}
               for m in milestones if not m.is_current]
    rows = [_milestone_row(kind, current.get(kind), tz, ship_milestones) for kind in MILESTONE_KINDS]

    open_tasks = [t for t in tasks if t.status in ACTIVE_TASK_STATES]
    blocked = [t for t in open_tasks if t.status == "blocked"]
    open_issues = [i for i in issues if i.status in ("open", "in_progress")]
    next_task = sorted([t for t in open_tasks if t.due_at], key=lambda t: ensure_aware(t.due_at))
    next_task = next_task[0] if next_task else None

    open_shipments = [s for s in shipments if s.status in OPEN_SHIPMENT_STATUSES]
    stages = {d: _stage(v, d, now) for d in STAGE_DIMENSIONS}

    acquired = ensure_aware(v.acquired_at)
    sold_at = ensure_aware(sale.completed_at) if sale and sale.completed_at else ensure_aware(v.sold_at)
    turnaround = {
        "acquisition_to_sale": {"days": _days_between(acquired, sold_at), "definition": "acquired → sale completed"},
        "received_to_ready": {"days": _days_between(v.received_at, v.ready_at), "definition": "received → ready for sale"},
        "listed_to_sold": {"days": _days_between(v.listed_at, sold_at), "definition": "listed → sale completed"},
    }
    for key, data in turnaround.items():
        data["excluded"] = data["days"] is None
        data["missing_endpoints"] = _missing_endpoints(key, v, sold_at)

    item = {
        "vehicle_id": v.id, "stock_no": v.stock_no, "title": v.title, "allocation": v.allocation,
        "health": v.health, "health_reason": v.health_reason, "situation": v.situation,
        "exception_summary": v.exception_summary, "location": v.location,
        "states": {d: stages[d]["state"] for d in STAGE_DIMENSIONS},
        "stages": stages,
        "current_stage": stages["recon"],
        "time_in_current_stage": {"dimension": "recon", "since": stages["recon"]["since"],
                                  "days": stages["recon"]["days_in_stage"], "source": stages["recon"]["since_source"]},
        "milestones": rows,
        "milestone_history": history,
        "acquisition": _at(acquired, tz),
        "days_since_acquisition": _days_between(acquired, now),
        "next_action": {"title": v.next_action, "owner_id": v.next_action_owner_id, "due_at": _iso(v.next_action_due_at),
                        "due_label": fmt_local(v.next_action_due_at, tz) if v.next_action_due_at else NOT_RECORDED,
                        "source": "vehicle.next_action" if v.next_action else None},
        "next_task": _task_brief(next_task, tz) if next_task else None,
        "open_tasks": [_task_brief(t, tz) for t in open_tasks],
        "blocked_work": ([_task_brief(t, tz) for t in blocked]
                         + [{"kind": "recon_issue", "id": i.id, "title": i.title, "severity": i.severity,
                             "status": i.status, "disclosure_required": bool(i.disclosure_required)}
                            for i in open_issues if i.severity in ("high", "critical")]),
        "open_shipments": [_shipment_brief(s, tz) for s in open_shipments],
        "shipment_open_while_sold": bool(open_shipments and v.commercial_state in ("sold", "delivered")),
        "sale": ({"sale_id": sale.id, "status": sale.status, "completed_at": _iso(sale.completed_at),
                  "delivery_appointment_at": _iso(sale.delivery_appointment_at)} if sale else None),
        "turnaround": turnaround,
        "upcoming": _upcoming(v, rows, open_shipments, open_tasks, sale, now=now, tz=tz, horizon_days=horizon_days),
        "horizon_days": horizon_days,
        "no_invented_dates": True,
    }
    if period is not None:
        a, b = period
        item["period_view"] = {
            "from": a.isoformat(), "to": b.isoformat(),
            "completed": [r for r in rows if r["status"] == "completed" and r["at"]
                          and a <= ensure_aware(datetime.fromisoformat(r["at"])) < b],
            "note": "completed milestones inside the selected period; current blocked work and next actions above are not filtered",
        }
    return item


def _missing_endpoints(key: str, v: Vehicle, sold_at: datetime | None) -> list[str]:
    pairs = {"acquisition_to_sale": [("acquired_at", v.acquired_at), ("sale completed", sold_at)],
             "received_to_ready": [("received_at", v.received_at), ("ready_at", v.ready_at)],
             "listed_to_sold": [("listed_at", v.listed_at), ("sale completed", sold_at)]}
    return [name for name, value in pairs[key] if value is None]


def _task_brief(t: Task, tz: str) -> dict:
    return {"kind": "task", "id": t.id, "title": t.title, "type": t.type, "status": t.status, "priority": t.priority,
            "owner_user_id": t.owner_user_id, "due_at": _iso(t.due_at),
            "due_label": fmt_local(t.due_at, tz) if t.due_at else NOT_RECORDED,
            "block_reason": t.block_reason, "blocked_at": _iso(t.blocked_at),
            "next_check_at": _iso(t.next_check_at), "next_check_label": t.next_check_label}


def _shipment_brief(s: Shipment, tz: str) -> dict:
    return {"kind": "shipment", "id": s.id, "ref": s.ref, "status": s.status, "vessel": s.vessel, "voyage": s.voyage,
            "container_no": s.container_no, "route": {"from": s.route_from, "to": s.route_to},
            "eta_at": _iso(s.eta_at), "eta_source": s.eta_source,
            "eta_label": fmt_local(s.eta_at, tz) if s.eta_at else NOT_RECORDED,
            "storage_deadline_at": _iso(s.storage_deadline_at), "storage_deadline_source": s.storage_deadline_source,
            "exception_summary": s.exception_summary}


def _upcoming(v: Vehicle, rows: list[dict], shipments: list[Shipment], tasks: list[Task], sale: Sale | None, *,
              now: datetime, tz: str, horizon_days: int) -> list[dict]:
    """Future events inside the horizon. Every entry says whether the date is sourced or an internal target."""
    end = now + timedelta(days=horizon_days)
    out: list[dict] = []

    def add(kind: str, label: str, at, basis: str, source: dict, ref: dict) -> None:
        at = ensure_aware(at if isinstance(at, datetime) else (datetime.fromisoformat(at) if at else None))
        if at is None or not (now <= at <= end):
            return
        out.append({"kind": kind, "label": label, "at": at.isoformat(), "at_label": fmt_local(at, tz),
                    "basis": basis, "source": source, "ref": ref})

    for r in rows:
        if r["status"] in ("planned", "estimated") and r["at"]:
            add("milestone", r["label"], r["at"], f"{r['status']} milestone", r["source"],
                {"milestone_id": r["milestone_id"], "vehicle_id": v.id})
        exp = r.get("shipment_expectation")
        if exp and exp["at"]:
            add("shipment_milestone", r["label"], exp["at"], f"{exp['status']} shipment notice", exp["source"],
                {"shipment_id": exp["shipment_id"], "vehicle_id": v.id})
    for s in shipments:
        add("shipment_eta", f"ETA {s.ref or s.id[:8]}", s.eta_at,
            "sourced" if s.eta_source else "internal target", {"kind": s.eta_source or "internal", "ref": s.ref},
            {"shipment_id": s.id})
        add("storage_deadline", f"Storage deadline {s.ref or s.id[:8]}", s.storage_deadline_at,
            "sourced" if s.storage_deadline_source else "internal target",
            {"kind": s.storage_deadline_source or "internal", "ref": s.storage_deadline_source_ref}, {"shipment_id": s.id})
    for t in tasks:
        add("task", t.title, t.due_at, "internal target", {"kind": t.source_kind or "manual", "ref": t.source_id},
            {"task_id": t.id, "vehicle_id": v.id})
    if sale is not None:
        add("delivery_appointment", "Delivery appointment", sale.delivery_appointment_at, "sourced",
            {"kind": "sale", "ref": sale.id}, {"sale_id": sale.id})
        if sale.status == "reserved":
            add("reservation_expiry", "Reservation expires", sale.reservation_expires_at, "internal target",
                {"kind": "sale", "ref": sale.id}, {"sale_id": sale.id})
    out.sort(key=lambda r: r["at"])
    return out


# ── public read API ──────────────────────────────────────────────────────────
async def timeline(db: AsyncSession, actor: Actor, *, vehicle_ids: list[str] | None = None, view: str | None = None,
                   owner: str | None = None, blocked_only: bool = False, horizon_days: int = 7,
                   tz: str = PHOENIX, now: datetime | None = None, limit: int = 50, include_archived: bool = False,
                   period_from: datetime | None = None, period_to: datetime | None = None) -> dict:
    """Per-vehicle milestone timeline. Filters (vehicle / view / owner / blocked) never need a second store."""
    now = ensure_aware(now) or datetime.now(timezone.utc)
    if horizon_days not in (7, 30):
        if horizon_days <= 0 or horizon_days > 365:
            raise ValidationFailed("horizon_days must be 7, 30 or a positive number of days up to 365")
    q = select(Vehicle)
    if not include_archived:
        q = q.where(Vehicle.archived_at.is_(None))
    if vehicle_ids:
        q = q.where(Vehicle.id.in_(vehicle_ids))
    scope = await visible_vehicle_ids(db, actor)
    if scope is not None:
        if not scope:
            return _empty(now, tz, horizon_days, view, owner, blocked_only, scoped=True)
        q = q.where(Vehicle.id.in_(list(scope)))
    if view and view != "all":
        if view in VIEW_STATES:
            q = q.where(Vehicle.logistics_state.in_(VIEW_STATES[view]))
        elif view == "shop":
            q = q.where(Vehicle.logistics_state == "received", Vehicle.recon_state != "ready_for_sale")
        elif view == "sales":
            q = q.where(or_(Vehicle.recon_state == "ready_for_sale", Vehicle.commercial_state.in_(("listed", "reserved", "sold"))))
        else:
            raise ValidationFailed("view must be all|sourcing|shipping|shop|sales")
    rows = (await db.execute(q.order_by(Vehicle.stock_no.asc().nulls_last(), Vehicle.created_at))).scalars().all()
    if owner:
        owned = {t.vehicle_id for t in (await db.execute(select(Task).where(Task.owner_user_id == owner,
                                                                            Task.vehicle_id.is_not(None),
                                                                            Task.status.in_(ACTIVE_TASK_STATES)))).scalars().all()}
        rows = [v for v in rows if v.next_action_owner_id == owner or v.id in owned]
    ids = [v.id for v in rows]
    if not ids:
        return _empty(now, tz, horizon_days, view, owner, blocked_only)

    ms = (await db.execute(select(VehicleMilestone).where(VehicleMilestone.vehicle_id.in_(ids))
                           .order_by(VehicleMilestone.created_at))).scalars().all()
    tasks = (await db.execute(select(Task).where(Task.vehicle_id.in_(ids),
                                                 Task.status.notin_(("completed", "cancelled"))))).scalars().all()
    issues = (await db.execute(select(ReconIssue).where(ReconIssue.vehicle_id.in_(ids)))).scalars().all()
    sales = (await db.execute(select(Sale).where(Sale.vehicle_id.in_(ids), Sale.status.notin_(("cancelled", "expired")))
                              .order_by(Sale.is_active.desc(), Sale.created_at.desc()))).scalars().all()
    shipments = (await db.execute(select(Shipment))).scalars().all()
    shipments = [s for s in shipments if set(s.vehicle_ids or []) & set(ids)]
    ship_ms = (await db.execute(select(ShipmentMilestone).where(
        ShipmentMilestone.shipment_id.in_([s.id for s in shipments]), ShipmentMilestone.is_current.is_(True)))).scalars().all() if shipments else []

    by_vehicle_ms: dict[str, list] = {}
    for m in ms:
        by_vehicle_ms.setdefault(m.vehicle_id, []).append(m)
    by_vehicle_tasks: dict[str, list] = {}
    for t in tasks:
        by_vehicle_tasks.setdefault(t.vehicle_id, []).append(t)
    by_vehicle_issues: dict[str, list] = {}
    for i in issues:
        by_vehicle_issues.setdefault(i.vehicle_id, []).append(i)
    sale_of: dict[str, Sale] = {}
    for s in sales:
        sale_of.setdefault(s.vehicle_id, s)

    period = None
    if period_from and period_to:
        period = (ensure_aware(period_from), ensure_aware(period_to))

    items = []
    for v in rows:
        vs = [s for s in shipments if v.id in (s.vehicle_ids or [])]
        vsm = [m for m in ship_ms if m.shipment_id in {s.id for s in vs} and m.vehicle_id in (None, v.id)]
        items.append(await _vehicle_item(db, v, now=now, tz=tz, horizon_days=horizon_days, period=period,
                                         milestones=by_vehicle_ms.get(v.id, []), shipments=vs, ship_milestones=vsm,
                                         tasks=by_vehicle_tasks.get(v.id, []), issues=by_vehicle_issues.get(v.id, []),
                                         sale=sale_of.get(v.id)))
    if blocked_only:
        items = [i for i in items if i["blocked_work"]]
    total = len(items)
    items = items[:limit]
    excluded = {k: sum(1 for i in items if i["turnaround"][k]["excluded"])
                for k in ("acquisition_to_sale", "received_to_ready", "listed_to_sold")}
    return {
        "as_of": now.isoformat(), "timezone": tz, "horizon_days": horizon_days,
        "filters": {"vehicle_ids": vehicle_ids, "view": view or "all", "owner": owner, "blocked_only": blocked_only,
                    "period": ({"from": period[0].isoformat(), "to": period[1].isoformat()} if period else None)},
        "milestone_kinds": list(MILESTONE_KINDS), "items": items, "total": total, "returned": len(items),
        "turnaround_excluded": excluded,
        "notes": ["missing dates stay \"Not recorded\"", "no invented ETAs or completion percentages",
                  "logistics, recon, commercial and document states are independent"],
    }


def _empty(now: datetime, tz: str, horizon_days: int, view, owner, blocked_only, *, scoped: bool = False) -> dict:
    return {"as_of": now.isoformat(), "timezone": tz, "horizon_days": horizon_days,
            "filters": {"vehicle_ids": None, "view": view or "all", "owner": owner, "blocked_only": blocked_only, "period": None},
            "milestone_kinds": list(MILESTONE_KINDS), "items": [], "total": 0, "returned": 0,
            "turnaround_excluded": {"acquisition_to_sale": 0, "received_to_ready": 0, "listed_to_sold": 0},
            "notes": (["no vehicles are assigned to you"] if scoped else ["no vehicles match these filters"])}


async def compact(db: AsyncSession, actor: Actor, *, limit: int = 8, horizon_days: int = 7, tz: str = PHOENIX,
                  now: datetime | None = None) -> dict:
    """The Home card: one compact line per vehicle (stage, age in stage, next event, blocked work)."""
    full = await timeline(db, actor, horizon_days=horizon_days, tz=tz, now=now, limit=limit)
    items = []
    for i in full["items"]:
        last = [m for m in i["milestones"] if m["status"] == "completed" and m["at"]]
        last = sorted(last, key=lambda m: m["at"])[-1] if last else None
        items.append({
            "vehicle_id": i["vehicle_id"], "stock_no": i["stock_no"], "title": i["title"],
            "allocation": i["allocation"], "health": i["health"], "states": i["states"],
            "current_stage": i["current_stage"]["state"], "days_in_stage": i["current_stage"]["days_in_stage"],
            "days_since_acquisition": i["days_since_acquisition"],
            "last_milestone": ({"kind": last["kind"], "label": last["label"], "at": last["at"]} if last else None),
            "next_event": (i["upcoming"][0] if i["upcoming"] else None),
            "next_action": i["next_action"], "blocked_work": len(i["blocked_work"]),
            "open_shipment": bool(i["open_shipments"]),
            "shipment_open_while_sold": i["shipment_open_while_sold"],
        })
    return {"as_of": full["as_of"], "timezone": tz, "horizon_days": horizon_days, "items": items,
            "total": full["total"], "returned": len(items), "turnaround_excluded": full["turnaround_excluded"]}
