"""Finance core (spec §3.2, §5.2, §6.1–6.4; invariants 4, 5, 6, 15).

Cost items are single economic expenses whose estimate → quote → invoice → paid observations are NOT additive
(active_amount = invoiced else quoted else estimated). Evidence (ledger rows, emailed invoices, receipts) is matched
to ONE item: exact identity (supplier + invoice/order number) matches; anything fuzzier is proposed/ambiguous and
needs review; a material amount/currency discrepancy stays `conflict` and is never averaged.

Payments are financial facts; allocation to an obligation (invoice) is a separate, owner-confirmed step
(invariant 4). Only a confirmed allocation that satisfies a deposit invoice triggers the deposit handoff
(§5.2) — exactly once per invoice.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..core.errors import Blocked, Conflict, NotFound, ValidationFailed
from ..core.ids import new_id, sha256_hex
from ..core.money import convert, parse_amount, quantize, split_balanced
from ..core.time import ensure_aware
from ..domain.commands import REGISTRY, CommandContext, command, dispatch
from ..models.finance import (COST_CATEGORIES, CostAllocation, CostEvidence, CostItem, Invoice, Payment,
                              PaymentAllocation, Sale)
from ..models.runtime import WorkflowControl
from ..models.sales import Opportunity
from ..models.vehicles import Vehicle

OBSERVATION_KINDS = ("estimate", "quote", "invoice", "receipt", "ledger", "paid", "credit")
EVIDENCE_KINDS = ("ledger_row", "email_invoice", "email_receipt", "quote", "receipt_photo", "manual", "credit_note")
MATCH_STATES = ("matched", "proposed", "ambiguous", "unmatched", "ignored", "conflict")
INVOICE_KINDS = ("deposit", "balance", "reservation", "shipping")
PAYMENT_CONFIRMED = ("completed", "partially_refunded")
ZERO = Decimal("0")
AMOUNT_TOLERANCE = Decimal("1.00")          # absolute tolerance for "same amount" corroboration
FUZZY_DAYS = 45                            # date proximity window for fuzzy candidates


# ── helpers ──────────────────────────────────────────────────────────────────
def norm_text(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def norm_ref(s: str | None) -> str | None:
    n = re.sub(r"[^a-z0-9]+", "", (s or "").lower())
    return n or None


def money(amount: Decimal | None, currency: str | None) -> dict | None:
    if amount is None or not currency:
        return None
    return {"amount": str(quantize(parse_amount(amount), currency)), "currency": currency.upper()}


def iso(dt: datetime | date | None) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return ensure_aware(dt).isoformat()
    return dt.isoformat()


def _d(v) -> Decimal:
    return parse_amount(v) if v is not None else ZERO


def net_amount(item: CostItem) -> Decimal | None:
    a = item.active_amount
    if a is None:
        return None
    return a - (item.amount_credited or ZERO)


def serialize_cost_item(c: CostItem, allocations: list[CostAllocation] | None = None) -> dict:
    cur = c.currency
    active = c.active_amount
    return {
        "id": c.id, "version": c.version, "vehicle_id": c.vehicle_id, "category": c.category, "description": c.description,
        "vendor_name": c.vendor_name, "vendor_contact_id": c.vendor_contact_id, "currency": cur, "status": c.status,
        "amount_estimated": money(c.amount_estimated, cur), "amount_quoted": money(c.amount_quoted, cur),
        "amount_invoiced": money(c.amount_invoiced, cur), "amount_paid": money(c.amount_paid or ZERO, cur),
        "amount_credited": money(c.amount_credited or ZERO, cur), "active_amount": money(active, cur),
        "net_amount": money(net_amount(c), cur),
        "remaining_payable": money(max(ZERO, (active or ZERO) - (c.amount_credited or ZERO) - (c.amount_paid or ZERO)), cur)
        if active is not None else None,
        "tax_amount": money(c.tax_amount, cur), "freight_amount": money(c.freight_amount, cur),
        "discount_amount": money(c.discount_amount, cur),
        "invoice_ref": c.invoice_ref, "order_ref": c.order_ref, "occurred_at": iso(c.occurred_at),
        "fx": {"rate": str(c.fx_rate) if c.fx_rate is not None else None, "source": c.fx_source, "date": iso(c.fx_date),
               "usd_amount": money(c.usd_amount, "USD"), "actual": bool(c.fx_actual)},
        "shared": c.shared, "is_estimate_only": c.is_estimate_only, "allocation_state": c.allocation_state,
        "observations": c.observations or [], "restatements": c.restatements or [], "credits": c.credits or [],
        "restated_from_id": c.restated_from_id, "restatement_note": c.restatement_note, "notes": c.notes,
        "is_pass_through": c.is_pass_through, "dedupe_key": c.dedupe_key,
        "allocations": [serialize_allocation(a) for a in allocations] if allocations is not None else None,
        "created_at": iso(c.created_at),
    }


def serialize_allocation(a: CostAllocation) -> dict:
    return {"id": a.id, "version": a.version, "cost_item_id": a.cost_item_id, "vehicle_id": a.vehicle_id,
            "amount": money(a.amount, a.currency), "amount_credited": money(a.amount_credited or ZERO, a.currency),
            "net_amount": money((a.amount or ZERO) - (a.amount_credited or ZERO), a.currency),
            "basis": a.basis, "state": a.state, "weight": str(a.weight) if a.weight is not None else None,
            "components": a.components or {}, "credits": a.credits or [], "review_reasons": a.review_reasons or [],
            "confirmed_by": a.confirmed_by, "confirmed_at": iso(a.confirmed_at)}


def serialize_evidence(e: CostEvidence) -> dict:
    return {"id": e.id, "version": e.version, "cost_item_id": e.cost_item_id, "kind": e.kind, "source_ref": e.source_ref,
            "source_hash": e.source_hash, "source_link": source_link(e), "extracted": e.extracted or {},
            "amount": money(e.amount, e.currency), "vendor": e.vendor, "invoice_no": e.invoice_no, "order_no": e.order_no,
            "occurred_at": iso(e.occurred_at), "vehicle_ref_text": e.vehicle_ref_text,
            "proposed_vehicle_id": e.proposed_vehicle_id, "proposed_cost_item_id": e.proposed_cost_item_id,
            "proposed_category": e.proposed_category, "confidence": e.confidence, "match_state": e.match_state,
            "match_reasons": e.match_reasons or [], "discrepancy": e.discrepancy or {}, "candidates": e.candidates or [],
            "reviewed_by": e.reviewed_by, "reviewed_at": iso(e.reviewed_at), "is_settlement": e.is_settlement,
            "is_credit": e.is_credit, "revision": e.revision, "supersedes_id": e.supersedes_id, "is_current": e.is_current,
            "ledger_row_id": e.ledger_row_id, "review_note": e.review_note, "applied": e.applied or {},
            "created_at": iso(e.created_at)}


def source_link(e: CostEvidence) -> str | None:
    """Human-openable link for the evidence source (no side effects; spec §6.4)."""
    ref = e.source_ref or ""
    if ref.startswith("ledger:"):
        parts = ref.split(":")
        if len(parts) >= 3:
            gid = parts[2] if parts[2] else "0"
            return f"https://docs.google.com/spreadsheets/d/{parts[1]}/edit#gid={gid}"
    if ref.startswith("gmail:") or ref.startswith("message:"):
        return f"/inbox/messages/{ref.split(':', 1)[1]}"
    if ref.startswith("asset:"):
        return f"/files/{ref.split(':', 1)[1]}"
    return None


def serialize_payment(p: Payment) -> dict:
    return {"id": p.id, "version": p.version, "provider": p.provider, "merchant_id": p.merchant_id, "location_id": p.location_id,
            "provider_payment_id": p.provider_payment_id, "provider_order_id": p.provider_order_id,
            "provider_customer_id": p.provider_customer_id, "provider_invoice_id": p.provider_invoice_id,
            "amount": money(p.amount, p.currency), "fee_amount": money(p.fee_amount, p.currency),
            "net_amount": money(p.net_amount, p.currency), "refunded_amount": money(p.refunded_amount or ZERO, p.currency),
            "available_amount": money(p.available_amount, p.currency), "allocated_amount": money(p.allocated_amount or ZERO, p.currency),
            "unallocated_amount": money(p.available_amount - (p.allocated_amount or ZERO), p.currency),
            "status": p.status, "status_label": payment_status_label(p), "payer_name": p.payer_name, "payer_email": p.payer_email,
            "contact_id": p.contact_id, "occurred_at": iso(p.occurred_at), "provider_version": p.provider_version,
            "fetched_at": iso(p.fetched_at), "source_kind": p.source_kind, "source_ref": p.source_ref,
            "confirmed_by": p.confirmed_by, "confirmed_at": iso(p.confirmed_at), "refunds": p.refunds or [],
            "disputes": p.disputes or [], "is_payout": p.is_payout, "report_flags": p.report_flags or [],
            "report_source": p.report_source or {}, "provider_event_ids": p.provider_event_ids or [],
            "exceptions": p.exceptions or [], "method": p.method, "evidence_asset_id": p.evidence_asset_id,
            "evidence_ref": p.evidence_ref, "created_at": iso(p.created_at)}


def payment_status_label(p: Payment) -> str:
    if p.is_payout:
        return "Payout to bank (not customer revenue)"
    return {"reported": "Payment reported — needs confirmation", "pending": "Pending with provider",
            "completed": "Confirmed", "refunded": "Refunded", "partially_refunded": "Partially refunded",
            "disputed": "Disputed", "failed": "Failed", "cancelled": "Cancelled"}.get(p.status, p.status)


def serialize_invoice(i: Invoice) -> dict:
    remaining = (i.amount_due or ZERO) - (i.amount_allocated or ZERO)
    return {"id": i.id, "version": i.version, "kind": i.kind, "contact_id": i.contact_id, "opportunity_id": i.opportunity_id,
            "import_request_id": i.import_request_id, "sale_id": i.sale_id, "vehicle_id": i.vehicle_id,
            "agreement_id": i.agreement_id, "amount_due": money(i.amount_due, i.currency),
            "amount_allocated": money(i.amount_allocated or ZERO, i.currency),
            "remaining": money(max(ZERO, remaining), i.currency), "overpaid_by": money(-remaining, i.currency) if remaining < 0 else None,
            "due_at": iso(i.due_at), "status": i.status, "provider_invoice_id": i.provider_invoice_id,
            "satisfied_at": iso(i.satisfied_at), "notes": i.notes, "terms_source": i.terms_source, "dedupe_key": i.dedupe_key,
            "handoff": i.handoff or {}, "reopened_at": iso(i.reopened_at), "exceptions": i.exceptions or [],
            "created_at": iso(i.created_at)}


def serialize_payment_allocation(a: PaymentAllocation) -> dict:
    return {"id": a.id, "version": a.version, "payment_id": a.payment_id, "invoice_id": a.invoice_id,
            "amount": money(a.amount, a.currency), "applied_amount": money(a.applied_amount, a.applied_currency) if a.applied_amount is not None else None,
            "state": a.state, "confirmed_by": a.confirmed_by, "confirmed_at": iso(a.confirmed_at),
            "reversed_at": iso(a.reversed_at), "reason": a.reason, "candidate_group": a.candidate_group,
            "ambiguous": a.ambiguous, "conversion": a.conversion or {}, "flags": a.flags or [],
            "proposed_by": a.proposed_by, "reversal_kind": a.reversal_kind, "created_at": iso(a.created_at)}


async def _item(ctx: CommandContext, item_id: str, expected_version: int | None = None) -> CostItem:
    c = (await ctx.db.execute(select(CostItem).where(CostItem.id == item_id).with_for_update())).scalar_one_or_none()
    if c is None:
        raise NotFound("cost item not found")
    if expected_version is not None and c.version != expected_version:
        raise Conflict("cost item changed since you loaded it", current_version=c.version)
    return c


async def _evidence(ctx: CommandContext, evidence_id: str, expected_version: int | None = None) -> CostEvidence:
    e = (await ctx.db.execute(select(CostEvidence).where(CostEvidence.id == evidence_id).with_for_update())).scalar_one_or_none()
    if e is None:
        raise NotFound("evidence not found")
    if expected_version is not None and e.version != expected_version:
        raise Conflict("evidence changed since you loaded it", current_version=e.version)
    return e


async def _allocations_of(db, item_id: str) -> list[CostAllocation]:
    return list((await db.execute(select(CostAllocation).where(CostAllocation.cost_item_id == item_id)
                                  .order_by(CostAllocation.created_at))).scalars().all())


def _mark_changed(ctx: CommandContext, kind: str, row) -> None:
    """Keep the result envelope's version for a row bumped outside ctx.touch (secondary rows in the same command),
    so a client's next expected_version is the version that was actually written."""
    for entry in ctx.changed:
        if entry.get("kind") == kind and entry.get("id") == row.id:
            entry["version"] = row.version
            return
    ctx.changed.append({"kind": kind, "id": row.id, "version": row.version})


def _emit_cost(ctx: CommandContext, c: CostItem, change: str, **extra) -> None:
    ctx.emit("cost.changed", aggregate_type="cost_item", aggregate_id=c.id, aggregate_version=c.version,
             payload={"cost_item_id": c.id, "vehicle_id": c.vehicle_id, "change": change, "status": c.status, **extra})
    ctx.emit("metrics.invalidated", aggregate_type="cost_item", aggregate_id=c.id,
             payload={"reason": f"cost.{change}", "vehicle_id": c.vehicle_id})


def _observe(ctx: CommandContext, c: CostItem, kind: str, amount: Decimal | None, currency: str | None, *,
             source_ref: str | None = None, evidence_id: str | None = None, invoice_no: str | None = None,
             order_no: str | None = None, note: str | None = None) -> dict:
    """Attach one observation. Amounts replace the stage value (never additive); paying settles, it does not add cost."""
    if kind not in OBSERVATION_KINDS:
        raise ValidationFailed(f"observation kind must be one of {OBSERVATION_KINDS}")
    cur = (currency or c.currency).upper()
    amt = quantize(parse_amount(amount), cur) if amount is not None else None
    if amt is not None and cur != c.currency and kind not in ("paid",):
        raise Conflict("observation currency differs from the cost item currency; record a conflict or set fx explicitly",
                       item_currency=c.currency, observation_currency=cur)
    obs = {"kind": kind, "amount": str(amt) if amt is not None else None, "currency": cur, "at": ctx.now.isoformat(),
           "source_ref": source_ref, "evidence_id": evidence_id, "by": ctx.actor.user_id, "note": note}
    order = ["estimated", "quoted", "invoiced", "partially_paid", "paid"]
    prev_status = c.status

    def _advance(new: str) -> None:
        if c.status in ("credited", "cancelled"):
            return
        if order.index(new) > order.index(c.status if c.status in order else "estimated"):
            c.status = new

    if kind == "estimate":
        if amt is not None:
            c.amount_estimated = amt
    elif kind == "quote":
        if amt is not None:
            c.amount_quoted = amt
            c.is_estimate_only = c.amount_invoiced is None
        _advance("quoted")
    elif kind in ("invoice", "ledger"):
        if amt is not None:
            if c.amount_invoiced is None:
                c.amount_invoiced = amt
            elif c.amount_invoiced != amt:
                obs["discrepancy"] = {"existing_invoiced": str(c.amount_invoiced), "observed": str(amt)}
            c.is_estimate_only = False
        if invoice_no and not c.invoice_ref:
            c.invoice_ref = invoice_no
        if order_no and not c.order_ref:
            c.order_ref = order_no
        _advance("invoiced")
    elif kind == "receipt":
        # a receipt proves the purchase, not settlement; corroborates the invoiced amount
        if amt is not None and c.amount_invoiced is None:
            c.amount_invoiced = amt
            c.is_estimate_only = False
            _advance("invoiced")
    elif kind == "paid":
        if amt is None:
            raise ValidationFailed("a paid observation needs an amount")
        c.amount_paid = (c.amount_paid or ZERO) + amt
        active = c.active_amount
        if active is not None and c.amount_paid >= active - (c.amount_credited or ZERO):
            _advance("paid")
        else:
            _advance("partially_paid")
    elif kind == "credit":
        if amt is None:
            raise ValidationFailed("a credit observation needs an amount")
        c.amount_credited = (c.amount_credited or ZERO) + amt
    c.observations = list(c.observations or []) + [obs]
    obs["status_from"], obs["status_to"] = prev_status, c.status
    return obs


# ── cost items ───────────────────────────────────────────────────────────────
class CostCreateIn(BaseModel):
    category: str = "other"
    vehicle_id: str | None = None
    description: str = ""
    vendor_name: str | None = None
    vendor_contact_id: str | None = None
    currency: str = "USD"
    amount_estimated: Decimal | None = None
    amount_quoted: Decimal | None = None
    amount_invoiced: Decimal | None = None
    tax_amount: Decimal | None = None
    freight_amount: Decimal | None = None
    discount_amount: Decimal | None = None
    invoice_ref: str | None = None
    order_ref: str | None = None
    occurred_at: datetime | None = None
    fx_rate: Decimal | None = None
    fx_source: str | None = None
    fx_date: date | None = None
    usd_amount: Decimal | None = None
    fx_actual: bool = False
    source_ref: str | None = None
    dedupe_key: str | None = None
    is_pass_through: bool = False
    notes: str = ""


@command("costs.create_item", input=CostCreateIn, perm="finance.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)] if p.vehicle_id else [],
         description="Create one economic cost item with its first estimate/quote/invoice observation (not additive).")
async def costs_create_item(ctx: CommandContext, inp: CostCreateIn) -> dict:
    if inp.category not in COST_CATEGORIES:
        raise ValidationFailed(f"category must be one of {COST_CATEGORIES}")
    cur = inp.currency.upper()
    if len(cur) != 3:
        raise ValidationFailed("currency must be an ISO code")
    if inp.dedupe_key:
        ex = (await ctx.db.execute(select(CostItem).where(CostItem.dedupe_key == inp.dedupe_key))).scalars().first()
        if ex is not None:
            return {"cost_item": serialize_cost_item(ex), "created": False}
    if inp.vehicle_id and await ctx.db.get(Vehicle, inp.vehicle_id) is None:
        raise NotFound("vehicle not found")
    c = CostItem(category=inp.category, vehicle_id=inp.vehicle_id, description=inp.description.strip(),
                 vendor_name=inp.vendor_name, vendor_norm=norm_text(inp.vendor_name) or None,
                 vendor_contact_id=inp.vendor_contact_id, currency=cur, invoice_ref=inp.invoice_ref, order_ref=inp.order_ref,
                 occurred_at=inp.occurred_at, tax_amount=quantize(inp.tax_amount, cur) if inp.tax_amount is not None else None,
                 freight_amount=quantize(inp.freight_amount, cur) if inp.freight_amount is not None else None,
                 discount_amount=quantize(inp.discount_amount, cur) if inp.discount_amount is not None else None,
                 dedupe_key=inp.dedupe_key, notes=inp.notes, is_pass_through=inp.is_pass_through,
                 amount_paid=ZERO, amount_credited=ZERO, status="estimated", observations=[],
                 created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
    ctx.db.add(c)
    await ctx.db.flush()
    for kind, amt in (("estimate", inp.amount_estimated), ("quote", inp.amount_quoted), ("invoice", inp.amount_invoiced)):
        if amt is not None:
            _observe(ctx, c, kind, amt, cur, source_ref=inp.source_ref, invoice_no=inp.invoice_ref, order_no=inp.order_ref)
    if c.active_amount is None:
        raise ValidationFailed("a cost item needs at least an estimated, quoted or invoiced amount")
    _apply_fx(c, inp.fx_rate, inp.fx_source, inp.fx_date, inp.usd_amount, inp.fx_actual)
    if not c.vehicle_id:
        c.allocation_state = "none"
    ctx.changed.append({"kind": "cost_item", "id": c.id, "version": c.version})
    ctx.record(f"Cost item created: {c.description or c.category} {c.active_amount} {cur}", entity_kind="cost_item",
               entity_id=c.id, kind="payment", state=c.status, visibility="finance",
               sources=[inp.source_ref] if inp.source_ref else None, details={"vehicle_id": c.vehicle_id, "category": c.category})
    _emit_cost(ctx, c, "created")
    return {"cost_item": serialize_cost_item(c), "created": True}


def _apply_fx(c: CostItem, rate, source, fx_date, usd_amount, actual: bool) -> None:
    if c.currency == "USD":
        c.usd_amount = c.active_amount
        c.fx_actual = True
        return
    if usd_amount is not None and actual:
        c.usd_amount = quantize(usd_amount, "USD")
        c.fx_actual = True
        c.fx_source = source or c.fx_source or "bank"
        c.fx_date = fx_date or c.fx_date
        c.fx_rate = rate if rate is not None else c.fx_rate
        return
    if rate is not None:
        c.fx_rate = rate
        c.fx_source = source
        c.fx_date = fx_date
        if not c.fx_actual and c.active_amount is not None:
            c.usd_amount = convert(c.active_amount, rate, "USD")


class CostObserveIn(BaseModel):
    cost_item_id: str
    kind: str                      # estimate|quote|invoice|receipt|ledger|paid|credit
    amount: Decimal | None = None
    currency: str | None = None
    source_ref: str | None = None
    evidence_id: str | None = None
    invoice_no: str | None = None
    order_no: str | None = None
    note: str | None = None
    expected_version: int | None = None


@command("costs.observe", input=CostObserveIn, perm="finance.write", action_class="internal",
         description="Attach a new observation (estimate → quoted → invoiced → paid). Amounts replace stages; paying settles.")
async def costs_observe(ctx: CommandContext, inp: CostObserveIn) -> dict:
    c = await _item(ctx, inp.cost_item_id, inp.expected_version)
    if inp.kind == "credit":
        return await _record_credit(ctx, c, inp.amount, inp.note or "credit", inp.evidence_id, inp.source_ref, None)
    obs = _observe(ctx, c, inp.kind, inp.amount, inp.currency, source_ref=inp.source_ref, evidence_id=inp.evidence_id,
                   invoice_no=inp.invoice_no, order_no=inp.order_no, note=inp.note)
    if c.currency == "USD":
        c.usd_amount = c.active_amount
    elif c.fx_rate is not None and not c.fx_actual:
        c.usd_amount = convert(c.active_amount, c.fx_rate, "USD")
    await _rebalance_single(ctx, c)
    ctx.touch(c, "cost_item")
    ctx.record(f"Cost {inp.kind} observed: {c.description or c.category} {obs['amount']} {obs['currency']}",
               entity_kind="cost_item", entity_id=c.id, kind="payment", state=c.status, visibility="finance",
               sources=[inp.source_ref] if inp.source_ref else None, details=obs)
    _emit_cost(ctx, c, "observed", observation=inp.kind)
    return {"cost_item": serialize_cost_item(c), "observation": obs}


async def _rebalance_single(ctx: CommandContext, c: CostItem) -> None:
    """After the active amount changes: a single allocation follows it; a multi-vehicle split cannot be re-derived
    without a decision, so it is marked unbalanced AND each line carries the imbalance as a review reason —
    invariant 5 is never silently broken (the per-vehicle money would otherwise under/over-report with no flag)."""
    allocs = await _allocations_of(ctx.db, c.id)
    if not allocs:
        return
    active = c.active_amount or ZERO
    if len(allocs) == 1:
        if allocs[0].amount != active:
            allocs[0].amount = active
            allocs[0].bump(ctx.actor.user_id)
        c.allocation_state = "balanced" if allocs[0].state == "confirmed" else "proposed"
        return
    total = sum(a.amount for a in allocs)
    if total != active:
        c.allocation_state = "unbalanced"
        reason = (f"allocations total {total} {c.currency} no longer balance to the source amount {active} {c.currency}; "
                  "re-allocate before these costs are read as recorded")
        for a in allocs:
            if reason not in (a.review_reasons or []):
                a.review_reasons = list(a.review_reasons or []) + [reason]
                a.bump(ctx.actor.user_id)
                ctx.changed.append({"kind": "cost_allocation", "id": a.id, "version": a.version})
    else:
        c.allocation_state = "balanced" if all(a.state == "confirmed" for a in allocs) else "proposed"


class CostFxIn(BaseModel):
    cost_item_id: str
    rate: Decimal | None = None
    source: str | None = None
    fx_date: date | None = None
    usd_amount: Decimal | None = None
    actual: bool = False           # True = actual bank conversion (wins over rate estimates)
    expected_version: int | None = None


@command("costs.set_fx", input=CostFxIn, perm="finance.write", action_class="internal",
         description="Record the FX rate/source/date for a non-USD cost; an actual bank conversion wins when provided.")
async def costs_set_fx(ctx: CommandContext, inp: CostFxIn) -> dict:
    c = await _item(ctx, inp.cost_item_id, inp.expected_version)
    if c.currency == "USD":
        raise ValidationFailed("cost item is already in USD")
    if inp.actual and inp.usd_amount is None:
        raise ValidationFailed("an actual conversion needs usd_amount")
    if inp.rate is None and inp.usd_amount is None:
        raise ValidationFailed("rate or usd_amount required")
    if inp.rate is not None and not inp.source:
        raise ValidationFailed("an FX rate needs its source")
    if c.fx_actual and not inp.actual:
        # estimates never overwrite an actual bank conversion; keep it, record the estimate for reference
        c.fx_rate, c.fx_source, c.fx_date = inp.rate, inp.source, inp.fx_date
    else:
        _apply_fx(c, inp.rate, inp.source, inp.fx_date, inp.usd_amount, inp.actual)
    ctx.touch(c, "cost_item")
    ctx.record(f"FX set for cost {c.description or c.category}: {c.usd_amount} USD ({'actual' if c.fx_actual else 'estimated'})",
               entity_kind="cost_item", entity_id=c.id, kind="payment", state=c.status, visibility="finance",
               details={"rate": str(inp.rate) if inp.rate is not None else None, "source": inp.source, "date": iso(inp.fx_date)})
    _emit_cost(ctx, c, "fx")
    return {"cost_item": serialize_cost_item(c)}


class CostRestateIn(BaseModel):
    cost_item_id: str
    field: str                       # amount_invoiced|amount_quoted|amount_estimated|category|vehicle_id|tax_amount|freight_amount
    new_value: str | None
    note: str = Field(min_length=1)
    expected_version: int | None = None


RESTATABLE = ("amount_invoiced", "amount_quoted", "amount_estimated", "category", "vehicle_id", "tax_amount", "freight_amount")


@command("costs.restate", input=CostRestateIn, perm="finance.write", action_class="internal",
         description="Late correction: change a recorded value with a restatement note; sold vehicles get the note on their sale.")
async def costs_restate(ctx: CommandContext, inp: CostRestateIn) -> dict:
    if inp.field not in RESTATABLE:
        raise ValidationFailed(f"field must be one of {RESTATABLE}")
    c = await _item(ctx, inp.cost_item_id, inp.expected_version)
    old = getattr(c, inp.field)
    if inp.field.startswith("amount") or inp.field.endswith("_amount"):
        new = quantize(parse_amount(inp.new_value), c.currency) if inp.new_value is not None else None
    elif inp.field == "category":
        if inp.new_value not in COST_CATEGORIES:
            raise ValidationFailed(f"category must be one of {COST_CATEGORIES}")
        new = inp.new_value
    else:
        new = inp.new_value
    setattr(c, inp.field, new)
    entry = {"at": ctx.now.isoformat(), "by": ctx.actor.user_id, "field": inp.field, "old": str(old) if old is not None else None,
             "new": str(new) if new is not None else None, "note": inp.note}
    c.restatements = list(c.restatements or []) + [entry]
    c.restatement_note = inp.note
    if c.currency == "USD":
        c.usd_amount = c.active_amount
    elif c.fx_rate is not None and not c.fx_actual:
        c.usd_amount = convert(c.active_amount, c.fx_rate, "USD")
    await _rebalance_single(ctx, c)
    ctx.touch(c, "cost_item")
    sold = await _restate_sold_sales(ctx, c, entry)
    ctx.record(f"Cost restated ({inp.field}): {c.description or c.category} — {inp.note}", entity_kind="cost_item",
               entity_id=c.id, kind="payment", state=c.status, visibility="finance", exception=True, details=entry)
    _emit_cost(ctx, c, "restated", restatement=entry, sales=sold)
    return {"cost_item": serialize_cost_item(c), "restatement": entry, "sales_restated": sold}


async def _vehicle_ids_of(db, c: CostItem) -> set[str]:
    ids = {c.vehicle_id} if c.vehicle_id else set()
    for a in await _allocations_of(db, c.id):
        ids.add(a.vehicle_id)
    return ids


async def _restate_sold_sales(ctx: CommandContext, c: CostItem, entry: dict) -> list[str]:
    """K03: a late cost correction on a sold vehicle restates the original sale cohort with a visible note."""
    vids = await _vehicle_ids_of(ctx.db, c)
    if not vids:
        return []
    sales = (await ctx.db.execute(select(Sale).where(Sale.vehicle_id.in_(list(vids)), Sale.completed_at.is_not(None),
                                                     Sale.status.notin_(("cancelled", "expired"))))).scalars().all()
    out = []
    for s in sales:
        s.restatements = list(s.restatements or []) + [{**entry, "kind": "cost", "cost_item_id": c.id,
                                                        "cohort_date": iso(s.completed_at)}]
        s.bump(ctx.actor.user_id)
        ctx.changed.append({"kind": "sale", "id": s.id, "version": s.version})
        ctx.emit("metrics.invalidated", aggregate_type="sale", aggregate_id=s.id,
                 payload={"reason": "cost.restated", "cohort_date": iso(s.completed_at), "cost_item_id": c.id})
        out.append(s.id)
    return out


class CostCreditIn(BaseModel):
    cost_item_id: str
    amount: Decimal
    reason: str = Field(min_length=1)
    evidence_id: str | None = None
    source_ref: str | None = None
    allocations: list[dict] | None = None   # [{vehicle_id, amount}] explicit; None = proportional to allocations
    expected_version: int | None = None


@command("costs.record_credit", input=CostCreditIn, perm="finance.write", action_class="internal",
         description="Partial return / vendor credit: reduces the item via amount_credited with credit allocations traceable "
                     "to the original allocations (E03); sold vehicles get a restatement note (K03).")
async def costs_record_credit(ctx: CommandContext, inp: CostCreditIn) -> dict:
    c = await _item(ctx, inp.cost_item_id, inp.expected_version)
    return await _record_credit(ctx, c, inp.amount, inp.reason, inp.evidence_id, inp.source_ref, inp.allocations)


async def _record_credit(ctx: CommandContext, c: CostItem, amount, reason: str, evidence_id: str | None,
                         source_ref: str | None, explicit: list[dict] | None) -> dict:
    if amount is None:
        raise ValidationFailed("credit amount required")
    amt = quantize(parse_amount(amount), c.currency)
    if amt <= 0:
        raise ValidationFailed("credit amount must be positive")
    active = c.active_amount or ZERO
    if (c.amount_credited or ZERO) + amt > active:
        raise ValidationFailed("credit exceeds the active cost amount", active=str(active), credited=str(c.amount_credited))
    credit_id = new_id()
    allocs = await _allocations_of(ctx.db, c.id)
    shares: list[tuple[CostAllocation, Decimal]] = []
    if allocs:
        if explicit:
            by_vehicle = {a.vehicle_id: a for a in allocs}
            total = ZERO
            for row in explicit:
                a = by_vehicle.get(row.get("vehicle_id"))
                if a is None:
                    raise ValidationFailed(f"no allocation for vehicle {row.get('vehicle_id')}")
                part = quantize(parse_amount(row.get("amount")), c.currency)
                if (a.amount_credited or ZERO) + part > a.amount:
                    raise ValidationFailed("credit exceeds the original allocation", vehicle_id=a.vehicle_id)
                shares.append((a, part))
                total += part
            if total != amt:
                raise ValidationFailed("credit allocations must balance to the credit amount", total=str(total), credit=str(amt))
        else:
            parts = split_balanced(amt, [a.amount for a in allocs], c.currency)
            shares = list(zip(allocs, parts))
        for a, part in shares:
            a.amount_credited = (a.amount_credited or ZERO) + part
            a.credits = list(a.credits or []) + [{"credit_id": credit_id, "amount": str(part), "at": ctx.now.isoformat(),
                                                  "reason": reason, "evidence_id": evidence_id, "original_allocation_id": a.id}]
            a.bump(ctx.actor.user_id)
            ctx.changed.append({"kind": "cost_allocation", "id": a.id, "version": a.version})
    obs = _observe(ctx, c, "credit", amt, c.currency, source_ref=source_ref, evidence_id=evidence_id, note=reason)
    c.credits = list(c.credits or []) + [{"credit_id": credit_id, "amount": str(amt), "at": ctx.now.isoformat(), "reason": reason,
                                          "evidence_id": evidence_id, "allocations": [{"allocation_id": a.id, "vehicle_id": a.vehicle_id,
                                                                                       "amount": str(p)} for a, p in shares]}]
    if (c.amount_credited or ZERO) >= active:
        c.status = "credited"
    if c.currency == "USD":
        c.usd_amount = c.active_amount
    ctx.touch(c, "cost_item")
    entry = {"at": ctx.now.isoformat(), "by": ctx.actor.user_id, "field": "amount_credited", "old": str(c.amount_credited - amt),
             "new": str(c.amount_credited), "note": f"credit: {reason}"}
    sold = await _restate_sold_sales(ctx, c, entry)
    ctx.record(f"Credit {amt} {c.currency} on cost {c.description or c.category}: {reason}", entity_kind="cost_item",
               entity_id=c.id, kind="payment", state=c.status, visibility="finance",
               sources=[source_ref] if source_ref else None, details={"credit_id": credit_id, "evidence_id": evidence_id})
    _emit_cost(ctx, c, "credited", credit_id=credit_id, sales=sold)
    return {"cost_item": serialize_cost_item(c, await _allocations_of(ctx.db, c.id)), "credit_id": credit_id, "observation": obs,
            "sales_restated": sold}


# ── evidence matching (spec §6.1) ────────────────────────────────────────────
class EvidenceIn(BaseModel):
    kind: str                            # ledger_row|email_invoice|email_receipt|quote|receipt_photo|manual|credit_note
    source_ref: str | None = None        # message id / ledger row ref / asset id (identity of the source)
    source_hash: str | None = None       # raw content hash; a different hash for the same source_ref = new revision
    extracted: dict = Field(default_factory=dict)
    amount: Decimal | None = None
    currency: str | None = None
    vendor: str | None = None
    invoice_no: str | None = None
    order_no: str | None = None
    occurred_at: datetime | None = None
    vehicle_ref_text: str | None = None
    vehicle_id: str | None = None        # known vehicle (explicit evidence)
    cost_item_id: str | None = None      # explicit link (owner/finance knows the item)
    category: str | None = None
    is_credit: bool = False
    is_settlement: bool = False          # only true when the ledger mapping declares a verified settlement column
    ledger_row_id: str | None = None
    create_if_unmatched: bool = False    # ledger import: an unmatched row is a real expense, never dropped
    confidence: str = "low"


def _amounts_agree(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= AMOUNT_TOLERANCE


async def _resolve_vehicle_ref(db, text: str | None) -> Vehicle | None:
    if not text:
        return None
    t = text.strip()
    m = re.search(r"STK[-\s]?(\d{3,5})", t, re.I)
    rows = []
    if m:
        rows = (await db.execute(select(Vehicle).where(func.upper(Vehicle.stock_no) == f"STK-{int(m.group(1)):04d}"))).scalars().all()
    if not rows:
        n = norm_text(t).upper()
        if len(n) >= 6:
            rows = (await db.execute(select(Vehicle).where(Vehicle.frame_no_norm == n))).scalars().all()
    return rows[0] if len(rows) == 1 else None


async def match_evidence(db, fields: dict, *, exclude_evidence_id: str | None = None) -> dict:
    """Deterministic matching (spec §6.1, §3.3). Returns {state, cost_item_id, candidates, reasons, discrepancy, vehicle_id}.
    Pure read; used by record_evidence and by the ledger preview (dry run)."""
    vendor_n = norm_text(fields.get("vendor"))
    inv_n, ord_n = norm_ref(fields.get("invoice_no")), norm_ref(fields.get("order_no"))
    amount = quantize(parse_amount(fields["amount"]), (fields.get("currency") or "USD")) if fields.get("amount") is not None else None
    currency = (fields.get("currency") or "").upper() or None
    when = ensure_aware(fields.get("occurred_at"))
    reasons: list[str] = []
    vehicle = await _resolve_vehicle_ref(db, fields.get("vehicle_ref_text"))
    vehicle_id = fields.get("vehicle_id") or (vehicle.id if vehicle else None)
    if fields.get("vehicle_ref_text") and not vehicle_id:
        reasons.append(f"vehicle reference '{fields['vehicle_ref_text']}' did not resolve to one vehicle")

    if fields.get("cost_item_id"):
        return {"state": "matched", "cost_item_id": fields["cost_item_id"], "candidates": [], "reasons": ["explicit cost item"],
                "discrepancy": {}, "vehicle_id": vehicle_id, "confidence": "high"}

    # 1. exact identity: supplier + invoice/order number (on items or on already-matched evidence)
    exact: dict[str, list[str]] = {}
    if vendor_n and (inv_n or ord_n):
        items = (await db.execute(select(CostItem).where(CostItem.vendor_norm == vendor_n,
                                                         CostItem.status != "cancelled"))).scalars().all()
        for it in items:
            why = []
            if inv_n and norm_ref(it.invoice_ref) == inv_n:
                why.append(f"invoice {fields.get('invoice_no')} matches item invoice ref")
            if ord_n and norm_ref(it.order_ref) == ord_n:
                why.append(f"order {fields.get('order_no')} matches item order ref")
            if why:
                exact.setdefault(it.id, []).extend(["supplier matches"] + why)
        q = select(CostEvidence).where(CostEvidence.cost_item_id.is_not(None), CostEvidence.is_current.is_(True),
                                       CostEvidence.match_state.in_(("matched", "conflict")))
        if exclude_evidence_id:
            q = q.where(CostEvidence.id != exclude_evidence_id)
        for ev in (await db.execute(q)).scalars().all():
            if norm_text(ev.vendor) != vendor_n:
                continue
            if (inv_n and norm_ref(ev.invoice_no) == inv_n) or (ord_n and norm_ref(ev.order_no) == ord_n):
                exact.setdefault(ev.cost_item_id, []).append(f"same supplier + reference as {ev.kind} evidence {ev.id[:8]}")
    if len(exact) == 1:
        item_id = next(iter(exact))
        it = await db.get(CostItem, item_id)
        rs = ["exact identity: " + "; ".join(dict.fromkeys(exact[item_id]))] + reasons
        discrepancy: dict = {}
        if amount is not None and currency and currency != it.currency:
            discrepancy = {"field": "currency", "evidence": f"{amount} {currency}", "item": f"{it.active_amount} {it.currency}"}
        elif amount is not None and it.active_amount is not None and not _amounts_agree(amount, it.active_amount) \
                and not fields.get("is_credit") and it.amount_invoiced is not None:
            discrepancy = {"field": "amount", "evidence": str(amount), "item": str(it.active_amount), "currency": it.currency,
                           "difference": str(amount - it.active_amount)}
        if discrepancy:
            rs.append(f"material discrepancy on {discrepancy['field']} — kept visible, not averaged")
            return {"state": "conflict", "cost_item_id": item_id, "candidates": [], "reasons": rs, "discrepancy": discrepancy,
                    "vehicle_id": vehicle_id or it.vehicle_id, "confidence": "high"}
        if amount is not None and it.active_amount is not None:
            rs.append("amount corroborates" if _amounts_agree(amount, it.active_amount) else "amount differs from the current estimate/quote")
        return {"state": "matched", "cost_item_id": item_id, "candidates": [], "reasons": rs, "discrepancy": {},
                "vehicle_id": vehicle_id or it.vehicle_id, "confidence": "high"}
    if len(exact) > 1:
        cands = [{"cost_item_id": k, "reasons": list(dict.fromkeys(v)), "score": 3} for k, v in exact.items()]
        return {"state": "ambiguous", "cost_item_id": None, "candidates": cands,
                "reasons": ["several items share this supplier + reference"] + reasons, "discrepancy": {}, "vehicle_id": vehicle_id,
                "confidence": "medium"}

    # 2. fuzzy: supplier + amount/currency (+ date proximity, + vehicle) → proposed / ambiguous; always reviewed
    cands = []
    if amount is not None:
        q = select(CostItem).where(CostItem.status != "cancelled")
        if currency:
            q = q.where(CostItem.currency == currency)
        ledger_kind = fields.get("kind") == "ledger_row"
        for it in (await db.execute(q)).scalars().all():
            if ledger_kind and (it.dedupe_key or "").startswith("evidence:ledger_row:"):
                # a second ledger row is a second expense unless an explicit reference says otherwise (E01: never drop)
                reasons.append(f"similar ledger row for {it.vendor_name or 'vendor'} kept as a distinct expense (no shared reference)") \
                    if vendor_n and it.vendor_norm == vendor_n and _amounts_agree(amount, it.active_amount) else None
                continue
            score, why = 0, []
            if vendor_n and it.vendor_norm == vendor_n:
                score += 2
                why.append("supplier matches")
            if _amounts_agree(amount, it.active_amount):
                score += 2
                why.append("amount matches")
            if when and it.occurred_at and abs((ensure_aware(it.occurred_at) - when).days) <= FUZZY_DAYS:
                score += 1
                why.append("date within window")
            if vehicle_id and it.vehicle_id == vehicle_id:
                score += 1
                why.append("vehicle matches")
            # an amount alone is never a candidate: it needs the supplier or the vehicle as well
            if "amount matches" in why and ("supplier matches" in why or "vehicle matches" in why):
                cands.append({"cost_item_id": it.id, "reasons": why, "score": score})
    cands.sort(key=lambda c: -c["score"])
    if len(cands) == 1 or (len(cands) > 1 and cands[0]["score"] > cands[1]["score"]):
        return {"state": "proposed", "cost_item_id": None, "candidates": cands,
                "reasons": ["fuzzy match needs review: " + ", ".join(cands[0]["reasons"])] + reasons, "discrepancy": {},
                "vehicle_id": vehicle_id, "confidence": "medium"}
    if len(cands) > 1:
        return {"state": "ambiguous", "cost_item_id": None, "candidates": cands,
                "reasons": ["several cost items could be this expense"] + reasons, "discrepancy": {}, "vehicle_id": vehicle_id,
                "confidence": "low"}
    return {"state": "unmatched", "cost_item_id": None, "candidates": [], "reasons": ["no cost item matches"] + reasons,
            "discrepancy": {}, "vehicle_id": vehicle_id, "confidence": "low"}


def _apply_evidence(ctx: CommandContext, ev: CostEvidence, c: CostItem) -> dict:
    """A matched observation corroborates the item (invoice → invoiced amount; receipt → proof of purchase;
    verified settlement → paid). Never applied for conflict/proposed states."""
    kind_map = {"quote": "quote", "email_invoice": "invoice", "ledger_row": "ledger", "email_receipt": "receipt",
                "receipt_photo": "receipt", "manual": "invoice", "credit_note": "credit"}
    okind = kind_map.get(ev.kind, "invoice")
    if ev.is_credit or okind == "credit":
        return {"kind": "credit", "applied": False, "note": "credit notes are applied through costs.record_credit"}
    applied: dict = {"kind": okind, "amount": str(ev.amount) if ev.amount is not None else None}
    if ev.amount is not None and ev.currency and ev.currency.upper() != c.currency:
        return {**applied, "applied": False, "note": "currency differs"}
    obs = _observe(ctx, c, okind, ev.amount, ev.currency or c.currency, source_ref=ev.source_ref, evidence_id=ev.id,
                   invoice_no=ev.invoice_no, order_no=ev.order_no)
    applied["applied"] = True
    if ev.is_settlement and ev.amount is not None:
        # ONE settlement source settles once: a later revision of the same source (changed ledger row) restates the
        # paid amount by its delta and never adds a second payment for the same money.
        prior = ZERO
        for o in (c.observations or []):
            if o.get("kind") != "paid" or o.get("amount") is None:
                continue
            if o.get("evidence_id") == ev.id or (ev.source_ref and o.get("source_ref") == ev.source_ref):
                prior += parse_amount(o["amount"])
        delta = quantize(ev.amount, c.currency) - prior
        if delta != 0:
            _observe(ctx, c, "paid", delta, c.currency, source_ref=ev.source_ref, evidence_id=ev.id,
                     note="verified settlement column" if prior == ZERO else
                          f"verified settlement restated from {prior} (same ledger source)")
            applied["settlement"] = True
            applied["settlement_delta"] = str(delta)
            settled = (c.amount_paid or ZERO)
            owed = (c.active_amount or ZERO) - (c.amount_credited or ZERO)
            if delta < 0 and c.status == "paid" and settled < owed:
                c.status = "partially_paid" if settled > ZERO else "invoiced"
        else:
            applied["settlement"] = "unchanged"
    if obs.get("discrepancy"):
        applied["discrepancy"] = obs["discrepancy"]
    return applied


@command("costs.record_evidence", input=EvidenceIn, perm="finance.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)] if p.vehicle_id else [],
         description="Record cost evidence (ledger row / emailed invoice / receipt) and match it to ONE cost item. "
                     "Exact supplier+reference identity matches; fuzzy matches are proposed for review; discrepancies conflict.")
async def costs_record_evidence(ctx: CommandContext, inp: EvidenceIn) -> dict:
    if inp.kind not in EVIDENCE_KINDS:
        raise ValidationFailed(f"kind must be one of {EVIDENCE_KINDS}")
    cur = inp.currency.upper() if inp.currency else None
    amt = quantize(parse_amount(inp.amount), cur or "USD") if inp.amount is not None else None
    prior = None
    if inp.source_ref:
        prior = (await ctx.db.execute(select(CostEvidence).where(CostEvidence.source_ref == inp.source_ref,
                                                                 CostEvidence.kind == inp.kind, CostEvidence.is_current.is_(True))
                                      .with_for_update())).scalars().first()
        if prior is not None and (inp.source_hash or "") == (prior.source_hash or ""):
            return {"evidence": serialize_evidence(prior), "created": False, "idempotent": True}
    fields = {"kind": inp.kind, "vendor": inp.vendor, "invoice_no": inp.invoice_no, "order_no": inp.order_no, "amount": amt, "currency": cur,
              "occurred_at": inp.occurred_at, "vehicle_ref_text": inp.vehicle_ref_text, "vehicle_id": inp.vehicle_id,
              "cost_item_id": inp.cost_item_id or (prior.cost_item_id if prior is not None and prior.match_state == "matched" else None),
              "is_credit": inp.is_credit}
    m = await match_evidence(ctx.db, fields, exclude_evidence_id=prior.id if prior else None)
    ev = CostEvidence(kind=inp.kind, source_ref=inp.source_ref, source_hash=inp.source_hash, extracted=inp.extracted, amount=amt,
                      currency=cur, vendor=inp.vendor, invoice_no=inp.invoice_no, order_no=inp.order_no, occurred_at=inp.occurred_at,
                      vehicle_ref_text=inp.vehicle_ref_text, proposed_vehicle_id=m["vehicle_id"], proposed_category=inp.category,
                      confidence=m["confidence"] if inp.confidence == "low" else inp.confidence, match_state=m["state"],
                      match_reasons=m["reasons"], discrepancy=m["discrepancy"], candidates=m["candidates"], is_credit=inp.is_credit,
                      is_settlement=inp.is_settlement, ledger_row_id=inp.ledger_row_id,
                      revision=(prior.revision + 1) if prior is not None else 1, supersedes_id=prior.id if prior is not None else None,
                      created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
    if m["state"] in ("matched", "conflict"):
        ev.cost_item_id = m["cost_item_id"]
    elif m["state"] == "proposed" and m["candidates"]:
        ev.proposed_cost_item_id = m["candidates"][0]["cost_item_id"]
    if prior is not None:
        prior.is_current = False
        prior.bump(ctx.actor.user_id)
    ctx.db.add(ev)
    await ctx.db.flush()
    ctx.changed.append({"kind": "cost_evidence", "id": ev.id, "version": ev.version})
    created_item = None
    if ev.match_state == "matched":
        c = await _item(ctx, ev.cost_item_id)
        ev.applied = _apply_evidence(ctx, ev, c)
        if ev.applied.get("discrepancy"):
            ev.match_state = "conflict"
            ev.discrepancy = {"field": "amount", **ev.applied["discrepancy"], "currency": c.currency}
            ev.match_reasons = list(ev.match_reasons) + ["invoiced amount already recorded differently — conflict kept visible"]
        if m["vehicle_id"] and not c.vehicle_id and not (await _allocations_of(ctx.db, c.id)):
            c.vehicle_id = m["vehicle_id"]
        await _rebalance_single(ctx, c)
        ctx.touch(c, "cost_item")
        _emit_cost(ctx, c, "evidence_matched", evidence_id=ev.id)
    elif ev.match_state == "unmatched" and inp.create_if_unmatched and amt is not None and cur:
        c = CostItem(category=inp.category if inp.category in COST_CATEGORIES else "other", vehicle_id=m["vehicle_id"],
                     description=(inp.extracted.get("description") or inp.vendor or inp.kind)[:200], vendor_name=inp.vendor,
                     vendor_norm=norm_text(inp.vendor) or None, currency=cur, invoice_ref=inp.invoice_no, order_ref=inp.order_no,
                     occurred_at=inp.occurred_at, amount_paid=ZERO, amount_credited=ZERO, status="estimated", observations=[],
                     dedupe_key=f"evidence:{inp.kind}:{inp.source_ref}" if inp.source_ref else None,
                     created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
        ctx.db.add(c)
        await ctx.db.flush()
        ev.cost_item_id = c.id
        ev.match_state = "matched"
        ev.match_reasons = list(ev.match_reasons) + ["no existing item: created from this evidence (real expense, not dropped)"]
        ev.applied = _apply_evidence(ctx, ev, c)
        if c.currency == "USD":
            c.usd_amount = c.active_amount
        ctx.changed.append({"kind": "cost_item", "id": c.id, "version": c.version})
        created_item = serialize_cost_item(c)
        _emit_cost(ctx, c, "created_from_evidence", evidence_id=ev.id)
    ctx.record(f"Cost evidence {ev.kind} {ev.match_state}: {ev.vendor or '?'} {ev.amount} {ev.currency or ''}".strip(),
               entity_kind="cost_evidence", entity_id=ev.id, kind="payment", state=ev.match_state, visibility="finance",
               sources=[inp.source_ref] if inp.source_ref else None, exception=ev.match_state in ("conflict", "ambiguous"),
               details={"cost_item_id": ev.cost_item_id, "reasons": ev.match_reasons, "discrepancy": ev.discrepancy,
                        "revision": ev.revision})
    ctx.emit("evidence.saved", aggregate_type="cost_evidence", aggregate_id=ev.id, aggregate_version=ev.version,
             payload={"evidence_id": ev.id, "kind": ev.kind, "match_state": ev.match_state, "cost_item_id": ev.cost_item_id,
                      "revision": ev.revision, "supersedes_id": ev.supersedes_id})
    ctx.emit("ledger.evidence_changed" if ev.kind == "ledger_row" else "cost.evidence_changed", aggregate_type="cost_evidence",
             aggregate_id=ev.id, payload={"evidence_id": ev.id, "match_state": ev.match_state, "cost_item_id": ev.cost_item_id})
    return {"evidence": serialize_evidence(ev), "created": True, "match": m, "cost_item_created": created_item}


class ReviewIn(BaseModel):
    evidence_id: str
    cost_item_id: str | None = None
    vehicle_id: str | None = None
    category: str | None = None
    resolve_discrepancy: str | None = None   # evidence|item — which value is right when amounts differ
    note: str | None = None
    expected_version: int | None = None


async def _link_reviewed(ctx: CommandContext, ev: CostEvidence, item_id: str, inp: ReviewIn, verb: str) -> dict:
    c = await _item(ctx, item_id)
    if ev.cost_item_id and ev.cost_item_id != item_id:
        ev.match_reasons = list(ev.match_reasons or []) + [f"{verb}: moved from item {ev.cost_item_id[:8]}"]
    ev.cost_item_id = item_id
    ev.proposed_cost_item_id = None
    ev.reviewed_by, ev.reviewed_at, ev.review_note = ctx.actor.user_id, ctx.now, inp.note
    if inp.vehicle_id:
        ev.proposed_vehicle_id = inp.vehicle_id
        if not c.vehicle_id and not await _allocations_of(ctx.db, c.id):
            c.vehicle_id = inp.vehicle_id
    if inp.category:
        if inp.category not in COST_CATEGORIES:
            raise ValidationFailed(f"category must be one of {COST_CATEGORIES}")
        c.category = inp.category
        ev.proposed_category = inp.category
    disc = {}
    if ev.amount is not None and c.amount_invoiced is not None and not ev.is_credit:
        if ev.currency and ev.currency.upper() != c.currency:
            disc = {"field": "currency", "evidence": f"{ev.amount} {ev.currency}", "item": f"{c.active_amount} {c.currency}"}
        elif not _amounts_agree(ev.amount, c.amount_invoiced):
            disc = {"field": "amount", "evidence": str(ev.amount), "item": str(c.amount_invoiced), "currency": c.currency,
                    "difference": str(ev.amount - c.amount_invoiced)}
    if disc and inp.resolve_discrepancy == "evidence" and disc["field"] == "amount":
        old = c.amount_invoiced
        c.amount_invoiced = ev.amount
        c.restatements = list(c.restatements or []) + [{"at": ctx.now.isoformat(), "by": ctx.actor.user_id, "field": "amount_invoiced",
                                                        "old": str(old), "new": str(ev.amount), "note": f"{verb}: evidence {ev.id[:8]} is authoritative"}]
        await _rebalance_single(ctx, c)
        disc = {}
        ev.match_state = "matched"
        ev.discrepancy = {}
    elif disc and inp.resolve_discrepancy == "item":
        ev.match_state = "matched"
        ev.discrepancy = {**disc, "resolved": "item value kept", "by": ctx.actor.user_id}
    elif disc:
        ev.match_state = "conflict"
        ev.discrepancy = disc
    else:
        ev.match_state = "matched"
        ev.discrepancy = {}
        if not ev.applied or not ev.applied.get("applied"):
            ev.applied = _apply_evidence(ctx, ev, c)
    ev.match_reasons = list(ev.match_reasons or []) + [f"{verb} by {ctx.actor.display_name or ctx.actor.user_id}"]
    ev.candidates = []
    if c.currency == "USD":
        c.usd_amount = c.active_amount
    ctx.touch(ev, "cost_evidence")
    ctx.touch(c, "cost_item")
    ctx.record(f"Evidence {verb}: {ev.kind} → {c.description or c.category} ({ev.match_state})", entity_kind="cost_evidence",
               entity_id=ev.id, kind="payment", state=ev.match_state, visibility="finance", exception=ev.match_state == "conflict",
               details={"cost_item_id": c.id, "discrepancy": ev.discrepancy, "note": inp.note})
    ctx.emit("evidence.saved", aggregate_type="cost_evidence", aggregate_id=ev.id, aggregate_version=ev.version,
             payload={"evidence_id": ev.id, "match_state": ev.match_state, "cost_item_id": c.id, "review": verb})
    _emit_cost(ctx, c, "evidence_reviewed", evidence_id=ev.id)
    return {"evidence": serialize_evidence(ev), "cost_item": serialize_cost_item(c)}


@command("costs.confirm_match", input=ReviewIn, perm="finance.write", action_class="internal",
         description="Review action: confirm a proposed/ambiguous/conflicting match (choose the item; resolve a discrepancy explicitly).")
async def costs_confirm_match(ctx: CommandContext, inp: ReviewIn) -> dict:
    ev = await _evidence(ctx, inp.evidence_id, inp.expected_version)
    item_id = inp.cost_item_id or ev.cost_item_id or ev.proposed_cost_item_id
    if not item_id:
        if len(ev.candidates or []) == 1:
            item_id = ev.candidates[0]["cost_item_id"]
        else:
            raise ValidationFailed("choose the cost item to confirm (ambiguous or unmatched evidence)")
    return await _link_reviewed(ctx, ev, item_id, inp, "confirmed")


@command("costs.correct_match", input=ReviewIn, perm="finance.write", action_class="internal",
         description="Review action: relink evidence to a different cost item / vehicle / category.")
async def costs_correct_match(ctx: CommandContext, inp: ReviewIn) -> dict:
    ev = await _evidence(ctx, inp.evidence_id, inp.expected_version)
    if not inp.cost_item_id and not inp.vehicle_id and not inp.category:
        raise ValidationFailed("a correction needs a cost_item_id, vehicle_id or category")
    item_id = inp.cost_item_id or ev.cost_item_id
    if not item_id:
        raise ValidationFailed("evidence is not linked; give the cost_item_id to link it to")
    return await _link_reviewed(ctx, ev, item_id, inp, "corrected")


class LeaveUnmatchedIn(BaseModel):
    evidence_id: str
    reason: str = Field(min_length=1)
    ignore: bool = False
    expected_version: int | None = None


@command("costs.leave_unmatched", input=LeaveUnmatchedIn, perm="finance.write", action_class="internal",
         description="Review action: leave evidence unmatched (or ignore it) with a reason; nothing is deleted.")
async def costs_leave_unmatched(ctx: CommandContext, inp: LeaveUnmatchedIn) -> dict:
    ev = await _evidence(ctx, inp.evidence_id, inp.expected_version)
    ev.match_state = "ignored" if inp.ignore else "unmatched"
    ev.cost_item_id = None
    ev.proposed_cost_item_id = None
    ev.reviewed_by, ev.reviewed_at, ev.review_note = ctx.actor.user_id, ctx.now, inp.reason
    ev.match_reasons = list(ev.match_reasons or []) + [f"left {ev.match_state}: {inp.reason}"]
    ctx.touch(ev, "cost_evidence")
    ctx.record(f"Evidence left {ev.match_state}: {inp.reason}", entity_kind="cost_evidence", entity_id=ev.id, kind="payment",
               state=ev.match_state, visibility="finance")
    ctx.emit("evidence.saved", aggregate_type="cost_evidence", aggregate_id=ev.id, aggregate_version=ev.version,
             payload={"evidence_id": ev.id, "match_state": ev.match_state, "cost_item_id": None})
    return {"evidence": serialize_evidence(ev)}


# ── allocations (invariant 5) ────────────────────────────────────────────────
class AllocationLine(BaseModel):
    vehicle_id: str
    amount: Decimal | None = None    # explicit base/total amount
    weight: Decimal | None = None    # weighted basis


class AllocateIn(BaseModel):
    cost_item_id: str
    basis: str = "explicit"          # explicit|equal|weighted
    lines: list[AllocationLine] = Field(min_length=1)
    confirm: bool = True             # explicit lines by a human with finance.write are confirmed; fuzzy bases stay proposed
    note: str | None = None
    expected_version: int | None = None


@command("costs.allocate", input=AllocateIn, perm="finance.write", action_class="internal",
         description="Allocate a cost across vehicles (explicit/equal/weighted). Totals balance exactly to the active amount; "
                     "tax/freight are distributed proportionally with controlled rounding. Weighted/equal splits need review.")
async def costs_allocate(ctx: CommandContext, inp: AllocateIn) -> dict:
    if inp.basis not in ("explicit", "equal", "weighted"):
        raise ValidationFailed("basis must be explicit|equal|weighted")
    c = await _item(ctx, inp.cost_item_id, inp.expected_version)
    active = c.active_amount
    if active is None:
        raise Blocked("cost item has no amount to allocate")
    cur = c.currency
    vids = [l.vehicle_id for l in inp.lines]
    if len(set(vids)) != len(vids):
        raise ValidationFailed("each vehicle may appear once")
    found = {v.id for v in (await ctx.db.execute(select(Vehicle).where(Vehicle.id.in_(vids)))).scalars().all()}
    missing = [v for v in vids if v not in found]
    if missing:
        raise NotFound("vehicle not found", vehicle_ids=missing)
    tax = c.tax_amount or ZERO
    freight = c.freight_amount or ZERO
    base_total = active - tax - freight
    if inp.basis == "explicit":
        if any(l.amount is None for l in inp.lines):
            raise ValidationFailed("explicit allocation needs an amount per line")
        amounts = [quantize(l.amount, cur) for l in inp.lines]
        total = sum(amounts)
        if total == active:
            # lines are totals already (tax/freight included by the caller)
            base_parts = amounts
            tax_parts = [ZERO] * len(amounts)
            freight_parts = [ZERO] * len(amounts)
            finals = amounts
        elif total == base_total and (tax or freight):
            base_parts = amounts
            tax_parts = split_balanced(tax, amounts, cur) if tax else [ZERO] * len(amounts)
            freight_parts = split_balanced(freight, amounts, cur) if freight else [ZERO] * len(amounts)
            finals = [quantize(b + t + f, cur) for b, t, f in zip(base_parts, tax_parts, freight_parts)]
        else:
            raise ValidationFailed("allocations must balance to the source amount (invariant 5)",
                                   lines_total=str(total), active_amount=str(active), base_before_tax_freight=str(base_total))
        weights = amounts
    else:
        weights = [Decimal("1")] * len(inp.lines) if inp.basis == "equal" else [l.weight if l.weight is not None else Decimal("0") for l in inp.lines]
        if sum(weights) <= 0:
            raise ValidationFailed("weights must be positive")
        base_parts = split_balanced(base_total, weights, cur)
        tax_parts = split_balanced(tax, weights, cur) if tax else [ZERO] * len(weights)
        freight_parts = split_balanced(freight, weights, cur) if freight else [ZERO] * len(weights)
        finals = [quantize(b + t + f, cur) for b, t, f in zip(base_parts, tax_parts, freight_parts)]
    if sum(finals) != active:   # invariant 5 is enforced, not assumed (asserts vanish under -O)
        raise ValidationFailed("allocation split did not balance to the source amount (invariant 5)",
                               lines_total=str(sum(finals)), active_amount=str(active))
    confirmed = inp.confirm and ctx.actor.is_human and inp.basis == "explicit"
    review_reasons = [] if confirmed else [f"{inp.basis} split proposed by {ctx.actor.kind}; needs finance review"]
    existing = {a.vehicle_id: a for a in await _allocations_of(ctx.db, c.id)}
    kept: list[CostAllocation] = []
    for line, base, t, f, final, w in zip(inp.lines, base_parts, tax_parts, freight_parts, finals, weights):
        a = existing.pop(line.vehicle_id, None)
        if a is None:
            a = CostAllocation(cost_item_id=c.id, vehicle_id=line.vehicle_id, currency=cur, amount_credited=ZERO, credits=[],
                               created_by=ctx.actor.user_id)
            ctx.db.add(a)
        a.amount, a.basis, a.weight = final, inp.basis, w
        a.components = {"base": str(base), "tax": str(t), "freight": str(f)}
        a.state = "confirmed" if confirmed else "proposed"
        a.review_reasons = review_reasons
        a.confirmed_by = ctx.actor.user_id if confirmed else None
        a.confirmed_at = ctx.now if confirmed else None
        a.updated_by = ctx.actor.user_id
        kept.append(a)
    for a in existing.values():
        if (a.amount_credited or ZERO) > 0:
            raise Blocked("cannot remove an allocation that carries a credit; record the change as a restatement",
                          vehicle_id=a.vehicle_id)
        await ctx.db.delete(a)
    await ctx.db.flush()
    for a in kept:
        ctx.changed.append({"kind": "cost_allocation", "id": a.id, "version": a.version})
    c.shared = len(kept) > 1
    c.vehicle_id = kept[0].vehicle_id if len(kept) == 1 else None
    c.allocation_state = "balanced" if confirmed else "proposed"
    ctx.touch(c, "cost_item")
    ctx.record(f"Cost allocated ({inp.basis}, {'confirmed' if confirmed else 'needs review'}): {c.description or c.category} "
               f"{active} {cur} across {len(kept)} vehicle(s)", entity_kind="cost_item", entity_id=c.id, kind="payment",
               state=c.allocation_state, visibility="finance", exception=not confirmed,
               details={"lines": [{"vehicle_id": a.vehicle_id, "amount": str(a.amount), **a.components} for a in kept], "note": inp.note})
    _emit_cost(ctx, c, "allocated", state=c.allocation_state, vehicle_ids=[a.vehicle_id for a in kept])
    return {"cost_item": serialize_cost_item(c, kept), "allocations": [serialize_allocation(a) for a in kept],
            "balanced": True, "needs_review": not confirmed}


class ConfirmAllocationsIn(BaseModel):
    cost_item_id: str
    expected_version: int | None = None


@command("costs.confirm_allocations", input=ConfirmAllocationsIn, perm="finance.write", action_class="owner_only",
         approval_kind="allocation", summary=lambda p: f"Confirm cost allocation {p.cost_item_id[:8]}",
         description="Owner confirms a proposed (fuzzy) cost allocation after review.")
async def costs_confirm_allocations(ctx: CommandContext, inp: ConfirmAllocationsIn) -> dict:
    c = await _item(ctx, inp.cost_item_id, inp.expected_version)
    allocs = await _allocations_of(ctx.db, c.id)
    if not allocs:
        raise Blocked("no allocations to confirm")
    if sum(a.amount for a in allocs) != (c.active_amount or ZERO):
        raise Blocked("allocations do not balance to the active amount; re-allocate first",
                      total=str(sum(a.amount for a in allocs)), active=str(c.active_amount))
    for a in allocs:
        a.state, a.confirmed_by, a.confirmed_at, a.review_reasons = "confirmed", ctx.actor.user_id, ctx.now, []
        a.bump(ctx.actor.user_id)
        ctx.changed.append({"kind": "cost_allocation", "id": a.id, "version": a.version})
    c.allocation_state = "balanced"
    ctx.touch(c, "cost_item")
    ctx.record(f"Allocation confirmed: {c.description or c.category}", entity_kind="cost_item", entity_id=c.id, kind="payment",
               state="balanced", visibility="finance")
    _emit_cost(ctx, c, "allocation_confirmed")
    return {"cost_item": serialize_cost_item(c, allocs)}


# ── payments (spec §6.3) ─────────────────────────────────────────────────────
PROVIDER_DOMAINS = {"square": ("squareup.com", "square.com", "messaging.squareup.com"),
                    "stripe": ("stripe.com",), "paypal": ("paypal.com",)}


async def _payment(ctx: CommandContext, payment_id: str, expected_version: int | None = None) -> Payment:
    p = (await ctx.db.execute(select(Payment).where(Payment.id == payment_id).with_for_update())).scalar_one_or_none()
    if p is None:
        raise NotFound("payment not found")
    if expected_version is not None and p.version != expected_version:
        raise Conflict("payment changed since you loaded it", current_version=p.version)
    return p


async def _invoice(ctx: CommandContext, invoice_id: str, expected_version: int | None = None) -> Invoice:
    i = (await ctx.db.execute(select(Invoice).where(Invoice.id == invoice_id).with_for_update())).scalar_one_or_none()
    if i is None:
        raise NotFound("invoice not found")
    if expected_version is not None and i.version != expected_version:
        raise Conflict("invoice changed since you loaded it", current_version=i.version)
    return i


def _emit_payment(ctx: CommandContext, p: Payment, change: str, **extra) -> None:
    ctx.emit("payment.changed", aggregate_type="payment", aggregate_id=p.id, aggregate_version=p.version,
             payload={"payment_id": p.id, "change": change, "status": p.status, "provider": p.provider, **extra})


class PaymentReportedIn(BaseModel):
    amount: Decimal
    currency: str = "USD"
    claimed_by: str = "customer"          # customer|email|telegram|agent
    provider_hint: str | None = "square"  # which provider the claim names
    source_ref: str | None = None         # message id / claim reference (dedupe)
    sender: str | None = None             # email sender address as received (untrusted)
    subject: str | None = None
    payer_name: str | None = None
    payer_email: str | None = None
    contact_id: str | None = None
    occurred_at: datetime | None = None
    raw: dict = Field(default_factory=dict)


@command("payments.record_reported", input=PaymentReportedIn, perm="finance.status", action_class="internal",
         description="A customer claim or provider-looking email says a payment happened: record it as "
                     "'Payment reported — needs confirmation'. Never allocates and never triggers Deposit Paid (E05).")
async def payments_record_reported(ctx: CommandContext, inp: PaymentReportedIn) -> dict:
    cur = inp.currency.upper()
    if inp.source_ref:
        ex = (await ctx.db.execute(select(Payment).where(Payment.provider == "claim", Payment.merchant_id == "",
                                                         Payment.provider_payment_id == inp.source_ref))).scalar_one_or_none()
        if ex is not None:
            return {"payment": serialize_payment(ex), "created": False}
    flags = ["needs_confirmation"]
    if inp.claimed_by == "customer":
        flags.append("customer_claim")
    if inp.sender:
        domain = inp.sender.split("@")[-1].strip(" >").lower()
        allowed = PROVIDER_DOMAINS.get((inp.provider_hint or "").lower(), ())
        if allowed and not any(domain == d or domain.endswith("." + d) for d in allowed):
            flags.append("sender_not_provider")
    if inp.subject and re.search(r"\bpaid\b", inp.subject, re.I):
        flags.append("subject_claims_paid_is_not_evidence")
    p = Payment(provider="claim", merchant_id="", provider_payment_id=inp.source_ref, amount=quantize(inp.amount, cur), currency=cur,
                status="reported", payer_name=inp.payer_name, payer_email=inp.payer_email, contact_id=inp.contact_id,
                occurred_at=inp.occurred_at, source_kind="email" if inp.claimed_by == "email" else "manual", source_ref=inp.source_ref,
                report_flags=flags, report_source={"claimed_by": inp.claimed_by, "sender": inp.sender, "subject": inp.subject,
                                                   "provider_hint": inp.provider_hint, "recorded_by": ctx.actor.user_id},
                raw=inp.raw, refunded_amount=ZERO, allocated_amount=ZERO, created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
    ctx.db.add(p)
    await ctx.db.flush()
    ctx.changed.append({"kind": "payment", "id": p.id, "version": p.version})
    ctx.record(f"Payment reported — needs confirmation: {p.amount} {cur} ({inp.claimed_by})", entity_kind="payment", entity_id=p.id,
               kind="payment", state="reported", visibility="finance", sources=[inp.source_ref] if inp.source_ref else None,
               exception="sender_not_provider" in flags, details={"flags": flags})
    _emit_payment(ctx, p, "reported", flags=flags)
    return {"payment": serialize_payment(p), "created": True, "decision": "Needs review"}


class PaymentManualIn(BaseModel):
    amount: Decimal
    currency: str = "USD"
    method: str = "other"                 # cash|zelle|wire|check|card|other
    evidence_ref: str | None = None       # bank line / receipt reference
    evidence_asset_id: str | None = None  # uploaded evidence
    payer_name: str | None = None
    contact_id: str | None = None
    occurred_at: datetime | None = None
    reported_payment_id: str | None = None  # upgrade a reported claim once verified
    dedupe_key: str | None = None
    note: str | None = None


@command("payments.record_manual_confirmed", input=PaymentManualIn, perm="finance.write", action_class="owner_only",
         approval_kind="payment", summary=lambda p: f"Confirm manual payment {p.amount} {p.currency}",
         consequence=lambda p: {"amount": str(p.amount), "currency": p.currency, "moves_money": False},
         description="Owner confirms a payment from verified manual evidence (bank line, receipt). Confirmation is not allocation.")
async def payments_record_manual_confirmed(ctx: CommandContext, inp: PaymentManualIn) -> dict:
    if not inp.evidence_ref and not inp.evidence_asset_id:
        raise Blocked("verified evidence required (evidence_ref or evidence_asset_id)")
    cur = inp.currency.upper()
    amt = quantize(inp.amount, cur)
    if inp.reported_payment_id:
        p = await _payment(ctx, inp.reported_payment_id)
        if p.status != "reported":
            raise Blocked(f"payment is {p.status}, not a reported claim")
        if p.amount != amt or p.currency != cur:
            p.exceptions = list(p.exceptions or []) + [{"kind": "claim_differs_from_evidence", "at": ctx.now.isoformat(),
                                                        "claimed": f"{p.amount} {p.currency}", "verified": f"{amt} {cur}"}]
            p.amount, p.currency = amt, cur
        p.status, p.provider, p.confirmed_by, p.confirmed_at = "completed", "manual", ctx.actor.user_id, ctx.now
        p.method, p.evidence_ref, p.evidence_asset_id = inp.method, inp.evidence_ref, inp.evidence_asset_id
        p.contact_id = inp.contact_id or p.contact_id
        p.occurred_at = inp.occurred_at or p.occurred_at
        p.report_flags = [f for f in (p.report_flags or []) if f != "needs_confirmation"] + ["confirmed_manually"]
        ctx.touch(p, "payment")
        created = False
    else:
        if inp.dedupe_key:
            ex = (await ctx.db.execute(select(Payment).where(Payment.provider == "manual", Payment.merchant_id == "",
                                                             Payment.provider_payment_id == inp.dedupe_key))).scalar_one_or_none()
            if ex is not None:
                return {"payment": serialize_payment(ex), "created": False}
        p = Payment(provider="manual", merchant_id="", provider_payment_id=inp.dedupe_key, amount=amt, currency=cur, status="completed",
                    payer_name=inp.payer_name, contact_id=inp.contact_id, occurred_at=inp.occurred_at or ctx.now, source_kind="manual",
                    source_ref=inp.evidence_ref, confirmed_by=ctx.actor.user_id, confirmed_at=ctx.now, method=inp.method,
                    evidence_ref=inp.evidence_ref, evidence_asset_id=inp.evidence_asset_id, refunded_amount=ZERO, allocated_amount=ZERO,
                    created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
        ctx.db.add(p)
        await ctx.db.flush()
        ctx.changed.append({"kind": "payment", "id": p.id, "version": p.version})
        created = True
    ctx.record(f"Manual payment confirmed: {amt} {cur} via {inp.method}", entity_kind="payment", entity_id=p.id, kind="payment",
               state="completed", visibility="finance", sources=[s for s in (inp.evidence_ref, inp.evidence_asset_id) if s],
               details={"note": inp.note, "confirmed_by": ctx.actor.user_id})
    _emit_payment(ctx, p, "confirmed")
    return {"payment": serialize_payment(p), "created": created}


def _version_key(v: str | None):
    if v is None:
        return None
    try:
        return (0, int(v))
    except (TypeError, ValueError):
        pass
    try:
        from ..core.time import parse_iso
        return (1, parse_iso(v).timestamp())
    except Exception:  # noqa: BLE001
        return (2, str(v))


STATUS_MAP = {"completed": "completed", "approved": "pending", "pending": "pending", "failed": "failed", "canceled": "cancelled",
              "cancelled": "cancelled", "refunded": "refunded", "partially_refunded": "partially_refunded", "disputed": "disputed"}


class ProviderPaymentIn(BaseModel):
    provider: str
    merchant_id: str
    provider_payment_id: str
    provider_event_id: str | None = None
    provider_version: str | None = None
    location_id: str | None = None
    connection_id: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    status: str | None = None
    fee_amount: Decimal | None = None
    net_amount: Decimal | None = None
    refunds: list[dict] = Field(default_factory=list)   # [{id, amount, currency, status, at}]
    disputes: list[dict] = Field(default_factory=list)
    payer_name: str | None = None
    payer_email: str | None = None
    contact_id: str | None = None
    provider_customer_id: str | None = None
    provider_order_id: str | None = None
    provider_invoice_id: str | None = None
    occurred_at: datetime | None = None
    fetched_at: datetime | None = None
    is_payout: bool = False
    raw: dict = Field(default_factory=dict)


@command("payments.upsert_provider", input=ProviderPaymentIn, perm="finance.write", action_class="internal",
         description="Adapter entry point: upsert a provider payment namespaced by provider+merchant+payment id. Duplicate or "
                     "reordered events never duplicate or regress; refunds reduce availability without erasing the original.")
async def payments_upsert_provider(ctx: CommandContext, inp: ProviderPaymentIn) -> dict:
    p = (await ctx.db.execute(select(Payment).where(Payment.provider == inp.provider, Payment.merchant_id == inp.merchant_id,
                                                    Payment.provider_payment_id == inp.provider_payment_id).with_for_update())).scalar_one_or_none()
    created = p is None
    if created:
        if inp.amount is None or not inp.currency:
            raise ValidationFailed("a new provider payment needs amount and currency")
        p = Payment(provider=inp.provider, merchant_id=inp.merchant_id, provider_payment_id=inp.provider_payment_id,
                    amount=quantize(inp.amount, inp.currency), currency=inp.currency.upper(), status="pending", source_kind="webhook",
                    refunds=[], disputes=[], provider_event_ids=[], refunded_amount=ZERO, allocated_amount=ZERO, exceptions=[],
                    created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
        ctx.db.add(p)
        await ctx.db.flush()
    seen = list(p.provider_event_ids or [])
    if inp.provider_event_id and inp.provider_event_id in seen:
        return {"payment": serialize_payment(p), "applied": False, "duplicate": True, "created": False}
    if inp.provider_event_id:
        seen.append(inp.provider_event_id)
        p.provider_event_ids = seen
    stale = (not created and inp.provider_version is not None and p.provider_version is not None
             and _version_key(inp.provider_version) < _version_key(p.provider_version))
    # refunds/disputes are facts keyed by their own ids: union them whatever the event order
    refunds = {r.get("id"): r for r in (p.refunds or []) if r.get("id")}
    for r in inp.refunds:
        if r.get("id"):
            refunds[r["id"]] = {**refunds.get(r["id"], {}), **r}
    p.refunds = list(refunds.values())
    disputes = {d.get("id"): d for d in (p.disputes or []) if d.get("id")}
    for d in inp.disputes:
        if d.get("id"):
            disputes[d["id"]] = {**disputes.get(d["id"], {}), **d}
    p.disputes = list(disputes.values())
    if not stale:
        if inp.amount is not None:
            p.amount = quantize(inp.amount, inp.currency or p.currency)
        if inp.currency:
            p.currency = inp.currency.upper()
        for f in ("location_id", "connection_id", "payer_name", "payer_email", "contact_id", "provider_customer_id",
                  "provider_order_id", "provider_invoice_id", "occurred_at", "fetched_at"):
            v = getattr(inp, f)
            if v is not None:
                setattr(p, f, v)
        if inp.fee_amount is not None:
            p.fee_amount = quantize(inp.fee_amount, p.currency)
        if inp.net_amount is not None:
            p.net_amount = quantize(inp.net_amount, p.currency)
        if inp.provider_version is not None:
            p.provider_version = inp.provider_version
        p.is_payout = inp.is_payout or p.is_payout
        if inp.raw:
            p.raw = inp.raw
        if inp.status:
            p.status = STATUS_MAP.get(inp.status.lower(), p.status)
    p.refunded_amount = sum((quantize(parse_amount(r.get("amount", "0")), p.currency) for r in p.refunds
                             if (r.get("status") or "completed").lower() in ("completed", "approved")), ZERO)
    if p.refunded_amount > 0 and p.status in ("completed", "refunded", "partially_refunded"):
        p.status = "refunded" if p.refunded_amount >= p.amount else "partially_refunded"
    if any((d.get("status") or "").lower() in ("open", "evidence_required", "processing") for d in p.disputes):
        p.exceptions = list(p.exceptions or []) + [{"kind": "dispute_open", "at": ctx.now.isoformat()}] \
            if not any(e.get("kind") == "dispute_open" for e in (p.exceptions or [])) else p.exceptions
        if p.status == "completed":
            p.status = "disputed"
    p.fetched_at = inp.fetched_at or p.fetched_at or ctx.now
    if created:
        ctx.changed.append({"kind": "payment", "id": p.id, "version": p.version})
    else:
        ctx.touch(p, "payment")
    paused = await _availability_check(ctx, p)
    ctx.record(f"Provider payment {'received' if created else 'updated'}: {p.provider} {p.provider_payment_id} {p.amount} {p.currency} "
               f"({p.status}{', stale event ignored' if stale else ''})", entity_kind="payment", entity_id=p.id, kind="payment",
               state=p.status, visibility="finance", sources=[f"{p.provider}:{inp.provider_event_id}"] if inp.provider_event_id else None,
               exception=bool(paused), details={"event_id": inp.provider_event_id, "version": inp.provider_version, "stale": stale,
                                                "is_payout": p.is_payout})
    _emit_payment(ctx, p, "provider_upsert", created=created, stale=stale, event_id=inp.provider_event_id)
    ctx.emit(f"{p.provider}.object_changed", aggregate_type="payment", aggregate_id=p.id, provider=p.provider,
             provider_event_id=f"{p.merchant_id}:{inp.provider_event_id}" if inp.provider_event_id else None,
             payload={"payment_id": p.id, "status": p.status, "refunded_amount": str(p.refunded_amount)})
    return {"payment": serialize_payment(p), "applied": not stale, "duplicate": False, "created": created, "reordered": stale,
            "paused": paused}


async def _confirmed_total(db, payment_id: str) -> Decimal:
    v = (await db.execute(select(func.coalesce(func.sum(PaymentAllocation.amount), 0)).where(
        PaymentAllocation.payment_id == payment_id, PaymentAllocation.state == "confirmed"))).scalar_one()
    return parse_amount(v)


async def _pause_thread(ctx: CommandContext, key: str, reason: str) -> str:
    wc = await ctx.db.get(WorkflowControl, key)
    if wc is None:
        wc = WorkflowControl(key=key, paused=True, reason=reason, changed_by=ctx.actor.user_id, changed_at=ctx.now)
        ctx.db.add(wc)
    else:
        wc.paused, wc.reason, wc.changed_by, wc.changed_at = True, reason, ctx.actor.user_id, ctx.now
    return key


async def _availability_check(ctx: CommandContext, p: Payment) -> list[str]:
    """Refund/dispute after allocation: availability shrinks below what was allocated → exception + dependent pause
    (E06/E09). The original allocation and handoff history are kept; reversal is a separate owner action."""
    allocated = await _confirmed_total(ctx.db, p.id)
    p.allocated_amount = allocated
    if allocated <= p.available_amount and p.status not in ("disputed", "failed", "cancelled"):
        return []
    if allocated == 0:
        return []
    kind = "refund_exceeds_allocation" if allocated > p.available_amount else f"payment_{p.status}_after_allocation"
    if not any(e.get("kind") == kind and e.get("open") for e in (p.exceptions or [])):
        p.exceptions = list(p.exceptions or []) + [{"kind": kind, "at": ctx.now.isoformat(), "allocated": str(allocated),
                                                    "available": str(p.available_amount), "open": True}]
    paused: list[str] = []
    allocs = (await ctx.db.execute(select(PaymentAllocation).where(PaymentAllocation.payment_id == p.id,
                                                                   PaymentAllocation.state == "confirmed"))).scalars().all()
    for a in allocs:
        inv = await ctx.db.get(Invoice, a.invoice_id)
        if inv is None:
            continue
        inv.exceptions = list(inv.exceptions or []) + [{"kind": kind, "at": ctx.now.isoformat(), "payment_id": p.id,
                                                        "allocation_id": a.id}]
        inv.bump(ctx.actor.user_id)
        _mark_changed(ctx, "invoice", inv)
        h = dict(inv.handoff or {})
        if h.get("done") and h.get("id"):
            key = await _pause_thread(ctx, f"thread:{h['id']}", f"{kind}: payment {p.id[:8]} — owner review required")
            paused.append(key)
            h["exceptions"] = list(h.get("exceptions") or []) + [{"kind": kind, "at": ctx.now.isoformat(), "paused": key}]
            inv.handoff = h
        ctx.emit("payment.allocated", aggregate_type="invoice", aggregate_id=inv.id, aggregate_version=inv.version,
                 payload={"change": "availability_reduced", "payment_id": p.id, "invoice_id": inv.id, "allocation_id": a.id,
                          "allocated": str(allocated), "available": str(p.available_amount), "kind": kind, "paused": paused})
    ctx.record(f"Payment exception: {kind} on {p.provider} {p.provider_payment_id or p.id[:8]} (allocated {allocated}, available "
               f"{p.available_amount} {p.currency})", entity_kind="payment", entity_id=p.id, kind="payment", state=kind,
               visibility="finance", exception=True, details={"paused": paused})
    ctx.emit("metrics.invalidated", aggregate_type="payment", aggregate_id=p.id, payload={"reason": kind})
    return paused


# ── invoices (obligations) ───────────────────────────────────────────────────
class InvoiceCreateIn(BaseModel):
    kind: str = "deposit"               # deposit|balance|reservation|shipping
    contact_id: str | None = None
    opportunity_id: str | None = None
    import_request_id: str | None = None
    sale_id: str | None = None
    vehicle_id: str | None = None
    agreement_id: str | None = None
    amount_due: Decimal | None = None
    currency: str | None = None
    due_at: datetime | None = None
    dedupe_key: str | None = None
    notes: str = ""


@command("invoices.create", input=InvoiceCreateIn, perm="finance.write", action_class="internal",
         description="Create a customer obligation. Amount/currency come from the agreement (or the import request's deposit rule) "
                     "unless given explicitly; an unset deposit rule blocks an automatic deposit invoice (§5.2).")
async def invoices_create(ctx: CommandContext, inp: InvoiceCreateIn) -> dict:
    if inp.kind not in INVOICE_KINDS:
        raise ValidationFailed(f"kind must be one of {INVOICE_KINDS}")
    if inp.dedupe_key:
        ex = (await ctx.db.execute(select(Invoice).where(Invoice.dedupe_key == inp.dedupe_key))).scalars().first()
        if ex is not None:
            return {"invoice": serialize_invoice(ex), "created": False}
    opp = await ctx.db.get(Opportunity, inp.opportunity_id) if inp.opportunity_id else None
    if inp.opportunity_id and opp is None:
        raise NotFound("opportunity not found")
    if opp is not None:
        ex = (await ctx.db.execute(select(Invoice).where(Invoice.opportunity_id == opp.id, Invoice.kind == inp.kind,
                                                         Invoice.status.in_(("open", "partially_paid"))))).scalars().first()
        if ex is not None:
            return {"invoice": serialize_invoice(ex), "created": False, "reason": "open obligation of this kind already exists"}
    amount, currency, source = inp.amount_due, inp.currency, "explicit"
    agreement = None
    if amount is None and inp.agreement_id:
        from ..models.finance import Agreement
        agreement = await ctx.db.get(Agreement, inp.agreement_id)
        if agreement is None:
            raise NotFound("agreement not found")
        terms = agreement.terms or {}
        if inp.kind == "deposit" and agreement.deposit_amount is not None:
            amount, currency, source = agreement.deposit_amount, agreement.deposit_currency, "agreement"
        elif inp.kind == "balance" and agreement.price_amount is not None:
            amount = agreement.price_amount - (agreement.deposit_amount or ZERO)
            currency, source = agreement.price_currency, "agreement"
        elif inp.kind == "reservation" and terms.get("reservation_amount") is not None:
            amount, currency, source = parse_amount(terms["reservation_amount"]), terms.get("reservation_currency") or agreement.price_currency, "agreement"
        elif inp.kind == "shipping" and terms.get("shipping_amount") is not None:
            amount, currency, source = parse_amount(terms["shipping_amount"]), terms.get("shipping_currency") or agreement.price_currency, "agreement"
    request_id = inp.import_request_id or (opp.import_request_id if opp is not None else None)
    if amount is None and inp.kind == "deposit" and request_id:
        try:
            from ..models.sourcing import ImportRequest
            r = await ctx.db.get(ImportRequest, request_id)
        except Exception:  # noqa: BLE001
            r = None
        if r is not None and r.deposit_rule and r.deposit_rule.get("amount"):
            amount, currency, source = parse_amount(r.deposit_rule["amount"]), r.deposit_rule.get("currency"), "import_request"
    if amount is None or not currency:
        if inp.kind == "deposit":
            raise Blocked("deposit rule not configured — cannot create an automatic deposit invoice; the owner must set the "
                          "required amount and currency (agreement or import request terms)", kind=inp.kind)
        raise ValidationFailed(f"{inp.kind} obligation needs amount_due and currency")
    cur = currency.upper()
    amt = quantize(amount, cur)
    if amt <= 0:
        raise ValidationFailed("amount_due must be positive")
    inv = Invoice(kind=inp.kind, contact_id=inp.contact_id or (opp.contact_id if opp is not None else None),
                  opportunity_id=inp.opportunity_id, import_request_id=request_id, sale_id=inp.sale_id,
                  vehicle_id=inp.vehicle_id or (opp.vehicle_id if opp is not None else None), agreement_id=inp.agreement_id,
                  amount_due=amt, currency=cur, amount_allocated=ZERO, due_at=inp.due_at, status="open", notes=inp.notes,
                  dedupe_key=inp.dedupe_key, terms_source=source, handoff={}, exceptions=[],
                  created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
    ctx.db.add(inv)
    await ctx.db.flush()
    ctx.changed.append({"kind": "invoice", "id": inv.id, "version": inv.version})
    ctx.record(f"Obligation created: {inv.kind} {amt} {cur} ({source})", entity_kind="invoice", entity_id=inv.id, kind="payment",
               state="open", visibility="finance", details={"opportunity_id": inv.opportunity_id, "contact_id": inv.contact_id})
    ctx.emit("invoice.changed", aggregate_type="invoice", aggregate_id=inv.id, aggregate_version=inv.version,
             payload={"invoice_id": inv.id, "kind": inv.kind, "status": inv.status, "change": "created"})
    return {"invoice": serialize_invoice(inv), "created": True}


# ── payment allocation (invariant 4) ─────────────────────────────────────────
class ProposeAllocationIn(BaseModel):
    payment_id: str
    invoice_id: str | None = None      # None = find candidate open obligations
    amount: Decimal | None = None
    conversion: dict | None = None     # {rate, source, date} when payment and invoice currencies differ
    note: str | None = None


def _invoice_status(inv: Invoice) -> str:
    remaining = (inv.amount_due or ZERO) - (inv.amount_allocated or ZERO)
    if (inv.amount_allocated or ZERO) == 0:
        return "open"
    if remaining > 0:
        return "partially_paid"
    if remaining == 0:
        return "paid"
    return "overpaid"


@command("payments.propose_allocation", input=ProposeAllocationIn, perm="finance.write", action_class="internal",
         description="Propose applying a confirmed payment to an obligation. Explicit references first; a payment that could "
                     "match several open obligations stays proposed with every candidate (E07). No guessed conversion.")
async def payments_propose_allocation(ctx: CommandContext, inp: ProposeAllocationIn) -> dict:
    p = await _payment(ctx, inp.payment_id)
    if p.is_payout:
        raise Blocked("a payout to the bank is not a customer payment and cannot be allocated to an obligation")
    if p.status in ("reported", "failed", "cancelled"):
        raise Blocked(f"payment is {payment_status_label(p)}; confirm it before allocating", status=p.status)
    available = p.available_amount - await _confirmed_total(ctx.db, p.id)
    if available <= 0:
        raise Blocked("nothing left to allocate on this payment", available=str(available))
    if inp.invoice_id:
        cands = [await _invoice(ctx, inp.invoice_id)]
        how = "explicit invoice"
    else:
        q = select(Invoice).where(Invoice.status.in_(("open", "partially_paid")))
        rows = (await ctx.db.execute(q)).scalars().all()
        cands, how = [], "no candidates"
        if p.provider_invoice_id:
            cands = [i for i in rows if i.provider_invoice_id and i.provider_invoice_id == p.provider_invoice_id]
            how = "provider invoice reference"
        if not cands and p.contact_id:
            cands = [i for i in rows if i.contact_id == p.contact_id]
            how = "same contact (open obligations)"
        if not cands and p.payer_email:
            from ..models.contacts import Contact
            cs = (await ctx.db.execute(select(Contact.id).where(func.lower(Contact.primary_email) == p.payer_email.lower()))).scalars().all()
            cands = [i for i in rows if i.contact_id in set(cs)]
            how = "payer email → contact"
    if not cands:
        return {"state": "no_candidates", "how": how, "allocations": [], "payment": serialize_payment(p)}
    group = new_id() if len(cands) > 1 else None
    out = []
    for inv in cands:
        ex = (await ctx.db.execute(select(PaymentAllocation).where(PaymentAllocation.payment_id == p.id, PaymentAllocation.invoice_id == inv.id,
                                                                   PaymentAllocation.state == "proposed"))).scalars().first()
        if ex is not None:
            out.append(ex)
            continue
        flags = []
        remaining = (inv.amount_due or ZERO) - (inv.amount_allocated or ZERO)
        if inv.currency != p.currency:
            flags.append("currency_mismatch")
            amt = quantize(inp.amount, p.currency) if inp.amount is not None else available
            if not (inp.conversion and inp.conversion.get("rate") and inp.conversion.get("source")):
                flags.append("conversion_required")
        else:
            if remaining <= 0 and inp.amount is None:
                raise Blocked("obligation is already satisfied; give an explicit amount to record an overpayment", invoice_id=inv.id,
                              status=inv.status)
            amt = quantize(inp.amount, p.currency) if inp.amount is not None else min(available, remaining)
            if amt < remaining:
                flags.append("partial")
            if amt > remaining:
                flags.append("overpayment")
        if amt > available:
            raise Blocked("proposed amount exceeds the payment's available amount (invariant 4)", available=str(available))
        if group:
            flags.append("ambiguous")
        a = PaymentAllocation(payment_id=p.id, invoice_id=inv.id, amount=amt, currency=p.currency, state="proposed", candidate_group=group,
                              ambiguous=bool(group), conversion=inp.conversion or {}, flags=flags, proposed_by=ctx.actor.user_id,
                              reason=inp.note, created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
        ctx.db.add(a)
        await ctx.db.flush()
        ctx.changed.append({"kind": "payment_allocation", "id": a.id, "version": a.version})
        out.append(a)
    state = "ambiguous" if group else ("proposed" if out else "no_candidates")
    ctx.record(f"Allocation proposed ({how}): payment {p.amount} {p.currency} → {len(out)} candidate obligation(s)", entity_kind="payment",
               entity_id=p.id, kind="payment", state=state, visibility="finance", exception=bool(group),
               details={"allocation_ids": [a.id for a in out], "flags": sorted({f for a in out for f in a.flags})})
    ctx.emit("payment.allocated", aggregate_type="payment", aggregate_id=p.id, aggregate_version=p.version,
             payload={"change": "proposed", "payment_id": p.id, "state": state, "candidates": [a.invoice_id for a in out]})
    return {"state": state, "how": how, "allocations": [serialize_payment_allocation(a) for a in out], "payment": serialize_payment(p),
            "decision": "Needs review"}


class ConfirmPaymentAllocationIn(BaseModel):
    allocation_id: str
    amount: Decimal | None = None
    conversion: dict | None = None
    expected_version: int | None = None
    note: str | None = None


@command("payments.confirm_allocation", input=ConfirmPaymentAllocationIn, perm="finance.write", action_class="owner_only",
         approval_kind="payment", summary=lambda p: f"Confirm payment allocation {p.allocation_id[:8]}",
         description="Owner confirms a proposed allocation. Sum of confirmed allocations never exceeds amount − refunds "
                     "(invariant 4); currency mismatch needs explicit conversion; partial/overpaid stay explicit. A satisfied "
                     "deposit obligation triggers the handoff exactly once (§5.2).")
async def payments_confirm_allocation(ctx: CommandContext, inp: ConfirmPaymentAllocationIn) -> dict:
    a = (await ctx.db.execute(select(PaymentAllocation).where(PaymentAllocation.id == inp.allocation_id).with_for_update())).scalar_one_or_none()
    if a is None:
        raise NotFound("allocation not found")
    if inp.expected_version is not None and a.version != inp.expected_version:
        raise Conflict("allocation changed since you loaded it", current_version=a.version)
    if a.state == "confirmed":
        inv = await _invoice(ctx, a.invoice_id)
        p = await _payment(ctx, a.payment_id)
        return {"allocation": serialize_payment_allocation(a), "invoice": serialize_invoice(inv), "payment": serialize_payment(p),
                "confirmed": False, "idempotent": True, "handoff": inv.handoff or {}}
    if a.state != "proposed":
        raise Blocked(f"allocation is {a.state}")
    p = await _payment(ctx, a.payment_id)
    inv = await _invoice(ctx, a.invoice_id)
    if p.status not in PAYMENT_CONFIRMED:
        raise Blocked(f"payment is '{payment_status_label(p)}' — only confirmed payments can be allocated", status=p.status)
    if p.is_payout:
        raise Blocked("payouts are not customer payments")
    if inv.status in ("cancelled", "refunded"):
        raise Blocked(f"obligation is {inv.status}")
    amt = quantize(inp.amount, p.currency) if inp.amount is not None else a.amount
    if amt <= 0:
        raise ValidationFailed("allocation amount must be positive")
    confirmed = await _confirmed_total(ctx.db, p.id)
    available = p.available_amount - confirmed
    if amt > available:
        raise Blocked("allocation would exceed the payment's available amount (amount − refunds − confirmed allocations)",
                      amount=str(amt), available=str(available), refunded=str(p.refunded_amount))
    conversion = inp.conversion or a.conversion or {}
    if inv.currency != p.currency:
        if not (conversion.get("rate") and conversion.get("source") and conversion.get("date")):
            raise Blocked(f"currency mismatch: payment {p.currency} vs obligation {inv.currency}; explicit conversion "
                          "(rate, source, date) is required — nothing is guessed", payment_currency=p.currency, invoice_currency=inv.currency)
        applied = convert(amt, parse_amount(conversion["rate"]), inv.currency)
    else:
        applied = amt
    a.amount, a.applied_amount, a.applied_currency, a.conversion = amt, applied, inv.currency, conversion
    a.state, a.confirmed_by, a.confirmed_at = "confirmed", ctx.actor.user_id, ctx.now
    if inp.note:
        a.reason = inp.note
    inv.amount_allocated = (inv.amount_allocated or ZERO) + applied
    inv.status = _invoice_status(inv)
    flags = [f for f in (a.flags or []) if f not in ("partial", "overpayment", "ambiguous", "conversion_required")]
    if inv.status == "partially_paid":
        flags.append("partial")
    if inv.status == "overpaid":
        flags.append("overpayment")
    a.flags = flags
    if inv.status in ("paid", "overpaid") and inv.satisfied_at is None:
        inv.satisfied_at = ctx.now
    if a.candidate_group:
        others = (await ctx.db.execute(select(PaymentAllocation).where(PaymentAllocation.candidate_group == a.candidate_group,
                                                                       PaymentAllocation.id != a.id, PaymentAllocation.state == "proposed"))).scalars().all()
        for o in others:
            o.state, o.reason = "declined", f"another candidate confirmed ({inv.id[:8]})"
            o.bump(ctx.actor.user_id)
    p.allocated_amount = confirmed + amt
    ctx.touch(a, "payment_allocation")
    ctx.touch(inv, "invoice")
    ctx.touch(p, "payment")
    remaining = (inv.amount_due or ZERO) - (inv.amount_allocated or ZERO)
    ctx.record(f"Allocation confirmed: {amt} {p.currency} → {inv.kind} obligation ({inv.status}"
               f"{', remaining ' + str(remaining) + ' ' + inv.currency if remaining > 0 else ''}"
               f"{', overpaid by ' + str(-remaining) + ' ' + inv.currency if remaining < 0 else ''})",
               entity_kind="invoice", entity_id=inv.id, kind="payment", state=inv.status, visibility="finance",
               exception=inv.status == "overpaid", details={"payment_id": p.id, "allocation_id": a.id, "conversion": conversion})
    ctx.emit("payment.allocated", aggregate_type="invoice", aggregate_id=inv.id, aggregate_version=inv.version,
             payload={"change": "confirmed", "payment_id": p.id, "invoice_id": inv.id, "allocation_id": a.id, "amount": str(amt),
                      "currency": p.currency, "applied": str(applied), "invoice_currency": inv.currency, "invoice_status": inv.status,
                      "invoice_kind": inv.kind, "remaining": str(remaining)})
    ctx.emit("metrics.invalidated", aggregate_type="invoice", aggregate_id=inv.id, payload={"reason": "payment.allocated"})
    handoff = {}
    if inv.kind == "deposit" and inv.status in ("paid", "overpaid"):
        handoff = await _deposit_handoff(ctx, inv, p, a)
    return {"allocation": serialize_payment_allocation(a), "invoice": serialize_invoice(inv), "payment": serialize_payment(p),
            "confirmed": True, "handoff": handoff}


async def _safe_dispatch(ctx: CommandContext, name: str, payload: dict, notes: list[str]):
    """Nested command in the same transaction; a domain refusal becomes a visible note, never a silent skip."""
    from ..core.errors import Denied
    if name not in REGISTRY:
        notes.append(f"{name} not available")
        return None
    try:
        res = await dispatch(ctx.child(), name, payload, commit=False)
    except (Blocked, Conflict, NotFound, Denied, ValidationFailed) as e:
        notes.append(f"{name}: {e.message}")
        ctx.record(f"Handoff step refused: {name} — {e.message}", kind="payment", state="exception", exception=True,
                   visibility="finance", details=e.detail)
        return None
    if res.status != "ok":
        notes.append(f"{name}: {res.status} (approval {res.approval_id})")
        return res
    return res


async def _cancel_deposit_tasks(ctx: CommandContext, opportunity_id: str | None, notes: list[str]) -> list[str]:
    if not opportunity_id:
        return []
    from ..models.tasks import Task
    rows = (await ctx.db.execute(select(Task).where(Task.opportunity_id == opportunity_id,
                                                    Task.status.notin_(("completed", "cancelled"))))).scalars().all()
    out = []
    for t in rows:
        if t.gate_requirement == "deposit" or (t.extra or {}).get("cancel_on_deposit"):
            res = await _safe_dispatch(ctx, "tasks.cancel", {"task_id": t.id, "reason": "deposit confirmed"}, notes)
            if res is not None and res.status == "ok":
                out.append(t.id)
    return out


async def _deposit_handoff(ctx: CommandContext, inv: Invoice, p: Payment, a: PaymentAllocation) -> dict:
    """Spec §5.2 / E08 / C08: exactly-once handoff when a confirmed allocation satisfies a deposit obligation.
    IRQ → one ImportRequest (existing pre-deposit record reused) + conversion link; Vehicle → one reservation/sale.
    Deposit Paid never approves a bid; owner reminder scheduling reads the emitted event (stage 2)."""
    h = dict(inv.handoff or {})
    if h.get("done"):
        return {**h, "repeated": True}
    notes: list[str] = []
    opp = await ctx.db.get(Opportunity, inv.opportunity_id) if inv.opportunity_id else None
    source_ref = f"payment:{p.id}"
    receipt_ref = p.provider_payment_id or p.evidence_ref or p.source_ref
    pipeline = opp.pipeline if opp is not None else ("irq" if inv.import_request_id else ("vehicle" if (inv.sale_id or inv.vehicle_id) else None))
    contact_id = inv.contact_id or (opp.contact_id if opp is not None else None)
    kind, target_id, vehicle_id = None, None, inv.vehicle_id or (opp.vehicle_id if opp is not None else None)
    cancelled = await _cancel_deposit_tasks(ctx, opp.id if opp is not None else None, notes)
    if pipeline == "irq":
        request_id = inv.import_request_id or (opp.import_request_id if opp is not None else None)
        if not request_id and opp is not None:
            try:
                from ..models.sourcing import ImportRequest
                r = (await ctx.db.execute(select(ImportRequest).where(ImportRequest.opportunity_id == opp.id))).scalars().first()
                request_id = r.id if r is not None else None
            except Exception:  # noqa: BLE001
                request_id = None
        if not request_id and "import_requests.create" in REGISTRY and contact_id:
            res = await _safe_dispatch(ctx, "import_requests.create", {"contact_id": contact_id, "opportunity_id": opp.id if opp else None,
                                                                       "title": ((opp.enquiry if opp else "") or "Import request")[:120],
                                                                       "source_ref": f"deposit:{inv.id}"}, notes)
            if res is not None and res.status == "ok":
                request_id = res.data["request"]["id"]
        if not request_id:
            await _safe_dispatch(ctx, "tasks.create", {"title": "Link import request", "opportunity_id": opp.id if opp else None,
                                                       "contact_id": contact_id, "notes": f"Deposit confirmed on invoice {inv.id}; "
                                                       "sourcing module unavailable — link the import request manually.",
                                                       "source_kind": "finance", "source_id": inv.id, "priority": "high"}, notes)
            notes.append("import request not linked: task created")
        else:
            kind, target_id = "import_request", request_id
            if opp is not None and "import_requests.attach_opportunity" in REGISTRY:
                await _safe_dispatch(ctx, "import_requests.attach_opportunity", {"request_id": request_id, "opportunity_id": opp.id}, notes)
            try:
                from ..models.sourcing import ImportRequest
                r = await ctx.db.get(ImportRequest, request_id)
                if r is not None and not r.deposit_rule:
                    await _safe_dispatch(ctx, "import_requests.set_deposit_rule", {"request_id": request_id, "amount": str(inv.amount_due),
                                                                                   "currency": inv.currency, "source_ref": f"invoice:{inv.id}"}, notes)
            except Exception:  # noqa: BLE001
                pass
            await _safe_dispatch(ctx, "import_requests.confirm_deposit", {"request_id": request_id, "payment_id": p.id,
                                                                          "amount": str(a.applied_amount or a.amount), "currency": inv.currency,
                                                                          "source_ref": source_ref, "confirmed_at": ctx.now.isoformat()}, notes)
    elif pipeline == "vehicle":
        if inv.sale_id:
            kind, target_id = "sale", inv.sale_id
        elif vehicle_id and contact_id:
            res = await _safe_dispatch(ctx, "sales.reserve", {"vehicle_id": vehicle_id, "buyer_contact_id": contact_id,
                                                              "opportunity_id": opp.id if opp else None, "agreement_id": inv.agreement_id,
                                                              "source_ref": source_ref, "dedupe_key": f"deposit:{inv.id}",
                                                              "notes": "reserved from confirmed deposit"}, notes)
            if res is not None and res.status == "ok":
                if res.data.get("outcome") == "conflict":
                    notes.append(f"reservation conflict: owner decision {res.data.get('approval_id')}")
                    kind, target_id = "reservation_conflict", res.data.get("active_sale_id")
                else:
                    kind, target_id = "sale", res.data["sale"]["id"]
        else:
            await _safe_dispatch(ctx, "tasks.create", {"title": "Link vehicle for reservation", "opportunity_id": opp.id if opp else None,
                                                       "contact_id": contact_id, "notes": f"Deposit confirmed on invoice {inv.id} but no vehicle "
                                                       "is linked to the opportunity.", "source_kind": "finance", "source_id": inv.id,
                                                       "priority": "high"}, notes)
            notes.append("no vehicle linked: task created")
    else:
        notes.append("no opportunity / pipeline on the obligation: handoff recorded without conversion")
    conversion = None
    if opp is not None and kind in ("import_request", "sale") and target_id:
        payload = {"opportunity_id": opp.id, "converted_kind": kind, "converted_id": target_id, "source_ref": source_ref,
                   "deposit_payment_id": p.id, "confirmed_at": ctx.now.isoformat()}
        res = await _safe_dispatch(ctx, "sales.set_conversion", payload, notes)
        if res is not None and res.status == "ok":
            conversion = res.data.get("converted")
        elif "sales.set_conversion" not in REGISTRY:
            # documented fallback only when the sales module is absent: set the single conversion link directly
            if not opp.converted_id:
                opp.converted_kind, opp.converted_id, opp.converted_at = kind, target_id, ctx.now
                opp.conversion_source_ref, opp.deposit_payment_id, opp.deposit_confirmed_at = source_ref, p.id, ctx.now
                opp.stage = "deposit_paid"
                opp.bump(ctx.actor.user_id)
                conversion = True
                notes.append("conversion set directly (sales.set_conversion unavailable)")
    h = {"done": True, "at": ctx.now.isoformat(), "pipeline": pipeline, "kind": kind, "id": target_id, "payment_id": p.id,
         "allocation_id": a.id, "opportunity_id": opp.id if opp else None, "vehicle_id": vehicle_id, "conversion": conversion,
         "cancelled_tasks": cancelled, "notes": notes, "reversals": [], "exceptions": []}
    inv.handoff = h
    inv.bump(ctx.actor.user_id)
    _mark_changed(ctx, "invoice", inv)
    ctx.record(f"Deposit Paid: {inv.amount_due} {inv.currency} confirmed → {kind or 'no handoff target'}"
               + (f" ({', '.join(notes)})" if notes else ""), entity_kind="invoice", entity_id=inv.id, kind="payment", state="deposit_paid",
               visibility="finance", sources=[source_ref], exception=bool(notes), details=h)
    ctx.emit("deposit.confirmed", aggregate_type="invoice", aggregate_id=inv.id, aggregate_version=inv.version,
             payload={"invoice_id": inv.id, "opportunity_id": opp.id if opp else None, "contact_id": contact_id, "vehicle_id": vehicle_id,
                      "pipeline": pipeline, "handoff_kind": kind, "handoff_id": target_id, "amount": str(inv.amount_due), "currency": inv.currency,
                      "payment_id": p.id, "provider": p.provider, "provider_receipt_ref": receipt_ref, "allocation_id": a.id,
                      "cancelled_tasks": cancelled, "notes": notes, "owner_reminder": "stage 2 (reads this event)"})
    return h


class ReverseAllocationIn(BaseModel):
    allocation_id: str
    reason: str = Field(min_length=1)
    kind: str = "refund"               # refund|dispute|correction
    expected_version: int | None = None


@command("payments.reverse_allocation", input=ReverseAllocationIn, perm="finance.write", action_class="owner_only",
         approval_kind="payment", summary=lambda p: f"Reverse allocation {p.allocation_id[:8]} ({p.kind})",
         description="Refund/dispute/correction: reverse a confirmed allocation, reopen the obligation, keep the handoff history "
                     "and pause dependent automation (thread:<sale or request id>). Never refunds money itself (E09).")
async def payments_reverse_allocation(ctx: CommandContext, inp: ReverseAllocationIn) -> dict:
    if inp.kind not in ("refund", "dispute", "correction"):
        raise ValidationFailed("kind must be refund|dispute|correction")
    a = (await ctx.db.execute(select(PaymentAllocation).where(PaymentAllocation.id == inp.allocation_id).with_for_update())).scalar_one_or_none()
    if a is None:
        raise NotFound("allocation not found")
    if inp.expected_version is not None and a.version != inp.expected_version:
        raise Conflict("allocation changed since you loaded it", current_version=a.version)
    if a.state == "reversed":
        inv = await _invoice(ctx, a.invoice_id)
        return {"allocation": serialize_payment_allocation(a), "invoice": serialize_invoice(inv), "reversed": False, "idempotent": True}
    if a.state != "confirmed":
        raise Blocked(f"allocation is {a.state}; only confirmed allocations can be reversed")
    p = await _payment(ctx, a.payment_id)
    inv = await _invoice(ctx, a.invoice_id)
    applied = a.applied_amount if a.applied_amount is not None else a.amount
    a.state, a.reversed_at, a.reason, a.reversal_kind = "reversed", ctx.now, inp.reason, inp.kind
    inv.amount_allocated = max(ZERO, (inv.amount_allocated or ZERO) - applied)
    inv.status = _invoice_status(inv)
    if inv.status in ("open", "partially_paid"):
        inv.reopened_at = ctx.now
        inv.satisfied_at = None
    inv.exceptions = list(inv.exceptions or []) + [{"kind": inp.kind, "at": ctx.now.isoformat(), "allocation_id": a.id, "payment_id": p.id,
                                                    "reason": inp.reason, "reversed_amount": str(applied)}]
    p.allocated_amount = max(ZERO, (p.allocated_amount or ZERO) - a.amount)
    p.exceptions = list(p.exceptions or []) + [{"kind": f"allocation_reversed_{inp.kind}", "at": ctx.now.isoformat(), "allocation_id": a.id,
                                                "reason": inp.reason}]
    for e in p.exceptions:
        if e.get("kind") == "refund_exceeds_allocation" and e.get("open"):
            e["open"] = False
            e["resolved_at"] = ctx.now.isoformat()
    paused: list[str] = []
    h = dict(inv.handoff or {})
    if h.get("done"):
        if h.get("id"):
            paused.append(await _pause_thread(ctx, f"thread:{h['id']}", f"{inp.kind}: allocation reversed — {inp.reason}"))
        if h.get("kind") == "import_request" and h.get("id"):
            await _safe_dispatch(ctx, "import_requests.pause", {"request_id": h["id"], "reason": f"deposit {inp.kind}: {inp.reason}"}, [])
        if h.get("kind") == "sale" and h.get("id"):
            s = await ctx.db.get(Sale, h["id"])
            if s is not None:
                s.exceptions = list(s.exceptions or []) + [{"kind": f"deposit_{inp.kind}", "at": ctx.now.isoformat(), "reason": inp.reason,
                                                            "allocation_id": a.id}]
                s.bump(ctx.actor.user_id)
        h["reversals"] = list(h.get("reversals") or []) + [{"at": ctx.now.isoformat(), "kind": inp.kind, "reason": inp.reason,
                                                            "allocation_id": a.id, "paused": paused}]
        inv.handoff = h  # history preserved; conversion link is never deleted here
    ctx.touch(a, "payment_allocation")
    ctx.touch(inv, "invoice")
    ctx.touch(p, "payment")
    ctx.record(f"Allocation reversed ({inp.kind}): {a.amount} {a.currency} from {inv.kind} obligation — {inp.reason}"
               + (f"; paused {', '.join(paused)}" if paused else ""), entity_kind="invoice", entity_id=inv.id, kind="payment",
               state="reversed", visibility="finance", exception=True, details={"allocation_id": a.id, "payment_id": p.id, "paused": paused})
    ctx.emit("payment.allocated", aggregate_type="invoice", aggregate_id=inv.id, aggregate_version=inv.version,
             payload={"change": "reversed", "reversal": inp.kind, "payment_id": p.id, "invoice_id": inv.id, "allocation_id": a.id,
                      "amount": str(a.amount), "currency": a.currency, "invoice_status": inv.status, "paused": paused,
                      "handoff_kept": bool(h.get("done"))})
    ctx.emit("metrics.invalidated", aggregate_type="invoice", aggregate_id=inv.id, payload={"reason": f"allocation.{inp.kind}"})
    return {"allocation": serialize_payment_allocation(a), "invoice": serialize_invoice(inv), "payment": serialize_payment(p),
            "reversed": True, "paused": paused, "handoff": inv.handoff or {}}
