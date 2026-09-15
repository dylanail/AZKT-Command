"""Sales API (spec §2.3 Sales / Lead detail, §5.1, §5.2). Board and lists are projections; every
write dispatches a services/sales.py command. Money fields obey costs.read."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import command_context, require
from ..core.errors import NotFound
from ..db import get_db
from ..domain.access import can_see_finance_status, sanitize_money, visible_vehicle_ids
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..models import User
from ..models.contacts import Contact
from ..models.sales import PIPELINES, STAGES, Opportunity
from ..models.tasks import Task
from ..models.vehicles import Vehicle
from ..services.sales import (ACTIVE_TASK_STATUSES, BOARD_STAGES, STAGE_LABELS, lead_card, next_tasks_for,
                              serialize_opportunity, vehicle_title)

router = APIRouter(prefix="/api/sales", tags=["sales"])
MONEY_KEYS = ("budget_amount", "budget_currency")
# sales.set_conversion is deliberately NOT an HTTP action: Deposit Paid is evidence-backed (spec §5.2) and only
# finance's deposit handoff dispatches it. "Record payment" opens reconciliation instead.
COMMANDS = {
    "update": "sales.update_opportunity", "move-stage": "sales.move_stage", "mark-lost": "sales.mark_lost",
    "reopen": "sales.reopen", "link-vehicle": "sales.link_vehicle", "link-request": "sales.link_request",
    "link-conversation": "sales.link_conversation",
}


async def _scope_clause(db: AsyncSession, actor: Actor):
    limit = await visible_vehicle_ids(db, actor)
    if limit is None:
        return None
    if actor.kind == "external":
        # a record-limited client follows its vehicle grant only; the owner's own leads are not part of it
        return Opportunity.vehicle_id.in_(list(limit))
    return or_(Opportunity.vehicle_id.in_(list(limit)), Opportunity.owner_user_id == actor.user_id)


async def _maps(db: AsyncSession, opps: list[Opportunity]) -> tuple[dict, dict, dict, dict]:
    cids = {o.contact_id for o in opps if o.contact_id}
    vids = {o.vehicle_id for o in opps if o.vehicle_id}
    uids = {o.owner_user_id for o in opps if o.owner_user_id}
    contacts = {c.id: c for c in (await db.execute(select(Contact).where(Contact.id.in_(cids)))).scalars().all()} if cids else {}
    vehicles = {v.id: v for v in (await db.execute(select(Vehicle).where(Vehicle.id.in_(vids)))).scalars().all()} if vids else {}
    users = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(uids)))).scalars().all()} if uids else {}
    tasks = await next_tasks_for(db, [o.id for o in opps])
    return contacts, vehicles, users, tasks


def _card(o, contacts, vehicles, users, tasks, now):
    return lead_card(o, contacts.get(o.contact_id), vehicles.get(o.vehicle_id), tasks.get(o.id),
                     users.get(o.owner_user_id), now)


@router.get("/board")
async def board(pipeline: str = Query("irq"), owner: str | None = None, lost_limit: int = Query(50, ge=0, le=500),
                actor: Actor = Depends(require("sales.read")), db: AsyncSession = Depends(get_db)):
    if pipeline not in PIPELINES:
        raise HTTPException(422, f"pipeline must be one of {PIPELINES}")
    now = datetime.now(timezone.utc)
    clauses = [Opportunity.pipeline == pipeline, Opportunity.archived_at.is_(None)]
    scope = await _scope_clause(db, actor)
    if scope is not None:
        clauses.append(scope)
    if owner:
        clauses.append(Opportunity.owner_user_id.is_(None) if owner == "none" else Opportunity.owner_user_id == owner)
    active = (await db.execute(select(Opportunity).where(*clauses, Opportunity.stage != "lost")
                               .order_by(Opportunity.created_at))).scalars().all()
    lost = (await db.execute(select(Opportunity).where(*clauses, Opportunity.stage == "lost")
                             .order_by(Opportunity.lost_at.desc().nulls_last()).limit(lost_limit))).scalars().all()
    contacts, vehicles, users, tasks = await _maps(db, list(active) + list(lost))
    columns = [{"stage": s, "label": STAGE_LABELS[s], "items": [], "count": 0} for s in BOARD_STAGES]
    by_stage = {c["stage"]: c for c in columns}
    for o in active:
        col = by_stage.get(o.stage)
        if col is None:
            continue
        col["items"].append(_card(o, contacts, vehicles, users, tasks, now))
        col["count"] += 1
    return {"pipeline": pipeline, "pipelines": list(PIPELINES), "stages": list(STAGES),
            "columns": columns, "lost": [_card(o, contacts, vehicles, users, tasks, now) for o in lost],
            "total": len(active) + len(lost), "as_of": now.isoformat()}


@router.get("/opportunities")
async def list_opportunities(pipeline: str | None = None, stage: str | None = None, contact_id: str | None = None,
                             vehicle_id: str | None = None, owner: str | None = None, q: str | None = None,
                             include_lost: bool = False, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                             actor: Actor = Depends(require("sales.read")), db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    clauses = [Opportunity.archived_at.is_(None)]
    scope = await _scope_clause(db, actor)
    if scope is not None:
        clauses.append(scope)
    if pipeline:
        clauses.append(Opportunity.pipeline == pipeline)
    if stage:
        clauses.append(Opportunity.stage == stage)
    elif not include_lost:
        clauses.append(Opportunity.stage != "lost")
    if contact_id:
        clauses.append(Opportunity.contact_id == contact_id)
    if vehicle_id:
        clauses.append(Opportunity.vehicle_id == vehicle_id)
    if owner:
        clauses.append(Opportunity.owner_user_id.is_(None) if owner == "none" else Opportunity.owner_user_id == owner)
    if q:
        pat = f"%{q.strip().lower()}%"
        sub = select(Contact.id).where(Contact.search_text.ilike(pat))
        clauses.append(or_(Opportunity.enquiry.ilike(pat), Opportunity.contact_id.in_(sub)))
    total = (await db.execute(select(func.count()).select_from(Opportunity).where(*clauses))).scalar_one()
    rows = (await db.execute(select(Opportunity).where(*clauses).order_by(Opportunity.created_at.desc())
                             .limit(limit).offset(offset))).scalars().all()
    contacts, vehicles, users, tasks = await _maps(db, list(rows))
    items = []
    for o in rows:
        d = sanitize_money(actor, serialize_opportunity(o), MONEY_KEYS)
        d["card"] = _card(o, contacts, vehicles, users, tasks, now)
        items.append(d)
    return {"items": items, "total": int(total)}


async def _deposit_state(db: AsyncSession, o: Opportunity, actor: Actor) -> dict:
    """Deposit obligation state projected from finance records when they exist. Never invents a payment."""
    try:
        from ..models.finance import Invoice
    except Exception:  # noqa: BLE001 - finance module may be mid-edit in development
        return {"state": "unknown", "label": "Finance records unavailable", "invoices": []}
    clauses = [Invoice.opportunity_id == o.id]
    if o.converted_kind == "sale" and o.converted_id:
        clauses.append(Invoice.sale_id == o.converted_id)
    if o.import_request_id:
        clauses.append(Invoice.import_request_id == o.import_request_id)
    invs = (await db.execute(select(Invoice).where(Invoice.kind == "deposit", or_(*clauses))
                             .order_by(Invoice.created_at))).scalars().all()
    if not invs:
        state = "confirmed" if o.deposit_confirmed_at else "not_recorded"
        label = "Deposit confirmed from payment evidence" if o.deposit_confirmed_at else "No deposit obligation recorded"
        return {"state": state, "label": label, "invoices": [], "confirmed_at": o.deposit_confirmed_at.isoformat() if o.deposit_confirmed_at else None}
    show = can_see_finance_status(actor)
    items = []
    for i in invs:
        d = {"id": i.id, "kind": i.kind, "status": i.status if show else None, "currency": i.currency,
             "amount_due": str(i.amount_due), "amount_allocated": str(i.amount_allocated),
             "due_at": i.due_at.isoformat() if i.due_at else None,
             "satisfied_at": i.satisfied_at.isoformat() if i.satisfied_at else None}
        items.append(sanitize_money(actor, d, ("amount_due", "amount_allocated", "currency")))
    statuses = {i.status for i in invs}
    if "paid" in statuses or "overpaid" in statuses:
        state, label = "confirmed", "Deposit paid (matched payment evidence)"
    elif "partially_paid" in statuses:
        state, label = "partial", "Partial payment; balance remaining"
    else:
        state, label = "awaiting", "Deposit obligation open; awaiting matched payment"
    return {"state": state if show else ("confirmed" if state == "confirmed" else "recorded"),
            "label": label if show else "Deposit status requires finance access", "invoices": items,
            "confirmed_at": o.deposit_confirmed_at.isoformat() if o.deposit_confirmed_at else None}


async def _load(db: AsyncSession, actor: Actor, opportunity_id: str) -> Opportunity:
    clauses = [Opportunity.id == opportunity_id]
    scope = await _scope_clause(db, actor)
    if scope is not None:
        clauses.append(scope)
    o = (await db.execute(select(Opportunity).where(*clauses))).scalar_one_or_none()
    if o is None:
        raise NotFound("opportunity not found")
    return o


@router.get("/opportunities/{opportunity_id}")
async def get_opportunity(opportunity_id: str, actor: Actor = Depends(require("sales.read")),
                          db: AsyncSession = Depends(get_db)):
    from ..routers.tasks import task_view
    from ..services.contacts import identities_of, serialize_contact
    now = datetime.now(timezone.utc)
    o = await _load(db, actor, opportunity_id)
    contacts, vehicles, users, tasks = await _maps(db, [o])
    contact = contacts.get(o.contact_id)
    vehicle = vehicles.get(o.vehicle_id)
    trows = (await db.execute(select(Task).where(Task.opportunity_id == o.id)
                              .order_by(Task.due_at.asc().nulls_last(), Task.created_at))).scalars().all()
    open_tasks = [task_view(t, now=now) for t in trows if t.status in ACTIVE_TASK_STATUSES]
    done_tasks = [task_view(t, now=now) for t in trows if t.status not in ACTIVE_TASK_STATUSES][-20:]
    request = None
    if o.import_request_id:
        try:
            from ..models.sourcing import ImportRequest
            r = await db.get(ImportRequest, o.import_request_id)
            if r is not None:
                request = {"id": r.id, "title": r.title, "status": r.status, "deposit_status": r.deposit_status,
                           "agreement_status": r.agreement_status, "paused": r.paused}
        except Exception:  # noqa: BLE001
            request = {"id": o.import_request_id}
    conversion = None
    if o.converted_id:
        conversion = {"kind": o.converted_kind, "id": o.converted_id, "at": o.converted_at.isoformat() if o.converted_at else None,
                      "source_ref": o.conversion_source_ref,
                      "path": f"/import-requests/{o.converted_id}" if o.converted_kind == "import_request" else f"/vehicles/{o.vehicle_id}/sale"}
    return {
        "opportunity": sanitize_money(actor, serialize_opportunity(o), MONEY_KEYS),
        "card": _card(o, contacts, vehicles, users, tasks, now),
        "contact": serialize_contact(contact, await identities_of(db, contact.id)) if contact else None,
        "vehicle": ({"id": vehicle.id, "title": vehicle_title(vehicle), "stock_no": vehicle.stock_no,
                     "commercial_state": vehicle.commercial_state, "allocation": vehicle.allocation,
                     "hero_asset_id": vehicle.hero_asset_id, "photo": None if vehicle.hero_asset_id else "No photo yet"}
                    if vehicle else None),
        "import_request": request, "conversion": conversion,
        "deposit": await _deposit_state(db, o, actor),
        "tasks": {"open": open_tasks, "recent_closed": done_tasks, "next": open_tasks[0] if open_tasks else None},
        "stages": [{"stage": s, "label": STAGE_LABELS[s], "hand_settable": s != "deposit_paid"} for s in STAGES],
    }


# ── writes ───────────────────────────────────────────────────────────────────
@router.post("/opportunities")
async def create_opportunity(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    res = await dispatch(ctx, "sales.create_opportunity", payload)
    return res.to_dict()


@router.post("/opportunities/{opportunity_id}/{action}")
async def opportunity_action(opportunity_id: str, action: str, payload: dict = Body(default={}),
                             ctx: CommandContext = Depends(command_context)):
    name = COMMANDS.get(action)
    if name is None:
        raise HTTPException(404, f"unknown opportunity action {action!r}")
    res = await dispatch(ctx, name, {**(payload or {}), "opportunity_id": opportunity_id})
    return res.to_dict()
