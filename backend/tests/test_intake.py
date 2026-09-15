"""Photo / voice intake through Manager (spec §7.4; acceptance I01–I05, I07, H13; invariant 13).
No model API key is configured in tests, so extraction runs the deterministic fallback unless a test
monkeypatches the model path; either way the provenance is recorded and nothing is fabricated."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from backend.app.adapters.model import ModelUnavailable
from backend.app.core.config import settings
from backend.app.core.errors import Blocked, Denied, NotFound
from backend.app.domain.commands import dispatch
from backend.app.models.assets import Asset, AssetLink
from backend.app.models.intake import IntakeObservation, VehicleIntake
from backend.app.models.runtime import Approval, Event
from backend.app.models.tasks import Task
from backend.app.models.vehicles import ReconIssue, Vehicle, VehicleMilestone
from backend.app.services import intake_analysis as ia
from backend.tests.conftest import ctx_for, login
from backend.tests.test_assets import jpeg_bytes, upload_asset, upload_via_commands, wav_bytes

NOTE = "left-door dent, A/C not cold, needs tires and detailing"
EXPECTED_TASKS = {"Inspect and repair left door damage", "Diagnose A/C", "Inspect and replace tires", "Detail vehicle after required work"}


def _u() -> str:
    return uuid.uuid4().hex[:8]


async def _vehicle(db, owner, **kw) -> dict:
    return (await dispatch(ctx_for(db, owner), "vehicles.create", {"make": "Daihatsu", "model": "Hijet", "logistics_state": "received",
                                                                  "create_missing_task": False, **kw})).data["vehicle"]


async def _task_titles(db, vehicle_id: str, *, active_only: bool = True) -> list[str]:
    q = select(Task).where(Task.vehicle_id == vehicle_id)
    if active_only:
        q = q.where(Task.status.notin_(("cancelled", "completed")))
    return [t.title for t in (await db.execute(q.order_by(Task.created_at))).scalars().all()]


async def _links(db, asset_id: str, vehicle_id: str) -> list[AssetLink]:
    return list((await db.execute(select(AssetLink).where(AssetLink.asset_id == asset_id, AssetLink.entity_kind == "vehicle",
                                                          AssetLink.entity_id == vehicle_id, AssetLink.removed_at.is_(None)))).scalars().all())


# ── I01 ──────────────────────────────────────────────────────────────────────
async def test_I01_new_vehicle_from_photos_and_notes(client, db, owner):
    login(client, owner)
    key = f"intake-{_u()}"
    r = await client.post("/api/vehicle-intakes", json={"target_mode": "new", "request_key": key, "channel": "web"})
    assert r.status_code == 200 and r.json()["data"]["created"] is True
    it = r.json()["data"]["intake"]
    assert it["status"] == "open" and it["target_mode"] == "new" and it["complete"] is False
    ids = [(await upload_asset(client, jpeg_bytes(100 + i), f"IMG_{i}.jpg"))["asset"]["id"] for i in range(3)]
    r = await client.post(f"/api/vehicle-intakes/{it['id']}/continue", json={"asset_ids": ids, "text": NOTE})
    assert r.status_code == 200, r.text
    st = r.json()["status"]
    assert st["saved_to_azkt"]["assets"] == 3 and st["saved_to_azkt"]["failed"] == 0 and st["target"]["label"] == "New vehicle"
    assert [i["status"] for i in st["items"]] == ["saved", "saved", "saved"]
    r = await client.post(f"/api/vehicle-intakes/{it['id']}/apply", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok" and body["data"]["decision"] == "Allowed" and body["data"]["status"] == "applied"
    res = body["data"]["result"]
    veh = body["data"]["vehicle"]
    assert res["created"] is True and res["stock_no"].startswith("STK-") and res["vehicle_id"] == veh["id"]
    assert res["intake_status"] == "incomplete" and set(res["missing"]) == {"frame_no", "model_year", "purchase_amount"}
    assert res["photos_saved"] == 3 and res["photos_failed"] == [] and res["failed_observations"] == []
    bullets = res["condition_bullets"]
    assert len(bullets) >= 4 and all(b["source"] == "owner_reported" and b["observation_id"] and b["new"] for b in bullets)
    assert {b["text"] for b in bullets} >= {"left-door dent", "A/C not cold", "needs tires", "detailing"}
    titles = {t["title"] for t in res["tasks_created"]}
    assert EXPECTED_TASKS <= titles and "Record missing vehicle identity" in titles and res["tasks_updated"] == []
    assert all(t["title"].split()[0][0].isupper() for t in res["tasks_created"])
    # nothing invented
    assert veh["frame_no_raw"] is None and veh["purchase_amount"] is None and veh["dates"]["acquired_at"] is None
    assert veh["dates"]["received_at"] is None and veh["recon_state"] == "needs_inspection" and veh["hero_asset_id"] in ids
    assert res["needs_confirmation"] == [] and res["milestones"] == []
    assert body["data"]["intake"]["analysis"]["provenance"]["path"] == "deterministic" and body["data"]["intake"]["complete"] is True
    # no per-task approval rows; all internal work
    n = await db.scalar(select(func.count()).select_from(Approval).where(Approval.entity_id == veh["id"]))
    n2 = await db.scalar(select(func.count()).select_from(Approval).where(Approval.command_name.like("intake.%")))
    assert n == 0 and n2 == 0
    # photos private, linked once each, and visible on the card's Files tab; condition on Overview; work on Work
    for aid in ids:
        a = await db.get(Asset, aid)
        assert a.public_eligible is False and a.visibility == "internal" and len(await _links(db, aid, veh["id"])) == 1
    d = (await client.get(f"/api/vehicles/{veh['id']}")).json()
    assert len(d["tabs"]["files"]["photos"]) == 3 and len(d["tabs"]["overview"]["condition"]) == 4
    assert {i["source_kind"] for i in d["tabs"]["work"]["recon_issues"]} == {"owner_reported"} and len(d["tabs"]["work"]["recon_issues"]) == 4
    assert EXPECTED_TASKS <= {t["title"] for t in d["tabs"]["work"]["tasks"]}
    assert d["vehicle"]["health"] == "risk" and "unassigned" in d["vehicle"]["health_reason"]   # honest: work is not yet assigned
    ev = (await db.execute(select(Event).where(Event.type == "intake.applied", Event.aggregate_id == it["id"]))).scalars().all()
    assert len(ev) == 1 and ev[0].payload["created"] is True
    # status endpoint shows saved-to-AZKT per item
    st = (await client.get(f"/api/vehicle-intakes/{it['id']}")).json()
    assert st["intake"]["status"] == "applied" and st["saved_to_azkt"]["applied"] is True and all(i["linked_to_vehicle"] for i in st["items"])
    assert st["vehicle"]["stock_no"] == res["stock_no"]
    # retrying the start with the same request key resumes the same intake (no second card)
    r = await client.post("/api/vehicle-intakes", json={"target_mode": "new", "request_key": key})
    assert r.json()["data"]["created"] is False and r.json()["data"]["intake"]["id"] == it["id"]


# ── I02 ──────────────────────────────────────────────────────────────────────
async def test_I02_existing_vehicle_retry_and_album_do_not_duplicate(db, owner):
    v = await _vehicle(db, owner, frame_no_raw=f"S510P-{uuid.uuid4().int % 900000 + 100000}")
    a1 = await upload_via_commands(db, owner, jpeg_bytes(201), "album-1.jpg")
    a2 = await upload_via_commands(db, owner, jpeg_bytes(202), "album-2.jpg")
    key = f"tg-album-{_u()}"
    s1 = await dispatch(ctx_for(db, owner), "intake.start", {"target_mode": "existing", "vehicle_id": v["id"], "channel": "telegram",
                                                            "request_key": key, "telegram_media_group_id": key})
    s2 = await dispatch(ctx_for(db, owner), "intake.start", {"target_mode": "existing", "vehicle_id": v["id"], "channel": "telegram", "request_key": key})
    it = s1.data["intake"]
    assert s1.data["created"] is True and s2.data["created"] is False and s2.data["intake"]["id"] == it["id"]
    await dispatch(ctx_for(db, owner), "intake.add_assets", {"intake_id": it["id"], "asset_ids": [a1]})
    await dispatch(ctx_for(db, owner), "intake.add_note", {"intake_id": it["id"], "text": "needs tires, A/C not cold"})
    r1 = (await dispatch(ctx_for(db, owner), "intake.apply", {"intake_id": it["id"]})).data
    assert r1["status"] == "applied" and r1["result"]["created"] is False and r1["result"]["vehicle_id"] == v["id"]
    assert {t["title"] for t in r1["result"]["tasks_created"]} == {"Inspect and replace tires", "Diagnose A/C"} and r1["result"]["photos_saved"] == 1
    # reconnect / album re-delivery: same photo again plus the second one, the same note repeated
    again = (await dispatch(ctx_for(db, owner), "intake.add_assets", {"intake_id": it["id"], "asset_ids": [a1, a2]})).data
    assert [a["id"] for a in again["saved"]] == [a1, a2]
    await dispatch(ctx_for(db, owner), "intake.add_note", {"intake_id": it["id"], "text": "needs tires, A/C not cold"})
    r2 = (await dispatch(ctx_for(db, owner), "intake.apply", {"intake_id": it["id"]})).data
    assert r2["status"] == "applied" and r2["result"]["tasks_created"] == [] and r2["result"]["photos_saved"] == 2
    assert {t["title"] for t in r2["result"]["tasks_updated"]} == {"Inspect and replace tires", "Diagnose A/C"} and all(t["merged"] for t in r2["result"]["tasks_updated"])
    assert r2["result"]["condition_bullets"] and not any(b["new"] for b in r2["result"]["condition_bullets"])
    titles = await _task_titles(db, v["id"])
    assert titles.count("Inspect and replace tires") == 1 and titles.count("Diagnose A/C") == 1
    assert len(await _links(db, a1, v["id"])) == 1 and len(await _links(db, a2, v["id"])) == 1
    n_veh = await db.scalar(select(func.count()).select_from(Vehicle).where(Vehicle.frame_no_norm == v["frame_no_norm"]))
    n_iss = await db.scalar(select(func.count()).select_from(ReconIssue).where(ReconIssue.vehicle_id == v["id"]))
    n_int = await db.scalar(select(func.count()).select_from(VehicleIntake).where(VehicleIntake.request_key == key))
    assert n_veh == 1 and n_iss == 2 and n_int == 1
    row = await db.get(Vehicle, v["id"])
    await db.refresh(row)
    assert len([b for b in row.condition_summary if not b.get("removed")]) == 2 and row.condition_version >= 1
    obs = (await db.execute(select(IntakeObservation).where(IntakeObservation.intake_id == it["id"]))).scalars().all()
    assert all(o.status == "applied" and o.applied_command for o in obs) and {o.revision for o in obs} == {1}
    itrow = await db.get(VehicleIntake, it["id"])
    await db.refresh(itrow)
    assert itrow.revision == 2 and itrow.applied["revision"] == 2 and itrow.applied["vehicle_id"] == v["id"]


# ── I03 ──────────────────────────────────────────────────────────────────────
async def test_I03_similar_vehicles_need_choice_and_uncertain_frame_not_written(db, owner):
    v1 = await _vehicle(db, owner, make="Subaru", model="Sambar", model_year=2001, color="beige", frame_no_raw=f"TT2-{_u()}")
    v2 = await _vehicle(db, owner, make="Subaru", model="Sambar", model_year=2001, color="beige", frame_no_raw=f"TT2-{_u()}")
    it = (await dispatch(ctx_for(db, owner), "intake.start", {"target_mode": "find"})).data["intake"]
    await dispatch(ctx_for(db, owner), "intake.add_note", {"intake_id": it["id"], "text": "beige 2001 sambar needs tires"})
    an = (await dispatch(ctx_for(db, owner), "intake.analyze", {"intake_id": it["id"]})).data
    assert an["intake"]["status"] == "needs_choice" and {v1["id"], v2["id"]} <= set(an["intake"]["candidate_vehicle_ids"])
    cands = {c["vehicle_id"]: c for c in an["intake"]["choice"]["candidates"]}
    assert cands[v1["id"]]["stock_no"] == v1["stock_no"] and cands[v1["id"]]["frame_no_raw"] == v1["frame_no_raw"]   # identifying context
    ap = (await dispatch(ctx_for(db, owner), "intake.apply", {"intake_id": it["id"]})).data
    assert ap["decision"] == "Blocked" and ap["status"] == "needs_choice"
    assert await _task_titles(db, v1["id"]) == [] and await _task_titles(db, v2["id"]) == []   # nothing written to either truck
    # the saved intake content is still there and recoverable; an explicit choice unblocks it
    row = await db.get(VehicleIntake, it["id"])
    await db.refresh(row)
    assert row.text_notes == "beige 2001 sambar needs tires" and row.status == "needs_choice"
    ch = (await dispatch(ctx_for(db, owner), "intake.choose_vehicle", {"intake_id": it["id"], "vehicle_id": v1["id"]})).data
    assert ch["intake"]["status"] == "analyzed" and ch["intake"]["vehicle_id"] == v1["id"]
    ap = (await dispatch(ctx_for(db, owner), "intake.apply", {"intake_id": it["id"]})).data
    assert ap["decision"] == "Allowed" and ap["result"]["vehicle_id"] == v1["id"]
    assert await _task_titles(db, v1["id"]) == ["Inspect and replace tires"] and await _task_titles(db, v2["id"]) == []
    # an uncertain frame in the transcript is never written to the card; a focused task asks for confirmation
    it2 = (await dispatch(ctx_for(db, owner), "intake.start", {"target_mode": "existing", "vehicle_id": v2["id"]})).data["intake"]
    await dispatch(ctx_for(db, owner), "intake.add_transcript", {"intake_id": it2["id"], "text": "frame might be S510P-123456 or S510P-123457, arrived today"})
    ap2 = (await dispatch(ctx_for(db, owner), "intake.apply", {"intake_id": it2["id"]})).data
    nc = ap2["result"]["needs_confirmation"]
    assert {x["value"] for x in nc if x["field"] == "frame_no"} == {"S510P-123456", "S510P-123457"} and ap2["result"]["facts"] == []
    assert ap2["vehicle"]["frame_no_raw"] == v2["frame_no_raw"]
    assert (await _task_titles(db, v2["id"])).count("Confirm frame number") == 1
    obs = (await db.execute(select(IntakeObservation).where(IntakeObservation.intake_id == it2["id"], IntakeObservation.kind == "identifier"))).scalars().all()
    assert {o.status for o in obs} == {"needs_confirmation"} and {o.confidence for o in obs} == {"uncertain"}
    # "arrived today" is a stated milestone: received completed with source owner_reported; no inspection/readiness implied
    assert ap2["result"]["milestones"][0]["source_kind"] == "owner_reported" and ap2["vehicle"]["dates"]["received_at"]
    assert ap2["vehicle"]["dates"]["inspected_at"] is None and ap2["vehicle"]["recon_state"] == "needs_inspection"
    ms = (await db.execute(select(VehicleMilestone).where(VehicleMilestone.vehicle_id == v2["id"], VehicleMilestone.kind == "received"))).scalars().all()
    assert len(ms) == 1 and ms[0].source_kind == "owner_reported"


# ── I04 ──────────────────────────────────────────────────────────────────────
async def test_I04_failed_upload_keeps_intake_open_and_retry_does_not_duplicate(client, db, owner, monkeypatch):
    login(client, owner)
    it = (await client.post("/api/vehicle-intakes", json={"target_mode": "new", "request_key": f"drop-{_u()}"})).json()["data"]["intake"]
    good = (await upload_asset(client, jpeg_bytes(301), "ok.jpg"))["asset"]
    # the network drops mid-upload: only the first half of the second photo arrives
    data = jpeg_bytes(302)
    half = len(data) // 2
    up = (await client.post("/api/uploads", json={"purpose": "intake", "content_type": "image/jpeg", "size_bytes": len(data), "filename": "drop.jpg"})).json()["data"]["upload"]
    r = await client.put(up["put_url"], content=data[:half], headers={"Content-Range": f"bytes 0-{half - 1}/{len(data)}"})
    assert r.status_code == 200 and r.json()["complete"] is False
    # the model is configured but fails: analysis falls back; photos and verbatim notes survive
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test-key")

    async def _boom(*a, **k):
        raise ModelUnavailable("provider error 503")
    monkeypatch.setattr(ia, "extract_with_model", _boom)
    r = await client.post(f"/api/vehicle-intakes/{it['id']}/continue", json={"asset_ids": [good["id"]], "upload_ids": [up["id"]], "text": "needs tires"})
    st = r.json()["status"]
    assert st["saved_to_azkt"]["assets"] == 1 and st["saved_to_azkt"]["failed"] == 1
    failed = [i for i in st["items"] if i["status"] == "failed"][0]
    assert failed["upload_id"] == up["id"] and failed["retryable"] is True and "received" in failed["error"]
    ap = (await client.post(f"/api/vehicle-intakes/{it['id']}/apply", json={})).json()["data"]
    assert ap["status"] == "partially_applied" and ap["intake"]["complete"] is False and ap["intake"]["label"] == "Partially saved"
    res = ap["result"]
    assert res["created"] is True and res["photos_saved"] == 1 and [f["upload_id"] for f in res["photos_failed"]] == [up["id"]]
    assert {t["title"] for t in res["tasks_created"]} >= {"Inspect and replace tires"}
    prov = ap["intake"]["analysis"]["provenance"]
    assert prov["path"] == "deterministic" and "model unavailable" in prov["reason"] and ap["intake"]["text_notes"] == "needs tires"
    vid = res["vehicle_id"]
    # resume the upload from the recorded offset, then retry: no second card, no second task, the photo is linked
    r = await client.get(f"/api/uploads/{up['id']}")
    assert r.json()["next_offset"] == half
    r = await client.put(up["put_url"], content=data[half:], headers={"Content-Range": f"bytes {half}-{len(data) - 1}/{len(data)}"})
    assert r.status_code == 200
    fin = (await client.post(f"/api/uploads/{up['id']}/finalize", json={})).json()["data"]
    assert fin["status"] == "ready"
    r = await client.post(f"/api/vehicle-intakes/{it['id']}/continue", json={"upload_ids": [up["id"]]})
    assert r.json()["status"]["saved_to_azkt"]["failed"] == 0
    ap2 = (await client.post(f"/api/vehicle-intakes/{it['id']}/apply", json={})).json()["data"]
    assert ap2["status"] == "applied" and ap2["intake"]["complete"] is True
    assert ap2["result"]["vehicle_id"] == vid and ap2["result"]["created"] is False and ap2["result"]["card_created_by_intake"] is True
    assert ap2["result"]["photos_saved"] == 2 and ap2["result"]["photos_failed"] == [] and ap2["result"]["tasks_created"] == []
    assert (await _task_titles(db, vid)).count("Inspect and replace tires") == 1
    assert len(await _links(db, fin["asset"]["id"], vid)) == 1
    n = await db.scalar(select(func.count()).select_from(Vehicle).where(Vehicle.extra["create_key"].as_string() == f"intake:{it['id']}"))
    assert n == 1


# ── I05 ──────────────────────────────────────────────────────────────────────
async def test_I05_image_observed_dent_creates_inspection_only(db, owner, monkeypatch):
    v = await _vehicle(db, owner)
    photo = await upload_via_commands(db, owner, jpeg_bytes(401), "dent.jpg")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test-key")
    seen: dict = {}

    async def _vision(db_, *, text, images, now, run_id=None, mission_id=None):
        seen["images"] = [(len(b), mt, aid) for b, mt, aid in images]
        return ia.IntakeExtraction(
            conditions=[ia.ExtractedCondition(text="Visible dent on left door", source="image_observed", image_index=0, panel="left door"),
                        ia.ExtractedCondition(text="Fluid mark visible under engine bay", source="image_observed", image_index=0),
                        ia.ExtractedCondition(text="Check brake fluid level", source="proposed_check")],
            requests=[ia.ExtractedRequest(title="Inspect and repair left door damage", condition_index=0, image_index=0),
                      ia.ExtractedRequest(title="Inspect for fluid leak", condition_index=1)],
            identifiers=[ia.ExtractedIdentifier(field="odometer_km", value="84120", confidence="observed", image_index=0, note="partially legible")]), "model"
    monkeypatch.setattr(ia, "extract_with_model", _vision)
    it = (await dispatch(ctx_for(db, owner), "intake.start", {"target_mode": "existing", "vehicle_id": v["id"]})).data["intake"]
    await dispatch(ctx_for(db, owner), "intake.add_assets", {"intake_id": it["id"], "asset_ids": [photo]})
    ap = (await dispatch(ctx_for(db, owner), "intake.apply", {"intake_id": it["id"]})).data
    assert ap["status"] == "applied" and seen["images"][0][1] == "image/jpeg" and seen["images"][0][2] == photo
    assert ap["intake"]["analysis"]["provenance"]["path"] == "model" and ap["intake"]["analysis"]["provenance"]["images_sent"] == 1
    bullets = {b["text"]: b for b in ap["result"]["condition_bullets"]}
    assert bullets["Visible dent on left door"]["source"] == "image_observed" and bullets["Visible dent on left door"]["evidence"] == [photo]
    assert bullets["Check brake fluid level"]["source"] == "proposed_check"
    titles = {t["title"] for t in ap["result"]["tasks_created"]}
    assert titles == {"Inspect and repair left door damage", "Inspect for fluid leak"} and all(t.startswith("Inspect") for t in titles)
    issues = (await db.execute(select(ReconIssue).where(ReconIssue.vehicle_id == v["id"]))).scalars().all()
    assert {i.source_kind for i in issues} == {"image_observed"} and all(i.asset_ids == [photo] and i.status == "open" for i in issues)
    # an odometer read from a photo is recorded as a reported fact that still needs the owner; nothing is verified/ready/paid
    nc = ap["result"]["needs_confirmation"]
    assert nc and nc[0]["field"] == "odometer_km" and ap["result"]["facts"][0]["status"] == "reported"
    veh = ap["vehicle"]
    assert veh["recon_state"] == "needs_inspection" and veh["dates"]["inspected_at"] is None and veh["dates"]["ready_at"] is None
    assert ap["result"]["milestones"] == [] and veh["asking_price"] is None
    a = await db.get(Asset, photo)
    await db.refresh(a)
    assert a.public_eligible is False and a.classification == "unknown"
    tasks = (await db.execute(select(Task).where(Task.vehicle_id == v["id"]))).scalars().all()
    assert all(t.status == "open" and t.verification_status == "none" and t.source_kind == "intake" for t in tasks)


# ── I07 ──────────────────────────────────────────────────────────────────────
async def test_I07_corrections_keep_history_and_undo_restores(db, owner, mechanic):
    it = (await dispatch(ctx_for(db, owner), "intake.start", {"target_mode": "new"})).data["intake"]
    photo = await upload_via_commands(db, owner, jpeg_bytes(501), "truck.jpg")
    await dispatch(ctx_for(db, owner), "intake.add_assets", {"intake_id": it["id"], "asset_ids": [photo]})
    await dispatch(ctx_for(db, owner), "intake.add_note", {"intake_id": it["id"], "text": "left-door dent, needs tires"})
    ap = (await dispatch(ctx_for(db, owner), "intake.apply", {"intake_id": it["id"]})).data
    vid = ap["result"]["vehicle_id"]
    obs = {(o.kind, o.text): o for o in (await db.execute(select(IntakeObservation).where(IntakeObservation.intake_id == it["id"]))).scalars().all()}
    dent, tires_req = obs[("condition", "left-door dent")], obs[("request", "Inspect and replace tires")]
    dent_task = [t for t in ap["result"]["tasks_created"] if t["title"] == "Inspect and repair left door damage"][0]
    version_before = ap["result"]["condition_version"]
    # 1. edit a bullet through its observation: current summary changes, prior text kept
    c1 = (await dispatch(ctx_for(db, owner), "intake.correct", {"intake_id": it["id"], "observation_id": dent.id, "text": "Left door dent (small, no rust)", "reason": "wording"})).data
    assert c1["observation"]["text"] == "Left door dent (small, no rust)" and c1["observation"]["history"][0]["text_before"] == "left-door dent"
    row = await db.get(Vehicle, vid)
    await db.refresh(row)
    b = [b for b in row.condition_summary if b["id"] == dent.applied_id][0]
    assert b["text"] == "Left door dent (small, no rust)" and b["history"][0]["text_before"] == "left-door dent" and row.condition_version == version_before + 1
    # 2. remove a mistaken observation: bullet marked removed (evidence kept), its task cancelled, its issue rejected
    c2 = (await dispatch(ctx_for(db, owner), "intake.correct", {"intake_id": it["id"], "observation_id": tires_req.id, "remove": True, "reason": "tires are new"})).data
    assert {r["command"] for r in c2["change"]["results"]} == {"tasks.cancel", "shop.update_issue"}
    assert "Inspect and replace tires" not in await _task_titles(db, vid)
    issue = await db.get(ReconIssue, tires_req.meta["issue_id"])
    await db.refresh(issue)
    assert issue.status == "rejected" and issue.resolution_note == "tires are new"
    # 3. change the assignee of a created task
    c3 = (await dispatch(ctx_for(db, owner), "intake.correct", {"intake_id": it["id"], "task_id": dent_task["id"], "assignee_user_id": mechanic.id})).data
    t = await db.get(Task, dent_task["id"])
    await db.refresh(t)
    assert t.owner_user_id == mechanic.id and c3["change"]["results"][0]["command"] == "tasks.assign"
    itrow = await db.get(VehicleIntake, it["id"])
    await db.refresh(itrow)
    assert len(itrow.corrections) == 3 and itrow.vehicle_id == vid
    assert {b["text"] for b in itrow.result["condition_bullets"]} == {"Left door dent (small, no rust)", "needs tires"}
    # the target never silently changes: a new intake for another vehicle leaves this one bound
    other = await _vehicle(db, owner)
    await dispatch(ctx_for(db, owner), "intake.start", {"target_mode": "existing", "vehicle_id": other["id"]})
    await db.refresh(itrow)
    assert itrow.vehicle_id == vid
    with pytest.raises(NotFound):   # another person's intake is not visible to the mechanic at all
        await dispatch(ctx_for(db, mechanic), "intake.correct", {"intake_id": it["id"], "task_id": dent_task["id"], "assignee_user_id": owner.id})
    await db.rollback()
    for r in (owner, mechanic):
        await db.refresh(r)
    # 4. undo reverses the reversible changes: bullets removed, tasks cancelled, links retired, the created card archived
    un = (await dispatch(ctx_for(db, owner), "intake.undo", {"intake_id": it["id"], "reason": "wrong truck"})).data
    assert un["intake"]["status"] == "undone" and un["undo"]["vehicle_archived"] is True and dent_task["id"] in un["undo"]["tasks_cancelled"]
    await db.refresh(row)
    assert row.archived_at is not None and all(b.get("removed") for b in row.condition_summary) and await _task_titles(db, vid) == []
    assert await _links(db, photo, vid) == [] and (await db.get(Asset, photo)) is not None   # evidence retained
    obs2 = (await db.execute(select(IntakeObservation).where(IntakeObservation.intake_id == it["id"]))).scalars().all()
    assert all(o.status == "pending" and o.applied_id is None for o in obs2 if not o.removed)
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "intake.undo", {"intake_id": it["id"]})


# ── H13 ──────────────────────────────────────────────────────────────────────
async def test_H13_scoped_report_writes_unscoped_asks_for_the_vehicle(db, owner):
    v = await _vehicle(db, owner, make="Honda", model="Acty", model_year=1998, color="green")
    twin = await _vehicle(db, owner, make="Honda", model="Acty", model_year=1998, color="green")
    scoped = (await dispatch(ctx_for(db, owner), "intake.start", {"target_mode": "existing", "vehicle_id": v["id"], "channel": "agent"})).data["intake"]
    await dispatch(ctx_for(db, owner), "intake.add_note", {"intake_id": scoped["id"], "text": "this one needs tires"})
    ap = (await dispatch(ctx_for(db, owner, kind="agent"), "intake.apply", {"intake_id": scoped["id"]})).data
    assert ap["decision"] == "Allowed" and await _task_titles(db, v["id"]) == ["Inspect and replace tires"]
    iss = (await db.execute(select(ReconIssue).where(ReconIssue.vehicle_id == v["id"]))).scalars().all()
    assert len(iss) == 1 and iss[0].source_kind == "owner_reported" and iss[0].intake_observation_id
    # from unscoped Home with two similar trucks: the vehicle must be chosen before anything is written
    unscoped = (await dispatch(ctx_for(db, owner), "intake.start", {"target_mode": "find", "channel": "agent"})).data["intake"]
    await dispatch(ctx_for(db, owner), "intake.add_note", {"intake_id": unscoped["id"], "text": "this one needs tires"})
    ap = (await dispatch(ctx_for(db, owner, kind="agent"), "intake.apply", {"intake_id": unscoped["id"]})).data
    assert ap["decision"] == "Blocked" and ap["status"] in ("needs_choice", "needs_info") and ap["intake"]["vehicle_id"] is None
    assert await _task_titles(db, twin["id"]) == [] and await _task_titles(db, v["id"]) == ["Inspect and replace tires"]
    ch = (await dispatch(ctx_for(db, owner), "intake.choose_vehicle", {"intake_id": unscoped["id"], "vehicle_id": twin["id"]})).data
    assert ch["intake"]["vehicle_id"] == twin["id"] and "vehicle" not in ch["intake"]["missing_fields"]
    ap = (await dispatch(ctx_for(db, owner, kind="agent"), "intake.apply", {"intake_id": unscoped["id"]})).data
    assert ap["decision"] == "Allowed" and await _task_titles(db, twin["id"]) == ["Inspect and replace tires"]


# ── voice note without transcript, employee scope, abandon ──────────────────
async def test_voice_note_without_transcript_never_blocks_photos(db, owner):
    v = await _vehicle(db, owner)
    photo = await upload_via_commands(db, owner, jpeg_bytes(601), "p.jpg")
    audio = await upload_via_commands(db, owner, wav_bytes(seed=uuid.uuid4().int % 30000 + 1), "note.wav", "audio/wav")
    it = (await dispatch(ctx_for(db, owner), "intake.start", {"target_mode": "existing", "vehicle_id": v["id"]})).data["intake"]
    await dispatch(ctx_for(db, owner), "intake.add_assets", {"intake_id": it["id"], "asset_ids": [photo]})
    tr = (await dispatch(ctx_for(db, owner), "intake.add_transcript", {"intake_id": it["id"], "audio_asset_id": audio})).data
    assert tr["needs_transcript"] is True and "TRANSCRIBE_URL" in tr["transcription_error"] and tr["intake"]["status"] == "needs_info"
    assert tr["intake"]["missing_fields"] == ["transcript"] and tr["intake"]["transcript_asset_ids"] == [audio]
    ap = (await dispatch(ctx_for(db, owner), "intake.apply", {"intake_id": it["id"]})).data
    assert ap["result"]["photos_saved"] == 1 and "transcript" in ap["result"]["missing"] and ap["status"] == "needs_info" and ap["intake"]["complete"] is False
    assert len(await _links(db, photo, v["id"])) == 1 and len(await _links(db, audio, v["id"])) == 1
    typed = (await dispatch(ctx_for(db, owner), "intake.add_transcript", {"intake_id": it["id"], "audio_asset_id": audio, "text": "needs tires"})).data
    assert typed["needs_transcript"] is False and typed["intake"]["missing_fields"] == [] and typed["intake"]["status"] == "analyzed"
    a = await db.get(Asset, audio)
    await db.refresh(a)
    assert a.transcript == "needs tires" and a.analysis["transcript_source"] == "typed"
    ap2 = (await dispatch(ctx_for(db, owner), "intake.apply", {"intake_id": it["id"]})).data
    assert ap2["status"] == "applied" and [t["title"] for t in ap2["result"]["tasks_created"]] == ["Inspect and replace tires"]
    obs = (await db.execute(select(IntakeObservation).where(IntakeObservation.intake_id == it["id"], IntakeObservation.kind == "request"))).scalar_one()
    assert obs.source == "owner_voice"


async def test_employee_intake_limited_to_permitted_records(client, db, owner, mechanic):
    v = await _vehicle(db, owner)
    with pytest.raises(Denied):   # not assigned -> the mechanic cannot even open an intake on it
        await dispatch(ctx_for(db, mechanic), "intake.start", {"target_mode": "existing", "vehicle_id": v["id"]})
    await db.rollback()
    for r in (owner, mechanic):
        await db.refresh(r)
    await dispatch(ctx_for(db, owner), "tasks.create", {"title": f"Check lights {_u()}", "vehicle_id": v["id"], "owner_user_id": mechanic.id})
    it = (await dispatch(ctx_for(db, mechanic), "intake.start", {"target_mode": "existing", "vehicle_id": v["id"]})).data["intake"]
    photo = await upload_via_commands(db, mechanic, jpeg_bytes(701), "m.jpg")
    await dispatch(ctx_for(db, mechanic), "intake.add_assets", {"intake_id": it["id"], "asset_ids": [photo]})
    await dispatch(ctx_for(db, mechanic), "intake.add_note", {"intake_id": it["id"], "text": "needs tires, frame S510P-777777"})
    ap = (await dispatch(ctx_for(db, mechanic), "intake.apply", {"intake_id": it["id"]})).data
    assert ap["status"] == "applied" and "Inspect and replace tires" in await _task_titles(db, v["id"]) and ap["result"]["photos_saved"] == 1
    nc = ap["result"]["needs_confirmation"]
    assert nc and nc[0]["field"] == "frame_no" and "vehicles.write" in nc[0]["reason"] and ap["vehicle"]["frame_no_raw"] is None
    # the intake belongs to the mechanic: the owner can read it, another employee cannot
    login(client, owner)
    assert (await client.get(f"/api/vehicle-intakes/{it['id']}")).status_code == 200
    from backend.tests.conftest import make_user
    other = await make_user(db, f"mech2-{_u()}", "mechanic")
    login(client, other)
    assert (await client.get(f"/api/vehicle-intakes/{it['id']}")).status_code == 404
    # a mechanic cannot create vehicle cards
    it2 = (await dispatch(ctx_for(db, mechanic), "intake.start", {"target_mode": "new"})).data["intake"]
    await dispatch(ctx_for(db, mechanic), "intake.add_note", {"intake_id": it2["id"], "text": "needs tires"})
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, mechanic), "intake.apply", {"intake_id": it2["id"]})
    await db.rollback()
    for r in (owner, mechanic):
        await db.refresh(r)
    ab = (await dispatch(ctx_for(db, mechanic), "intake.abandon", {"intake_id": it2["id"], "reason": "not mine"})).data
    assert ab["intake"]["status"] == "abandoned"
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, mechanic), "intake.add_note", {"intake_id": it2["id"], "text": "x"})
    await db.rollback()


# ── F01: only an evidenced identity auto-links; similarity never assigns ─────
async def test_F01_only_evidenced_identity_auto_links(db, owner):
    tag = uuid.uuid4().int % 900000 + 100000
    similar = await _vehicle(db, owner, make="Subaru", model="Sambar", model_year=1996, color="gold")
    evidenced = await _vehicle(db, owner, make="Subaru", model="Sambar", model_year=1996, color="gold",
                               frame_no_raw=f"TT2-{tag}")
    # model / year / colour alone: one candidate, still no automatic assignment
    it = (await dispatch(ctx_for(db, owner), "intake.start", {"target_mode": "find"})).data["intake"]
    await dispatch(ctx_for(db, owner), "intake.add_note", {"intake_id": it["id"], "text": "gold 1996 sambar needs tires"})
    an = (await dispatch(ctx_for(db, owner), "intake.analyze", {"intake_id": it["id"]})).data
    assert an["intake"]["status"] == "needs_choice" and an["intake"]["vehicle_id"] is None
    assert similar["id"] in an["intake"]["candidate_vehicle_ids"]
    ap = (await dispatch(ctx_for(db, owner), "intake.apply", {"intake_id": it["id"]})).data
    assert ap["decision"] == "Blocked" and await _task_titles(db, similar["id"]) == []
    # a stated frame number is evidence: it resolves to that card and only that card
    it2 = (await dispatch(ctx_for(db, owner), "intake.start", {"target_mode": "find"})).data["intake"]
    await dispatch(ctx_for(db, owner), "intake.add_note", {"intake_id": it2["id"], "text": f"frame TT2-{tag}, needs tires"})
    ap2 = (await dispatch(ctx_for(db, owner), "intake.apply", {"intake_id": it2["id"]})).data
    assert ap2["decision"] == "Allowed" and ap2["result"]["vehicle_id"] == evidenced["id"]
    assert ap2["intake"]["choice"]["resolved"] in ("exact", "matched")
    assert await _task_titles(db, evidenced["id"]) == ["Inspect and replace tires"] and await _task_titles(db, similar["id"]) == []
