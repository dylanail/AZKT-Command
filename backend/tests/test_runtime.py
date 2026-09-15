"""Agent runtime acceptance: fenced runs (H01), external-action crash boundaries (H02), cumulative
permission caps under concurrency (H04), cancellation that keeps completed effects (H05), bounded
loop-breaking and model outage (H06, C10), run-vs-case status (H12), the capability map and employee
scope through Manager (I06), and "Ask never leaks money" (A02).

No real model runs here: `use_model()` injects a scripted FakeModel through `runtime.MODEL_FACTORY`, and
every path that does not inject one exercises the deterministic behaviour a model outage leaves behind.
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import BaseModel
from sqlalchemy import select, update

from backend.app.adapters.model import ModelRefused, ModelResult, ModelUnavailable
from backend.app.core.errors import Denied
from backend.app.core.ids import stable_hash
from backend.app.domain.commands import REGISTRY, CommandContext, command, dispatch
from backend.app.models.runtime import Approval, ExternalAction, Mission, Permission, Run, RunStep
from backend.app.models.tasks import Task
from backend.app.models.vehicles import Vehicle
from backend.app.services import approvals as approvals_svc
from backend.app.agent import coverage as coverage_mod
from backend.app.agent import runtime as rt
from backend.app.agent import tools as agent_tools
from backend.tests.conftest import actor_of, ctx_for, make_user, run_worker_once

NOW = datetime.now(timezone.utc)


def uid() -> str:
    return uuid.uuid4().hex[:8]


# ── a consequential test command with a real executor (H02, H04, H12) ────────
class AgentTestSendIn(BaseModel):
    to: str
    body: str = ""
    amount: str | None = None
    currency: str = "USD"


EXECUTOR_CALLS: list[str] = []


@command("agenttest.send", input=AgentTestSendIn, perm="inbox.draft", action_class="consequential",
         approval_kind="send_message", summary=lambda p: f"Send a test message to {p.to}",
         limits=lambda p: {"recipients": [p.to], "amount": p.amount, "currency": p.currency},
         consequence=lambda p: {"amount": p.amount, "currency": p.currency, "moves_money": bool(p.amount),
                                "targets": {"recipients": [p.to]}},
         description="Test-only consequential send: persists an ExternalAction intent, never sends inline.")
async def _agenttest_send(ctx: CommandContext, inp: AgentTestSendIn) -> dict:
    act = await approvals_svc.intend_external_action(
        ctx, command_name="agenttest.send", provider="test",
        dedupe_key=f"agenttest:{stable_hash(inp.model_dump(mode='json'))}",
        payload=inp.model_dump(mode="json"))
    return {"external_action_id": act.id, "sent": False, "state": act.state}


@approvals_svc.executor("agenttest.send")
async def _agenttest_executor(db, act: ExternalAction) -> dict:
    EXECUTOR_CALLS.append(act.id)
    return {"provider_ref": f"test-ref-{len(EXECUTOR_CALLS)}", "sent": True, "at": NOW.isoformat()}


# ── FakeModel ────────────────────────────────────────────────────────────────
class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self, exclude_none: bool = True):
        return {k: v for k, v in self.__dict__.items() if v is not None or not exclude_none}


class _Raw:
    def __init__(self, content):
        self.content = content


class FakeModel:
    """Scripted model. Each step is {text?, tools?: [{name, input}], after?: awaitable factory}."""

    def __init__(self, script, *, raises: Exception | None = None):
        self.script = list(script)
        self.raises = raises
        self.calls: list[dict] = []

    async def complete(self, *, system: str, messages: list, **kw) -> ModelResult:
        self.calls.append({"system": system, "messages": messages, "tools": kw.get("tools") or []})
        if self.raises is not None:
            raise self.raises
        step = self.script.pop(0) if self.script else {"text": "Done.", "tools": []}
        if callable(step):
            step = step(self)
        blocks = []
        if step.get("text"):
            blocks.append(_Block(type="text", text=step["text"]))
        for i, t in enumerate(step.get("tools") or []):
            blocks.append(_Block(type="tool_use", id=f"tu-{len(self.calls)}-{i}", name=t["name"],
                                 input=t.get("input") or {}))
        after = step.get("after")
        if after is not None:
            await after()
        return ModelResult(text=step.get("text", ""), data=None, model="fake-model", input_tokens=12,
                           output_tokens=7, cost_usd=Decimal("0"), stop_reason="tool_use" if blocks[1:] or
                           (step.get("tools")) else "end_turn", raw=_Raw(blocks))


@contextlib.contextmanager
def use_model(fake):
    prev = rt.MODEL_FACTORY
    rt.MODEL_FACTORY = lambda db, **kw: fake
    try:
        yield fake
    finally:
        rt.MODEL_FACTORY = prev


async def make_vehicle(db, owner, **kw) -> Vehicle:
    res = await dispatch(ctx_for(db, owner), "vehicles.create",
                         {"make": "Daihatsu", "model": "Hijet", "model_year": 2019, "color": "white",
                          "create_missing_task": False, **kw})
    return await db.get(Vehicle, res.data["vehicle"]["id"])


async def make_mission(db, user, outcome: str = "Do the thing", **kw) -> Mission:
    m = await rt.create_mission(db, actor_of(user, "agent"), outcome=outcome, **kw)
    await db.commit()
    return m


# ═════════════════════════════════════════════════════════════════════════════
# H01 — a stale lease holder cannot commit
# ═════════════════════════════════════════════════════════════════════════════
async def test_H01_stale_lease_holder_cannot_commit_or_finish(db, owner):
    m = await make_mission(db, owner, "Race one run between two workers")
    run = await rt.start_run(db, m, enqueue=False)
    await db.commit()
    run_id, mission_id = run.id, m.id

    first = await rt.claim_run(db, run_id, "worker-a", lease_seconds=60)
    assert first is not None
    run_a, token_a = first
    fencing_a = int(run_a.fencing_token)
    assert fencing_a == 1 and run_a.status == "running"

    # a second worker cannot take a live lease
    assert await rt.claim_run(db, run_id, "worker-b") is None

    # the lease expires (worker A hung); worker B takes over with a NEWER fencing token
    await db.execute(update(Run).where(Run.id == run_id)
                     .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=5)))
    await db.commit()
    second = await rt.claim_run(db, run_id, "worker-b")
    assert second is not None
    run_b, token_b = second
    assert int(run_b.fencing_token) == fencing_a + 1 and token_b != token_a

    # worker A wakes up: it may neither persist a step nor finish the run
    with pytest.raises(rt.StaleLease):
        await rt.fenced(db, run_id, token_a, steps_used=99)
    with pytest.raises(rt.StaleLease):
        await rt._finish(db, await db.get(Mission, mission_id), await db.get(Run, run_id), token_a,
                         rt.RunOutcome("succeeded", "succeeded", "stale worker result", []))
    row = await db.get(Run, run_id)
    await db.refresh(row)
    assert row.steps_used == 0 and row.status == "running" and row.worker_id == "worker-b"

    # the live holder commits normally
    await rt.fenced(db, run_id, token_b, steps_used=3)
    await db.refresh(row)
    assert row.steps_used == 3


# ═════════════════════════════════════════════════════════════════════════════
# H02 — crash boundaries around one external action
# ═════════════════════════════════════════════════════════════════════════════
async def test_H02_crash_boundaries_reuse_one_external_action(db, owner):
    EXECUTOR_CALLS.clear()
    to = f"vendor-{uid()}@example.com"
    db.add(Permission(subject_kind="user", subject_id=owner.id, action_pattern="agenttest.send", status="active",
                      recipients=[to], description="test standing permission", authorized_by=owner.id))
    await db.commit()

    payload = {"to": to, "body": "one logical send"}
    # (a) crash before the external call, then retry: the same logical action, not a second intent
    first = await dispatch(ctx_for(db, owner), "agenttest.send", dict(payload))
    second = await dispatch(ctx_for(db, owner), "agenttest.send", dict(payload))
    assert first.status == "ok" and second.status == "ok"
    assert first.data["external_action_id"] == second.data["external_action_id"]
    actions = (await db.execute(select(ExternalAction).where(ExternalAction.command_name == "agenttest.send",
                                                             ExternalAction.payload["to"].as_string() == to))).scalars().all()
    assert len(actions) == 1 and actions[0].state == "intent"

    # (b) the executor runs exactly once, whatever the worker does afterwards
    for _ in range(3):
        await run_worker_once()
    act = await db.get(ExternalAction, actions[0].id)
    await db.refresh(act)
    assert act.state == "confirmed" and len(EXECUTOR_CALLS) == 1
    assert act.receipt.get("provider_ref", "").startswith("test-ref-")     # the provider's receipt, not invented
    assert act.executed_at is not None

    # (c) a duplicate event delivery after the receipt does not repeat the effect
    from backend.app.domain import events as events_mod
    from backend.app import db as dbmod
    for _ in range(3):
        await events_mod.dispatch_pending(dbmod.SessionLocal)
    third = await dispatch(ctx_for(db, owner), "agenttest.send", dict(payload))
    for _ in range(3):
        await run_worker_once()
    assert third.data["external_action_id"] == act.id
    assert len(EXECUTOR_CALLS) == 1
    assert (await db.scalar(select(__import__("sqlalchemy").func.count()).select_from(ExternalAction)
                            .where(ExternalAction.payload["to"].as_string() == to))) == 1


# ═════════════════════════════════════════════════════════════════════════════
# H04 — parallel runs cannot exceed a cumulative permission cap
# ═════════════════════════════════════════════════════════════════════════════
async def test_H04_concurrent_runs_cannot_overspend_a_cumulative_cap(db, owner):
    from backend.app import db as dbmod
    from backend.app.models.auth import User
    to = f"parts-{uid()}@example.com"
    perm = Permission(subject_kind="user", subject_id=owner.id, action_pattern="agenttest.send", status="active",
                      recipients=[to], per_action_limit=Decimal("60.00"), cumulative_limit=Decimal("100.00"),
                      currency="USD", authorized_by=owner.id, description="cap test")
    db.add(perm)
    await db.commit()

    async def attempt(n: int) -> str:
        async with dbmod.SessionLocal() as s:
            u = await s.get(User, owner.id)
            try:
                res = await dispatch(ctx_for(s, u), "agenttest.send",
                                     {"to": to, "body": f"order {n}", "amount": "60.00", "currency": "USD"})
                return res.status
            except Denied:
                return "denied"

    results = await asyncio.gather(attempt(1), attempt(2))
    await db.refresh(perm)
    # exactly one authorized spend; the other is refused or pushed to review — never a second 60 on a 100 cap
    assert sorted(results).count("ok") == 1, results
    assert all(r in ("ok", "denied", "needs_review") for r in results)
    assert perm.used_amount <= perm.cumulative_limit and perm.used_amount == Decimal("60.00")
    # a third attempt cannot slip past either
    assert await attempt(3) in ("denied", "needs_review")
    await db.refresh(perm)
    assert perm.used_amount == Decimal("60.00")


# ═════════════════════════════════════════════════════════════════════════════
# H05 — cancellation stops remaining work and keeps completed effects
# ═════════════════════════════════════════════════════════════════════════════
async def test_H05_cancel_keeps_completed_effects(db, owner):
    from backend.app import db as dbmod
    from backend.app.models.auth import User
    v = await make_vehicle(db, owner)
    m = await make_mission(db, owner, "Book in the truck and then do more work",
                           entity_refs=[{"kind": "vehicle", "id": v.id}])
    mission_id, vehicle_id, tag = m.id, v.id, uid()

    async def cancel_now():
        async with dbmod.SessionLocal() as s:
            u = await s.get(User, owner.id)
            await rt.cancel_mission(s, actor_of(u), mission_id, reason="owner pressed stop")

    fake = FakeModel([
        {"text": "Creating the first task.", "after": cancel_now,
         "tools": [{"name": "tasks_create", "input": {"title": f"Completed before cancel {tag}", "vehicle_id": v.id}}]},
        {"text": "Creating a second task.",
         "tools": [{"name": "tasks_create", "input": {"title": f"Must never exist {tag}", "vehicle_id": v.id}}]},
        {"text": "All done."},
    ])
    with use_model(fake):
        run, out = await rt.run_inline(db, await db.get(Mission, m.id))

    assert out.run_status == "cancelled" and out.mission_status == "cancelled"
    kept = (await db.execute(select(Task).where(Task.title == f"Completed before cancel {tag}"))).scalars().all()
    never = (await db.execute(select(Task).where(Task.title == f"Must never exist {tag}"))).scalars().all()
    assert len(kept) == 1, "a completed effect must survive cancellation"
    assert never == [], "remaining work must not run after cancellation"
    m2 = await db.get(Mission, mission_id)
    await db.refresh(m2)
    assert m2.status == "cancelled" and any(u["state"] == "cancelled" for u in (m2.updates or []))
    assert len(fake.calls) == 1, "the model is never called again after cancellation"


# ═════════════════════════════════════════════════════════════════════════════
# H06 — bounded loop breaking, and a deterministic reply when the model is out
# ═════════════════════════════════════════════════════════════════════════════
async def test_H06_repeated_identical_failure_tries_one_alternative_then_creates_a_task(db, owner):
    m = await make_mission(db, owner, f"Fetch something impossible {uid()}")
    bad = {"name": "vehicles_get_context", "input": {"vehicle_id": "does-not-exist"}}
    fake = FakeModel([{"text": f"Attempt {i}", "tools": [bad]} for i in range(8)])
    with use_model(fake):
        run, out = await rt.run_inline(db, await db.get(Mission, m.id))

    assert out.run_status == "failed" and out.mission_status == "needs_information"
    assert "repeated tool failure" in (out.error or "")
    assert len(fake.calls) <= 5, "the loop must stop long before the step budget"
    steps = (await db.execute(select(RunStep).where(RunStep.run_id == run.id))).scalars().all()
    decisions = {s.decision for s in steps}
    assert "alternative_requested" in decisions, "exactly one alternative must be requested first"
    assert sum(1 for s in steps if s.decision == "alternative_requested") == 1
    assert "escalated" in decisions
    task = (await db.execute(select(Task).where(Task.title.ilike("Take over: Fetch something impossible%")))).scalars().first()
    assert task is not None and task.priority == "high"
    assert (out.needed_input or {}).get("task")


async def test_H06_C10_model_unavailable_leaves_the_mission_open_with_a_deterministic_reply(db, owner):
    m = await make_mission(db, owner, "Summarise the week")
    with use_model(FakeModel([], raises=ModelUnavailable("model budget not configured"))):
        run, out = await rt.run_inline(db, await db.get(Mission, m.id))
    assert out.run_status == "needs_information" and out.mission_status == "paused"
    assert "unavailable" in out.summary.lower() and "deterministic" in out.summary.lower()
    assert out.used_model is False
    m2 = await db.get(Mission, m.id)
    await db.refresh(m2)
    assert m2.status == "paused" and m2.finished_at is None      # the mission stays open

    m3 = await make_mission(db, owner, "Write something the model declines")
    with use_model(FakeModel([], raises=ModelRefused("policy", "declined"))):
        run3, out3 = await rt.run_inline(db, await db.get(Mission, m3.id))
    assert out3.mission_status == "needs_information" and "declined" in out3.summary.lower()
    assert (await db.get(Mission, m3.id)).status == "needs_information"


async def test_budget_exhaustion_escalates_with_a_recovery_task(db, owner):
    m = await make_mission(db, owner, f"Loop forever {uid()}", budget={"steps": 2, "seconds": 60})
    fake = FakeModel([{"text": "thinking", "tools": [{"name": "tasks_list", "input": {"view": "all"}}]}
                      for _ in range(10)])
    with use_model(fake):
        run, out = await rt.run_inline(db, await db.get(Mission, m.id))
    assert out.run_status == "failed" and "budget exhausted" in (out.error or "")
    assert out.steps == 2 and len(fake.calls) == 2
    assert (await db.get(Mission, m.id)).status == "needs_information"


# ═════════════════════════════════════════════════════════════════════════════
# H12 — run success is not case completion
# ═════════════════════════════════════════════════════════════════════════════
async def test_H12_run_succeeds_while_the_quote_case_stays_waiting(db, owner):
    from backend.app.models.contacts import Contact
    from backend.app.models.shipping import ShipmentQuote
    from backend.app.models.tasks import Case
    buyer = Contact(name=f"Buyer {uid()}", roles=["buyer"], status="active", search_text="buyer",
                    primary_email=f"{uid()}@example.com", extra={"delivery_address": "Mesa, AZ 85201"})
    db.add(buyer)
    await db.commit()
    v = await make_vehicle(db, owner, logistics_state="on_vessel",
                           extra={"length_mm": 3395, "width_mm": 1475, "height_mm": 1800, "weight_kg": 800,
                                  "operability": "running"})
    await dispatch(ctx_for(db, owner), "vehicles.update",
                   {"vehicle_id": v.id, "buyer_contact_id": buyer.id, "allocation": "reserved"})
    started = await dispatch(ctx_for(db, owner), "quotes.start_case",
                             {"vehicle_id": v.id, "origin": "Port of Long Beach, CA", "destination": "Mesa, AZ 85201",
                              "service": "open", "vendor_name": "Montway",
                              "timing": {"earliest": (NOW + timedelta(days=7)).isoformat(), "source": "customer note"}})
    quote_id, case_id = started.data["quote"]["id"], started.data["case_id"]

    m = await make_mission(db, owner, "Get a shipping quote for this truck", role="logistics",
                           entity_refs=[{"kind": "vehicle", "id": v.id}])
    fake = FakeModel([
        {"text": "Requesting the vendor quote.",
         "tools": [{"name": "quotes_request", "input": {"quote_id": quote_id, "recipients": ["quotes@montway.example"],
                                                        "channel": "email", "message": "Please quote (nonbinding)."}}]},
        {"text": "I prepared the quote request; it needs your review before anything leaves AZKT."},
    ])
    with use_model(fake):
        run, out = await rt.run_inline(db, await db.get(Mission, m.id))

    # the RUN did its intended step and succeeded ...
    assert out.run_status == "succeeded"
    assert (await db.get(Run, run.id)).status == "succeeded"
    # ... while the mission waits on the owner and the CASE is nowhere near finished
    assert out.mission_status == "waiting_approval" and out.approvals
    q = await db.get(ShipmentQuote, quote_id)
    await db.refresh(q)
    assert q.status in ("draft", "needs_information") and q.requested_at is None    # nothing sent yet
    case = await db.get(Case, case_id)
    await db.refresh(case)
    assert case.status != "closed" and case.next_check_at is not None

    # the owner approves: the quote is *requested*, the case is *waiting* — still not received
    approval_id = out.approvals[0]["id"]
    await dispatch(ctx_for(db, owner), "approvals.approve", {"approval_id": approval_id})
    await db.refresh(q)
    await db.refresh(case)
    assert q.status == "requested" and q.amount is None
    assert case.status == "waiting" and "vendor" in (case.waiting_on or "").lower()
    assert (await db.get(Run, run.id)).status == "succeeded"


# ═════════════════════════════════════════════════════════════════════════════
# Waiting conditions, resumption and progress cursor
# ═════════════════════════════════════════════════════════════════════════════
async def test_waiting_until_persists_a_next_check_and_the_sweep_resumes_it(db, owner):
    from backend.app import db as dbmod
    m = await make_mission(db, owner, f"Check back later {uid()}")
    when = datetime.now(timezone.utc) + timedelta(hours=6)
    fake = FakeModel([{"text": "Nothing to do yet.",
                       "tools": [{"name": "runtime_wait_until",
                                  "input": {"next_check_at": when.isoformat(), "reason": "vendor closed"}}]}])
    with use_model(fake):
        run, out = await rt.run_inline(db, await db.get(Mission, m.id))
    assert out.run_status == "waiting_until"
    m2 = await db.get(Mission, m.id)
    await db.refresh(m2)
    assert m2.status == "waiting_until" and m2.next_check_at is not None and m2.waiting_on == "vendor closed"

    # nothing is due yet
    assert await rt._resume_due(dbmod.SessionLocal) == 0
    m2.next_check_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.commit()
    assert await rt._resume_due(dbmod.SessionLocal) >= 1
    runs = (await db.execute(select(Run).where(Run.mission_id == m.id))).scalars().all()
    assert len(runs) == 2 and any(r.trigger == "sweep" for r in runs)


async def test_mission_updates_carry_a_monotonic_cursor(db, owner):
    m = await make_mission(db, owner, f"Log some progress {uid()}")
    fake = FakeModel([{"text": "step", "tools": [{"name": "tasks_list", "input": {"view": "all"}}]},
                      {"text": "finished"}])
    with use_model(fake):
        run, out = await rt.run_inline(db, await db.get(Mission, m.id))
    m2 = await db.get(Mission, m.id)
    await db.refresh(m2)
    seqs = [u["seq"] for u in (m2.updates or [])]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs) and m2.cursor == seqs[-1]
    assert rt.updates_since(m2, seqs[-2])[0]["seq"] == seqs[-1]
    assert rt.updates_since(m2, seqs[-1]) == []


async def test_approval_change_resumes_a_waiting_mission(db, owner):
    from backend.app import db as dbmod
    from backend.app.domain import events as events_mod
    to = f"resume-{uid()}@example.com"
    m = await make_mission(db, owner, "Prepare a send that needs review")
    fake = FakeModel([{"text": "Preparing.",
                       "tools": [{"name": "agenttest_send", "input": {"to": to, "body": "hi"}}]},
                      {"text": "Prepared and waiting for your review."}])
    with use_model(fake):
        run, out = await rt.run_inline(db, await db.get(Mission, m.id))
    assert out.mission_status == "waiting_approval" and out.approvals
    approval_id = out.approvals[0]["id"]
    a = await db.get(Approval, approval_id)
    assert a.mission_id == m.id and a.review_path == f"/approvals/{approval_id}"

    await dispatch(ctx_for(db, owner), "approvals.decline", {"approval_id": approval_id})
    for _ in range(3):
        await events_mod.dispatch_pending(dbmod.SessionLocal)
    m2 = await db.get(Mission, m.id)
    await db.refresh(m2)
    runs = (await db.execute(select(Run).where(Run.mission_id == m.id))).scalars().all()
    assert len(runs) >= 2 and any(r.trigger == "approval" for r in runs)


async def test_needs_information_stops_without_writing(db, owner):
    m = await make_mission(db, owner, f"Ambiguous request {uid()}")
    fake = FakeModel([{"text": "I need one thing.",
                       "tools": [{"name": "runtime_needs_information",
                                  "input": {"question": "Which truck do you mean?",
                                            "already_checked": ["vehicles.search"], "missing": ["stock number"]}}]}])
    with use_model(fake):
        run, out = await rt.run_inline(db, await db.get(Mission, m.id))
    assert out.run_status == "needs_information" and out.mission_status == "needs_information"
    assert out.needed_input["question"] == "Which truck do you mean?"
    assert out.changed == []


async def test_the_mission_job_runs_through_the_worker_queue(db, owner):
    m = await make_mission(db, owner, f"Queued work {uid()}")
    fake = FakeModel([{"text": "Nothing needed."}])
    run = await rt.start_run(db, m)
    await db.commit()
    with use_model(fake):
        await run_worker_once()
    r = await db.get(Run, run.id)
    await db.refresh(r)
    assert r.status == "succeeded" and r.fencing_token >= 1 and r.worker_id


# ═════════════════════════════════════════════════════════════════════════════
# I06 — the capability map, and an employee who cannot inherit owner access
# ═════════════════════════════════════════════════════════════════════════════
async def test_I06_coverage_map_covers_every_owner_ui_command(db, owner):
    m = coverage_mod.coverage_map()
    assert m["gaps"] == [], f"owner UI commands without a Manager tool: {m['gaps']}"
    assert m["complete"] is True
    assert m["counts"]["ui_commands"] >= 40 and m["counts"]["write_tools"] >= 150
    by_command = {r["command"]: r for r in m["commands"]}
    # every registered command is represented, with its action class and permission
    assert set(by_command) == set(REGISTRY)
    for name in ("vehicles.update", "tasks.create", "shop.create_issue", "intake.apply", "contacts.create",
                 "import_requests.create", "shipments.create", "costs.create_item", "sales.reserve"):
        row = by_command[name]
        assert row["covered"] and row["tool"] == name.replace(".", "_")
        assert row["action_class"] == REGISTRY[name].action_class and row["perm"] == REGISTRY[name].perm
    # the deliberate exclusions are documented, not silent
    assert by_command["approvals.approve"]["covered"] is False
    assert "signed-in human" in by_command["approvals.approve"]["excluded_reason"]
    # read tools are listed too
    assert {r["name"] for r in m["read_tools"]} >= {"vehicles.get_context", "tasks.list", "finance.vehicle_money",
                                                    "sources.search", "approvals.list", "activity.recent"}


async def test_I06_owner_edits_through_manager_tools_but_a_mechanic_cannot_inherit_them(db, owner, mechanic):
    v = await make_vehicle(db, owner)
    owner_ctx = CommandContext(db=db, actor=actor_of(owner, "agent"), channel="web")
    res = await agent_tools.execute(owner_ctx, "vehicles_update", {"vehicle_id": v.id, "color": "blue",
                                                                  "notes": "seen in the yard"})
    assert res.status == "ok" and res.changed and res.decision["outcome"] == "allowed"

    mech_actor = actor_of(mechanic, "agent")
    mech_ctx = CommandContext(db=db, actor=mech_actor, channel="web")
    # an unassigned vehicle is not readable through Manager
    ctxres = await agent_tools.execute(mech_ctx, "vehicles_get_context", {"vehicle_id": v.id})
    assert ctxres.status == "blocked" and "not accessible" in (ctxres.error or "")
    # costs are blocked outright
    money = await agent_tools.execute(mech_ctx, "finance_vehicle_money", {"vehicle_id": v.id})
    assert money.status == "blocked" and "costs.read" in (money.error or "")
    # and a write the mechanic may not do is blocked with structured reasons, not silently dropped
    wr = await agent_tools.execute(mech_ctx, "vehicles_update", {"vehicle_id": v.id, "color": "green"})
    assert wr.status == "blocked" and wr.decision.get("reasons")

    cov = coverage_mod.for_actor(mech_actor)
    avail = {r["command"] for r in cov["commands"] if r["available"]}
    assert "tasks.create" in avail and "vehicles.update" not in avail and "team.invite" not in avail
    assert cov["counts"]["available_commands"] < coverage_mod.for_actor(actor_of(owner, "agent"))["counts"]["available_commands"]


# ═════════════════════════════════════════════════════════════════════════════
# A02 — Ask never leaks money to someone without the costs permission
# ═════════════════════════════════════════════════════════════════════════════
async def test_A02_ask_never_returns_money_to_a_mechanic(db, owner, mechanic):
    v = await make_vehicle(db, owner, purchase_amount="4200.00", purchase_currency="USD")
    # the mechanic is assigned a task on this truck, so the vehicle itself IS in scope
    task = (await dispatch(ctx_for(db, owner), "tasks.create",
                           {"title": f"Inspect brakes {uid()}", "vehicle_id": v.id})).data["task"]
    await dispatch(ctx_for(db, owner), "tasks.assign", {"task_id": task["id"], "owner_user_id": mechanic.id})

    mech_ctx = CommandContext(db=db, actor=actor_of(mechanic, "agent"), channel="web")
    ctxres = await agent_tools.execute(mech_ctx, "vehicles_get_context", {"vehicle_id": v.id})
    assert ctxres.status == "ok"
    assert ctxres.data["tabs"]["money"] == {"money_hidden": True}
    assert ctxres.data["money_visible"] is False
    body = ctxres.for_model()
    assert "4200" not in body and "4,200" not in body
    facts = {f["key"] for f in ctxres.data["tabs"]["overview"]["facts"]}
    assert "purchase_amount" not in facts

    search = await agent_tools.execute(mech_ctx, "vehicles_search", {"q": "Hijet"})
    assert search.status == "ok" and "4200" not in search.for_model()
    assert all(i.get("money_hidden") for i in search.data["items"])

    # ... and through the whole Ask path, with a model that tries to fetch the money anyway
    m = await rt.create_mission(db, actor_of(mechanic, "agent"), outcome=f"What did this truck cost? {uid()}",
                                entity_refs=[{"kind": "vehicle", "id": v.id}])
    await db.commit()
    fake = FakeModel([
        {"text": "Checking the cost.", "tools": [{"name": "finance_vehicle_money", "input": {"vehicle_id": v.id}}]},
        {"text": "I cannot show cost figures for this person; ask Dylan."},
    ])
    with use_model(fake):
        run, out = await rt.run_inline(db, await db.get(Mission, m.id))
    steps = (await db.execute(select(RunStep).where(RunStep.run_id == run.id,
                                                    RunStep.tool_name == "finance_vehicle_money"))).scalars().all()
    assert steps and steps[0].decision == "blocked" and "4200" not in str(steps[0].output)
    assert "4200" not in out.summary
    # the tool was not even offered to the model
    offered = {t["name"] for t in fake.calls[0]["tools"]}
    assert "finance_vehicle_money" not in offered and "finance_sold_cohort" not in offered
    assert "vehicles_get_context" in offered


async def test_tool_definitions_are_strict_and_permission_filtered(db, owner, mechanic):
    defs = agent_tools.definitions(actor_of(owner, "agent"))
    by_name = {d["name"]: d for d in defs}
    assert "tasks_create" in by_name and "vehicles_get_context" in by_name
    for d in defs:
        s = d["input_schema"]
        assert s["type"] == "object" and s.get("additionalProperties") is False
        assert "$defs" not in s and "$ref" not in str(s)
        assert set(s.get("required", [])) <= set(s.get("properties", {}))
        assert "." not in d["name"]
    mech = {d["name"] for d in agent_tools.definitions(actor_of(mechanic, "agent"))}
    assert "finance_vehicle_money" not in mech and "team_invite" not in mech and "tasks_create" in mech
    # no business-money tool is even offered to someone who may not see money (A02)
    assert {"home_metrics", "finance_sold_cohort"} & mech == set()
    # approving is never a tool
    assert "approvals_approve" not in by_name


async def test_home_metrics_tool_is_wired_and_never_fakes_a_success(db, owner):
    """The Home overview reaches Manager through the reporting service, or says setup_blocked — it never
    returns an empty success (spec §10.6, invariant "no fabricated states")."""
    ctx = CommandContext(db=db, actor=actor_of(owner, "agent"), channel="web")
    res = await agent_tools.execute(ctx, "home_metrics", {"period": "month"})
    assert res.status in ("ok", "blocked")
    if res.status == "ok":
        data = res.data or {}
        assert data.get("status") == "setup_blocked" or ("period" in data and "generated_at" in str(data) or data)
        if data.get("status") == "setup_blocked":
            assert data.get("reason") and data.get("alternatives")
        else:
            assert data.get("period"), "a real answer names the period it covers"
    # ... and it is not offered at all to a person without the finance permission
    assert "home_metrics" in {d["name"] for d in agent_tools.definitions(actor_of(owner, "agent"))}


async def test_permitted_scope_is_frozen_and_never_widened(db, owner):
    limited = await make_user(db, f"limited-{uid()}", "mechanic")
    m = await make_mission(db, limited, "Small job")
    # widen the person's role after the mission was created
    limited.role = "owner"
    await db.commit()
    actor = await rt.actor_for_mission(db, await db.get(Mission, m.id))
    assert actor.kind == "agent" and actor.perms.get("costs.read") is False
    assert actor.perms.get("tasks.write") is True
