"""Vehicles API (spec §2.3 Vehicles / Vehicle detail). Reads apply record scope and money permissions;
every write dispatches a services/vehicles.py (or shop / intake) command."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import command_context, require
from ..core.errors import NotFound
from ..db import get_db
from ..domain.access import assert_vehicle_visible, visible_vehicle_ids
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..models.vehicles import HEALTH, RECON_STATES, Vehicle
from ..services import vehicles as svc

router = APIRouter(prefix="/api/vehicles", tags=["vehicles"])

COMMANDS = {
    "update": "vehicles.update", "states": "vehicles.set_states", "milestone": "vehicles.record_milestone",
    "propose_fact": "vehicles.propose_fact", "confirm_fact": "vehicles.confirm_fact", "condition": "vehicles.set_condition",
    "condition_bullet": "vehicles.edit_condition_bullet", "archive": "vehicles.archive", "restore": "vehicles.restore",
    "price": "vehicles.set_asking_price", "move_stage": "shop.move_stage", "inspection": "shop.log_inspection",
    "issue": "shop.create_issue", "part": "shop.request_part",
}
SEARCHABLE = (Vehicle.stock_no, Vehicle.frame_no_raw, Vehicle.frame_no_norm, Vehicle.title, Vehicle.make, Vehicle.model, Vehicle.color)


def _sanitize_result(actor: Actor, d: dict) -> dict:
    data = d.get("data")
    if isinstance(data, dict) and isinstance(data.get("vehicle"), dict):
        data["vehicle"] = svc.sanitize_vehicle(actor, data["vehicle"])
        if not svc.can_see_costs(actor) and isinstance(data.get("facts"), list):
            data["facts"] = [f for f in data["facts"] if f.get("key") != "purchase_amount" and f.get("visibility") != "owner"]
    return d


async def _base_query(db: AsyncSession, actor: Actor, *, include_archived: bool):
    q = select(Vehicle)
    if not include_archived:
        q = q.where(Vehicle.archived_at.is_(None))
    limit = await visible_vehicle_ids(db, actor)
    if limit is not None:
        q = q.where(Vehicle.id.in_(list(limit)) if limit else Vehicle.id.is_(None))
    return q


@router.get("")
async def list_vehicles(view: str = Query("all"), q: str | None = None, health: str | None = None, recon_state: str | None = None,
                        include_archived: bool = False, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                        actor: Actor = Depends(require("vehicles.read")), db: AsyncSession = Depends(get_db)):
    if view not in svc.VIEWS:
        raise HTTPException(422, f"view must be one of {svc.VIEWS}")
    if health and health not in HEALTH:
        raise HTTPException(422, f"health must be one of {HEALTH}")
    if recon_state and recon_state not in RECON_STATES:
        raise HTTPException(422, f"recon_state must be one of {RECON_STATES}")
    base = await _base_query(db, actor, include_archived=include_archived)
    clause = svc.view_clause(view)
    if clause is not None:
        base = base.where(clause)
    if health:
        base = base.where(Vehicle.health == health)
    if recon_state:
        base = base.where(Vehicle.recon_state == recon_state)
    if q:
        term = q.strip()
        needle = f"%{term}%"
        ors = [c.ilike(needle) for c in SEARCHABLE]
        norm = svc.normalize_frame(term)
        if norm:
            ors.append(Vehicle.frame_no_norm.ilike(f"%{norm}%"))
        stock = svc.normalize_stock_no(term)
        if stock:
            ors.append(Vehicle.stock_no == stock)
        base = base.where(or_(*ors))
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    rows = (await db.execute(base.order_by(Vehicle.updated_at.desc()).limit(limit).offset(offset))).scalars().all()
    return {"items": [svc.serialize_list_item(v) for v in rows], "total": int(total or 0), "view": view, "views": list(svc.VIEWS),
            "empty_state": None if rows else ("No vehicles yet" if view == "all" else f"No vehicles in {view}")}


@router.get("/views")
async def view_counts(actor: Actor = Depends(require("vehicles.read")), db: AsyncSession = Depends(get_db)):
    out = {}
    for view in svc.VIEWS:
        base = await _base_query(db, actor, include_archived=False)
        clause = svc.view_clause(view)
        if clause is not None:
            base = base.where(clause)
        out[view] = int(await db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    return {"counts": out}


async def _load(db: AsyncSession, actor: Actor, vehicle_id: str) -> Vehicle:
    v = await db.get(Vehicle, vehicle_id)
    if v is None:
        raise NotFound("vehicle not found")
    await assert_vehicle_visible(db, actor, v.id)
    return v


@router.get("/{vehicle_id}")
async def get_vehicle(vehicle_id: str, actor: Actor = Depends(require("vehicles.read")), db: AsyncSession = Depends(get_db)):
    v = await _load(db, actor, vehicle_id)
    return await svc.vehicle_detail(db, actor, v)


@router.get("/{vehicle_id}/timeline")
async def get_timeline(vehicle_id: str, actor: Actor = Depends(require("vehicles.read")), db: AsyncSession = Depends(get_db)):
    """Milestone projection for the card / Home timeline (planned / estimated / completed with sources)."""
    v = await _load(db, actor, vehicle_id)
    return await svc.timeline(db, v)


@router.get("/{vehicle_id}/facts")
async def get_facts(vehicle_id: str, history: bool = False, actor: Actor = Depends(require("vehicles.read")),
                    db: AsyncSession = Depends(get_db)):
    v = await _load(db, actor, vehicle_id)
    facts = [svc.serialize_fact(f) for f in await svc.facts_of(db, v.id, include_history=history)]
    if not svc.can_see_costs(actor):
        facts = [f for f in facts if f["key"] not in ("purchase_amount", "asking_price") and f["visibility"] != "owner"]
    return {"items": facts, "total": len(facts)}


@router.get("/{vehicle_id}/milestones")
async def get_milestones(vehicle_id: str, history: bool = False, actor: Actor = Depends(require("vehicles.read")),
                         db: AsyncSession = Depends(get_db)):
    v = await _load(db, actor, vehicle_id)
    items = [svc.serialize_milestone(m) for m in await svc.milestones_of(db, v.id, include_history=history)]
    return {"items": items, "total": len(items)}


@router.get("/{vehicle_id}/photos")
async def get_photos(vehicle_id: str, actor: Actor = Depends(require("vehicles.read")), db: AsyncSession = Depends(get_db)):
    v = await _load(db, actor, vehicle_id)
    links = await svc.photo_links(db, v.id)
    items = [svc.serialize_asset_brief(a, l) for l, a in links if a.kind == "photo" and not a.sensitive]
    return {"items": items, "total": len(items), "hero_asset_id": v.hero_asset_id, "empty_state": None if items else "No photo yet"}


@router.post("")
async def create_vehicle(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    res = await dispatch(ctx, "vehicles.create", payload)
    return _sanitize_result(ctx.actor, res.to_dict())


@router.post("/{vehicle_id}/{action}")
async def vehicle_action(vehicle_id: str, action: str, payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context)):
    name = COMMANDS.get(action)
    if name is None:
        raise HTTPException(404, f"unknown vehicle action {action!r}")
    body = dict(payload or {})
    body["vehicle_id"] = vehicle_id
    res = await dispatch(ctx, name, body)
    return _sanitize_result(ctx.actor, res.to_dict())
