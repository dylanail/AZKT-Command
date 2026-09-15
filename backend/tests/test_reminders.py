"""Durable reminders (spec §5.4, §5.6, §12.4).

C02 a chosen offset produces a real server-scheduled delivery row.
C03 reschedule / snooze / cancel just before firing suppresses the obsolete delivery; snooze keeps the meeting time.
C04 restart drill: a delivery scheduled before the worker stopped is delivered exactly once, labelled late.
C07 one overdue email per task revision; blocker events group by case; an empty digest is not sent.
C10 reminders, the feed and the task lists work with no AI model configured.
E08 the owner deposit email happens exactly once per payment, even when two domains confirm it.
H11 a stale connection never produces an all-clear.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.app.adapters.email import MemoryTransport
from backend.app.core.config import settings
from backend.app.domain.commands import dispatch
from backend.app.models.comms import Connection
from backend.app.models.notify import Notification, ScheduledDelivery
from backend.app.models.runtime import Event
from backend.app.services import email_templates, notifications_feed, reminders
from backend.tests.conftest import ctx_for, login, make_user, run_worker_once


def _u() -> str:
    return uuid.uuid4().hex[:8]


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).replace(microsecond=0).isoformat()


async def _fresh(db):
    """Other sessions committed meanwhile; refresh what this session already holds."""
    for obj in list(db.identity_map.values()):
        if isinstance(obj, (ScheduledDelivery, Notification)):
            await db.refresh(obj)


def _mails(marker: str) -> list[dict]:
    return [m for m in MemoryTransport.sent if marker in (m["subject"] + m["text"])]


async def _deliveries(db, task_id: str) -> list[ScheduledDelivery]:
    await _fresh(db)
    q = (select(ScheduledDelivery).where(ScheduledDelivery.task_id == task_id)
         .order_by(ScheduledDelivery.kind, ScheduledDelivery.channel).execution_options(populate_existing=True))
    return list((await db.execute(q)).scalars().all())


# ── C02 ─────────────────────────────────────────────────────────────────────
async def test_C02_chosen_offset_creates_a_real_scheduled_delivery(client, db, owner):
    login(client, owner)
    tag = _u()
    due = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=3)
    r = await client.post("/api/tasks", json={"title": f"Call Tomás {tag}", "type": "call", "due_at": due.isoformat(),
                                              "timezone": "America/Phoenix", "reminder_kind": "15m",
                                              "owner_user_id": owner.id, "dedupe": False})
    t = r.json()["data"]["task"]
    assert t["reminder_kind"] == "15m" and t["timezone"] == "America/Phoenix"
    await run_worker_once()
    rows = await _deliveries(db, t["id"])
    by = {(x.kind, x.channel): x for x in rows}
    assert ("task_reminder", "email") in by and ("overdue", "email") in by
    tr = by[("task_reminder", "email")]
    assert tr.state == "scheduled" and tr.task_revision == 1
    assert tr.dedupe_key == f"{t['id']}:1:task_reminder:{owner.id}:email"
    assert abs((tr.deliver_at - (due - timedelta(minutes=15))).total_seconds()) < 2
    assert abs((by[("overdue", "email")].deliver_at - (due + timedelta(minutes=60))).total_seconds()) < 2
    # the API exposes the same durable states to the UI
    res = await client.get("/api/notifications/deliveries", params={"task_id": t["id"]})
    states = {(d["kind"], d["state_label"]) for d in res.json()["deliveries"]}
    assert ("task_reminder", "Queued") in states


async def test_a_meeting_that_already_ended_gets_no_starts_in_15_minutes_email(client, db, owner):
    login(client, owner)
    tag = _u()
    r = await client.post("/api/tasks", json={"title": f"Past meeting {tag}", "type": "meeting",
                                              "due_at": _iso(timedelta(hours=-4)), "reminder_kind": "15m",
                                              "owner_user_id": owner.id, "dedupe": False})
    t = r.json()["data"]["task"]
    await run_worker_once()
    kinds = {x.kind for x in await _deliveries(db, t["id"])}
    assert "task_reminder" not in kinds     # never a misleading "starts in 15 minutes" (spec §5.6)
    assert "overdue" in kinds


async def test_a_passed_offset_on_an_upcoming_task_fires_now(client, db, owner):
    login(client, owner)
    tag = _u()
    r = await client.post("/api/tasks", json={"title": f"Soon {tag}", "type": "call",
                                              "due_at": _iso(timedelta(minutes=20)), "reminder_kind": "1h",
                                              "owner_user_id": owner.id, "dedupe": False})
    t = r.json()["data"]["task"]
    await run_worker_once()
    tr = [x for x in await _deliveries(db, t["id"]) if x.kind == "task_reminder"]
    assert tr, "the offset already passed but the appointment has not: remind now"
    assert tr[0].deliver_at <= datetime.now(timezone.utc) + timedelta(seconds=5)


# ── C03 ─────────────────────────────────────────────────────────────────────
async def test_C03_snooze_keeps_the_meeting_time_and_supersedes_the_old_delivery(client, db, owner):
    login(client, owner)
    tag = _u()
    due = _iso(timedelta(hours=4))
    t = (await client.post("/api/tasks", json={"title": f"Snoozeme {tag}", "type": "call", "due_at": due,
                                               "reminder_kind": "1h", "owner_user_id": owner.id,
                                               "dedupe": False})).json()["data"]["task"]
    await run_worker_once()
    first = [x for x in await _deliveries(db, t["id"]) if x.kind == "task_reminder"][0]
    s = (await client.post(f"/api/tasks/{t['id']}/snooze", json={"minutes": 45})).json()["data"]["task"]
    assert s["due_at"] == t["due_at"], "snooze must not move the meeting"
    await run_worker_once()
    rows = await _deliveries(db, t["id"])
    old = [x for x in rows if x.id == first.id][0]
    assert old.state == "superseded" and "revision" in (old.cancel_reason or "")
    new = [x for x in rows if x.kind == "task_reminder" and x.task_revision == s["schedule_revision"]]
    assert new and abs((new[0].deliver_at - datetime.fromisoformat(s["snoozed_until"])).total_seconds()) < 2
    assert not _mails(tag), "nothing was due yet, so nothing was sent"


async def test_C03_cancel_just_before_firing_suppresses_the_obsolete_delivery(client, db, owner):
    login(client, owner)
    tag = _u()
    t = (await client.post("/api/tasks", json={"title": f"Cancelme {tag}", "type": "call",
                                               "due_at": _iso(timedelta(hours=2)), "reminder_kind": "1h",
                                               "owner_user_id": owner.id, "dedupe": False})).json()["data"]["task"]
    await run_worker_once()
    rows = await _deliveries(db, t["id"])
    row = [x for x in rows if x.kind == "task_reminder"][0]
    # the reminder becomes due, and the task is cancelled in the same moment (before the worker runs again)
    row.deliver_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    await db.commit()
    await client.post(f"/api/tasks/{t['id']}/cancel", json={"reason": "customer moved it"})
    await run_worker_once()
    await _fresh(db)
    await db.refresh(row)
    assert row.state == "cancelled" and row.cancel_reason
    assert not _mails(tag)
    assert all(x.state in ("cancelled", "superseded") for x in await _deliveries(db, t["id"]))


async def test_C03_complete_before_firing_sends_nothing(client, db, owner):
    login(client, owner)
    tag = _u()
    t = (await client.post("/api/tasks", json={"title": f"Completeme {tag}", "type": "follow_up",
                                               "due_at": _iso(timedelta(hours=2)), "reminder_kind": "1h",
                                               "owner_user_id": owner.id, "dedupe": False})).json()["data"]["task"]
    await run_worker_once()
    row = [x for x in await _deliveries(db, t["id"]) if x.kind == "task_reminder"][0]
    row.deliver_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    await db.commit()
    await client.post(f"/api/tasks/{t['id']}/complete", json={})
    await run_worker_once()
    assert not _mails(tag)


# ── C04 ─────────────────────────────────────────────────────────────────────
async def test_C04_reminder_survives_restart_and_is_delivered_once_with_a_late_label(client, db, owner):
    login(client, owner)
    tag = _u()
    t = (await client.post("/api/tasks", json={"title": f"Call Jordan {tag}", "type": "call",
                                               "due_at": _iso(timedelta(hours=2)), "reminder_kind": "1h",
                                               "owner_user_id": owner.id, "dedupe": False})).json()["data"]["task"]
    await run_worker_once()                       # the schedule is durable before the "outage"
    row = [x for x in await _deliveries(db, t["id"]) if x.kind == "task_reminder"][0]
    assert row.state == "scheduled"
    # the worker was down across the deadline
    row.deliver_at = datetime.now(timezone.utc) - timedelta(minutes=45)
    await db.commit()
    before = len(_mails(tag))
    await run_worker_once()                       # restart
    await _fresh(db)
    await db.refresh(row)
    assert row.state == "accepted" and row.late is True and row.provider_ref
    mails = _mails(tag)
    assert len(mails) == before + 1
    assert "late" in mails[-1]["text"].lower() and not mails[-1]["subject"].lower().startswith("reminder:")
    await run_worker_once()
    assert len(_mails(tag)) == before + 1, "a restart must not repeat a delivered reminder"


async def test_a_missed_reminder_older_than_the_obsolete_window_collapses_into_the_digest(client, db, owner):
    login(client, owner)
    tag = _u()
    t = (await client.post("/api/tasks", json={"title": f"Ancient {tag}", "type": "call",
                                               "due_at": _iso(timedelta(hours=6)), "reminder_kind": "1h",
                                               "owner_user_id": owner.id, "dedupe": False})).json()["data"]["task"]
    await run_worker_once()
    row = [x for x in await _deliveries(db, t["id"]) if x.kind == "task_reminder"][0]
    row.deliver_at = datetime.now(timezone.utc) - timedelta(hours=settings.REMINDER_OBSOLETE_HOURS + 2)
    await db.commit()
    await run_worker_once()
    await _fresh(db)
    await db.refresh(row)
    assert row.state == "expired" and "digest" in (row.cancel_reason or "")
    assert not _mails(tag)


# ── C07 ─────────────────────────────────────────────────────────────────────
async def test_C07_one_overdue_email_per_task_revision(client, db, owner):
    login(client, owner)
    tag = _u()
    t = (await client.post("/api/tasks", json={"title": f"Overdue once {tag}", "type": "call",
                                               "due_at": _iso(timedelta(hours=-3)), "owner_user_id": owner.id,
                                               "dedupe": False})).json()["data"]["task"]
    for _ in range(3):
        await run_worker_once()
    mails = [m for m in _mails(tag) if m["subject"].startswith("Overdue:")]
    assert len(mails) == 1, "repeated sweeps never repeat the overdue email"
    rows = [x for x in await _deliveries(db, t["id"]) if x.kind == "overdue"]
    assert len(rows) == 1 and rows[0].state == "accepted"
    # a new revision is new work, so one more (and only one more) overdue email
    await client.post(f"/api/tasks/{t['id']}/reschedule", json={"due_at": _iso(timedelta(hours=-2))})
    for _ in range(3):
        await run_worker_once()
    mails = [m for m in _mails(tag) if m["subject"].startswith("Overdue:")]
    assert len(mails) == 2


async def test_C07_related_blockers_on_one_case_group_into_a_single_feed_row(client, db, owner, mechanic):
    login(client, owner)
    tag = _u()
    case_id = f"case-{tag}"
    ids = []
    for i in range(3):
        t = (await client.post("/api/tasks", json={"title": f"Blocked {i} {tag}", "case_id": case_id,
                                                   "owner_user_id": owner.id, "dedupe": False})).json()["data"]["task"]
        await client.post(f"/api/tasks/{t['id']}/block", json={"reason": f"waiting on parts {i}"})
        ids.append(t["id"])
    feed = (await client.get("/api/notifications")).json()
    rows = [i for i in feed["high"] if i["group_key"] == f"case:{case_id}"]
    assert len(rows) == 1 and rows[0]["occurrences"] == 3, "one row per underlying problem, not a storm"


async def test_C07_empty_digest_is_not_sent_but_a_real_one_is(client, db, owner):
    from zoneinfo import ZoneInfo
    login(client, owner)
    tag = _u()
    quiet = await make_user(db, f"quiet{tag}", "mechanic", email=f"quiet{tag}@example.com")
    local = datetime.now(timezone.utc).astimezone(ZoneInfo(quiet.timezone))
    await dispatch(ctx_for(db, quiet), "me.update_prefs",
                   {"notification_prefs": {"digest_time": local.strftime("%H:%M")}})
    await dispatch(ctx_for(db, owner), "settings.update",
                   {"key": "reminders", "value": {"employee_reminders_enabled": True}})
    try:
        await reminders.digest_sweep(_sf())
        await _fresh(db)
        rows = (await db.execute(select(ScheduledDelivery).where(
            ScheduledDelivery.recipient_user_id == quiet.id, ScheduledDelivery.kind == "digest"))).scalars().all()
        assert rows == [], "a digest with nothing in it is not sent"
        t = (await client.post("/api/tasks", json={"title": f"Digest me {tag}", "due_at": _iso(timedelta(hours=-2)),
                                                   "owner_user_id": quiet.id, "dedupe": False})).json()["data"]["task"]
        await reminders.digest_sweep(_sf())
        await _fresh(db)
        rows = (await db.execute(select(ScheduledDelivery).where(
            ScheduledDelivery.recipient_user_id == quiet.id, ScheduledDelivery.kind == "digest"))).scalars().all()
        assert len(rows) == 1 and rows[0].payload["overdue"], "a non-empty digest is queued once for the day"
        await reminders.digest_sweep(_sf())
        await _fresh(db)
        again = (await db.execute(select(ScheduledDelivery).where(
            ScheduledDelivery.recipient_user_id == quiet.id, ScheduledDelivery.kind == "digest")
            .execution_options(populate_existing=True))).scalars().all()
        assert len(again) == 1, "once a day"
        subject, text, html = email_templates.render("digest", {"overdue": rows[0].payload["overdue"],
                                                                "today": [], "approvals": [], "tomorrow": []})
        assert subject.startswith("Today:") and "overdue" in subject and "1 tasks" not in subject
    finally:
        await dispatch(ctx_for(db, owner), "settings.update",
                       {"key": "reminders", "value": {"employee_reminders_enabled": False}})


def _sf():
    from backend.app import db as dbmod
    return dbmod.SessionLocal


# ── C10 ─────────────────────────────────────────────────────────────────────
async def test_C10_reminders_and_the_feed_work_with_no_model(client, db, owner, monkeypatch):
    from backend.app.adapters import model as model_adapter
    calls = []

    class _Boom:
        def __init__(self, *a, **k):
            calls.append(a)
            raise AssertionError("the deterministic reminder path must never call the model")

    monkeypatch.setattr(model_adapter, "ModelClient", _Boom)
    monkeypatch.setattr(model_adapter, "available", lambda: False)
    login(client, owner)
    tag = _u()
    t = (await client.post("/api/tasks", json={"title": f"No model {tag}", "type": "call",
                                               "due_at": _iso(timedelta(hours=-2)), "owner_user_id": owner.id,
                                               "dedupe": False})).json()["data"]["task"]
    for _ in range(2):
        await run_worker_once()
    assert [m for m in _mails(tag) if m["subject"].startswith("Overdue:")]
    feed = (await client.get("/api/notifications")).json()
    assert any(i["entity_id"] == t["id"] for i in feed["high"])
    assert feed["status"]["message"] and "summary" not in feed["status"]["message"].lower()
    assert (await client.get("/api/tasks", params={"bucket": "overdue"})).status_code == 200
    assert calls == []


# ── E08 ─────────────────────────────────────────────────────────────────────
async def test_E08_owner_deposit_reminder_happens_exactly_once(client, db, owner):
    tag = _u()
    payment_id = f"pay-{tag}"
    for i in range(2):     # finance confirms it, then sourcing confirms the same payment
        db.add(Event(type="deposit.confirmed", aggregate_type="invoice" if i == 0 else "import_request",
                     aggregate_id=f"inv-{tag}" if i == 0 else f"req-{tag}",
                     payload={"invoice_id": f"inv-{tag}", "payment_id": payment_id, "amount": "1000.00",
                              "currency": "USD", "pipeline": "vehicle", "handoff_kind": "sale",
                              "provider_receipt_ref": f"sq-{tag}"},
                     happened_at=datetime.now(timezone.utc), actor={}))
        await db.commit()
        await run_worker_once()
    await _fresh(db)
    rows = (await db.execute(select(ScheduledDelivery).where(
        ScheduledDelivery.kind == "deposit_confirmed",
        ScheduledDelivery.dedupe_key.like(f"deposit:{payment_id}:%")).execution_options(populate_existing=True)))
    rows = list(rows.scalars().all())
    assert len(rows) == 1 and rows[0].recipient_user_id == owner.id and rows[0].channel == "email"
    await run_worker_once()
    mails = [m for m in MemoryTransport.sent if m["subject"].startswith("Deposit paid") and tag in m["text"]]
    assert len(mails) == 1
    assert "1,000.00 USD" in mails[0]["text"], "money detail goes to the owner"
    await run_worker_once()
    assert len([m for m in MemoryTransport.sent if m["subject"].startswith("Deposit paid") and tag in m["text"]]) == 1
    notes = (await db.execute(select(Notification).where(Notification.dedupe_key == f"deposit:{payment_id}:{owner.id}")
                              .execution_options(populate_existing=True))).scalars().all()
    assert len(notes) == 1


# ── H11 ─────────────────────────────────────────────────────────────────────
async def test_H11_a_stale_connection_never_claims_all_clear(client, db, owner):
    """Other domains own the gmail_business connection rows, so this test makes every one of them stale
    and restores the exact prior state afterwards."""
    login(client, owner)
    conns = (await db.execute(select(Connection).where(Connection.provider == "gmail_business")
                              .execution_options(populate_existing=True))).scalars().all()
    if not conns:
        conns = [Connection(provider="gmail_business", label="Business email (info@azkeitrucks.com)",
                            status="connected", environment="test")]
        db.add(conns[0])
        await db.flush()
        created = True
    else:
        created = False
    prior = [(c, c.status, c.last_success_at, dict(c.failure or {})) for c in conns]
    for c in conns:
        c.status = "connected"
        c.last_success_at = datetime.now(timezone.utc) - timedelta(hours=6)
    db.add(Event(type="connection.degraded", aggregate_type="connection", aggregate_id=conns[0].id,
                 payload={"provider": "gmail_business", "kind": "auth_expired", "message": "token expired"},
                 happened_at=datetime.now(timezone.utc), actor={}))
    await db.commit()
    await run_worker_once()
    await _fresh(db)
    try:
        status = await notifications_feed.feed_status(db, 0)
        assert status["all_clear"] is False
        assert status["message"].startswith("No urgent items found in synced data;")
        assert "needs attention" in status["message"] and "Business email" in status["message"]

        feed = (await client.get("/api/notifications")).json()
        assert feed["status"]["all_clear"] is False and feed["status"]["stale_connections"]
        note = (await db.execute(select(Notification).where(
            Notification.dedupe_key == f"connection:gmail_business:auth_expired:{owner.id}")
            .execution_options(populate_existing=True))).scalar_one()
        assert note.urgency == "high" and "not an all-clear" in note.body and "needs attention" in note.title
        assert any(i["group_key"] == "connection:gmail_business" for i in feed["high"])

        # one "needs attention" per incident, not per failed poll
        for _ in range(2):
            db.add(Event(type="connection.degraded", aggregate_type="connection", aggregate_id=conns[0].id,
                         payload={"provider": "gmail_business", "kind": "auth_expired", "message": "token expired"},
                         happened_at=datetime.now(timezone.utc), actor={}))
            await db.commit()
            await run_worker_once()
        await _fresh(db)
        rows = (await db.execute(select(ScheduledDelivery).where(
            ScheduledDelivery.kind == "connection_issue",
            ScheduledDelivery.dedupe_key.like("connissue:gmail_business:%"))
            .execution_options(populate_existing=True))).scalars().all()
        assert len({r.dedupe_key for r in rows}) == len(rows)
        note = (await db.execute(select(Notification).where(
            Notification.dedupe_key == f"connection:gmail_business:auth_expired:{owner.id}")
            .execution_options(populate_existing=True))).scalar_one()
        assert all(r.dedupe_key.startswith(f"connissue:gmail_business:auth_expired:{note.id}") for r in rows), \
            "one 'needs attention' per incident, whatever the channel"
        assert note.occurrences >= 3
        await client.post(f"/api/notifications/{note.id}/dismiss", json={})
    finally:
        if created:
            await db.delete(conns[0])
        else:
            for c, st, last, failure in prior:
                c.status, c.last_success_at, c.failure = st, last, failure
        await db.commit()


# ── email templates ─────────────────────────────────────────────────────────
def test_the_four_templates_follow_the_design_rules():
    now = datetime.now(timezone.utc)
    s, text, html = email_templates.render("task_reminder", {
        "title": "Call Tomás Aldana", "task_id": "t1", "due_at": now + timedelta(minutes=15), "now": now,
        "pipeline": "IRQ · Awaiting Deposit", "contact": "+1 (520) 555-3391", "owner": True,
        "amount": "9500", "currency": "USD"})
    assert s.startswith("Call Tomás Aldana in 15 minutes") and not s.lower().startswith("reminder:")
    assert "#E6B35C" in html and html.count("#E6B35C") == 1, "exactly one amber rule"
    assert "background:#121212;border-radius:10px" in html, "one charcoal button"
    assert f"{email_templates.origin()}/tasks/t1" in html and "9,500.00 USD" in text
    # secondary links open an authenticated review; a GET never mutates (C06)
    assert f"{email_templates.origin()}/tasks/t1?action=done" in html
    assert "nothing changes until you do" in text

    s2, text2, _ = email_templates.render("task_reminder", {
        "title": "Call Tomás Aldana", "task_id": "t1", "due_at": now + timedelta(minutes=15), "now": now,
        "owner": False, "amount": "9500", "currency": "USD"})
    assert "9,500.00" not in text2, "money detail only in owner email"

    s3, text3, html3 = email_templates.render("deposit_confirmed", {
        "person": "Jordan Mills", "vehicle": "1999 Subaru Sambar · STK-0405", "amount": "1000", "currency": "USD",
        "owner": True, "receipt_ref": "ST-88213", "entity_kind": "vehicle", "entity_id": "v1", "now": now,
        "confirmed_at": now})
    assert s3 == "Deposit paid · Jordan Mills · 1999 Subaru Sambar · STK-0405"
    assert f"{email_templates.origin()}/vehicles/v1?tab=sale" in html3 and "#1E7F4F" in html3

    with pytest.raises(ValueError):
        email_templates.render("not_a_template", {})

    # money is Decimal and quantized for the real currency: yen has no minor units
    assert email_templates.money("900000", "JPY") == "900,000 JPY"
    assert email_templates.money("1234.5", "USD") == "1,234.50 USD"
    assert email_templates.money(None, "USD") == "Not recorded"
    # the subject rule is enforced at runtime, not with an assert (python -O strips asserts)
    original = email_templates.RENDERERS["digest"]
    email_templates.RENDERERS["digest"] = lambda ctx: ("Reminder: something", "t", "<p>h</p>")
    try:
        with pytest.raises(ValueError, match="never 'Reminder:'"):
            email_templates.render("digest", {})
    finally:
        email_templates.RENDERERS["digest"] = original


# ── unknown provider results are never blindly retried ──────────────────────
async def test_an_unknown_email_result_stays_unknown_and_is_not_resent(client, db, owner, monkeypatch):
    from backend.app.adapters import email as email_adapter
    from backend.app.core.errors import ProviderError
    login(client, owner)
    tag = _u()
    t = (await client.post("/api/tasks", json={"title": f"Unknown result {tag}", "type": "call",
                                               "due_at": _iso(timedelta(hours=3)), "reminder_kind": "15m",
                                               "owner_user_id": owner.id,
                                               "dedupe": False})).json()["data"]["task"]
    await run_worker_once()
    row = [x for x in await _deliveries(db, t["id"]) if x.kind == "task_reminder"][0]
    row.deliver_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.commit()

    async def flaky(self, msg):
        raise ProviderError("smtp failure: connection reset after DATA", kind="transient")
    monkeypatch.setattr(email_adapter.MemoryTransport, "send", flaky)
    await run_worker_once()
    await db.refresh(row)
    assert row.state == "unknown" and "unknown" in (row.last_error or "")
    monkeypatch.undo()
    await run_worker_once()
    await db.refresh(row)
    assert row.state == "unknown", "a possibly-accepted send is never blindly resent"
    assert not _mails(tag)
    out = await reminders.reconcile_unknown(_sf())
    assert out["reconciled"] == 0 and "unknown" in out.get("note", "unknown")


async def test_a_delivery_stuck_in_claimed_returns_to_the_queue(client, db, owner):
    login(client, owner)
    tag = _u()
    t = (await client.post("/api/tasks", json={"title": f"Stuck {tag}", "type": "call",
                                               "due_at": _iso(timedelta(hours=3)), "reminder_kind": "15m",
                                               "owner_user_id": owner.id, "dedupe": False})).json()["data"]["task"]
    await run_worker_once()
    row = [x for x in await _deliveries(db, t["id"]) if x.kind == "task_reminder"][0]
    row.state = "claimed"                      # the worker died between claiming and sending
    row.lease_token = "dead-worker"
    row.lease_until = datetime.now(timezone.utc) - timedelta(minutes=5)
    row.deliver_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.commit()
    out = await reminders.repair_missing(_sf())
    assert out["requeued"] >= 1
    await db.refresh(row)
    assert row.state == "scheduled" and row.lease_token is None
    await run_worker_once()
    await db.refresh(row)
    assert row.state == "accepted" and len(_mails(tag)) == 1


# ── snooze actually delivers (C03 / §5.3: snooze delays the notification, not the meeting) ──
async def test_C03_a_snooze_delivers_the_reminder_at_the_snoozed_time(client, db, owner):
    """A snooze is a request to be reminded again. It must fire even when the meeting time itself has
    passed (that is the usual case: the reminder arrives, the meeting slips, the owner snoozes)."""
    from backend.app.models.tasks import Task
    login(client, owner)
    tag = _u()
    t = (await client.post("/api/tasks", json={"title": f"Chase Renee {tag}", "type": "call",
                                               "due_at": _iso(timedelta(hours=-2)), "reminder_kind": "15m",
                                               "owner_user_id": owner.id, "dedupe": False})).json()["data"]["task"]
    await run_worker_once()
    s = (await client.post(f"/api/tasks/{t['id']}/snooze", json={"minutes": 30})).json()["data"]["task"]
    assert s["due_at"] == t["due_at"], "snooze never moves the meeting"
    await run_worker_once()
    row = [x for x in await _deliveries(db, t["id"])
           if x.kind == "task_reminder" and x.task_revision == s["schedule_revision"]]
    assert row, "the snooze must schedule a new reminder"
    row = row[0]
    assert abs((row.deliver_at - datetime.fromisoformat(s["snoozed_until"])).total_seconds()) < 2
    # the snooze window elapses
    task = await db.get(Task, t["id"])
    task.snoozed_until = datetime.now(timezone.utc) - timedelta(seconds=5)
    row.deliver_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    await db.commit()
    await run_worker_once()
    await _fresh(db)
    await db.refresh(row)
    assert row.state == "accepted", f"the snoozed reminder was {row.state}: {row.cancel_reason}"
    mail = _mails(tag)[-1]
    assert "snoozed reminder" in mail["subject"] and "meeting time has not changed" in mail["text"]


async def test_a_snooze_without_a_chosen_offset_still_schedules_the_nudge(client, db, owner):
    login(client, owner)
    tag = _u()
    t = (await client.post("/api/tasks", json={"title": f"No offset {tag}", "due_at": _iso(timedelta(hours=3)),
                                               "owner_user_id": owner.id, "dedupe": False})).json()["data"]["task"]
    await run_worker_once()
    assert not [x for x in await _deliveries(db, t["id"]) if x.kind == "task_reminder"], "no offset, no reminder"
    s = (await client.post(f"/api/tasks/{t['id']}/snooze", json={"minutes": 45})).json()["data"]["task"]
    await run_worker_once()
    rows = [x for x in await _deliveries(db, t["id"])
            if x.kind == "task_reminder" and x.task_revision == s["schedule_revision"]]
    assert rows, "the app promised a nudge in 45 minutes; it has to be scheduled"
    assert abs((rows[0].deliver_at - datetime.fromisoformat(s["snoozed_until"])).total_seconds()) < 2


async def test_C07_snoozing_an_overdue_task_does_not_repeat_the_overdue_email(client, db, owner):
    """§5.4: 'respect ... snooze'. The overdue nag waits until after the snoozed reminder."""
    login(client, owner)
    tag = _u()
    t = (await client.post("/api/tasks", json={"title": f"Snoozed overdue {tag}", "due_at": _iso(timedelta(hours=-3)),
                                               "owner_user_id": owner.id, "dedupe": False})).json()["data"]["task"]
    for _ in range(2):
        await run_worker_once()
    before = len([m for m in _mails(tag) if m["subject"].startswith("Overdue:")])
    assert before == 1
    s = (await client.post(f"/api/tasks/{t['id']}/snooze", json={"minutes": 120})).json()["data"]["task"]
    for _ in range(2):
        await run_worker_once()
    assert len([m for m in _mails(tag) if m["subject"].startswith("Overdue:")]) == before, \
        "snoozing must not fire a second overdue email on the spot"
    new = [x for x in await _deliveries(db, t["id"]) if x.kind == "overdue"
           and x.task_revision == s["schedule_revision"]][0]
    assert new.state == "scheduled" and new.deliver_at > datetime.fromisoformat(s["snoozed_until"])


# ── truthful dates and truthful failures ────────────────────────────────────
async def test_E08_the_deposit_email_dates_the_confirmation_not_the_worker_run(client, db, owner):
    """The worker may run long after the deposit was confirmed; the email must not invent 'now'."""
    from backend.app.services import email_templates as et
    tag = _u()
    happened = (datetime.now(timezone.utc) - timedelta(hours=5)).replace(microsecond=0)
    db.add(Event(type="deposit.confirmed", aggregate_type="invoice", aggregate_id=f"inv-{tag}",
                 payload={"invoice_id": f"inv-{tag}", "payment_id": f"pay-{tag}", "amount": "500.00",
                          "currency": "USD", "handoff_kind": "sale", "provider_receipt_ref": f"sq-{tag}"},
                 happened_at=happened, actor={}))
    await db.commit()
    await run_worker_once()
    await _fresh(db)
    row = (await db.execute(select(ScheduledDelivery).where(
        ScheduledDelivery.dedupe_key == f"deposit:pay-{tag}:{owner.id}:email")
        .execution_options(populate_existing=True))).scalar_one()
    assert datetime.fromisoformat(row.payload["confirmed_at"]) == happened
    await run_worker_once()
    mail = [m for m in MemoryTransport.sent if m["subject"].startswith("Deposit paid") and tag in m["text"]][-1]
    assert et.clock(happened, "America/Phoenix") in mail["text"], "the email states when it was confirmed"


async def test_a_refused_recipient_is_failed_not_unknown(client, db, owner):
    """`unknown` is reserved for a send that may have been accepted. A blocked address never was."""
    login(client, owner)
    tag = _u()
    t = (await client.post("/api/tasks", json={"title": f"Forbidden {tag}", "type": "call",
                                               "due_at": _iso(timedelta(hours=3)), "reminder_kind": "15m",
                                               "owner_user_id": owner.id, "dedupe": False})).json()["data"]["task"]
    await run_worker_once()
    row = [x for x in await _deliveries(db, t["id"]) if x.kind == "task_reminder"][0]
    row.deliver_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    await db.commit()
    old = settings.FORBIDDEN_RECIPIENTS
    settings.FORBIDDEN_RECIPIENTS = f"{old},{reminders.recipient_email(owner)}"
    try:
        await run_worker_once()
    finally:
        settings.FORBIDDEN_RECIPIENTS = old
    await _fresh(db)
    await db.refresh(row)
    assert row.state == "failed" and "refused recipient" in (row.last_error or "")
    assert not _mails(tag)


# ── H11: an incident ends when the source recovers ──────────────────────────
async def test_H11_a_source_that_recovered_and_broke_again_is_a_new_incident(client, db, owner):
    login(client, owner)
    tag = _u()
    provider = f"probe_{tag}"          # a provider Home does not gate on: no effect on other feeds
    conn = Connection(provider=provider, label=f"Probe {tag}", status="connected", environment="test")
    db.add(conn)
    await db.flush()

    async def degrade():
        db.add(Event(type="connection.degraded", aggregate_type="connection", aggregate_id=conn.id,
                     payload={"provider": provider, "kind": "auth_expired", "message": "token expired"},
                     happened_at=datetime.now(timezone.utc), actor={}))
        await db.commit()
        await run_worker_once()
        await _fresh(db)

    async def alerts() -> list[ScheduledDelivery]:
        return list((await db.execute(select(ScheduledDelivery).where(
            ScheduledDelivery.dedupe_key.like(f"connissue:{provider}:auth_expired:%"),
            ScheduledDelivery.fallback_of_id.is_(None)).execution_options(populate_existing=True))).scalars().all())

    try:
        await degrade()
        await degrade()
        note = (await db.execute(select(Notification).where(
            Notification.dedupe_key == f"connection:{provider}:auth_expired:{owner.id}")
            .execution_options(populate_existing=True))).scalar_one()
        assert len(await alerts()) == 1, "one 'needs attention' per incident, not per failed poll"
        assert note.occurrences == 2 and note.payload.get("incident") == 1

        # Dylan acknowledges; while it is still broken he is not told again
        await client.post(f"/api/notifications/{note.id}/acknowledge", json={})
        await degrade()
        assert len(await alerts()) == 1
        await db.refresh(note)
        assert note.state == "acknowledged", "an acknowledged, still-broken source is not re-nagged"

        # he reconnects it, and weeks later it expires again: that is a new incident
        conn.last_success_at = datetime.now(timezone.utc)
        conn.status = "connected"
        await db.commit()
        await degrade()
        await db.refresh(note)
        assert len(await alerts()) == 2 and note.payload.get("incident") == 2
        assert note.state == "unread" and note.occurrences == 1
        feed = (await client.get("/api/notifications")).json()
        assert any(i["group_key"] == f"connection:{provider}" for i in feed["high"]), \
            "the bell shows the new incident again"
    finally:
        for r in (await db.execute(select(ScheduledDelivery).where(
                ScheduledDelivery.dedupe_key.like(f"connissue:{provider}:%")))).scalars().all():
            await db.delete(r)
        for n in (await db.execute(select(Notification).where(
                Notification.dedupe_key.like(f"connection:{provider}:%")))).scalars().all():
            await db.delete(n)
        await db.delete(conn)
        await db.commit()


# ── truthful "late": a task entered after its own deadline was never delayed by anything ──
async def test_an_overdue_notice_for_a_task_entered_late_is_not_labelled_late(client, db, owner):
    """C04's late label means "we owed you this and the worker was down". A task typed in after it was
    already due produces a notice at the first possible moment, so it must not claim an interruption."""
    login(client, owner)
    tag = _u()
    t = (await client.post("/api/tasks", json={"title": f"Typed in after due {tag}", "due_at": _iso(timedelta(hours=-5)),
                                               "owner_user_id": owner.id, "dedupe": False})).json()["data"]["task"]
    for _ in range(2):
        await run_worker_once()
    row = [x for x in await _deliveries(db, t["id"]) if x.kind == "overdue"][0]
    assert row.state == "accepted" and row.payload.get("scheduled_late") is True
    assert row.late is False, "nothing was delayed; the notice went out as soon as the task existed"
    mail = [m for m in _mails(tag) if m["subject"].startswith("Overdue:")][-1]
    assert "(late reminder)" not in mail["subject"].lower()
    assert "this reminder is late" not in mail["text"].lower()
    assert "interruption" not in mail["text"].lower(), "no fabricated cause"


async def test_a_genuinely_late_reminder_states_the_fact_without_inventing_a_cause(client, db, owner):
    login(client, owner)
    tag = _u()
    t = (await client.post("/api/tasks", json={"title": f"Truly late {tag}", "type": "call",
                                               "due_at": _iso(timedelta(hours=2)), "reminder_kind": "1h",
                                               "owner_user_id": owner.id, "dedupe": False})).json()["data"]["task"]
    await run_worker_once()
    row = [x for x in await _deliveries(db, t["id"]) if x.kind == "task_reminder"][0]
    assert row.payload.get("scheduled_late") is False
    row.deliver_at = datetime.now(timezone.utc) - timedelta(minutes=45)
    await db.commit()
    await run_worker_once()
    await _fresh(db)
    await db.refresh(row)
    assert row.late is True
    mail = _mails(tag)[-1]
    assert "late" in mail["text"].lower() and "service interruption" not in mail["text"].lower()


async def test_a_transient_transport_error_is_retried_with_bounded_backoff(client, db, owner, monkeypatch):
    """Nothing was handed to the transport, so this is a retry, not a second copy — three attempts,
    then the delivery stays failed. setup_blocked and invalid_input are never retried."""
    from backend.app.adapters import email as email_adapter
    from backend.app.core.errors import ProviderError, Unsupported

    login(client, owner)
    tag = _u()

    async def _due(title: str) -> ScheduledDelivery:
        t = (await client.post("/api/tasks", json={"title": f"{title} {tag}", "type": "call",
                                                   "due_at": _iso(timedelta(hours=3)), "reminder_kind": "15m",
                                                   "owner_user_id": owner.id, "dedupe": False})).json()["data"]["task"]
        await run_worker_once()
        row = [x for x in await _deliveries(db, t["id"]) if x.kind == "task_reminder"][0]
        row.deliver_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await db.commit()
        return row

    row = await _due("Flaky transport")
    attempts = {"n": 0}

    async def flaky(self, msg):
        attempts["n"] += 1
        raise ProviderError("smtp error 451: b'try later'", kind="transient", retryable=True)

    monkeypatch.setattr(email_adapter.MemoryTransport, "send", flaky)
    await run_worker_once()
    await db.refresh(row)
    assert row.state == "scheduled" and row.attempts == 1, "the first failure reschedules, it does not fail"
    assert "retrying in" in (row.last_error or "") and row.deliver_at > datetime.now(timezone.utc)
    assert not _mails(tag)

    # the backoff elapses and the transport is healthy again: one delivery, never two
    monkeypatch.undo()
    row.deliver_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()
    await run_worker_once()
    await db.refresh(row)
    assert row.state == "accepted" and row.attempts == 2 and attempts["n"] == 1
    assert len(_mails(tag)) == 1

    # a transport that never recovers stops after three attempts
    row2 = await _due("Broken transport")
    monkeypatch.setattr(email_adapter.MemoryTransport, "send", flaky)
    for expected in ("scheduled", "scheduled", "failed"):
        row2.deliver_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        await run_worker_once()
        await db.refresh(row2)
        assert row2.state == expected, f"attempt {row2.attempts} -> {row2.state}"
    assert row2.attempts == 3 and "after 3 attempts" in (row2.last_error or "")
    monkeypatch.undo()

    # setup_blocked is not a transport hiccup: no retry
    row3 = await _due("Setup blocked")

    async def unconfigured(self, msg):
        raise Unsupported("SMTP_HOST not configured")

    monkeypatch.setattr(email_adapter.MemoryTransport, "send", unconfigured)
    await run_worker_once()
    await db.refresh(row3)
    assert row3.state == "failed" and "setup_blocked" in (row3.last_error or "") and row3.attempts == 1
    monkeypatch.undo()

    # invalid input is not retried either
    row4 = await _due("Invalid input")

    async def rejected(self, msg):
        raise ProviderError("smtp error 550: b'no such mailbox'", kind="invalid_input")

    monkeypatch.setattr(email_adapter.MemoryTransport, "send", rejected)
    await run_worker_once()
    await db.refresh(row4)
    assert row4.state == "failed" and row4.attempts == 1 and "550" in (row4.last_error or "")
