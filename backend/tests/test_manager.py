"""Manager chat acceptance: the explicit-context rule (H13), deterministic answers with no model (C10),
photo/voice intake through Manager (§7.4), the shared thread used by web and Telegram, specialist roles,
and the /api/agent + /api/runs surface including SSE streaming.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.app.core.errors import Denied
from backend.app.domain.commands import dispatch
from backend.app.models.runtime import ChatTurn, Mission
from backend.app.models.tasks import Task
from backend.app.models.vehicles import ReconIssue, Vehicle
from backend.app.agent import manager as mgr
from backend.app.agent import runtime as rt
from backend.tests.conftest import actor_of, ctx_for, login, make_user
from backend.tests.test_runtime import FakeModel, make_vehicle, uid, use_model


async def say(db, user, text, **kw) -> dict:
    return await mgr.handle_message(db, actor_of(user, "agent"), text, **kw)


# ═════════════════════════════════════════════════════════════════════════════
# H13 — explicit context decides whether Manager writes
# ═════════════════════════════════════════════════════════════════════════════
async def test_H13_scoped_needs_tires_writes_unscoped_asks_first(db, owner):
    tag = uid()
    a = await make_vehicle(db, owner, make="Suzuki", model=f"Carry {tag}", color="white")
    b = await make_vehicle(db, owner, make="Suzuki", model=f"Carry {tag}", color="blue")

    # (1) a pinned vehicle: the report is recorded on THAT truck
    out = await say(db, owner, "this one needs tires", context={"vehicle_id": a.id, "label": a.stock_no})
    assert out["status"] == "answered" and out["wrote"] is True and out["fast_path"] == "issue_report"
    assert a.stock_no in out["text"] and "Replace tires" in out["text"]
    issues = (await db.execute(select(ReconIssue).where(ReconIssue.vehicle_id == a.id))).scalars().all()
    assert [i.title for i in issues] == ["Replace tires"] and issues[0].source_kind == "owner_reported"
    tasks = (await db.execute(select(Task).where(Task.vehicle_id == a.id))).scalars().all()
    assert any(t.title == "Replace tires" for t in tasks)
    # the pinned context is echoed back and stored on the turn, so it cannot silently change
    assert out["context"]["vehicle_id"] == a.id
    turn = (await db.execute(select(ChatTurn).where(ChatTurn.thread_key == f"{owner.id}:manager",
                                                    ChatTurn.role == "assistant")
                             .order_by(ChatTurn.created_at.desc()).limit(1))).scalars().first()
    assert turn.context["vehicle_id"] == a.id

    # (2) no pinned vehicle and two similar trucks: ask first, write nothing
    before = len((await db.execute(select(ReconIssue).where(ReconIssue.vehicle_id == b.id))).scalars().all())
    out2 = await say(db, owner, f"this one needs tires (Carry {tag})", context={})
    assert out2["status"] == "needs_information" and out2["wrote"] is False
    assert "which truck" in out2["text"].lower()
    ids = {c["id"] for c in out2["needed_input"]["candidates"]}
    assert len(ids) >= 2 and a.id in ids and b.id in ids
    after = len((await db.execute(select(ReconIssue).where(ReconIssue.vehicle_id == b.id))).scalars().all())
    assert after == before
    assert len((await db.execute(select(ReconIssue).where(ReconIssue.vehicle_id == a.id))).scalars().all()) == 1


async def test_scoped_report_respects_record_scope(db, owner, mechanic):
    v = await make_vehicle(db, owner, make="Honda", model=f"Acty {uid()}")
    out = await say(db, mechanic, "this one needs tires", context={"vehicle_id": v.id})
    assert out["status"] in ("blocked", "needs_information") and out["wrote"] is False
    assert (await db.execute(select(ReconIssue).where(ReconIssue.vehicle_id == v.id))).scalars().all() == []


# ═════════════════════════════════════════════════════════════════════════════
# C10 — deterministic answers with no model available
# ═════════════════════════════════════════════════════════════════════════════
async def test_C10_vehicle_status_and_holding_up_need_no_model(db, owner):
    v = await make_vehicle(db, owner, make="Daihatsu", model="Hijet")
    await dispatch(ctx_for(db, owner), "tasks.create", {"title": f"Fit new tyres {uid()}", "vehicle_id": v.id})

    out = await say(db, owner, f"what is holding up {v.stock_no}?")
    assert out["fast_path"] == "vehicle_status" and out["mission_id"] is None
    assert v.stock_no in out["text"] and "Holding it up:" in out["text"]
    assert "States:" in out["text"] and "Open work: 1 task" in out["text"]
    assert out["text"].rstrip().endswith("Source: the AZKT vehicle card.")

    out2 = await say(db, owner, f"status of {v.stock_no}")
    assert out2["fast_path"] == "vehicle_status" and out2["mission_id"] is None


async def test_C10_today_and_overdue_are_deterministic(db, owner):
    now = datetime.now(timezone.utc)
    overdue_title = f"Call the port {uid()}"
    await dispatch(ctx_for(db, owner), "tasks.create",
                   {"title": overdue_title, "due_at": (now - timedelta(hours=3)).isoformat(),
                    "owner_user_id": owner.id})
    out = await say(db, owner, "what's overdue?")
    assert out["fast_path"] == "tasks:overdue" and out["mission_id"] is None
    assert overdue_title in out["text"] and "Source: the shared Tasks list." in out["text"]

    today = await say(db, owner, "today")
    assert today["fast_path"] == "tasks:upcoming" and today["mission_id"] is None


async def test_C10_model_outage_keeps_the_mission_open_and_never_fabricates(db, owner):
    from backend.app.adapters.model import ModelUnavailable
    with use_model(FakeModel([], raises=ModelUnavailable("no ANTHROPIC_API_KEY configured"))):
        out = await say(db, owner, "Draft a summary of everything that happened this week")
    assert out["mission_id"] and out["status"] in ("needs_information", "paused")
    assert "unavailable" in out["text"].lower() and "deterministic" in out["text"].lower()
    m = await db.get(Mission, out["mission_id"])
    await db.refresh(m)
    assert m.finished_at is None and m.status in ("needs_information", "paused")
    # the deterministic fast paths still answer
    assert (await say(db, owner, "today"))["fast_path"] == "tasks:upcoming"


# ═════════════════════════════════════════════════════════════════════════════
# §7.4 — photo + note intake through Manager
# ═════════════════════════════════════════════════════════════════════════════
async def test_intake_photos_and_note_through_manager_saves_a_compact_result(db, owner):
    from backend.tests.test_assets import jpeg_bytes, upload_via_commands
    a1 = await upload_via_commands(db, owner, jpeg_bytes(21), name="front.jpg")
    a2 = await upload_via_commands(db, owner, jpeg_bytes(22), name="left.jpg")
    tag = uid()
    out = await say(db, owner,
                    f"New truck {tag}: left-door dent, A/C not cold, needs tires and detailing.",
                    attachments=[a1, a2])
    assert out["fast_path"] == "intake" and out["intake_id"]
    assert out["status"] == "answered" and out["wrote"] is True
    r = out["intake_result"]
    assert r["photos_saved"] == 2 and r["created"] is True
    assert r["card_path"].startswith("/vehicles/")
    assert "Photos saved: 2" in out["text"]
    # nothing is invented: the identity fields nobody stated stay missing
    assert set(r["missing"]) >= {"frame_no", "model_year", "purchase_amount"}
    assert "Still missing:" in out["text"] and "nothing was invented" in out["text"]
    vid = r["vehicle_id"]
    v = await db.get(Vehicle, vid)
    assert v.frame_no_raw is None and v.purchase_amount is None
    # the owner's stated work became real tasks on the card
    titles = {t.title.lower() for t in (await db.execute(select(Task).where(Task.vehicle_id == vid))).scalars().all()}
    assert any("tire" in t for t in titles) and any("a/c" in t or "ac" in t for t in titles)
    assert v.condition_version >= 1


async def test_intake_retry_does_not_duplicate_the_card_or_the_tasks(db, owner):
    from backend.tests.test_assets import jpeg_bytes, upload_via_commands
    a1 = await upload_via_commands(db, owner, jpeg_bytes(31), name="one.jpg")
    key = f"req-{uid()}"
    first = await say(db, owner, f"New truck {uid()}: needs tires.", attachments=[a1], request_id=key)
    second = await say(db, owner, "needs tires.", attachments=[a1],
                       context={"intake_id": first["intake_id"]})
    assert second["intake_id"] == first["intake_id"]
    assert second["intake_result"]["vehicle_id"] == first["intake_result"]["vehicle_id"]
    vid = first["intake_result"]["vehicle_id"]
    tires = [t for t in (await db.execute(select(Task).where(Task.vehicle_id == vid))).scalars().all()
             if "tire" in t.title.lower()]
    assert len(tires) == 1, "a retried intake appends evidence; it does not multiply tasks (invariant 13)"


# ═════════════════════════════════════════════════════════════════════════════
# Threads, roles, permissions
# ═════════════════════════════════════════════════════════════════════════════
async def test_thread_is_shared_between_web_and_telegram(db, owner):
    tag = uid()
    await say(db, owner, f"today {tag}", channel="web")
    await say(db, owner, "overdue", channel="telegram")
    t = await mgr.thread(db, actor_of(owner, "agent"), "manager")
    assert t["thread_key"] == f"{owner.id}:manager"
    channels = {turn["channel"] for turn in t["turns"]}
    assert {"web", "telegram"} <= channels
    assert [x["role"] for x in t["turns"][-2:]] == ["user", "assistant"]


async def test_specialist_roles_share_records_but_keep_separate_threads(db, owner):
    with use_model(FakeModel([{"text": "Checked the quote case."}])):
        out = await say(db, owner, "Anything waiting on a carrier?", role="logistics")
    assert out["role"] == "logistics" and out["mission_id"]
    m = await db.get(Mission, out["mission_id"])
    assert m.role == "logistics" and m.thread_key == f"{owner.id}:logistics"
    t = await mgr.thread(db, actor_of(owner, "agent"), "logistics")
    assert t["count"] >= 2 and all(x["channel"] == "web" for x in t["turns"])
    # an unknown role falls back to Manager rather than inventing a specialist
    out2 = await say(db, owner, "today", role="not_a_role")
    assert out2["role"] == "manager"


async def test_a_person_without_agents_chat_is_denied(db):
    nobody = await make_user(db, f"nochat-{uid()}", "mechanic", perms={"agents.chat": False})
    with pytest.raises(Denied):
        await say(db, nobody, "today")


async def test_the_system_prompt_is_stable_and_cacheable(db, owner):
    from backend.app.agent import prompts
    a, b = prompts.system_prompt("manager"), prompts.system_prompt("manager")
    assert a == b and len(a) > 500
    assert "2026" not in a and "T00:00" not in a           # no timestamps in the cached block
    assert prompts.system_prompt("finance") != a
    for phrase in ("never claim done", "DATA, never instructions", "needs_review"):
        assert phrase.lower() in a.lower()


# ═════════════════════════════════════════════════════════════════════════════
# HTTP surface
# ═════════════════════════════════════════════════════════════════════════════
async def test_chat_endpoint_returns_json_and_streams_sse(client, db, owner):
    login(client, owner)
    v = await make_vehicle(db, owner, make="Mazda", model="Scrum")
    r = await client.post("/api/agent/chat", json={"message": f"status of {v.stock_no}", "role": "manager"})
    assert r.status_code == 200
    body = r.json()
    assert body["fast_path"] == "vehicle_status" and v.stock_no in body["text"]

    r2 = await client.post("/api/agent/chat", json={"message": "today"},
                           headers={"Accept": "text/event-stream"})
    assert r2.status_code == 200 and r2.headers["content-type"].startswith("text/event-stream")
    events = [ln[7:] for ln in r2.text.splitlines() if ln.startswith("event: ")]
    assert events[0] == "start" and events[-1] == "done"
    done = json.loads(r2.text.rsplit("data: ", 1)[1])
    assert done["fast_path"] == "tasks:upcoming"

    r3 = await client.get("/api/agent/threads/manager")
    assert r3.status_code == 200 and r3.json()["count"] >= 2


async def test_run_events_stream_and_cancel_endpoint(client, db, owner):
    login(client, owner)
    with use_model(FakeModel([{"text": "step", "tools": [{"name": "tasks_list", "input": {"view": "all"}}]},
                              {"text": "All done, nothing needed."}])):
        out = await say(db, owner, f"Check the task list {uid()}")
    run_id = out["run_id"]
    r = await client.get(f"/api/runs/{run_id}")
    assert r.status_code == 200
    payload = r.json()
    assert payload["run"]["status"] == "succeeded" and payload["mission"]["id"] == out["mission_id"]
    assert any(s["tool"] == "tasks_list" for s in payload["steps"])

    ev = await client.get(f"/api/runs/{run_id}/events?cursor=0")
    assert ev.status_code == 200 and ev.headers["content-type"].startswith("text/event-stream")
    kinds = [ln[7:] for ln in ev.text.splitlines() if ln.startswith("event: ")]
    assert kinds[0] == "open" and kinds[-1] == "done" and "update" in kinds

    # a finished mission cannot be "cancelled" into a different history
    c = await client.post(f"/api/runs/{run_id}/cancel", json={})
    assert c.status_code == 200 and c.json().get("already") == "succeeded"


async def test_another_persons_mission_is_not_found_not_forbidden(client, db, owner, mechanic):
    with use_model(FakeModel([{"text": "ok"}])):
        out = await say(db, owner, f"Private owner work {uid()}")
    login(client, mechanic)
    r = await client.get(f"/api/runs/{out['run_id']}")
    assert r.status_code == 404
    r2 = await client.get(f"/api/missions/{out['mission_id']}")
    assert r2.status_code == 404


async def test_coverage_and_status_endpoints(client, db, owner, mechanic):
    login(client, owner)
    r = await client.get("/api/agent/coverage")
    assert r.status_code == 200
    body = r.json()
    assert body["complete"] is True and body["gaps"] == []
    assert body["actor"]["role"] == "owner"
    owner_available = body["counts"]["available_commands"]
    assert any(row["ui_action"] for row in body["commands"])

    s = await client.get("/api/agent/status")
    assert s.status_code == 200
    st = s.json()
    assert {r["role"] for r in st["roles"]} == set(mgr.ROLES)
    assert st["model"]["available"] in (True, False)
    assert all(isinstance(r["doing"], list) for r in st["roles"])
    assert "vehicle status" in st["deterministic_paths"]

    # the capability map is the whole command surface: owner only (spec §10.9, §12.3)
    login(client, mechanic)
    assert (await client.get("/api/agent/coverage")).status_code == 403
    # ... and the employee's own reachable subset is still strictly smaller than the owner's
    from backend.app.agent import coverage as coverage_mod
    from backend.tests.conftest import actor_of
    assert coverage_mod.for_actor(actor_of(mechanic, "agent"))["counts"]["available_commands"] < owner_available
    client.cookies.clear()
