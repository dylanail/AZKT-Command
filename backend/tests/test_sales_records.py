"""Sale / reservation records: C09 concurrent reservations, sale lifecycle (agree → complete → deliver), cancellation
reconciliation, expiry sweep, credits/restatements (K03 input), documents and agreements."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from backend.app import db as dbmod
from backend.app.core.errors import Blocked, ValidationFailed
from backend.app.domain.commands import CommandContext, dispatch
from backend.app.models.assets import Asset
from backend.app.models.contacts import Contact
from backend.app.models.finance import Document, Sale
from backend.app.models.runtime import Approval
from backend.app.models.vehicles import Vehicle
from backend.app.services import sales_records as sr
from backend.tests.conftest import actor_of, ctx_for, login, run_worker_once

NOW = datetime.now(timezone.utc)


def _u() -> str:
    return uuid.uuid4().hex[:8]


@pytest_asyncio.fixture(autouse=True, scope="module")
async def _drain_outbox_after_module():
    yield
    for _ in range(50):
        out = await run_worker_once()
        if not out.get("events") and not out.get("jobs"):
            break


async def contact(db, name: str | None = None) -> Contact:
    name = name or f"Buyer {_u()}"
    c = Contact(name=name, roles=["buyer"], status="active", search_text=name.lower())
    db.add(c)
    await db.commit()
    return c


async def vehicle(db, **kw) -> Vehicle:
    stock = f"STK-{_u()[:4].upper()}"
    v = Vehicle(stock_no=stock, title=stock, make="Suzuki", model="Carry", model_year=2003, **kw)
    db.add(v)
    await db.commit()
    await db.refresh(v)
    return v


async def cmd(db, user, name, payload, **kw):
    return await dispatch(ctx_for(db, user, **kw), name, payload)


# ── C09 ──────────────────────────────────────────────────────────────────────
async def test_C09_two_concurrent_reservations_one_valid_plus_owner_decision(db, owner):
    v = await vehicle(db)
    a, b = await contact(db, "Buyer A"), await contact(db, "Buyer B")

    async def reserve(buyer: Contact):
        async with dbmod.SessionLocal() as s:
            ctx = CommandContext(db=s, actor=actor_of(owner), correlation_id=f"c09-{buyer.id[:6]}")
            return (await dispatch(ctx, "sales.reserve", {"vehicle_id": v.id, "buyer_contact_id": buyer.id})).data

    r1, r2 = await asyncio.gather(reserve(a), reserve(b))
    outcomes = sorted([r1["outcome"], r2["outcome"]])
    assert outcomes == ["conflict", "reserved"]
    won = r1 if r1["outcome"] == "reserved" else r2
    lost = r2 if won is r1 else r1
    assert lost["active_sale_id"] == won["sale"]["id"] and lost["decision"] == "Needs review" and lost["approval_id"]
    active = (await db.execute(select(Sale).where(Sale.vehicle_id == v.id, Sale.is_active.is_(True)))).scalars().all()
    assert len(active) == 1 and active[0].id == won["sale"]["id"]
    ap = await db.get(Approval, lost["approval_id"])
    assert ap.kind == "reservation_conflict" and ap.status == "pending" and ap.command_name == "sales.request_reservation_override"
    await db.refresh(v)
    assert v.active_sale_id == won["sale"]["id"] and v.allocation == "reserved" and v.commercial_state == "reserved"
    assert "terms not configured" in won["sale"]["terms_flags"] and won["sale"]["reservation_expires_at"] is None
    # same buyer again is idempotent; a third buyer gets the same conflict shape
    again = (await cmd(db, owner, "sales.reserve", {"vehicle_id": v.id, "buyer_contact_id": active[0].buyer_contact_id})).data
    assert again["outcome"] == "existing" and again["sale"]["id"] == won["sale"]["id"]
    # the owner decides: approve the override → the losing buyer gets the vehicle, the earlier reservation is cancelled (no refund)
    ok = await cmd(db, owner, "approvals.approve", {"approval_id": ap.id, "expected_version": ap.approval_version})
    assert ok.status == "ok" and ok.data["result"]["data"]["cancelled_sale_id"] == won["sale"]["id"]
    active = (await db.execute(select(Sale).where(Sale.vehicle_id == v.id, Sale.is_active.is_(True)))).scalars().all()
    assert len(active) == 1 and active[0].buyer_contact_id != won["sale"]["buyer_contact_id"]
    old = await db.get(Sale, won["sale"]["id"])
    await db.refresh(old)
    assert old.status == "cancelled" and "owner override" in old.cancel_reason


async def test_sale_lifecycle_agree_complete_deliver_and_credit_restatement(db, owner, manager):
    v = await vehicle(db, listed_at=NOW)
    c = await contact(db)
    s = (await cmd(db, owner, "sales.reserve", {"vehicle_id": v.id, "buyer_contact_id": c.id, "terms": {"reservation_days": 7}})).data["sale"]
    assert s["reservation_expires_at"] is not None and s["terms_flags"] == []
    with pytest.raises(Blocked):   # no price yet
        await cmd(db, owner, "sales.mark_completed", {"sale_id": s["id"]})
    with pytest.raises(Blocked):   # manager cannot agree terms
        from backend.app.core.errors import Denied
        try:
            await cmd(db, manager, "sales.agree", {"sale_id": s["id"], "price": "8000.00", "currency": "USD"})
        except Denied:
            raise Blocked("denied")
    ag = (await cmd(db, owner, "sales.agree", {"sale_id": s["id"], "price": "8000.00", "currency": "USD", "sales_tax": "560.00", "pass_through": "150.00"})).data["sale"]
    assert ag["status"] == "agreed" and ag["price"]["amount"] == "8000.00" and ag["net_sale_value"]["amount"] == "8000.00"
    with pytest.raises(Blocked):   # delivery before completion
        await cmd(db, owner, "sales.record_delivery", {"sale_id": s["id"], "note": "handed over"})
    when = NOW - timedelta(days=2)
    done = (await cmd(db, owner, "sales.mark_completed", {"sale_id": s["id"], "completed_at": when.isoformat(), "source_ref": "bill_of_sale"})).data["sale"]
    assert done["status"] == "completed" and done["completed_at"][:10] == when.isoformat()[:10]
    await db.refresh(v)
    assert v.commercial_state == "sold" and v.allocation == "sold" and v.sold_at is not None and v.active_sale_id == s["id"]
    with pytest.raises(Blocked):   # delivery needs evidence
        await cmd(db, owner, "sales.record_delivery", {"sale_id": s["id"], "delivered_at": NOW.isoformat()})
    d = (await cmd(db, owner, "sales.record_delivery", {"sale_id": s["id"], "note": "keys handed over", "asset_ids": []})).data["sale"]
    assert d["status"] == "delivered" and d["handoff_evidence"][0]["note"] == "keys handed over"
    await db.refresh(v)
    assert v.commercial_state == "delivered" and v.delivered_at is not None
    cr = (await cmd(db, owner, "sales.record_credit", {"sale_id": s["id"], "amount": "500.00", "reason": "AC compressor goodwill"})).data
    assert cr["restated"] is True and cr["sale"]["net_sale_value"]["amount"] == "7500.00" and cr["sale"]["restatements"][0]["field"] == "credits"
    with pytest.raises(ValidationFailed):
        await cmd(db, owner, "sales.record_credit", {"sale_id": s["id"], "amount": "9000.00", "reason": "too much"})


async def test_cancel_creates_reconciliation_exception_without_refunding(db, owner):
    v = await vehicle(db)
    c = await contact(db)
    s = (await cmd(db, owner, "sales.reserve", {"vehicle_id": v.id, "buyer_contact_id": c.id})).data["sale"]
    inv = (await cmd(db, owner, "invoices.create", {"kind": "reservation", "sale_id": s["id"], "contact_id": c.id, "amount_due": "300.00", "currency": "USD"})).data["invoice"]
    pay = (await cmd(db, owner, "payments.record_manual_confirmed", {"amount": "300.00", "currency": "USD", "method": "cash", "evidence_ref": "receipt-7"})).data["payment"]
    prop = (await cmd(db, owner, "payments.propose_allocation", {"payment_id": pay["id"], "invoice_id": inv["id"]})).data["allocations"][0]
    await cmd(db, owner, "payments.confirm_allocation", {"allocation_id": prop["id"]})
    res = (await cmd(db, owner, "sales.cancel", {"sale_id": s["id"], "reason": "buyer withdrew"})).data
    assert res["cancelled"] and res["reconciliation_exception"]["invoices"][0]["allocated"] == "300.00"
    await db.refresh(v)
    assert v.active_sale_id is None and v.allocation == "inventory" and v.commercial_state == "not_listed"
    p = (await db.execute(select(Sale).where(Sale.id == s["id"]))).scalar_one()
    assert p.status == "cancelled" and not p.is_active
    from backend.app.models.finance import Invoice
    i = await db.get(Invoice, inv["id"])
    await db.refresh(i)
    assert i.amount_allocated == Decimal("300.00")                                       # nothing refunded automatically


async def test_expire_due_sweep_frees_vehicle(db, owner):
    v = await vehicle(db)
    c = await contact(db)
    s = (await cmd(db, owner, "sales.reserve", {"vehicle_id": v.id, "buyer_contact_id": c.id, "expires_at": (NOW - timedelta(minutes=1)).isoformat()})).data["sale"]
    n = await sr.expire_due(dbmod.SessionLocal)
    assert n >= 1
    row = await db.get(Sale, s["id"])
    await db.refresh(row)
    assert row.status == "expired" and not row.is_active and row.expired_at is not None
    await db.refresh(v)
    assert v.active_sale_id is None and v.allocation == "inventory"
    fresh = (await cmd(db, owner, "sales.reserve", {"vehicle_id": v.id, "buyer_contact_id": c.id})).data
    assert fresh["outcome"] == "reserved"
    with pytest.raises(Blocked):    # no expiry configured → cannot expire
        await cmd(db, owner, "sales.expire_reservation", {"sale_id": fresh["sale"]["id"]})


async def test_documents_conflict_requires_both_sources(db, owner):
    v = await vehicle(db)
    c = await contact(db)
    s = (await cmd(db, owner, "sales.reserve", {"vehicle_id": v.id, "buyer_contact_id": c.id})).data["sale"]
    await cmd(db, owner, "sales.update_checklist", {"sale_id": s["id"], "checklist": [{"type": "title", "required": True}, {"type": "bill_of_sale", "required": True}]})
    d = (await cmd(db, owner, "sales.add_document", {"sale_id": s["id"], "type": "title", "status": "pending"})).data["document"]
    with pytest.raises(Blocked):
        await cmd(db, owner, "documents.set_status", {"document_id": d["id"], "status": "on_file"})
    with pytest.raises(Blocked):
        await cmd(db, owner, "documents.set_status", {"document_id": d["id"], "status": "conflicted", "source": {"kind": "exporter", "value": "title #1"}})
    r = (await cmd(db, owner, "documents.set_status", {"document_id": d["id"], "status": "conflicted", "source": {"kind": "scan", "value": "title #2"}})).data
    assert r["document"]["status"] == "conflicted" and len(r["document"]["sources"]) == 2 and r["document"]["conflict"]["sources"]
    assert next(x for x in r["sale"]["documents_checklist"] if x["type"] == "title")["status"] == "conflicted"
    asset = Asset(kind="document", storage_key=f"docs/{_u()}.pdf", content_type="application/pdf", status="ready")
    db.add(asset)
    await db.commit()
    ok = (await cmd(db, owner, "documents.set_status", {"document_id": d["id"], "status": "on_file", "asset_id": asset.id})).data["document"]
    assert ok["status"] == "on_file" and ok["conflict"]["resolved_as"] == "on_file"
    docs = (await db.execute(select(Document).where(Document.entity_id == s["id"]))).scalars().all()
    assert len(docs) == 1


async def test_agreements_sent_needs_evidence_and_signed_needs_asset(db, owner):
    c = await contact(db)
    v = await vehicle(db)
    s = (await cmd(db, owner, "sales.reserve", {"vehicle_id": v.id, "buyer_contact_id": c.id})).data["sale"]
    a = (await cmd(db, owner, "agreements.create", {"kind": "sale", "contact_id": c.id, "sale_id": s["id"], "vehicle_id": v.id,
                                                    "deposit_amount": "500.00", "deposit_currency": "USD", "price_amount": "8000.00", "price_currency": "USD",
                                                    "terms": {"reservation_days": 10}})).data["agreement"]
    assert a["status"] == "draft" and a["agreement_version"] == 1
    with pytest.raises(ValidationFailed):
        await cmd(db, owner, "agreements.mark_sent", {"agreement_id": a["id"], "source_ref": ""})
    sent = (await cmd(db, owner, "agreements.mark_sent", {"agreement_id": a["id"], "source_ref": "gmail:msg-1"})).data["agreement"]
    assert sent["status"] == "sent" and sent["sent_evidence"]["source_ref"] == "gmail:msg-1"
    with pytest.raises(Blocked):
        await cmd(db, owner, "agreements.mark_signed", {"agreement_id": a["id"], "evidence_asset_id": "missing-asset"})
    asset = Asset(kind="document", storage_key=f"docs/{_u()}.pdf", content_type="application/pdf", status="ready")
    db.add(asset)
    await db.commit()
    signed = (await cmd(db, owner, "agreements.mark_signed", {"agreement_id": a["id"], "evidence_asset_id": asset.id})).data["agreement"]
    assert signed["status"] == "signed" and signed["evidence_asset_id"] == asset.id
    row = await db.get(Sale, s["id"])
    await db.refresh(row)
    assert row.agreement_id == a["id"] and row.price == Decimal("8000.00") and row.price_source == "agreement"
    a2 = (await cmd(db, owner, "agreements.create", {"kind": "sale", "contact_id": c.id, "sale_id": s["id"], "price_amount": "7800.00", "price_currency": "USD"})).data["agreement"]
    assert a2["agreement_version"] == 2 and a2["supersedes_id"] == a["id"]
    await cmd(db, owner, "agreements.mark_signed", {"agreement_id": a2["id"], "evidence_asset_id": asset.id})
    old = (await db.execute(select(sr.Agreement).where(sr.Agreement.id == a["id"]))).scalar_one()
    await db.refresh(old)
    assert old.status == "superseded"


async def test_sales_router_reserve_conflict_returns_409(client, db, owner):
    v = await vehicle(db)
    a, b = await contact(db), await contact(db)
    login(client, owner)
    r1 = await client.post("/api/finance/sales/reserve", json={"vehicle_id": v.id, "buyer_contact_id": a.id})
    assert r1.status_code == 200 and r1.json()["data"]["outcome"] == "reserved"
    r2 = await client.post("/api/finance/sales/reserve", json={"vehicle_id": v.id, "buyer_contact_id": b.id})
    assert r2.status_code == 409 and r2.json()["data"]["outcome"] == "conflict" and r2.json()["data"]["approval_id"]
    lst = await client.get(f"/api/finance/sales?vehicle_id={v.id}&active=true")
    assert lst.status_code == 200 and lst.json()["total"] == 1
    got = await client.get(f"/api/finance/sales/{r1.json()['data']['sale']['id']}")
    assert got.status_code == 200 and got.json()["sale"]["status"] == "reserved"


async def test_A03_sale_reads_hide_every_money_field_from_finance_status_only(client, db, owner, manager):
    """A03: a finance.status holder sees the sale's state, never its numbers — including the amounts carried inside
    terms, credit history, restatements and reconciliation exceptions."""
    v = await vehicle(db)
    c = await contact(db)
    agr = (await cmd(db, owner, "agreements.create", {"kind": "sale", "contact_id": c.id, "vehicle_id": v.id,
                                                      "price_amount": "8000.00", "price_currency": "USD",
                                                      "deposit_amount": "500.00", "deposit_currency": "USD",
                                                      "terms": {"reservation_days": 7, "reservation_amount": "500.00",
                                                                "shipping_amount": "1200.00"}})).data["agreement"]
    s = (await cmd(db, owner, "sales.reserve", {"vehicle_id": v.id, "buyer_contact_id": c.id, "agreement_id": agr["id"]})).data["sale"]
    await cmd(db, owner, "sales.agree", {"sale_id": s["id"], "price": "8000.00", "currency": "USD"})
    await cmd(db, owner, "sales.mark_completed", {"sale_id": s["id"]})
    await cmd(db, owner, "sales.record_credit", {"sale_id": s["id"], "amount": "250.00", "reason": "goodwill"})
    login(client, manager)
    body = (await client.get(f"/api/finance/sales/{s['id']}")).json()
    sale = body["sale"]
    assert sale["money_hidden"] is True and sale["price"] is None and sale["net_sale_value"] is None
    assert sale["status"] == "completed" and sale["terms"]["reservation_days"] == 7      # state and non-money terms stay
    for key in ("terms", "credits_history", "restatements", "exceptions"):
        blob = str(sale[key])
        assert not any(tok in blob for tok in ("8000", "500.00", "1200", "250")), f"money leaked in {key}"
    assert body["invoices"] == [] and body["agreement"] is None
    row = next(x for x in (await client.get("/api/finance/sales")).json()["items"] if x["id"] == s["id"])
    assert row["money_hidden"] is True and "250" not in str(row["credits_history"]) and "1200" not in str(row["terms"])
    login(client, owner)
    full = (await client.get(f"/api/finance/sales/{s['id']}")).json()["sale"]
    assert full["price"]["amount"] == "8000.00" and full["net_sale_value"]["amount"] == "7750.00"
    assert full["credits_history"][0]["amount"] == "250.00"
