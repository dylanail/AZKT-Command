"""Shipping API (spec §2.3 Shipment, §8.3). `/api/shipments` lists/details shipments with legs, per-vehicle effective
milestones (K04 shape: planned/estimated/completed with source), storage/release evidence and quotes;
`/api/shipping/quotes` drives the adaptive quote case (start, request, reply, compare, forward, book). Reads apply
vehicle record scope; leg/quote/booking amounts obey costs.read; every write dispatches services/shipping.py."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import cast, func, or_, select, String
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import command_context, require
from ..core.errors import NotFound, ValidationFailed
from ..core.time import PHOENIX
from ..db import get_db
from ..domain.access import can_see_costs, sanitize_money, visible_vehicle_ids
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..models.runtime import Approval, ExternalAction
from ..models.shipping import QUOTE_STATUSES, SHIPMENT_STATUSES, Shipment, ShipmentLeg, ShipmentMilestone, ShipmentQuote
from ..models.tasks import Case, Task
from ..models.vehicles import Vehicle
from ..services.shipping import effective_milestones, serialize_leg, serialize_milestone, serialize_quote, serialize_shipment

shipments_router = APIRouter(prefix="/api/shipments", tags=["shipments"])
quotes_router = APIRouter(prefix="/api/shipping/quotes", tags=["shipping-quotes"])

LEG_MONEY = ("amount", "currency")
QUOTE_MONEY = ("amount", "comparison", "booking", "forward_payload")
SHIPMENT_COMMANDS = {"update": "shipments.update", "record-milestone": "shipments.record_milestone",
                     "set-storage-deadline": "shipments.set_storage_deadline", "add-leg": "shipments.add_leg"}
LEG_COMMANDS = {"update": "shipments.update_leg", "remove": "shipments.remove_leg"}
QUOTE_COMMANDS = {"request": "quotes.request", "record-reply": "quotes.record_reply", "compare": "quotes.compare",
                  "forward": "quotes.forward_to_customer", "book": "quotes.book", "decline": "quotes.decline"}
DT_KEYS = ("at", "eta_at", "appointment_at", "pickup_at", "delivered_at")


def _to_utc(value, tz: str = PHOENIX):
    if value is None or value == "" or isinstance(value, datetime):
        return value
    from zoneinfo import ZoneInfo
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationFailed(f"invalid datetime {value!r}; use ISO 8601")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(timezone.utc).isoformat()


def _normalize(payload: dict) -> dict:
    out = dict(payload or {})
    tz = out.pop("timezone", None) or PHOENIX
    for k in DT_KEYS:
        if k in out:
            out[k] = _to_utc(out[k], tz)
    return out


def _leg_view(actor: Actor, l: ShipmentLeg) -> dict:
    return sanitize_money(actor, serialize_leg(l), LEG_MONEY)


def _quote_view(actor: Actor, q: ShipmentQuote, action: ExternalAction | None = None) -> dict:
    d = serialize_quote(q, action)
    return d if can_see_costs(actor) else sanitize_money(actor, d, QUOTE_MONEY)


async def _vehicle_limit(db: AsyncSession, actor: Actor) -> set[str] | None:
    return await visible_vehicle_ids(db, actor)


def _shipment_visible(s: Shipment, limit: set[str] | None) -> bool:
    return limit is None or any(v in limit for v in (s.vehicle_ids or []))


async def _load_shipment(db: AsyncSession, actor: Actor, shipment_id: str) -> Shipment:
    s = await db.get(Shipment, shipment_id)
    if s is None or not _shipment_visible(s, await _vehicle_limit(db, actor)):
        raise NotFound("shipment not found")
    return s


def _vehicle_brief(v: Vehicle | None, vid: str) -> dict:
    if v is None:
        return {"id": vid, "title": "Not recorded", "stock_no": None}
    return {"id": v.id, "title": v.title, "stock_no": v.stock_no, "logistics_state": v.logistics_state, "health": v.health,
            "photo": None if v.hero_asset_id else "No photo yet", "hero_asset_id": v.hero_asset_id}


# ── shipments ───────────────────────────────────────────────────────────────
@shipments_router.get("")
async def list_shipments(status: str | None = None, vehicle_id: str | None = None, include_complete: bool = False,
                         limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                         actor: Actor = Depends(require("shipping.read")), db: AsyncSession = Depends(get_db)):
    clauses: list = []
    if status:
        if status not in SHIPMENT_STATUSES:
            raise HTTPException(422, f"status must be one of {SHIPMENT_STATUSES}")
        clauses.append(Shipment.status == status)
    elif not include_complete:
        clauses.append(Shipment.status != "complete")
    if vehicle_id:
        clauses.append(cast(Shipment.vehicle_ids, String).contains(f'"{vehicle_id}"'))
    rows = (await db.execute(select(Shipment).where(*clauses).order_by(Shipment.updated_at.desc()))).scalars().all()
    limit_ids = await _vehicle_limit(db, actor)
    rows = [s for s in rows if _shipment_visible(s, limit_ids)]
    total = len(rows)
    rows = rows[offset:offset + limit]
    sids = [s.id for s in rows]
    ms = (await db.execute(select(ShipmentMilestone).where(ShipmentMilestone.shipment_id.in_(sids), ShipmentMilestone.is_current.is_(True))
                           .order_by(ShipmentMilestone.created_at))).scalars().all() if sids else []
    by_s: dict[str, list] = {}
    for m in ms:
        by_s.setdefault(m.shipment_id, []).append(m)
    items = []
    for s in rows:
        d = serialize_shipment(s)
        current = [m for m in by_s.get(s.id, []) if m.vehicle_id is None]
        d["latest_milestone"] = serialize_milestone(current[-1]) if current else None
        d["vehicle_count"] = len(s.vehicle_ids or [])
        items.append(d)
    return {"items": items, "total": total}


@shipments_router.get("/{shipment_id}")
async def get_shipment(shipment_id: str, actor: Actor = Depends(require("shipping.read")), db: AsyncSession = Depends(get_db)):
    s = await _load_shipment(db, actor, shipment_id)
    legs = (await db.execute(select(ShipmentLeg).where(ShipmentLeg.shipment_id == s.id).order_by(ShipmentLeg.created_at))).scalars().all()
    ms = (await db.execute(select(ShipmentMilestone).where(ShipmentMilestone.shipment_id == s.id).order_by(ShipmentMilestone.created_at))).scalars().all()
    quotes = (await db.execute(select(ShipmentQuote).where(ShipmentQuote.shipment_id == s.id).order_by(ShipmentQuote.created_at))).scalars().all()
    vids = list(s.vehicle_ids or [])
    vehicles = {v.id: v for v in (await db.execute(select(Vehicle).where(Vehicle.id.in_(vids)))).scalars().all()} if vids else {}
    case = await db.get(Case, s.case_id) if s.case_id else None
    tasks = (await db.execute(select(Task).where(Task.shipment_id == s.id, Task.status.notin_(("completed", "cancelled")))
                              .order_by(Task.due_at.asc().nulls_last()))).scalars().all()
    return {
        "shipment": serialize_shipment(s),
        "vehicles": [_vehicle_brief(vehicles.get(v), v) for v in vids],
        "legs": [_leg_view(actor, l) for l in legs],
        "milestones": {"current": [serialize_milestone(m) for m in ms if m.is_current],
                       "history": [serialize_milestone(m) for m in ms if not m.is_current],
                       "effective": effective_milestones(ms, vids)},
        "release_evidence": {"storage_deadline": serialize_shipment(s)["storage_deadline"], "source": s.storage_deadline_source,
                             "source_ref": s.storage_deadline_source_ref, "note": s.storage_deadline_note},
        "quotes": [_quote_view(actor, q) for q in quotes],
        "case": ({"id": case.id, "status": case.status, "waiting_on": case.waiting_on, "next_action": case.next_action,
                  "next_check_at": case.next_check_at.isoformat() if case.next_check_at else None} if case else None),
        "tasks": [{"id": t.id, "title": t.title, "status": t.status, "due_at": t.due_at.isoformat() if t.due_at else None} for t in tasks],
    }


@shipments_router.post("")
async def create_shipment(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    res = await dispatch(ctx, "shipments.create", _normalize(payload))
    return res.to_dict()


@shipments_router.post("/legs/{leg_id}/{action}")
async def leg_action(leg_id: str, action: str, payload: dict | None = Body(None), ctx: CommandContext = Depends(command_context)):
    name = LEG_COMMANDS.get(action)
    if name is None:
        raise HTTPException(404, f"unknown action {action}; one of {sorted(LEG_COMMANDS)}")
    res = await dispatch(ctx, name, {**_normalize(payload or {}), "leg_id": leg_id})
    return res.to_dict()


@shipments_router.post("/{shipment_id}/{action}")
async def shipment_action(shipment_id: str, action: str, payload: dict | None = Body(None),
                          ctx: CommandContext = Depends(command_context)):
    name = SHIPMENT_COMMANDS.get(action)
    if name is None:
        raise HTTPException(404, f"unknown action {action}; one of {sorted(SHIPMENT_COMMANDS)}")
    res = await dispatch(ctx, name, {**_normalize(payload or {}), "shipment_id": shipment_id})
    return res.to_dict()


# ── quote cases ─────────────────────────────────────────────────────────────
async def _load_quote(db: AsyncSession, actor: Actor, quote_id: str) -> ShipmentQuote:
    q = await db.get(ShipmentQuote, quote_id)
    if q is None:
        raise NotFound("quote not found")
    limit = await _vehicle_limit(db, actor)
    if limit is not None and q.vehicle_id and q.vehicle_id not in limit:
        raise NotFound("quote not found")
    return q


@quotes_router.get("")
async def list_quotes(status: str | None = None, vehicle_id: str | None = None, shipment_id: str | None = None,
                      waiting: bool = False, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                      actor: Actor = Depends(require("shipping.read")), db: AsyncSession = Depends(get_db)):
    clauses: list = []
    if status:
        if status not in QUOTE_STATUSES:
            raise HTTPException(422, f"status must be one of {QUOTE_STATUSES}")
        clauses.append(ShipmentQuote.status == status)
    if waiting:
        clauses.append(ShipmentQuote.status.in_(("requested", "clarifying")))
    if vehicle_id:
        clauses.append(ShipmentQuote.vehicle_id == vehicle_id)
    if shipment_id:
        clauses.append(ShipmentQuote.shipment_id == shipment_id)
    limit_ids = await _vehicle_limit(db, actor)
    if limit_ids is not None:
        clauses.append(or_(ShipmentQuote.vehicle_id.in_(list(limit_ids)), ShipmentQuote.vehicle_id.is_(None)))
    total = (await db.execute(select(func.count()).select_from(ShipmentQuote).where(*clauses))).scalar_one()
    rows = (await db.execute(select(ShipmentQuote).where(*clauses).order_by(ShipmentQuote.updated_at.desc()).limit(limit).offset(offset))).scalars().all()
    return {"items": [_quote_view(actor, q) for q in rows], "total": int(total)}


@quotes_router.get("/{quote_id}")
async def get_quote(quote_id: str, actor: Actor = Depends(require("shipping.read")), db: AsyncSession = Depends(get_db)):
    q = await _load_quote(db, actor, quote_id)
    action_ids = [a for a in (q.request_action_id, q.forward_action_id, q.booking_action_id) if a]
    actions = {a.id: a for a in (await db.execute(select(ExternalAction).where(ExternalAction.id.in_(action_ids)))).scalars().all()} if action_ids else {}
    approvals = (await db.execute(select(Approval).where(Approval.entity_kind == "shipment_quote", Approval.entity_id == q.id)
                                  .order_by(Approval.created_at.desc()))).scalars().all()
    case = await db.get(Case, q.case_id) if q.case_id else None
    task = await db.get(Task, q.clarification_task_id) if q.clarification_task_id else None

    def act(aid):
        a = actions.get(aid) if aid else None
        return {"id": a.id, "state": a.state, "provider": a.provider, "receipt": a.receipt, "executed_at": a.executed_at.isoformat() if a.executed_at else None} if a else None

    return {
        "quote": _quote_view(actor, q, actions.get(q.request_action_id)),
        "actions": {"request": act(q.request_action_id), "forward": act(q.forward_action_id), "booking": act(q.booking_action_id)},
        "approvals": [{"id": a.id, "kind": a.kind, "command_name": a.command_name, "status": a.status, "title": a.title,
                       "version": a.approval_version, "invalidated_reason": a.invalidated_reason, "review_path": a.review_path,
                       "targets": a.targets, "consequence": (a.consequence if can_see_costs(actor) else {k: v for k, v in (a.consequence or {}).items() if k not in ("amount",)})}
                      for a in approvals],
        "case": ({"id": case.id, "status": case.status, "waiting_on": case.waiting_on, "next_action": case.next_action,
                  "next_check_at": case.next_check_at.isoformat() if case.next_check_at else None, "summary": case.summary} if case else None),
        "clarification_task": ({"id": task.id, "title": task.title, "status": task.status} if task else None),
        "decisions": {"forwarded": q.status in ("forwarded", "booked"), "booked": q.status == "booked",
                      "note": "Forwarding and booking are separate approvals; neither authorizes the other"},
    }


@quotes_router.post("/start")
async def start_case(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    res = await dispatch(ctx, "quotes.start_case", payload)
    return res.to_dict()


@quotes_router.post("/reply")
async def record_reply(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    """A vendor reply that has not been matched to a quote id yet (matched by shipment/vehicle/sender)."""
    res = await dispatch(ctx, "quotes.record_reply", payload)
    return res.to_dict()


@quotes_router.post("/{quote_id}/{action}")
async def quote_action(quote_id: str, action: str, payload: dict | None = Body(None), ctx: CommandContext = Depends(command_context)):
    name = QUOTE_COMMANDS.get(action)
    if name is None:
        raise HTTPException(404, f"unknown action {action}; one of {sorted(QUOTE_COMMANDS)}")
    res = await dispatch(ctx, name, {**(payload or {}), "quote_id": quote_id})
    return res.to_dict()


router = APIRouter()
router.include_router(shipments_router)
router.include_router(quotes_router)
