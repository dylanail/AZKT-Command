"""Home business metrics (spec §2.2, §2.4, invariant 15).

K01 sold cohort totals and drill-down equality with Finance; K02 estimates / duplicates / missing FX / negative
profit / zero denominator; K03 period changes, late cost correction, refund on the payment-date view; plus the
snapshot cache (stale served with as_of) and the deterministic, model-free guarantee used by K05.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from backend.app.core.errors import Denied
from backend.app.core.time import PHOENIX
from backend.app.domain.commands import dispatch
from backend.app.models.finance import CostItem, Sale
from backend.app.models.contacts import Contact
from backend.app.models.reporting import MetricSnapshot
from backend.app.models.vehicles import Vehicle
from backend.app.services import finance_queries as fq
from backend.app.services import reporting as rep
from backend.tests.conftest import actor_of, ctx_for, make_user, run_worker_once

D = Decimal
USD = "USD"


@pytest_asyncio.fixture(autouse=True, scope="module")
async def _drain_outbox_after_module():
    yield
    for _ in range(50):
        out = await run_worker_once()
        if not out.get("events") and not out.get("jobs"):
            break


def _u() -> str:
    return uuid.uuid4().hex[:8]


async def cmd(db, user, name: str, payload: dict, **kw):
    return await dispatch(ctx_for(db, user, **kw), name, payload)


async def contact(db) -> Contact:
    name = f"Buyer {_u()}"
    c = Contact(name=name, roles=["buyer"], status="active", search_text=name.lower(), primary_email=f"{_u()}@example.com")
    db.add(c)
    await db.commit()
    return c


async def vehicle(db, **kw) -> Vehicle:
    stock = kw.pop("stock_no", f"RPT-{_u()[:5].upper()}")
    v = Vehicle(stock_no=stock, title=stock, make="Daihatsu", model="Hijet", model_year=2001, **kw)
    db.add(v)
    await db.commit()
    await db.refresh(v)
    return v


async def item(db, owner, **kw) -> dict:
    return (await cmd(db, owner, "costs.create_item", kw)).data["cost_item"]


async def sell(db, owner, v: Vehicle, c: Contact, price: str, completed_at: datetime, **kw) -> dict:
    s = (await cmd(db, owner, "sales.reserve", {"vehicle_id": v.id, "buyer_contact_id": c.id})).data["sale"]
    await cmd(db, owner, "sales.agree", {"sale_id": s["id"], "price": price, "currency": kw.pop("currency", USD),
                                         "sales_tax": kw.pop("sales_tax", "700.00")})
    return (await cmd(db, owner, "sales.mark_completed", {"sale_id": s["id"], "completed_at": completed_at.isoformat(),
                                                          "source_ref": f"bill-of-sale-{_u()}"})).data["sale"]


def period(y: int, m: int, last_day: int) -> dict:
    return {"period": "custom", "start": date(y, m, 1), "end": date(y, m, last_day)}


async def metrics(db, user, y: int, m: int, last_day: int, **kw):
    p = period(y, m, last_day)
    return await rep.metrics(db, actor_of(user), p["period"], p["start"], p["end"], PHOENIX,
                             use_cache=kw.pop("use_cache", False), store=kw.pop("store", False), **kw)


# ── K01 ──────────────────────────────────────────────────────────────────────
_K01: dict = {}


@pytest_asyncio.fixture
async def k01(db, owner):
    """Two completed sales (10,000 / 12,000) with complete allocated costs (7,000 / 9,000), plus a deposit,
    an unsold vehicle with costs and a bank payout — none of which may reach recorded profit. Built once."""
    if _K01:
        return _K01
    c = await contact(db)
    tag = _u()
    when = datetime(2019, 5, 10, 17, 0, tzinfo=timezone.utc)
    v1 = await vehicle(db, acquired_at=datetime(2019, 3, 1, tzinfo=timezone.utc), received_at=datetime(2019, 4, 1, tzinfo=timezone.utc),
                       ready_at=datetime(2019, 4, 11, tzinfo=timezone.utc), listed_at=datetime(2019, 4, 20, tzinfo=timezone.utc))
    v2 = await vehicle(db, acquired_at=datetime(2019, 4, 1, tzinfo=timezone.utc), received_at=datetime(2019, 4, 20, tzinfo=timezone.utc),
                       ready_at=datetime(2019, 5, 2, tzinfo=timezone.utc), listed_at=datetime(2019, 5, 5, tzinfo=timezone.utc))
    v3 = await vehicle(db, acquired_at=datetime(2019, 4, 15, tzinfo=timezone.utc))          # unsold stock
    await item(db, owner, category="purchase", vehicle_id=v1.id, vendor_name=f"Auction {tag}", amount_invoiced="4000.00")
    await item(db, owner, category="recon", vehicle_id=v1.id, vendor_name=f"Shop {tag}", amount_invoiced="1000.00")
    await item(db, owner, category="purchase", vehicle_id=v2.id, vendor_name=f"Auction {tag}", amount_invoiced="6000.00")
    await item(db, owner, category="recon", vehicle_id=v2.id, vendor_name=f"Shop {tag}", amount_invoiced="1000.00")
    shared = await item(db, owner, category="import", vendor_name=f"Forwarder {tag}", amount_invoiced="4000.00")
    await cmd(db, owner, "costs.allocate", {"cost_item_id": shared["id"], "basis": "explicit",
                                            "lines": [{"vehicle_id": v1.id, "amount": "2000.00"},
                                                      {"vehicle_id": v2.id, "amount": "2000.00"}]})
    await item(db, owner, category="purchase", vehicle_id=v3.id, vendor_name=f"Auction {tag}", amount_invoiced="5000.00")
    await item(db, owner, category="recon", vehicle_id=v3.id, vendor_name=f"Shop {tag}", amount_estimated="800.00")
    s1 = await sell(db, owner, v1, c, "10000.00", when)
    s2 = await sell(db, owner, v2, c, "12000.00", when + timedelta(days=5))
    # a deposit (cash, not profit) and a bank payout (not revenue) inside the same period
    inv = (await cmd(db, owner, "invoices.create", {"kind": "deposit", "sale_id": s1["id"], "contact_id": c.id,
                                                    "amount_due": "500.00", "currency": USD})).data["invoice"]
    pay = (await cmd(db, owner, "payments.upsert_provider", {"provider": "square", "merchant_id": f"M{tag}",
                                                             "provider_payment_id": f"dep-{tag}", "provider_event_id": f"e1-{tag}",
                                                             "amount": "500.00", "currency": USD, "status": "COMPLETED",
                                                             "occurred_at": when.isoformat()})).data["payment"]
    prop = await cmd(db, owner, "payments.propose_allocation", {"payment_id": pay["id"], "invoice_id": inv["id"]})
    alloc = prop.data["allocations"][0]
    await cmd(db, owner, "payments.confirm_allocation", {"allocation_id": alloc["id"], "expected_version": alloc["version"]})
    await cmd(db, owner, "payments.upsert_provider", {"provider": "square", "merchant_id": f"M{tag}",
                                                      "provider_payment_id": f"payout-{tag}", "provider_event_id": f"e2-{tag}",
                                                      "amount": "9000.00", "currency": USD, "status": "COMPLETED",
                                                      "is_payout": True, "occurred_at": when.isoformat()})
    await cmd(db, owner, "vehicles.set_asking_price", {"vehicle_id": v3.id, "amount": "9500.00", "currency": USD,
                                                       "reason": "approved for listing"})
    _K01.update({"v1": v1.id, "v2": v2.id, "v3": v3.id, "s1": s1["id"], "s2": s2["id"], "payment": pay["id"]})
    return _K01


async def test_K01_home_metrics_match_the_finance_sold_cohort(db, owner, k01):
    m = await metrics(db, owner, 2019, 5, 31)
    v = m["values"]
    assert v["vehicles_sold"]["count"] == 2
    assert v["vehicle_costs_sold_cohort"]["value"] == {"amount": "16000.00", "currency": USD}
    assert v["net_vehicle_sales_value"]["value"] == {"amount": "22000.00", "currency": USD}
    assert v["gross_profit"]["value"] == {"amount": "6000.00", "currency": USD}
    assert v["gross_profit"]["state"] == "recorded" and v["gross_profit"]["label"] == "Recorded gross profit"
    assert m["completeness"]["recorded"] is True and not m["completeness"]["reasons"]
    # weighted aggregate margin 6000 / 22000, never an average of per-vehicle percentages
    assert v["gross_margin"]["available"] is True
    assert v["gross_margin"]["value"] == str((D("6000") / D("22000")).quantize(D("0.0001")))
    assert v["gross_margin"]["numerator"] == {"amount": "6000.00", "currency": USD}
    assert v["gross_margin"]["denominator"] == {"amount": "22000.00", "currency": USD}
    # deposits, payouts and unsold estimates are excluded from the cohort
    assert set(m["cohort"]["sale_ids"]) == {k01["s1"], k01["s2"]}
    assert k01["v3"] not in m["cohort"]["vehicle_ids"]
    assert m["cash_flows"]["receipts"] == {"amount": "500.00", "currency": USD}
    assert m["cash_flows"]["payouts"] == {"amount": "9000.00", "currency": USD}
    # category breakdown from the same non-duplicated basis
    cats = v["vehicle_costs_sold_cohort"]["by_category"]
    assert cats["purchase"]["usd"] == {"amount": "10000.00", "currency": USD}
    assert cats["import"]["usd"] == {"amount": "4000.00", "currency": USD}
    assert cats["recon"]["usd"] == {"amount": "2000.00", "currency": USD}
    # days to sale = median(70, 44) with the two drill-down turnarounds
    assert v["days_to_sale"]["median_days"] == (70 + 44) / 2 and v["days_to_sale"]["included"] == 2
    assert v["days_to_sale"]["excluded"] == 0
    # unsold inventory is a separate point-in-time balance that includes the estimate
    row = next(r for r in (await fq.unsold_inventory_cost(db))["vehicles"] if r["vehicle_id"] == k01["v3"])
    assert row["cost_usd"] == {"amount": "5800.00", "currency": USD}
    assert k01["v3"] in m["contributing_ids"]["unsold_vehicle_ids"]
    assert all(r["label"] in ("Inventory", "Reserved") for r in (await fq.unsold_inventory_cost(db))["vehicles"])
    # the projection is an estimate with disclosed coverage, never folded into recorded profit
    pr = v["projected_gross_profit"]
    assert k01["v3"] in pr["vehicle_ids"] and pr["state"] == "projected"     # 9500 asking − 5800 cost
    assert pr["coverage"]["eligible_vehicles"] >= 1
    assert pr["coverage"]["considered"] == pr["coverage"]["eligible_vehicles"] + len(pr["unknown_components"])
    assert all(u.get("reason") for u in pr["unknown_components"])
    assert v["gross_profit"]["value"] == {"amount": "6000.00", "currency": USD}   # projection not mixed in
    assert m["model_used"] is False


async def test_K01_drilldown_ids_equal_finance_sold_cohort(db, owner, k01):
    p = rep.resolve_period("custom", PHOENIX, date(2019, 5, 1), date(2019, 5, 31))
    cohort = await fq.sold_cohort(db, p["_a"], p["_b"])
    dd = await rep.drilldown(db, actor_of(owner), "gross_profit", "custom", date(2019, 5, 1), date(2019, 5, 31), PHOENIX)
    assert dd["sale_ids"] == sorted(r["sale"]["id"] for r in cohort["sales"])
    assert dd["vehicle_ids"] == sorted(r["vehicle_id"] for r in cohort["sales"])
    lines = await fq.cost_lines(db, dd["vehicle_ids"])
    assert dd["cost_item_ids"] == sorted({l["cost_item_id"] for ls in lines.values() for l in ls})
    # the same filters open Finance on exactly this cohort
    assert dd["finance_filters"]["path"] == "/api/finance/sold-cohort"
    assert dd["finance_filters"]["from"] == p["from"] and dd["finance_filters"]["to"] == p["to"]
    # and the totals agree with Finance line for line
    m = await metrics(db, owner, 2019, 5, 31)
    assert m["values"]["vehicle_costs_sold_cohort"]["value"] == cohort["costs_total"]
    assert m["values"]["gross_profit"]["value"] == cohort["gross_profit"]


# ── K02 ──────────────────────────────────────────────────────────────────────
async def test_K02_estimated_cost_labels_the_profit_estimated_with_coverage(db, owner):
    c = await contact(db)
    v = await vehicle(db, acquired_at=datetime(2019, 6, 1, tzinfo=timezone.utc))
    tag = _u()
    await item(db, owner, category="purchase", vehicle_id=v.id, vendor_name=f"Auction {tag}", amount_invoiced="5000.00")
    await item(db, owner, category="import", vehicle_id=v.id, vendor_name=f"Forwarder {tag}", amount_estimated="900.00")
    await sell(db, owner, v, c, "9000.00", datetime(2019, 6, 12, tzinfo=timezone.utc))
    m = await metrics(db, owner, 2019, 6, 30)
    g = m["values"]["gross_profit"]
    assert g["state"] == "estimated" and g["label"] == "Estimated gross profit"
    assert g["value"] == {"amount": "3100.00", "currency": USD}          # still a number, never hidden or zeroed
    assert m["completeness"]["recorded"] is False
    assert any("estimates or quotes" in r for r in m["completeness"]["reasons"])
    # the configurable required categories are disclosed with the gap
    assert m["completeness"]["required_cost_categories"] == list(rep.DEFAULT_REQUIRED_CATEGORIES)
    assert m["completeness"]["required_source"] == "default"
    gaps = {g_["category"]: g_["reason"] for row in m["completeness"]["missing_required"] for g_ in row["gaps"]}
    assert gaps["recon"] == "no cost recorded" and gaps["import"] == "estimated only"


async def test_K02_invoice_and_receipt_for_one_expense_are_not_counted_twice(db, owner):
    c = await contact(db)
    v = await vehicle(db, acquired_at=datetime(2019, 7, 1, tzinfo=timezone.utc))
    tag = _u()
    vendor = f"Kei Parts {tag}"
    inv_no = f"INV-{tag}"
    it = await item(db, owner, category="parts", vehicle_id=v.id, vendor_name=vendor, amount_invoiced="500.00",
                    invoice_ref=inv_no)
    for kind, ref in (("email_invoice", f"gmail:{tag}"), ("receipt_photo", f"asset:{tag}"), ("ledger_row", f"ledger:{tag}")):
        ev = (await cmd(db, owner, "costs.record_evidence", {"kind": kind, "source_ref": ref, "source_hash": f"h-{kind}-{tag}",
                                                             "vendor": vendor, "invoice_no": inv_no, "amount": "500.00",
                                                             "currency": USD})).data["evidence"]
        assert ev["cost_item_id"] == it["id"]
    await cmd(db, owner, "costs.observe", {"cost_item_id": it["id"], "kind": "paid", "amount": "500.00", "source_ref": f"bank:{tag}"})
    await item(db, owner, category="purchase", vehicle_id=v.id, vendor_name=f"Auction {tag}", amount_invoiced="3000.00")
    await item(db, owner, category="import", vehicle_id=v.id, vendor_name=f"Forwarder {tag}", amount_invoiced="600.00")
    await item(db, owner, category="recon", vehicle_id=v.id, vendor_name=f"Shop {tag}", amount_invoiced="400.00")
    await sell(db, owner, v, c, "6000.00", datetime(2019, 7, 12, tzinfo=timezone.utc))
    m = await metrics(db, owner, 2019, 7, 31)
    # 3000 + 600 + 400 + 500 — the invoice, the receipt, the ledger row and the payment are ONE expense
    assert m["values"]["vehicle_costs_sold_cohort"]["value"] == {"amount": "4500.00", "currency": USD}
    assert m["values"]["gross_profit"]["value"] == {"amount": "1500.00", "currency": USD}
    assert m["values"]["gross_profit"]["state"] == "recorded"
    items = (await db.execute(select(CostItem).where(CostItem.vendor_name == vendor))).scalars().all()
    assert len(items) == 1 and items[0].amount_paid == D("500.00")


async def test_K02_missing_fx_stays_unknown_and_is_never_zero_filled(db, owner):
    c = await contact(db)
    v = await vehicle(db, acquired_at=datetime(2019, 8, 1, tzinfo=timezone.utc))
    tag = _u()
    await item(db, owner, category="purchase", vehicle_id=v.id, vendor_name=f"Auction {tag}", amount_invoiced="2000.00")
    await item(db, owner, category="import", vehicle_id=v.id, vendor_name=f"Forwarder {tag}", currency="JPY",
               amount_invoiced="150000")                                     # no rate, no usd_amount
    await item(db, owner, category="recon", vehicle_id=v.id, vendor_name=f"Shop {tag}", amount_invoiced="300.00")
    await sell(db, owner, v, c, "7000.00", datetime(2019, 8, 12, tzinfo=timezone.utc))
    m = await metrics(db, owner, 2019, 8, 31)
    cost = m["values"]["vehicle_costs_sold_cohort"]
    assert cost["value"] == {"amount": "2300.00", "currency": USD}            # the JPY line is NOT added as zero
    assert cost["missing_lines"]["fx_missing"] == 1
    assert m["values"]["gross_profit"]["state"] == "estimated"
    assert any("USD conversion" in r for r in m["completeness"]["reasons"])
    gaps = {g["category"]: g["reason"] for row in m["completeness"]["missing_required"] for g in row["gaps"]}
    assert gaps["import"] == "no FX conversion"


async def test_K02_negative_profit_is_retained_and_a_zero_denominator_makes_margin_unavailable(db, owner):
    c = await contact(db)
    tag = _u()
    loss = await vehicle(db, acquired_at=datetime(2019, 9, 1, tzinfo=timezone.utc))
    for cat, amt in (("purchase", "4000.00"), ("import", "700.00"), ("recon", "300.00")):
        await item(db, owner, category=cat, vehicle_id=loss.id, vendor_name=f"{cat} {tag}", amount_invoiced=amt)
    await sell(db, owner, loss, c, "1000.00", datetime(2019, 9, 12, tzinfo=timezone.utc))
    m = await metrics(db, owner, 2019, 9, 30)
    assert m["values"]["gross_profit"]["value"] == {"amount": "-4000.00", "currency": USD}   # never floored at zero
    assert m["values"]["gross_margin"]["available"] is True

    zero = await vehicle(db, acquired_at=datetime(2019, 10, 1, tzinfo=timezone.utc))
    for cat, amt in (("purchase", "500.00"), ("import", "100.00"), ("recon", "100.00")):
        await item(db, owner, category=cat, vehicle_id=zero.id, vendor_name=f"{cat} {tag}", amount_invoiced=amt)
    s = await sell(db, owner, zero, c, "500.00", datetime(2019, 10, 12, tzinfo=timezone.utc))
    await cmd(db, owner, "sales.record_credit", {"sale_id": s["id"], "amount": "500.00", "reason": "full price credit"})
    m2 = await metrics(db, owner, 2019, 10, 31)
    assert m2["values"]["net_vehicle_sales_value"]["value"] == {"amount": "0.00", "currency": USD}
    assert m2["values"]["gross_margin"]["available"] is False
    assert m2["values"]["gross_margin"]["value"] is None
    assert "zero" in m2["values"]["gross_margin"]["unavailable_reason"]


# ── K03 ──────────────────────────────────────────────────────────────────────
async def test_K03_period_change_keeps_one_cohort_and_a_late_correction_restates_it(db, owner):
    c = await contact(db)
    v = await vehicle(db, acquired_at=datetime(2019, 11, 1, tzinfo=timezone.utc))
    tag = _u()
    purchase = await item(db, owner, category="purchase", vehicle_id=v.id, vendor_name=f"Auction {tag}", amount_invoiced="4000.00")
    await item(db, owner, category="import", vehicle_id=v.id, vendor_name=f"Forwarder {tag}", amount_invoiced="800.00")
    await item(db, owner, category="recon", vehicle_id=v.id, vendor_name=f"Shop {tag}", amount_invoiced="200.00")
    s = await sell(db, owner, v, c, "9000.00", datetime(2019, 11, 12, tzinfo=timezone.utc))

    nov = await metrics(db, owner, 2019, 11, 30)
    dec = await metrics(db, owner, 2019, 12, 31)
    assert s["id"] in nov["cohort"]["sale_ids"] and s["id"] not in dec["cohort"]["sale_ids"]
    assert nov["values"]["gross_profit"]["value"] == {"amount": "4000.00", "currency": USD}
    # the cohort definition is identical whichever period is selected
    assert nov["cohort"]["definition"] == dec["cohort"]["definition"] == rep.COHORT_DEFINITION

    # late cost correction in a later month restates the ORIGINAL (November) cohort with a visible note
    await cmd(db, owner, "costs.restate", {"cost_item_id": purchase["id"], "field": "amount_invoiced",
                                           "new_value": "4500.00", "note": "auction invoice corrected"})
    nov2 = await metrics(db, owner, 2019, 11, 30)
    assert nov2["values"]["vehicle_costs_sold_cohort"]["value"] == {"amount": "5500.00", "currency": USD}
    assert nov2["values"]["gross_profit"]["value"] == {"amount": "3500.00", "currency": USD}
    notes = [r for r in nov2["restatements"] if "auction invoice corrected" in str(r.get("note"))]
    assert {r["source"] for r in notes} == {"cost_item", "sale"}            # the sale carries the cohort note too
    assert any(r["source"] == "sale" and r.get("sale_id") == s["id"] and r.get("cohort_date") for r in notes)
    dec2 = await metrics(db, owner, 2019, 12, 31)
    assert s["id"] not in dec2["cohort"]["sale_ids"]                        # the correction never moves the sale


async def test_K03_refund_uses_the_payment_date_view_while_the_credit_restates_the_sale(db, owner):
    c = await contact(db)
    v = await vehicle(db, acquired_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    tag = _u()
    for cat, amt in (("purchase", "3000.00"), ("import", "500.00"), ("recon", "500.00")):
        await item(db, owner, category=cat, vehicle_id=v.id, vendor_name=f"{cat} {tag}", amount_invoiced=amt)
    s = await sell(db, owner, v, c, "8000.00", datetime(2020, 1, 10, tzinfo=timezone.utc))
    pay = (await cmd(db, owner, "payments.upsert_provider", {"provider": "square", "merchant_id": f"M{tag}",
                                                             "provider_payment_id": f"p-{tag}", "provider_event_id": f"ev-{tag}",
                                                             "amount": "8000.00", "currency": USD, "status": "COMPLETED",
                                                             "occurred_at": datetime(2020, 1, 10, tzinfo=timezone.utc).isoformat()})).data["payment"]
    # the price credit restates the January cohort ...
    await cmd(db, owner, "sales.record_credit", {"sale_id": s["id"], "amount": "600.00", "reason": "paint touch-up credit"})
    # ... while the cash leaves the bank in February and only shows up in the payment-date view
    await cmd(db, owner, "payments.upsert_provider", {"provider": "square", "merchant_id": f"M{tag}",
                                                      "provider_payment_id": f"p-{tag}", "provider_event_id": f"ev2-{tag}",
                                                      "amount": "8000.00", "currency": USD, "status": "PARTIALLY_REFUNDED",
                                                      "occurred_at": datetime(2020, 1, 10, tzinfo=timezone.utc).isoformat(),
                                                      "refunds": [{"id": f"r-{tag}", "amount": "600.00", "currency": USD,
                                                                   "status": "COMPLETED",
                                                                   "at": datetime(2020, 2, 3, tzinfo=timezone.utc).isoformat()}]})
    jan = await metrics(db, owner, 2020, 1, 31)
    feb = await metrics(db, owner, 2020, 2, 29)
    assert jan["values"]["net_vehicle_sales_value"]["value"] == {"amount": "7400.00", "currency": USD}
    assert jan["values"]["gross_profit"]["value"] == {"amount": "3400.00", "currency": USD}
    assert any("paint touch-up credit" in str(r.get("note")) for r in jan["restatements"])
    assert jan["cash_flows"]["receipts"] == {"amount": "8000.00", "currency": USD}
    assert jan["cash_flows"]["refunds"] == {"amount": "0.00", "currency": USD}
    assert feb["cash_flows"]["refunds"] == {"amount": "600.00", "currency": USD}   # payment-date view
    assert s["id"] not in feb["cohort"]["sale_ids"]                                 # cash never moves the cohort
    assert pay["id"] in jan["cash_flows"]["payment_ids"]


# ── snapshot cache / K05 support ─────────────────────────────────────────────
async def test_stale_snapshot_is_served_with_its_as_of_instead_of_a_false_zero(db, owner, monkeypatch):
    c = await contact(db)
    v = await vehicle(db, acquired_at=datetime(2020, 3, 1, tzinfo=timezone.utc))
    tag = _u()
    for cat, amt in (("purchase", "2000.00"), ("import", "400.00"), ("recon", "100.00")):
        await item(db, owner, category=cat, vehicle_id=v.id, vendor_name=f"{cat} {tag}", amount_invoiced=amt)
    await sell(db, owner, v, c, "5000.00", datetime(2020, 3, 10, tzinfo=timezone.utc))
    a = actor_of(owner)
    fresh = await rep.metrics(db, a, "custom", date(2020, 3, 1), date(2020, 3, 31), PHOENIX)
    assert fresh["served_from"] == "computed" and fresh["stale"] is False
    p = rep.resolve_period("custom", PHOENIX, date(2020, 3, 1), date(2020, 3, 31))
    # the read wrote nothing: the cache is filled through the command (or the worker sweep), never by a GET
    assert (await db.execute(select(MetricSnapshot).where(MetricSnapshot.key == rep.snapshot_key(p)))).scalars().first() is None
    await cmd(db, owner, "reporting.recompute", {"period": "custom", "start": "2020-03-01", "end": "2020-03-31"})
    row = (await db.execute(select(MetricSnapshot).where(MetricSnapshot.key == rep.snapshot_key(p)))).scalars().first()
    assert row is not None and row.period_kind == "custom" and row.cohort_hash == rep.cohort_hash(p)

    cached = await rep.metrics(db, a, "custom", date(2020, 3, 1), date(2020, 3, 31), PHOENIX)
    assert cached["served_from"] == "snapshot" and cached["as_of"] == row.as_of.isoformat()

    await rep.mark_stale(db, "cost.changed test", commit=True)

    async def boom(*args, **kw):
        raise RuntimeError("finance read unavailable")
    monkeypatch.setattr(fq, "sold_cohort", boom)
    stale = await rep.metrics(db, a, "custom", date(2020, 3, 1), date(2020, 3, 31), PHOENIX)
    assert stale["stale"] is True and stale["served_from"] == "stale_snapshot"
    assert stale["as_of"] == row.as_of.isoformat() and "recompute failed" in stale["stale_reason"]
    assert stale["values"]["gross_profit"]["value"] == {"amount": "2500.00", "currency": USD}   # not a zero


async def test_a_metrics_read_never_writes_or_commits(db, owner):
    """Invariant 11 / ARCHITECTURE rule 10: the Home read path has no side effect. It must not create the cache
    row and must never commit whatever transaction its caller (an HTTP GET, or an agent read tool inside a
    running mission) happens to be in."""
    v = Vehicle(stock_no=f"NOC-{_u()[:5].upper()}", title="uncommitted")
    db.add(v)
    await db.flush()
    vid = v.id
    before = (await db.execute(select(MetricSnapshot))).scalars().all()
    await rep.metrics(db, actor_of(owner), "custom", date(2020, 5, 1), date(2020, 5, 31), PHOENIX)
    after = (await db.execute(select(MetricSnapshot))).scalars().all()
    assert {r.id for r in after} == {r.id for r in before}          # the read cached nothing
    await db.rollback()
    assert (await db.execute(select(Vehicle).where(Vehicle.id == vid))).scalars().first() is None


async def test_unsettled_payments_are_never_counted_as_cash(db, owner):
    """A claimed, failed or cancelled payment is not money: it is disclosed, never added to Home's cash flows."""
    tag = _u()
    when = datetime(2018, 5, 10, 12, 0, tzinfo=timezone.utc)
    for pid, status in ((f"ok-{tag}", "COMPLETED"), (f"fail-{tag}", "FAILED"), (f"void-{tag}", "CANCELED")):
        await cmd(db, owner, "payments.upsert_provider", {"provider": "square", "merchant_id": f"M{tag}",
                                                          "provider_payment_id": pid, "provider_event_id": f"e-{pid}",
                                                          "amount": "1000.00", "currency": USD, "status": status,
                                                          "occurred_at": when.isoformat()})
    p = rep.resolve_period("custom", PHOENIX, date(2018, 5, 1), date(2018, 5, 31))
    cf = await rep.cash_flows(db, p["_a"], p["_b"])
    assert cf["receipts"] == {"amount": "1000.00", "currency": USD}      # only the settled one
    assert cf["counts"]["receipts"] == 1 and cf["counts"]["unsettled_excluded"] == 2
    assert {u["status"] for u in cf["unsettled"]} == {"failed", "cancelled"}
    m = await metrics(db, owner, 2018, 5, 31)
    assert m["cash_flows"]["receipts"] == {"amount": "1000.00", "currency": USD}


async def test_canonical_events_mark_the_snapshot_stale(db, owner):
    await cmd(db, owner, "reporting.recompute", {"period": "custom", "start": "2020-04-01", "end": "2020-04-30"})
    p = rep.resolve_period("custom", PHOENIX, date(2020, 4, 1), date(2020, 4, 30))
    key = rep.snapshot_key(p)
    row = (await db.execute(select(MetricSnapshot).where(MetricSnapshot.key == key))).scalars().first()
    assert row is not None and row.stale is False
    c = await contact(db)
    v = await vehicle(db, acquired_at=datetime(2020, 4, 1, tzinfo=timezone.utc))
    await item(db, owner, category="purchase", vehicle_id=v.id, vendor_name=f"Auction {_u()}", amount_invoiced="100.00")
    await run_worker_once()          # the outbox dispatches cost.changed -> mark stale
    await db.refresh(row)
    assert row.stale is True and row.invalidated_at is not None


async def test_reporting_is_deterministic_and_never_calls_a_model(db, owner):
    import inspect
    from backend.app.routers import home as home_router
    from backend.app.services import home as home_svc
    from backend.app.services import timeline as tl
    for mod in (rep, tl, home_svc, home_router):
        src = inspect.getsource(mod)
        assert "adapters.model" not in src and "ModelClient" not in src and "transcribe" not in src
    at = datetime.now(timezone.utc)
    one = await metrics(db, owner, 2019, 5, 31, now=at)
    two = await metrics(db, owner, 2019, 5, 31, now=at)
    assert one["values"] == two["values"] and one["contributing_ids"] == two["contributing_ids"]
    assert one["model_used"] is False


async def test_role_safety_mechanic_denied_manager_gets_counts_only(db, owner, manager, mechanic):
    with pytest.raises(Denied):
        await metrics(db, mechanic, 2019, 5, 31)
    assert rep.can_read_metrics(actor_of(mechanic)) is False
    m = await metrics(db, manager, 2019, 5, 31)         # manager: finance.status, no costs.read
    assert m["money_hidden"] is True and m["visible"] == "counts and timing only"
    assert m["values"]["vehicles_sold"]["count"] == 2
    assert m["values"]["gross_profit"]["value"] is None
    assert m["values"]["vehicle_costs_sold_cohort"]["value"] is None
    assert m["values"]["gross_margin"]["available"] is False
    assert m["values"]["days_to_sale"]["median_days"] == (70 + 44) / 2   # timing stays visible
    o = await metrics(db, owner, 2019, 5, 31)
    assert o["money_hidden"] is False and o["values"]["gross_profit"]["value"] is not None


async def test_recompute_command_stores_a_snapshot_without_changing_business_facts(db, owner):
    before = (await db.execute(select(Sale))).scalars().all()
    res = await cmd(db, owner, "reporting.recompute", {"period": "custom", "start": "2019-05-01", "end": "2019-05-31",
                                                       "reason": "test"})
    assert res.status == "ok" and res.data["snapshot"]["key"].startswith("home_metrics:")
    after = (await db.execute(select(Sale))).scalars().all()
    assert {s.id: s.version for s in before} == {s.id: s.version for s in after}


async def test_record_limited_actor_never_gets_a_partial_business_total(db):
    """A person granted costs.read but limited to assigned records is refused the business-wide aggregate
    rather than shown a silently filtered total."""
    scoped = await make_user(db, f"scoped{_u()}", "mechanic", perms={"costs.read": True})
    a = actor_of(scoped)
    assert a.perms["costs.read"] is True and a.perms["vehicles.all"] is False
    with pytest.raises(Denied) as e:
        await rep.metrics(db, a, "custom", date(2019, 5, 1), date(2019, 5, 31), PHOENIX, use_cache=False, store=False)
    assert "vehicles.all" in e.value.message


async def test_days_to_sale_drilldown_carries_the_two_secondary_turnarounds(db, owner, k01):
    d = (await metrics(db, owner, 2019, 5, 31))["values"]["days_to_sale"]
    assert d["median_days"] == (70 + 44) / 2
    r2r = d["drilldown"]["received_to_ready"]
    l2s = d["drilldown"]["listed_to_sold"]
    assert r2r["median_days"] == (10 + 12) / 2 and r2r["definition"].startswith("median calendar days from received")
    assert l2s["median_days"] == (20 + 10) / 2
    dd = await rep.drilldown(db, actor_of(owner), "days_to_sale", "custom", date(2019, 5, 1), date(2019, 5, 31), PHOENIX)
    assert set(dd["durations"]) == {"acquisition_to_sale", "received_to_ready", "listed_to_sold"}


async def test_required_cost_categories_are_configurable(db, owner, k01):
    """The cost-completeness rule behind "Recorded gross profit" comes from the `reporting` settings row."""
    from backend.app.models.legacy import Setting
    row = Setting(key="reporting", value={"data": {"required_cost_categories": ["purchase", "import", "recon", "selling"]},
                                          "version": 1})
    db.add(row)
    await db.commit()
    try:
        cats, source = await rep.required_cost_categories(db)
        assert cats == ["purchase", "import", "recon", "selling"] and source == "settings"
        m = await metrics(db, owner, 2019, 5, 31)
        assert m["completeness"]["required_source"] == "settings"
        assert m["values"]["gross_profit"]["state"] == "estimated"     # no selling costs recorded
        assert m["values"]["gross_profit"]["value"] == {"amount": "6000.00", "currency": USD}   # the number is unchanged
    finally:
        await db.delete(row)
        await db.commit()
    assert (await rep.required_cost_categories(db))[1] == "default"


async def test_margin_has_the_same_shape_available_or_not(db, owner):
    """A caller reading `percent` on an unavailable margin gets an explicit null, not a missing key."""
    empty = await metrics(db, owner, 2017, 2, 28)
    m = empty["values"]["gross_margin"]
    assert m["available"] is False and m["value"] is None and m["percent"] is None
    assert m["unavailable_reason"]
    live = (await metrics(db, owner, 2019, 5, 31))["values"]["gross_margin"]
    assert set(live) >= set(m) and live["available"] is True and live["percent"]


async def test_a_snapshot_from_an_older_computation_version_is_still_sanitized_honestly(db, owner, manager, monkeypatch):
    """The stale path exists so Home answers with an old, labelled number instead of a false zero. It must
    survive a snapshot whose payload predates the current value layout rather than raising on a missing key."""
    p = rep.resolve_period("custom", PHOENIX, date(2017, 4, 1), date(2017, 4, 30))
    thin = {"period": {k: v for k, v in p.items() if not k.startswith("_")}, "currency": USD,
            "as_of": datetime(2017, 5, 1, tzinfo=timezone.utc).isoformat(), "values": {}, "completeness": {},
            "contributing_ids": {}, "cohort": {}, "restatements": [], "model_used": False}
    row = MetricSnapshot(key=rep.snapshot_key(p), period_kind="custom", cohort_hash=rep.cohort_hash(p),
                         period_from=p["_a"], period_to=p["_b"], timezone=PHOENIX, values=thin,
                         as_of=datetime(2017, 5, 1, tzinfo=timezone.utc), computation_version=0, stale=True)
    db.add(row)
    await db.commit()

    async def boom(*args, **kw):
        raise RuntimeError("finance read unavailable")
    monkeypatch.setattr(fq, "sold_cohort", boom)
    try:
        for user in (owner, manager):
            out = await rep.metrics(db, actor_of(user), "custom", date(2017, 4, 1), date(2017, 4, 30), PHOENIX)
            assert out["stale"] is True and out["served_from"] == "stale_snapshot"
            assert out["as_of"] == row.as_of.isoformat()
        assert out["money_hidden"] is True and out["cash_flows"]["money_hidden"] is True
    finally:
        await db.delete(row)
        await db.commit()
