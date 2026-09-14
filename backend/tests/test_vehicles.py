"""Vehicles domain (spec §2.3, §3.1, §3.2, §2.4): stock allocation and honest incomplete identity, facts with
provenance and owner-only confirmation, independent states, sourced milestones, versioned condition, health
projection, archive/restore, money permissions, saved views and the Manager capability map (I06)."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.app.core.errors import Blocked, Conflict, Denied, ValidationFailed
from backend.app.domain.commands import REGISTRY, dispatch
from backend.app.models.runtime import Approval, Event
from backend.app.models.tasks import Task
from backend.app.models.vehicles import Vehicle, VehicleFact, VehicleMilestone
from backend.app.services import vehicles as svc
from backend.tests.conftest import ctx_for, login, make_user, run_worker_once
from backend.tests.test_assets import drain_outbox, jpeg_bytes, upload_via_commands


def _u() -> str:
    return uuid.uuid4().hex[:8]


async def _create(db, user, **kw) -> dict:
    return (await dispatch(ctx_for(db, user), "vehicles.create", kw)).data


# ── creation / identity ─────────────────────────────────────────────────────
async def test_create_allocates_stock_no_and_never_invents_identity(db, owner):
    data = await _create(db, owner, make="Daihatsu", model="Hijet", color="white")
    v = data["vehicle"]
    assert re.fullmatch(r"STK-\d{4}", v["stock_no"]) and data["created"] is True
    assert v["intake_status"] == "incomplete" and set(v["missing_identity_fields"]) == {"frame_no", "model_year", "purchase_amount"}
    assert v["frame_no_raw"] is None and v["purchase_amount"] is None and v["dates"]["acquired_at"] is None
    assert v["photo"] == "No photo yet" and v["states"] == {"logistics": "purchased", "recon": "needs_inspection", "commercial": "not_listed", "documents": "pending"}
    assert {f["key"] for f in data["facts"]} == {"make", "model", "color"} and all(f["status"] == "reported" for f in data["facts"])
    assert data["missing_identity_task"]["title"] == "Record missing vehicle identity"
    # a second card gets the next number; an explicit stock number is normalized and must be unique
    v2 = (await _create(db, owner, make="Suzuki", model="Carry"))["vehicle"]
    assert int(v2["stock_no"][4:]) == int(v["stock_no"][4:]) + 1
    with pytest.raises(Conflict):
        await _create(db, owner, stock_no=v["stock_no"].lower())
    # create_key makes the creation retry-safe (invariant 13); a known frame number blocks a duplicate card
    key = f"create-{_u()}"
    a = await _create(db, owner, make="Honda", model="Acty", frame_no_raw="HA4-1234567", create_key=key)
    b = await _create(db, owner, make="Honda", model="Acty", frame_no_raw="HA4-1234567", create_key=key)
    assert a["vehicle"]["id"] == b["vehicle"]["id"] and b["created"] is False
    assert a["vehicle"]["frame_no_norm"] == "HA41234567" and a["vehicle"]["frame_no_raw"] == "HA4-1234567"
    with pytest.raises(Conflict):
        await _create(db, owner, make="Honda", model="Acty", frame_no_raw="ha4 1234567")


async def test_update_writes_facts_with_provenance_and_versions(db, owner, manager):
    v = (await _create(db, owner, make="Subaru", model="Sambar"))["vehicle"]
    res = await dispatch(ctx_for(db, manager), "vehicles.update", {"vehicle_id": v["id"], "expected_version": v["version"], "color": "blue",
                                                                  "location": "Bay 2", "source_kind": "manual", "reason": "seen in the yard"})
    assert res.data["changed"]["color"] == {"from": None, "to": "blue"} and res.data["vehicle"]["version"] == v["version"] + 1
    assert {(f["key"], f["status"], f["source_kind"]) for f in res.data["facts"]} == {("color", "reported", "manual"), ("location", "reported", "manual")}
    with pytest.raises(Conflict):
        await dispatch(ctx_for(db, manager), "vehicles.update", {"vehicle_id": v["id"], "expected_version": v["version"], "color": "red"})
    # owner-confirmed edit supersedes the reported fact and keeps history
    res = await dispatch(ctx_for(db, owner), "vehicles.update", {"vehicle_id": v["id"], "color": "navy blue", "confirmed": True})
    facts = (await db.execute(select(VehicleFact).where(VehicleFact.vehicle_id == v["id"], VehicleFact.key == "color").order_by(VehicleFact.created_at))).scalars().all()
    assert [(f.value, f.status, f.is_current) for f in facts] == [("blue", "outdated", False), ("navy blue", "confirmed", True)]
    assert facts[1].supersedes_id == facts[0].id and facts[1].actor == owner.id
    # mechanic has no vehicles.write
    mech = await make_user(db, f"mech-{_u()}", "mechanic")
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, mech), "vehicles.update", {"vehicle_id": v["id"], "color": "green"})


async def test_independent_states_with_reason_and_history(db, owner):
    v = (await _create(db, owner, make="Mazda", model="Scrum"))["vehicle"]
    with pytest.raises(ValidationFailed):
        await dispatch(ctx_for(db, owner), "vehicles.set_states", {"vehicle_id": v["id"], "recon_state": "in_recon", "reason": "x"})
    res = await dispatch(ctx_for(db, owner), "vehicles.set_states", {"vehicle_id": v["id"], "logistics_state": "on_vessel",
                                                                    "documents_state": "missing", "reason": "exporter update"})
    veh = res.data["vehicle"]
    assert veh["states"]["logistics"] == "on_vessel" and veh["states"]["documents"] == "missing" and veh["states"]["recon"] == "needs_inspection"
    assert [c["dimension"] for c in res.data["changed"]] == ["logistics", "documents"]
    assert veh["view"] == "shipping" and veh["health"] == "risk" and "documents missing" in veh["health_reason"]
    evs = (await db.execute(select(Event).where(Event.type == "vehicle.state_changed", Event.aggregate_id == v["id"]))).scalars().all()
    assert {(e.payload["dimension"], e.payload["to"]) for e in evs} >= {("logistics", "on_vessel"), ("documents", "missing")}
    # backward move is allowed with the reason kept in history
    res = await dispatch(ctx_for(db, owner), "vehicles.set_states", {"vehicle_id": v["id"], "logistics_state": "purchased", "reason": "wrong vessel"})
    assert res.data["changed"][0]["backward"] is True and res.data["vehicle"]["state_history"][-1]["reason"] == "wrong vessel"
    with pytest.raises(ValidationFailed):
        await dispatch(ctx_for(db, owner), "vehicles.set_states", {"vehicle_id": v["id"], "logistics_state": "teleported", "reason": "x"})


async def test_milestones_need_sourced_time_and_supersede(db, owner):
    v = (await _create(db, owner, make="Daihatsu", model="Hijet"))["vehicle"]
    with pytest.raises(ValidationFailed):   # completed needs a time
        await dispatch(ctx_for(db, owner), "vehicles.record_milestone", {"vehicle_id": v["id"], "kind": "received", "status": "completed", "source_kind": "owner_reported"})
    with pytest.raises(ValidationFailed):   # completed needs a source
        await dispatch(ctx_for(db, owner), "vehicles.record_milestone", {"vehicle_id": v["id"], "kind": "received", "status": "completed",
                                                                        "at": datetime.now(timezone.utc).isoformat(), "source_kind": "unknown"})
    eta = (datetime.now(timezone.utc) + timedelta(days=10)).replace(microsecond=0)
    est = await dispatch(ctx_for(db, owner), "vehicles.record_milestone", {"vehicle_id": v["id"], "kind": "arrived_port", "status": "estimated",
                                                                          "at": eta.isoformat(), "source_kind": "carrier", "source_ref": "ETA mail"})
    assert est.data["milestone"]["status"] == "estimated" and est.data["vehicle"]["dates"]["received_at"] is None
    # "arrived today" from the owner: received completed with source owner_reported; inspection/readiness untouched
    now = datetime.now(timezone.utc).replace(microsecond=0)
    got = await dispatch(ctx_for(db, owner), "vehicles.record_milestone", {"vehicle_id": v["id"], "kind": "received", "status": "completed",
                                                                          "at": now.isoformat(), "source_kind": "owner_reported"})
    veh = got.data["vehicle"]
    assert got.data["milestone"]["source_kind"] == "owner_reported" and veh["dates"]["received_at"] == now.isoformat()
    assert veh["states"]["logistics"] == "received" and veh["states"]["recon"] == "needs_inspection"
    assert veh["dates"]["inspected_at"] is None and veh["dates"]["ready_at"] is None
    # a corrected time supersedes the prior milestone but keeps it in history
    later = now - timedelta(hours=3)
    got2 = await dispatch(ctx_for(db, owner), "vehicles.record_milestone", {"vehicle_id": v["id"], "kind": "received", "status": "completed",
                                                                           "at": later.isoformat(), "source_kind": "document", "source_ref": "gate pass"})
    rows = (await db.execute(select(VehicleMilestone).where(VehicleMilestone.vehicle_id == v["id"], VehicleMilestone.kind == "received"))).scalars().all()
    cur = [m for m in rows if m.is_current]
    assert len(rows) == 2 and len(cur) == 1 and cur[0].supersedes_id == got.data["milestone"]["id"] and got2.data["vehicle"]["dates"]["received_at"] == later.isoformat()
    with pytest.raises(Blocked):   # cannot downgrade a completed milestone
        await dispatch(ctx_for(db, owner), "vehicles.record_milestone", {"vehicle_id": v["id"], "kind": "received", "status": "planned", "source_kind": "manual"})
    tl = await svc.timeline(db, await db.get(Vehicle, v["id"]))
    assert [m["kind"] for m in tl["milestones"]] == ["received", "arrived_port"] and len(tl["history"]) == 1 and tl["acquisition_label"] == "Not recorded"
    ev = (await db.execute(select(Event).where(Event.type == "milestone.changed", Event.aggregate_id == v["id"]))).scalars().all()
    assert len(ev) == 3


async def test_facts_conflict_and_owner_only_confirmation(db, owner, manager):
    v = (await _create(db, owner, make="Suzuki", model="Carry"))["vehicle"]
    r1 = await dispatch(ctx_for(db, manager), "vehicles.propose_fact", {"vehicle_id": v["id"], "key": "odometer_km", "value": "85000",
                                                                       "source_kind": "auction_sheet", "source_ref": "sheet.pdf"})
    assert r1.data["outcome"] == "recorded" and r1.data["vehicle"]["odometer_km"] == 85000 and r1.data["needs_confirmation"] is True
    r2 = await dispatch(ctx_for(db, manager), "vehicles.propose_fact", {"vehicle_id": v["id"], "key": "odometer_km", "value": "90,000",
                                                                       "source_kind": "message", "source_ref": "email 42"})
    assert r2.data["outcome"] == "conflicted" and r2.data["fact"]["status"] == "conflicted" and r2.data["fact"]["conflict_with_id"] == r1.data["fact"]["id"]
    assert r2.data["vehicle"]["odometer_km"] == 85000   # never overwritten silently
    same = await dispatch(ctx_for(db, manager), "vehicles.propose_fact", {"vehicle_id": v["id"], "key": "odometer_km", "value": "85,000", "source_kind": "document"})
    assert same.data["outcome"] == "restated"
    with pytest.raises(Denied):   # confirmation is owner-only
        await dispatch(ctx_for(db, manager), "vehicles.confirm_fact", {"vehicle_id": v["id"], "fact_id": r2.data["fact"]["id"]})
    agent = await dispatch(ctx_for(db, owner, kind="agent"), "vehicles.confirm_fact", {"vehicle_id": v["id"], "fact_id": r2.data["fact"]["id"]})
    assert agent.status == "needs_review" and agent.approval_id   # Manager prepares the exact change for the owner
    ok = await dispatch(ctx_for(db, owner), "vehicles.confirm_fact", {"vehicle_id": v["id"], "fact_id": r2.data["fact"]["id"], "note": "read on the cluster"})
    assert ok.data["fact"]["status"] == "confirmed" and ok.data["vehicle"]["odometer_km"] == 90000
    facts = (await db.execute(select(VehicleFact).where(VehicleFact.vehicle_id == v["id"], VehicleFact.key == "odometer_km"))).scalars().all()
    assert sorted(f.status for f in facts) == ["confirmed", "outdated", "outdated"] and [f for f in facts if f.is_current][0].status == "confirmed"
    with pytest.raises(ValidationFailed):
        await dispatch(ctx_for(db, owner), "vehicles.propose_fact", {"vehicle_id": v["id"], "key": "purchase_amount", "value": "1000", "status": "confirmed"})
    # frame number proposal fills the empty identity field and clears it from the missing list
    fr = await dispatch(ctx_for(db, owner), "vehicles.propose_fact", {"vehicle_id": v["id"], "key": "frame_no", "value": "DA63T-501234", "source_kind": "document"})
    assert fr.data["vehicle"]["frame_no_raw"] == "DA63T-501234" and "frame_no" not in fr.data["vehicle"]["missing_identity_fields"]


async def test_condition_versions_merge_and_removal_keeps_evidence(db, owner):
    v = (await _create(db, owner, make="Honda", model="Acty"))["vehicle"]
    photo = await upload_via_commands(db, owner, jpeg_bytes(51), "dent.jpg")
    res = await dispatch(ctx_for(db, owner), "vehicles.set_condition", {"vehicle_id": v["id"], "bullets": [
        {"text": "Dent on left door", "source": "owner_reported", "evidence": [photo]},
        {"text": "A/C not cold", "source": "owner_reported"}], "source_ref": "intake:test"})
    assert len(res.data["added"]) == 2 and res.data["condition_version"] == 1
    again = await dispatch(ctx_for(db, owner), "vehicles.set_condition", {"vehicle_id": v["id"], "bullets": [
        {"text": "dent on left door.", "source": "owner_reported", "evidence": [photo]}]})
    assert again.data["added"] == [] and again.data["updated"] == [] and len(again.data["bullets"]) == 2   # equivalent bullet merges, no dup
    bid = res.data["added"][0]
    ed = await dispatch(ctx_for(db, owner), "vehicles.edit_condition_bullet", {"vehicle_id": v["id"], "bullet_id": bid, "text": "Dent on left door (small)", "reason": "typo"})
    assert ed.data["bullet"]["history"][0]["text_before"] == "Dent on left door" and ed.data["condition_version"] == 2
    rm = await dispatch(ctx_for(db, owner), "vehicles.edit_condition_bullet", {"vehicle_id": v["id"], "bullet_id": bid, "remove": True, "reason": "wrong truck"})
    row = await db.get(Vehicle, v["id"])
    await db.refresh(row)
    removed = [b for b in row.condition_summary if b["id"] == bid][0]
    assert removed["removed"] is True and removed["evidence"] == [photo] and len(svc.current_bullets(row)) == 1
    assert [h["version"] for h in row.condition_history] == [1, 2, 3]
    with pytest.raises(ValidationFailed):
        await dispatch(ctx_for(db, owner), "vehicles.set_condition", {"vehicle_id": v["id"], "bullets": [{"text": "x", "source": "owner_reported", "evidence": ["missing-asset"]}]})


async def test_health_projection_from_tasks(db, owner, mechanic):
    v = (await _create(db, owner, make="Daihatsu", model="Hijet", create_missing_task=False))["vehicle"]
    assert v["health"] == "ok" and v["next_action"] is None
    t = (await dispatch(ctx_for(db, owner), "tasks.create", {"title": f"Replace belt {_u()}", "vehicle_id": v["id"]})).data["task"]
    await drain_outbox()   # task.changed -> recompute
    row = await db.get(Vehicle, v["id"])
    await db.refresh(row)
    assert row.health == "risk" and "unassigned" in row.health_reason and row.next_action == t["title"]
    await dispatch(ctx_for(db, owner), "tasks.assign", {"task_id": t["id"], "owner_user_id": mechanic.id})
    await drain_outbox()
    await db.refresh(row)
    assert row.health == "ok" and row.next_action_owner_id == mechanic.id
    await dispatch(ctx_for(db, mechanic), "tasks.report_blocker", {"task_id": t["id"], "reason": "belt on backorder"})
    await drain_outbox()
    await db.refresh(row)
    assert row.health == "blocked" and "backorder" in row.health_reason and row.exception_summary
    await dispatch(ctx_for(db, mechanic), "tasks.start", {"task_id": t["id"]})
    await dispatch(ctx_for(db, owner), "tasks.reschedule", {"task_id": t["id"], "due_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()})
    await drain_outbox()
    await db.refresh(row)
    assert row.health == "risk" and "overdue" in row.health_reason
    await dispatch(ctx_for(db, owner), "tasks.update", {"task_id": t["id"], "evidence_required": [{"kind": "note", "min": 1}]})
    await dispatch(ctx_for(db, mechanic), "tasks.attach_evidence", {"task_id": t["id"], "note": "done"})
    await dispatch(ctx_for(db, mechanic), "tasks.complete", {"task_id": t["id"]})
    await drain_outbox()
    await db.refresh(row)
    assert row.health == "wait" and "awaiting verification" in row.health_reason
    await dispatch(ctx_for(db, owner), "tasks.verify", {"task_id": t["id"]})
    await drain_outbox()
    await db.refresh(row)
    assert row.health == "ok" and row.next_action is None


async def test_archive_restore_and_asking_price(db, owner, manager):
    v = (await _create(db, owner, make="Nissan", model="Clipper"))["vehicle"]
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, manager), "vehicles.set_asking_price", {"vehicle_id": v["id"], "amount": "9500", "currency": "usd"})
    ag = await dispatch(ctx_for(db, owner, kind="agent"), "vehicles.set_asking_price", {"vehicle_id": v["id"], "amount": "9500", "currency": "usd"})
    assert ag.status == "needs_review" and (await db.get(Approval, ag.approval_id)).kind == "price_change"
    pr = await dispatch(ctx_for(db, owner), "vehicles.set_asking_price", {"vehicle_id": v["id"], "amount": "9500", "currency": "usd"})
    assert pr.data["vehicle"]["asking_price"] == "9500.00" and pr.data["vehicle"]["asking_currency"] == "USD" and pr.data["vehicle"]["price_approved_at"]
    ar = await dispatch(ctx_for(db, owner), "vehicles.archive", {"vehicle_id": v["id"], "reason": "duplicate card"})
    assert ar.data["archived"] is True and len(ar.data["tasks_cancelled"]) == 1   # the missing-identity task
    tasks = (await db.execute(select(Task).where(Task.vehicle_id == v["id"]))).scalars().all()
    assert all(t.status == "cancelled" for t in tasks)
    rs = await dispatch(ctx_for(db, owner), "vehicles.restore", {"vehicle_id": v["id"]})
    assert rs.data["restored"] is True and rs.data["vehicle"]["archived_at"] is None


# ── API: views, detail tabs, money permission, record scope ─────────────────
async def test_api_views_detail_tabs_and_money_permission(client, db, owner, manager, mechanic):
    tag = _u()
    v = (await _create(db, owner, make="Daihatsu", model=f"Hijet {tag}", purchase_amount="4200.00", purchase_currency="USD",
                       logistics_state="received"))["vehicle"]
    shipping = (await _create(db, owner, make="Suzuki", model=f"Carry {tag}", logistics_state="on_vessel"))["vehicle"]
    login(client, owner)
    r = await client.get("/api/vehicles", params={"view": "shop", "q": tag})
    assert r.status_code == 200 and [x["id"] for x in r.json()["items"]] == [v["id"]] and r.json()["total"] == 1
    r = await client.get("/api/vehicles", params={"view": "shipping", "q": tag})
    assert [x["id"] for x in r.json()["items"]] == [shipping["id"]]
    r = await client.get("/api/vehicles", params={"view": "nope"})
    assert r.status_code == 422
    r = await client.get("/api/vehicles", params={"q": v["stock_no"].lower()})
    assert [x["id"] for x in r.json()["items"]] == [v["id"]]
    r = await client.get(f"/api/vehicles/{v['id']}")
    d = r.json()
    assert set(d["tabs"]) == {"overview", "work", "files", "sale", "money"} and d["vehicle"]["purchase_amount"] == "4200.00"
    assert d["tabs"]["money"]["money_hidden"] is False and d["tabs"]["files"]["photo_status"] == "No photo yet"
    assert d["tabs"]["overview"]["identity"]["intake_status"] == "incomplete"
    r = await client.get(f"/api/vehicles/{v['id']}/timeline")
    assert r.status_code == 200 and r.json()["acquisition_label"] == "Not recorded"
    # manager: no costs.read -> money hidden everywhere, purchase facts filtered
    login(client, manager)
    d = (await client.get(f"/api/vehicles/{v['id']}")).json()
    assert d["vehicle"]["purchase_amount"] is None and d["vehicle"]["money_hidden"] is True and d["tabs"]["money"] == {"money_hidden": True}
    assert not [f for f in d["tabs"]["overview"]["facts"] if f["key"] == "purchase_amount"]
    r = await client.post(f"/api/vehicles/{v['id']}/price", json={"amount": "9000"})
    assert r.status_code == 403
    # mechanic: assigned scope -> nothing until a task on the vehicle is theirs
    login(client, mechanic)
    r = await client.get("/api/vehicles", params={"q": tag})
    assert r.json()["items"] == [] and r.json()["total"] == 0
    r = await client.get(f"/api/vehicles/{v['id']}")
    assert r.status_code == 403
    await dispatch(ctx_for(db, owner), "tasks.create", {"title": f"Inspect brakes {tag}", "vehicle_id": v["id"], "owner_user_id": mechanic.id})
    r = await client.get("/api/vehicles", params={"q": tag})
    assert [x["id"] for x in r.json()["items"]] == [v["id"]]
    d = (await client.get(f"/api/vehicles/{v['id']}")).json()
    assert d["tabs"]["money"] == {"money_hidden": True} and [t["title"] for t in d["tabs"]["work"]["tasks"]] == [f"Inspect brakes {tag}"]
    r = await client.post(f"/api/vehicles/{v['id']}/update", json={"color": "red"})
    assert r.status_code == 403   # no vehicles.write
    # creation through the API allocates the id and returns the command envelope
    login(client, owner)
    r = await client.post("/api/vehicles", json={"make": "Honda", "model": f"Acty {tag}"}, headers={"Idempotency-Key": f"k-{tag}"})
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["data"]["vehicle"]["stock_no"].startswith("STK-")
    r2 = await client.post("/api/vehicles", json={"make": "Honda", "model": f"Acty {tag}"}, headers={"Idempotency-Key": f"k-{tag}"})
    assert r2.json()["data"]["vehicle"]["id"] == r.json()["data"]["vehicle"]["id"]


# ── I06 (partial): owner edits through commands; employee cannot inherit owner access ───────
async def test_I06_partial_owner_edits_through_commands_employee_scoped(db, owner, mechanic):
    v = (await _create(db, owner, make="Subaru", model="Sambar"))["vehicle"]
    # every Manager-facing UI action maps to a registered command
    for action, name in svc.COMMANDS_FOR_MANAGER.items():
        assert name in REGISTRY, f"{action} -> {name} not registered"
    # owner (and the Manager acting for the owner) reads/edits the card, condition and tasks with saved versions
    agent = ctx_for(db, owner, kind="agent")
    up = await dispatch(agent, "vehicles.update", {"vehicle_id": v["id"], "color": "white", "notes": "fleet truck"})
    assert up.status == "ok" and up.data["vehicle"]["version"] == v["version"] + 1
    cond = await dispatch(agent, "vehicles.set_condition", {"vehicle_id": v["id"], "bullets": [{"text": "Rust under bed", "source": "owner_reported"}]})
    assert cond.status == "ok" and cond.data["condition_version"] == 1
    task = (await dispatch(agent, "tasks.create", {"title": "Treat rust under bed", "vehicle_id": v["id"]})).data["task"]
    assert task["vehicle_id"] == v["id"]
    detail = await svc.vehicle_detail(db, agent.actor, await db.get(Vehicle, v["id"]))
    assert detail["tabs"]["overview"]["condition"][0]["text"] == "Rust under bed" and detail["tabs"]["money"]["money_hidden"] is False
    # the mechanic cannot edit a vehicle not assigned to them (record scope), nor critical facts, nor prices
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, mechanic), "vehicles.set_condition", {"vehicle_id": v["id"], "bullets": [{"text": "x", "source": "owner_reported"}]})
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, mechanic), "tasks.create", {"title": "Do something", "vehicle_id": v["id"]})
    mech_detail_ids = await __import__("backend.app.domain.access", fromlist=["visible_vehicle_ids"]).visible_vehicle_ids(db, ctx_for(db, mechanic).actor)
    assert v["id"] not in (mech_detail_ids or set())
    await dispatch(agent, "tasks.assign", {"task_id": task["id"], "owner_user_id": mechanic.id})
    ok = await dispatch(ctx_for(db, mechanic), "vehicles.set_condition", {"vehicle_id": v["id"], "bullets": [{"text": "Bed floor soft", "source": "inspection"}]})
    assert ok.status == "ok" and len(ok.data["added"]) == 1
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, mechanic), "vehicles.confirm_fact", {"vehicle_id": v["id"], "key": "odometer_km", "value": "1"})
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, mechanic, kind="agent"), "vehicles.set_asking_price", {"vehicle_id": v["id"], "amount": "1"})
