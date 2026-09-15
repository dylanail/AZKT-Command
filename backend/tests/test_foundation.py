"""Foundation smoke tests: command dispatch, policy outcomes, idempotency, approvals, jobs, events."""
from __future__ import annotations

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from backend.app.core.errors import Denied
from backend.app.domain import jobs
from backend.app.domain.commands import REGISTRY, command, dispatch
from backend.app.models.runtime import ActivityEntry, Approval, Event, ExternalAction, Job
from backend.app.services import approvals as approvals_svc
from backend.tests.conftest import ctx_for, make_user, run_worker_once


class NoteIn(BaseModel):
    text: str
    vehicle_id: str | None = None


class SendIn(BaseModel):
    to: str
    body: str


if "test.note" not in REGISTRY:
    @command("test.note", input=NoteIn, perm="tasks.write", action_class="internal",
             records=lambda p: [("vehicle", p.vehicle_id)] if p.vehicle_id else [])
    async def _note(ctx, inp):
        ctx.record(f"note: {inp.text}", entity_kind="vehicle", entity_id=inp.vehicle_id, kind="task")
        ctx.emit("test.noted", aggregate_type="vehicle", aggregate_id=inp.vehicle_id, payload={"text": inp.text})
        return {"echo": inp.text}

    @command("test.send", input=SendIn, perm="inbox.send", action_class="consequential", approval_kind="send_message",
             summary=lambda p: f"Send to {p.to}", limits=lambda p: {"recipients": [p.to]},
             consequence=lambda p: {"targets": {"recipients": [p.to]}})
    async def _send(ctx, inp):
        act = await approvals_svc.intend_external_action(ctx, command_name="test.send", payload=inp.model_dump(),
                                                         dedupe_key=f"test.send:{inp.to}:{inp.body}", provider="test")
        return {"external_action_id": act.id}

    @approvals_svc.executor("test.send")
    async def _exec(db, act):
        return {"provider_ref": "msg-1", "sent": True}

    @command("test.handoff", input=SendIn, perm="inbox.send", action_class="consequential", approval_kind="send_message",
             summary=lambda p: f"Hand off to {p.to}", limits=lambda p: {"recipients": [p.to]},
             consequence=lambda p: {"targets": {"recipients": [p.to]}})
    async def _handoff(ctx, inp):
        act = await approvals_svc.intend_external_action(ctx, command_name="test.handoff", payload=inp.model_dump(),
                                                         dedupe_key=f"test.handoff:{inp.to}:{inp.body}", provider="test")
        return {"external_action_id": act.id}

    @approvals_svc.executor("test.handoff")
    async def _exec_handoff(db, act):
        # no adapter could do it: a person has to finish the work by hand
        return {"sent": False, "handed_off": True, "state": "manual_send_required"}

    @jobs.job("test.echo")
    async def _echo(jctx, payload):
        return {"echo": payload.get("x")}


async def test_owner_internal_command_records_activity_and_event(db, owner):
    res = await dispatch(ctx_for(db, owner), "test.note", {"text": "hello"})
    assert res.status == "ok" and res.data == {"echo": "hello"}
    acts = (await db.execute(select(ActivityEntry).where(ActivityEntry.what == "note: hello"))).scalars().all()
    assert len(acts) == 1 and acts[0].actor["user_id"] == owner.id
    evs = (await db.execute(select(Event).where(Event.type == "test.noted"))).scalars().all()
    assert evs and evs[0].processed_at is None


async def test_idempotent_replay_returns_stored_result(db, owner):
    c1 = ctx_for(db, owner, request_id="req-1")
    r1 = await dispatch(c1, "test.note", {"text": "once"})
    c2 = ctx_for(db, owner, request_id="req-1")
    r2 = await dispatch(c2, "test.note", {"text": "once"})
    assert r2.data == r1.data
    acts = (await db.execute(select(ActivityEntry).where(ActivityEntry.what == "note: once"))).scalars().all()
    assert len(acts) == 1


async def test_mechanic_blocked_outside_assigned_scope(db, mechanic):
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, mechanic), "test.note", {"text": "x", "vehicle_id": "veh-not-mine"})


async def test_consequential_needs_review_then_approval_executes_once(db, owner, manager):
    # manager cannot send: needs review (no inbox.send) -> blocked; owner-agent -> needs review
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, manager), "test.send", {"to": "a@example.com", "body": "hi"})
    res = await dispatch(ctx_for(db, owner, kind="agent"), "test.send", {"to": "a@example.com", "body": "hi"})
    assert res.status == "needs_review" and res.approval_id
    a = await db.get(Approval, res.approval_id)
    assert a.status == "pending" and a.title == "Send to a@example.com"
    # same request again does not duplicate the pending approval
    res2 = await dispatch(ctx_for(db, owner, kind="agent"), "test.send", {"to": "a@example.com", "body": "hi"})
    assert res2.approval_id == res.approval_id
    # owner approves -> external action intent queued, then worker executes with receipt
    ok = await dispatch(ctx_for(db, owner), "approvals.approve", {"approval_id": a.id, "expected_version": 1})
    assert ok.status == "ok"
    await db.refresh(a)
    assert a.status == "queued" and a.external_action_id
    await run_worker_once()
    await db.refresh(a)
    act = await db.get(ExternalAction, a.external_action_id)
    await db.refresh(act)
    assert act.state == "confirmed" and act.receipt["provider_ref"] == "msg-1"
    assert a.status == "confirmed" and a.receipt["provider_ref"] == "msg-1"
    # approving again is rejected (not pending); a stale version is a conflict
    from backend.app.core.errors import Blocked
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "approvals.approve", {"approval_id": a.id})


async def test_edit_creates_new_version_and_invalidates_old(db, owner):
    res = await dispatch(ctx_for(db, owner, kind="agent"), "test.send", {"to": "b@example.com", "body": "v1"})
    a = await db.get(Approval, res.approval_id)
    ed = await dispatch(ctx_for(db, owner), "approvals.edit", {"approval_id": a.id, "payload": {"body": "v2"}})
    b = await db.get(Approval, ed.data["approval"]["id"])
    await db.refresh(a)
    assert a.status == "invalidated" and "review again" in a.invalidated_reason
    assert b.approval_version == 2 and b.payload["body"] == "v2" and b.supersedes_id == a.id


async def test_jobs_run_with_lease_and_dedupe(db):
    j = await jobs.enqueue(db, "test.echo", {"x": 1}, dedupe_key="echo-1")
    dup = await jobs.enqueue(db, "test.echo", {"x": 2}, dedupe_key="echo-1")
    await db.commit()
    assert j is not None and dup is None
    await run_worker_once()
    row = await db.get(Job, j.id)
    await db.refresh(row)
    assert row.state == "done" and row.result == {"echo": 1} and row.lease_token is None


async def test_external_client_cannot_exceed_scope(db, owner):
    from backend.app.domain.actors import Actor
    from backend.app.domain.commands import CommandContext
    from backend.app.domain.policy import effective_perms
    ext = Actor(kind="external", user_id=owner.id, role="owner", perms=effective_perms("owner", {}),
                client_id="c1", client_scopes=["read:vehicles"], client_record_scope={"vehicle_ids": ["v1"]})
    with pytest.raises(Denied):
        await dispatch(CommandContext(db=db, actor=ext), "test.note", {"text": "x", "vehicle_id": "v1"})
    ext.client_scopes.append("write:tasks")
    with pytest.raises(Denied):
        await dispatch(CommandContext(db=db, actor=ext), "test.note", {"text": "x", "vehicle_id": "v2"})
    res = await dispatch(CommandContext(db=db, actor=ext), "test.note", {"text": "x", "vehicle_id": "v1"})
    assert res.status == "ok"


async def test_a_failing_outbox_handler_never_commits_its_half_applied_writes(db, owner):
    """One handler raises after writing a row: that row is rolled back and the event is retried
    later, while a sibling handler's effect on the same event is kept."""
    import uuid
    from datetime import datetime, timezone

    from backend.app.domain import events as events_mod
    from backend.app import db as dbmod

    tag = uuid.uuid4().hex[:8]
    if not any(p == "test.savepoint" for p, _ in events_mod.HANDLERS):
        @events_mod.on_event("test.savepoint")
        async def _good(db_, ev):                     # noqa: ANN001
            db_.add(ActivityEntry(at=datetime.now(timezone.utc), actor={}, what=f"sibling kept {ev.payload['tag']}",
                                  kind="system"))
            await db_.flush()

        @events_mod.on_event("test.savepoint")
        async def _bad(db_, ev):                      # noqa: ANN001
            db_.add(ActivityEntry(at=datetime.now(timezone.utc), actor={}, what=f"partial write {ev.payload['tag']}",
                                  kind="system"))
            await db_.flush()
            raise RuntimeError("handler exploded after writing")

    ev = Event(type="test.savepoint", payload={"tag": tag}, happened_at=datetime.now(timezone.utc), actor={})
    db.add(ev)
    await db.commit()
    ev_id = ev.id

    await events_mod.dispatch_pending(dbmod.SessionLocal, limit=500)

    rows = (await db.execute(select(ActivityEntry).where(ActivityEntry.what.like(f"%{tag}%")))).scalars().all()
    whats = {r.what for r in rows}
    assert f"partial write {tag}" not in whats, "the failing handler's write was committed"
    assert f"sibling kept {tag}" in whats, "the healthy handler's effect was thrown away"

    row = await db.get(Event, ev_id)
    await db.refresh(row)
    assert row.processed_at is None and row.attempts == 1, "the event stays pending for a retry"
    assert "RuntimeError" in (row.error or "") and row.next_attempt_at is not None, "the failure is recorded with backoff"

    # backed off: the next pass does not touch it again, so the sibling handler does not run twice
    await events_mod.dispatch_pending(dbmod.SessionLocal, limit=500)
    row = await db.get(Event, ev_id)
    await db.refresh(row)
    assert row.attempts == 1, "a backed-off event is not retried immediately"
    rows = (await db.execute(select(ActivityEntry).where(ActivityEntry.what == f"sibling kept {tag}"))).scalars().all()
    assert len(rows) == 1

    # when the backoff has elapsed the event is picked up again
    row.next_attempt_at = datetime.now(timezone.utc)
    await db.commit()
    await events_mod.dispatch_pending(dbmod.SessionLocal, limit=500)
    row = await db.get(Event, ev_id)
    await db.refresh(row)
    assert row.attempts == 2 and row.processed_at is None


async def test_a_handed_off_action_is_not_labelled_confirmed(db, owner):
    """An executor that could only hand the work to a person leaves the approval in its own
    `handed_off` status; "confirmed" is reserved for a provider-confirmed effect."""
    from backend.app.models.runtime import APPROVAL_STATES
    assert "handed_off" in APPROVAL_STATES
    res = await dispatch(ctx_for(db, owner, kind="agent"), "test.handoff", {"to": "h@example.com", "body": "by hand"})
    a = await db.get(Approval, res.approval_id)
    await dispatch(ctx_for(db, owner), "approvals.approve", {"approval_id": a.id, "expected_version": 1})
    await db.refresh(a)
    assert a.status == "queued"
    await run_worker_once()
    await db.refresh(a)
    act = await db.get(ExternalAction, a.external_action_id)
    await db.refresh(act)
    assert act.state == "handed_off" and act.receipt["sent"] is False
    assert a.status == "handed_off", "work a person still has to finish is never shown as confirmed"
    assert a.receipt["state"] == "manual_send_required"


def test_managed_postgres_urls_get_the_async_driver():
    """Railway and Heroku-style add-ons hand out a driverless URL that SQLAlchemy reads as
    synchronous, and `create_async_engine` then refuses it. Pasting the provider's own variable is
    the obvious thing to do, so the scheme is normalised rather than left as a note to read."""
    from backend.app.core.config import Settings
    assert Settings(DATABASE_URL="postgresql://u:p@h:5432/d").DATABASE_URL == "postgresql+asyncpg://u:p@h:5432/d"
    assert Settings(DATABASE_URL="postgres://u:p@h:5432/d").DATABASE_URL == "postgresql+asyncpg://u:p@h:5432/d"
    # an explicit driver is somebody's deliberate choice and is left alone
    assert Settings(DATABASE_URL="postgresql+asyncpg://u:p@h/d").DATABASE_URL == "postgresql+asyncpg://u:p@h/d"
    assert Settings(DATABASE_URL="postgresql+psycopg://u:p@h/d").DATABASE_URL == "postgresql+psycopg://u:p@h/d"
    assert Settings(TEST_DATABASE_URL="postgres://u@h/t").TEST_DATABASE_URL == "postgresql+asyncpg://u@h/t"
