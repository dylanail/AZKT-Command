"""Finance API (spec §2.3 Finance row, §6.1–6.4, §5.2). Reads are role-safe: owner / costs.read see amounts;
finance.status holders get sanitized status without amounts (A03); everyone else is denied. Every write dispatches
a command. Money is serialized as {"amount": "123.45", "currency": "USD"}; GET never has side effects."""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import command_context, current_actor, require
from ..core.time import PHOENIX, period_bounds
from ..db import get_db
from ..domain.access import assert_vehicle_visible, can_see_costs, can_see_finance_status, visible_vehicle_ids
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..models.finance import (Agreement, CostAllocation, CostEvidence, CostItem, Document, Invoice, LedgerMapping, LedgerRow, Payment,
                              PaymentAllocation, Sale)
from ..services import finance as fin
from ..services import finance_queries as fq
from ..services import ledger as ledger_svc
from ..services import sales_records as sr

router = APIRouter(prefix="/api/finance", tags=["finance"])

COMMANDS = {
    "costs": {"create-item": "costs.create_item", "observe": "costs.observe", "record-evidence": "costs.record_evidence",
              "confirm-match": "costs.confirm_match", "correct-match": "costs.correct_match", "leave-unmatched": "costs.leave_unmatched",
              "allocate": "costs.allocate", "confirm-allocations": "costs.confirm_allocations", "record-credit": "costs.record_credit",
              "restate": "costs.restate", "set-fx": "costs.set_fx"},
    "payments": {"record-reported": "payments.record_reported", "record-manual-confirmed": "payments.record_manual_confirmed",
                 "upsert-provider": "payments.upsert_provider", "propose-allocation": "payments.propose_allocation",
                 "confirm-allocation": "payments.confirm_allocation", "reverse-allocation": "payments.reverse_allocation"},
    "invoices": {"create": "invoices.create"},
    "sales": {"reserve": "sales.reserve", "request-reservation-override": "sales.request_reservation_override", "agree": "sales.agree",
              "mark-completed": "sales.mark_completed", "record-delivery": "sales.record_delivery", "cancel": "sales.cancel",
              "expire-reservation": "sales.expire_reservation", "add-document": "sales.add_document", "update-checklist": "sales.update_checklist",
              "record-credit": "sales.record_credit"},
    "agreements": {"create": "agreements.create", "mark-sent": "agreements.mark_sent", "mark-signed": "agreements.mark_signed"},
    "documents": {"set-status": "documents.set_status"},
    "ledger": {"propose-mapping": "ledger.propose_mapping", "update-mapping": "ledger.update_mapping", "preview": "ledger.preview",
               "activate-mapping": "ledger.activate_mapping", "import-rows": "ledger.import_rows"},
}


def _finance_status(actor: Actor) -> Actor:
    if not can_see_finance_status(actor):
        raise HTTPException(403, "finance access required")
    return actor


async def finance_status_actor(actor: Actor = Depends(current_actor)) -> Actor:
    return _finance_status(actor)


async def _run(group: str, action: str, payload: dict, ctx: CommandContext) -> dict:
    name = COMMANDS.get(group, {}).get(action)
    if name is None:
        raise HTTPException(404, f"unknown {group} action {action}")
    res = await dispatch(ctx, name, payload)
    return res.to_dict()


def _parse_day(s: str | None, tz: str) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, f"invalid date {s!r} (YYYY-MM-DD)")


def _bounds(period: str, frm: str | None, to: str | None, tz: str) -> tuple[datetime, datetime]:
    if frm or to:
        a, b = _parse_day(frm, tz), _parse_day(to, tz)
        if not (a and b):
            raise HTTPException(422, "from and to are both required for a custom range")
        return period_bounds("custom", tz, start=a, end=b)
    if period not in ("month", "7d", "30d"):
        raise HTTPException(422, "period must be month|7d|30d or give from/to")
    return period_bounds(period, tz)


# ── summary / tabs ───────────────────────────────────────────────────────────
@router.get("/summary")
async def summary(actor: Actor = Depends(finance_status_actor), db: AsyncSession = Depends(get_db)):
    """Owner / costs.read: amounts. finance.status only: sanitized status (counts, no amounts) — A03."""
    return await fq.summary(db, with_amounts=can_see_costs(actor))


@router.get("/needs-matching")
async def needs_matching(actor: Actor = Depends(require("costs.read")), db: AsyncSession = Depends(get_db)):
    return await fq.needs_matching(db, await visible_vehicle_ids(db, actor))


@router.get("/receivables")
async def receivables(actor: Actor = Depends(require("costs.read")), db: AsyncSession = Depends(get_db)):
    return await fq.receivables(db)


@router.get("/payables")
async def payables(actor: Actor = Depends(require("costs.read")), db: AsyncSession = Depends(get_db)):
    return await fq.payables(db, await visible_vehicle_ids(db, actor))


@router.get("/vehicle-costs")
async def vehicle_costs(actor: Actor = Depends(require("costs.read")), db: AsyncSession = Depends(get_db)):
    return await fq.vehicle_costs(db, await visible_vehicle_ids(db, actor))


@router.get("/vehicles/{vehicle_id}/money")
async def vehicle_money(vehicle_id: str, actor: Actor = Depends(require("costs.read")), db: AsyncSession = Depends(get_db)):
    await assert_vehicle_visible(db, actor, vehicle_id)
    out = await fq.vehicle_money(db, vehicle_id)
    if out.get("missing"):
        raise HTTPException(404, "vehicle not found")
    return out


@router.get("/sold-cohort")
async def sold_cohort(period: str = Query("month"), from_: str | None = Query(None, alias="from"), to: str | None = None,
                      tz: str = Query(PHOENIX), actor: Actor = Depends(require("costs.read")), db: AsyncSession = Depends(get_db)):
    a, b = _bounds(period, from_, to, tz)
    out = await fq.sold_cohort(db, a, b)
    limit = await visible_vehicle_ids(db, actor)
    if limit is not None:
        out["sales"] = [s for s in out["sales"] if s["vehicle_id"] in limit]
    return out


@router.get("/unsold-inventory")
async def unsold_inventory(as_of: str | None = None, actor: Actor = Depends(require("costs.read")), db: AsyncSession = Depends(get_db)):
    at = datetime.fromisoformat(as_of.replace("Z", "+00:00")) if as_of else None
    if at is not None and at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    out = await fq.unsold_inventory_cost(db, at)
    limit = await visible_vehicle_ids(db, actor)
    if limit is not None:
        out["vehicles"] = [v for v in out["vehicles"] if v["vehicle_id"] in limit]
    return out


@router.get("/export.csv")
async def export_csv(kind: str = Query("allocations"), from_: str | None = Query(None, alias="from"), to: str | None = None,
                     vehicle_id: str | None = None, tz: str = Query(PHOENIX), actor: Actor = Depends(require("costs.read")),
                     db: AsyncSession = Depends(get_db)):
    a = b = None
    if from_ or to:
        a, b = _bounds("custom", from_, to, tz)
    if vehicle_id:
        await assert_vehicle_visible(db, actor, vehicle_id)
    try:
        text = await fq.export_csv(db, kind, a, b, vehicle_id, await visible_vehicle_ids(db, actor))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return Response(content=text, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="azkt-finance-{kind}.csv"'})


# ── lists / detail ───────────────────────────────────────────────────────────
@router.get("/costs")
async def list_costs(vehicle_id: str | None = None, status: str | None = None, category: str | None = None,
                     limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                     actor: Actor = Depends(require("costs.read")), db: AsyncSession = Depends(get_db)):
    q = select(CostItem).where(CostItem.status != "cancelled")
    if vehicle_id:
        await assert_vehicle_visible(db, actor, vehicle_id)
        alloc_ids = select(CostAllocation.cost_item_id).where(CostAllocation.vehicle_id == vehicle_id)
        q = q.where((CostItem.vehicle_id == vehicle_id) | CostItem.id.in_(alloc_ids))
    if status:
        q = q.where(CostItem.status == status)
    if category:
        q = q.where(CostItem.category == category)
    limit_ids = await visible_vehicle_ids(db, actor)
    rows = (await db.execute(q.order_by(CostItem.occurred_at.desc().nulls_last(), CostItem.created_at.desc()))).scalars().all()
    if limit_ids is not None:
        rows = [c for c in rows if c.vehicle_id is None or c.vehicle_id in limit_ids]
    total = len(rows)
    rows = rows[offset: offset + limit]
    allocs = {}
    if rows:
        for a in (await db.execute(select(CostAllocation).where(CostAllocation.cost_item_id.in_([c.id for c in rows])))).scalars().all():
            allocs.setdefault(a.cost_item_id, []).append(a)
    return {"items": [fin.serialize_cost_item(c, allocs.get(c.id, [])) for c in rows], "total": total}


@router.get("/costs/{item_id}")
async def get_cost(item_id: str, actor: Actor = Depends(require("costs.read")), db: AsyncSession = Depends(get_db)):
    c = await db.get(CostItem, item_id)
    if c is None:
        raise HTTPException(404, "cost item not found")
    if c.vehicle_id:
        await assert_vehicle_visible(db, actor, c.vehicle_id)
    allocs = (await db.execute(select(CostAllocation).where(CostAllocation.cost_item_id == c.id))).scalars().all()
    evs = (await db.execute(select(CostEvidence).where(CostEvidence.cost_item_id == c.id).order_by(CostEvidence.created_at))).scalars().all()
    return {"cost_item": fin.serialize_cost_item(c, list(allocs)), "evidence": [fin.serialize_evidence(e) for e in evs]}


@router.get("/evidence")
async def list_evidence(state: str | None = None, kind: str | None = None, current: bool = True, limit: int = Query(100, ge=1, le=500),
                        actor: Actor = Depends(require("costs.read")), db: AsyncSession = Depends(get_db)):
    q = select(CostEvidence)
    if current:
        q = q.where(CostEvidence.is_current.is_(True))
    if state:
        q = q.where(CostEvidence.match_state == state)
    if kind:
        q = q.where(CostEvidence.kind == kind)
    rows = (await db.execute(q.order_by(CostEvidence.created_at.desc()).limit(limit))).scalars().all()
    limit_ids = await visible_vehicle_ids(db, actor)
    if limit_ids is not None:
        rows = [e for e in rows if e.proposed_vehicle_id is None or e.proposed_vehicle_id in limit_ids]
    return {"items": [fin.serialize_evidence(e) for e in rows], "total": len(rows)}


@router.get("/evidence/{evidence_id}")
async def get_evidence(evidence_id: str, actor: Actor = Depends(require("costs.read")), db: AsyncSession = Depends(get_db)):
    e = await db.get(CostEvidence, evidence_id)
    if e is None:
        raise HTTPException(404, "evidence not found")
    if e.proposed_vehicle_id:
        await assert_vehicle_visible(db, actor, e.proposed_vehicle_id)
    history = (await db.execute(select(CostEvidence).where(CostEvidence.source_ref == e.source_ref, CostEvidence.kind == e.kind)
                                .order_by(CostEvidence.revision))).scalars().all() if e.source_ref else [e]
    return {"evidence": fin.serialize_evidence(e), "revisions": [fin.serialize_evidence(x) for x in history]}


@router.get("/payments")
async def list_payments(status: str | None = None, provider: str | None = None, limit: int = Query(100, ge=1, le=500),
                        actor: Actor = Depends(require("costs.read")), db: AsyncSession = Depends(get_db)):
    q = select(Payment)
    if status:
        q = q.where(Payment.status == status)
    if provider:
        q = q.where(Payment.provider == provider)
    rows = (await db.execute(q.order_by(Payment.occurred_at.desc().nulls_last(), Payment.created_at.desc()).limit(limit))).scalars().all()
    return {"items": [fin.serialize_payment(p) for p in rows], "total": len(rows)}


@router.get("/payments/{payment_id}")
async def get_payment(payment_id: str, actor: Actor = Depends(require("costs.read")), db: AsyncSession = Depends(get_db)):
    p = await db.get(Payment, payment_id)
    if p is None:
        raise HTTPException(404, "payment not found")
    allocs = (await db.execute(select(PaymentAllocation).where(PaymentAllocation.payment_id == p.id).order_by(PaymentAllocation.created_at))).scalars().all()
    return {"payment": fin.serialize_payment(p), "allocations": [fin.serialize_payment_allocation(a) for a in allocs]}


@router.get("/invoices")
async def list_invoices(status: str | None = None, kind: str | None = None, opportunity_id: str | None = None,
                        actor: Actor = Depends(require("costs.read")), db: AsyncSession = Depends(get_db)):
    q = select(Invoice)
    if status:
        q = q.where(Invoice.status == status)
    if kind:
        q = q.where(Invoice.kind == kind)
    if opportunity_id:
        q = q.where(Invoice.opportunity_id == opportunity_id)
    rows = (await db.execute(q.order_by(Invoice.created_at.desc()))).scalars().all()
    return {"items": [fin.serialize_invoice(i) for i in rows], "total": len(rows)}


@router.get("/invoices/{invoice_id}")
async def get_invoice(invoice_id: str, actor: Actor = Depends(require("costs.read")), db: AsyncSession = Depends(get_db)):
    i = await db.get(Invoice, invoice_id)
    if i is None:
        raise HTTPException(404, "invoice not found")
    allocs = (await db.execute(select(PaymentAllocation).where(PaymentAllocation.invoice_id == i.id).order_by(PaymentAllocation.created_at))).scalars().all()
    return {"invoice": fin.serialize_invoice(i), "allocations": [fin.serialize_payment_allocation(a) for a in allocs]}


@router.get("/sales")
async def list_sales(vehicle_id: str | None = None, status: str | None = None, active: bool | None = None,
                     actor: Actor = Depends(require("sales.read")), db: AsyncSession = Depends(get_db)):
    q = select(Sale)
    if vehicle_id:
        q = q.where(Sale.vehicle_id == vehicle_id)
    if status:
        q = q.where(Sale.status == status)
    if active is not None:
        q = q.where(Sale.is_active.is_(active))
    rows = (await db.execute(q.order_by(Sale.created_at.desc()))).scalars().all()
    limit = await visible_vehicle_ids(db, actor)
    if limit is not None:
        rows = [s for s in rows if s.vehicle_id in limit]
    items = [sr.serialize_sale(s) for s in rows]
    if not can_see_costs(actor):
        items = [{**s, "price": None, "sales_tax": None, "pass_through": None, "credits": None, "net_sale_value": None, "money_hidden": True} for s in items]
    return {"items": items, "total": len(items)}


@router.get("/sales/{sale_id}")
async def get_sale(sale_id: str, actor: Actor = Depends(require("sales.read")), db: AsyncSession = Depends(get_db)):
    s = await db.get(Sale, sale_id)
    if s is None:
        raise HTTPException(404, "sale not found")
    await assert_vehicle_visible(db, actor, s.vehicle_id)
    docs = (await db.execute(select(Document).where(Document.entity_kind == "sale", Document.entity_id == s.id))).scalars().all()
    invs = (await db.execute(select(Invoice).where(Invoice.sale_id == s.id))).scalars().all()
    agr = await db.get(Agreement, s.agreement_id) if s.agreement_id else None
    out = sr.serialize_sale(s)
    if not can_see_costs(actor):
        out.update({"price": None, "sales_tax": None, "pass_through": None, "credits": None, "net_sale_value": None, "money_hidden": True})
    return {"sale": out, "documents": [sr.serialize_document(d) for d in docs],
            "invoices": [fin.serialize_invoice(i) for i in invs] if can_see_costs(actor) else [],
            "agreement": sr.serialize_agreement(agr) if agr and can_see_costs(actor) else None}


@router.get("/agreements")
async def list_agreements(contact_id: str | None = None, status: str | None = None, actor: Actor = Depends(require("costs.read")),
                          db: AsyncSession = Depends(get_db)):
    q = select(Agreement)
    if contact_id:
        q = q.where(Agreement.contact_id == contact_id)
    if status:
        q = q.where(Agreement.status == status)
    rows = (await db.execute(q.order_by(Agreement.created_at.desc()))).scalars().all()
    return {"items": [sr.serialize_agreement(a) for a in rows], "total": len(rows)}


# ── ledger ───────────────────────────────────────────────────────────────────
@router.get("/ledger/mappings")
async def list_mappings(sheet_id: str | None = None, actor: Actor = Depends(require("costs.read")), db: AsyncSession = Depends(get_db)):
    q = select(LedgerMapping)
    if sheet_id:
        q = q.where(LedgerMapping.sheet_id == sheet_id)
    rows = (await db.execute(q.order_by(LedgerMapping.created_at.desc()))).scalars().all()
    return {"items": [ledger_svc.serialize_mapping(m) for m in rows], "total": len(rows)}


@router.get("/ledger/mappings/{mapping_id}")
async def get_mapping(mapping_id: str, actor: Actor = Depends(require("costs.read")), db: AsyncSession = Depends(get_db)):
    m = await db.get(LedgerMapping, mapping_id)
    if m is None:
        raise HTTPException(404, "mapping not found")
    return {"mapping": ledger_svc.serialize_mapping(m)}


@router.get("/ledger/rows")
async def list_ledger_rows(mapping_id: str, status: str | None = None, limit: int = Query(200, ge=1, le=1000),
                           actor: Actor = Depends(require("costs.read")), db: AsyncSession = Depends(get_db)):
    q = select(LedgerRow).where(LedgerRow.mapping_id == mapping_id)
    if status:
        q = q.where(LedgerRow.status == status)
    rows = (await db.execute(q.order_by(LedgerRow.row_number_seen.asc().nulls_last()).limit(limit))).scalars().all()
    return {"items": [ledger_svc.serialize_ledger_row(r) for r in rows], "total": len(rows)}


# ── commands ─────────────────────────────────────────────────────────────────
@router.post("/costs/{action}")
async def costs_action(action: str, payload: dict = Body(default_factory=dict), ctx: CommandContext = Depends(command_context)):
    return await _run("costs", action, payload, ctx)


@router.post("/payments/{action}")
async def payments_action(action: str, payload: dict = Body(default_factory=dict), ctx: CommandContext = Depends(command_context)):
    return await _run("payments", action, payload, ctx)


@router.post("/invoices/{action}")
async def invoices_action(action: str, payload: dict = Body(default_factory=dict), ctx: CommandContext = Depends(command_context)):
    return await _run("invoices", action, payload, ctx)


@router.post("/sales/{action}")
async def sales_action(action: str, payload: dict = Body(default_factory=dict), ctx: CommandContext = Depends(command_context)):
    out = await _run("sales", action, payload, ctx)
    if action == "reserve" and isinstance(out.get("data"), dict) and out["data"].get("outcome") == "conflict":
        return Response(content=__import__("json").dumps(out, default=str), media_type="application/json", status_code=409)
    return out


@router.post("/agreements/{action}")
async def agreements_action(action: str, payload: dict = Body(default_factory=dict), ctx: CommandContext = Depends(command_context)):
    return await _run("agreements", action, payload, ctx)


@router.post("/documents/{action}")
async def documents_action(action: str, payload: dict = Body(default_factory=dict), ctx: CommandContext = Depends(command_context)):
    return await _run("documents", action, payload, ctx)


@router.post("/ledger/{action}")
async def ledger_action(action: str, payload: dict = Body(default_factory=dict), ctx: CommandContext = Depends(command_context)):
    return await _run("ledger", action, payload, ctx)
