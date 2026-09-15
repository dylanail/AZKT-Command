"""One notification source for the bell, Home and the mobile sheet (spec §2.2, §5.4).

Covers grouping and the badge, record scope, money hidden without `costs.read`, acknowledge/snooze/
dismiss through commands, the truthful delivery states behind a task, and the preferences surface.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from backend.app.domain.actors import Actor
from backend.app.models.notify import Notification
from backend.app.models.runtime import Approval
from backend.app.services import notifications_feed, reminders
from backend.tests.conftest import actor_of, login, run_worker_once


def _u() -> str:
    return uuid.uuid4().hex[:8]


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).replace(microsecond=0).isoformat()


async def test_the_feed_is_one_source_for_bell_home_and_mobile(client, db, owner):
    login(client, owner)
    tag = _u()
    overdue = (await client.post("/api/tasks", json={"title": f"Overdue {tag}", "due_at": _iso(timedelta(hours=-2)),
                                                     "owner_user_id": owner.id, "dedupe": False})).json()["data"]["task"]
    soon = (await client.post("/api/tasks", json={"title": f"Soon {tag}", "due_at": _iso(timedelta(hours=3)),
                                                  "owner_user_id": owner.id, "dedupe": False})).json()["data"]["task"]
    api = (await client.get("/api/notifications")).json()
    direct = await notifications_feed.build(db, actor_of(owner))
    assert {i["id"] for i in api["high"]} == {i["id"] for i in direct["high"]}
    assert any(i["entity_id"] == overdue["id"] and i["urgency"] == "high" for i in api["high"])
    assert any(i["entity_id"] == soon["id"] and i["urgency"] == "today" for i in api["today"])
    assert api["badge"]["count"] == len(api["high"]) + len(api["today"])
    assert api["badge"]["color"] == "red" and api["badge"]["high"] >= 1
    assert all("group_key" in i for i in api["high"] + api["today"] + api["later"])
    keys = [i["group_key"] for i in api["high"] + api["today"] + api["later"]]
    assert len(keys) == len(set(keys)), "one row per underlying problem"


async def test_record_scope_limits_the_feed_and_the_delivery_states(client, db, owner, mechanic):
    tag = _u()
    login(client, owner)
    mine = (await client.post("/api/tasks", json={"title": f"Mechanic work {tag}", "due_at": _iso(timedelta(hours=-1)),
                                                  "owner_user_id": mechanic.id, "dedupe": False})).json()["data"]["task"]
    theirs = (await client.post("/api/tasks", json={"title": f"Owner work {tag}", "due_at": _iso(timedelta(hours=-1)),
                                                    "owner_user_id": owner.id, "dedupe": False})).json()["data"]["task"]
    login(client, mechanic)
    feed = (await client.get("/api/notifications")).json()
    ids = {i["entity_id"] for i in feed["high"] + feed["today"] + feed["later"]}
    assert mine["id"] in ids and theirs["id"] not in ids
    assert not any(i["kind"] == "approval" for i in feed["today"]), "approvals need the approve permission"
    r = await client.get("/api/notifications/deliveries", params={"task_id": theirs["id"]})
    assert r.status_code == 403
    r = await client.get("/api/notifications/deliveries", params={"task_id": mine["id"]})
    assert r.status_code == 200 and "not proof" in r.json()["note"]


async def test_delivery_states_are_truthful(client, db, owner):
    login(client, owner)
    tag = _u()
    t = (await client.post("/api/tasks", json={"title": f"States {tag}", "due_at": _iso(timedelta(hours=-3)),
                                               "owner_user_id": owner.id, "dedupe": False})).json()["data"]["task"]
    for _ in range(2):
        await run_worker_once()
    rows = (await client.get("/api/notifications/deliveries", params={"task_id": t["id"]})).json()["deliveries"]
    overdue = [d for d in rows if d["kind"] == "overdue"][0]
    assert overdue["state"] == "accepted" and overdue["state_label"] == "Provider accepted"
    assert overdue["provider_ref"] and overdue["sent_at"] and overdue["delivered_at"] is None
    assert overdue["dedupe_key"].endswith(f":overdue:{owner.id}:email")


async def test_money_in_approval_rows_is_hidden_without_costs_read(client, db, owner):
    tag = _u()
    a = Approval(kind="parts_order", command_name="parts.order", payload={}, payload_hash=f"h{tag}",
                 title=f"Order compressor {tag}", status="pending", requested_by={"kind": "agent"},
                 consequence={"amount": "1200.00", "currency": "USD", "moves_money": True},
                 expires_at=datetime.now(timezone.utc) + timedelta(hours=8))
    db.add(a)
    await db.flush()
    a.review_path = f"/approvals/{a.id}"
    await db.commit()
    try:
        rich = actor_of(owner)
        feed = await notifications_feed.build(db, rich)
        row = [i for i in feed["today"] if i["id"] == a.id][0]
        assert row["consequence"]["amount"] == "1200.00"
        # an approver without costs.read sees the decision, never the amount
        poor = Actor(kind="user", user_id=owner.id, role="owner", scope="all",
                     perms={**rich.perms, "costs.read": False, "finance.status": False})
        feed = await notifications_feed.build(db, poor)
        row = [i for i in feed["today"] if i["id"] == a.id][0]
        assert row["consequence"]["amount"] is None and row["consequence"]["money_hidden"] is True
        assert row["title"] == a.title
    finally:
        a.status = "expired"
        await db.commit()


async def test_acknowledge_snooze_and_dismiss_go_through_commands(client, db, owner):
    tag = _u()
    note, created = await reminders.notify(db, user_id=owner.id, kind="case_update", urgency="later",
                                           title=f"Case update {tag}", body="Carrier replied",
                                           entity_kind="case", entity_id=f"case-{tag}",
                                           group_key=f"case:{tag}", dedupe=f"case:{tag}")
    await db.commit()
    assert created
    login(client, owner)
    feed = (await client.get("/api/notifications")).json()
    assert any(i["id"] == note.id for i in feed["later"])

    r = await client.post(f"/api/notifications/{note.id}/snooze", json={"minutes": 120})
    assert r.json()["data"]["notification"]["state"] == "snoozed"
    feed = (await client.get("/api/notifications")).json()
    assert not any(i["id"] == note.id for i in feed["later"]), "a snoozed row is hidden until it is due"

    r = await client.post(f"/api/notifications/{note.id}/acknowledge", json={})
    assert r.json()["data"]["notification"]["state"] == "acknowledged"
    feed = (await client.get("/api/notifications")).json()
    assert not any(i["id"] == note.id for i in feed["high"] + feed["today"] + feed["later"])

    r = await client.post(f"/api/notifications/{note.id}/explode", json={})
    assert r.status_code == 404


async def test_a_notification_belongs_to_one_person(client, db, owner, mechanic):
    tag = _u()
    note, _ = await reminders.notify(db, user_id=owner.id, kind="case_update", urgency="later",
                                     title=f"Private {tag}", dedupe=f"private:{tag}", group_key=f"private:{tag}")
    await db.commit()
    login(client, mechanic)
    r = await client.post(f"/api/notifications/{note.id}/acknowledge", json={})
    assert r.status_code == 403
    feed = (await client.get("/api/notifications")).json()
    assert not any(i["id"] == note.id for i in feed["later"])


async def test_repeated_facts_about_one_problem_collapse(client, db, owner):
    tag = _u()
    for _ in range(4):
        await reminders.notify(db, user_id=owner.id, kind="connection_issue", urgency="high",
                               title=f"Sheet {tag} needs attention", body="not an all-clear",
                               group_key=f"connection:sheet{tag}", dedupe=f"connection:sheet{tag}:warn:{owner.id}")
    await db.commit()
    rows = (await db.execute(select(Notification).where(
        Notification.dedupe_key == f"connection:sheet{tag}:warn:{owner.id}"))).scalars().all()
    assert len(rows) == 1 and rows[0].occurrences == 4
    login(client, owner)
    feed = (await client.get("/api/notifications")).json()
    row = [i for i in feed["high"] if i["group_key"] == f"connection:sheet{tag}"][0]
    assert row["occurrences"] == 4
    await client.post(f"/api/notifications/{rows[0].id}/dismiss", json={})


async def test_prefs_surface_shows_channels_defaults_and_pairing(client, db, owner):
    login(client, owner)
    body = (await client.get("/api/notifications/prefs")).json()
    channels = body["prefs"]["notification_prefs"]["channels"]
    assert channels["task_reminder"] == "email_only" and channels["overdue"] == "email_only"
    assert channels["case_update"] == "telegram_fallback_email"
    assert body["business_defaults"]["overdue_delay_minutes"] == 60
    assert set(body["telegram"]) >= {"active", "history", "bot_username", "webhook_secret_configured"}
    assert body["write_with"] == "PATCH /api/me/prefs"

    r = await client.patch("/api/notifications/prefs", json={"digest_time": "07:15"})
    assert r.status_code == 200
    body = (await client.get("/api/notifications/prefs")).json()
    assert body["prefs"]["notification_prefs"]["digest_time"] == "07:15"
    r = await client.patch("/api/notifications/prefs", json={"role": "owner"})
    assert r.status_code == 403
    await client.patch("/api/notifications/prefs", json={"digest_time": "08:00"})


async def test_the_feed_needs_a_signed_in_person(client):
    client.cookies.clear()
    r = await client.get("/api/notifications")
    assert r.status_code in (401, 403)


async def test_expected_version_is_enforced_on_a_notification_write(client, db, owner):
    """A write that carries `expected_version` must conflict, not silently win (ARCHITECTURE rule 4)."""
    tag = _u()
    note, _ = await reminders.notify(db, user_id=owner.id, kind="case_update", urgency="later",
                                     title=f"Versioned {tag}", dedupe=f"versioned:{tag}",
                                     group_key=f"versioned:{tag}")
    await db.commit()
    login(client, owner)
    stale = note.version + 5
    r = await client.post(f"/api/notifications/{note.id}/snooze", json={"minutes": 30, "expected_version": stale})
    assert r.status_code == 409 and "changed since" in r.json()["message"]
    fresh = (await client.get("/api/notifications")).json()
    row = [i for i in fresh["later"] if i["id"] == note.id][0]
    assert row["state"] == "unread", "the refused write changed nothing"
    r = await client.post(f"/api/notifications/{note.id}/snooze",
                          json={"minutes": 30, "expected_version": note.version})
    assert r.status_code == 200 and r.json()["data"]["notification"]["state"] == "snoozed"
    await client.post(f"/api/notifications/{note.id}/dismiss", json={})


async def test_another_persons_delivery_destination_is_not_in_the_feed_api(client, db, owner, mechanic):
    """A sales task reminds the assignee *and* Dylan. The assignee may see that Dylan was reminded and
    whether it worked; Dylan's email address and private Telegram chat are not their record data."""
    login(client, owner)
    tag = _u()
    t = (await client.post("/api/tasks", json={"title": f"Sales call {tag}", "type": "call",
                                               "due_at": _iso(timedelta(hours=-3)),
                                               "owner_user_id": mechanic.id, "dedupe": False})).json()["data"]["task"]
    for _ in range(2):
        await run_worker_once()
    login(client, mechanic)
    rows = (await client.get("/api/notifications/deliveries", params={"task_id": t["id"]})).json()["deliveries"]
    mine = [d for d in rows if d["recipient_user_id"] == mechanic.id]
    theirs = [d for d in rows if d["recipient_user_id"] != mechanic.id]
    assert mine and theirs, "both people are reminded about a sales task"
    assert all(d["receipt"].get("to") for d in mine) and all(d["destination_hidden"] is False for d in mine)
    for d in theirs:
        assert d["destination_hidden"] is True
        assert "to" not in d["receipt"] and "chat_id" not in d["receipt"]
        assert d["state"] and d["state_label"], "the state itself is still visible"
    login(client, owner)
    rows = (await client.get("/api/notifications/deliveries", params={"task_id": t["id"]})).json()["deliveries"]
    assert all(d["receipt"].get("to") for d in rows), "Dylan set these addresses up and still sees them"


async def test_H11_an_unchecked_source_is_not_reported_as_all_clear(client, db, owner, monkeypatch):
    """Failing to *evaluate* freshness is itself a reason not to claim all-clear (spec §2.2, H11)."""
    from backend.app.services import connections

    async def boom(_db):
        raise RuntimeError("connection overview unavailable")
    monkeypatch.setattr(connections, "overview", boom)
    status = await notifications_feed.feed_status(db, 0)
    assert status["all_clear"] is False and status["sources_checked"] is False
    assert "not an all-clear" in status["message"] and "could not be checked" in status["message"]
    login(client, owner)
    feed = (await client.get("/api/notifications")).json()
    assert feed["status"]["all_clear"] is False


async def test_inapp_preference_switches_bell_items_per_kind(client, db, owner):
    """The Settings UI can turn the in-app bell off entirely or one reminder kind at a time."""
    login(client, owner)
    r = await client.patch("/api/notifications/prefs", json={"inapp": {"task_reminder": False}})
    assert r.status_code == 200, r.text
    await db.refresh(owner)
    assert (owner.notification_prefs or {}).get("inapp") == {"task_reminder": False}
    assert reminders.inapp_enabled(owner, "task_reminder") is False
    assert reminders.inapp_enabled(owner, "digest") is True

    r = await client.patch("/api/notifications/prefs", json={"inapp": {"not_a_kind": False}})
    assert r.status_code == 422 and "unknown reminder kind" in r.text

    r = await client.patch("/api/notifications/prefs", json={"inapp": False})
    assert r.status_code == 200
    await db.refresh(owner)
    assert reminders.inapp_enabled(owner) is False
    prefs = (await client.get("/api/notifications/prefs")).json()["prefs"]["notification_prefs"]
    assert prefs["inapp"] is False
    await client.patch("/api/notifications/prefs", json={"inapp": True})
    await db.refresh(owner)


async def test_prefs_say_where_owner_reminders_go_without_exposing_the_address(client, db, owner, mechanic):
    from backend.app.core.config import settings

    login(client, owner)
    d = (await client.get("/api/notifications/prefs")).json()["business_defaults"]
    assert d["owner_reminder_email_configured"] is True
    assert d["owner_reminder_email"] == settings.OWNER_REMINDER_EMAIL, "the owner sees their own address"
    assert d["owner_reminder_email_masked"].endswith("@example.com") and "***" in d["owner_reminder_email_masked"]

    login(client, mechanic)
    d = (await client.get("/api/notifications/prefs")).json()["business_defaults"]
    assert d["owner_reminder_email_configured"] is True
    assert d["owner_reminder_email"] == d["owner_reminder_email_masked"]
    assert settings.OWNER_REMINDER_EMAIL not in str(d), "the full owner address never reaches a non-owner"


async def test_mine_deliveries_are_recipient_scoped_and_hide_other_destinations(client, db, owner, mechanic):
    login(client, owner)
    tag = _u()
    mine = (await client.post("/api/tasks", json={"title": f"Mechanic reminder {tag}", "type": "call",
                                                  "due_at": _iso(timedelta(hours=3)), "reminder_kind": "15m",
                                                  "owner_user_id": mechanic.id, "dedupe": False})).json()["data"]["task"]
    theirs = (await client.post("/api/tasks", json={"title": f"Owner reminder {tag}", "type": "call",
                                                    "due_at": _iso(timedelta(hours=3)), "reminder_kind": "15m",
                                                    "owner_user_id": owner.id, "dedupe": False})).json()["data"]["task"]
    await run_worker_once()

    login(client, mechanic)
    body = (await client.get("/api/notifications/deliveries", params={"mine": "true"})).json()
    assert body["mine"] is True and len(body["deliveries"]) <= 50
    task_ids = {d["task_id"] for d in body["deliveries"]}
    assert mine["id"] in task_ids and theirs["id"] not in task_ids, "only the caller's own rows"
    assert all(d["recipient_user_id"] == mechanic.id for d in body["deliveries"])
    assert all(d["destination_hidden"] is False for d in body["deliveries"]), "their own destination is theirs to see"
    assert reminders.recipient_email(owner) not in str(body), "another person's address is never in this list"
    assert body["deliveries"] == sorted(body["deliveries"], key=lambda d: d["deliver_at"] or "", reverse=True)


async def test_bell_actions_work_on_derived_task_and_approval_ids(client, db, owner):
    """The bell shows derived items (`task:<id>`); snoozing one has to stick, so the action
    materialises a real notification row for the caller and the feed honours it."""
    login(client, owner)
    tag = _u()
    t = (await client.post("/api/tasks", json={"title": f"Derived {tag}", "due_at": _iso(timedelta(hours=-3)),
                                               "owner_user_id": owner.id, "dedupe": False})).json()["data"]["task"]
    feed = (await client.get("/api/notifications")).json()
    item = next(i for i in feed["high"] if i["entity_id"] == t["id"])
    assert item["id"] == f"task:{t['id']}" and item["source"] == "derived"

    r = await client.post(f"/api/notifications/{item['id']}/snooze", json={"minutes": 120})
    assert r.status_code == 200, r.text
    row = r.json()["data"]["notification"]
    assert row["state"] == "snoozed" and row["derived_ref"] == f"task:{t['id']}" and row["entity_id"] == t["id"]
    feed = (await client.get("/api/notifications")).json()
    assert not any(i["entity_id"] == t["id"] for i in feed["high"] + feed["today"] + feed["later"]), \
        "a snoozed bell item stays hidden until it is due again"
    stored = (await db.execute(select(Notification).where(Notification.id == row["id"]))).scalar_one()
    assert stored.user_id == owner.id and stored.dedupe_key.endswith(f"task:{t['id']}")
    acts = (await client.get("/api/activity", params={"entity_kind": "notification", "entity_id": row["id"]})).json()
    assert any("Snoozed" in a["what"] for a in acts["items"]), "the action is audited"

    # dismissing the same derived id upserts the same row, it never creates a second one
    r = await client.post(f"/api/notifications/{item['id']}/dismiss", json={})
    assert r.json()["data"]["notification"]["id"] == row["id"]
    assert await db.scalar(select(func.count()).select_from(Notification).where(
        Notification.dedupe_key == stored.dedupe_key)) == 1

    # an approval feed id works the same way
    a = Approval(kind="send_message", command_name="test.send", payload={}, payload_hash=f"h{tag}",
                 status="pending", title=f"Derived approval {tag}", approval_version=1)
    db.add(a)
    await db.commit()
    r = await client.post(f"/api/notifications/approval:{a.id}/dismiss", json={})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["notification"]["entity_kind"] == "approval"
    feed = (await client.get("/api/notifications")).json()
    assert not any(i["entity_id"] == a.id for i in feed["today"]), "a dismissed approval leaves the bell"

    # someone else's task is not theirs to snooze
    from backend.tests.conftest import make_user
    other = await make_user(db, f"bell{tag}", "mechanic")
    login(client, other)
    r = await client.post(f"/api/notifications/task:{t['id']}/snooze", json={"minutes": 10})
    assert r.status_code == 403
