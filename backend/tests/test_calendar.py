"""Calendar sync: tasks that are appointments become entries on the business calendar (spec §5.3, §11.2).

Dylan's decision, verbatim: *"create events from tasks on calendar — but only like meetings, call
people back, schedules, etc."*

What these scenarios pin down:

* only `call`, `meeting` and a `follow_up` with a real scheduled block become entries; an operational
  to-do and an appointment with no time never do;
* a reschedule patches the entry that already exists — it never leaves two of them behind;
* completing or cancelling the task takes the entry off the calendar;
* a replayed `task.changed` reaches the provider zero times;
* with the connection or the owner capability missing the feature says `setup_blocked` and writes
  nothing — never a silent no-op;
* Phoenix, a DST region and Tokyo all land on the right UTC instant with the right IANA zone;
* a provider failure is retried and a lost write result is reconciled, so neither ever duplicates;
* an overlapping entry is reported on the task and to the owner, and the appointment is still made;
* AZKT keeps the reminder and never invites a customer;
* outside production a real write cannot reach a real calendar (H08).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select, update

from backend.app.adapters import calendar as cal
from backend.app.core.config import settings
from backend.app.core.errors import Blocked, Denied, ProviderError, Unsupported
from backend.app.domain.commands import dispatch
from backend.app.models.comms import Connection
from backend.app.models.legacy import Setting
from backend.app.models.notify import Notification
from backend.app.models.runtime import Event, Job
from backend.app.models.tasks import Task
from backend.app.services import calendar_sync as svc
from backend.app.services import connections as conn_svc
from backend.tests.conftest import ctx_for, login, make_user
from backend.tests.fixtures_providers import drain_events, run_jobs

CAL_ID = "info@azkeitrucks.com"
BOTH_SCOPES = [cal.READ_SCOPE, cal.WRITE_SCOPE]


def _u() -> str:
    return uuid.uuid4().hex[:8]


def now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(delta: timedelta) -> str:
    return (now() + delta).replace(microsecond=0).isoformat()


# ── fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture
def fake_calendar():
    """The in-memory calendar every scenario runs against. No test ever reaches Google."""
    fake = cal.FakeCalendar(calendar_id=CAL_ID)
    cal.set_adapter(fake)
    yield fake
    cal.set_adapter(None)


@pytest.fixture(autouse=True)
async def calendar_reset(db):
    """Both halves of the setup are global rows, so every scenario starts and ends from "off"."""
    await _reset(db)
    yield
    await _reset(db)


async def _reset(db) -> None:
    await db.execute(delete(Setting).where(Setting.key == svc.SETTING_KEY))
    await db.execute(update(Connection).where(Connection.provider == svc.PROVIDER)
                     .values(status="disconnected", granted_scopes=[], scopes=[]))
    await db.commit()


async def connect_calendar(db, *, scopes: list[str] | None = None, status: str = "connected") -> Connection:
    conn = await conn_svc.get(db, svc.PROVIDER, create=True)
    conn.granted_scopes = list(BOTH_SCOPES if scopes is None else scopes)
    conn.scopes = list(conn.granted_scopes)
    conn.status = status
    conn.account_identity = CAL_ID
    conn.connected_at = conn.connected_at or now()
    await db.commit()
    await db.refresh(conn)
    return conn


async def enable_writes(db, owner, **patch) -> dict:
    res = await dispatch(ctx_for(db, owner), "calendar.set_writes",
                         {"enabled": True, "calendar_id": CAL_ID, **patch})
    assert res.status == "ok", res.decision
    return res.data


async def setup_calendar(db, owner, *, scopes: list[str] | None = None) -> Connection:
    conn = await connect_calendar(db, scopes=scopes)
    await enable_writes(db, owner)
    return conn


async def sync(pull_forward: bool = True) -> int:
    """Drain the outbox (which queues the work) and then run the durable jobs that do it."""
    await drain_events()
    if pull_forward:
        await _pull_retries_forward()
    return await run_jobs()


async def _pull_retries_forward() -> None:
    """A retry is scheduled with backoff; tests exercise the retry, not the clock."""
    from backend.app import db as dbmod
    async with dbmod.SessionLocal() as s:
        await s.execute(update(Job).where(Job.kind == "calendar.sync", Job.state == "queued")
                        .values(run_at=now() - timedelta(seconds=1)))
        await s.commit()


async def make_task(client, owner, **body) -> dict:
    payload = {"title": f"Call Tomás {_u()}", "type": "call", "owner_user_id": owner.id, "dedupe": False,
               "due_at": _iso(timedelta(hours=3))}
    payload.update(body)
    r = await client.post("/api/tasks", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["data"]["task"]


async def reload_task(db, task_id: str) -> Task:
    return (await db.execute(select(Task).where(Task.id == task_id)
                             .execution_options(populate_existing=True))).scalar_one()


def raw_event(fake: cal.FakeCalendar, event_id: str) -> dict:
    return fake._cal(CAL_ID)[event_id]


# ── which tasks are appointments ────────────────────────────────────────────
async def test_only_appointment_task_types_become_calendar_events(client, db, owner, fake_calendar):
    """"only like meetings, call people back, schedules" — encoded, not interpreted per call."""
    login(client, owner)
    await setup_calendar(db, owner)

    call = await make_task(client, owner, title=f"Call Tomás back {_u()}", type="call")
    meeting = await make_task(client, owner, title=f"Meet the shipper {_u()}", type="meeting")
    chore = await make_task(client, owner, title=f"Reorder shop rags {_u()}", type="operational")
    loose_followup = await make_task(client, owner, title=f"Chase the title {_u()}", type="follow_up")
    blocked_followup = await make_task(
        client, owner, title=f"Port paperwork block {_u()}", type="follow_up",
        start_at=_iso(timedelta(days=1)), end_at=_iso(timedelta(days=1, hours=1)))
    untimed = await make_task(client, owner, title=f"Call whenever {_u()}", type="call", due_at=None)
    await sync()

    on_calendar, off_calendar = [], []
    for t in (call, meeting, blocked_followup):
        row = await reload_task(db, t["id"])
        on_calendar.append((row.title, row.calendar_state, row.calendar_event_id))
        assert row.calendar_state == "synced", row.calendar_error
        assert row.calendar_event_id == cal.event_id_for(row.id)
        assert row.calendar_synced_revision == row.schedule_revision
    for t in (chore, loose_followup, untimed):
        row = await reload_task(db, t["id"])
        off_calendar.append(row.title)
        assert row.calendar_event_id is None
        assert row.calendar_state in (None, "skipped")

    assert len(on_calendar) == 3 and len(off_calendar) == 3
    assert fake_calendar.live_count() == 3
    assert len(fake_calendar.inserted) == 3

    # the refusal is explained in words a person can read, not left blank
    assert "to-do" in svc.eligibility(await reload_task(db, chore["id"]))["reason"]
    assert "scheduled start and end" in svc.eligibility(await reload_task(db, loose_followup["id"]))["reason"]
    assert "no scheduled time" in svc.eligibility(await reload_task(db, untimed["id"]))["reason"]

    # an operational task never even reaches the queue
    from backend.app import db as dbmod
    async with dbmod.SessionLocal() as s:
        queued = (await s.execute(select(Job.payload).where(Job.kind == "calendar.sync"))).scalars().all()
    assert chore["id"] not in [p.get("task_id") for p in queued]


async def test_a_suggested_task_is_not_a_commitment_yet(client, db, owner, fake_calendar):
    login(client, owner)
    await setup_calendar(db, owner)
    t = await make_task(client, owner, title=f"Maybe call the broker {_u()}", type="call", is_suggestion=True)
    await sync()
    row = await reload_task(db, t["id"])
    assert row.calendar_event_id is None and fake_calendar.live_count() == 0
    assert "not a commitment yet" in svc.eligibility(row)["reason"]


# ── reschedule, edit, cancel ────────────────────────────────────────────────
async def test_a_reschedule_patches_the_same_event_instead_of_making_a_second(client, db, owner, fake_calendar):
    login(client, owner)
    await setup_calendar(db, owner)
    t = await make_task(client, owner, title=f"Call the port {_u()}", type="call")
    await sync()
    row = await reload_task(db, t["id"])
    event_id = row.calendar_event_id
    assert event_id and fake_calendar.live_count() == 1

    moved = _iso(timedelta(days=2))
    r = await client.post(f"/api/tasks/{t['id']}/reschedule", json={"due_at": moved})
    assert r.status_code == 200, r.text
    await sync()

    row = await reload_task(db, t["id"])
    assert row.calendar_event_id == event_id, "the same entry is moved, never a second one"
    assert row.calendar_synced_revision == row.schedule_revision == 2
    assert fake_calendar.live_count() == 1
    assert len(fake_calendar.inserted) == 1 and fake_calendar.patched == [event_id]
    assert cal.parse_time(raw_event(fake_calendar, event_id)["start"]).isoformat() == \
        r.json()["data"]["task"]["due_at"]


async def test_a_title_change_patches_and_an_unrelated_edit_writes_nothing(client, db, owner, fake_calendar):
    login(client, owner)
    await setup_calendar(db, owner)
    t = await make_task(client, owner, title=f"Call Ana {_u()}", type="call")
    await sync()
    event_id = (await reload_task(db, t["id"])).calendar_event_id

    new_title = f"Call Ana about the Hijet {_u()}"
    r = await client.post(f"/api/tasks/{t['id']}/update", json={"title": new_title})
    assert r.status_code == 200, r.text
    await sync()
    assert fake_calendar.patched == [event_id]
    assert raw_event(fake_calendar, event_id)["summary"] == new_title

    # a change that a calendar entry does not show (priority) must not produce a calendar write
    r = await client.post(f"/api/tasks/{t['id']}/update", json={"priority": "high"})
    assert r.status_code == 200, r.text
    await sync()
    assert fake_calendar.patched == [event_id], "an edit the calendar cannot show is not a calendar write"


async def test_reassigning_the_task_patches_the_entry_with_the_new_owner(client, db, owner, fake_calendar):
    """A calendar entry says whose appointment it is, so an owner change is a calendar change."""
    login(client, owner)
    await setup_calendar(db, owner)
    luis = await make_user(db, f"luis{_u()}", "manager", display_name="Luis")
    t = await make_task(client, owner, title=f"Meet the broker {_u()}", type="meeting")
    await sync()
    event_id = (await reload_task(db, t["id"])).calendar_event_id
    assert "Dylan" in raw_event(fake_calendar, event_id)["description"]

    r = await client.post(f"/api/tasks/{t['id']}/assign", json={"owner_user_id": luis.id})
    assert r.status_code == 200, r.text
    await sync()
    assert fake_calendar.patched == [event_id] and len(fake_calendar.inserted) == 1
    assert "Luis" in raw_event(fake_calendar, event_id)["description"]


@pytest.mark.parametrize("closer,expected", [("complete", "completed"), ("cancel", "cancelled")])
async def test_completing_or_cancelling_a_task_takes_the_entry_off_the_calendar(
        client, db, owner, fake_calendar, closer, expected):
    login(client, owner)
    await setup_calendar(db, owner)
    t = await make_task(client, owner, title=f"Meet the hauler {_u()}", type="meeting")
    await sync()
    event_id = (await reload_task(db, t["id"])).calendar_event_id
    assert fake_calendar.live_count() == 1

    r = await client.post(f"/api/tasks/{t['id']}/{closer}", json={"reason": "no longer needed"})
    assert r.status_code == 200 and r.json()["data"]["task"]["status"] == expected
    await sync()

    row = await reload_task(db, t["id"])
    assert row.calendar_state == "cancelled" and row.calendar_event_id is None
    assert fake_calendar.deleted == [event_id] and fake_calendar.live_count() == 0

    # and a replay of the close does not try to delete it a second time
    await sync()
    assert fake_calendar.deleted == [event_id]


# ── idempotency ─────────────────────────────────────────────────────────────
async def test_a_replayed_task_changed_event_never_creates_a_second_entry(client, db, owner, fake_calendar):
    """The outbox can deliver the same event again after a crash; the provider is not called twice."""
    login(client, owner)
    await setup_calendar(db, owner)
    t = await make_task(client, owner, title=f"Call the broker {_u()}", type="call")
    await sync()
    event_id = (await reload_task(db, t["id"])).calendar_event_id
    assert len(fake_calendar.inserted) == 1
    before = list(fake_calendar.calls)

    # replay the task.changed events for this task, exactly as a redelivery would
    from backend.app import db as dbmod
    async with dbmod.SessionLocal() as s:
        await s.execute(update(Event).where(Event.type == "task.changed",
                                            Event.aggregate_id == t["id"])
                        .values(processed_at=None, next_attempt_at=None, attempts=0))
        await s.commit()
    await sync()
    await sync()

    row = await reload_task(db, t["id"])
    assert row.calendar_event_id == event_id
    assert fake_calendar.live_count() == 1
    assert len(fake_calendar.inserted) == 1 and fake_calendar.patched == []
    assert fake_calendar.calls == before, "a replay that changes nothing does not touch the provider at all"


async def test_bookkeeping_lost_after_a_write_is_adopted_not_duplicated(client, db, owner, fake_calendar):
    """The entry exists, AZKT's record of it does not (restored backup). The deterministic id makes
    the provider itself refuse the second insert, and AZKT adopts what is already there."""
    login(client, owner)
    await setup_calendar(db, owner)
    t = await make_task(client, owner, title=f"Meet the inspector {_u()}", type="meeting")
    await sync()
    event_id = (await reload_task(db, t["id"])).calendar_event_id

    from backend.app import db as dbmod
    async with dbmod.SessionLocal() as s:
        await s.execute(update(Task).where(Task.id == t["id"]).values(
            calendar_event_id=None, calendar_state=None, calendar_synced_revision=None, calendar_synced_hash=None))
        await s.commit()
    row = await reload_task(db, t["id"])
    res = await svc.sync_task(db, row)
    await db.commit()

    assert res["action"] == "adopt" and res["state"] == "synced"
    assert (await reload_task(db, t["id"])).calendar_event_id == event_id
    assert fake_calendar.live_count() == 1 and len(fake_calendar.inserted) == 1


# ── setup gates: nothing is written without both permissions ────────────────
async def test_without_a_connection_the_write_is_setup_blocked_and_nothing_is_written(
        client, db, owner, fake_calendar):
    login(client, owner)
    t = await make_task(client, owner, title=f"Call with no calendar {_u()}", type="call")
    await sync()

    row = await reload_task(db, t["id"])
    assert row.calendar_state == "setup_blocked" and row.calendar_event_id is None
    assert "not connected" in (row.calendar_error or "")
    assert fake_calendar.live_count() == 0 and fake_calendar.calls == []

    with pytest.raises(Unsupported) as exc:
        cal.adapter_for(db, None, writes_enabled=True)
    assert exc.value.detail.get("setup_blocked") == "calendar.oauth"


async def test_without_the_owner_capability_the_write_is_setup_blocked_and_nothing_is_written(
        client, db, owner, fake_calendar):
    """A connected Google account is not permission to write: §11.2 makes that a separate switch."""
    login(client, owner)
    await connect_calendar(db)                      # connected, but the owner never turned writes on
    t = await make_task(client, owner, title=f"Call with writes off {_u()}", type="call")
    await sync()

    row = await reload_task(db, t["id"])
    assert row.calendar_state == "setup_blocked" and row.calendar_event_id is None
    assert "calendar writes are off" in (row.calendar_error or "")
    assert fake_calendar.live_count() == 0 and fake_calendar.calls == []

    conn = await conn_svc.get(db, svc.PROVIDER)
    with pytest.raises(Unsupported) as exc:
        cal.adapter_for(db, conn, writes_enabled=False)
    assert exc.value.detail.get("setup_blocked") == "calendar.capability"

    # the same connection reads fine — only the write is held
    assert cal.adapter_for(db, conn, writes_enabled=False, write=False) is fake_calendar


async def test_a_read_only_grant_is_setup_blocked_for_writes(client, db, owner, fake_calendar):
    """Google was only asked for read access, so nothing AZKT does locally can make a write legal."""
    login(client, owner)
    conn = await connect_calendar(db, scopes=[cal.READ_SCOPE])
    with pytest.raises(Blocked) as blocked:
        await dispatch(ctx_for(db, owner), "calendar.set_writes", {"enabled": True, "calendar_id": CAL_ID})
    assert blocked.value.detail.get("setup_blocked") == "calendar.write_scope"

    # and even with the switch forced on, the adapter still refuses
    with pytest.raises(Unsupported) as exc:
        cal.adapter_for(db, conn, writes_enabled=True)
    assert exc.value.detail.get("setup_blocked") == "calendar.write_scope"
    assert cal.adapter_for(db, conn, writes_enabled=False, write=False) is fake_calendar


async def test_turning_writes_on_is_owner_only_and_needs_something_to_write_to(client, db, owner, fake_calendar):
    manager = await make_user(db, f"mgr{_u()}", "manager")
    with pytest.raises(Denied) as exc:
        await dispatch(ctx_for(db, manager), "calendar.set_writes", {"enabled": True})
    assert "settings" in str(exc.value)          # a manager has no business settings permission at all

    # the owner cannot switch on a capability with no connected calendar behind it
    with pytest.raises(Blocked) as blocked:
        await dispatch(ctx_for(db, owner), "calendar.set_writes", {"enabled": True})
    assert blocked.value.detail.get("setup_blocked") == "calendar.oauth"
    assert (await svc.config(db))["writes_enabled"] is False

    await connect_calendar(db)
    data = await enable_writes(db, owner)
    assert data["writes_enabled"] is True
    assert (await svc.config(db))["calendar_id"] == CAL_ID

    off = await dispatch(ctx_for(db, owner), "calendar.set_writes", {"enabled": False})
    assert off.status == "ok" and (await svc.config(db))["writes_enabled"] is False


# ── time zones ──────────────────────────────────────────────────────────────
async def test_C05_dst_region_and_tokyo_are_written_as_the_right_instant_in_the_right_zone(
        client, db, owner, fake_calendar):
    """The offset in `dateTime` fixes the instant; `timeZone` is how the person reads it. A spring
    forward cannot move the appointment, and Tokyo is not rendered as Phoenix."""
    login(client, owner)
    await setup_calendar(db, owner)

    # America/New_York springs forward 2027-03-14 02:00 local: 01:30 EST = 06:30Z, 03:30 EDT = 07:30Z.
    ny = await make_task(client, owner, title=f"NY call {_u()}", type="call",
                         due_at="2027-03-14T01:30:00", timezone="America/New_York")
    assert ny["due_at"] == "2027-03-14T06:30:00+00:00"
    tokyo = await make_task(client, owner, title=f"Auction block {_u()}", type="meeting",
                            due_at="2027-04-01T10:00:00+09:00", timezone="Asia/Tokyo")
    assert tokyo["due_at"] == "2027-04-01T01:00:00+00:00"
    phoenix = await make_task(client, owner, title=f"Shop walkthrough {_u()}", type="meeting",
                              due_at="2027-03-14T09:00:00", timezone="America/Phoenix")
    await sync()

    ny_ev = raw_event(fake_calendar, cal.event_id_for(ny["id"]))
    assert ny_ev["start"] == {"dateTime": "2027-03-14T06:30:00Z", "timeZone": "America/New_York"}
    assert ny_ev["end"] == {"dateTime": "2027-03-14T07:00:00Z", "timeZone": "America/New_York"}  # 30-min call
    assert cal.parse_time(ny_ev["start"]).astimezone(ZoneInfo("America/New_York")).strftime("%H:%M %Z") == "01:30 EST"

    # rescheduling across the DST boundary keeps local 03:30 and moves the instant by one hour only
    r = await client.post(f"/api/tasks/{ny['id']}/reschedule", json={"due_at": "2027-03-14T03:30:00"})
    assert r.json()["data"]["task"]["due_at"] == "2027-03-14T07:30:00+00:00"
    await sync()
    ny_ev = raw_event(fake_calendar, cal.event_id_for(ny["id"]))
    assert ny_ev["start"]["dateTime"] == "2027-03-14T07:30:00Z"
    assert cal.parse_time(ny_ev["start"]).astimezone(ZoneInfo("America/New_York")).strftime("%H:%M %Z") == "03:30 EDT"

    jp_ev = raw_event(fake_calendar, cal.event_id_for(tokyo["id"]))
    assert jp_ev["start"] == {"dateTime": "2027-04-01T01:00:00Z", "timeZone": "Asia/Tokyo"}
    assert jp_ev["end"]["dateTime"] == "2027-04-01T02:00:00Z"                      # 60-min meeting
    assert cal.parse_time(jp_ev["start"]).astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M") == \
        "2027-04-01 10:00"
    # the same instant read in Phoenix is the previous evening — and the entry says so
    assert "Wed Mar 31 · 18:00" in jp_ev["description"] and "Asia/Tokyo" in jp_ev["description"]

    az_ev = raw_event(fake_calendar, cal.event_id_for(phoenix["id"]))
    assert az_ev["start"] == {"dateTime": "2027-03-14T16:00:00Z", "timeZone": "America/Phoenix"}  # no DST in AZ


# ── failure handling: retried, reconciled, never duplicated ─────────────────
async def test_a_provider_failure_is_retried_and_never_duplicates(client, db, owner, fake_calendar):
    login(client, owner)
    await setup_calendar(db, owner)
    fake_calendar.fail_next_write = ProviderError("calendar is rate limiting", kind="rate_limited")
    t = await make_task(client, owner, title=f"Call the customs broker {_u()}", type="call")

    await sync()
    row = await reload_task(db, t["id"])
    assert row.calendar_state == "failed" and row.calendar_event_id is None
    assert "rate limiting" in (row.calendar_error or "")
    assert fake_calendar.live_count() == 0, "a refusal the provider actually returned wrote nothing"

    await sync()                                   # the durable queue retries it
    row = await reload_task(db, t["id"])
    assert row.calendar_state == "synced" and row.calendar_error is None
    assert fake_calendar.live_count() == 1 and len(fake_calendar.inserted) == 1

    await sync()
    assert len(fake_calendar.inserted) == 1, "the retry is not repeated once it worked"


async def test_a_lost_write_result_is_reconciled_rather_than_repeated(client, db, owner, fake_calendar):
    """The calendar stored it and the answer never came back (spec §11.5.4): unknown, then reconciled
    by looking the deterministic event id up — never a blind second insert."""
    login(client, owner)
    await setup_calendar(db, owner)
    fake_calendar.lose_next_write_result = True
    t = await make_task(client, owner, title=f"Meet the shipping agent {_u()}", type="meeting")

    await sync()
    row = await reload_task(db, t["id"])
    assert row.calendar_state == "unknown"
    assert row.calendar_event_id == cal.event_id_for(t["id"])
    assert row.calendar_synced_revision is None, "an unknown result is never recorded as synced"
    assert fake_calendar.live_count() == 1 and len(fake_calendar.inserted) == 1

    await sync()
    row = await reload_task(db, t["id"])
    assert row.calendar_state == "synced" and row.calendar_synced_revision == row.schedule_revision
    assert fake_calendar.live_count() == 1
    assert len(fake_calendar.inserted) == 1, "reconciliation adopted the entry instead of making another"
    assert ("get_event", CAL_ID) in fake_calendar.calls, "the retry looked it up before writing"


async def test_an_entry_deleted_in_google_is_recreated_on_the_next_change(client, db, owner, fake_calendar):
    login(client, owner)
    await setup_calendar(db, owner)
    t = await make_task(client, owner, title=f"Call the yard {_u()}", type="call")
    await sync()
    event_id = (await reload_task(db, t["id"])).calendar_event_id
    fake_calendar._cal(CAL_ID).pop(event_id)                    # somebody deleted it in Google

    await client.post(f"/api/tasks/{t['id']}/reschedule", json={"due_at": _iso(timedelta(days=3))})
    await sync()
    row = await reload_task(db, t["id"])
    assert row.calendar_state == "synced" and row.calendar_event_id == event_id
    assert fake_calendar.live_count() == 1 and len(fake_calendar.inserted) == 2


# ── conflicts are reported, not refused ─────────────────────────────────────
async def test_an_overlapping_entry_is_recorded_and_raised_not_used_to_refuse(client, db, owner, fake_calendar):
    login(client, owner)
    await setup_calendar(db, owner)
    start = (now() + timedelta(days=1)).replace(microsecond=0)
    fake_calendar.add_event(summary="Dentist", start=start - timedelta(minutes=15),
                            end=start + timedelta(minutes=15))
    fake_calendar.add_event(summary="Free block", start=start, end=start + timedelta(minutes=30),
                            transparency="transparent")

    t = await make_task(client, owner, title=f"Call the exporter {_u()}", type="call",
                        due_at=start.isoformat())
    await sync()

    row = await reload_task(db, t["id"])
    assert row.calendar_state == "synced", "a clash never refuses the appointment the owner asked for"
    assert fake_calendar.live_count() == 3
    conflicts = list(row.calendar_conflicts or [])
    assert [c["summary"] for c in conflicts] == ["Dentist"], "an entry marked free is not a clash"

    note = (await db.execute(select(Notification).where(Notification.kind == "calendar_conflict",
                                                        Notification.entity_id == t["id"]))).scalars().first()
    assert note is not None and "Dentist" in note.body and note.user_id == owner.id
    assert note.deep_link.endswith(f"/tasks/{t['id']}")


async def test_conflict_checking_can_be_turned_off(client, db, owner, fake_calendar):
    login(client, owner)
    await connect_calendar(db)
    await enable_writes(db, owner, conflict_check=False)
    start = (now() + timedelta(days=1)).replace(microsecond=0)
    fake_calendar.add_event(summary="Dentist", start=start, end=start + timedelta(hours=1))
    t = await make_task(client, owner, title=f"Call while busy {_u()}", type="call", due_at=start.isoformat())
    await sync()
    row = await reload_task(db, t["id"])
    assert row.calendar_state == "synced" and not (row.calendar_conflicts or [])
    assert not any(op == "list_events" for op, _ in fake_calendar.calls)


# ── what AZKT deliberately never puts on an event ───────────────────────────
async def test_azkt_keeps_the_reminder_and_never_invites_anyone(client, db, owner, fake_calendar):
    login(client, owner)
    await setup_calendar(db, owner)
    t = await make_task(client, owner, title=f"Call Ana back {_u()}", type="call",
                        notes="She asked about the 4WD Hijet.", reminder_kind="15m")
    await sync()
    ev = raw_event(fake_calendar, cal.event_id_for(t["id"]))

    assert ev["reminders"] == {"useDefault": False, "overrides": []}, \
        "AZKT already sends the reminder; Google must not send a second one"
    assert "attendees" not in ev, "inviting anybody is a customer send and needs its own approval"
    assert f"/tasks/{t['id']}" in ev["description"] and settings.PUBLIC_ORIGIN in ev["description"]
    assert ev["source"]["url"].endswith(f"/tasks/{t['id']}")
    assert ev["extendedProperties"]["private"]["azkt_task_id"] == t["id"]
    assert "She asked about the 4WD Hijet." in ev["description"]

    with pytest.raises(Unsupported):
        await fake_calendar.invite(CAL_ID, ev["id"], ["customer@example.com"])
    with pytest.raises(Unsupported):
        await fake_calendar.share_calendar(CAL_ID, "someone@example.com")


# ── H08: a real write cannot reach a real calendar outside production ───────
async def test_H08_a_non_production_write_cannot_reach_a_production_calendar(db):
    conn = await connect_calendar(db)
    live = cal.GoogleCalendarAdapter(db, conn, writes_enabled=True)
    old = settings.NON_PROD_DESTINATION_ALLOWLIST
    settings.NON_PROD_DESTINATION_ALLOWLIST = ""
    try:
        for call_it in (
            lambda: live.insert_event(CAL_ID, {"summary": "x"}, event_id="taskabc12"),
            lambda: live.patch_event(CAL_ID, "taskabc12", {"summary": "x"}),
            lambda: live.delete_event(CAL_ID, "taskabc12"),
        ):
            with pytest.raises(Blocked) as exc:
                await call_it()
            assert "destination_blocked" == exc.value.detail.get("status")
        settings.NON_PROD_DESTINATION_ALLOWLIST = "staging-calendar@azkeitrucks.example"
        with pytest.raises(Blocked):
            await live.insert_event(CAL_ID, {"summary": "x"}, event_id="taskabc12")
        # an allowlisted calendar passes the guard (and only then would it reach the network)
        settings.NON_PROD_DESTINATION_ALLOWLIST = CAL_ID
        live._assert_writable("insert_event", CAL_ID)
    finally:
        settings.NON_PROD_DESTINATION_ALLOWLIST = old


async def test_a_blocked_destination_is_recorded_on_the_task_and_not_retried_for_ever(client, db, owner,
                                                                                        fake_calendar):
    """If the H08 guard refuses the destination, retrying cannot change an allowlist — so it is
    reported like any other setup problem, not looped."""
    login(client, owner)
    await setup_calendar(db, owner)
    fake_calendar.fail_next_write = Blocked("staging must not reach a production calendar destination",
                                            status="destination_blocked")
    t = await make_task(client, owner, title=f"Call from staging {_u()}", type="call")
    await sync()
    row = await reload_task(db, t["id"])
    assert row.calendar_state == "setup_blocked" and row.calendar_event_id is None
    assert "production calendar" in (row.calendar_error or "")
    assert fake_calendar.live_count() == 0


async def test_a_live_client_is_never_constructed_in_tests(db):
    cal.set_adapter(None)
    conn = await connect_calendar(db)
    with pytest.raises(Unsupported, match="disabled in tests"):
        cal.adapter_for(db, conn, writes_enabled=True)


# ── the read API ────────────────────────────────────────────────────────────
async def test_the_read_api_reports_the_schedule_and_the_setup_state(client, db, owner, fake_calendar):
    login(client, owner)
    r = await client.get("/api/calendar/status")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is False and body["writes_enabled"] is False
    assert body["write_blocked"]["setup_blocked"] == "calendar.oauth"
    assert body["rules"] == {"always": ["call", "meeting"], "with_a_block": ["follow_up"],
                             "never": ["operational"]}

    await setup_calendar(db, owner)
    tz = "America/Phoenix"
    start = (now() + timedelta(days=1)).astimezone(ZoneInfo(tz)).replace(hour=10, minute=0, second=0, microsecond=0)
    t = await make_task(client, owner, title=f"Call the exporter {_u()}", type="call",
                        due_at=start.astimezone(timezone.utc).isoformat(), timezone=tz)
    await make_task(client, owner, title=f"Sweep the bay {_u()}", type="operational",
                    due_at=start.astimezone(timezone.utc).isoformat())
    await sync()

    r = await client.get("/api/calendar/today", params={"day": start.date().isoformat(), "tz": tz})
    assert r.status_code == 200
    day = r.json()
    ids = {row["task_id"]: row for row in day["tasks"]}
    assert t["id"] in ids and ids[t["id"]]["calendar_state"] == "synced"
    assert ids[t["id"]]["start_local"].endswith("AZ") and ids[t["id"]]["eligible"] is True
    assert day["calendar"]["available"] is True
    assert cal.event_id_for(t["id"]) in {e["id"] for e in day["calendar"]["events"]}

    r = await client.get("/api/calendar/status")
    st = r.json()
    assert st["connected"] is True and st["writes_enabled"] is True and st["write_blocked"] is None
    assert st["counts"].get("synced", 0) >= 1

    r = await client.get(f"/api/calendar/tasks/{t['id']}")
    assert r.status_code == 200 and r.json()["calendar_event_id"] == cal.event_id_for(t["id"])


async def test_the_status_endpoint_is_owner_only_and_the_day_needs_tasks_read(client, db, owner, fake_calendar):
    mech = await make_user(db, f"mech{_u()}", "mechanic")
    login(client, mech)
    assert (await client.get("/api/calendar/status")).status_code == 403
    r = await client.get("/api/calendar/today")
    assert r.status_code == 200
    # a mechanic never sees the business calendar itself, only their own appointments
    assert r.json()["calendar"]["available"] is False
    assert (await client.post("/api/calendar/writes", json={"enabled": True})).status_code == 403
    assert (await client.post("/api/calendar/resync", json={})).status_code == 403


async def test_resync_queues_work_and_reports_setup_blocked_when_it_cannot(client, db, owner, fake_calendar):
    login(client, owner)
    r = await client.post("/api/calendar/resync", json={})
    assert r.status_code == 409 and r.json()["setup_blocked"] == "calendar.oauth"

    await setup_calendar(db, owner)
    t = await make_task(client, owner, title=f"Call the bank {_u()}", type="call")
    await drain_events()
    await run_jobs()
    r = await client.post("/api/calendar/resync", json={"task_id": t["id"]})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.json()["data"]["queued"] in (0, 1)

    r = await client.post("/api/calendar/resync", json={})
    assert r.json()["data"]["considered"] >= 1


# ── the repair sweep ────────────────────────────────────────────────────────
async def test_the_repair_sweep_picks_up_an_appointment_whose_job_was_lost(client, db, owner, fake_calendar):
    """A lost enqueue, a crashed worker or a restored backup must not leave an appointment behind."""
    login(client, owner)
    await setup_calendar(db, owner)
    t = await make_task(client, owner, title=f"Meet the surveyor {_u()}", type="meeting")
    from backend.app import db as dbmod
    await drain_events()
    # the job the outbox queued never runs: exactly what a crashed worker leaves behind
    async with dbmod.SessionLocal() as s:
        await s.execute(update(Job).where(Job.kind == "calendar.sync", Job.state == "queued")
                        .values(state="cancelled"))
        await s.commit()
    await run_jobs()
    assert fake_calendar.live_count() == 0

    out = await svc.repair_sweep(dbmod.SessionLocal)
    assert out["queued"] >= 1 and out["considered"] >= 1
    await run_jobs()
    assert (await reload_task(db, t["id"])).calendar_state == "synced"
    assert fake_calendar.inserted.count(cal.event_id_for(t["id"])) == 1

    # with the calendar disconnected the sweep is honest about why it did nothing
    await _reset(db)
    out = await svc.repair_sweep(dbmod.SessionLocal)
    assert out["setup_blocked"] == "calendar.oauth"


async def test_the_repair_sweep_is_registered_every_five_minutes():
    from backend.app.domain.jobs import SWEEPS
    assert SWEEPS["calendar.repair"][1] == svc.REPAIR_SECONDS == 300
