"""Tasks API over services/tasks.py: C03 revision semantics, C05 time zones across DST, buckets without
duplicate rows, summary/schedule, mechanic sees only own work (A02 flavour), verify is owner-only."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from backend.app.domain.commands import dispatch
from backend.app.models import User
from backend.app.models.tasks import Task
from backend.tests.conftest import ctx_for, login, make_user


def _u() -> str:
    return uuid.uuid4().hex[:8]


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).replace(microsecond=0).isoformat()


# ── C03 ─────────────────────────────────────────────────────────────────────
async def test_C03_reschedule_bumps_revision_and_clears_snooze_snooze_keeps_due(client, owner):
    login(client, owner)
    due = _iso(timedelta(hours=2))
    r = await client.post("/api/tasks", json={"title": f"Meet {_u()}", "type": "meeting", "due_at": due, "reminder_kind": "1h",
                                              "owner_user_id": owner.id})
    t = r.json()["data"]["task"]
    assert t["schedule_revision"] == 1 and t["snoozed_until"] is None
    r = await client.post(f"/api/tasks/{t['id']}/snooze", json={"minutes": 20, "expected_version": t["version"]})
    s = r.json()["data"]["task"]
    assert s["schedule_revision"] == 2 and s["snoozed_until"] and s["due_at"] == due   # meeting time unchanged
    assert s["snoozed_until_local"].endswith("AZ")
    r = await client.post(f"/api/tasks/{t['id']}/snooze", json={"minutes": 5, "expected_version": t["version"]})
    assert r.status_code == 409   # stale version -> conflict
    new_due = _iso(timedelta(hours=5))
    r = await client.post(f"/api/tasks/{t['id']}/reschedule", json={"due_at": new_due, "reminder_kind": "15m", "expected_version": s["version"]})
    rs = r.json()["data"]["task"]
    assert rs["schedule_revision"] == 3 and rs["snoozed_until"] is None and rs["due_at"] == new_due and rs["reminder_kind"] == "15m"
    r = await client.post(f"/api/tasks/{t['id']}/cancel", json={"reason": "moved"})
    assert r.json()["data"]["task"]["status"] == "cancelled" and r.json()["data"]["task"]["schedule_revision"] == 4
    r = await client.post(f"/api/tasks/{t['id']}/snooze", json={"minutes": 5})
    assert r.status_code == 409
    r = await client.post(f"/api/tasks/{t['id']}/reopen", json={})
    assert r.json()["data"]["task"]["status"] == "open" and r.json()["data"]["task"]["schedule_revision"] == 5
    r = await client.post(f"/api/tasks/{t['id']}/complete", json={"note": "done"})
    assert r.json()["data"]["task"]["status"] == "completed" and "completed" in r.json()["data"]["task"]["buckets"]


# ── C05 ─────────────────────────────────────────────────────────────────────
async def test_C05_dst_region_and_tokyo_keep_correct_utc_instants(client, owner):
    login(client, owner)
    # America/New_York springs forward on 2027-03-14 at 02:00 local. 01:30 EST = 06:30Z; 03:30 EDT = 07:30Z.
    r = await client.post("/api/tasks", json={"title": f"NY call {_u()}", "type": "call", "due_at": "2027-03-14T01:30:00",
                                              "timezone": "America/New_York", "owner_user_id": owner.id, "dedupe": False})
    t = r.json()["data"]["task"]
    assert t["due_at"] == "2027-03-14T06:30:00+00:00" and t["timezone"] == "America/New_York"
    assert t["due_local_iso"] == "2027-03-14T01:30:00-05:00" and t["zone_due"].startswith("Sun Mar 14 · 01:30")
    r = await client.post(f"/api/tasks/{t['id']}/reschedule", json={"due_at": "2027-03-14T03:30:00"})   # naive -> task zone
    t2 = r.json()["data"]["task"]
    assert t2["due_at"] == "2027-03-14T07:30:00+00:00" and t2["due_local_iso"] == "2027-03-14T03:30:00-04:00"
    assert t2["local_due"].startswith("Sun Mar 14 · 00:30") and t2["local_due"].endswith("AZ")   # Phoenix has no DST
    r = await client.post(f"/api/tasks/{t['id']}/reschedule", json={"due_at": "2027-03-14T03:30:00-04:00"})   # explicit offset wins
    assert r.json()["data"]["task"]["due_at"] == "2027-03-14T07:30:00+00:00"
    r = await client.post(f"/api/tasks/{t['id']}/reschedule", json={"due_at": "2027-03-14T03:30:00", "timezone": "Not/AZone"})
    assert r.status_code == 422
    # Tokyo: 10:00 JST on Apr 1 is 01:00Z, which is 18:00 the previous day in Phoenix
    r = await client.post("/api/tasks", json={"title": f"Auction watch {_u()}", "type": "follow_up", "due_at": "2027-04-01T10:00:00+09:00",
                                              "timezone": "Asia/Tokyo", "owner_user_id": owner.id, "dedupe": False})
    jp = r.json()["data"]["task"]
    assert jp["due_at"] == "2027-04-01T01:00:00+00:00" and jp["tokyo_due"].startswith("Thu Apr 1 · 10:00") and jp["tokyo_due"].endswith("JST")
    assert jp["local_due"].startswith("Wed Mar 31 · 18:00") and jp["local_due"].endswith("AZ")
    # jp=true adds the Tokyo display to a Phoenix task without changing its instant
    r = await client.get(f"/api/tasks/{t['id']}", params={"jp": "true"})
    assert r.json()["task"]["tokyo_due"].startswith("Sun Mar 14 · 16:30") and r.json()["task"]["due_at"] == "2027-03-14T07:30:00+00:00"
    r = await client.get(f"/api/tasks/{t['id']}")
    assert r.json()["task"]["tokyo_due"] is None


# ── views, buckets, summary, schedule ───────────────────────────────────────
async def test_buckets_do_not_duplicate_rows_and_summary_counts(client, db, owner):
    login(client, owner)
    tag = _u()
    overdue_unassigned = (await client.post("/api/tasks", json={"title": f"Overdue unassigned {tag}", "due_at": _iso(timedelta(hours=-3))})).json()["data"]["task"]
    upcoming = (await client.post("/api/tasks", json={"title": f"Upcoming {tag}", "due_at": _iso(timedelta(hours=1)), "owner_user_id": owner.id})).json()["data"]["task"]
    blocked = (await client.post("/api/tasks", json={"title": f"Blocked {tag}", "owner_user_id": owner.id})).json()["data"]["task"]
    await client.post(f"/api/tasks/{blocked['id']}/block", json={"reason": "waiting on parts"})
    ev = (await client.post("/api/tasks", json={"title": f"Verify me {tag}", "owner_user_id": owner.id,
                                                "evidence_required": [{"kind": "note", "min": 1}]})).json()["data"]["task"]
    await client.post(f"/api/tasks/{ev['id']}/evidence", json={"note": "done, see photo"})
    await client.post(f"/api/tasks/{ev['id']}/complete", json={})
    r = await client.get("/api/tasks", params={"view": "all", "bucket": "overdue"})
    items = [x for x in r.json()["items"] if tag in x["title"]]
    assert [x["id"] for x in items] == [overdue_unassigned["id"]]
    assert set(items[0]["buckets"]) == {"overdue", "unassigned"}   # one row, several buckets
    r = await client.get("/api/tasks", params={"view": "all", "bucket": "unassigned"})
    assert [x["id"] for x in r.json()["items"] if tag in x["title"]] == [overdue_unassigned["id"]]
    r = await client.get("/api/tasks", params={"view": "all", "bucket": "upcoming"})
    ids = [x["id"] for x in r.json()["items"] if tag in x["title"]]
    assert ids == [upcoming["id"]]
    r = await client.get("/api/tasks", params={"view": "all", "bucket": "blocked"})
    assert [x["id"] for x in r.json()["items"] if tag in x["title"]] == [blocked["id"]]
    r = await client.get("/api/tasks", params={"view": "all", "bucket": "awaiting_verification"})
    assert [x["id"] for x in r.json()["items"] if tag in x["title"]] == [ev["id"]]
    r = await client.get("/api/tasks", params={"view": "all"})
    all_ids = [x["id"] for x in r.json()["items"] if tag in x["title"]]
    assert len(all_ids) == len(set(all_ids)) == 4 and r.json()["total"] >= 4
    r = await client.get("/api/tasks", params={"view": "my"})
    mine = [x["id"] for x in r.json()["items"] if tag in x["title"]]
    assert overdue_unassigned["id"] not in mine and upcoming["id"] in mine
    s = (await client.get("/api/tasks/summary")).json()
    assert s["overdue"] >= 1 and s["unassigned"] >= 1 and s["blocked"] >= 1 and s["awaiting_verification"] >= 1 and s["today"] >= 1
    r = await client.get("/api/tasks", params={"view": "all", "bucket": "nope"})
    assert r.status_code == 422


async def test_schedule_range_groups_by_local_day(client, owner):
    login(client, owner)
    tag = _u()
    base = datetime(2027, 6, 10, 16, 0, tzinfo=timezone.utc)   # 09:00 Phoenix
    for i in range(3):
        await client.post("/api/tasks", json={"title": f"Sched {tag} {i}", "due_at": (base + timedelta(days=i)).isoformat(),
                                              "owner_user_id": owner.id, "dedupe": False})
    r = await client.get("/api/tasks/schedule", params={"from": "2027-06-10T00:00:00", "to": "2027-06-12T00:00:00", "tz": "America/Phoenix"})
    body = r.json()
    items = [x for x in body["items"] if tag in x["title"]]
    assert [x["day"] for x in items] == ["2027-06-10", "2027-06-11"] and body["timezone"] == "America/Phoenix"
    assert body["from"] == "2027-06-10T07:00:00+00:00"
    r = await client.get("/api/tasks/schedule", params={"from": "2027-06-12T00:00:00", "to": "2027-06-11T00:00:00"})
    assert r.status_code == 422


# ── visibility (A02 flavour) and owner-only verification ───────────────────
async def test_A02_mechanic_sees_only_own_tasks_and_manager_sees_reports(client, db, owner, manager, mechanic):
    tag = _u()
    other_mech = await make_user(db, f"mech-{tag}", "mechanic", display_name="Other")
    mechanic.manager_id = manager.id
    await db.commit()
    login(client, owner)
    mine = (await client.post("/api/tasks", json={"title": f"Tires {tag}", "owner_user_id": mechanic.id, "notes": "owner-only cost 900 USD"})).json()["data"]["task"]
    theirs = (await client.post("/api/tasks", json={"title": f"Brakes {tag}", "owner_user_id": other_mech.id, "dedupe": False})).json()["data"]["task"]
    owners = (await client.post("/api/tasks", json={"title": f"Owner private {tag}", "owner_user_id": owner.id, "dedupe": False})).json()["data"]["task"]
    unassigned = (await client.post("/api/tasks", json={"title": f"Nobody {tag}", "dedupe": False})).json()["data"]["task"]
    login(client, mechanic)
    r = await client.get("/api/tasks", params={"view": "all"})
    ids = {x["id"] for x in r.json()["items"] if tag in x["title"]}
    assert ids == {mine["id"]}
    assert (await client.get(f"/api/tasks/{theirs['id']}")).status_code == 404
    assert (await client.get(f"/api/tasks/{owners['id']}")).status_code == 404
    r = await client.get(f"/api/tasks/{mine['id']}")
    assert r.status_code == 200 and r.json()["task"]["title"] == f"Tires {tag}"
    r = await client.get("/api/tasks/summary")
    assert r.status_code == 200 and r.json()["unassigned"] == 0
    r = await client.post(f"/api/tasks/{theirs['id']}/start", json={})
    assert r.status_code == 403 and "Brakes" not in r.text
    r = await client.post(f"/api/tasks/{mine['id']}/start", json={})
    assert r.status_code == 200 and r.json()["data"]["task"]["status"] == "in_progress"
    login(client, manager)
    r = await client.get("/api/tasks", params={"view": "all"})
    ids = {x["id"] for x in r.json()["items"] if tag in x["title"]}
    assert ids == {mine["id"], unassigned["id"]}    # own + reports + unassigned, not the owner's or another team's
    r = await client.post(f"/api/tasks/{unassigned['id']}/assign", json={"owner_user_id": mechanic.id})
    assert r.status_code == 200 and r.json()["data"]["task"]["owner_user_id"] == mechanic.id


async def test_verify_and_reject_are_owner_only_via_policy(client, db, owner, manager, mechanic):
    tag = _u()
    login(client, owner)
    t = (await client.post("/api/tasks", json={"title": f"Photo job {tag}", "owner_user_id": mechanic.id,
                                               "evidence_required": [{"kind": "note", "min": 1}]})).json()["data"]["task"]
    login(client, mechanic)
    r = await client.post(f"/api/tasks/{t['id']}/complete", json={})
    assert r.status_code == 409 and r.json()["error"] == "blocked"   # evidence missing
    await client.post(f"/api/tasks/{t['id']}/evidence", json={"note": "replaced, torqued"})
    r = await client.post(f"/api/tasks/{t['id']}/complete", json={})
    assert r.json()["data"]["task"]["status"] == "awaiting_verification"
    login(client, manager)
    r = await client.post(f"/api/tasks/{t['id']}/verify", json={})
    assert r.status_code == 403
    r = await client.post(f"/api/tasks/{t['id']}/reject", json={"reason": "blurry"})
    assert r.status_code == 403
    login(client, mechanic)
    assert (await client.post(f"/api/tasks/{t['id']}/verify", json={})).status_code == 403
    login(client, owner)
    r = await client.post(f"/api/tasks/{t['id']}/verify", json={})
    assert r.status_code == 200 and r.json()["data"]["task"]["status"] == "completed"
    assert r.json()["data"]["task"]["verification_status"] == "verified" and r.json()["data"]["task"]["verified_by"] == owner.id
    row = await db.get(Task, t["id"])
    await db.refresh(row)
    assert row.status == "completed"
    r = await client.post(f"/api/tasks/{t['id']}/nonsense", json={})
    assert r.status_code == 404


async def test_external_client_task_visibility_follows_its_vehicle_grant(db, owner):
    """A record-limited external client (spec §10.8, invariant 14) sees only tasks on the vehicles in its grant:
    never the owner's private follow-ups or another customer's call, even though they carry no vehicle."""
    from backend.app.domain.actors import Actor
    from backend.app.domain.policy import effective_perms
    from backend.app.models.vehicles import Vehicle
    from backend.app.routers.tasks import visibility_clauses
    tag = _u()
    v1, v2 = Vehicle(stock_no=f"STK-X1{tag[:4]}", title="in scope"), Vehicle(stock_no=f"STK-X2{tag[:4]}", title="out of scope")
    db.add_all([v1, v2])
    await db.commit()
    for title, vid in ((f"Ext in {tag}", v1.id), (f"Ext out {tag}", v2.id), (f"Ext none {tag}", None)):
        await dispatch(ctx_for(db, owner), "tasks.create", {"title": title, "vehicle_id": vid, "owner_user_id": owner.id, "dedupe": False})
    client = Actor(kind="external", user_id=owner.id, role="owner", scope="all", perms=effective_perms("owner", {}),
                   client_id=f"client-{tag}", client_name="partner", client_scopes=["read:tasks"],
                   client_record_scope={"vehicle_ids": [v1.id]})
    clauses = await visibility_clauses(db, client, "all")
    rows = (await db.execute(select(Task).where(Task.title.ilike(f"%{tag}%"), *clauses))).scalars().all()
    assert [t.title for t in rows] == [f"Ext in {tag}"]
    unlimited = Actor(kind="external", user_id=owner.id, role="owner", scope="all", perms=effective_perms("owner", {}),
                      client_id=f"client-all-{tag}", client_scopes=["read:tasks"])
    rows = (await db.execute(select(Task).where(Task.title.ilike(f"%{tag}%"), *await visibility_clauses(db, unlimited, "all")))).scalars().all()
    assert len(rows) == 3


async def test_cases_and_promises_views_are_scoped_and_name_contacts_only_with_contacts_read(client, db, owner, mechanic):
    from backend.app.models.contacts import Contact
    from backend.app.models.tasks import Case, Commitment
    from backend.app.models.vehicles import Vehicle
    mine = (await dispatch(ctx_for(db, owner), "vehicles.create", {"make": "Suzuki", "model": "Carry", "logistics_state": "received",
                                                                   "create_missing_task": False, "stock_no": f"CP-{uuid.uuid4().hex[:6]}"})).data["vehicle"]
    other = (await dispatch(ctx_for(db, owner), "vehicles.create", {"make": "Honda", "model": "Acty", "logistics_state": "received",
                                                                    "create_missing_task": False, "stock_no": f"CO-{uuid.uuid4().hex[:6]}"})).data["vehicle"]
    t = (await dispatch(ctx_for(db, owner), "tasks.create", {"title": "Inspect", "vehicle_id": mine["id"]})).data["task"]
    await dispatch(ctx_for(db, owner), "tasks.assign", {"task_id": t["id"], "owner_user_id": mechanic.id})
    c = Contact(name="Maria Reyes")
    db.add(c); await db.flush()
    past = datetime.now(timezone.utc) - timedelta(days=1)
    db.add_all([
        Case(title="Quote from MOL", kind="shipping_quote", status="waiting", vehicle_id=mine["id"], next_check_at=past, waiting_on="MOL"),
        Case(title="Dispute", kind="dispute", status="open", vehicle_id=other["id"]),
        Commitment(text="Call back with the price", contact_id=c.id, vehicle_id=mine["id"], due_at=past, status="open"),
        Commitment(text="Send photos", contact_id=c.id, vehicle_id=other["id"], status="open"),
    ])
    await db.commit()

    login(client, owner)
    r = await client.get("/api/tasks/cases")
    assert r.status_code == 200
    titles = {i["title"]: i for i in r.json()["items"]}
    assert "Quote from MOL" in titles and "Dispute" in titles and titles["Quote from MOL"]["overdue_check"] is True
    r = await client.get("/api/tasks/promises")
    texts = {i["text"]: i for i in r.json()["items"]}
    assert texts["Call back with the price"]["overdue"] is True and texts["Call back with the price"]["contact_name"] == "Maria Reyes"

    login(client, mechanic)  # assigned scope, no contacts.read
    r = await client.get("/api/tasks/cases")
    assert {i["title"] for i in r.json()["items"]} == {"Quote from MOL"}
    r = await client.get("/api/tasks/promises")
    items = r.json()["items"]
    assert [i["text"] for i in items] == ["Call back with the price"] and items[0]["contact_name"] is None
    # the literal path segments never resolve to a task id lookup
    assert (await client.get("/api/tasks/cases?status=all")).status_code == 200
