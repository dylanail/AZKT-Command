from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.passkey import current_user
from ..db import get_db
from ..models import IRQ, ApprovalCard, Customer, NotificationLog, Setting, Vehicle
from ..services import health as health_svc
from ..services import pricing
from ..services.usage_ingest import ingest as ingest_usage

router = APIRouter(prefix="/api", tags=["misc"], dependencies=[Depends(current_user)])


# ── Customers (card view: IRQ history + vehicles bought) ───────────────────
@router.get("/customers")
async def customers(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Customer))).scalars().all()
    return [{"id": c.id, "name": c.name, "email": c.email, "phone": c.phone} for c in rows]


@router.get("/customers/{cid}")
async def customer_card(cid: str, db: AsyncSession = Depends(get_db)):
    c = await db.get(Customer, cid)
    if not c:
        raise HTTPException(404, "not found")
    irqs = (await db.execute(select(IRQ).where(IRQ.customer_id == cid))).scalars().all()
    veh = (await db.execute(select(Vehicle).where(Vehicle.customer_id == cid))).scalars().all()
    return {
        "customer": {"id": c.id, "name": c.name, "email": c.email, "phone": c.phone},
        "irqs": [{"id": i.id, "title": i.title, "status": i.status} for i in irqs],
        "vehicles": [{"id": v.id, "title": v.title, "stage": v.stage} for v in veh],
    }


# ── IRQs ───────────────────────────────────────────────────────────────────
@router.get("/irqs")
async def irqs(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(IRQ))).scalars().all()
    return [{"id": i.id, "title": i.title, "status": i.status,
             "assignee": i.assignee,
             "received_at": i.received_at.isoformat() if i.received_at else None}
            for i in rows]


@router.post("/irqs/quick-add")
async def quick_add_irq(body: dict, db: AsyncSession = Depends(get_db)):
    """Floating quick-add: jot an IRQ on the go."""
    i = IRQ(title=body.get("title", "").strip(),
            received_at=datetime.now(timezone.utc), status="open")
    if not i.title:
        raise HTTPException(400, "title required")
    db.add(i)
    await db.commit()
    return {"id": i.id}


# ── Unified approval queue ─────────────────────────────────────────────────
@router.get("/approvals")
async def approvals(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(ApprovalCard).where(ApprovalCard.status == "pending")
        .order_by(ApprovalCard.created_at)
    )).scalars().all()
    return [{"id": a.id, "agent_key": a.agent_key, "title": a.title,
             "context": a.context, "vehicle_id": a.vehicle_id} for a in rows]


@router.post("/approvals/{aid}/{decision}")
async def decide(aid: str, decision: str, db: AsyncSession = Depends(get_db)):
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve|reject")
    card = await db.get(ApprovalCard, aid)
    if not card:
        raise HTTPException(404, "not found")
    card.status = "approved" if decision == "approve" else "rejected"
    card.decided_at = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True, "status": card.status}


# ── Cost / token burn ──────────────────────────────────────────────────────
@router.post("/costs/refresh")
async def costs_refresh(db: AsyncSession = Depends(get_db)):
    return {"ingested": await ingest_usage(db)}


@router.get("/costs/summary")
async def costs_summary(db: AsyncSession = Depends(get_db)):
    from sqlalchemy import func

    from ..models import UsageEvent
    now = datetime.now(timezone.utc)

    async def agg(since):
        q = select(
            UsageEvent.agent_key, UsageEvent.model,
            func.sum(UsageEvent.input_tokens), func.sum(UsageEvent.output_tokens),
            func.sum(UsageEvent.cost_usd),
        ).group_by(UsageEvent.agent_key, UsageEvent.model)
        if since:
            q = q.where(UsageEvent.ts >= since)
        rows = (await db.execute(q)).all()
        return [{"agent_key": r[0], "model": r[1], "input_tokens": int(r[2] or 0),
                 "output_tokens": int(r[3] or 0), "cost_usd": round(r[4] or 0, 4)}
                for r in rows]

    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "today": await agg(day),
        "week": await agg(day.fromordinal(day.toordinal() - day.weekday())),
        "month": await agg(day.replace(day=1)),
        "all_time": await agg(None),
        "pricing": pricing.pricing_table(),
    }


# ── System health ──────────────────────────────────────────────────────────
@router.get("/health/overview")
async def health_overview(db: AsyncSession = Depends(get_db)):
    return await health_svc.overview(db)


# ── Notifications feed (push ones flagged; transport added in frontend) ────
@router.get("/notifications")
async def notifications_feed(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(NotificationLog).where(NotificationLog.acknowledged.is_(False))
        .order_by(NotificationLog.created_at.desc())
    )).scalars().all()
    return [{"id": n.id, "rule_id": n.rule_id, "tier": n.tier, "title": n.title,
             "body": n.body, "pushed": n.pushed} for n in rows]


# ── Not-connected integrations (GHL / Twilio / Square) ─────────────────────
@router.get("/integrations")
async def integrations(db: AsyncSession = Depends(get_db)):
    out = {}
    for name in ("gohighlevel", "twilio", "square"):
        s = await db.get(Setting, f"integration:{name}")
        out[name] = {"connected": bool(s and s.value.get("connected")),
                     "status": "Not connected — add credentials in Settings"}
    return out


@router.put("/integrations/{name}")
async def set_integration(name: str, body: dict, db: AsyncSession = Depends(get_db)):
    if name not in ("gohighlevel", "twilio", "square"):
        raise HTTPException(404, "unknown integration")
    key = f"integration:{name}"
    s = await db.get(Setting, key)
    if not s:
        s = Setting(key=key, value={})
        db.add(s)
    s.value = {"connected": True, "fields": body}
    await db.commit()
    return {"ok": True}
