"""Finance read models (spec §6.2 money presentation, §2.4 cohort helpers, invariant 15).

One non-duplicated cost basis is used everywhere: a cost item contributes to a vehicle EITHER through its confirmed
allocations OR (unallocated single-vehicle item) through its own vehicle link — never both, never invoice + payment.
`sold_cohort()` is the single source the Home metrics reporting calls. Money is Decimal + ISO currency; unknown stays
unknown (None + flag), never zero-filled."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, or_, select

from ..core.money import quantize, split_balanced
from ..core.time import ensure_aware
from ..models.contacts import Contact
from ..models.finance import CostAllocation, CostEvidence, CostItem, Invoice, LedgerMapping, LedgerRow, Payment, PaymentAllocation, Sale
from ..models.vehicles import Vehicle
from . import finance as fin
from .finance import money, iso
from .sales_records import serialize_sale

ZERO = Decimal("0")
ESTIMATE_STATUSES = ("estimated", "quoted")
COMPLETED_SALE_STATUSES = ("completed", "delivered")
REVIEW_EVIDENCE_STATES = ("unmatched", "proposed", "ambiguous", "conflict")


def _usd_share(item: CostItem, line_amount: Decimal, all_amounts: list[Decimal] | None, idx: int | None) -> tuple[Decimal | None, bool]:
    """USD value of one line: exact for USD; from usd_amount (actual or estimated fx) otherwise; None + fx_missing flag."""
    if item.currency == "USD":
        return line_amount, False
    if item.usd_amount is None:
        return None, True
    net = fin.net_amount(item) or ZERO
    if net == 0:
        return ZERO, False
    usd_net = quantize(item.usd_amount * (net / (item.active_amount or net)), "USD") if item.active_amount else item.usd_amount
    if all_amounts and idx is not None and len(all_amounts) > 1:
        parts = split_balanced(usd_net, all_amounts, "USD") if sum(all_amounts) else [ZERO] * len(all_amounts)
        return parts[idx], False
    return quantize(usd_net, "USD"), False


def _paid_usd(paid: Decimal, amount: Decimal, usd: Decimal | None) -> Decimal | None:
    if usd is None:
        return None
    if amount == 0 or paid <= 0:
        return ZERO
    return quantize(usd * (paid / amount), "USD")


async def cost_lines(db, vehicle_ids: list[str] | None = None, *, as_of: datetime | None = None) -> dict[str, list[dict]]:
    """Per-vehicle non-duplicated cost lines. vehicle_ids None = every vehicle with costs."""
    q = select(CostItem).where(CostItem.status != "cancelled")
    if as_of is not None:
        q = q.where(CostItem.created_at <= as_of)
    items = (await db.execute(q)).scalars().all()
    allocs_by_item: dict[str, list[CostAllocation]] = {}
    if items:
        rows = (await db.execute(select(CostAllocation).where(CostAllocation.cost_item_id.in_([i.id for i in items]))
                                 .order_by(CostAllocation.created_at))).scalars().all()
        for a in rows:
            allocs_by_item.setdefault(a.cost_item_id, []).append(a)
    want = set(vehicle_ids) if vehicle_ids is not None else None
    out: dict[str, list[dict]] = {}

    def add(vid: str, line: dict) -> None:
        if want is None or vid in want:
            out.setdefault(vid, []).append(line)

    for it in items:
        allocs = allocs_by_item.get(it.id, [])
        estimate = it.status in ESTIMATE_STATUSES or it.amount_invoiced is None
        if allocs:
            amounts = [(a.amount or ZERO) - (a.amount_credited or ZERO) for a in allocs]
            for idx, a in enumerate(allocs):
                usd, fx_missing = _usd_share(it, amounts[idx], amounts, idx)
                paid_total = min(it.amount_paid or ZERO, sum(amounts))
                paid_share = split_balanced(paid_total, amounts, it.currency)[idx] if paid_total > 0 and sum(amounts) > 0 else ZERO
                add(a.vehicle_id, {"cost_item_id": it.id, "category": it.category, "description": it.description, "vendor": it.vendor_name,
                                   "currency": it.currency, "amount": amounts[idx], "usd_amount": usd, "fx_missing": fx_missing,
                                   "status": it.status, "estimate": estimate, "basis": "allocation", "allocation_state": a.state,
                                   "needs_review": a.state != "confirmed", "pass_through": it.is_pass_through, "shared": len(allocs) > 1,
                                   "paid": paid_share, "paid_usd": _paid_usd(paid_share, amounts[idx], usd),
                                   "credited": a.amount_credited or ZERO, "occurred_at": it.occurred_at, "fx_actual": it.fx_actual})
        elif it.vehicle_id:
            amt = fin.net_amount(it) or ZERO
            usd, fx_missing = _usd_share(it, amt, None, None)
            add(it.vehicle_id, {"cost_item_id": it.id, "category": it.category, "description": it.description, "vendor": it.vendor_name,
                                "currency": it.currency, "amount": amt, "usd_amount": usd, "fx_missing": fx_missing, "status": it.status,
                                "estimate": estimate, "basis": "direct", "allocation_state": it.allocation_state, "needs_review": False,
                                "pass_through": it.is_pass_through, "shared": False, "paid": min(it.amount_paid or ZERO, amt),
                                "paid_usd": _paid_usd(min(it.amount_paid or ZERO, amt), amt, usd),
                                "credited": it.amount_credited or ZERO, "occurred_at": it.occurred_at, "fx_actual": it.fx_actual})
    return out


def _sum_usd(lines: list[dict]) -> tuple[Decimal, int, int]:
    """(usd total over lines with a USD value, lines missing fx, estimated lines)."""
    total, missing, est = ZERO, 0, 0
    for l in lines:
        if l["pass_through"]:
            continue
        if l["usd_amount"] is None:
            missing += 1
        else:
            total += l["usd_amount"]
        if l["estimate"]:
            est += 1
    return quantize(total, "USD"), missing, est


def _by_category(lines: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for l in lines:
        if l["pass_through"]:
            continue
        c = out.setdefault(l["category"], {"usd": ZERO, "lines": 0, "fx_missing": 0, "estimated": 0})
        c["lines"] += 1
        if l["usd_amount"] is None:
            c["fx_missing"] += 1
        else:
            c["usd"] += l["usd_amount"]
        if l["estimate"]:
            c["estimated"] += 1
    return {k: {**v, "usd": money(v["usd"], "USD")} for k, v in out.items()}


async def unmatched_evidence_for(db, vehicle_id: str) -> tuple[int, dict]:
    v = await db.get(Vehicle, vehicle_id)
    clauses = [CostEvidence.proposed_vehicle_id == vehicle_id]
    if v is not None and v.stock_no:
        clauses.append(func.upper(CostEvidence.vehicle_ref_text) == v.stock_no.upper())
    rows = (await db.execute(select(CostEvidence).where(CostEvidence.is_current.is_(True), CostEvidence.match_state.in_(REVIEW_EVIDENCE_STATES),
                                                        or_(*clauses)))).scalars().all()
    totals: dict[str, Decimal] = {}
    for e in rows:
        if e.amount is not None and e.currency:
            totals[e.currency] = totals.get(e.currency, ZERO) + e.amount
    return len(rows), {k: str(v) for k, v in totals.items()}


async def vehicle_money(db, vehicle_id: str) -> dict:
    """Section 6.2: estimated total, committed/invoiced, cash paid, remaining payable, unmatched evidence, sale price, margin."""
    v = await db.get(Vehicle, vehicle_id)
    if v is None:
        return {"vehicle_id": vehicle_id, "missing": True}
    lines = (await cost_lines(db, [vehicle_id])).get(vehicle_id, [])
    est_total, fx_missing, est_lines = _sum_usd(lines)
    committed_lines = [l for l in lines if not l["estimate"]]
    committed, _, _ = _sum_usd(committed_lines)
    # cash paid (vendor settlement) per line, from paid observations (verified settlement / paid evidence)
    cash, cash_fx_missing = ZERO, 0
    for l in committed_lines:
        if l["pass_through"] or l["paid"] <= 0:
            continue
        if l["paid_usd"] is None:
            cash_fx_missing += 1
            continue
        cash += l["paid_usd"]
    cash = quantize(cash, "USD")
    remaining = max(ZERO, committed - cash)
    unmatched_n, unmatched_totals = await unmatched_evidence_for(db, vehicle_id)
    review_lines = [l for l in lines if l["needs_review"]]
    sale = (await db.execute(select(Sale).where(Sale.vehicle_id == vehicle_id, Sale.status.notin_(("cancelled", "expired")))
                             .order_by(Sale.is_active.desc(), Sale.created_at.desc()))).scalars().first()
    approved_price = None
    price_label = None
    if sale is not None and sale.price is not None and sale.status in ("agreed", "paid", "completed", "delivered"):
        approved_price = (sale.price - (sale.credits or ZERO), sale.currency)
        price_label = f"agreed sale price ({sale.status})"
    elif v.asking_price is not None and v.price_approved_at:
        approved_price = (v.asking_price, v.asking_currency or "USD")
        price_label = "approved asking price"
    margin = None
    margin_note = []
    if approved_price is not None:
        if approved_price[1] != "USD":
            margin_note.append(f"sale price in {approved_price[1]}; no USD basis")
        elif fx_missing:
            margin_note.append(f"{fx_missing} cost line(s) have no FX conversion")
        else:
            margin = quantize(approved_price[0] - est_total, "USD")
    complete = not est_lines and not fx_missing and not unmatched_n and not review_lines
    return {
        "vehicle_id": vehicle_id, "stock_no": v.stock_no, "currency_basis": "USD",
        "estimated_total": money(est_total, "USD"), "committed_invoiced": money(committed, "USD"), "cash_paid": money(cash, "USD"),
        "remaining_payable": money(remaining, "USD"),
        "unmatched_evidence": {"count": unmatched_n, "totals": unmatched_totals},
        "approved_sale_price": money(*approved_price) if approved_price else None, "sale_price_label": price_label,
        "sale_id": sale.id if sale else None, "sale_status": sale.status if sale else None,
        "estimated_margin": money(margin, "USD") if margin is not None else None,
        "margin_label": ("Recorded margin" if complete else "Estimated margin") if margin is not None else "Margin unavailable",
        "completeness": {"label": "Recorded" if complete else "Estimated", "estimated_lines": est_lines, "fx_missing_lines": fx_missing,
                         "cash_fx_missing": cash_fx_missing, "unmatched_evidence": unmatched_n, "allocations_needing_review": len(review_lines),
                         "notes": margin_note, "components": {"estimated": est_lines, "invoiced": len(committed_lines)}},
        "by_category": _by_category(lines),
        "lines": [{**{k: v for k, v in l.items() if k not in ("amount", "usd_amount", "paid", "paid_usd", "credited", "occurred_at")},
                   "amount": money(l["amount"], l["currency"]), "usd_amount": money(l["usd_amount"], "USD") if l["usd_amount"] is not None else None,
                   "paid": money(l["paid"], l["currency"]), "credited": money(l["credited"], l["currency"]), "occurred_at": iso(l["occurred_at"])}
                  for l in lines],
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


def _period(period_from: datetime, period_to: datetime) -> tuple[datetime, datetime]:
    a, b = ensure_aware(period_from), ensure_aware(period_to)
    if b <= a:
        raise ValueError("period_to must be after period_from")
    return a, b


async def sold_cohort(db, period_from: datetime, period_to: datetime) -> dict:
    """Spec §2.4 / K01: sales completed in [from, to) with net sale value and the non-duplicated allocated cost basis.
    Deposits, payouts, unsold inventory and estimates never become recorded profit (invariant 15)."""
    a, b = _period(period_from, period_to)
    sales = (await db.execute(select(Sale).where(Sale.completed_at.is_not(None), Sale.completed_at >= a, Sale.completed_at < b,
                                                 Sale.status.in_(COMPLETED_SALE_STATUSES)).order_by(Sale.completed_at))).scalars().all()
    # one sale per vehicle in the cohort (a cancelled-then-resold vehicle keeps only its live completed sale)
    by_vehicle: dict[str, Sale] = {}
    for s in sales:
        by_vehicle.setdefault(s.vehicle_id, s)
    sales = list(by_vehicle.values())
    lines_by_vehicle = await cost_lines(db, [s.vehicle_id for s in sales]) if sales else {}
    vehicles = {v.id: v for v in (await db.execute(select(Vehicle).where(Vehicle.id.in_(list(by_vehicle))))).scalars().all()} if sales else {}
    rows = []
    net_total, cost_total = ZERO, ZERO
    fx_missing_total, est_total_lines, review_total, unallocated = 0, 0, 0, []
    currencies = set()
    restatements = []
    for s in sales:
        lines = lines_by_vehicle.get(s.vehicle_id, [])
        cost_usd, fx_missing, est = _sum_usd(lines)
        review = [l for l in lines if l["needs_review"]]
        net = (s.price or ZERO) - (s.credits or ZERO)
        currencies.add(s.currency)
        net_usd = net if s.currency == "USD" else None
        if net_usd is not None:
            net_total += net_usd
        cost_total += cost_usd
        fx_missing_total += fx_missing
        est_total_lines += est
        review_total += len(review)
        if not lines:
            unallocated.append({"vehicle_id": s.vehicle_id, "reason": "no costs recorded for this sold vehicle"})
        acq = vehicles.get(s.vehicle_id).acquired_at if vehicles.get(s.vehicle_id) else None
        days = (ensure_aware(s.completed_at) - ensure_aware(acq)).days if acq else None
        restatements.extend([{**r, "sale_id": s.id} for r in (s.restatements or [])])
        rows.append({"sale": serialize_sale(s), "vehicle_id": s.vehicle_id, "stock_no": vehicles.get(s.vehicle_id).stock_no if vehicles.get(s.vehicle_id) else None,
                     "net_sale_value": money(net, s.currency), "net_sale_value_usd": money(net_usd, "USD") if net_usd is not None else None,
                     "sales_tax_excluded": money(s.sales_tax, s.currency), "pass_through_excluded": money(s.pass_through, s.currency),
                     "costs_usd": money(cost_usd, "USD"), "by_category": _by_category(lines),
                     "gross_profit_usd": money(net_usd - cost_usd, "USD") if net_usd is not None else None,
                     "flags": {"fx_missing_lines": fx_missing, "estimated_lines": est, "allocations_needing_review": len(review), "no_costs": not lines},
                     "days_to_sale": days, "completed_at": iso(s.completed_at), "restatements": s.restatements or []})
    unknown_net = any(r["net_sale_value_usd"] is None for r in rows)
    gross = (net_total - cost_total) if rows and not unknown_net else None
    margin = None
    if gross is not None and net_total != 0:
        margin = (gross / net_total).quantize(Decimal("0.0001"))
    complete = rows and not fx_missing_total and not est_total_lines and not review_total and not unallocated and not unknown_net
    durations = sorted(d for d in (r["days_to_sale"] for r in rows) if d is not None)
    median = None
    if durations:
        mid = len(durations) // 2
        median = durations[mid] if len(durations) % 2 else (durations[mid - 1] + durations[mid]) / 2
    return {
        "period": {"from": a.isoformat(), "to": b.isoformat()}, "currency": "USD", "as_of": datetime.now(timezone.utc).isoformat(),
        "vehicles_sold": len(rows), "net_sales_value": money(net_total, "USD") if not unknown_net else None,
        "costs_total": money(cost_total, "USD"), "costs_by_category": _by_category([l for s in sales for l in lines_by_vehicle.get(s.vehicle_id, [])]),
        "gross_profit": money(gross, "USD") if gross is not None else None,
        "gross_profit_label": "Recorded gross profit" if complete else "Estimated gross profit",
        "gross_margin": str(margin) if margin is not None else None,
        "gross_margin_note": None if margin is not None else ("net sales value is zero or unknown" if rows else "no sales in period"),
        "days_to_sale_median": median, "days_to_sale_included": len(durations), "days_to_sale_excluded": len(rows) - len(durations),
        "flags": {"fx_missing_lines": fx_missing_total, "estimated_lines": est_total_lines, "allocations_needing_review": review_total,
                  "unallocated": unallocated, "sale_currencies": sorted(currencies), "unknown_net": unknown_net,
                  "excluded": ["deposits", "unsold inventory", "payouts", "sales tax", "pass-through charges"]},
        "restatements": restatements, "sales": rows,
    }


async def unsold_inventory_cost(db, as_of: datetime | None = None) -> dict:
    as_of = ensure_aware(as_of) or datetime.now(timezone.utc)
    vehicles = (await db.execute(select(Vehicle).where(Vehicle.archived_at.is_(None), Vehicle.allocation.in_(("inventory", "reserved")),
                                                       Vehicle.logistics_state != "candidate"))).scalars().all()
    lines_by = await cost_lines(db, [v.id for v in vehicles], as_of=as_of) if vehicles else {}
    rows, total, fx_missing_total, est_total = [], ZERO, 0, 0
    for v in vehicles:
        lines = lines_by.get(v.id, [])
        if not lines:
            continue
        cost, fx_missing, est = _sum_usd(lines)
        total += cost
        fx_missing_total += fx_missing
        est_total += est
        rows.append({"vehicle_id": v.id, "stock_no": v.stock_no, "allocation": v.allocation, "label": "Reserved" if v.allocation == "reserved" else "Inventory",
                     "cost_usd": money(cost, "USD"), "flags": {"fx_missing_lines": fx_missing, "estimated_lines": est},
                     "asking_price": money(v.asking_price, v.asking_currency) if v.asking_price is not None and v.price_approved_at else None})
    return {"as_of": as_of.isoformat(), "currency": "USD", "total": money(total, "USD"), "vehicles": rows,
            "flags": {"fx_missing_lines": fx_missing_total, "estimated_lines": est_total, "vehicles_without_costs":
                      [v.stock_no or v.id for v in vehicles if not lines_by.get(v.id)]}}


# ── Finance tabs ─────────────────────────────────────────────────────────────
def _in_scope(vid: str | None, limit: set[str] | None) -> bool:
    return limit is None or vid is None or vid in limit


async def needs_matching(db, vehicle_limit: set[str] | None = None) -> dict:
    ev = (await db.execute(select(CostEvidence).where(CostEvidence.is_current.is_(True), CostEvidence.match_state.in_(REVIEW_EVIDENCE_STATES))
                           .order_by(CostEvidence.created_at.desc()))).scalars().all()
    ev = [e for e in ev if _in_scope(e.proposed_vehicle_id, vehicle_limit)]
    pa = (await db.execute(select(PaymentAllocation).where(PaymentAllocation.state == "proposed").order_by(PaymentAllocation.created_at.desc()))).scalars().all()
    ca = (await db.execute(select(CostAllocation).where(CostAllocation.state == "proposed").order_by(CostAllocation.created_at.desc()))).scalars().all()
    ca = [a for a in ca if _in_scope(a.vehicle_id, vehicle_limit)]
    reported = (await db.execute(select(Payment).where(Payment.status == "reported").order_by(Payment.created_at.desc()))).scalars().all()
    items = ([{**fin.serialize_evidence(e), "item_kind": "cost_evidence"} for e in ev]
             + [{**fin.serialize_payment_allocation(a), "item_kind": "payment_allocation"} for a in pa]
             + [{**fin.serialize_allocation(a), "item_kind": "cost_allocation"} for a in ca]
             + [{**fin.serialize_payment(p), "item_kind": "payment_reported"} for p in reported])
    return {"items": items, "total": len(items),
            "counts": {"evidence": len(ev), "payment_allocations": len(pa), "cost_allocations": len(ca), "payments_reported": len(reported)}}


async def receivables(db) -> dict:
    inv = (await db.execute(select(Invoice).where(Invoice.status.in_(("open", "partially_paid", "overpaid"))).order_by(Invoice.due_at.asc().nulls_last(),
                                                                                                                       Invoice.created_at))).scalars().all()
    items = [fin.serialize_invoice(i) for i in inv]
    totals: dict[str, Decimal] = {}
    for i in inv:
        rem = (i.amount_due or ZERO) - (i.amount_allocated or ZERO)
        if rem > 0:
            totals[i.currency] = totals.get(i.currency, ZERO) + rem
    return {"items": items, "total": len(items), "outstanding": {k: str(v) for k, v in totals.items()}}


async def payables(db, vehicle_limit: set[str] | None = None) -> dict:
    items = (await db.execute(select(CostItem).where(CostItem.status.in_(("invoiced", "partially_paid"))).order_by(CostItem.occurred_at.asc().nulls_last()))).scalars().all()
    items = [c for c in items if _in_scope(c.vehicle_id, vehicle_limit)]
    out, totals = [], {}
    for c in items:
        rem = max(ZERO, (c.active_amount or ZERO) - (c.amount_credited or ZERO) - (c.amount_paid or ZERO))
        if rem <= 0:
            continue
        totals[c.currency] = totals.get(c.currency, ZERO) + rem
        out.append({**fin.serialize_cost_item(c), "remaining": money(rem, c.currency)})
    return {"items": out, "total": len(out), "outstanding": {k: str(v) for k, v in totals.items()}}


async def vehicle_costs(db, vehicle_limit: set[str] | None = None) -> dict:
    lines_by = await cost_lines(db, list(vehicle_limit) if vehicle_limit is not None else None)
    ids = list(lines_by)
    vehicles = {v.id: v for v in (await db.execute(select(Vehicle).where(Vehicle.id.in_(ids)))).scalars().all()} if ids else {}
    out = []
    for vid, lines in lines_by.items():
        v = vehicles.get(vid)
        total, fx_missing, est = _sum_usd(lines)
        committed, _, _ = _sum_usd([l for l in lines if not l["estimate"]])
        out.append({"vehicle_id": vid, "stock_no": v.stock_no if v else None, "title": v.title if v else None, "allocation": v.allocation if v else None,
                    "estimated_total": money(total, "USD"), "committed_invoiced": money(committed, "USD"), "lines": len(lines),
                    "by_category": _by_category(lines), "flags": {"fx_missing_lines": fx_missing, "estimated_lines": est,
                                                                  "allocations_needing_review": sum(1 for l in lines if l["needs_review"])},
                    "label": "Estimated" if (fx_missing or est) else "Recorded"})
    out.sort(key=lambda r: r["stock_no"] or "")
    return {"items": out, "total": len(out)}


async def summary(db, *, with_amounts: bool) -> dict:
    nm = await needs_matching(db)
    rec = await receivables(db)
    pay = await payables(db)
    now = datetime.now(timezone.utc)
    from ..core.time import period_bounds
    a, b = period_bounds("month")
    cohort = await sold_cohort(db, a, b)
    out = {"as_of": now.isoformat(), "needs_matching": nm["counts"], "receivables_count": rec["total"], "payables_count": pay["total"],
           "vehicles_sold_this_month": cohort["vehicles_sold"], "period": cohort["period"],
           "status": {"needs_matching": nm["total"] > 0, "receivables_open": rec["total"] > 0, "payables_open": pay["total"] > 0}}
    if with_amounts:
        out.update({"receivables_outstanding": rec["outstanding"], "payables_outstanding": pay["outstanding"],
                    "sold_cohort": {k: cohort[k] for k in ("net_sales_value", "costs_total", "gross_profit", "gross_profit_label", "gross_margin",
                                                           "gross_margin_note", "flags")},
                    "money_hidden": False})
    else:
        out.update({"receivables_outstanding": None, "payables_outstanding": None, "sold_cohort": None, "money_hidden": True})
    return out


# ── export (spec §6.4) ───────────────────────────────────────────────────────
async def export_csv(db, kind: str = "allocations", period_from: datetime | None = None, period_to: datetime | None = None,
                     vehicle_id: str | None = None, vehicle_limit: set[str] | None = None) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    a, b = ensure_aware(period_from), ensure_aware(period_to)
    if kind == "allocations":
        w.writerow(["cost_item_id", "vehicle_id", "stock_no", "category", "vendor", "description", "amount", "credited", "net", "currency",
                    "basis", "state", "confirmed_at", "usd_amount", "fx_source", "invoice_ref", "occurred_at", "source_links"])
        allocs = (await db.execute(select(CostAllocation, CostItem).join(CostItem, CostItem.id == CostAllocation.cost_item_id)
                                   .where(CostAllocation.state == "confirmed").order_by(CostItem.occurred_at.asc().nulls_last()))).all()
        vids = {al.vehicle_id for al, _ in allocs}
        vehicles = {v.id: v for v in (await db.execute(select(Vehicle).where(Vehicle.id.in_(list(vids))))).scalars().all()} if vids else {}
        for al, it in allocs:
            if vehicle_id and al.vehicle_id != vehicle_id:
                continue
            if not _in_scope(al.vehicle_id, vehicle_limit):
                continue
            if a and it.occurred_at and ensure_aware(it.occurred_at) < a:
                continue
            if b and it.occurred_at and ensure_aware(it.occurred_at) >= b:
                continue
            links = " ".join(l for l in [fin.source_link(e) for e in (await db.execute(select(CostEvidence).where(CostEvidence.cost_item_id == it.id,
                                                                                                                 CostEvidence.is_current.is_(True)))).scalars().all()] if l)
            v = vehicles.get(al.vehicle_id)
            w.writerow([it.id, al.vehicle_id, v.stock_no if v else "", it.category, it.vendor_name or "", it.description, str(al.amount),
                        str(al.amount_credited or ZERO), str((al.amount or ZERO) - (al.amount_credited or ZERO)), al.currency, al.basis, al.state,
                        iso(al.confirmed_at) or "", str(it.usd_amount) if it.usd_amount is not None else "", it.fx_source or "", it.invoice_ref or "",
                        iso(it.occurred_at) or "", links])
    elif kind == "payments":
        w.writerow(["payment_id", "provider", "provider_payment_id", "status", "amount", "currency", "refunded", "fee", "net", "is_payout",
                    "occurred_at", "invoice_id", "invoice_kind", "allocated", "allocation_state", "source_ref"])
        pays = (await db.execute(select(Payment).order_by(Payment.occurred_at.asc().nulls_last()))).scalars().all()
        allocs = (await db.execute(select(PaymentAllocation, Invoice).join(Invoice, Invoice.id == PaymentAllocation.invoice_id)
                                   .where(PaymentAllocation.state == "confirmed"))).all()
        by_pay: dict[str, list] = {}
        for al, inv in allocs:
            by_pay.setdefault(al.payment_id, []).append((al, inv))
        for p in pays:
            if a and p.occurred_at and ensure_aware(p.occurred_at) < a:
                continue
            if b and p.occurred_at and ensure_aware(p.occurred_at) >= b:
                continue
            rows = by_pay.get(p.id) or [(None, None)]
            for al, inv in rows:
                if vehicle_id and (inv is None or inv.vehicle_id != vehicle_id):
                    continue
                w.writerow([p.id, p.provider, p.provider_payment_id or "", p.status, str(p.amount), p.currency, str(p.refunded_amount or ZERO),
                            str(p.fee_amount) if p.fee_amount is not None else "", str(p.net_amount) if p.net_amount is not None else "",
                            "yes" if p.is_payout else "no", iso(p.occurred_at) or "", inv.id if inv else "", inv.kind if inv else "",
                            str(al.amount) if al else "", al.state if al else "", p.source_ref or p.evidence_ref or ""])
    elif kind == "costs":
        w.writerow(["cost_item_id", "vehicle_id", "category", "vendor", "description", "status", "estimated", "quoted", "invoiced", "paid", "credited",
                    "active_amount", "currency", "usd_amount", "fx_actual", "invoice_ref", "order_ref", "occurred_at", "source_links"])
        items = (await db.execute(select(CostItem).where(CostItem.status != "cancelled").order_by(CostItem.occurred_at.asc().nulls_last()))).scalars().all()
        for it in items:
            if vehicle_id and it.vehicle_id != vehicle_id:
                continue
            if not _in_scope(it.vehicle_id, vehicle_limit):
                continue
            if a and it.occurred_at and ensure_aware(it.occurred_at) < a:
                continue
            if b and it.occurred_at and ensure_aware(it.occurred_at) >= b:
                continue
            evs = (await db.execute(select(CostEvidence).where(CostEvidence.cost_item_id == it.id, CostEvidence.is_current.is_(True)))).scalars().all()
            links = " ".join(l for l in (fin.source_link(e) for e in evs) if l)
            w.writerow([it.id, it.vehicle_id or "", it.category, it.vendor_name or "", it.description, it.status,
                        str(it.amount_estimated) if it.amount_estimated is not None else "", str(it.amount_quoted) if it.amount_quoted is not None else "",
                        str(it.amount_invoiced) if it.amount_invoiced is not None else "", str(it.amount_paid or ZERO), str(it.amount_credited or ZERO),
                        str(it.active_amount) if it.active_amount is not None else "", it.currency,
                        str(it.usd_amount) if it.usd_amount is not None else "", "yes" if it.fx_actual else "no", it.invoice_ref or "", it.order_ref or "",
                        iso(it.occurred_at) or "", links])
    else:
        raise ValueError("kind must be allocations|payments|costs")
    return buf.getvalue()
