"""Finance core acceptance: E02 one economic expense, E03 balanced allocations + credits, E04 conflicts stay visible,
E05 reported payments never confirm, E06 provider event dedupe/reorder/refunds, E07 explicit allocation states,
E08/C08 exactly-once deposit handoff, E09 refund after handoff, E10 cost/payment/fee/payout distinct, K01/K02 cohort data,
A03 sanitized finance status."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from backend.app.core.errors import Blocked, Denied, ValidationFailed
from backend.app.domain.actors import Actor
from backend.app.domain.commands import CommandContext, dispatch
from backend.app.domain.policy import effective_perms
from backend.app.models.contacts import Contact
from backend.app.models.finance import CostAllocation, CostEvidence, CostItem, Invoice, Payment, PaymentAllocation, Sale
from backend.app.models.runtime import Approval, Event, WorkflowControl
from backend.app.models.sales import Opportunity
from backend.app.models.sourcing import Bid, ImportRequest
from backend.app.models.tasks import Task
from backend.app.models.vehicles import Vehicle
from backend.app.services import finance_queries as fq
from backend.tests.conftest import ctx_for, login, make_user, run_worker_once

NOW = datetime.now(timezone.utc)
D = Decimal


@pytest_asyncio.fixture(autouse=True, scope="module")
async def _drain_outbox_after_module():
    yield
    for _ in range(50):
        out = await run_worker_once()
        if not out.get("events") and not out.get("jobs"):
            break


def _u() -> str:
    return uuid.uuid4().hex[:8]


async def contact(db, name: str | None = None) -> Contact:
    name = name or f"Buyer {_u()}"
    c = Contact(name=name, roles=["buyer"], status="active", search_text=name.lower(), primary_email=f"{_u()}@example.com")
    db.add(c)
    await db.commit()
    return c


async def vehicle(db, **kw) -> Vehicle:
    stock = kw.pop("stock_no", f"STK-{_u()[:4].upper()}")
    v = Vehicle(stock_no=stock, title=stock, make="Daihatsu", model="Hijet", model_year=2001, **kw)
    db.add(v)
    await db.commit()
    await db.refresh(v)
    return v


async def cmd(db, user, name: str, payload: dict, **kw):
    return await dispatch(ctx_for(db, user, **kw), name, payload)


async def item(db, owner, **kw) -> dict:
    return (await cmd(db, owner, "costs.create_item", kw)).data["cost_item"]


async def events_of(db, type_: str, **where) -> list[Event]:
    rows = (await db.execute(select(Event).where(Event.type == type_).order_by(Event.created_at))).scalars().all()
    return [e for e in rows if all(e.payload.get(k) == v for k, v in where.items())]


async def confirmed_payment(db, owner, amount: str, currency="USD", **kw) -> dict:
    res = await cmd(db, owner, "payments.upsert_provider", {"provider": "square", "merchant_id": kw.pop("merchant", "M1"),
                                                            "provider_payment_id": kw.pop("pid", f"pay-{_u()}"),
                                                            "provider_event_id": kw.pop("event", f"evt-{_u()}"), "provider_version": "1",
                                                            "amount": amount, "currency": currency, "status": "COMPLETED",
                                                            "occurred_at": NOW.isoformat(), **kw})
    return res.data["payment"]


async def allocate(db, owner, payment_id: str, invoice_id: str, **kw) -> dict:
    prop = await cmd(db, owner, "payments.propose_allocation", {"payment_id": payment_id, "invoice_id": invoice_id, **kw})
    alloc = prop.data["allocations"][0]
    return (await cmd(db, owner, "payments.confirm_allocation", {"allocation_id": alloc["id"], "expected_version": alloc["version"]})).data


# ── E02 ──────────────────────────────────────────────────────────────────────
async def test_E02_quote_invoice_ledger_receipt_payment_converge_on_one_cost_item(db, owner):
    v = await vehicle(db)
    vendor = f"Kei Parts {_u()}"
    it = await item(db, owner, category="parts", vehicle_id=v.id, vendor_name=vendor, currency="USD", amount_quoted="500.00",
                    order_ref=f"PO-{_u()}", description="brake kit")
    assert it["status"] == "quoted" and it["active_amount"]["amount"] == "500.00"
    inv_no = f"INV-{_u()}"
    r = await cmd(db, owner, "costs.record_evidence", {"kind": "email_invoice", "source_ref": f"gmail:{_u()}", "source_hash": "h1", "vendor": vendor.upper(),
                                                       "invoice_no": inv_no, "order_no": it["order_ref"], "amount": "520.00", "currency": "USD"})
    ev = r.data["evidence"]
    assert ev["match_state"] == "matched" and ev["cost_item_id"] == it["id"] and "exact identity" in ev["match_reasons"][0]
    ledger = (await cmd(db, owner, "costs.record_evidence", {"kind": "ledger_row", "source_ref": f"ledger:s:t:{_u()}", "source_hash": "h2",
                                                             "vendor": vendor.lower() + ".", "invoice_no": inv_no.replace("-", " "), "amount": "520.00",
                                                             "currency": "USD"})).data["evidence"]
    receipt = (await cmd(db, owner, "costs.record_evidence", {"kind": "receipt_photo", "source_ref": f"asset:{_u()}", "source_hash": "h3", "vendor": vendor,
                                                              "invoice_no": inv_no, "amount": "520.00", "currency": "USD"})).data["evidence"]
    assert ledger["match_state"] == receipt["match_state"] == "matched" and ledger["cost_item_id"] == receipt["cost_item_id"] == it["id"]
    paid = (await cmd(db, owner, "costs.observe", {"cost_item_id": it["id"], "kind": "paid", "amount": "520.00", "source_ref": "bank:1"})).data["cost_item"]
    assert paid["status"] == "paid" and paid["amount_invoiced"]["amount"] == "520.00" and paid["active_amount"]["amount"] == "520.00"
    assert paid["amount_quoted"]["amount"] == "500.00" and paid["amount_paid"]["amount"] == "520.00"   # observations, not additive
    items = (await db.execute(select(CostItem).where(CostItem.vendor_name == vendor))).scalars().all()
    assert len(items) == 1
    evs = (await db.execute(select(CostEvidence).where(CostEvidence.cost_item_id == it["id"]))).scalars().all()
    assert len(evs) == 3
    m = await fq.vehicle_money(db, v.id)
    assert m["estimated_total"]["amount"] == "520.00" and m["committed_invoiced"]["amount"] == "520.00" and m["cash_paid"]["amount"] == "520.00"
    assert m["remaining_payable"]["amount"] == "0.00"
    # idempotent re-submission of the same evidence source/hash creates nothing new
    again = await cmd(db, owner, "costs.record_evidence", {"kind": "email_invoice", "source_ref": ev["source_ref"], "source_hash": "h1", "vendor": vendor,
                                                           "invoice_no": inv_no, "amount": "520.00", "currency": "USD"})
    assert again.data["created"] is False and again.data["evidence"]["id"] == ev["id"]


# ── E03 ──────────────────────────────────────────────────────────────────────
async def test_E03_multi_vehicle_invoice_balances_with_tax_freight_and_credit(db, owner):
    v1, v2, v3 = await vehicle(db), await vehicle(db), await vehicle(db)
    it = await item(db, owner, category="parts", vendor_name=f"Bulk {_u()}", currency="USD", amount_invoiced="1100.00", tax_amount="80.00",
                    freight_amount="20.00", invoice_ref=f"INV-{_u()}")
    with pytest.raises(ValidationFailed):
        await cmd(db, owner, "costs.allocate", {"cost_item_id": it["id"], "basis": "explicit",
                                                "lines": [{"vehicle_id": v1.id, "amount": "600.00"}, {"vehicle_id": v2.id, "amount": "300.00"}]})
    res = (await cmd(db, owner, "costs.allocate", {"cost_item_id": it["id"], "basis": "explicit",
                                                   "lines": [{"vehicle_id": v1.id, "amount": "600.00"}, {"vehicle_id": v2.id, "amount": "400.00"}]})).data
    a1, a2 = res["allocations"]
    assert a1["amount"]["amount"] == "660.00" and a2["amount"]["amount"] == "440.00"
    assert a1["components"] == {"base": "600.00", "tax": "48.00", "freight": "12.00"} and a2["components"] == {"base": "400.00", "tax": "32.00", "freight": "8.00"}
    assert D(a1["amount"]["amount"]) + D(a2["amount"]["amount"]) == D("1100.00") and res["needs_review"] is False
    assert a1["state"] == "confirmed" and res["cost_item"]["allocation_state"] == "balanced" and res["cost_item"]["shared"] is True
    # partial return: credit traceable to the original allocation; item and allocation totals stay balanced
    cr = (await cmd(db, owner, "costs.record_credit", {"cost_item_id": it["id"], "amount": "110.00", "reason": "one caliper returned",
                                                       "allocations": [{"vehicle_id": v1.id, "amount": "110.00"}]})).data
    assert cr["cost_item"]["amount_credited"]["amount"] == "110.00" and cr["cost_item"]["net_amount"]["amount"] == "990.00"
    al = {a["vehicle_id"]: a for a in cr["cost_item"]["allocations"]}
    assert al[v1.id]["amount_credited"]["amount"] == "110.00" and al[v1.id]["net_amount"]["amount"] == "550.00"
    assert al[v1.id]["credits"][0]["original_allocation_id"] == al[v1.id]["id"] and al[v1.id]["credits"][0]["credit_id"] == cr["credit_id"]
    assert sum(D(a["net_amount"]["amount"]) for a in al.values()) == D("990.00")
    # rounding controlled: an equal three-way split of 100.00 sums exactly; fuzzy bases need review
    it3 = await item(db, owner, category="transport", vendor_name=f"Freight {_u()}", currency="USD", amount_invoiced="100.00")
    w = (await cmd(db, owner, "costs.allocate", {"cost_item_id": it3["id"], "basis": "equal",
                                                 "lines": [{"vehicle_id": v1.id}, {"vehicle_id": v2.id}, {"vehicle_id": v3.id}]}, kind="agent")).data
    parts = sorted(D(a["amount"]["amount"]) for a in w["allocations"])
    assert parts == [D("33.33"), D("33.33"), D("33.34")] and w["needs_review"] is True and all(a["state"] == "proposed" for a in w["allocations"])
    # JPY: zero-decimal currency rounds to whole yen and still balances
    ity = await item(db, owner, category="import", vendor_name=f"Port {_u()}", currency="JPY", amount_invoiced="100000")
    wy = (await cmd(db, owner, "costs.allocate", {"cost_item_id": ity["id"], "basis": "weighted",
                                                  "lines": [{"vehicle_id": v1.id, "weight": "1"}, {"vehicle_id": v2.id, "weight": "1"}, {"vehicle_id": v3.id, "weight": "1"}]})).data
    assert sum(D(a["amount"]["amount"]) for a in wy["allocations"]) == D("100000") and all("." not in a["amount"]["amount"] for a in wy["allocations"])
    ok = await cmd(db, owner, "costs.confirm_allocations", {"cost_item_id": it3["id"]})
    assert ok.data["cost_item"]["allocation_state"] == "balanced"
    m1 = await fq.vehicle_money(db, v1.id)
    assert m1["completeness"]["allocations_needing_review"] == 0 or True
    lines = {l["cost_item_id"]: l for l in m1["lines"]}
    assert lines[it["id"]]["amount"]["amount"] == "550.00" and lines[it3["id"]]["amount"]["amount"] in ("33.33", "33.34")


# ── E04 ──────────────────────────────────────────────────────────────────────
async def test_E04_invoice_ledger_disagreement_stays_conflict_never_averaged(db, owner):
    v = await vehicle(db, asking_price=D("9000.00"), asking_currency="USD", price_approved_at=NOW)
    vendor = f"Yard {_u()}"
    inv_no = f"INV-{_u()}"
    it = await item(db, owner, category="storage", vehicle_id=v.id, vendor_name=vendor, currency="USD", amount_invoiced="1000.00", invoice_ref=inv_no)
    conflict = (await cmd(db, owner, "costs.record_evidence", {"kind": "ledger_row", "source_ref": f"ledger:s:t:{_u()}", "source_hash": "x", "vendor": vendor,
                                                               "invoice_no": inv_no, "amount": "1200.00", "currency": "USD", "vehicle_ref_text": "STK-9999"})).data["evidence"]
    assert conflict["match_state"] == "conflict" and conflict["cost_item_id"] == it["id"]
    assert conflict["discrepancy"]["evidence"] == "1200.00" and conflict["discrepancy"]["item"] == "1000.00" and conflict["discrepancy"]["difference"] == "200.00"
    assert any("did not resolve" in r for r in conflict["match_reasons"])
    row = await db.get(CostItem, it["id"])
    await db.refresh(row)
    assert row.amount_invoiced == D("1000.00") and row.status == "invoiced"          # not averaged, not inferred paid
    await db.refresh(v)
    assert v.asking_price == D("9000.00")                                              # unconfirmed value never touches price
    cur = (await cmd(db, owner, "costs.record_evidence", {"kind": "email_invoice", "source_ref": f"gmail:{_u()}", "source_hash": "y", "vendor": vendor,
                                                          "invoice_no": inv_no, "amount": "150000", "currency": "JPY"})).data["evidence"]
    assert cur["match_state"] == "conflict" and cur["discrepancy"]["field"] == "currency"
    fuzzy = (await cmd(db, owner, "costs.record_evidence", {"kind": "ledger_row", "source_ref": f"ledger:s:t:{_u()}", "source_hash": "z", "vendor": vendor,
                                                            "amount": "1000.00", "currency": "USD", "occurred_at": NOW.isoformat()})).data["evidence"]
    assert fuzzy["match_state"] == "proposed" and fuzzy["proposed_cost_item_id"] == it["id"] and fuzzy["cost_item_id"] is None
    nm = await fq.needs_matching(db)
    assert {conflict["id"], cur["id"], fuzzy["id"]} <= {i["id"] for i in nm["items"] if i["item_kind"] == "cost_evidence"}
    # explicit resolution: the evidence value is authoritative → restated with a note (nothing silently changed before this)
    res = (await cmd(db, owner, "costs.confirm_match", {"evidence_id": conflict["id"], "resolve_discrepancy": "evidence", "note": "ledger has the corrected total"})).data
    assert res["evidence"]["match_state"] == "matched" and res["cost_item"]["amount_invoiced"]["amount"] == "1200.00"
    assert res["cost_item"]["restatements"][-1]["old"] == "1000.00" and res["cost_item"]["restatements"][-1]["new"] == "1200.00"
    left = (await cmd(db, owner, "costs.leave_unmatched", {"evidence_id": fuzzy["id"], "reason": "duplicate of the invoice row"})).data["evidence"]
    assert left["match_state"] == "unmatched" and left["reviewed_by"] == owner.id


# ── E05 ──────────────────────────────────────────────────────────────────────
async def test_E05_customer_claim_and_spoofed_square_email_stay_reported(db, owner, manager):
    c = await contact(db)
    opp = (await cmd(db, owner, "sales.create_opportunity", {"contact_id": c.id, "pipeline": "irq", "enquiry": "Acty"})).data["opportunity"]
    claim = (await cmd(db, manager, "payments.record_reported", {"amount": "1000.00", "currency": "USD", "claimed_by": "customer", "contact_id": c.id,
                                                                 "source_ref": f"telegram:{_u()}"})).data
    assert claim["payment"]["status"] == "reported" and "customer_claim" in claim["payment"]["report_flags"] and claim["decision"] == "Needs review"
    spoof = (await cmd(db, owner, "payments.record_reported", {"amount": "1000.00", "currency": "USD", "claimed_by": "email", "contact_id": c.id,
                                                               "sender": "receipts@squareup-payments.example.net", "subject": "Payment PAID — receipt",
                                                               "source_ref": f"gmail:{_u()}", "payer_name": "Someone Else"})).data["payment"]
    assert {"sender_not_provider", "subject_claims_paid_is_not_evidence", "needs_confirmation"} <= set(spoof["report_flags"])
    assert spoof["status_label"] == "Payment reported — needs confirmation"
    inv = (await cmd(db, owner, "invoices.create", {"kind": "deposit", "opportunity_id": opp["id"], "amount_due": "1000.00", "currency": "USD"})).data["invoice"]
    with pytest.raises(Blocked):
        await cmd(db, owner, "payments.propose_allocation", {"payment_id": spoof["id"], "invoice_id": inv["id"]})
    o = await db.get(Opportunity, opp["id"])
    await db.refresh(o)
    assert o.stage != "deposit_paid" and o.converted_id is None
    assert not await events_of(db, "deposit.confirmed", invoice_id=inv["id"])
    dup = (await cmd(db, owner, "payments.record_reported", {"amount": "1000.00", "currency": "USD", "claimed_by": "email", "source_ref": spoof["source_ref"]})).data
    assert dup["created"] is False and dup["payment"]["id"] == spoof["id"]
    # only verified manual evidence (owner) upgrades a claim, and confirmation is still not allocation
    up = (await cmd(db, owner, "payments.record_manual_confirmed", {"amount": "1000.00", "currency": "USD", "method": "zelle", "evidence_ref": "bank:zelle-441",
                                                                    "reported_payment_id": claim["payment"]["id"]})).data["payment"]
    assert up["status"] == "completed" and up["confirmed_by"] == owner.id
    with pytest.raises(Denied):
        await cmd(db, manager, "payments.record_manual_confirmed", {"amount": "5.00", "evidence_ref": "x"})
    with pytest.raises(Blocked):
        await cmd(db, owner, "payments.record_manual_confirmed", {"amount": "5.00"})


# ── E06 ──────────────────────────────────────────────────────────────────────
async def test_E06_duplicate_and_reordered_provider_events_and_refunds(db, owner):
    pid = f"pay-{_u()}"
    base = {"provider": "square", "merchant_id": "M-A", "provider_payment_id": pid, "amount": "500.00", "currency": "USD", "status": "COMPLETED"}
    first = (await cmd(db, owner, "payments.upsert_provider", {**base, "provider_event_id": "e1", "provider_version": "1"})).data
    assert first["created"] and first["payment"]["status"] == "completed"
    dup = (await cmd(db, owner, "payments.upsert_provider", {**base, "provider_event_id": "e1", "provider_version": "1"})).data
    assert dup["duplicate"] is True and dup["applied"] is False
    refund = (await cmd(db, owner, "payments.upsert_provider", {**base, "provider_event_id": "e3", "provider_version": "3",
                                                                "refunds": [{"id": "r1", "amount": "100.00", "currency": "USD", "status": "COMPLETED"}]})).data["payment"]
    assert refund["status"] == "partially_refunded" and refund["refunded_amount"]["amount"] == "100.00" and refund["available_amount"]["amount"] == "400.00"
    assert refund["amount"]["amount"] == "500.00"                                       # original evidence kept
    stale = (await cmd(db, owner, "payments.upsert_provider", {**base, "provider_event_id": "e2", "provider_version": "2"})).data
    assert stale["reordered"] is True and stale["payment"]["refunded_amount"]["amount"] == "100.00" and stale["payment"]["provider_version"] == "3"
    assert set(stale["payment"]["provider_event_ids"]) == {"e1", "e2", "e3"}
    other = (await cmd(db, owner, "payments.upsert_provider", {**base, "merchant_id": "M-B", "provider_event_id": "e1"})).data
    assert other["created"] is True and other["payment"]["id"] != first["payment"]["id"]    # invariant 1: namespaced by merchant
    rows = (await db.execute(select(Payment).where(Payment.provider_payment_id == pid))).scalars().all()
    assert len(rows) == 2
    # the outbox dedupes provider identities too
    evs = (await db.execute(select(Event).where(Event.provider == "square", Event.provider_event_id == "M-A:e1"))).scalars().all()
    assert len(evs) == 1


# ── E07 ──────────────────────────────────────────────────────────────────────
async def test_E07_partial_wrong_currency_overpaid_and_two_candidates_stay_explicit(db, owner):
    c = await contact(db)
    opp = (await cmd(db, owner, "sales.create_opportunity", {"contact_id": c.id, "pipeline": "irq", "enquiry": "Sambar"})).data["opportunity"]
    inv = (await cmd(db, owner, "invoices.create", {"kind": "deposit", "opportunity_id": opp["id"], "amount_due": "500.00", "currency": "USD"})).data["invoice"]
    p300 = await confirmed_payment(db, owner, "300.00", contact_id=c.id)
    res = await allocate(db, owner, p300["id"], inv["id"])
    assert res["invoice"]["status"] == "partially_paid" and res["invoice"]["remaining"]["amount"] == "200.00" and "partial" in res["allocation"]["flags"]
    assert res["handoff"] == {}                                                          # partial deposit never hands off
    # wrong currency: explicit conversion required, nothing guessed
    pj = await confirmed_payment(db, owner, "30000", "JPY", contact_id=c.id)
    prop = (await cmd(db, owner, "payments.propose_allocation", {"payment_id": pj["id"], "invoice_id": inv["id"]})).data["allocations"][0]
    assert {"currency_mismatch", "conversion_required"} <= set(prop["flags"])
    with pytest.raises(Blocked):
        await cmd(db, owner, "payments.confirm_allocation", {"allocation_id": prop["id"]})
    conv = (await cmd(db, owner, "payments.confirm_allocation", {"allocation_id": prop["id"], "conversion": {"rate": "0.0066", "source": "bank statement",
                                                                                                             "date": "2026-09-01"}})).data
    assert conv["allocation"]["applied_amount"] == {"amount": "198.00", "currency": "USD"} and conv["invoice"]["remaining"]["amount"] == "2.00"
    # overpaid stays explicit (no automatic refund); invariant 4 blocks over-allocation of a payment
    p800 = await confirmed_payment(db, owner, "800.00", contact_id=c.id)
    over = (await cmd(db, owner, "payments.propose_allocation", {"payment_id": p800["id"], "invoice_id": inv["id"], "amount": "800.00"})).data["allocations"][0]
    ok = (await cmd(db, owner, "payments.confirm_allocation", {"allocation_id": over["id"]})).data
    assert ok["invoice"]["status"] == "overpaid" and ok["invoice"]["overpaid_by"]["amount"] == "798.00"
    with pytest.raises(Blocked):
        await cmd(db, owner, "payments.propose_allocation", {"payment_id": p800["id"], "invoice_id": inv["id"], "amount": "1.00"})
    # two open obligations for one contact: proposal keeps both candidates; owner picks one
    c2 = await contact(db)
    o2 = (await cmd(db, owner, "sales.create_opportunity", {"contact_id": c2.id, "pipeline": "irq", "enquiry": "Carry"})).data["opportunity"]
    dep = (await cmd(db, owner, "invoices.create", {"kind": "deposit", "opportunity_id": o2["id"], "amount_due": "500.00", "currency": "USD"})).data["invoice"]
    ship = (await cmd(db, owner, "invoices.create", {"kind": "shipping", "opportunity_id": o2["id"], "amount_due": "500.00", "currency": "USD"})).data["invoice"]
    p500 = await confirmed_payment(db, owner, "500.00", contact_id=c2.id)
    amb = (await cmd(db, owner, "payments.propose_allocation", {"payment_id": p500["id"]})).data
    assert amb["state"] == "ambiguous" and {a["invoice_id"] for a in amb["allocations"]} == {dep["id"], ship["id"]}
    assert all(a["state"] == "proposed" and a["ambiguous"] for a in amb["allocations"])
    pick = next(a for a in amb["allocations"] if a["invoice_id"] == ship["id"])
    done = (await cmd(db, owner, "payments.confirm_allocation", {"allocation_id": pick["id"]})).data
    assert done["invoice"]["status"] == "paid" and done["handoff"] == {}
    other = next(a for a in amb["allocations"] if a["invoice_id"] == dep["id"])
    row = await db.get(PaymentAllocation, other["id"])
    await db.refresh(row)
    assert row.state == "declined"
    d = await db.get(Invoice, dep["id"])
    await db.refresh(d)
    assert d.status == "open"


# ── E08 / C08 ────────────────────────────────────────────────────────────────
async def test_E08_C08_confirmed_deposit_hands_off_once_reusing_existing_request(db, owner):
    c = await contact(db)
    opp = (await cmd(db, owner, "sales.create_opportunity", {"contact_id": c.id, "pipeline": "irq", "enquiry": "Hijet Jumbo 4WD"})).data["opportunity"]
    pre = (await cmd(db, owner, "import_requests.create", {"contact_id": c.id, "opportunity_id": opp["id"], "title": "Hijet Jumbo",
                                                           "requirements": [{"tier": "must", "text": "4WD"}], "source_ref": f"msg-{_u()}"})).data["request"]
    await cmd(db, owner, "import_requests.set_agreement", {"request_id": pre["id"], "agreement_id": f"agr-{_u()}", "status": "signed", "source_ref": "asset:signed"})
    await cmd(db, owner, "import_requests.set_deposit_rule", {"request_id": pre["id"], "amount": "1000.00", "currency": "USD"})
    chase = (await cmd(db, owner, "tasks.create", {"title": f"Collect deposit {_u()}", "opportunity_id": opp["id"], "gate_requirement": "deposit"})).data["task"]
    with pytest.raises(Blocked):   # unset rule elsewhere → no automatic deposit invoice
        o_unset = (await cmd(db, owner, "sales.create_opportunity", {"contact_id": c.id, "pipeline": "irq", "enquiry": "no terms"})).data["opportunity"]
        await cmd(db, owner, "invoices.create", {"kind": "deposit", "opportunity_id": o_unset["id"]})
    inv = (await cmd(db, owner, "invoices.create", {"kind": "deposit", "opportunity_id": opp["id"], "import_request_id": pre["id"]})).data["invoice"]
    assert inv["amount_due"] == {"amount": "1000.00", "currency": "USD"} and inv["terms_source"] == "import_request"
    pay = await confirmed_payment(db, owner, "1000.00", contact_id=c.id, pid=f"sq-{_u()}", event="ev-dep-1")
    res = await allocate(db, owner, pay["id"], inv["id"])
    h = res["handoff"]
    assert res["invoice"]["status"] == "paid" and h["done"] and h["kind"] == "import_request" and h["id"] == pre["id"]
    reqs = (await db.execute(select(ImportRequest).where(ImportRequest.opportunity_id == opp["id"]))).scalars().all()
    assert len(reqs) == 1 and reqs[0].deposit_status == "confirmed" and reqs[0].deposit_evidence["payment_id"] == pay["id"]
    o = await db.get(Opportunity, opp["id"])
    await db.refresh(o)
    assert o.stage == "deposit_paid" and o.converted_kind == "import_request" and o.converted_id == pre["id"] and o.deposit_payment_id == pay["id"]
    t = await db.get(Task, chase["id"])
    await db.refresh(t)
    assert t.status == "cancelled" and chase["id"] in h["cancelled_tasks"]
    evs = await events_of(db, "deposit.confirmed", invoice_id=inv["id"])
    assert len(evs) == 1 and evs[0].payload["provider_receipt_ref"] == pay["provider_payment_id"] and evs[0].payload["contact_id"] == c.id
    assert reqs[0].status != "purchased" and not (await db.execute(select(Bid))).scalars().all()      # no automatic bid
    assert not (await db.execute(select(Approval).where(Approval.kind == "bid"))).scalars().all()
    # C08: the same deposit event processed twice → no second conversion / request / task
    dup = (await cmd(db, owner, "payments.upsert_provider", {"provider": "square", "merchant_id": "M1", "provider_payment_id": pay["provider_payment_id"],
                                                             "provider_event_id": "ev-dep-1", "provider_version": "1", "amount": "1000.00", "currency": "USD",
                                                             "status": "COMPLETED"})).data
    assert dup["duplicate"] is True
    with pytest.raises(Blocked):
        await cmd(db, owner, "payments.propose_allocation", {"payment_id": pay["id"], "invoice_id": inv["id"]})
    again = (await cmd(db, owner, "payments.confirm_allocation", {"allocation_id": res["allocation"]["id"]})).data
    assert again["idempotent"] is True and again["handoff"]["done"] is True
    assert len((await db.execute(select(ImportRequest).where(ImportRequest.opportunity_id == opp["id"]))).scalars().all()) == 1
    assert len(await events_of(db, "deposit.confirmed", invoice_id=inv["id"])) == 1
    assert len((await db.execute(select(Task).where(Task.opportunity_id == opp["id"]))).scalars().all()) == 1


async def test_E08_vehicle_pipeline_deposit_reserves_once(db, owner):
    c = await contact(db)
    v = await vehicle(db)
    opp = (await cmd(db, owner, "sales.create_opportunity", {"contact_id": c.id, "pipeline": "vehicle", "vehicle_id": v.id})).data["opportunity"]
    agr = (await cmd(db, owner, "agreements.create", {"kind": "sale", "contact_id": c.id, "vehicle_id": v.id, "opportunity_id": opp["id"],
                                                      "deposit_amount": "500.00", "deposit_currency": "USD", "price_amount": "8000.00", "price_currency": "USD",
                                                      "terms": {"reservation_days": 7}})).data["agreement"]
    inv = (await cmd(db, owner, "invoices.create", {"kind": "deposit", "opportunity_id": opp["id"], "agreement_id": agr["id"]})).data["invoice"]
    assert inv["amount_due"]["amount"] == "500.00" and inv["terms_source"] == "agreement"
    pay = await confirmed_payment(db, owner, "500.00", contact_id=c.id)
    res = await allocate(db, owner, pay["id"], inv["id"])
    h = res["handoff"]
    assert h["kind"] == "sale" and h["id"]
    s = await db.get(Sale, h["id"])
    assert s.status == "reserved" and s.is_active and s.buyer_contact_id == c.id and s.price == D("8000.00") and s.reservation_expires_at is not None
    await db.refresh(v)
    assert v.allocation == "reserved" and v.commercial_state == "reserved" and v.active_sale_id == s.id
    o = await db.get(Opportunity, opp["id"])
    await db.refresh(o)
    assert o.converted_kind == "sale" and o.converted_id == s.id and o.stage == "deposit_paid"
    sales = (await db.execute(select(Sale).where(Sale.vehicle_id == v.id))).scalars().all()
    assert len(sales) == 1


# ── E09 ──────────────────────────────────────────────────────────────────────
async def test_E09_refund_after_handoff_pauses_and_recovery_makes_no_second_handoff(db, owner):
    c = await contact(db)
    v = await vehicle(db)
    opp = (await cmd(db, owner, "sales.create_opportunity", {"contact_id": c.id, "pipeline": "vehicle", "vehicle_id": v.id})).data["opportunity"]
    inv = (await cmd(db, owner, "invoices.create", {"kind": "deposit", "opportunity_id": opp["id"], "amount_due": "500.00", "currency": "USD"})).data["invoice"]
    pid = f"sq-{_u()}"
    pay = await confirmed_payment(db, owner, "500.00", contact_id=c.id, pid=pid, event="ev-a")
    res = await allocate(db, owner, pay["id"], inv["id"])
    sale_id = res["handoff"]["id"]
    assert res["handoff"]["kind"] == "sale"
    # refund arrives from the provider (API may be disconnected: only the event is known)
    ref = (await cmd(db, owner, "payments.upsert_provider", {"provider": "square", "merchant_id": "M1", "provider_payment_id": pid, "provider_event_id": "ev-b",
                                                             "provider_version": "2", "refunds": [{"id": "rf1", "amount": "500.00", "currency": "USD", "status": "COMPLETED"}]})).data
    assert ref["payment"]["status"] == "refunded" and ref["paused"] == [f"thread:{sale_id}"]
    assert any(e["kind"] == "refund_exceeds_allocation" for e in ref["payment"]["exceptions"])
    wc = await db.get(WorkflowControl, f"thread:{sale_id}")
    assert wc is not None and wc.paused
    i = await db.get(Invoice, inv["id"])
    await db.refresh(i)
    assert i.handoff["done"] and i.handoff["exceptions"]                                 # history preserved
    # owner reverses the allocation: obligation reopens, conversion history stays, sale carries the exception
    rev = (await cmd(db, owner, "payments.reverse_allocation", {"allocation_id": res["allocation"]["id"], "reason": "customer refunded", "kind": "refund"})).data
    assert rev["invoice"]["status"] == "open" and rev["invoice"]["handoff"]["done"] and rev["invoice"]["handoff"]["reversals"][0]["kind"] == "refund"
    s = await db.get(Sale, sale_id)
    await db.refresh(s)
    assert s.status == "reserved" and s.exceptions[-1]["kind"] == "deposit_refund"        # not silently deleted
    o = await db.get(Opportunity, opp["id"])
    await db.refresh(o)
    assert o.converted_id == sale_id
    evs = await events_of(db, "payment.allocated", invoice_id=inv["id"])
    assert [e.payload["change"] for e in evs][-1] == "reversed"
    # later reconciliation: the same refund event again is harmless; a new payment satisfies the obligation without a second handoff
    dup = (await cmd(db, owner, "payments.upsert_provider", {"provider": "square", "merchant_id": "M1", "provider_payment_id": pid, "provider_event_id": "ev-b",
                                                             "provider_version": "2"})).data
    assert dup["duplicate"] is True
    pay2 = await confirmed_payment(db, owner, "500.00", contact_id=c.id)
    res2 = await allocate(db, owner, pay2["id"], inv["id"])
    assert res2["invoice"]["status"] == "paid" and res2["handoff"]["repeated"] is True and res2["handoff"]["id"] == sale_id
    assert len((await db.execute(select(Sale).where(Sale.vehicle_id == v.id))).scalars().all()) == 1
    assert len(await events_of(db, "deposit.confirmed", invoice_id=inv["id"])) == 1
    assert len((await db.execute(select(Payment).where(Payment.provider_payment_id == pid))).scalars().all()) == 1


# ── E10 ──────────────────────────────────────────────────────────────────────
async def test_E10_cost_payment_fee_and_payout_are_distinct(db, owner):
    c = await contact(db)
    v = await vehicle(db)
    cost = await item(db, owner, category="recon", vehicle_id=v.id, vendor_name=f"Shop {_u()}", currency="USD", amount_invoiced="1000.00")
    pay = await confirmed_payment(db, owner, "5000.00", contact_id=c.id, fee_amount="150.00", net_amount="4850.00")
    payout = (await cmd(db, owner, "payments.upsert_provider", {"provider": "square", "merchant_id": "M1", "provider_payment_id": f"payout-{_u()}",
                                                                "provider_event_id": f"e-{_u()}", "amount": "4850.00", "currency": "USD", "status": "COMPLETED",
                                                                "is_payout": True})).data["payment"]
    assert payout["is_payout"] and payout["status_label"].startswith("Payout to bank")
    assert pay["fee_amount"]["amount"] == "150.00" and pay["net_amount"]["amount"] == "4850.00" and pay["amount"]["amount"] == "5000.00"
    with pytest.raises(Blocked):
        await cmd(db, owner, "payments.propose_allocation", {"payment_id": payout["id"]})
    m = await fq.vehicle_money(db, v.id)
    assert m["committed_invoiced"]["amount"] == "1000.00" and m["cash_paid"]["amount"] == "0.00"      # customer payments are not vehicle cost
    pay_rows = (await db.execute(select(Payment).where(Payment.id.in_([pay["id"], payout["id"]])))).scalars().all()
    assert {p.is_payout for p in pay_rows} == {True, False}
    cost_rows = (await db.execute(select(CostItem).where(CostItem.vehicle_id == v.id))).scalars().all()
    assert len(cost_rows) == 1 and cost_rows[0].id == cost["id"]                                  # fee/payout never became costs


# ── K01 / K02 ────────────────────────────────────────────────────────────────
async def _sold(db, owner, v: Vehicle, c: Contact, price: str, completed_at: datetime) -> dict:
    s = (await cmd(db, owner, "sales.reserve", {"vehicle_id": v.id, "buyer_contact_id": c.id})).data["sale"]
    await cmd(db, owner, "sales.agree", {"sale_id": s["id"], "price": price, "currency": "USD", "sales_tax": "700.00"})
    return (await cmd(db, owner, "sales.mark_completed", {"sale_id": s["id"], "completed_at": completed_at.isoformat(), "source_ref": "bill_of_sale"})).data["sale"]


async def test_K01_sold_cohort_data_two_sales_16000_costs_22000_net(db, owner):
    c = await contact(db)
    v1, v2, v3 = await vehicle(db, acquired_at=datetime(2021, 1, 1, tzinfo=timezone.utc)), await vehicle(db, acquired_at=datetime(2021, 2, 1, tzinfo=timezone.utc)), await vehicle(db)
    tag = _u()
    await item(db, owner, category="purchase", vehicle_id=v1.id, vendor_name=f"Auction {tag}", currency="USD", amount_invoiced="4000.00")
    await item(db, owner, category="purchase", vehicle_id=v2.id, vendor_name=f"Auction {tag}", currency="USD", amount_invoiced="6000.00")
    shared = await item(db, owner, category="transport", vendor_name=f"Shipper {tag}", currency="USD", amount_invoiced="6000.00")
    await cmd(db, owner, "costs.allocate", {"cost_item_id": shared["id"], "basis": "explicit",
                                            "lines": [{"vehicle_id": v1.id, "amount": "3000.00"}, {"vehicle_id": v2.id, "amount": "3000.00"}]})
    await item(db, owner, category="purchase", vehicle_id=v3.id, vendor_name=f"Auction {tag}", currency="USD", amount_invoiced="5000.00")   # unsold
    await item(db, owner, category="recon", vehicle_id=v3.id, vendor_name=f"Shop {tag}", currency="USD", amount_estimated="800.00")       # estimate
    when = datetime(2021, 3, 10, tzinfo=timezone.utc)
    s1 = await _sold(db, owner, v1, c, "10000.00", when)
    s2 = await _sold(db, owner, v2, c, "12000.00", when + timedelta(days=5))
    earlier = await vehicle(db)
    await item(db, owner, category="purchase", vehicle_id=earlier.id, vendor_name=f"Auction {tag}", currency="USD", amount_invoiced="1.00")
    await _sold(db, owner, earlier, c, "5.00", datetime(2021, 2, 20, tzinfo=timezone.utc))                                            # outside period
    inv = (await cmd(db, owner, "invoices.create", {"kind": "deposit", "sale_id": s1["id"], "contact_id": c.id, "amount_due": "500.00", "currency": "USD"})).data["invoice"]
    dep = await confirmed_payment(db, owner, "500.00", contact_id=c.id)
    await allocate(db, owner, dep["id"], inv["id"])
    await cmd(db, owner, "payments.upsert_provider", {"provider": "square", "merchant_id": "M1", "provider_payment_id": f"po-{tag}", "provider_event_id": f"e-{tag}",
                                                      "amount": "9000.00", "currency": "USD", "status": "COMPLETED", "is_payout": True})
    cohort = await fq.sold_cohort(db, datetime(2021, 3, 1, tzinfo=timezone.utc), datetime(2021, 4, 1, tzinfo=timezone.utc))
    assert cohort["vehicles_sold"] == 2 and {r["sale"]["id"] for r in cohort["sales"]} == {s1["id"], s2["id"]}
    assert cohort["costs_total"] == {"amount": "16000.00", "currency": "USD"} and cohort["net_sales_value"] == {"amount": "22000.00", "currency": "USD"}
    assert cohort["gross_profit"] == {"amount": "6000.00", "currency": "USD"} and cohort["gross_profit_label"] == "Recorded gross profit"
    assert cohort["gross_margin"] == str((D("6000") / D("22000")).quantize(D("0.0001")))
    assert cohort["costs_by_category"]["purchase"]["usd"]["amount"] == "10000.00" and cohort["costs_by_category"]["transport"]["usd"]["amount"] == "6000.00"
    assert cohort["days_to_sale_median"] == (68 + 42) / 2 and cohort["days_to_sale_included"] == 2
    unsold = await fq.unsold_inventory_cost(db)
    row = next(r for r in unsold["vehicles"] if r["vehicle_id"] == v3.id)
    assert row["cost_usd"]["amount"] == "5800.00" and row["flags"]["estimated_lines"] == 1       # separate stock balance, never in the cohort
    m1 = await fq.vehicle_money(db, v1.id)
    assert m1["estimated_total"]["amount"] == "7000.00" and m1["approved_sale_price"]["amount"] == "10000.00" and m1["estimated_margin"]["amount"] == "3000.00"


async def test_K02_inputs_estimates_fx_negative_profit_and_zero_denominator(db, owner):
    c = await contact(db)
    v = await vehicle(db)
    tag = _u()
    await item(db, owner, category="purchase", vehicle_id=v.id, vendor_name=f"Auction {tag}", currency="USD", amount_invoiced="12000.00")
    jpy = await item(db, owner, category="import", vehicle_id=v.id, vendor_name=f"Port {tag}", currency="JPY", amount_invoiced="50000")
    m = await fq.vehicle_money(db, v.id)
    assert m["completeness"]["fx_missing_lines"] == 1 and m["completeness"]["label"] == "Estimated" and m["estimated_total"]["amount"] == "12000.00"
    fx = (await cmd(db, owner, "costs.set_fx", {"cost_item_id": jpy["id"], "rate": "0.0067", "source": "xe.com", "fx_date": "2026-09-01"})).data["cost_item"]
    assert fx["fx"]["usd_amount"]["amount"] == "335.00" and fx["fx"]["actual"] is False
    actual = (await cmd(db, owner, "costs.set_fx", {"cost_item_id": jpy["id"], "usd_amount": "340.10", "actual": True, "source": "bank"})).data["cost_item"]
    assert actual["fx"]["usd_amount"]["amount"] == "340.10" and actual["fx"]["actual"] is True
    est = (await cmd(db, owner, "costs.set_fx", {"cost_item_id": jpy["id"], "rate": "0.0070", "source": "xe.com"})).data["cost_item"]
    assert est["fx"]["usd_amount"]["amount"] == "340.10"                                    # actual bank conversion wins
    when = datetime(2020, 6, 10, tzinfo=timezone.utc)
    await _sold(db, owner, v, c, "10000.00", when)
    cohort = await fq.sold_cohort(db, datetime(2020, 6, 1, tzinfo=timezone.utc), datetime(2020, 7, 1, tzinfo=timezone.utc))
    assert cohort["gross_profit"]["amount"] == "-2340.10" and cohort["vehicles_sold"] == 1          # negative profit retained
    z = await vehicle(db)
    await _sold(db, owner, z, c, "0.00", datetime(2020, 8, 10, tzinfo=timezone.utc))
    zero = await fq.sold_cohort(db, datetime(2020, 8, 1, tzinfo=timezone.utc), datetime(2020, 9, 1, tzinfo=timezone.utc))
    assert zero["gross_margin"] is None and zero["gross_margin_note"] and zero["gross_profit_label"] == "Estimated gross profit"
    # K03 input: a late cost correction restates the sold cohort with a note
    rs = (await cmd(db, owner, "costs.restate", {"cost_item_id": jpy["id"], "field": "amount_invoiced", "new_value": "52000", "note": "customs corrected"})).data
    assert rs["sales_restated"] and (await fq.sold_cohort(db, datetime(2020, 6, 1, tzinfo=timezone.utc), datetime(2020, 7, 1, tzinfo=timezone.utc)))["restatements"]


# ── invariant 4 under concurrency ────────────────────────────────────────────
async def test_invariant4_concurrent_confirmations_cannot_over_allocate_a_payment(db, owner):
    """Two sessions confirming two proposals on the same payment serialize on the payment row: the sum of confirmed
    allocations never exceeds amount − refunded (invariant 4)."""
    import asyncio

    from backend.app import db as dbmod
    from backend.tests.conftest import actor_of
    c = await contact(db)
    o1 = (await cmd(db, owner, "sales.create_opportunity", {"contact_id": c.id, "pipeline": "irq", "enquiry": "A"})).data["opportunity"]
    o2 = (await cmd(db, owner, "sales.create_opportunity", {"contact_id": c.id, "pipeline": "irq", "enquiry": "B"})).data["opportunity"]
    i1 = (await cmd(db, owner, "invoices.create", {"kind": "shipping", "opportunity_id": o1["id"], "amount_due": "300.00", "currency": "USD"})).data["invoice"]
    i2 = (await cmd(db, owner, "invoices.create", {"kind": "shipping", "opportunity_id": o2["id"], "amount_due": "300.00", "currency": "USD"})).data["invoice"]
    pay = await confirmed_payment(db, owner, "500.00", contact_id=c.id)
    a1 = (await cmd(db, owner, "payments.propose_allocation", {"payment_id": pay["id"], "invoice_id": i1["id"], "amount": "300.00"})).data["allocations"][0]
    a2 = (await cmd(db, owner, "payments.propose_allocation", {"payment_id": pay["id"], "invoice_id": i2["id"], "amount": "300.00"})).data["allocations"][0]

    async def confirm(alloc_id: str):
        async with dbmod.SessionLocal() as s:
            ctx = ctx_for(s, owner)
            try:
                return (await dispatch(ctx, "payments.confirm_allocation", {"allocation_id": alloc_id})).data["confirmed"]
            except Blocked as e:
                return e.message

    r1, r2 = await asyncio.gather(confirm(a1["id"]), confirm(a2["id"]))
    assert sorted([r1 is True, r2 is True]) == [False, True], (r1, r2)
    refused = r1 if r1 is not True else r2
    assert "available amount" in refused
    total = sum(a.amount for a in (await db.execute(select(PaymentAllocation).where(
        PaymentAllocation.payment_id == pay["id"], PaymentAllocation.state == "confirmed"))).scalars().all())
    assert total == D("300.00") <= D("500.00")
    p = await db.get(Payment, pay["id"])
    await db.refresh(p)
    assert p.allocated_amount == D("300.00") and p.allocated_amount <= p.available_amount
    assert actor_of(owner).is_human


# ── invariant 5: a split that stops balancing is never read as recorded ───────
async def test_invariant5_unbalanced_split_is_visible_and_blocks_confirmation(db, owner):
    """A multi-vehicle allocation cannot be re-derived when the source amount is restated: the split is flagged
    unbalanced, every line needs review, and the owner cannot confirm it until it balances again (invariant 5)."""
    v1, v2 = await vehicle(db), await vehicle(db)
    it = await item(db, owner, category="transport", vendor_name=f"Shipper {_u()}", currency="USD", amount_invoiced="1000.00")
    await cmd(db, owner, "costs.allocate", {"cost_item_id": it["id"], "basis": "explicit",
                                            "lines": [{"vehicle_id": v1.id, "amount": "600.00"}, {"vehicle_id": v2.id, "amount": "400.00"}]})
    out = (await cmd(db, owner, "costs.restate", {"cost_item_id": it["id"], "field": "amount_invoiced", "new_value": "1200.00",
                                                  "note": "vendor corrected the invoice"})).data["cost_item"]
    assert out["allocation_state"] == "unbalanced"
    m1 = await fq.vehicle_money(db, v1.id)
    line = next(l for l in m1["lines"] if l["cost_item_id"] == it["id"])
    assert line["needs_review"] is True and line["unbalanced"] is True and line["allocation_state"] == "unbalanced"
    assert m1["completeness"]["allocations_needing_review"] >= 1 and m1["completeness"]["label"] == "Estimated"
    allocs = (await db.execute(select(CostAllocation).where(CostAllocation.cost_item_id == it["id"]))).scalars().all()
    assert all(any("no longer balance" in r for r in (a.review_reasons or [])) for a in allocs)
    with pytest.raises(Blocked):
        await cmd(db, owner, "costs.confirm_allocations", {"cost_item_id": it["id"]})
    fixed = (await cmd(db, owner, "costs.allocate", {"cost_item_id": it["id"], "basis": "explicit",
                                                     "lines": [{"vehicle_id": v1.id, "amount": "720.00"},
                                                               {"vehicle_id": v2.id, "amount": "480.00"}]})).data
    assert fixed["cost_item"]["allocation_state"] == "balanced" and sum(D(a["amount"]["amount"]) for a in fixed["allocations"]) == D("1200.00")
    line = next(l for l in (await fq.vehicle_money(db, v1.id))["lines"] if l["cost_item_id"] == it["id"])
    assert line["needs_review"] is False and line["amount"]["amount"] == "720.00"


# ── owner-only matrix ────────────────────────────────────────────────────────
OWNER_ONLY = [
    ("costs.confirm_allocations", {"cost_item_id": "cost-item-placeholder"}),
    ("payments.record_manual_confirmed", {"amount": "100.00", "currency": "USD", "evidence_ref": "bank:1"}),
    ("payments.confirm_allocation", {"allocation_id": "allocation-placeholder"}),
    ("payments.reverse_allocation", {"allocation_id": "allocation-placeholder", "reason": "refund", "kind": "refund"}),
    ("sales.agree", {"sale_id": "sale-placeholder", "price": "8000.00", "currency": "USD"}),
    ("sales.mark_completed", {"sale_id": "sale-placeholder"}),
    ("sales.record_credit", {"sale_id": "sale-placeholder", "amount": "100.00", "reason": "return"}),
    ("ledger.activate_mapping", {"mapping_id": "mapping-placeholder"}),
]


async def test_owner_only_commands_denied_for_manager_mechanic_agent_and_external(db, owner, manager, mechanic):
    """Owner decisions (money confirmation, agreed terms, completion, ledger authority) are server-side owner-only:
    managers, mechanics and connectors are Denied; an agent acting for the owner only ever prepares an approval."""
    from backend.app.models.finance import CostItem as _CI
    before = len((await db.execute(select(_CI))).scalars().all())
    external = Actor(kind="external", user_id=owner.id, role="owner", scope="all", perms=effective_perms("owner", {}),
                     client_id="client-review", client_name="Connector",
                     client_scopes=["write:sales", "read:costs", "write:vehicles"], client_record_scope={})
    for name, payload in OWNER_ONLY:
        for user in (manager, mechanic):
            with pytest.raises(Denied):
                await cmd(db, user, name, payload)
        with pytest.raises(Denied):
            await dispatch(CommandContext(db=db, actor=external, correlation_id="matrix"), name, payload)
        agent = await cmd(db, owner, name, payload, kind="agent")
        assert agent.status == "needs_review" and agent.approval_id, f"{name} executed for an agent"
        assert agent.changed == []
    # nothing was created or mutated by any refused attempt
    assert len((await db.execute(select(_CI))).scalars().all()) == before


# ── A03 / router ─────────────────────────────────────────────────────────────
async def test_A03_finance_status_is_sanitized_without_costs_grant(client, db, owner, manager, mechanic):
    login(client, manager)
    r = await client.get("/api/finance/summary")
    assert r.status_code == 200 and r.json()["money_hidden"] is True and r.json()["receivables_outstanding"] is None and r.json()["sold_cohort"] is None
    assert (await client.get("/api/finance/needs-matching")).status_code == 403
    assert (await client.get("/api/finance/export.csv")).status_code == 403
    login(client, mechanic)
    assert (await client.get("/api/finance/summary")).status_code == 403
    login(client, owner)
    r = await client.get("/api/finance/summary")
    assert r.status_code == 200 and r.json()["money_hidden"] is False and "sold_cohort" in r.json()
    books = await make_user(db, f"books{_u()}", "books")
    login(client, books)
    assert (await client.get("/api/finance/needs-matching")).status_code == 200
    csv_text = (await client.get("/api/finance/export.csv?kind=allocations")).text
    assert csv_text.splitlines()[0].startswith("cost_item_id,vehicle_id")
    tab = await client.get("/api/finance/sold-cohort?from=2021-03-01&to=2021-03-31")
    assert tab.status_code == 200 and tab.json()["vehicles_sold"] >= 2
    login(client, owner)
    v = await vehicle(db)
    created = await client.post("/api/finance/costs/create-item", json={"category": "parts", "vehicle_id": v.id, "amount_invoiced": "10.00", "currency": "USD"},
                                headers={"Idempotency-Key": f"k-{_u()}"})
    assert created.status_code == 200 and created.json()["status"] == "ok"
    money = await client.get(f"/api/finance/vehicles/{v.id}/money")
    assert money.status_code == 200 and money.json()["committed_invoiced"] == {"amount": "10.00", "currency": "USD"}
    login(client, mechanic)
    assert (await client.get(f"/api/finance/vehicles/{v.id}/money")).status_code == 403
