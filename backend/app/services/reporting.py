"""Home business metrics (spec §2.2, §2.4, §12.1 "Home reporting", invariant 15).

Deterministic and model-free: every number is computed from canonical records, never from an LLM summary.

ONE sold-vehicle cohort definition is used everywhere — sales whose recorded completion date falls inside the
selected period and that are not cancelled — and it is the *same* function Finance uses
(`services.finance_queries.sold_cohort`), which also supplies the non-duplicated cost basis (a cost item reaches a
vehicle either through its confirmed allocations or through its own vehicle link, never invoice + payment twice).
So Costs / Profit on Home open Finance with identical filters and identical ids (K01).

Rules kept here:
  * Money is Decimal + ISO currency, serialized as {"amount": "123.45", "currency": "USD"}.
  * Missing data stays null with a reason — never zero. Negative profit is retained, never floored.
  * FX uses the same basis as Finance (usd_amount when present, else the amount when the currency is USD, else
    unknown + a coverage flag). Mixed currencies are never silently aggregated.
  * Deposits, payouts, unsold inventory and estimates never become recorded profit (invariant 15).
  * Role safety: amounts need `costs.read`; `finance.status` holders get counts and timing only; anyone else
    (a mechanic) gets nothing financial at all.
  * `MetricSnapshot` is a rebuildable cache keyed by period + timezone + cohort hash. Canonical events mark it
    stale; a stale snapshot is served with `stale: true` and its own `as_of` rather than a false zero.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import Denied, ValidationFailed
from ..core.ids import stable_hash
from ..core.money import quantize
from ..core.time import PHOENIX, ensure_aware, period_bounds
from ..domain.access import can_see_costs, can_see_finance_status, visible_vehicle_ids
from ..domain.actors import Actor
from ..domain.commands import CommandContext, command
from ..domain.events import on_event
from ..domain.jobs import sweep
from ..models.finance import COST_CATEGORIES, CostItem, Payment
from ..models.legacy import Setting
from ..models.reporting import MetricSnapshot
from ..models.vehicles import Vehicle
from . import finance_queries as fq
from .finance import iso, money

log = logging.getLogger("azkt.reporting")

ZERO = Decimal("0")
USD = "USD"
COMPUTATION_VERSION = 1
SNAPSHOT_PREFIX = "home_metrics"
PERIOD_KINDS = ("month", "7d", "30d", "custom")
# Cost categories that must be recorded (not estimated, not missing) before gross profit is called "Recorded".
DEFAULT_REQUIRED_CATEGORIES = ("purchase", "import", "recon")
COHORT_DEFINITION = ("sales with a recorded completion date inside the period, not cancelled "
                     "(a reservation or deposit is not a completed sale)")
PROFIT_CAVEAT = ("gross profit on the sold cohort only; not business net profit — it excludes unallocated "
                 "overhead and tax")
# Canonical events that make a cached snapshot stale (spec §2.4, §12.1).
INVALIDATING_EVENTS = ("sale.*", "cost.*", "payment.allocated", "deposit.confirmed", "intake.applied",
                       "milestone.changed", "metrics.invalidated")


# ── period ───────────────────────────────────────────────────────────────────
def resolve_period(kind: str = "month", tz: str = PHOENIX, start: date | str | None = None,
                   end: date | str | None = None, ref: datetime | None = None) -> dict:
    """[from, to) as UTC instants plus the label shown next to every value (spec §2.4)."""
    kind = (kind or "month").lower()
    if start or end:
        kind = "custom"
    if kind not in PERIOD_KINDS:
        raise ValidationFailed(f"period must be one of {PERIOD_KINDS}")
    if kind == "custom":
        s, e = _as_date(start, "start"), _as_date(end, "end")
        if not (s and e):
            raise ValidationFailed("a custom period needs both start and end (YYYY-MM-DD)")
        if e < s:
            raise ValidationFailed("end must not be before start")
        a, b = period_bounds("custom", tz, start=s, end=e)
    else:
        a, b = period_bounds(kind, tz, ref=ref)
    return {"kind": kind, "from": a.isoformat(), "to": b.isoformat(), "timezone": tz,
            "label": _period_label(kind, a, b, tz), "_a": a, "_b": b}


def _as_date(value, label: str) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ValidationFailed(f"{label} must be YYYY-MM-DD")


def _period_label(kind: str, a: datetime, b: datetime, tz: str) -> str:
    from ..core.time import to_zone
    la, lb = to_zone(a, tz), to_zone(b, tz) - timedelta(seconds=1)
    if kind == "month":
        return la.strftime("%B %Y")
    if kind in ("7d", "30d"):
        return f"Last {kind[:-1]} days"
    return f"{la.date().isoformat()} – {lb.date().isoformat()}"


# ── configurable cost-completeness rule ──────────────────────────────────────
async def required_cost_categories(db: AsyncSession) -> tuple[list[str], str]:
    """Categories that must be recorded before gross profit is labelled "Recorded".

    Configurable through the `reporting` settings row ({"required_cost_categories": [...]}); the default is
    purchase + import + recon. Read defensively so a missing/corrupt row never breaks the read path."""
    data: dict = {}
    try:
        row = await db.get(Setting, "reporting")
        if row is not None and isinstance(row.value, dict):
            data = dict(row.value.get("data") or {}) if "data" in row.value and "version" in row.value else dict(row.value)
    except Exception:  # noqa: BLE001
        data = {}
    cats = [c for c in (data.get("required_cost_categories") or []) if c in COST_CATEGORIES]
    if cats:
        return cats, "settings"
    return list(DEFAULT_REQUIRED_CATEGORIES), "default"


# ── helpers ──────────────────────────────────────────────────────────────────
def _median(values: list) -> tuple[float | int | None, int]:
    vs = sorted(v for v in values if v is not None)
    if not vs:
        return None, 0
    mid = len(vs) // 2
    return (vs[mid] if len(vs) % 2 else (vs[mid - 1] + vs[mid]) / 2), len(vs)


def _days(a: datetime | None, b: datetime | None) -> int | None:
    a, b = ensure_aware(a), ensure_aware(b)
    if a is None or b is None:
        return None
    return (b - a).days


def _duration(rows: list[tuple[str, int | None]], label: str, definition: str) -> dict:
    med, included = _median([d for _, d in rows])
    excluded = [vid for vid, d in rows if d is None]
    return {"label": label, "definition": definition, "median_days": med, "included": included,
            "excluded": len(excluded), "excluded_vehicle_ids": excluded,
            "unavailable_reason": None if med is not None else "no record has both endpoints recorded"}


def _required_gaps(lines: list[dict], required: list[str]) -> list[dict]:
    gaps = []
    for cat in required:
        rows = [l for l in lines if l["category"] == cat and not l["pass_through"]]
        if not rows:
            gaps.append({"category": cat, "reason": "no cost recorded"})
        elif all(l["estimate"] for l in rows):
            gaps.append({"category": cat, "reason": "estimated only"})
        elif any(l["usd_amount"] is None for l in rows):
            gaps.append({"category": cat, "reason": "no FX conversion"})
    return gaps


# ── computation ──────────────────────────────────────────────────────────────
async def compute(db: AsyncSession, *, period: dict, now: datetime | None = None) -> dict:
    """The whole Home business overview for one period. Never raises on missing data — it reports it."""
    now = ensure_aware(now) or datetime.now(timezone.utc)
    a, b = period["_a"], period["_b"]
    cohort = await fq.sold_cohort(db, a, b)
    required, required_source = await required_cost_categories(db)

    sale_rows = cohort["sales"]
    vehicle_ids = [r["vehicle_id"] for r in sale_rows]
    lines_by_vehicle = await fq.cost_lines(db, vehicle_ids) if vehicle_ids else {}
    vehicles = {v.id: v for v in (await db.execute(select(Vehicle).where(Vehicle.id.in_(vehicle_ids)))).scalars().all()} if vehicle_ids else {}
    cost_item_ids = sorted({l["cost_item_id"] for ls in lines_by_vehicle.values() for l in ls})

    # cost completeness per sold vehicle
    missing_required, per_vehicle = [], []
    for r in sale_rows:
        lines = lines_by_vehicle.get(r["vehicle_id"], [])
        gaps = _required_gaps(lines, required)
        v = vehicles.get(r["vehicle_id"])
        if gaps:
            missing_required.append({"vehicle_id": r["vehicle_id"], "stock_no": r["stock_no"],
                                     "sale_id": r["sale"]["id"], "gaps": gaps})
        per_vehicle.append({"sale_id": r["sale"]["id"], "vehicle_id": r["vehicle_id"], "stock_no": r["stock_no"],
                            "net_sale_value": r["net_sale_value_usd"] or r["net_sale_value"],
                            "net_sale_value_is_usd": r["net_sale_value_usd"] is not None,
                            "costs": r["costs_usd"], "gross_profit": r["gross_profit_usd"], "flags": r["flags"],
                            "by_category": r["by_category"], "days_to_sale": r["days_to_sale"],
                            "completed_at": r["completed_at"],
                            "cost_item_ids": sorted({l["cost_item_id"] for l in lines})})

    cf = cohort["flags"]
    sale_adjustment_gaps = [{"sale_id": r["sale"]["id"], "exceptions": r["sale"].get("exceptions") or []}
                            for r in sale_rows if r["sale"].get("exceptions")]
    blocking: list[str] = []
    if not sale_rows:
        blocking.append("no completed sales in this period")
    if missing_required:
        blocking.append(f"{len(missing_required)} sold vehicle(s) missing a required cost category "
                        f"({', '.join(required)})")
    if cf["estimated_lines"]:
        blocking.append(f"{cf['estimated_lines']} cost line(s) are still estimates or quotes")
    if cf["fx_missing_lines"]:
        blocking.append(f"{cf['fx_missing_lines']} cost line(s) have no USD conversion")
    if cf["allocations_needing_review"]:
        blocking.append(f"{cf['allocations_needing_review']} cost allocation(s) need review")
    if cf["unallocated"]:
        blocking.append(f"{len(cf['unallocated'])} sold vehicle(s) have no recorded costs")
    if cf["unknown_net"]:
        blocking.append("a sale is recorded in a currency with no USD basis")
    if sale_adjustment_gaps:
        blocking.append(f"{len(sale_adjustment_gaps)} sale(s) have open adjustments/exceptions")
    recorded = not blocking

    # ── headline values ──
    net_value = cohort["net_sales_value"]
    costs_value = cohort["costs_total"]
    gross_value = cohort["gross_profit"]
    margin = _margin(gross_value, net_value, cohort)

    durations = await _durations(db, sale_rows, vehicles)
    unsold = await fq.unsold_inventory_cost(db, now)
    projected = _projected(unsold)

    values = {
        "vehicles_sold": {"label": "Vehicles sold", "count": cohort["vehicles_sold"],
                          "definition": "distinct non-cancelled sales completed in the period"},
        "net_vehicle_sales_value": {
            "label": "Net vehicle sales value", "value": net_value, "currency": USD,
            "unavailable_reason": None if net_value is not None else
            ("sale currency has no USD basis" if cf["unknown_net"] else "no completed sales in this period"),
            "excludes": ["sales tax", "pass-through charges", "customer cash receipts"],
            "note": "agreed vehicle sale amounts for the sold cohort, less recorded sale-price credits/returns"},
        "vehicle_costs_sold_cohort": {
            "label": "Vehicle costs (sold cohort)", "value": costs_value, "currency": USD,
            "by_category": cohort["costs_by_category"], "unallocated": cf["unallocated"],
            "missing_lines": {"fx_missing": cf["fx_missing_lines"], "estimated": cf["estimated_lines"],
                              "needing_review": cf["allocations_needing_review"]},
            "note": "non-duplicated allocated costs; invoiced-but-unpaid included, invoice and its payment never both"},
        "gross_profit": {
            "label": "Recorded gross profit" if recorded else "Estimated gross profit",
            "state": "recorded" if recorded else "estimated", "value": gross_value, "currency": USD,
            "unavailable_reason": None if gross_value is not None else
            ("net vehicle sales value is unknown" if cf["unknown_net"] else "no completed sales in this period"),
            "reasons": blocking, "caveat": PROFIT_CAVEAT},
        "gross_margin": margin,
        "days_to_sale": durations["acquisition_to_sale"],
        "unsold_inventory_cost": {
            "label": "Unsold inventory cost", "value": unsold["total"], "currency": USD, "as_of": unsold["as_of"],
            "point_in_time": True, "vehicles": len(unsold["vehicles"]),
            "reserved": [r for r in unsold["vehicles"] if r["allocation"] == "reserved"],
            "coverage": {"vehicles_without_costs": unsold["flags"]["vehicles_without_costs"],
                         "fx_missing_lines": unsold["flags"]["fx_missing_lines"],
                         "estimated_lines": unsold["flags"]["estimated_lines"]},
            "note": "stock balance as of now for unsold acquired vehicles (reserved included and labelled); "
                    "never part of the selected-period sold costs"},
        "projected_gross_profit": projected,
    }
    drill = {
        "sale_ids": sorted(r["sale"]["id"] for r in sale_rows),
        "vehicle_ids": sorted(vehicle_ids),
        "cost_item_ids": cost_item_ids,
        "unsold_vehicle_ids": sorted(r["vehicle_id"] for r in unsold["vehicles"]),
        "projected_vehicle_ids": projected["vehicle_ids"],
        "per_vehicle": per_vehicle,
        "days_to_sale_vehicle_ids": durations["acquisition_to_sale"]["excluded_vehicle_ids"],
    }
    completeness = {
        "label": "Recorded" if recorded else "Estimated",
        "recorded": recorded,
        "required_cost_categories": required,
        "required_source": required_source,
        "missing_required": missing_required,
        "sale_adjustment_gaps": sale_adjustment_gaps,
        "reasons": blocking,
        "counts": {"sales": len(sale_rows), "cost_items": len(cost_item_ids),
                   "estimated_lines": cf["estimated_lines"], "fx_missing_lines": cf["fx_missing_lines"],
                   "allocations_needing_review": cf["allocations_needing_review"],
                   "vehicles_without_costs": len(cf["unallocated"])},
    }
    restatements = await _restatements(db, cohort, cost_item_ids)
    return {
        "period": {k: v for k, v in period.items() if not k.startswith("_")},
        "currency": USD, "as_of": now.isoformat(), "computation_version": COMPUTATION_VERSION,
        "stale": False, "served_from": "computed", "money_hidden": False,
        "cohort": {"definition": COHORT_DEFINITION, "basis": "finance_queries.sold_cohort",
                   "sale_ids": drill["sale_ids"], "vehicle_ids": drill["vehicle_ids"],
                   "excluded": cf["excluded"], "sale_currencies": cf["sale_currencies"]},
        "values": values,
        "completeness": completeness,
        "contributing_ids": drill,
        "restatements": restatements,
        "cash_flows": await cash_flows(db, a, b),
        "sales": per_vehicle,
        "flags": {**cf, "required_cost_categories": required},
        "model_used": False,
    }


def _margin(gross: dict | None, net: dict | None, cohort: dict) -> dict:
    out = {"label": "Gross margin", "available": False, "value": None, "numerator": gross, "denominator": net,
           "basis": "weighted from period totals, not an average of per-vehicle percentages", "unavailable_reason": None}
    if gross is None or net is None:
        out["unavailable_reason"] = cohort["gross_margin_note"] or "net vehicle sales value is unknown"
        return out
    denom = Decimal(net["amount"])
    if denom == 0:
        out["unavailable_reason"] = "net vehicle sales value is zero"
        return out
    out["available"] = True
    out["value"] = str((Decimal(gross["amount"]) / denom).quantize(Decimal("0.0001")))
    out["percent"] = str((Decimal(gross["amount"]) / denom * 100).quantize(Decimal("0.01")))
    return out


async def _durations(db: AsyncSession, sale_rows: list[dict], vehicles: dict) -> dict:
    """Days to sale plus the two drill-down turnarounds, each with its own definition and excluded count."""
    acq, r2r, l2s = [], [], []
    for r in sale_rows:
        vid = r["vehicle_id"]
        v = vehicles.get(vid)
        completed = ensure_aware(datetime.fromisoformat(r["completed_at"])) if r["completed_at"] else None
        acq.append((vid, r["days_to_sale"]))
        r2r.append((vid, _days(v.received_at if v else None, v.ready_at if v else None)))
        l2s.append((vid, _days(v.listed_at if v else None, completed)))
    return {
        "acquisition_to_sale": {**_duration(acq, "Days to sale",
                                            "median calendar days from recorded acquisition to completed sale"),
                                "median_days_finance": None},
        "received_to_ready": _duration(r2r, "Received to ready",
                                       "median calendar days from received at the shop to ready for sale"),
        "listed_to_sold": _duration(l2s, "Listed to sold",
                                    "median calendar days from listed to completed sale"),
    }


def _projected(unsold: dict) -> dict:
    """Approved asking price − expected total cost for eligible unsold stock. Coverage disclosed; never mixed
    into recorded profit and never counting deposits."""
    eligible, unknown, total = [], [], ZERO
    for row in unsold["vehicles"]:
        price = row.get("asking_price")
        if price is None:
            unknown.append({"vehicle_id": row["vehicle_id"], "stock_no": row["stock_no"], "reason": "no approved asking price"})
            continue
        if price["currency"] != USD:
            unknown.append({"vehicle_id": row["vehicle_id"], "stock_no": row["stock_no"],
                            "reason": f"asking price in {price['currency']}; no USD basis"})
            continue
        if row["flags"]["fx_missing_lines"]:
            unknown.append({"vehicle_id": row["vehicle_id"], "stock_no": row["stock_no"], "reason": "cost line without USD conversion"})
            continue
        total += Decimal(price["amount"]) - Decimal(row["cost_usd"]["amount"])
        eligible.append(row["vehicle_id"])
    for stock in unsold["flags"]["vehicles_without_costs"]:
        unknown.append({"vehicle_id": None, "stock_no": stock, "reason": "no costs recorded"})
    covered = len(eligible)
    considered = covered + len(unknown)
    return {
        "label": "Projected gross profit (estimate)", "state": "projected",
        "value": money(quantize(total, USD), USD) if eligible else None, "currency": USD,
        "vehicle_ids": sorted(eligible),
        "coverage": {"eligible_vehicles": covered, "considered": considered,
                     "ratio": (f"{covered}/{considered}" if considered else "0/0")},
        "unknown_components": unknown,
        "unavailable_reason": None if eligible else "no unsold vehicle has both an approved asking price and a USD cost basis",
        "note": "estimate for unsold stock; never added to recorded sold profit and never counts deposits",
    }


async def _restatements(db: AsyncSession, cohort: dict, cost_item_ids: list[str]) -> list[dict]:
    """Refunds/credits and late cost corrections restate the ORIGINATING cohort with a visible note (K03)."""
    out = [{"source": "sale", **r} for r in cohort["restatements"]]
    if cost_item_ids:
        items = (await db.execute(select(CostItem).where(CostItem.id.in_(cost_item_ids)))).scalars().all()
        for it in items:
            for entry in (it.restatements or []):
                out.append({"source": "cost_item", "cost_item_id": it.id, "vehicle_id": it.vehicle_id,
                            "category": it.category, **entry})
    out.sort(key=lambda r: str(r.get("at") or ""))
    return out


async def cash_flows(db: AsyncSession, a: datetime, b: datetime) -> dict:
    """Cash is a separate measure from profit: payments and refunds use their own dates (spec §2.4, K03)."""
    rows = (await db.execute(select(Payment).where(Payment.occurred_at.is_not(None), Payment.occurred_at >= a,
                                                   Payment.occurred_at < b))).scalars().all()
    receipts, payouts, unknown_currency, receipt_ids, payout_ids = ZERO, ZERO, [], [], []
    for p in rows:
        if p.currency != USD:
            unknown_currency.append({"payment_id": p.id, "currency": p.currency, "amount": str(p.amount)})
            continue
        if p.is_payout:
            payouts += p.amount or ZERO
            payout_ids.append(p.id)
        else:
            receipts += p.amount or ZERO
            receipt_ids.append(p.id)
    # refunds carry their own date; a refund recorded later lands in the period that contains it
    refunds, refund_rows = ZERO, []
    all_payments = (await db.execute(select(Payment).where(Payment.refunded_amount > 0))).scalars().all()
    for p in all_payments:
        for r in (p.refunds or []):
            at = ensure_aware(_parse(r.get("at"))) or ensure_aware(p.occurred_at)
            if at is None or not (a <= at < b):
                continue
            if (r.get("currency") or p.currency) != USD:
                unknown_currency.append({"payment_id": p.id, "currency": r.get("currency") or p.currency,
                                         "amount": str(r.get("amount"))})
                continue
            amt = Decimal(str(r.get("amount") or "0"))
            refunds += amt
            refund_rows.append({"payment_id": p.id, "refund_id": r.get("id"), "at": at.isoformat(),
                                "amount": money(amt, USD)})
    return {
        "label": "Cash flows", "basis": "payment and refund dates (occurred_at), not the sale cohort",
        "period": {"from": a.isoformat(), "to": b.isoformat()}, "currency": USD,
        "receipts": money(quantize(receipts, USD), USD), "refunds": money(quantize(refunds, USD), USD),
        "payouts": money(quantize(payouts, USD), USD),
        "counts": {"receipts": len(receipt_ids), "refunds": len(refund_rows), "payouts": len(payout_ids)},
        "payment_ids": sorted(receipt_ids), "payout_ids": sorted(payout_ids), "refund_rows": refund_rows,
        "unknown_currency": unknown_currency,
        "note": "cash movement is never profit; payouts and deposits are excluded from the sold cohort",
    }


def _parse(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# ── snapshot cache ───────────────────────────────────────────────────────────
def cohort_hash(period: dict) -> str:
    return stable_hash({"from": period["from"], "to": period["to"], "tz": period["timezone"],
                        "definition": COHORT_DEFINITION, "v": COMPUTATION_VERSION})


def snapshot_key(period: dict) -> str:
    return f"{SNAPSHOT_PREFIX}:{period['kind']}:{period['timezone']}:{cohort_hash(period)}"


async def _snapshot(db: AsyncSession, key: str) -> MetricSnapshot | None:
    return (await db.execute(select(MetricSnapshot).where(MetricSnapshot.key == key)
                             .order_by(MetricSnapshot.as_of.desc()))).scalars().first()


async def store_snapshot(db: AsyncSession, period: dict, payload: dict, *, commit: bool = True) -> MetricSnapshot:
    """Write the rebuildable cache row. Carries no business fact — it can be dropped and recomputed."""
    key = snapshot_key(period)
    row = await _snapshot(db, key)
    if row is None:
        row = MetricSnapshot(key=key)
        db.add(row)
    row.period_kind = period["kind"]
    row.cohort_hash = cohort_hash(period)
    row.period_from, row.period_to = period["_a"], period["_b"]
    row.timezone = period["timezone"]
    row.cohort = payload["cohort"]
    row.values = payload
    row.contributing_ids = payload["contributing_ids"]
    row.coverage = payload["completeness"]
    row.restatements = payload["restatements"]
    row.computation_version = COMPUTATION_VERSION
    row.as_of = ensure_aware(datetime.fromisoformat(payload["as_of"]))
    row.stale = False
    row.invalidated_at = None
    row.last_error = None
    await db.flush()
    if commit:
        await db.commit()
    return row


async def mark_stale(db: AsyncSession, reason: str, *, commit: bool = False) -> int:
    """Canonical change → every cached period is stale; recompute happens lazily on the next read."""
    res = await db.execute(update(MetricSnapshot).where(MetricSnapshot.stale.is_(False))
                           .values(stale=True, invalidated_at=datetime.now(timezone.utc), last_error=reason[:200]))
    if commit:
        await db.commit()
    return res.rowcount or 0


for _pattern in INVALIDATING_EVENTS:
    @on_event(_pattern)
    async def _invalidate_metrics(db: AsyncSession, ev, _p=_pattern) -> None:  # noqa: ANN001
        await mark_stale(db, f"{ev.type} {ev.aggregate_id or ''}".strip())


@sweep("reporting.refresh_metrics", 300)
async def refresh_stale_snapshots(session_factory) -> dict:
    """Recompute snapshots that events marked stale. Never creates one on its own — reads do that."""
    refreshed, failed = 0, 0
    async with session_factory() as db:
        rows = (await db.execute(select(MetricSnapshot).where(MetricSnapshot.stale.is_(True)).limit(20))).scalars().all()
        for row in rows:
            try:
                period = {"kind": row.period_kind or "month", "from": row.period_from.isoformat(),
                          "to": row.period_to.isoformat(), "timezone": row.timezone,
                          "label": _period_label(row.period_kind or "month", row.period_from, row.period_to, row.timezone),
                          "_a": ensure_aware(row.period_from), "_b": ensure_aware(row.period_to)}
                payload = await compute(db, period=period)
                await store_snapshot(db, period, payload, commit=False)
                refreshed += 1
            except Exception as e:  # noqa: BLE001
                log.warning("metric snapshot %s refresh failed: %s", row.key, e)
                row.last_error = f"{type(e).__name__}: {e}"[:200]
                failed += 1
        await db.commit()
    return {"refreshed": refreshed, "failed": failed}


# ── role safety ──────────────────────────────────────────────────────────────
def _deny_reason(actor: Actor) -> str | None:
    if can_see_costs(actor):
        return None
    if can_see_finance_status(actor):
        return None
    return "financial reporting requires costs.read (amounts) or finance.status (counts and timing)"


def can_read_metrics(actor: Actor) -> bool:
    return _deny_reason(actor) is None


def _strip_money(obj):
    """Recursively drop {"amount","currency"} money objects for finance.status-only actors."""
    if isinstance(obj, dict):
        if set(obj.keys()) == {"amount", "currency"}:
            return None
        return {k: _strip_money(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_money(v) for v in obj]
    return obj


def sanitize(actor: Actor, payload: dict) -> dict:
    """Owner / costs.read: everything. finance.status only: counts, timing and record cohorts — no amounts."""
    if can_see_costs(actor):
        return payload
    out = _strip_money(payload)
    out["money_hidden"] = True
    out["values"]["vehicle_costs_sold_cohort"]["by_category"] = {
        k: {kk: vv for kk, vv in v.items() if kk != "usd"} for k, v in (payload["values"]["vehicle_costs_sold_cohort"]["by_category"] or {}).items()}
    out["values"]["gross_margin"]["value"] = None
    out["values"]["gross_margin"]["percent"] = None
    out["values"]["gross_margin"]["available"] = False
    out["values"]["gross_margin"]["unavailable_reason"] = "amounts hidden (costs.read required)"
    out["cash_flows"] = {"label": "Cash flows", "money_hidden": True,
                         "counts": payload["cash_flows"]["counts"], "period": payload["cash_flows"]["period"]}
    out["restatements"] = [{k: v for k, v in r.items() if k not in ("old", "new", "amount")} for r in payload["restatements"]]
    out["visible"] = "counts and timing only"
    return out


# ── public read API ──────────────────────────────────────────────────────────
async def metrics(db: AsyncSession, actor: Actor, period: str = "month", start=None, end=None,
                  tz: str = PHOENIX, *, now: datetime | None = None, use_cache: bool = True,
                  store: bool = True, refresh: bool = False) -> dict:
    """Home business overview for `period`. Deterministic; no model call anywhere in this path."""
    reason = _deny_reason(actor)
    if reason:
        raise Denied(reason)
    # a business-wide aggregate cannot be filtered down to one person's assigned records without lying about
    # the cohort, so a record-limited actor is refused rather than shown a partial total (spec §2.4 role safety)
    if await visible_vehicle_ids(db, actor) is not None:
        raise Denied("business-wide metrics need unrestricted vehicle access (vehicles.all); "
                     "open the vehicles and cost records you are assigned to instead")
    p = resolve_period(period, tz, start, end, ref=now)
    key = snapshot_key(p)
    row = await _snapshot(db, key) if use_cache else None
    if row is not None and not row.stale and not refresh and (row.computation_version or 0) == COMPUTATION_VERSION:
        payload = dict(row.values or {})
        if payload:
            payload["served_from"] = "snapshot"
            payload["stale"] = False
            payload["as_of"] = row.as_of.isoformat() if row.as_of else payload.get("as_of")
            return sanitize(actor, payload)
    try:
        payload = await compute(db, period=p, now=now)
    except Exception as e:  # noqa: BLE001
        log.exception("metrics recompute failed for %s", key)
        if row is not None and row.values:
            stale = dict(row.values)
            stale.update({"stale": True, "served_from": "stale_snapshot",
                          "as_of": row.as_of.isoformat() if row.as_of else stale.get("as_of"),
                          "stale_reason": f"recompute failed: {type(e).__name__}: {e}"[:200],
                          "recomputing": True})
            return sanitize(actor, stale)
        raise
    if store:
        try:
            await store_snapshot(db, p, payload)
        except Exception:  # noqa: BLE001
            await db.rollback()
            log.warning("could not cache metric snapshot %s", key)
    return sanitize(actor, payload)


DRILLDOWN_METRICS = ("vehicles_sold", "net_vehicle_sales_value", "vehicle_costs_sold_cohort", "gross_profit",
                     "gross_margin", "days_to_sale", "unsold_inventory_cost", "projected_gross_profit", "cash_flows")


async def drilldown(db: AsyncSession, actor: Actor, metric: str, period: str = "month", start=None, end=None,
                    tz: str = PHOENIX, *, now: datetime | None = None) -> dict:
    """Contributing record ids so Costs / Profit open Finance with exactly the same cohort and filters (K01)."""
    if metric not in DRILLDOWN_METRICS:
        raise ValidationFailed(f"metric must be one of {DRILLDOWN_METRICS}")
    out = await metrics(db, actor, period, start, end, tz, now=now)
    ids = out["contributing_ids"]
    p = out["period"]
    finance_filters = {"path": "/api/finance/sold-cohort", "period": p["kind"], "from": p["from"], "to": p["to"],
                       "tz": p["timezone"], "cohort": COHORT_DEFINITION}
    body = {"metric": metric, "period": p, "currency": USD, "as_of": out["as_of"], "stale": out.get("stale", False),
            "money_hidden": out.get("money_hidden", False), "finance_filters": finance_filters,
            "cohort": out["cohort"], "completeness": out["completeness"]}
    if metric in ("vehicles_sold", "net_vehicle_sales_value", "gross_profit", "gross_margin"):
        body.update({"sale_ids": ids["sale_ids"], "vehicle_ids": ids["vehicle_ids"], "rows": out["sales"]})
    if metric in ("vehicle_costs_sold_cohort", "gross_profit", "gross_margin"):
        body.update({"cost_item_ids": ids["cost_item_ids"], "vehicle_ids": ids["vehicle_ids"],
                     "by_category": out["values"]["vehicle_costs_sold_cohort"]["by_category"],
                     "unallocated": out["values"]["vehicle_costs_sold_cohort"]["unallocated"]})
    if metric == "days_to_sale":
        body.update({"vehicle_ids": ids["vehicle_ids"], "rows": out["sales"],
                     "durations": {"acquisition_to_sale": out["values"]["days_to_sale"]},
                     "excluded_vehicle_ids": ids["days_to_sale_vehicle_ids"]})
    if metric == "unsold_inventory_cost":
        body.update({"vehicle_ids": ids["unsold_vehicle_ids"], "as_of": out["values"]["unsold_inventory_cost"]["as_of"],
                     "finance_filters": {"path": "/api/finance/unsold-inventory", "as_of": out["values"]["unsold_inventory_cost"]["as_of"]}})
    if metric == "projected_gross_profit":
        body.update({"vehicle_ids": ids["projected_vehicle_ids"],
                     "unknown_components": out["values"]["projected_gross_profit"]["unknown_components"]})
    if metric == "cash_flows":
        body.update({"cash_flows": out["cash_flows"],
                     "finance_filters": {"path": "/api/finance/payments", "from": p["from"], "to": p["to"]}})
    body["restatements"] = out["restatements"]
    return body


async def drilldown_all(db: AsyncSession, actor: Actor, **kw) -> dict:
    return {m: await drilldown(db, actor, m, **kw) for m in DRILLDOWN_METRICS}


# ── the one write surface: rebuild the cache (no business fact changes) ──────
class RecomputeIn(BaseModel):
    period: str = "month"
    start: date | None = None
    end: date | None = None
    timezone: str = PHOENIX
    reason: str | None = None


@command("reporting.recompute", input=RecomputeIn, perm="finance.status", action_class="internal",
         description="Recompute and cache the Home metrics snapshot for a period after canonical cost/sale/intake/"
                     "milestone changes. Reads canonical records only; changes no business fact (spec §12.1).")
async def reporting_recompute(ctx: CommandContext, inp: RecomputeIn) -> dict:
    p = resolve_period(inp.period, inp.timezone, inp.start, inp.end, ref=ctx.now)
    payload = await compute(ctx.db, period=p, now=ctx.now)
    row = await store_snapshot(ctx.db, p, payload, commit=False)
    ctx.record(f"Home metrics recomputed for {p['label']}", entity_kind="metric_snapshot", entity_id=row.id,
               kind="system", state="recomputed", visibility="finance",
               details={"key": row.key, "reason": inp.reason, "sales": payload["values"]["vehicles_sold"]["count"]})
    return {"snapshot": {"id": row.id, "key": row.key, "as_of": payload["as_of"], "stale": False},
            "period": payload["period"], "completeness": payload["completeness"]}
