"""Held-out evaluation, workflow outcome measurement and evidence-backed promotion proposals
(spec §9.5, §11.3 "confidence is not authority", Decisions "Autonomy promotion review", G12, G13).

Evaluation sets are split by customer/thread and by time to limit leakage; every target is flagged
`excluded_from_retrieval` so the retrieval layer never returns it (see retrieval.chunk_clauses). Checks are
deterministic (recipients, facts, question coverage) and factual errors are counted separately from stylistic
differences; no model quality score is used as proof.

Promotion proposals read WorkflowOutcome rows for one workflow. A proposal is created only when the configured
thresholds are met (defaults: 50 reviewed cases over 14 days, >= 95% accepted without substantive correction,
0 critical errors, targeted failure tests passing). It never self-enables: only `promotion_proposals.decide`
(owner_only) creates a Permission row from the exact proposed permission. Declining keeps supervision and the
owner is not asked again within 14 days unless new evidence arrives. A critical regression pauses the affected
workflow (WorkflowControl `workflow:<key>`), pauses its proposals and pauses standing permissions for it.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..core.errors import Blocked, Conflict, NotFound, ValidationFailed
from ..core.ids import stable_hash
from ..core.money import parse_amount
from ..domain.commands import CommandContext, command, dispatch
from ..models.knowledge import EvalCase, PromotionProposal, WorkflowOutcome
from ..models.runtime import Permission, WorkflowControl
from .learning import OUTCOMES, record_outcome, serialize_outcome

DEFAULT_THRESHOLDS = {"cases": 50, "days": 14, "accepted_pct": 95, "critical_errors": 0, "require_failure_tests": True}
ASK_AGAIN_DAYS = 14
NEW_EVIDENCE_MIN_CASES = 10
REVIEWED = ("accepted", "style_edit", "factual_edit", "declined", "critical_error")
ACCEPTED_WITHOUT_SUBSTANTIVE = ("accepted", "style_edit")
# Never eligible for self-proposed automatic promotion (spec §11.2 last rows, §9.5 "high-risk classes").
HIGH_RISK_PREFIXES = ("bid", "payment", "refund", "price", "terms", "permission", "team", "verify", "delete", "install", "settings")
WORKFLOW_ACTIONS = {"reply": "inbox.send", "reply.availability": "inbox.send", "reply.routine": "inbox.send",
                    "listing.publish": "listings.publish", "listing.update": "listings.publish",
                    "quote.request": "shipping.request_quote", "translation.request": "sourcing.request_translation"}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ── evaluation cases ─────────────────────────────────────────────────────────
def serialize_case(c: EvalCase) -> dict:
    return {"id": c.id, "version": c.version, "set_name": c.set_name, "split": c.split, "input": dict(c.input or {}),
            "expected": dict(c.expected or {}), "tags": list(c.tags or []), "source_ref": c.source_ref, "target_ref": c.target_ref,
            "contact_id": c.contact_id, "thread_key": c.thread_key, "happened_at": _iso(c.happened_at), "workflow_key": c.workflow_key,
            "excluded_from_retrieval": bool(c.excluded_from_retrieval), "last_result": dict(c.last_result or {}),
            "last_run_at": _iso(c.last_run_at)}


def assign_split(group_key: str | None, happened_at: datetime | None, *, holdout_pct: int, time_cutoff: datetime | None) -> str:
    """Split by customer/thread group (stable hash) and by time (everything after the cutoff is held out)."""
    if time_cutoff is not None and happened_at is not None and happened_at >= time_cutoff:
        return "test"
    if not group_key:
        return "test"
    return "test" if int(stable_hash(group_key)[:8], 16) % 100 < holdout_pct else "train"


class EvalAddCasesIn(BaseModel):
    set_name: str = Field(min_length=1, max_length=120)
    cases: list[dict] = Field(min_length=1)
    holdout_pct: int = Field(default=30, ge=1, le=100)
    time_cutoff: datetime | None = None
    workflow_key: str | None = None


@command("eval.add_cases", input=EvalAddCasesIn, perm="knowledge.write", action_class="internal",
         description="Add held-out evaluation cases (split by customer/thread and time); targets are excluded from retrieval.")
async def eval_add_cases(ctx: CommandContext, inp: EvalAddCasesIn) -> dict:
    added, skipped = [], 0
    for raw in inp.cases:
        if not isinstance(raw, dict) or not raw.get("input"):
            raise ValidationFailed("each case needs an input")
        source_ref = raw.get("source_ref")
        target_ref = raw.get("target_ref") or (source_ref if source_ref and ":" in source_ref else None)
        key = stable_hash({"set": inp.set_name, "src": source_ref or target_ref or raw.get("input")})[:32]
        existing = (await ctx.db.execute(select(EvalCase).where(EvalCase.dedupe_key == key))).scalars().first()
        if existing is not None:
            skipped += 1
            continue
        happened_at = raw.get("happened_at")
        if isinstance(happened_at, str):
            from ..core.time import parse_iso
            happened_at = parse_iso(happened_at)
        group = raw.get("contact_id") or raw.get("thread_key") or source_ref
        split = raw.get("split") or assign_split(group, happened_at, holdout_pct=inp.holdout_pct, time_cutoff=inp.time_cutoff)
        if split not in ("train", "val", "test"):
            raise ValidationFailed("split must be train|val|test")
        c = EvalCase(set_name=inp.set_name, split=split, input=dict(raw["input"]), expected=dict(raw.get("expected") or {}),
                     tags=list(raw.get("tags") or []), source_ref=source_ref, target_ref=target_ref, contact_id=raw.get("contact_id"),
                     thread_key=raw.get("thread_key"), happened_at=happened_at, dedupe_key=key,
                     excluded_from_retrieval=bool(raw.get("excluded_from_retrieval", True)), workflow_key=raw.get("workflow_key") or inp.workflow_key,
                     created_by=ctx.actor.user_id)
        ctx.db.add(c)
        added.append(c)
    await ctx.db.flush()
    for c in added:
        ctx.changed.append({"kind": "eval_case", "id": c.id, "version": c.version})
    ctx.record(f"Evaluation set {inp.set_name}: +{len(added)} cases ({skipped} already present)", entity_kind="eval_set",
               entity_id=inp.set_name, kind="system", state="updated", visibility="owner",
               details={"added": len(added), "skipped": skipped, "splits": {s: sum(1 for c in added if c.split == s) for s in ("train", "val", "test")}})
    ctx.emit("eval.cases_added", aggregate_type="eval_set", aggregate_id=inp.set_name, payload={"added": len(added), "skipped": skipped})
    return {"items": [serialize_case(c) for c in added], "added": len(added), "skipped": skipped, "set_name": inp.set_name}


# ── deterministic checks ─────────────────────────────────────────────────────
def _norm(s: Any) -> str:
    return " ".join(str(s or "").lower().split())


def deterministic_checks(expected: dict, produced: dict) -> dict:
    """Recipient / fact / coverage checks provided by the caller's expectations. Factual and stylistic findings are
    reported separately; a missing produced draft is 'unanswered'."""
    body = _norm(produced.get("body") or produced.get("text") or "")
    checks: list[dict] = []
    factual_errors = 0
    stylistic = 0
    if not body:
        return {"ok": False, "unanswered": True, "factual_errors": 0, "stylistic": 0, "checks": [{"key": "answer", "ok": False, "label": "no draft produced"}]}
    exp_rcpt = {_norm(r) for r in (expected.get("recipients") or [])}
    got_rcpt = {_norm(r) for r in (produced.get("recipients") or produced.get("to") or [])}
    if exp_rcpt:
        ok = exp_rcpt == got_rcpt
        checks.append({"key": "recipients", "ok": ok, "label": "recipients match" if ok else f"recipients differ: extra {sorted(got_rcpt - exp_rcpt)}, missing {sorted(exp_rcpt - got_rcpt)}",
                       "class": "critical"})
        if not ok:
            factual_errors += 1
    for fact in expected.get("facts") or []:
        ok = _norm(fact) in body
        checks.append({"key": "fact", "ok": ok, "label": f"states {fact!r}" if ok else f"missing fact {fact!r}", "class": "factual"})
        if not ok:
            factual_errors += 1
    for fact in expected.get("forbidden_facts") or []:
        ok = _norm(fact) not in body
        checks.append({"key": "forbidden_fact", "ok": ok, "label": f"omits {fact!r}" if ok else f"contains stale/forbidden {fact!r}", "class": "factual"})
        if not ok:
            factual_errors += 1
    for q in expected.get("questions") or []:
        words = [w for w in re.findall(r"\w+", _norm(q)) if len(w) >= 4]
        ok = bool(words) and sum(1 for w in words if w in body) >= max(1, len(words) // 2)
        checks.append({"key": "coverage", "ok": ok, "label": f"answers {q!r}" if ok else f"does not address {q!r}", "class": "factual"})
        if not ok:
            factual_errors += 1
    for hint in expected.get("style") or []:
        ok = _norm(hint) in body
        checks.append({"key": "style", "ok": ok, "label": f"style {hint!r}", "class": "stylistic"})
        if not ok:
            stylistic += 1
    max_len = expected.get("max_length")
    if max_len:
        ok = len(body) <= int(max_len)
        checks.append({"key": "length", "ok": ok, "label": f"length {len(body)} <= {max_len}", "class": "stylistic"})
        if not ok:
            stylistic += 1
    return {"ok": factual_errors == 0, "unanswered": False, "factual_errors": factual_errors, "stylistic": stylistic, "checks": checks}


class EvalRecordResultsIn(BaseModel):
    set_name: str
    results: list[dict]  # [{case_id, produced, result}]
    runner: str = "unknown"
    config: dict = Field(default_factory=dict)  # model id / retrieval settings under test (pinned after evaluation)


@command("eval.record_results", input=EvalRecordResultsIn, perm="knowledge.write", action_class="internal",
         description="Store per-case deterministic check results and the factual-vs-stylistic summary for a run.")
async def eval_record_results(ctx: CommandContext, inp: EvalRecordResultsIn) -> dict:
    summary = {"cases": 0, "passed": 0, "factual_errors": 0, "stylistic": 0, "unanswered": 0, "runner": inp.runner, "config": inp.config,
               "at": ctx.now.isoformat()}
    for r in inp.results:
        c = await ctx.db.get(EvalCase, r.get("case_id"))
        if c is None or c.set_name != inp.set_name:
            raise NotFound(f"eval case {r.get('case_id')} not in set {inp.set_name}")
        result = dict(r.get("result") or {})
        c.last_result = {**result, "runner": inp.runner, "config": inp.config, "at": ctx.now.isoformat(),
                         "produced_excerpt": str((r.get("produced") or {}).get("body") or "")[:500]}
        c.last_run_at = ctx.now
        ctx.touch(c, "eval_case")
        summary["cases"] += 1
        summary["passed"] += 1 if result.get("ok") else 0
        summary["factual_errors"] += int(result.get("factual_errors") or 0)
        summary["stylistic"] += int(result.get("stylistic") or 0)
        summary["unanswered"] += 1 if result.get("unanswered") else 0
    summary["pass_pct"] = round(100.0 * summary["passed"] / summary["cases"], 1) if summary["cases"] else None
    ctx.record(f"Evaluation run {inp.set_name}: {summary['passed']}/{summary['cases']} passed, {summary['factual_errors']} factual, "
               f"{summary['stylistic']} stylistic", entity_kind="eval_set", entity_id=inp.set_name, kind="system", state="run",
               visibility="owner", details=summary)
    ctx.emit("eval.run_completed", aggregate_type="eval_set", aggregate_id=inp.set_name, payload=summary)
    return {"summary": summary}


async def run_eval(db, actor, set_name: str, runner: Callable[[dict], Awaitable[dict]], *, split: str | None = "test",
                   runner_name: str = "custom", config: dict | None = None, commit: bool = True) -> dict:
    """Run every case of a split (None = all splits, e.g. targeted failure sets) through `runner(input) -> produced`
    and apply deterministic checks; results are persisted through eval.record_results. The runner receives the case
    input only, never the expected answer."""
    q = select(EvalCase).where(EvalCase.set_name == set_name)
    if split:
        q = q.where(EvalCase.split == split)
    rows = (await db.execute(q.order_by(EvalCase.created_at))).scalars().all()
    results = []
    for c in rows:
        try:
            produced = await runner(dict(c.input or {}))
        except Exception as e:  # noqa: BLE001
            produced = {"body": "", "error": f"{type(e).__name__}: {e}"}
        res = deterministic_checks(c.expected or {}, produced or {})
        results.append({"case_id": c.id, "produced": produced or {}, "result": res})
    ctx = CommandContext(db=db, actor=actor, channel="worker")
    out = await dispatch(ctx, "eval.record_results", {"set_name": set_name, "results": results, "runner": runner_name,
                                                       "config": config or {}}, commit=commit)
    return {"summary": out.data["summary"], "results": results}


async def eval_summary(db, set_name: str | None = None) -> dict:
    q = select(EvalCase.set_name, EvalCase.split, func.count()).group_by(EvalCase.set_name, EvalCase.split)
    if set_name:
        q = q.where(EvalCase.set_name == set_name)
    sets: dict[str, dict] = {}
    for name, split, n in (await db.execute(q)).all():
        sets.setdefault(name, {"set_name": name, "splits": {}, "total": 0, "last_run": None})
        sets[name]["splits"][split] = int(n)
        sets[name]["total"] += int(n)
    for name in sets:
        last = (await db.execute(select(EvalCase).where(EvalCase.set_name == name, EvalCase.last_run_at.is_not(None))
                                 .order_by(EvalCase.last_run_at.desc()).limit(1))).scalars().first()
        if last is not None:
            sets[name]["last_run"] = {"at": _iso(last.last_run_at), "runner": (last.last_result or {}).get("runner")}
    return {"items": list(sets.values()), "total": len(sets)}


async def failure_tests_state(db, workflow_key: str) -> dict:
    """Targeted failure cases for a workflow: eval set '<workflow>:failures' (or cases tagged 'failure' with the workflow)."""
    rows = (await db.execute(select(EvalCase).where(EvalCase.workflow_key == workflow_key))).scalars().all()
    rows = [c for c in rows if "failure" in (c.tags or []) or c.set_name.endswith(":failures")]
    if not rows:
        return {"recorded": 0, "passed": 0, "ok": False, "reason": "no targeted failure tests recorded"}
    run = [c for c in rows if c.last_result]
    passed = sum(1 for c in run if (c.last_result or {}).get("ok"))
    ok = bool(run) and len(run) == len(rows) and passed == len(rows)
    return {"recorded": len(rows), "run": len(run), "passed": passed, "ok": ok,
            "reason": None if ok else ("failure tests not run" if len(run) < len(rows) else f"{len(rows) - passed} failure test(s) failing")}


# ── workflow evidence ────────────────────────────────────────────────────────
async def workflow_evidence(db, workflow_key: str, *, now: datetime, since: datetime | None = None) -> dict:
    q = select(WorkflowOutcome).where(WorkflowOutcome.workflow_key == workflow_key)
    if since is not None:
        q = q.where(WorkflowOutcome.at >= since)
    rows = (await db.execute(q.order_by(WorkflowOutcome.at))).scalars().all()
    reviewed = [r for r in rows if r.outcome in REVIEWED]
    counts = {o: sum(1 for r in rows if r.outcome == o) for o in OUTCOMES}
    cases = len(reviewed)
    first = reviewed[0].at if reviewed else None
    last = reviewed[-1].at if reviewed else None
    days = (last - first).days if first and last else 0
    accepted = sum(1 for r in reviewed if r.outcome in ACCEPTED_WITHOUT_SUBSTANTIVE)
    accepted_pct = round(100.0 * accepted / cases, 1) if cases else 0.0
    critical = sum(1 for r in rows if r.outcome == "critical_error" or r.severity == "critical")
    review_times = [r.review_seconds for r in reviewed if r.review_seconds is not None]
    median = sorted(review_times)[len(review_times) // 2] if review_times else None
    samples = {"accepted": [serialize_outcome(r) for r in reviewed if r.outcome == "accepted"][-5:],
               "edited": [serialize_outcome(r) for r in reviewed if r.outcome in ("style_edit", "factual_edit")][-5:],
               "declined_or_critical": [serialize_outcome(r) for r in reviewed if r.outcome in ("declined", "critical_error")][-5:]}
    return {"cases": cases, "days": days, "accepted": accepted, "accepted_pct": accepted_pct, "critical_errors": critical,
            "counts": counts, "window_from": _iso(first), "window_to": _iso(last), "median_review_seconds": median,
            "samples": samples, "as_of": now.isoformat()}


def serialize_proposal(p: PromotionProposal) -> dict:
    return {"id": p.id, "version": p.version, "workflow_key": p.workflow_key, "action_class": p.action_class, "status": p.status,
            "evidence": dict(p.evidence or {}), "thresholds": dict(p.thresholds or {}), "proposed_permission": dict(p.proposed_permission or {}),
            "exclusions": list(p.exclusions or []), "decided_by": p.decided_by, "decided_at": _iso(p.decided_at), "decision_note": p.decision_note,
            "permission_id": p.permission_id, "last_asked_at": _iso(p.last_asked_at), "asked_count": p.asked_count,
            "regression": dict(p.regression or {}), "paused_at": _iso(p.paused_at), "paused_reason": p.paused_reason,
            "window": {"from": _iso(p.window_from), "to": _iso(p.window_to)}, "created_at": _iso(p.created_at),
            "decision": {"proposed": "Needs review", "approved": "Allowed", "declined": "Needs review", "paused": "Blocked",
                         "withdrawn": "Blocked"}.get(p.status, "Needs review")}


def is_high_risk(workflow_key: str) -> bool:
    k = (workflow_key or "").lower()
    return any(k == pfx or k.startswith(pfx + ".") or k.startswith(pfx + "_") for pfx in HIGH_RISK_PREFIXES)


def action_for_workflow(workflow_key: str) -> str | None:
    """Longest dotted-prefix match: 'reply.availability.kei' -> 'reply.availability' -> 'reply'."""
    parts = (workflow_key or "").split(".")
    while parts:
        hit = WORKFLOW_ACTIONS.get(".".join(parts))
        if hit:
            return hit
        parts.pop()
    return None


def default_permission(workflow_key: str, action_pattern: str | None, now: datetime, evidence: dict, overrides: dict) -> dict:
    """The exact standing permission the owner would enable (spec §11.3 permission record). Bounded by default:
    per-day rate limit, 90-day expiry, no recipients/records outside what was reviewed, high-risk cases excluded."""
    base = {
        "subject_kind": "workflow", "subject_id": None, "workflow_key": workflow_key,
        "action_pattern": action_pattern or action_for_workflow(workflow_key) or workflow_key,
        "recipients": [], "domains": [], "allowed_records": {}, "fields": [],
        "per_action_limit": None, "cumulative_limit": None, "currency": None,
        "rate_limit": {"per_day": 20},
        "freshness_requirements": {"thread_refreshed_within_minutes": 15, "facts_from": "structured records only"},
        "excluded_cases": ["disputes", "refunds", "price or terms changes", "unknown identity", "taken-over threads",
                           "unsupported promises", "any recipient outside the reviewed set"],
        "effective_from": now.isoformat(), "expires_at": (now + timedelta(days=90)).isoformat(),
        "description": f"Automatic {workflow_key} within the reviewed scope; evidence: {evidence.get('cases')} cases, "
                       f"{evidence.get('accepted_pct')}% accepted, {evidence.get('critical_errors')} critical errors.",
        "monitoring": {"pause_on": "critical regression", "review_samples": True, "revocable": True},
        "still_requires_review": ["anything in excluded_cases", "recipients/records outside scope", "amount limits exceeded"],
    }
    for k, v in (overrides or {}).items():
        if k in base:
            base[k] = v
    return base


class ProposalGenerateIn(BaseModel):
    workflow_key: str = Field(min_length=1)
    thresholds: dict = Field(default_factory=dict)
    action_pattern: str | None = None
    permission_overrides: dict = Field(default_factory=dict)
    since: datetime | None = None
    force_ask: bool = False  # owner request: ask again even inside the quiet period


@command("promotion_proposals.generate", input=ProposalGenerateIn, perm="knowledge.write", action_class="internal",
         description="Create an evidence-backed promotion proposal only when thresholds are met. Never enables anything; "
                     "no nagging (quiet period unless new evidence).")
async def proposals_generate(ctx: CommandContext, inp: ProposalGenerateIn) -> dict:
    now = ctx.now
    thresholds = {**DEFAULT_THRESHOLDS, **{k: v for k, v in (inp.thresholds or {}).items() if k in DEFAULT_THRESHOLDS}}
    if is_high_risk(inp.workflow_key):
        raise Blocked("high-risk workflows never become automatic from sample counts", workflow_key=inp.workflow_key)
    evidence = await workflow_evidence(ctx.db, inp.workflow_key, now=now, since=inp.since)
    failures = await failure_tests_state(ctx.db, inp.workflow_key)
    evidence["failure_tests"] = failures
    unmet: list[str] = []
    if evidence["cases"] < int(thresholds["cases"]):
        unmet.append(f"{evidence['cases']} reviewed cases < {thresholds['cases']}")
    if evidence["days"] < int(thresholds["days"]):
        unmet.append(f"{evidence['days']} days of evidence < {thresholds['days']}")
    if evidence["accepted_pct"] < float(thresholds["accepted_pct"]):
        unmet.append(f"{evidence['accepted_pct']}% accepted < {thresholds['accepted_pct']}%")
    if evidence["critical_errors"] > int(thresholds["critical_errors"]):
        unmet.append(f"{evidence['critical_errors']} critical errors > {thresholds['critical_errors']}")
    if thresholds.get("require_failure_tests") and not failures["ok"]:
        unmet.append(f"targeted failure tests: {failures['reason']}")
    existing = (await ctx.db.execute(select(PromotionProposal).where(PromotionProposal.workflow_key == inp.workflow_key)
                                     .order_by(PromotionProposal.created_at.desc()))).scalars().all()
    open_ = next((p for p in existing if p.status == "proposed"), None)
    if open_ is not None:
        return {"created": False, "proposal": serialize_proposal(open_), "reason": "a proposal is already awaiting the owner",
                "evidence": evidence, "unmet": unmet}
    paused = next((p for p in existing if p.status == "paused"), None)
    if paused is not None:
        ctrl = await ctx.db.get(WorkflowControl, f"workflow:{inp.workflow_key}")
        if ctrl is not None and ctrl.paused:
            return {"created": False, "proposal": serialize_proposal(paused), "reason": "workflow is paused after a regression; owner review required",
                    "evidence": evidence, "unmet": unmet}
    if unmet:
        return {"created": False, "proposal": None, "reason": "thresholds not met — keeping supervised mode", "unmet": unmet,
                "evidence": evidence, "thresholds": thresholds, "decision": "Blocked"}
    last = existing[0] if existing else None
    if last is not None and last.last_asked_at and not inp.force_ask:
        quiet_until = last.last_asked_at + timedelta(days=ASK_AGAIN_DAYS)
        if now < quiet_until:
            new_since = await workflow_evidence(ctx.db, inp.workflow_key, now=now, since=last.last_asked_at)
            if new_since["cases"] < NEW_EVIDENCE_MIN_CASES:
                return {"created": False, "proposal": serialize_proposal(last), "evidence": evidence, "unmet": [],
                        "reason": f"asked {last.status} on {last.last_asked_at.date().isoformat()}; not asking again before "
                                  f"{quiet_until.date().isoformat()} without meaningful new evidence ({new_since['cases']} new cases)"}
    action_class = "consequential"
    permission = default_permission(inp.workflow_key, inp.action_pattern, now, evidence, inp.permission_overrides)
    exclusions = list(permission["excluded_cases"])
    # retry-safe for the same evidence window and decision state; a decided proposal never blocks a later ask
    dedupe = stable_hash({"w": inp.workflow_key, "to": evidence["window_to"], "cases": evidence["cases"], "n": len(existing)})[:32]
    dup = (await ctx.db.execute(select(PromotionProposal).where(PromotionProposal.dedupe_key == dedupe))).scalars().first()
    if dup is not None:
        return {"created": False, "proposal": serialize_proposal(dup), "reason": "already proposed for this evidence window", "evidence": evidence, "unmet": []}
    p = PromotionProposal(workflow_key=inp.workflow_key, action_class=action_class, evidence=evidence, thresholds=thresholds,
                          proposed_permission=permission, exclusions=exclusions, status="proposed", last_asked_at=now,
                          asked_count=(last.asked_count + 1) if last is not None else 1,
                          window_from=datetime.fromisoformat(evidence["window_from"]) if evidence["window_from"] else None,
                          window_to=datetime.fromisoformat(evidence["window_to"]) if evidence["window_to"] else None,
                          dedupe_key=dedupe, created_by=ctx.actor.user_id)
    ctx.db.add(p)
    await ctx.db.flush()
    ctx.changed.append({"kind": "promotion_proposal", "id": p.id, "version": p.version})
    ctx.record(f"Promotion proposal for {inp.workflow_key}: {evidence['cases']} cases, {evidence['accepted_pct']}% accepted, "
               f"{evidence['critical_errors']} critical — awaiting owner", entity_kind="promotion_proposal", entity_id=p.id, kind="approval",
               state="proposed", visibility="owner", details={"thresholds": thresholds, "permission": permission["action_pattern"], "self_enabled": False})
    ctx.emit("promotion_proposal.created", aggregate_type="promotion_proposal", aggregate_id=p.id,
             payload={"workflow_key": inp.workflow_key, "status": "proposed"})
    return {"created": True, "proposal": serialize_proposal(p), "evidence": evidence, "unmet": [], "decision": "Needs review",
            "message": (f"I handled {evidence['cases']} {inp.workflow_key} cases; {evidence['accepted']} needed no substantive changes. "
                        f"May I run this specific type automatically under these limits?")}


class ProposalDecideIn(BaseModel):
    proposal_id: str
    decision: str  # approve|decline
    expected_version: int | None = None
    note: str | None = None
    permission_edits: dict = Field(default_factory=dict)  # owner edits to recipients/domains/limits/expiry before enabling


def _dec(v) -> Decimal | None:
    return None if v in (None, "") else parse_amount(v)


@command("promotion_proposals.decide", input=ProposalDecideIn, perm="permissions", action_class="owner_only", approval_kind="permission",
         summary=lambda p: f"{p.decision.title()} promotion proposal {p.proposal_id[:8]}",
         description="Owner approves (creates the exact bounded standing Permission) or declines (supervision continues).")
async def proposals_decide(ctx: CommandContext, inp: ProposalDecideIn) -> dict:
    if inp.decision not in ("approve", "decline"):
        raise ValidationFailed("decision must be approve|decline")
    p = (await ctx.db.execute(select(PromotionProposal).where(PromotionProposal.id == inp.proposal_id).with_for_update())).scalar_one_or_none()
    if p is None:
        raise NotFound("promotion proposal not found")
    if inp.expected_version is not None and p.version != inp.expected_version:
        raise Conflict("proposal changed since you loaded it", current_version=p.version)
    if p.status != "proposed":
        raise Blocked(f"proposal is {p.status}", status=p.status)
    p.decided_by = ctx.actor.user_id
    p.decided_at = ctx.now
    p.decision_note = inp.note
    p.last_asked_at = ctx.now
    permission_row = None
    if inp.decision == "decline":
        p.status = "declined"
        ctx.touch(p, "promotion_proposal")
        ctx.record(f"Declined promotion for {p.workflow_key}: supervision continues", entity_kind="promotion_proposal", entity_id=p.id,
                   kind="approval", state="declined", visibility="owner", details={"note": inp.note})
        ctx.emit("promotion_proposal.decided", aggregate_type="promotion_proposal", aggregate_id=p.id,
                 payload={"workflow_key": p.workflow_key, "status": "declined"})
        return {"proposal": serialize_proposal(p), "permission": None, "mode": "supervised"}
    if is_high_risk(p.workflow_key):
        raise Blocked("high-risk workflows cannot be enabled from a promotion proposal")
    spec = {**(p.proposed_permission or {}), **{k: v for k, v in (inp.permission_edits or {}).items() if k in (p.proposed_permission or {})}}
    from ..core.time import parse_iso
    permission_row = Permission(
        subject_kind=spec.get("subject_kind") or "workflow", subject_id=spec.get("subject_id"), workflow_key=p.workflow_key,
        action_pattern=spec["action_pattern"], allowed_records=dict(spec.get("allowed_records") or {}),
        recipients=list(spec.get("recipients") or []), domains=list(spec.get("domains") or []), fields=list(spec.get("fields") or []),
        per_action_limit=_dec(spec.get("per_action_limit")), cumulative_limit=_dec(spec.get("cumulative_limit")), currency=spec.get("currency"),
        freshness_requirements=dict(spec.get("freshness_requirements") or {}), excluded_cases=list(spec.get("excluded_cases") or []),
        rate_limit=dict(spec.get("rate_limit") or {}),
        effective_from=parse_iso(spec["effective_from"]) if spec.get("effective_from") else ctx.now,
        expires_at=parse_iso(spec["expires_at"]) if spec.get("expires_at") else None,
        authorized_by=ctx.actor.user_id, status="active", proposal_id=p.id, description=str(spec.get("description") or ""),
        created_by=ctx.actor.user_id)
    ctx.db.add(permission_row)
    await ctx.db.flush()
    p.status = "approved"
    p.permission_id = permission_row.id
    p.proposed_permission = spec
    ctx.touch(p, "promotion_proposal")
    ctx.changed.append({"kind": "permission", "id": permission_row.id, "version": permission_row.version})
    ctx.record(f"Enabled standing permission for {p.workflow_key}: {permission_row.action_pattern}", entity_kind="permission",
               entity_id=permission_row.id, kind="access", state="active", visibility="owner",
               details={"proposal_id": p.id, "rate_limit": permission_row.rate_limit, "expires_at": _iso(permission_row.expires_at)})
    ctx.emit("permission.created", aggregate_type="permission", aggregate_id=permission_row.id,
             payload={"workflow_key": p.workflow_key, "action_pattern": permission_row.action_pattern, "proposal_id": p.id})
    ctx.emit("promotion_proposal.decided", aggregate_type="promotion_proposal", aggregate_id=p.id,
             payload={"workflow_key": p.workflow_key, "status": "approved", "permission_id": permission_row.id})
    return {"proposal": serialize_proposal(p), "permission": serialize_permission(permission_row), "mode": "bounded_automatic"}


def serialize_permission(x: Permission) -> dict:
    return {"id": x.id, "version": x.version, "subject_kind": x.subject_kind, "subject_id": x.subject_id, "workflow_key": x.workflow_key,
            "action_pattern": x.action_pattern, "allowed_records": dict(x.allowed_records or {}), "recipients": list(x.recipients or []),
            "domains": list(x.domains or []), "fields": list(x.fields or []),
            "per_action_limit": str(x.per_action_limit) if x.per_action_limit is not None else None,
            "cumulative_limit": str(x.cumulative_limit) if x.cumulative_limit is not None else None, "currency": x.currency,
            "rate_limit": dict(x.rate_limit or {}), "excluded_cases": list(x.excluded_cases or []),
            "freshness_requirements": dict(x.freshness_requirements or {}), "effective_from": _iso(x.effective_from),
            "expires_at": _iso(x.expires_at), "authorized_by": x.authorized_by, "status": x.status, "proposal_id": x.proposal_id,
            "description": x.description, "used_count": x.used_count}


class RegressionIn(BaseModel):
    workflow_key: str = Field(min_length=1)
    severity: str = "critical"  # critical|major|minor
    description: str = ""
    entity_kind: str | None = None
    entity_id: str | None = None
    source_ref: str | None = None
    dedupe_key: str | None = None
    record_outcome: bool = True


@command("promotion_proposals.record_regression", input=RegressionIn, perm=None, action_class="internal",
         description="Record a regression for a workflow. A critical one pauses the workflow, its proposals and its standing permissions.")
async def proposals_record_regression(ctx: CommandContext, inp: RegressionIn) -> dict:
    if inp.severity not in ("critical", "major", "minor"):
        raise ValidationFailed("severity must be critical|major|minor")
    key = inp.dedupe_key or stable_hash({"w": inp.workflow_key, "s": inp.severity, "d": inp.description, "e": inp.entity_id, "r": inp.source_ref})[:32]
    outcome_row = None
    if inp.record_outcome:
        outcome_row, created = await record_outcome(ctx, workflow_key=inp.workflow_key, outcome="critical_error" if inp.severity == "critical" else "declined",
                                                    entity_kind=inp.entity_kind, entity_id=inp.entity_id, severity=inp.severity,
                                                    details={"description": inp.description, "regression": True}, source_ref=inp.source_ref,
                                                    dedupe_key=f"regression:{key}")
        if not created:
            return {"recorded": False, "paused": False, "outcome": serialize_outcome(outcome_row), "reason": "already recorded"}
    paused_now = False
    paused_proposals: list[str] = []
    paused_permissions: list[str] = []
    if inp.severity == "critical":
        ckey = f"workflow:{inp.workflow_key}"
        c = (await ctx.db.execute(select(WorkflowControl).where(WorkflowControl.key == ckey).with_for_update())).scalar_one_or_none()
        if c is None:
            c = WorkflowControl(key=ckey, paused=False)
            ctx.db.add(c)
        paused_now = not c.paused
        c.paused = True
        c.reason = f"critical regression: {inp.description or 'unspecified'}"
        c.changed_by = ctx.actor.user_id
        c.changed_at = ctx.now
        for p in (await ctx.db.execute(select(PromotionProposal).where(PromotionProposal.workflow_key == inp.workflow_key,
                                                                        PromotionProposal.status.in_(("proposed", "approved"))))).scalars().all():
            p.status = "paused"
            p.paused_at = ctx.now
            p.paused_reason = inp.description or "critical regression"
            p.regression = {"severity": inp.severity, "description": inp.description, "at": ctx.now.isoformat(),
                            "entity_kind": inp.entity_kind, "entity_id": inp.entity_id, "outcome_id": outcome_row.id if outcome_row else None}
            ctx.touch(p, "promotion_proposal")
            paused_proposals.append(p.id)
        for perm in (await ctx.db.execute(select(Permission).where(Permission.workflow_key == inp.workflow_key, Permission.status == "active"))).scalars().all():
            perm.status = "paused"
            ctx.touch(perm, "permission")
            paused_permissions.append(perm.id)
            ctx.emit("permission.revoked", aggregate_type="permission", aggregate_id=perm.id,
                     payload={"workflow_key": inp.workflow_key, "reason": "paused after critical regression", "paused": True})
        await ctx.db.flush()
        ctx.changed.append({"kind": "workflow_control", "id": ckey, "version": 0})
        ctx.record(f"Critical regression in {inp.workflow_key}: workflow paused, returned to review", entity_kind="workflow_control",
                   entity_id=ckey, kind="system", state="paused", exception=True,
                   details={"description": inp.description, "proposals_paused": paused_proposals, "permissions_paused": paused_permissions})
        ctx.emit("workflow.control_changed", aggregate_type="workflow_control", aggregate_id=ckey,
                 payload={"key": ckey, "paused": True, "reason": c.reason, "regression": True})
    else:
        ctx.record(f"Regression ({inp.severity}) in {inp.workflow_key}: {inp.description}", entity_kind="workflow", entity_id=inp.workflow_key,
                   kind="system", state="regression", exception=True)
    ctx.emit("workflow.regression", aggregate_type="workflow", aggregate_id=inp.workflow_key,
             payload={"severity": inp.severity, "paused": inp.severity == "critical"})
    return {"recorded": True, "paused": inp.severity == "critical", "paused_now": paused_now, "proposals_paused": paused_proposals,
            "permissions_paused": paused_permissions, "outcome": serialize_outcome(outcome_row) if outcome_row else None,
            "decision": "Blocked" if inp.severity == "critical" else "Needs review"}


# ── reads ────────────────────────────────────────────────────────────────────
async def list_proposals(db, *, status: str | None = None, workflow_key: str | None = None, limit: int = 100, offset: int = 0) -> dict:
    clauses = []
    if status:
        clauses.append(PromotionProposal.status == status)
    if workflow_key:
        clauses.append(PromotionProposal.workflow_key == workflow_key)
    total = (await db.execute(select(func.count()).select_from(PromotionProposal).where(*clauses))).scalar_one()
    rows = (await db.execute(select(PromotionProposal).where(*clauses).order_by(PromotionProposal.created_at.desc())
                             .limit(limit).offset(offset))).scalars().all()
    return {"items": [serialize_proposal(p) for p in rows], "total": int(total)}


async def get_proposal(db, proposal_id: str) -> dict:
    p = await db.get(PromotionProposal, proposal_id)
    if p is None:
        raise NotFound("promotion proposal not found")
    d = serialize_proposal(p)
    if p.permission_id:
        perm = await db.get(Permission, p.permission_id)
        d["permission"] = serialize_permission(perm) if perm else None
    ctrl = await db.get(WorkflowControl, f"workflow:{p.workflow_key}")
    d["workflow_paused"] = bool(ctrl and ctrl.paused)
    return d


async def workflow_status(db, workflow_key: str) -> dict:
    now = datetime.now(timezone.utc)
    ev = await workflow_evidence(db, workflow_key, now=now)
    ctrl = await db.get(WorkflowControl, f"workflow:{workflow_key}")
    perms = (await db.execute(select(Permission).where(Permission.workflow_key == workflow_key))).scalars().all()
    return {"workflow_key": workflow_key, "evidence": ev, "paused": bool(ctrl and ctrl.paused), "pause_reason": ctrl.reason if ctrl else None,
            "permissions": [serialize_permission(x) for x in perms], "failure_tests": await failure_tests_state(db, workflow_key),
            "high_risk": is_high_risk(workflow_key), "mode": "paused" if ctrl and ctrl.paused else
            ("bounded_automatic" if any(x.status == "active" for x in perms) else "supervised")}
