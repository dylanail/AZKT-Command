"""Shop board and physical evidence (spec §8.4, invariant 10): one gated command for drag / button / keyboard /
agent moves (F1, H10, G10), inspection -> issues -> tasks, parts with physical state separate from payment (G10),
mechanic evidence and owner-only verification (G09, F4), consequential part orders via exact approval."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from backend.app.core.errors import Blocked, Denied, ValidationFailed
from backend.app.domain.commands import dispatch
from backend.app.models.runtime import Approval, Event, ExternalAction
from backend.app.models.tasks import Task
from backend.app.models.vehicles import Part, ReconIssue, Vehicle
from backend.tests.conftest import ctx_for, login, run_worker_once
from backend.tests.test_assets import drain_outbox, jpeg_bytes, upload_asset, upload_via_commands


def _u() -> str:
    return uuid.uuid4().hex[:8]


async def _vehicle(db, owner, **kw) -> dict:
    return (await dispatch(ctx_for(db, owner), "vehicles.create", {"make": "Daihatsu", "model": "Hijet", "logistics_state": "received",
                                                                  "create_missing_task": False, **kw})).data["vehicle"]


async def _release(db, *rows) -> None:
    """A dispatch that raised leaves the test session's transaction (and its row locks) open; roll it back and
    reload the ORM rows the test keeps using (rollback expires them)."""
    await db.rollback()
    for r in rows:
        await db.refresh(r)


async def _tasks(db, vehicle_id: str) -> list[Task]:
    return list((await db.execute(select(Task).where(Task.vehicle_id == vehicle_id).order_by(Task.created_at))).scalars().all())


# ── F1 / H10: gates through one command; keyboard == drag == button == agent ────────────
async def test_F1_gate_blocks_and_creates_linked_tasks_once(db, owner):
    v = await _vehicle(db, owner)
    r1 = await dispatch(ctx_for(db, owner), "shop.move_stage", {"vehicle_id": v["id"], "to_state": "in_recon", "source": "drag"})
    assert r1.data["decision"] == "Blocked" and r1.data["moved"] is False and r1.data["vehicle"]["recon_state"] == "needs_inspection"
    assert [g["requirement"] for g in r1.data["gates"]] == ["inspection_logged"] and r1.data["gates"][0]["factual"] is True
    assert r1.data["tasks"][0]["title"] == "Log inspection findings" and r1.data["tasks"][0]["created"] is True
    # keyboard "Move" and the agent use the identical command and rules; the gate task is not multiplied
    r2 = await dispatch(ctx_for(db, owner), "shop.move_stage", {"vehicle_id": v["id"], "to_state": "in_recon", "source": "keyboard"})
    r3 = await dispatch(ctx_for(db, owner, kind="agent"), "shop.move_stage", {"vehicle_id": v["id"], "to_state": "in_recon", "source": "agent"})
    for r in (r2, r3):
        assert r.data["decision"] == "Blocked" and r.data["tasks"][0]["task_id"] == r1.data["tasks"][0]["task_id"] and r.data["tasks"][0]["created"] is False
    gate_tasks = [t for t in await _tasks(db, v["id"]) if t.gate_requirement == "inspection_logged"]
    assert len(gate_tasks) == 1 and gate_tasks[0].source_kind == "gate"
    # an override cannot turn a factual gate green (invariant 10)
    r4 = await dispatch(ctx_for(db, owner), "shop.move_stage", {"vehicle_id": v["id"], "to_state": "in_recon", "overrides": {"inspection_logged": "trust me"}})
    assert r4.data["decision"] == "Blocked" and r4.data["gates"][0]["override_refused"]
    # logging the inspection satisfies the gate; the move records the recon_started milestone with source shop
    ins = await dispatch(ctx_for(db, owner), "shop.log_inspection", {"vehicle_id": v["id"], "findings": [
        {"title": "Worn front tires", "severity": "normal", "task_title": "Inspect and replace tires"}], "note": "walkaround"})
    assert ins.data["vehicle"]["dates"]["inspected_at"] and ins.data["issues"][0]["issue"]["source_kind"] == "inspection"
    assert ins.data["issues"][0]["task"]["task"]["title"] == "Inspect and replace tires"
    r5 = await dispatch(ctx_for(db, owner), "shop.move_stage", {"vehicle_id": v["id"], "to_state": "in_recon", "source": "button"})
    assert r5.data["decision"] == "Allowed" and r5.data["moved"] is True and r5.data["milestone"]["kind"] == "recon_started"
    assert r5.data["vehicle"]["recon_state"] == "in_recon" and r5.data["vehicle"]["state_history"][-1]["dimension"] == "recon"
    # backward moves need a reason and keep history
    with pytest.raises(ValidationFailed):
        await dispatch(ctx_for(db, owner), "shop.move_stage", {"vehicle_id": v["id"], "to_state": "needs_inspection"})
    back = await dispatch(ctx_for(db, owner), "shop.move_stage", {"vehicle_id": v["id"], "to_state": "needs_inspection", "reason": "re-inspect after tow"})
    assert back.data["moved"] is True and back.data["vehicle"]["state_history"][-1]["backward"] is True
    with pytest.raises(ValidationFailed):
        await dispatch(ctx_for(db, owner), "shop.move_stage", {"vehicle_id": v["id"], "to_state": "sold"})


async def test_H10_keyboard_move_via_api_matches_drag(client, db, owner):
    v = await _vehicle(db, owner)
    login(client, owner)
    drag = (await client.post(f"/api/shop/vehicles/{v['id']}/move", json={"to_state": "finalization", "source": "drag"})).json()
    key = (await client.post(f"/api/shop/vehicles/{v['id']}/move", json={"to_state": "finalization", "source": "keyboard"})).json()
    assert drag["status"] == "ok" and drag["data"]["decision"] == "Blocked" == key["data"]["decision"]
    assert [g["requirement"] for g in drag["data"]["gates"]] == [g["requirement"] for g in key["data"]["gates"]] == ["inspection_logged", "issues_resolved"]
    assert [t["task_id"] for t in drag["data"]["tasks"]] == [t["task_id"] for t in key["data"]["tasks"]]
    r = await client.post(f"/api/shop/vehicles/{v['id']}/move", json={"to_state": "finalization", "source": "mouse-wheel"})
    assert r.status_code == 422
    r = await client.get(f"/api/shop/vehicles/{v['id']}/gates", params={"to_state": "ready_for_sale"})
    assert r.json()["decision"] == "Blocked" and {g["requirement"] for g in r.json()["gates"]} >= {"photos_min", "recon_verified", "disclosures_written"}
    board = (await client.get("/api/shop/board")).json()
    assert [c["state"] for c in board["columns"]] == ["needs_inspection", "in_recon", "finalization", "ready_for_sale"]
    assert v["id"] in [x["id"] for x in board["columns"][0]["vehicles"]]
    rules = (await client.get("/api/shop/gate-rules")).json()
    assert {r["requirement"] for r in rules["items"]} >= {"inspection_logged", "photos_min", "recon_verified", "disclosures_written"}


# ── G10: paid is not installed; ready-for-sale gate ─────────────────────────
async def test_G10_paid_part_not_installed_blocks_ready_for_sale(db, owner, mechanic):
    v = await _vehicle(db, owner)
    await dispatch(ctx_for(db, owner), "shop.log_inspection", {"vehicle_id": v["id"], "findings": []})
    await dispatch(ctx_for(db, owner), "shop.move_stage", {"vehicle_id": v["id"], "to_state": "in_recon"})
    part = (await dispatch(ctx_for(db, owner), "shop.request_part", {"vehicle_id": v["id"], "name": "Alternator belt", "quantity": 1})).data["part"]
    again = await dispatch(ctx_for(db, owner), "shop.request_part", {"vehicle_id": v["id"], "name": "alternator belt"})
    assert again.data["created"] is False and again.data["part"]["id"] == part["id"]
    paid = await dispatch(ctx_for(db, owner), "shop.record_part_payment", {"part_id": part["id"], "vehicle_id": v["id"], "note": "card"})
    assert paid.data["part"]["payment_state"] == "paid" and paid.data["part"]["state"] == "requested"
    assert paid.data["part"]["arrived_at"] is None and paid.data["part"]["installed_at"] is None
    with pytest.raises(Blocked):   # cannot install what has not arrived, and never without evidence
        await dispatch(ctx_for(db, owner), "shop.mark_part_installed", {"part_id": part["id"], "vehicle_id": v["id"]})
    await _release(db, owner, mechanic)
    for source in ("drag", "keyboard"):
        r = await dispatch(ctx_for(db, owner), "shop.move_stage", {"vehicle_id": v["id"], "to_state": "ready_for_sale", "source": source})
        assert r.data["decision"] == "Blocked" and r.data["vehicle"]["recon_state"] == "in_recon"
        gates = {g["requirement"]: g for g in r.data["gates"]}
        assert {"photos_min", "recon_verified", "disclosures_written"} <= set(gates)
        assert any("paid, not installed" in item for item in gates["recon_verified"]["items"])
        assert gates["photos_min"]["ok"] is False and "0 of 6" in gates["photos_min"]["detail"]
    tasks = await _tasks(db, v["id"])
    gate_tasks = [t for t in tasks if t.source_kind == "gate"]
    assert sorted(t.gate_requirement for t in gate_tasks) == ["disclosures_written", "photos_min", "recon_verified"]   # created once, not per attempt
    row = await db.get(Vehicle, v["id"])
    await db.refresh(row)
    assert row.recon_state == "in_recon" and row.ready_at is None and row.health == "risk"


async def test_part_lifecycle_and_consequential_order(db, owner, mechanic, manager):
    v = await _vehicle(db, owner)
    await dispatch(ctx_for(db, owner), "tasks.create", {"title": f"Fit belt {_u()}", "vehicle_id": v["id"], "owner_user_id": mechanic.id})
    part = (await dispatch(ctx_for(db, mechanic), "shop.request_part", {"vehicle_id": v["id"], "name": "Water pump"})).data["part"]
    assert part["state"] == "requested" and part["requested_by"] == mechanic.id
    with pytest.raises(Denied):   # mechanic cannot order
        await dispatch(ctx_for(db, mechanic), "shop.order_part", {"part_id": part["id"], "vehicle_id": v["id"], "amount": "80"})
    # owner ordering is consequential: exact approval, then an ExternalAction intent executed by the worker
    res = await dispatch(ctx_for(db, owner), "shop.order_part", {"part_id": part["id"], "vehicle_id": v["id"], "amount": "80.00", "currency": "USD", "order_ref": "PO-7"})
    assert res.status == "needs_review" and res.approval_id
    a = await db.get(Approval, res.approval_id)
    assert a.kind == "parts_order" and a.consequence["moves_money"] is True and a.title.startswith("Order part:")
    p = await db.get(Part, part["id"])
    await db.refresh(p)
    assert p.state == "requested"   # nothing happened before approval
    ok = await dispatch(ctx_for(db, owner), "approvals.approve", {"approval_id": a.id, "expected_version": 1})
    assert ok.data["executed"] is True
    await db.refresh(a)
    assert a.status == "queued" and a.external_action_id
    act = await db.get(ExternalAction, a.external_action_id)
    assert act.state == "intent" and act.provider == "manual" and act.dedupe_key == f"parts.order:{part['id']}"
    await db.refresh(p)
    assert p.state == "approved" and p.approval_id == a.id
    await run_worker_once()
    await db.refresh(act)
    await db.refresh(p)
    await db.refresh(a)
    assert act.state == "confirmed" and act.receipt["provider"] == "manual" and act.receipt["recorded"] is True
    assert p.state == "ordered" and p.order_ref == "PO-7" and a.status == "confirmed"
    # arrival -> installation requires evidence -> verification is owner-only
    arr = await dispatch(ctx_for(db, mechanic), "shop.mark_part_arrived", {"part_id": part["id"], "vehicle_id": v["id"], "note": "box on bench"})
    assert arr.data["part"]["state"] == "arrived" and arr.data["part"]["payment_state"] == "unpaid"
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, mechanic), "shop.mark_part_installed", {"part_id": part["id"], "vehicle_id": v["id"]})
    await _release(db, owner, mechanic, manager)
    photo = await upload_via_commands(db, mechanic, jpeg_bytes(61), "pump.jpg")
    inst = await dispatch(ctx_for(db, mechanic), "shop.mark_part_installed", {"part_id": part["id"], "vehicle_id": v["id"], "asset_ids": [photo]})
    assert inst.data["part"]["state"] == "installed" and inst.data["part"]["evidence"][-1]["asset_ids"] == [photo]
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, manager), "shop.verify_part", {"part_id": part["id"], "vehicle_id": v["id"]})
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, mechanic), "shop.verify_part", {"part_id": part["id"], "vehicle_id": v["id"]})
    ver = await dispatch(ctx_for(db, owner), "shop.verify_part", {"part_id": part["id"], "vehicle_id": v["id"]})
    assert ver.data["part"]["state"] == "verified" and ver.data["part"]["verified_by"] == owner.id
    assert [h["to"] for h in ver.data["part"]["history"]] == ["requested", "approved", "ordered", "arrived", "installed", "verified"]
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "shop.cancel_part", {"part_id": part["id"], "vehicle_id": v["id"], "reason": "x"})
    await _release(db, owner, mechanic, manager)


# ── G09 / F4: employee evidence, owner-only verification ───────────────────
async def test_G09_mechanic_evidence_then_owner_only_verification(client, db, owner, mechanic, manager):
    v = await _vehicle(db, owner)
    iss = (await dispatch(ctx_for(db, owner), "shop.create_issue", {"vehicle_id": v["id"], "title": "Torn seat cover", "source_kind": "owner_reported",
                                                                    "task_title": "Replace seat cover", "assignee_user_id": mechanic.id})).data
    task = iss["task"]["task"]
    assert task["owner_user_id"] == mechanic.id and task["evidence_required"][0]["kind"] == "photo" and iss["issue"]["task_id"] == task["id"]
    # completing without the required photo is blocked
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, mechanic), "tasks.complete", {"task_id": task["id"]})
    await _release(db, owner, mechanic, manager)
    login(client, mechanic)
    # first upload fails (bad bytes) -> stays failed/draft; no asset id exists to attach
    r = await client.post("/api/uploads", json={"purpose": "evidence", "content_type": "image/jpeg", "filename": "seat.jpg"})
    up = r.json()["data"]["upload"]
    await client.put(up["put_url"], content=b"garbage bytes that are not a photo at all")
    fin = (await client.post(f"/api/uploads/{up['id']}/finalize", json={})).json()["data"]
    assert fin["status"] == "failed" and fin["asset"] is None
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, mechanic), "tasks.attach_evidence", {"task_id": task["id"], "asset_ids": [up["id"]]})
    await _release(db, owner, mechanic, manager)
    # then succeeds -> evidence saved -> Awaiting verification (mobile flow through the API)
    good = (await upload_asset(client, jpeg_bytes(71), "seat.jpg"))["asset"]
    r = await client.post(f"/api/tasks/{task['id']}/evidence", json={"asset_ids": [good["id"]], "note": "new cover fitted"})
    assert r.status_code == 200 and r.json()["data"]["task"]["evidence"][0]["asset_ids"] == [good["id"]]
    r = await client.post(f"/api/tasks/{task['id']}/complete", json={})
    t = r.json()["data"]["task"]
    assert t["status"] == "awaiting_verification" and t["verification_status"] == "awaiting"
    # unauthorized verification denied across API and command layer (manager, mechanic, Manager-as-manager)
    login(client, manager)
    r = await client.post(f"/api/tasks/{task['id']}/verify", json={})
    assert r.status_code == 403
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, manager, kind="agent"), "tasks.verify", {"task_id": task["id"]})
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, mechanic), "tasks.verify", {"task_id": task["id"]})
    await _release(db, owner, mechanic, manager)
    ag = await dispatch(ctx_for(db, owner, kind="agent"), "tasks.verify", {"task_id": task["id"]})
    assert ag.status == "needs_review"   # the owner's Manager prepares it; a person still decides
    row = await db.get(Task, task["id"])
    await db.refresh(row)
    assert row.status == "awaiting_verification"
    # the mechanic's evidence can be rejected with a reason (task reopens) or verified by the owner
    login(client, owner)
    r = await client.post(f"/api/tasks/{task['id']}/verify", json={})
    assert r.status_code == 200 and r.json()["data"]["task"]["status"] == "completed" and r.json()["data"]["task"]["verified_by"] == owner.id
    ev = (await db.execute(select(Event).where(Event.type == "work.verified", Event.aggregate_id == task["id"]))).scalars().all()
    assert len(ev) == 1
    # the shop's Ready-for-sale gate still sees the open issue until it is resolved/deferred
    res = await dispatch(ctx_for(db, owner), "shop.resolve_issue", {"issue_id": iss["issue"]["id"], "vehicle_id": v["id"], "note": "verified"})
    assert res.data["issue"]["status"] == "resolved"


async def test_F4_employee_evidence_task_view_and_blocker(client, db, owner, mechanic):
    v = await _vehicle(db, owner)
    task = (await dispatch(ctx_for(db, owner), "tasks.create", {"title": f"Photograph engine bay {_u()}", "vehicle_id": v["id"], "owner_user_id": mechanic.id,
                                                               "evidence_required": [{"kind": "photo", "min": 2}], "instructions": "Hood up, flash off"})).data["task"]
    login(client, mechanic)
    d = (await client.get(f"/api/tasks/{task['id']}")).json()["task"]
    assert d["evidence_required"] == [{"kind": "photo", "min": 2}] and d["instructions"] == "Hood up, flash off"
    one = (await upload_asset(client, jpeg_bytes(81), "bay1.jpg"))["asset"]
    r = await client.post(f"/api/tasks/{task['id']}/evidence", json={"asset_ids": [one["id"]]})
    r = await client.post(f"/api/tasks/{task['id']}/complete", json={})
    assert r.status_code == 409 and "photo (1/2)" in r.json()["missing"]
    r = await client.post(f"/api/tasks/{task['id']}/block", json={"reason": "battery dead, cannot open hood"})
    assert r.json()["data"]["task"]["status"] == "blocked"
    await drain_outbox()
    row = await db.get(Vehicle, v["id"])
    await db.refresh(row)
    assert row.health == "blocked" and "battery dead" in row.health_reason
    two = (await upload_asset(client, jpeg_bytes(82), "bay2.jpg"))["asset"]
    await client.post(f"/api/tasks/{task['id']}/evidence", json={"asset_ids": [two["id"]]})
    r = await client.post(f"/api/tasks/{task['id']}/complete", json={})
    assert r.json()["data"]["task"]["status"] == "awaiting_verification"
    # the mechanic can see the evidence they uploaded, not other trucks' files
    r = await client.get(f"/api/assets/{two['id']}/thumb")
    assert r.status_code == 200


async def test_issue_defer_and_update_history(db, owner):
    v = await _vehicle(db, owner)
    await dispatch(ctx_for(db, owner), "shop.log_inspection", {"vehicle_id": v["id"], "findings": [{"title": "Small dent rear panel", "severity": "info"}]})
    await dispatch(ctx_for(db, owner), "shop.move_stage", {"vehicle_id": v["id"], "to_state": "in_recon"})
    issue = (await db.execute(select(ReconIssue).where(ReconIssue.vehicle_id == v["id"]))).scalar_one()
    blocked = await dispatch(ctx_for(db, owner), "shop.move_stage", {"vehicle_id": v["id"], "to_state": "finalization"})
    assert blocked.data["decision"] == "Blocked" and blocked.data["gates"][0]["requirement"] == "issues_resolved"
    dup = await dispatch(ctx_for(db, owner), "shop.create_issue", {"vehicle_id": v["id"], "title": "small dent rear panel", "create_task": False})
    assert dup.data["created"] is False and dup.data["issue"]["id"] == issue.id
    upd = await dispatch(ctx_for(db, owner), "shop.update_issue", {"issue_id": issue.id, "vehicle_id": v["id"], "severity": "major"})
    assert upd.data["changed"]["severity"] == {"from": "info", "to": "major"}
    with pytest.raises(ValidationFailed):
        await dispatch(ctx_for(db, owner), "shop.update_issue", {"issue_id": issue.id, "vehicle_id": v["id"], "status": "rejected"})
    de = await dispatch(ctx_for(db, owner), "shop.defer_issue", {"issue_id": issue.id, "vehicle_id": v["id"], "reason": "cosmetic; disclose instead"})
    assert de.data["issue"]["status"] == "deferred" and de.data["task_cancelled"]
    ok = await dispatch(ctx_for(db, owner), "shop.move_stage", {"vehicle_id": v["id"], "to_state": "finalization"})
    assert ok.data["decision"] == "Allowed" and ok.data["vehicle"]["recon_state"] == "finalization"
    wo = await dispatch(ctx_for(db, owner), "shop.create_work_order", {"vehicle_id": v["id"], "title": "Paint touch-up", "recon_issue_id": issue.id})
    assert wo.data["work_order"]["ref"].startswith("WO-")


# ── invariant 10: factual gates cannot be turned green by hand ──────────────
async def test_readiness_and_inspection_dates_cannot_bypass_the_gates(db, owner, manager):
    v = await _vehicle(db, owner)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for kind in ("inspected", "recon_started", "ready"):
        for user in (owner, manager):
            with pytest.raises(Blocked):
                await dispatch(ctx_for(db, user), "vehicles.record_milestone", {"vehicle_id": v["id"], "kind": kind,
                                                                               "status": "completed", "at": now, "source_kind": "manual"})
    row = await db.get(Vehicle, v["id"])
    await db.refresh(row)
    assert row.inspected_at is None and row.ready_at is None and row.recon_state == "needs_inspection"
    # a forecast is still honest and allowed
    plan = await dispatch(ctx_for(db, owner), "vehicles.record_milestone", {"vehicle_id": v["id"], "kind": "ready", "status": "planned",
                                                                           "at": now, "source_kind": "manual", "note": "target"})
    assert plan.data["milestone"]["status"] == "planned" and plan.data["vehicle"]["dates"]["ready_at"] is None
    # the gate still blocks; only the shop commands write the completed recon milestones
    blocked = await dispatch(ctx_for(db, owner), "shop.move_stage", {"vehicle_id": v["id"], "to_state": "in_recon"})
    assert blocked.data["decision"] == "Blocked" and blocked.data["gates"][0]["requirement"] == "inspection_logged"
    await dispatch(ctx_for(db, owner), "shop.log_inspection", {"vehicle_id": v["id"], "findings": []})
    ok = await dispatch(ctx_for(db, owner), "shop.move_stage", {"vehicle_id": v["id"], "to_state": "in_recon"})
    assert ok.data["decision"] == "Allowed" and ok.data["milestone"]["kind"] == "recon_started" and ok.data["milestone"]["source_kind"] == "shop"


# ── shop board population equals "physically in the shop", grouped by recon state ────────────────
async def test_shop_board_only_shows_trucks_that_are_in_the_shop(client, db, owner):
    here = await _vehicle(db, owner, stock_no=f"IN-{_u()}")
    ready = await _vehicle(db, owner, stock_no=f"RD-{_u()}", recon_state="ready_for_sale")
    at_auction = await _vehicle(db, owner, stock_no=f"AU-{_u()}", logistics_state="candidate", allocation="candidate")
    on_vessel = await _vehicle(db, owner, stock_no=f"VS-{_u()}", logistics_state="on_vessel")
    login(client, owner)
    r = await client.get("/api/shop/board")
    assert r.status_code == 200
    ids = {v["id"] for col in r.json()["columns"] for v in col["vehicles"]}
    assert here["id"] in ids and ready["id"] in ids, "received trucks (including ready-for-sale) are on the board"
    assert at_auction["id"] not in ids and on_vessel["id"] not in ids, "trucks not yet here are never shop cards"
    # the board's total is the number of cards it shows, so the UI subtitle and the cards agree
    assert r.json()["total"] == sum(col["count"] for col in r.json()["columns"])
