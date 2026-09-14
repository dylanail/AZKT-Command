"""Procedures / Teach ladder (spec §9.4, G14) and evidence-backed promotion proposals (spec §9.5, Decisions
"Autonomy promotion review", G13): tests gate promotion, rollback withdraws and revalidates, thresholds gate proposals,
declining keeps supervision, a critical regression pauses the workflow, and no step ever creates a permission by itself."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.app.core.errors import Blocked, Denied
from backend.app.domain.commands import dispatch
from backend.app.domain.policy import evaluate
from backend.app.models.knowledge import Procedure, ProcedureVersion, PromotionProposal
from backend.app.models.runtime import Approval, Event, Permission, WorkflowControl
from backend.app.services import evaluation as ev
from backend.app.services import procedures as psvc
from backend.tests.conftest import actor_of, ctx_for, login, make_user


def _u() -> str:
    return uuid.uuid4().hex[:8]


TEACH = """Goal: reply to a routine availability question.
Needs the vehicle stock number
1. Look up the vehicle card and read the current commercial state.
2. Draft a reply that states the current availability only.
Never promise a delivery date without carrier confirmation.
If the buyer asks about financing, escalate to Dylan.
Done when the draft is linked to the conversation and cites the vehicle card.
"""
CASES = [
    {"name": "routine", "input": {"text": "is the carry still available?", "fields": {"vehicle stock number": "STK-0001"}},
     "expect": {"outcome": "complete", "required_evidence": ["draft is linked"], "must_include_steps": ["current availability"]}},
    {"name": "delivery date promise", "input": {"text": "can you promise a delivery date next week?"},
     "expect": {"outcome": "escalate", "escalate_on": "financing"}},
]


async def propose(db, user, **kw) -> dict:
    payload = {"title": kw.pop("title", f"Availability reply {_u()}"), "text": kw.pop("text", TEACH), "test_cases": kw.pop("test_cases", CASES),
               "workflow_key": kw.pop("workflow_key", f"reply.availability.{_u()}"), **kw}
    return (await dispatch(ctx_for(db, user), "procedures.propose", payload)).data


# ── Teach parsing ────────────────────────────────────────────────────────────
def test_parse_teach_text_sections_and_demonstration_uncertainty():
    spec = psvc.parse_teach_text(TEACH)
    assert [s["text"] for s in spec["normal_path"]] == ["Look up the vehicle card and read the current commercial state.",
                                                        "Draft a reply that states the current availability only."]
    assert spec["hard_constraints"][0]["text"].startswith("Never promise a delivery date") and spec["hard_constraints"][0]["trigger"] == "delivery date"
    assert spec["escalation"] == ["If the buyer asks about financing, escalate to Dylan."]
    assert spec["evidence_of_completion"][0].startswith("Done when")
    assert spec["inputs"] == ["the vehicle stock number"]
    demo, interp = psvc.build_spec(psvc.ProcedureProposeIn(title="demo", text="1. click Montway\n2. type the stock number", source_kind="demonstration"))
    assert interp == "uncertain" and demo["executable"] is False and demo["fact_status"] == "inferred"
    assert all(s["confidence"] == "inferred" and s["executable"] is False for s in demo["normal_path"]) and "not instructions" in demo["interpretation_note"]


def test_replay_is_deterministic():
    spec, _ = psvc.build_spec(psvc.ProcedureProposeIn(title="t", text=TEACH, test_cases=CASES))
    res = psvc.run_replay(spec, datetime.now(timezone.utc))
    assert res["ok"] and res["passed"] == 2 and res["spec_hash"] == psvc.spec_hash(spec)
    weaker = {**spec, "hard_constraints": []}  # the constraint disappears -> the delivery-date case no longer escalates
    res2 = psvc.run_replay(weaker, datetime.now(timezone.utc))
    assert not res2["ok"] and res2["failed"] == 1 and "expected outcome escalate" in res2["cases"][1]["reasons"][0]
    assert psvc.run_replay({**spec, "test_cases": []}, datetime.now(timezone.utc))["reason"].startswith("no test cases")


# ── G14 ──────────────────────────────────────────────────────────────────────
async def test_G14_test_failure_blocks_promotion_rollback_withdraws_and_revalidates(db, owner, manager):
    wf = f"reply.availability.{_u()}"
    perms_before = len((await db.execute(select(Permission))).scalars().all())
    d = await propose(db, owner, workflow_key=wf)
    proc, v1 = d["procedure"], d["version"]
    assert v1["stage"] == "proposed" and proc["status"] == "proposed" and d["created"] is True and proc["goal"] == "reply to a routine availability question."
    # idempotent proposal: the same spec for the same key is the same version (retry-safe)
    again = (await dispatch(ctx_for(db, owner), "procedures.propose", {"title": proc["title"], "text": TEACH, "test_cases": CASES, "key": proc["key"]})).data
    assert again["created"] is False and again["version"]["id"] == v1["id"]
    # promotion before tests: blocked; manager cannot promote at all
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "procedures.promote", {"version_id": v1["id"]})
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, manager), "procedures.promote", {"version_id": v1["id"]})
    t = await dispatch(ctx_for(db, owner), "procedures.run_tests", {"version_id": v1["id"]})
    assert t.data["results"]["ok"] and t.data["decision"] == "Allowed"
    # one step at a time: proposed -> offline_tested -> shadow
    p1 = await dispatch(ctx_for(db, owner), "procedures.promote", {"version_id": v1["id"]})
    assert p1.data["version"]["stage"] == "offline_tested" and p1.data["procedure"]["status"] == "offline_tested" and p1.data["permission_created"] is False
    p2 = await dispatch(ctx_for(db, owner), "procedures.promote", {"version_id": v1["id"]})
    assert p2.data["version"]["stage"] == "shadow" and p2.data["procedure"]["current_version_id"] == v1["id"]
    # v2 changes the procedure (drops the constraint): replay fails, promotion blocked
    d2 = (await dispatch(ctx_for(db, owner), "procedures.propose", {"key": proc["key"], "title": proc["title"], "text": TEACH.replace("Never promise a delivery date without carrier confirmation.\n", ""),
                                                                    "test_cases": CASES})).data
    v2 = d2["version"]
    assert v2["version_no"] == 2 and v2["stage"] == "proposed"
    t2 = await dispatch(ctx_for(db, owner), "procedures.run_tests", {"version_id": v2["id"]})
    assert t2.data["results"]["ok"] is False and t2.data["decision"] == "Blocked"
    with pytest.raises(Blocked) as ei:
        await dispatch(ctx_for(db, owner), "procedures.promote", {"version_id": v2["id"]})
    assert "failed" in ei.value.message
    # the passing version keeps running; a spec edit after passing tests also blocks (tests bound to spec hash)
    row = await db.get(ProcedureVersion, v1["id"])
    assert (await db.get(Procedure, proc["id"])).current_version_id == v1["id"] and row.stage == "shadow"
    # an approval bound to v1 (prepared under it) is invalidated when v1 is rolled back; the procedure reverts
    a = Approval(kind="send_message", command_name="test.send", payload={"to": "x@example.com", "body": "hi"}, payload_hash="h", title="bound",
                 record_versions={"procedure_version": v1["id"]}, status="pending", expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    db.add(a)
    await db.commit()
    rb = await dispatch(ctx_for(db, owner), "procedures.rollback", {"version_id": v1["id"], "reason": "regression in shadow"})
    assert rb.data["version"]["stage"] == "withdrawn" and rb.data["version"]["withdrawn_reason"] == "regression in shadow"
    assert rb.data["reverted_to"] is None and rb.data["procedure"]["status"] == "withdrawn" and rb.data["procedure"]["current_version_id"] is None
    assert rb.data["approvals_invalidated"] == 1
    await db.refresh(a)
    assert a.status == "invalidated" and "withdrawn" in a.invalidated_reason
    evs = (await db.execute(select(Event).where(Event.type == "procedure.withdrawn", Event.aggregate_id == proc["id"]))).scalars().all()
    assert len(evs) == 1 and evs[0].payload["revalidate"] is True and evs[0].payload["version_id"] == v1["id"]
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "procedures.promote", {"version_id": v1["id"]})  # withdrawn stays withdrawn
    assert await psvc.current_version_for(db, wf) is None
    # history retained
    got = await psvc.get_procedure(db, proc["id"])
    assert [x["version_no"] for x in got["versions"]] == [2, 1] and got["stage_history"][-1]["to"] == "withdrawn"
    # nothing along the way created a permission
    assert len((await db.execute(select(Permission))).scalars().all()) == perms_before


async def test_bounded_automatic_needs_existing_permission_and_never_creates_one(db, owner):
    wf = f"reply.routine.{_u()}"
    d = await propose(db, owner, workflow_key=wf)
    v = d["version"]
    await dispatch(ctx_for(db, owner), "procedures.run_tests", {"version_id": v["id"]})
    for _ in range(3):
        await dispatch(ctx_for(db, owner), "procedures.promote", {"version_id": v["id"]})
    assert (await db.get(ProcedureVersion, v["id"])).stage == "supervised"
    with pytest.raises(Blocked) as ei:
        await dispatch(ctx_for(db, owner), "procedures.promote", {"version_id": v["id"]})
    assert "does not create one" in ei.value.message
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "procedures.promote", {"version_id": v["id"], "permission_id": "nope"})
    perm = Permission(subject_kind="workflow", workflow_key=wf, action_pattern="inbox.send", status="active", authorized_by=owner.id, rate_limit={"per_day": 5})
    db.add(perm)
    await db.commit()
    ok = await dispatch(ctx_for(db, owner), "procedures.promote", {"version_id": v["id"], "permission_id": perm.id})
    assert ok.data["version"]["stage"] == "bounded_automatic" and ok.data["version"]["permission_id"] == perm.id and ok.data["permission_created"] is False
    # a demonstration-derived version can never become automatic
    demo = (await dispatch(ctx_for(db, owner), "procedures.propose", {"title": f"Demo {_u()}", "text": "1. open the form\n2. submit", "source_kind": "demonstration",
                                                                      "test_cases": [{"name": "n", "input": {"text": "x"}, "expect": {"outcome": "complete"}}]})).data["version"]
    await dispatch(ctx_for(db, owner), "procedures.run_tests", {"version_id": demo["id"]})
    for _ in range(3):
        await dispatch(ctx_for(db, owner), "procedures.promote", {"version_id": demo["id"]})
    with pytest.raises(Blocked) as ei:
        await dispatch(ctx_for(db, owner), "procedures.promote", {"version_id": demo["id"], "permission_id": perm.id})
    assert "demonstration" in ei.value.message


async def test_procedures_api(client, db, owner, manager):
    login(client, owner)
    r = await client.post("/api/procedures", json={"title": f"API proc {_u()}", "text": TEACH, "test_cases": CASES})
    assert r.status_code == 200, r.text
    pid, vid = r.json()["data"]["procedure"]["id"], r.json()["data"]["version"]["id"]
    r = await client.post(f"/api/procedures/{pid}/versions/{vid}/tests", json={})
    assert r.status_code == 200 and r.json()["data"]["results"]["ok"]
    r = await client.post(f"/api/procedures/{pid}/versions/{vid}/promote", json={})
    assert r.status_code == 200 and r.json()["data"]["version"]["stage"] == "offline_tested"
    r = await client.get("/api/procedures")
    assert r.status_code == 200 and any(p["id"] == pid for p in r.json()["items"]) and "total" in r.json()
    r = await client.get(f"/api/procedures/{pid}/versions")
    assert r.status_code == 200 and r.json()["total"] == 1
    login(client, manager)
    r = await client.get(f"/api/procedures/{pid}")
    assert r.status_code == 200 and r.json()["id"] == pid
    r = await client.post(f"/api/procedures/{pid}/versions/{vid}/promote", json={})
    assert r.status_code == 403
    r = await client.post(f"/api/procedures/{pid}/versions/{vid}/rollback", json={})
    assert r.status_code == 403


# ── G13 ──────────────────────────────────────────────────────────────────────
async def seed_outcomes(db, user, wf: str, *, n: int = 52, accepted: int = 50, days: int = 20, start: datetime | None = None) -> None:
    start = start or (datetime.now(timezone.utc) - timedelta(days=days))
    for i in range(n):
        outcome = "accepted" if i < accepted else "factual_edit"
        at = start + timedelta(seconds=int(i * days * 86400 / max(n - 1, 1)))
        await dispatch(ctx_for(db, user), "learning.record_outcome", {"workflow_key": wf, "outcome": outcome, "entity_kind": "draft",
                                                                      "entity_id": f"d-{wf}-{i}", "review_seconds": 30, "at": at.isoformat(),
                                                                      "dedupe_key": f"{wf}:{i}"})


async def seed_failure_tests(db, user, wf: str, *, passing: bool = True) -> None:
    r = await dispatch(ctx_for(db, user), "eval.add_cases", {"set_name": f"{wf}:failures", "workflow_key": wf, "cases": [
        {"input": {"text": "wrong recipient attempt"}, "expected": {"recipients": ["a@example.com"]}, "tags": ["failure"], "source_ref": f"{wf}-f1"},
        {"input": {"text": "stale eta"}, "expected": {"forbidden_facts": ["arrives friday"]}, "tags": ["failure"], "source_ref": f"{wf}-f2"}]})
    cases = r.data["items"]

    async def runner(inp: dict) -> dict:
        if "recipient" in inp["text"]:
            return {"body": "Sending now.", "recipients": ["a@example.com"] if passing else ["a@example.com", "b@example.com"]}
        return {"body": "The truck is at the port; the carrier has not confirmed a date." if passing else "It arrives Friday."}
    out = await ev.run_eval(db, actor_of(user), f"{wf}:failures", runner, runner_name="fixture", split=None)
    assert out["summary"]["cases"] == len(cases)


async def test_G13_proposal_only_after_thresholds_decline_keeps_supervision_regression_pauses(db, owner):
    wf = f"reply.availability.{_u()}"
    # too little evidence: no proposal, supervised mode stays
    await seed_outcomes(db, owner, wf, n=12, accepted=12, days=5)
    r = await dispatch(ctx_for(db, owner), "promotion_proposals.generate", {"workflow_key": wf})
    assert r.data["created"] is False and r.data["proposal"] is None and any("reviewed cases" in u for u in r.data["unmet"]) and any("days" in u for u in r.data["unmet"])
    assert (await ev.workflow_status(db, wf))["mode"] == "supervised"
    # enough cases and days, 96% accepted, zero critical, but no targeted failure tests yet -> still no proposal
    wf2 = f"reply.availability.{_u()}"
    await seed_outcomes(db, owner, wf2, n=52, accepted=50, days=20)
    r = await dispatch(ctx_for(db, owner), "promotion_proposals.generate", {"workflow_key": wf2})
    assert r.data["created"] is False and r.data["unmet"] == ["targeted failure tests: no targeted failure tests recorded"]
    await seed_failure_tests(db, owner, wf2, passing=True)
    perms_before = len((await db.execute(select(Permission))).scalars().all())
    r = await dispatch(ctx_for(db, owner), "promotion_proposals.generate", {"workflow_key": wf2})
    assert r.data["created"] is True, r.data
    prop = r.data["proposal"]
    e = prop["evidence"]
    assert e["cases"] == 52 and e["accepted_pct"] == 96.2 and e["critical_errors"] == 0 and e["days"] >= 14 and e["failure_tests"]["ok"]
    assert e["samples"]["accepted"] and e["samples"]["edited"] and prop["thresholds"]["cases"] == 50 and prop["status"] == "proposed"
    pp = prop["proposed_permission"]
    assert pp["action_pattern"] == "inbox.send" and pp["rate_limit"] == {"per_day": 20} and pp["expires_at"] and prop["exclusions"]
    assert "52" in r.data["message"] and "50 needed no substantive changes" in r.data["message"]
    # nothing is enabled by the proposal itself; policy still needs review for the automated actor
    assert len((await db.execute(select(Permission))).scalars().all()) == perms_before
    # generating again while pending does not duplicate
    r2 = await dispatch(ctx_for(db, owner), "promotion_proposals.generate", {"workflow_key": wf2})
    assert r2.data["created"] is False and r2.data["proposal"]["id"] == prop["id"]
    # owner declines: supervision continues, no permission, and no nagging within 14 days
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, await make_user(db, f"mgr-{_u()}", "manager")), "promotion_proposals.decide", {"proposal_id": prop["id"], "decision": "decline"})
    dec = await dispatch(ctx_for(db, owner), "promotion_proposals.decide", {"proposal_id": prop["id"], "decision": "decline", "expected_version": prop["version"], "note": "not yet"})
    assert dec.data["proposal"]["status"] == "declined" and dec.data["permission"] is None and dec.data["mode"] == "supervised"
    assert len((await db.execute(select(Permission))).scalars().all()) == perms_before
    r3 = await dispatch(ctx_for(db, owner), "promotion_proposals.generate", {"workflow_key": wf2})
    assert r3.data["created"] is False and "not asking again" in r3.data["reason"]
    # owner request overrides the quiet period
    r4 = await dispatch(ctx_for(db, owner), "promotion_proposals.generate", {"workflow_key": wf2, "force_ask": True})
    assert r4.data["created"] is True and r4.data["proposal"]["asked_count"] == 2
    prop2 = r4.data["proposal"]
    # later a critical error: the workflow pauses, the pending proposal pauses, the automated actor is blocked
    reg = await dispatch(ctx_for(db, owner, kind="agent"), "promotion_proposals.record_regression",
                        {"workflow_key": wf2, "severity": "critical", "description": "sent stale ETA to wrong recipient", "entity_kind": "message", "entity_id": "m-9"})
    assert reg.data["paused"] is True and reg.data["proposals_paused"] == [prop2["id"]] and reg.data["decision"] == "Blocked"
    ctrl = await db.get(WorkflowControl, f"workflow:{wf2}")
    assert ctrl.paused and "critical regression" in ctrl.reason
    assert (await db.get(PromotionProposal, prop2["id"])).status == "paused"
    st = await ev.workflow_status(db, wf2)
    assert st["mode"] == "paused" and st["evidence"]["critical_errors"] == 1
    r5 = await dispatch(ctx_for(db, owner), "promotion_proposals.generate", {"workflow_key": wf2, "force_ask": True})
    assert r5.data["created"] is False and "paused" in r5.data["reason"]
    # the pause is the deterministic policy switch: an automated actor running that workflow is blocked
    from backend.app.domain.commands import spec_for
    spec = spec_for("test.send")
    spec.workflow_key = wf2
    try:
        d = await evaluate(db, actor_of(owner, kind="agent"), spec, spec.input_model(to="a@example.com", body="x"))
        assert d.outcome == "blocked" and "paused" in d.reasons[0]
    finally:
        spec.workflow_key = None
    # a repeated identical regression report does not double-count
    reg2 = await dispatch(ctx_for(db, owner, kind="agent"), "promotion_proposals.record_regression",
                         {"workflow_key": wf2, "severity": "critical", "description": "sent stale ETA to wrong recipient", "entity_kind": "message", "entity_id": "m-9"})
    assert reg2.data["recorded"] is False


async def test_G13_approve_creates_exact_bounded_permission_and_regression_pauses_it(db, owner):
    wf = f"reply.routine.{_u()}"
    await seed_outcomes(db, owner, wf, n=60, accepted=58, days=30)
    await seed_failure_tests(db, owner, wf, passing=True)
    r = await dispatch(ctx_for(db, owner), "promotion_proposals.generate", {"workflow_key": wf, "action_pattern": "test.send",
                                                                            "permission_overrides": {"domains": ["example.com"], "rate_limit": {"per_day": 3}}})
    prop = r.data["proposal"]
    assert prop["proposed_permission"]["domains"] == ["example.com"] and prop["proposed_permission"]["action_pattern"] == "test.send"
    assert ev.action_for_workflow("reply.availability.kei") == "inbox.send" and ev.action_for_workflow("unknown.flow") is None
    # the owner-agent cannot approve on its own: exact approval is required
    ag = await dispatch(ctx_for(db, owner, kind="agent"), "promotion_proposals.decide", {"proposal_id": prop["id"], "decision": "approve"})
    assert ag.status == "needs_review"
    ok = await dispatch(ctx_for(db, owner), "promotion_proposals.decide", {"proposal_id": prop["id"], "decision": "approve",
                                                                            "permission_edits": {"rate_limit": {"per_day": 2}}, "note": "ok within limits"})
    perm = ok.data["permission"]
    assert ok.data["mode"] == "bounded_automatic" and perm["status"] == "active" and perm["action_pattern"] == "test.send"
    assert perm["workflow_key"] == wf and perm["rate_limit"] == {"per_day": 2} and perm["domains"] == ["example.com"] and perm["expires_at"]
    assert perm["authorized_by"] == owner.id and perm["proposal_id"] == prop["id"] and perm["excluded_cases"]
    row = await db.get(Permission, perm["id"])
    assert row.status == "active" and row.subject_kind == "workflow"
    evs = (await db.execute(select(Event).where(Event.type == "permission.created", Event.aggregate_id == perm["id"]))).scalars().all()
    assert len(evs) == 1
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "promotion_proposals.decide", {"proposal_id": prop["id"], "decision": "decline"})
    # the standing permission is what the policy engine honours for the automated actor (domain-limited)
    from backend.app.domain.commands import spec_for
    spec = spec_for("test.send")
    spec.workflow_key = wf
    try:
        d = await evaluate(db, actor_of(owner, kind="agent"), spec, spec.input_model(to="buyer@example.com", body="x"))
        assert d.outcome == "allowed" and d.permission_id == perm["id"]
        d2 = await evaluate(db, actor_of(owner, kind="agent"), spec, spec.input_model(to="buyer@other.org", body="x"))
        assert d2.outcome == "needs_review"
    finally:
        spec.workflow_key = None
    # a critical regression pauses the workflow and the standing permission: back to review
    reg = await dispatch(ctx_for(db, owner), "promotion_proposals.record_regression", {"workflow_key": wf, "severity": "critical", "description": "wrong recipient"})
    assert reg.data["permissions_paused"] == [perm["id"]]
    await db.refresh(row)
    assert row.status == "paused" and (await db.get(PromotionProposal, prop["id"])).status == "paused"
    spec.workflow_key = wf
    try:
        d3 = await evaluate(db, actor_of(owner, kind="agent"), spec, spec.input_model(to="buyer@example.com", body="x"))
        assert d3.outcome == "blocked"
    finally:
        spec.workflow_key = None


async def test_high_risk_workflows_and_failing_failure_tests_never_propose(db, owner):
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "promotion_proposals.generate", {"workflow_key": "bid.submit"})
    wf = f"reply.availability.{_u()}"
    await seed_outcomes(db, owner, wf, n=55, accepted=54, days=16)
    await seed_failure_tests(db, owner, wf, passing=False)
    r = await dispatch(ctx_for(db, owner), "promotion_proposals.generate", {"workflow_key": wf})
    assert r.data["created"] is False and any("failure test" in u for u in r.data["unmet"])
    # a single critical error in the window blocks too
    await dispatch(ctx_for(db, owner), "learning.record_outcome", {"workflow_key": wf, "outcome": "critical_error", "severity": "critical", "dedupe_key": f"{wf}:crit"})
    r = await dispatch(ctx_for(db, owner), "promotion_proposals.generate", {"workflow_key": wf, "thresholds": {"require_failure_tests": False}})
    assert r.data["created"] is False and any("critical" in u for u in r.data["unmet"])


async def test_eval_split_by_group_and_time_and_checks_measure_factual_vs_stylistic(db, owner):
    cutoff = datetime(2026, 6, 1, tzinfo=timezone.utc)
    cases = [{"input": {"q": i}, "expected": {}, "contact_id": f"c-{i % 3}", "source_ref": f"m-{_u()}-{i}",
              "happened_at": (cutoff + timedelta(days=1 if i == 9 else -30)).isoformat()} for i in range(10)]
    r = await dispatch(ctx_for(db, owner), "eval.add_cases", {"set_name": f"split-{_u()}", "cases": cases, "holdout_pct": 40, "time_cutoff": cutoff.isoformat()})
    items = r.data["items"]
    by_contact = {}
    for it in items:
        by_contact.setdefault(it["contact_id"], set()).add(it["split"])
    # every customer's cases land in one split (no cross-customer leakage); the post-cutoff case is held out
    assert all(len(s) == 1 for cid, s in by_contact.items() if cid != items[9]["contact_id"]) and items[9]["split"] == "test"
    again = await dispatch(ctx_for(db, owner), "eval.add_cases", {"set_name": items[0]["set_name"], "cases": cases[:2]})
    assert again.data["added"] == 0 and again.data["skipped"] == 2
    chk = ev.deterministic_checks({"recipients": ["a@example.com"], "facts": ["reserved"], "forbidden_facts": ["available"],
                                   "questions": ["when does it arrive?"], "style": ["thanks"], "max_length": 20},
                                  {"body": "Thanks! It is reserved and it should arrive next week.", "recipients": ["a@example.com", "b@example.com"]})
    assert chk["factual_errors"] == 1 and chk["stylistic"] == 1 and chk["ok"] is False
    assert [c["key"] for c in chk["checks"] if not c["ok"]] == ["recipients", "length"]
    assert ev.deterministic_checks({"facts": ["x"]}, {"body": ""})["unanswered"] is True
    summary = await ev.eval_summary(db, items[0]["set_name"])
    assert summary["items"][0]["total"] == 10


async def test_proposals_api(client, db, owner, manager):
    wf = f"reply.availability.{_u()}"
    await seed_outcomes(db, owner, wf, n=52, accepted=52, days=15)
    await seed_failure_tests(db, owner, wf, passing=True)
    login(client, owner)
    r = await client.post("/api/proposals/generate", json={"workflow_key": wf})
    assert r.status_code == 200 and r.json()["data"]["created"] is True, r.text
    pid = r.json()["data"]["proposal"]["id"]
    r = await client.get("/api/proposals", params={"status": "proposed"})
    assert r.status_code == 200 and any(p["id"] == pid for p in r.json()["items"])
    r = await client.get(f"/api/proposals/{pid}")
    assert r.status_code == 200 and r.json()["workflow_paused"] is False and r.json()["decision"] == "Needs review"
    r = await client.get(f"/api/knowledge/workflows/{wf}")
    assert r.status_code == 200 and r.json()["mode"] == "supervised"
    login(client, manager)
    assert (await client.get("/api/proposals")).status_code == 403
    r = await client.post(f"/api/proposals/{pid}/decide", json={"decision": "approve"})
    assert r.status_code == 403
    login(client, owner)
    r = await client.post(f"/api/proposals/{pid}/decide", json={"decision": "decline"})
    assert r.status_code == 200 and r.json()["data"]["proposal"]["status"] == "declined"
    r = await client.post("/api/proposals/regression", json={"workflow_key": wf, "severity": "critical", "description": "bad send"})
    assert r.status_code == 200 and r.json()["data"]["paused"] is True
    r = await client.get(f"/api/knowledge/workflows/{wf}")
    assert r.json()["mode"] == "paused"
