"""Sales pipelines (spec §5.1/5.2): C01 two pipelines for one contact, ingestion dedupe, sold-vehicle inquiries,
evidence-backed Deposit Paid, C08 single conversion link, C02 lead card shows the next task, board API."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.app.core.errors import Blocked, Conflict, Denied, ValidationFailed
from backend.app.domain.commands import dispatch
from backend.app.models.runtime import Event
from backend.app.models.sales import STAGES, Opportunity
from backend.app.models.vehicles import Vehicle
from backend.app.services.sales import DEPOSIT_PAID_BLOCK_REASON
from backend.tests.conftest import ctx_for, login


def _u() -> str:
    return uuid.uuid4().hex[:8]


async def make_vehicle(db, stock_no: str, **kw) -> Vehicle:
    v = Vehicle(stock_no=stock_no, title=stock_no, make=kw.pop("make", "Daihatsu"), model=kw.pop("model", "Hijet"),
                model_year=kw.pop("model_year", 2001), color=kw.pop("color", "silver"), **kw)
    db.add(v)
    await db.commit()
    await db.refresh(v)
    return v


async def make_contact(db, user, name: str, **kw) -> dict:
    return (await dispatch(ctx_for(db, user), "contacts.create", {"name": name, **kw})).data["contact"]


# ── C01 ─────────────────────────────────────────────────────────────────────
async def test_C01_two_opportunities_for_one_contact_in_separate_pipelines(db, owner):
    tag = _u()
    c = await make_contact(db, owner, f"Buyer {tag}")
    v = await make_vehicle(db, f"STK-C1{tag[:4]}")
    irq = await dispatch(ctx_for(db, owner), "sales.create_opportunity",
                         {"contact_id": c["id"], "pipeline": "irq", "enquiry": "1998 Acty 4WD, budget 9k", "source": "web"})
    veh = await dispatch(ctx_for(db, owner), "sales.create_opportunity",
                         {"contact_id": c["id"], "pipeline": "vehicle", "vehicle_id": v.id, "source": "instagram"})
    assert irq.data["created"] and veh.data["created"]
    a, b = irq.data["opportunity"], veh.data["opportunity"]
    assert a["id"] != b["id"] and a["contact_id"] == b["contact_id"] == c["id"]
    assert a["pipeline"] == "irq" and b["pipeline"] == "vehicle" and a["stage"] == b["stage"] == "new"
    assert "meeting" not in STAGES and STAGES == ("new", "conversation", "awaiting_deposit", "deposit_paid", "lost")
    assert a["stage_history"][0]["stage"] == "new" and a["stage_history"][0]["by"] == owner.id
    evs = (await db.execute(select(Event).where(Event.type == "opportunity.changed", Event.aggregate_id == a["id"]))).scalars().all()
    assert evs and evs[0].payload["change"] == "created"
    # inline contact creation goes through contacts.create (no duplicate for a known identity)
    inline = await dispatch(ctx_for(db, owner), "sales.create_opportunity", {
        "contact": {"name": f"Inline {tag}", "identities": [{"kind": "email", "value": f"inline.{tag}@x.io"}]},
        "pipeline": "irq", "enquiry": "Sambar van"})
    assert inline.data["contact_created"] is True
    with pytest.raises(ValidationFailed):
        await dispatch(ctx_for(db, owner), "sales.create_opportunity", {"contact_id": c["id"], "pipeline": "vehicle"})
    with pytest.raises(ValidationFailed):
        await dispatch(ctx_for(db, owner), "sales.create_opportunity", {"contact_id": c["id"], "pipeline": "irq", "stage": "deposit_paid"})


async def test_repeated_ingestion_does_not_create_repeated_leads(db, owner):
    tag = _u()
    c = await make_contact(db, owner, f"Repeat {tag}")
    v = await make_vehicle(db, f"STK-R{tag[:5]}")
    first = await dispatch(ctx_for(db, owner), "sales.create_opportunity",
                           {"contact_id": c["id"], "pipeline": "vehicle", "vehicle_id": v.id, "source_ref": f"gmail:{tag}:1"})
    same_ref = await dispatch(ctx_for(db, owner), "sales.create_opportunity",
                              {"contact_id": c["id"], "pipeline": "vehicle", "vehicle_id": v.id, "source_ref": f"gmail:{tag}:1"})
    assert same_ref.data["created"] is False and same_ref.data["matched_by"] == "source_ref"
    new_msg = await dispatch(ctx_for(db, owner), "sales.create_opportunity",
                             {"contact_id": c["id"], "pipeline": "vehicle", "vehicle_id": v.id, "source_ref": f"gmail:{tag}:2",
                              "notes": "asked again about price"})
    assert new_msg.data["created"] is False and new_msg.data["matched_by"] == "open_duplicate"
    o = await db.get(Opportunity, first.data["opportunity"]["id"])
    await db.refresh(o)
    assert o.extra["also_from"] == [f"gmail:{tag}:2"] and "asked again" in o.notes
    irq1 = await dispatch(ctx_for(db, owner), "sales.create_opportunity", {"contact_id": c["id"], "pipeline": "irq", "enquiry": "Looking for a Jimny, JA11"})
    irq2 = await dispatch(ctx_for(db, owner), "sales.create_opportunity", {"contact_id": c["id"], "pipeline": "irq", "enquiry": "looking for a JIMNY ja11!"})
    irq3 = await dispatch(ctx_for(db, owner), "sales.create_opportunity", {"contact_id": c["id"], "pipeline": "irq", "enquiry": "Also a dump-bed Carry"})
    assert irq2.data["opportunity"]["id"] == irq1.data["opportunity"]["id"] and irq3.data["created"] is True
    rows = (await db.execute(select(Opportunity).where(Opportunity.contact_id == c["id"]))).scalars().all()
    assert len(rows) == 3


async def test_sold_vehicle_inquiry_creates_new_opportunity_and_never_revives_vehicle(db, owner):
    tag = _u()
    c = await make_contact(db, owner, f"Late buyer {tag}")
    sold = await make_vehicle(db, f"STK-S{tag[:5]}", commercial_state="sold", allocation="sold")
    version_before, state_before = sold.version, sold.commercial_state
    with pytest.raises(Blocked) as ei:
        await dispatch(ctx_for(db, owner), "sales.create_opportunity", {"contact_id": c["id"], "pipeline": "vehicle", "vehicle_id": sold.id})
    hint = ei.value.detail["hint"]
    assert hint["reason"] == "vehicle_sold" and hint["stock_no"] == sold.stock_no
    assert {o["pipeline"] for o in hint["options"]} == {"irq", "vehicle"}
    alt = await make_vehicle(db, f"STK-A{tag[:5]}")
    irq = await dispatch(ctx_for(db, owner), "sales.create_opportunity",
                         {"contact_id": c["id"], "pipeline": "irq", "enquiry": f"something like {sold.stock_no}", "source": "inbox"})
    alt_opp = await dispatch(ctx_for(db, owner), "sales.create_opportunity",
                             {"contact_id": c["id"], "pipeline": "vehicle", "vehicle_id": alt.id})
    assert irq.data["created"] and alt_opp.data["created"]
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "sales.link_vehicle", {"opportunity_id": irq.data["opportunity"]["id"], "vehicle_id": sold.id})
    await db.refresh(sold)
    assert sold.version == version_before and sold.commercial_state == state_before == "sold" and sold.allocation == "sold"
    assert (await db.execute(select(Opportunity).where(Opportunity.vehicle_id == sold.id))).scalars().all() == []


async def test_move_stage_history_lost_reopen_and_deposit_paid_blocked(db, owner):
    tag = _u()
    c = await make_contact(db, owner, f"Stages {tag}")
    o = (await dispatch(ctx_for(db, owner), "sales.create_opportunity", {"contact_id": c["id"], "pipeline": "irq", "enquiry": f"stages {tag}"})).data["opportunity"]
    r = await dispatch(ctx_for(db, owner), "sales.move_stage", {"opportunity_id": o["id"], "stage": "conversation", "expected_version": o["version"]})
    assert r.data["moved"] and r.data["opportunity"]["stage"] == "conversation"
    with pytest.raises(Conflict):
        await dispatch(ctx_for(db, owner), "sales.move_stage", {"opportunity_id": o["id"], "stage": "awaiting_deposit", "expected_version": o["version"]})
    r = await dispatch(ctx_for(db, owner), "sales.move_stage", {"opportunity_id": o["id"], "stage": "awaiting_deposit"})
    with pytest.raises(Blocked) as ei:
        await dispatch(ctx_for(db, owner), "sales.move_stage", {"opportunity_id": o["id"], "stage": "deposit_paid"})
    assert ei.value.detail["reason"] == DEPOSIT_PAID_BLOCK_REASON and ei.value.detail["action"] == "record_payment"
    with pytest.raises(ValidationFailed):
        await dispatch(ctx_for(db, owner), "sales.move_stage", {"opportunity_id": o["id"], "stage": "lost"})
    lost = await dispatch(ctx_for(db, owner), "sales.mark_lost", {"opportunity_id": o["id"], "reason": "bought elsewhere"})
    assert lost.data["opportunity"]["stage"] == "lost" and lost.data["opportunity"]["lost_reason"] == "bought elsewhere"
    hist = lost.data["opportunity"]["stage_history"]
    assert [h["stage"] for h in hist] == ["new", "conversation", "awaiting_deposit", "lost"]
    assert all(h["by"] == owner.id and h["at"] for h in hist)
    re = await dispatch(ctx_for(db, owner), "sales.reopen", {"opportunity_id": o["id"]})
    assert re.data["opportunity"]["stage"] == "awaiting_deposit" and re.data["opportunity"]["lost_reason"] is None
    assert re.data["opportunity"]["extra"]["lost_history"][0]["reason"] == "bought elsewhere"
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "sales.reopen", {"opportunity_id": o["id"]})
    same = await dispatch(ctx_for(db, owner), "sales.move_stage", {"opportunity_id": o["id"], "stage": "awaiting_deposit"})
    assert same.data["moved"] is False


# ── C08 ─────────────────────────────────────────────────────────────────────
async def test_C08_set_conversion_is_idempotent_and_conflicts_on_a_different_target(db, owner, manager):
    tag = _u()
    c = await make_contact(db, owner, f"Deposit {tag}")
    o = (await dispatch(ctx_for(db, owner), "sales.create_opportunity", {"contact_id": c["id"], "pipeline": "irq", "enquiry": f"deposit {tag}"})).data["opportunity"]
    payload = {"opportunity_id": o["id"], "converted_kind": "import_request", "converted_id": f"ir-{tag}",
               "source_ref": f"square:payment:{tag}", "deposit_payment_id": f"pay-{tag}"}
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, manager), "sales.set_conversion", payload)   # not settable by hand without finance.write
    first = await dispatch(ctx_for(db, owner), "sales.set_conversion", payload)
    assert first.data["converted"] is True
    op = first.data["opportunity"]
    assert op["stage"] == "deposit_paid" and op["converted_id"] == f"ir-{tag}" and op["import_request_id"] == f"ir-{tag}"
    assert op["deposit_confirmed_at"] and op["conversion_source_ref"] == f"square:payment:{tag}"
    second = await dispatch(ctx_for(db, owner), "sales.set_conversion", payload)   # deposit event processed twice
    assert second.data["converted"] is False and second.data["idempotent"] is True
    assert second.data["opportunity"]["version"] == op["version"]
    with pytest.raises(Conflict):
        await dispatch(ctx_for(db, owner), "sales.set_conversion", {**payload, "converted_id": f"ir-other-{tag}"})
    with pytest.raises(Conflict):
        await dispatch(ctx_for(db, owner), "sales.set_conversion", {**payload, "converted_kind": "sale"})
    # a second opportunity cannot claim the same converted record
    o2 = (await dispatch(ctx_for(db, owner), "sales.create_opportunity", {"contact_id": c["id"], "pipeline": "irq", "enquiry": f"second {tag}"})).data["opportunity"]
    with pytest.raises(Conflict):
        await dispatch(ctx_for(db, owner), "sales.set_conversion", {**payload, "opportunity_id": o2["id"]})
    # once converted, the stage is not hand-editable
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "sales.move_stage", {"opportunity_id": o["id"], "stage": "conversation"})
    hist = [h["stage"] for h in (await db.get(Opportunity, o["id"])).stage_history]
    assert hist.count("deposit_paid") == 1
    evs = (await db.execute(select(Event).where(Event.type == "opportunity.changed", Event.aggregate_id == o["id"]))).scalars().all()
    assert [e.payload["change"] for e in evs].count("converted") == 1


# ── C02 + board API ─────────────────────────────────────────────────────────
async def test_C02_call_with_15m_reminder_appears_in_tasks_and_on_lead_card(client, db, owner, manager):
    tag = _u()
    login(client, owner)
    c = await make_contact(db, owner, f"Card {tag}")
    v = await make_vehicle(db, f"STK-B{tag[:5]}", model_year=1997, make="Honda", model="Acty", color="blue")
    o = (await client.post("/api/sales/opportunities", json={"contact_id": c["id"], "pipeline": "vehicle", "vehicle_id": v.id,
                                                              "owner_user_id": manager.id})).json()["data"]["opportunity"]
    due = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(microsecond=0)
    r = await client.post("/api/tasks", json={"title": f"Call about {v.stock_no}", "type": "call", "opportunity_id": o["id"],
                                              "contact_id": c["id"], "vehicle_id": v.id, "owner_user_id": manager.id,
                                              "due_at": due.isoformat(), "timezone": "America/Phoenix", "reminder_kind": "15m"})
    assert r.status_code == 200, r.text
    t = r.json()["data"]["task"]
    assert t["reminder_kind"] == "15m" and t["timezone"] == "America/Phoenix" and t["due_at"] == due.isoformat()
    assert t["local_due"].endswith("AZ") and "upcoming" in t["buckets"]
    evs = (await db.execute(select(Event).where(Event.type == "task.changed", Event.aggregate_id == t["id"]))).scalars().all()
    assert evs and evs[0].payload["change"] == "created" and evs[0].payload["schedule_revision"] == 1
    r = await client.get("/api/tasks", params={"view": "all", "opportunity_id": o["id"]})
    assert [x["id"] for x in r.json()["items"]] == [t["id"]]
    board = (await client.get("/api/sales/board", params={"pipeline": "vehicle"})).json()
    assert [col["stage"] for col in board["columns"]] == ["new", "conversation", "awaiting_deposit", "deposit_paid"]
    assert "meeting" not in board["stages"]
    card = next(x for x in board["columns"][0]["items"] if x["id"] == o["id"])
    assert card["name"] == f"Card {tag}" and card["subject"] == f"1997 Honda Acty blue · {v.stock_no}"
    assert card["next_task"]["id"] == t["id"] and card["next_task"]["reminder_kind"] == "15m" and card["overdue"] is False
    assert card["owner"]["id"] == manager.id and card["age"].endswith("m")
    # an overdue call is obvious on the card; a lead with no task says so truthfully
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    await client.post(f"/api/tasks/{t['id']}/reschedule", json={"due_at": past})
    o2 = (await client.post("/api/sales/opportunities", json={"contact_id": c["id"], "pipeline": "irq", "enquiry": f"no task {tag}"})).json()["data"]["opportunity"]
    board = (await client.get("/api/sales/board", params={"pipeline": "vehicle"})).json()
    card = next(x for x in board["columns"][0]["items"] if x["id"] == o["id"])
    assert card["overdue"] is True and card["next_task"]["overdue"] is True
    board_irq = (await client.get("/api/sales/board", params={"pipeline": "irq"})).json()
    card2 = next(x for x in board_irq["columns"][0]["items"] if x["id"] == o2["id"])
    assert card2["next_task"] is None and card2["next_action_label"] == "No next action"
    detail = (await client.get(f"/api/sales/opportunities/{o['id']}")).json()
    assert detail["tasks"]["next"]["id"] == t["id"] and detail["deposit"]["state"] == "not_recorded"
    assert detail["vehicle"]["photo"] == "No photo yet" and detail["conversion"] is None
    assert [s["stage"] for s in detail["stages"] if not s["hand_settable"]] == ["deposit_paid"]


async def test_sales_api_permissions_and_money(client, db, owner, manager, mechanic):
    tag = _u()
    login(client, owner)
    c = await make_contact(db, owner, f"Perm {tag}")
    r = await client.post("/api/sales/opportunities", json={"contact_id": c["id"], "pipeline": "irq", "enquiry": f"perm {tag}",
                                                            "budget_amount": "12000", "budget_currency": "USD"})
    oid = r.json()["data"]["opportunity"]["id"]
    r = await client.post(f"/api/sales/opportunities/{oid}/move-stage", json={"stage": "deposit_paid"})
    assert r.status_code == 409 and r.json()["action"] == "record_payment"
    r = await client.get("/api/sales/opportunities", params={"q": tag})
    assert r.json()["total"] == 1 and r.json()["items"][0]["budget_amount"] == "12000.00"
    login(client, manager)
    r = await client.get("/api/sales/opportunities", params={"q": tag})
    assert r.status_code == 200 and r.json()["items"][0]["budget_amount"] is None and r.json()["items"][0]["money_hidden"]
    r = await client.post(f"/api/sales/opportunities/{oid}/move-stage", json={"stage": "conversation"})
    assert r.status_code == 200 and r.json()["data"]["opportunity"]["stage"] == "conversation"
    # Deposit Paid is never a hand action over HTTP (spec §5.2): not for a manager, not even for the owner
    payload = {"converted_kind": "sale", "converted_id": f"s-{tag}"}
    assert (await client.post(f"/api/sales/opportunities/{oid}/set-conversion", json=payload)).status_code == 404
    login(client, owner)
    assert (await client.post(f"/api/sales/opportunities/{oid}/set-conversion", json=payload)).status_code == 404
    assert (await client.get(f"/api/sales/opportunities/{oid}")).json()["opportunity"]["converted_id"] is None
    login(client, mechanic)
    assert (await client.get("/api/sales/board")).status_code == 403
    assert (await client.get(f"/api/sales/opportunities/{oid}")).status_code == 403
    r = await client.post(f"/api/sales/opportunities/{oid}/move-stage", json={"stage": "lost", "reason": "x"})
    assert r.status_code == 403


# ── one thread, several interests; lost leads leave cleanly; conversion guards; external scope ─────
async def test_one_thread_links_multiple_interests_and_conversion_fixes_the_links(db, owner):
    tag = _u()
    c = await make_contact(db, owner, f"Multi {tag}")
    v1 = await make_vehicle(db, f"STK-M1{tag[:5]}")
    v2 = await make_vehicle(db, f"STK-M2{tag[:5]}")
    ref = f"gmail:{tag}:thread"
    first = await dispatch(ctx_for(db, owner), "sales.create_opportunity", {"contact_id": c["id"], "pipeline": "vehicle", "vehicle_id": v1.id, "source_ref": ref})
    second = await dispatch(ctx_for(db, owner), "sales.create_opportunity", {"contact_id": c["id"], "pipeline": "vehicle", "vehicle_id": v2.id, "source_ref": ref})
    irq = await dispatch(ctx_for(db, owner), "sales.create_opportunity", {"contact_id": c["id"], "pipeline": "irq", "enquiry": "or something similar", "source_ref": ref})
    assert first.data["created"] and second.data["created"] and irq.data["created"]   # spec §5.1: one thread, several interests
    replay = await dispatch(ctx_for(db, owner), "sales.create_opportunity", {"contact_id": c["id"], "pipeline": "vehicle", "vehicle_id": v1.id, "source_ref": ref})
    assert replay.data["created"] is False and replay.data["opportunity"]["id"] == first.data["opportunity"]["id"]
    assert len((await db.execute(select(Opportunity).where(Opportunity.source_ref == ref))).scalars().all()) == 3
    with pytest.raises(ValidationFailed):
        await dispatch(ctx_for(db, owner), "sales.create_opportunity", {"contact_id": c["id"], "pipeline": "irq", "enquiry": "x", "owner_user_id": "nobody"})
    # dragging a lost lead back onto the board clears the lost state; the reason stays in history
    o1 = first.data["opportunity"]["id"]
    await dispatch(ctx_for(db, owner), "sales.mark_lost", {"opportunity_id": o1, "reason": "went quiet"})
    back = await dispatch(ctx_for(db, owner), "sales.move_stage", {"opportunity_id": o1, "stage": "conversation"})
    op = back.data["opportunity"]
    assert op["stage"] == "conversation" and op["lost_reason"] is None and op["lost_at"] is None and op["reopened_at"]
    assert op["extra"]["lost_history"][0]["reason"] == "went quiet"
    # once converted, the vehicle / request link is fixed by the conversion record (invariant 6)
    await dispatch(ctx_for(db, owner), "sales.set_conversion", {"opportunity_id": o1, "converted_kind": "sale", "converted_id": f"sale-{tag}",
                                                                 "source_ref": f"square:payment:{tag}"})
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "sales.link_vehicle", {"opportunity_id": o1, "vehicle_id": v2.id})
    same = await dispatch(ctx_for(db, owner), "sales.link_vehicle", {"opportunity_id": o1, "vehicle_id": v1.id})
    assert same.data["linked"] is False
    oi = irq.data["opportunity"]["id"]
    await dispatch(ctx_for(db, owner), "sales.set_conversion", {"opportunity_id": oi, "converted_kind": "import_request", "converted_id": f"ir-{tag}"})
    with pytest.raises(Conflict):
        await dispatch(ctx_for(db, owner), "sales.link_request", {"opportunity_id": oi, "import_request_id": f"ir-other-{tag}"})
    assert (await dispatch(ctx_for(db, owner), "sales.link_request", {"opportunity_id": oi, "import_request_id": f"ir-{tag}"})).data["linked"] is False
    assert (await db.get(Vehicle, v1.id)).commercial_state == "not_listed"   # sales never touches the vehicle record


async def test_external_client_scope_excludes_leads_outside_its_vehicle_grant(db, owner):
    from backend.app.domain.actors import Actor
    from backend.app.domain.policy import effective_perms
    from backend.app.routers.sales import _scope_clause
    tag = _u()
    c = await make_contact(db, owner, f"Scoped {tag}")
    v_in = await make_vehicle(db, f"STK-E1{tag[:5]}")
    v_out = await make_vehicle(db, f"STK-E2{tag[:5]}")
    for payload in ({"pipeline": "vehicle", "vehicle_id": v_in.id}, {"pipeline": "vehicle", "vehicle_id": v_out.id},
                    {"pipeline": "irq", "enquiry": f"owner's own lead {tag}", "owner_user_id": owner.id}):
        await dispatch(ctx_for(db, owner), "sales.create_opportunity", {"contact_id": c["id"], **payload})
    client = Actor(kind="external", user_id=owner.id, role="owner", scope="all", perms=effective_perms("owner", {}),
                   client_id=f"client-{tag}", client_scopes=["read:sales"], client_record_scope={"vehicle_ids": [v_in.id]})
    clause = await _scope_clause(db, client)
    rows = (await db.execute(select(Opportunity).where(Opportunity.contact_id == c["id"], clause))).scalars().all()
    assert [o.vehicle_id for o in rows] == [v_in.id]   # not the other vehicle, not the owner's IRQ lead
