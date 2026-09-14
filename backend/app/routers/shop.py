"""Shop board API (spec §8.4). Four fixed columns; drag, button, keyboard and agent moves all call `shop.move_stage`."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import command_context, require
from ..core.errors import NotFound
from ..db import get_db
from ..domain.access import assert_vehicle_visible
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..models.vehicles import RECON_STATES, Part, ReconIssue, Vehicle
from ..services import shop as svc
from ..services.vehicles import serialize_part, serialize_vehicle

router = APIRouter(prefix="/api/shop", tags=["shop"])

ISSUE_ACTIONS = {"resolve": "shop.resolve_issue", "defer": "shop.defer_issue", "update": "shop.update_issue"}
PART_ACTIONS = {"order": "shop.order_part", "arrived": "shop.mark_part_arrived", "installed": "shop.mark_part_installed",
                "verify": "shop.verify_part", "payment": "shop.record_part_payment", "cancel": "shop.cancel_part"}
MOVE_SOURCES = ("button", "drag", "keyboard", "agent", "api")


@router.get("/board")
async def board(actor: Actor = Depends(require("vehicles.read")), db: AsyncSession = Depends(get_db)):
    out = await svc.board(db, actor)
    out["states"] = list(RECON_STATES)
    out["empty_state"] = None if out["total"] else "No vehicles in the shop"
    return out


@router.get("/gate-rules")
async def gate_rules(to_state: str | None = None, actor: Actor = Depends(require("vehicles.read")), db: AsyncSession = Depends(get_db)):
    if to_state and to_state not in RECON_STATES:
        raise HTTPException(422, f"to_state must be one of {RECON_STATES}")
    states = [to_state] if to_state else [s for s in RECON_STATES if s != "needs_inspection"]
    items = []
    for s in states:
        items.extend(await svc.rules_for(db, s))
    return {"items": items, "total": len(items)}


async def _vehicle(db: AsyncSession, actor: Actor, vehicle_id: str) -> Vehicle:
    v = await db.get(Vehicle, vehicle_id)
    if v is None:
        raise NotFound("vehicle not found")
    await assert_vehicle_visible(db, actor, v.id)
    return v


@router.get("/vehicles/{vehicle_id}/gates")
async def gates(vehicle_id: str, to_state: str = Query(...), actor: Actor = Depends(require("vehicles.read")),
                db: AsyncSession = Depends(get_db)):
    """Read-only gate preview for a proposed move (no side effects; the move itself creates the tasks)."""
    if to_state not in RECON_STATES:
        raise HTTPException(422, f"to_state must be one of {RECON_STATES}")
    v = await _vehicle(db, actor, vehicle_id)
    if RECON_STATES.index(to_state) <= RECON_STATES.index(v.recon_state):
        return {"vehicle_id": v.id, "from": v.recon_state, "to": to_state, "backward": to_state != v.recon_state, "gates": [],
                "decision": "Allowed" if to_state != v.recon_state else "Allowed", "reason_required": to_state != v.recon_state}
    items = await svc.evaluate_gates(db, v, to_state, v.recon_state)
    blocking = [g for g in items if not g["ok"]]
    return {"vehicle_id": v.id, "from": v.recon_state, "to": to_state, "backward": False, "gates": items,
            "decision": "Blocked" if blocking else "Allowed", "reason_required": False}


@router.get("/vehicles/{vehicle_id}/work")
async def work(vehicle_id: str, actor: Actor = Depends(require("vehicles.read")), db: AsyncSession = Depends(get_db)):
    v = await _vehicle(db, actor, vehicle_id)
    out = await svc.work_of(db, v.id)
    out["vehicle"] = {"id": v.id, "stock_no": v.stock_no, "recon_state": v.recon_state, "inspected_at": serialize_vehicle(v)["dates"]["inspected_at"]}
    return out


@router.post("/vehicles/{vehicle_id}/move")
async def move(vehicle_id: str, payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    body = dict(payload or {})
    body["vehicle_id"] = vehicle_id
    if body.get("source") not in (None, *MOVE_SOURCES):
        raise HTTPException(422, f"source must be one of {MOVE_SOURCES}")
    res = await dispatch(ctx, "shop.move_stage", body)
    return res.to_dict()


@router.post("/vehicles/{vehicle_id}/inspections")
async def log_inspection(vehicle_id: str, payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    body = dict(payload or {})
    body["vehicle_id"] = vehicle_id
    res = await dispatch(ctx, "shop.log_inspection", body)
    return res.to_dict()


@router.post("/vehicles/{vehicle_id}/issues")
async def create_issue(vehicle_id: str, payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    body = dict(payload or {})
    body["vehicle_id"] = vehicle_id
    res = await dispatch(ctx, "shop.create_issue", body)
    return res.to_dict()


@router.post("/issues/{issue_id}/{action}")
async def issue_action(issue_id: str, action: str, payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context)):
    name = ISSUE_ACTIONS.get(action)
    if name is None:
        raise HTTPException(404, f"unknown issue action {action!r}")
    i = await ctx.db.get(ReconIssue, issue_id)
    if i is None:
        raise NotFound("recon issue not found")
    body = dict(payload or {})
    body.update({"issue_id": issue_id, "vehicle_id": i.vehicle_id})
    res = await dispatch(ctx, name, body)
    return res.to_dict()


@router.post("/vehicles/{vehicle_id}/work-orders")
async def create_work_order(vehicle_id: str, payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    body = dict(payload or {})
    body["vehicle_id"] = vehicle_id
    res = await dispatch(ctx, "shop.create_work_order", body)
    return res.to_dict()


@router.get("/parts")
async def list_parts(vehicle_id: str = Query(...), actor: Actor = Depends(require("vehicles.read")), db: AsyncSession = Depends(get_db)):
    v = await _vehicle(db, actor, vehicle_id)
    rows = (await db.execute(select(Part).where(Part.vehicle_id == v.id).order_by(Part.created_at))).scalars().all()
    return {"items": [serialize_part(p) for p in rows], "total": len(rows)}


@router.post("/vehicles/{vehicle_id}/parts")
async def request_part(vehicle_id: str, payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    body = dict(payload or {})
    body["vehicle_id"] = vehicle_id
    res = await dispatch(ctx, "shop.request_part", body)
    return res.to_dict()


@router.post("/parts/{part_id}/{action}")
async def part_action(part_id: str, action: str, payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context)):
    name = PART_ACTIONS.get(action)
    if name is None:
        raise HTTPException(404, f"unknown part action {action!r}")
    p = await ctx.db.get(Part, part_id)
    if p is None:
        raise NotFound("part not found")
    body = dict(payload or {})
    body.update({"part_id": part_id, "vehicle_id": p.vehicle_id})
    if action == "order" and not body.get("part_name"):
        body["part_name"] = p.name
    res = await dispatch(ctx, name, body)
    return res.to_dict()
