from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.passkey import current_user
from ..db import get_db
from ..models import IRQ, StageTransition, Vehicle
from ..services import notion_sync

router = APIRouter(prefix="/api/vehicles", tags=["vehicles"], dependencies=[Depends(current_user)])

STAGES = ["sourced", "bid", "won", "in_transit_japan", "customs",
          "arrived", "reconditioning", "ready_for_sale", "sold"]


def _ser(v: Vehicle) -> dict:
    return {
        "id": v.id, "business_id": v.business_id, "title": v.title, "stage": v.stage,
        "auction_url": v.auction_url, "sold_price_usd": v.sold_price_usd,
        "landed_cost_usd": v.landed_cost_usd,
        "won_date": v.won_date.isoformat() if v.won_date else None,
        "sold_date": v.sold_date.isoformat() if v.sold_date else None,
        "days_on_market": v.days_on_market, "customer_id": v.customer_id,
        "stage_timestamps": v.stage_timestamps or {}, "notion_page_id": v.notion_page_id,
    }


@router.get("")
async def list_vehicles(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Vehicle))).scalars().all()
    return [_ser(v) for v in rows]


@router.get("/kanban")
async def kanban(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Vehicle))).scalars().all()
    board = {s: [] for s in STAGES}
    for v in rows:
        board.setdefault(v.stage, []).append(_ser(v))
    return {"stages": STAGES, "board": board}


@router.post("")
async def create_vehicle(body: dict, db: AsyncSession = Depends(get_db)):
    v = Vehicle(title=body.get("title", ""), auction_url=body.get("auction_url"),
                stage=body.get("stage", "sourced"))
    v.stage_timestamps = {f"{v.stage}_at": datetime.now(timezone.utc).isoformat()}
    db.add(v)
    await db.commit()
    await notion_sync.push_vehicle(v)  # Postgres is truth; mirror immediately
    await db.commit()
    return _ser(v)


@router.patch("/{vid}")
async def update_vehicle(vid: str, body: dict, db: AsyncSession = Depends(get_db)):
    v = await db.get(Vehicle, vid)
    if not v:
        raise HTTPException(404, "not found")
    for f in ("title", "auction_url", "sold_price_usd", "landed_cost_usd"):
        if f in body:
            setattr(v, f, body[f])
    await db.commit()
    await notion_sync.push_vehicle(v)
    await db.commit()
    return _ser(v)


@router.post("/{vid}/stage")
async def move_stage(vid: str, body: dict, db: AsyncSession = Depends(get_db)):
    """Kanban drag / one-tap flip. Writes back to Notion + logs the transition
    with a discrete timestamp so duration analytics never parse edit history."""
    v = await db.get(Vehicle, vid)
    if not v:
        raise HTTPException(404, "not found")
    to = body.get("stage")
    if to not in STAGES:
        raise HTTPException(400, f"stage must be one of {STAGES}")
    now = datetime.now(timezone.utc)
    db.add(StageTransition(vehicle_id=v.id, from_stage=v.stage, to_stage=to, at=now,
                           source=body.get("source", "dashboard")))
    ts = dict(v.stage_timestamps or {})
    ts[f"{to}_at"] = now.isoformat()
    v.stage_timestamps = ts
    v.stage = to
    if to == "won" and not v.won_date:
        v.won_date = now
    if to == "sold" and not v.sold_date:
        v.sold_date = now
    await db.commit()
    await notion_sync.push_vehicle(v)
    await db.commit()
    return _ser(v)


@router.get("/widgets/home")
async def home_widgets(db: AsyncSession = Depends(get_db)):
    """The things currently dug for by hand."""
    vehicles = (await db.execute(select(Vehicle))).scalars().all()
    irqs = (await db.execute(select(IRQ))).scalars().all()
    now = datetime.now(timezone.utc)

    def age_days(dt):
        return (now - dt).days if dt else None

    return {
        "won_awaiting_decision": [_ser(v) for v in vehicles if v.stage == "won"],
        "new_irqs_24h": [
            {"id": i.id, "title": i.title}
            for i in irqs
            if i.received_at and age_days(i.received_at) == 0
        ],
        "stale_irqs_7d": [
            {"id": i.id, "title": i.title, "age_days": age_days(i.received_at)}
            for i in irqs
            if i.received_at and (age_days(i.received_at) or 0) > 7 and i.status == "open"
        ],
        "listings_days_on_market": [
            {"id": v.id, "title": v.title, "days_on_market": v.days_on_market}
            for v in vehicles
            if v.stage in ("ready_for_sale",) and v.days_on_market is not None
        ],
    }
