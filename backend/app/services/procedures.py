"""Procedures / Teach (spec §9.4, G14).

A procedure is a versioned spec: goal, inputs, hard_constraints, normal_path, permitted_alternatives,
evidence_of_completion, escalation, source and test_cases. Teach text is parsed deterministically; a
*demonstration* (screenshots / recording / narrated workflow) is interpreted with uncertainty — every derived
step is `inferred`, `executable: false`, and nothing from an attachment is installed as an instruction.

Promotion ladder, one step at a time and owner-only:
    proposed -> offline_tested -> shadow -> supervised -> bounded_automatic
`procedures.run_tests` replays the stored test cases against the spec's declared expectations; a failure (or a
spec that changed since the last passing run) blocks promotion. Promotion never creates a Permission: the last
step only *references* an owner-enabled standing permission that already exists (permission changes are separate
records). Rollback withdraws the version, reverts the current version, invalidates approvals bound to the withdrawn
version and emits `procedure.withdrawn` so queued work revalidates.
"""
from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy import select

from ..core.errors import Blocked, Conflict, NotFound, ValidationFailed
from ..core.ids import stable_hash
from ..domain.commands import CommandContext, command
from ..models.knowledge import Procedure, ProcedureVersion
from ..models.runtime import Approval, Permission

LADDER = ("proposed", "offline_tested", "shadow", "supervised", "bounded_automatic")
SOURCE_KINDS = ("teach_text", "correction", "demonstration", "email_example", "manual")
CONSTRAINT_RE = re.compile(r"\b(never|must not|do not|don't|always|only|must)\b", re.I)
PROHIBITION_RE = re.compile(r"\b(never|must not|do not|don't)\b", re.I)
GOAL_RE = re.compile(r"^(goal|outcome|purpose)\s*:\s*(.+)$", re.I)
ESCALATE_RE = re.compile(r"\b(escalate|ask (?:dylan|the owner|me)|stop and ask|needs? (?:owner )?approval|check with)\b", re.I)
EVIDENCE_RE = re.compile(r"\b(done when|evidence|proof|receipt|confirmation|verified when|complete when)\b", re.I)
INPUT_RE = re.compile(r"\b(?:needs?|requires?|given|input(?:s)?:|using)\s+([^.\n]+)", re.I)
STEP_RE = re.compile(r"^\s*(?:\d+[.)]|[-•*])\s*(.+)$")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def serialize_version(v: ProcedureVersion) -> dict:
    return {"id": v.id, "version": v.version, "procedure_id": v.procedure_id, "version_no": v.version_no, "stage": v.stage,
            "spec": dict(v.spec or {}), "spec_hash": v.spec_hash, "source_kind": v.source_kind, "source_ref": v.source_ref,
            "interpretation": v.interpretation, "test_results": dict(v.test_results or {}), "tests_passed_at": _iso(v.tests_passed_at),
            "promoted_at": _iso(v.promoted_at), "promoted_by": v.promoted_by, "withdrawn_at": _iso(v.withdrawn_at),
            "withdrawn_by": v.withdrawn_by, "withdrawn_reason": v.withdrawn_reason, "superseded_at": _iso(v.superseded_at),
            "permission_id": v.permission_id, "stage_history": list(v.stage_history or []), "proposed_by": v.proposed_by,
            "created_at": _iso(v.created_at)}


def redact_for(d: dict, actor) -> dict:
    """Procedure specs are internal SOPs, but the stored `source.text_excerpt` can be a raw teach message or a
    customer email example. Only an actor who may curate knowledge sees the raw source (spec §11.1)."""
    from ..domain.policy import has_perm
    if actor is None or has_perm(actor, "knowledge.write"):
        return d
    out = dict(d)
    spec = dict(out.get("spec") or {})
    if spec.get("source"):
        src = {k: v for k, v in dict(spec["source"]).items() if k != "text_excerpt"}
        src["text_excerpt"] = None
        src["excerpt_hidden"] = True
        spec["source"] = src
        out["spec"] = spec
    if "source_ref" in out:
        out["source_ref"] = None
    for key in ("versions", "current_version"):
        if isinstance(out.get(key), list):
            out[key] = [redact_for(v, actor) for v in out[key]]
        elif isinstance(out.get(key), dict):
            out[key] = redact_for(out[key], actor)
    return out


def serialize_procedure(p: Procedure, versions: list[ProcedureVersion] | None = None) -> dict:
    d = {"id": p.id, "version": p.version, "key": p.key, "title": p.title, "goal": p.goal, "status": p.status,
         "current_version_id": p.current_version_id, "workflow_key": p.workflow_key, "description": p.description,
         "stage_history": list(p.stage_history or []), "last_promoted_at": _iso(p.last_promoted_at), "withdrawn_at": _iso(p.withdrawn_at),
         "created_at": _iso(p.created_at), "updated_at": _iso(p.updated_at), "ladder": list(LADDER)}
    if versions is not None:
        d["versions"] = [serialize_version(v) for v in versions]
    return d


# ── spec building ────────────────────────────────────────────────────────────
def parse_teach_text(text: str) -> dict:
    """Deterministic parse of Teach text into spec sections. Lines are classified by cue words; ordered/bulleted
    lines become the normal path. Nothing here executes; it is a proposal the owner reviews."""
    normal_path: list[dict] = []
    constraints: list[dict] = []
    escalation: list[str] = []
    evidence: list[str] = []
    inputs: list[str] = []
    alternatives: list[str] = []
    goal: str | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        g = GOAL_RE.match(line)
        if g and goal is None:
            goal = g.group(2).strip()
            continue
        m = STEP_RE.match(line)
        body = m.group(1).strip() if m else line
        low = body.lower()
        # cue precedence: a prohibition is a hard constraint even when it also mentions confirmation/evidence words
        if PROHIBITION_RE.search(body):
            constraints.append({"text": body, "trigger": _trigger_of(body)})
        elif ESCALATE_RE.search(body):
            escalation.append(body)
        elif EVIDENCE_RE.search(body):
            evidence.append(body)
        elif re.match(r"^(if|when|otherwise|alternatively|or else)\b", low) and re.search(r"\b(instead|alternative|fallback|may|can)\b", low):
            alternatives.append(body)
        elif m:
            normal_path.append({"step": len(normal_path) + 1, "text": body})
        elif CONSTRAINT_RE.search(body):
            constraints.append({"text": body, "trigger": _trigger_of(body)})
        else:
            im = INPUT_RE.search(body)
            if im and len(body) < 160:
                inputs.append(im.group(1).strip())
            else:
                normal_path.append({"step": len(normal_path) + 1, "text": body})
    return {"goal": goal, "inputs": inputs, "hard_constraints": constraints, "normal_path": normal_path,
            "permitted_alternatives": alternatives, "evidence_of_completion": evidence, "escalation": escalation}


def _trigger_of(constraint: str) -> str | None:
    """A phrase whose presence in a test input trips the constraint (e.g. 'never promise a delivery date' -> 'delivery date')."""
    m = re.search(r"\b(?:never|must not|do not|don't)\s+(?:\w+\s+){0,2}(?:a |an |the |your |their )?([\w'\- ]{3,40}?)(?:[.,;]|$| without| unless| before)",
                  constraint, re.I)
    return m.group(1).strip().lower() if m else None


def spec_hash(spec: dict) -> str:
    core = {k: spec.get(k) for k in ("goal", "inputs", "hard_constraints", "normal_path", "permitted_alternatives",
                                     "evidence_of_completion", "escalation", "test_cases")}
    return stable_hash(core)[:32]


class ProcedureProposeIn(BaseModel):
    key: str | None = None
    title: str = Field(min_length=1, max_length=200)
    goal: str = ""
    text: str = ""  # Teach text / correction / transcript of a demonstration
    source_kind: str = "teach_text"
    source_ref: str | None = None
    workflow_key: str | None = None
    inputs: list = Field(default_factory=list)
    hard_constraints: list = Field(default_factory=list)
    normal_path: list = Field(default_factory=list)
    permitted_alternatives: list = Field(default_factory=list)
    evidence_of_completion: list = Field(default_factory=list)
    escalation: list = Field(default_factory=list)
    test_cases: list = Field(default_factory=list)
    description: str = ""
    dedupe_key: str | None = None


def build_spec(inp: ProcedureProposeIn) -> tuple[dict, str]:
    parsed = parse_teach_text(inp.text) if inp.text else {"goal": None, "inputs": [], "hard_constraints": [], "normal_path": [],
                                                           "permitted_alternatives": [], "evidence_of_completion": [], "escalation": []}
    constraints = [c if isinstance(c, dict) else {"text": str(c), "trigger": _trigger_of(str(c))} for c in inp.hard_constraints] or parsed["hard_constraints"]
    path = [s if isinstance(s, dict) else {"step": i + 1, "text": str(s)} for i, s in enumerate(inp.normal_path)] or parsed["normal_path"]
    spec = {
        "goal": inp.goal or parsed.get("goal") or inp.title,
        "inputs": list(inp.inputs) or parsed["inputs"],
        "hard_constraints": constraints,
        "normal_path": path,
        "permitted_alternatives": list(inp.permitted_alternatives) or parsed["permitted_alternatives"],
        "evidence_of_completion": list(inp.evidence_of_completion) or parsed["evidence_of_completion"],
        "escalation": list(inp.escalation) or parsed["escalation"],
        "source": {"kind": inp.source_kind, "ref": inp.source_ref, "text_excerpt": (inp.text or "")[:2000]},
        "test_cases": list(inp.test_cases),
        "executable": False,  # a spec never becomes tool authority by itself
    }
    interpretation = "literal"
    if inp.source_kind == "demonstration":
        interpretation = "uncertain"
        for s in spec["normal_path"]:
            s["confidence"] = "inferred"
            s["executable"] = False
        spec["interpretation_note"] = ("Derived from a demonstration: steps are inferred, not instructions. Review each step; "
                                       "nothing from the attachment was installed as executable behaviour.")
        spec["fact_status"] = "inferred"
    else:
        spec["fact_status"] = "reported"
    return spec, interpretation


@command("procedures.propose", input=ProcedureProposeIn, perm="knowledge.write", action_class="internal",
         description="Teach: propose a procedure version (goal, inputs, hard constraints, normal path, alternatives, "
                     "evidence of completion, escalation, source, test cases). Demonstrations are interpreted with uncertainty.")
async def procedures_propose(ctx: CommandContext, inp: ProcedureProposeIn) -> dict:
    if inp.source_kind not in SOURCE_KINDS:
        raise ValidationFailed(f"source_kind must be one of {SOURCE_KINDS}")
    key = (inp.key or re.sub(r"[^a-z0-9]+", "-", inp.title.lower()).strip("-"))[:80]
    if not key:
        raise ValidationFailed("a procedure key or title is required")
    spec, interpretation = build_spec(inp)
    if not spec["normal_path"] and not spec["hard_constraints"]:
        raise ValidationFailed("nothing to teach: give steps (text) or a normal_path")
    dedupe = inp.dedupe_key or stable_hash({"key": key, "spec": spec_hash(spec)})[:32]
    existing = (await ctx.db.execute(select(ProcedureVersion).where(ProcedureVersion.dedupe_key == dedupe))).scalars().first()
    if existing is not None:
        p = await ctx.db.get(Procedure, existing.procedure_id)
        return {"procedure": serialize_procedure(p), "version": serialize_version(existing), "created": False}
    p = (await ctx.db.execute(select(Procedure).where(Procedure.key == key).with_for_update())).scalar_one_or_none()
    created_proc = False
    if p is None:
        p = Procedure(key=key, title=inp.title.strip(), goal=spec["goal"], status="proposed", workflow_key=inp.workflow_key,
                      description=inp.description, created_by=ctx.actor.user_id)
        ctx.db.add(p)
        await ctx.db.flush()
        created_proc = True
    else:
        if inp.workflow_key and not p.workflow_key:
            p.workflow_key = inp.workflow_key
        if inp.description:
            p.description = inp.description
    last_no = (await ctx.db.execute(select(ProcedureVersion.version_no).where(ProcedureVersion.procedure_id == p.id)
                                    .order_by(ProcedureVersion.version_no.desc()).limit(1))).scalar_one_or_none() or 0
    v = ProcedureVersion(procedure_id=p.id, version_no=last_no + 1, spec=spec, spec_hash=spec_hash(spec), source_kind=inp.source_kind,
                         source_ref=inp.source_ref, stage="proposed", interpretation=interpretation, dedupe_key=dedupe,
                         proposed_by=ctx.actor.user_id, stage_history=[{"from": None, "to": "proposed", "at": ctx.now.isoformat(),
                                                                         "by": ctx.actor.user_id}], created_by=ctx.actor.user_id)
    ctx.db.add(v)
    await ctx.db.flush()
    if created_proc:
        ctx.changed.append({"kind": "procedure", "id": p.id, "version": p.version})
    else:
        ctx.touch(p, "procedure")
    ctx.changed.append({"kind": "procedure_version", "id": v.id, "version": v.version})
    ctx.record(f"Proposed procedure {p.key} v{v.version_no}" + (" (from demonstration, uncertain)" if interpretation == "uncertain" else ""),
               entity_kind="procedure", entity_id=p.id, kind="system", state="proposed",
               details={"version_id": v.id, "source_kind": inp.source_kind, "interpretation": interpretation, "test_cases": len(spec["test_cases"])})
    ctx.emit("procedure.proposed", aggregate_type="procedure", aggregate_id=p.id, aggregate_version=p.version,
             payload={"version_id": v.id, "version_no": v.version_no, "interpretation": interpretation})
    return {"procedure": serialize_procedure(p), "version": serialize_version(v), "created": True,
            "decision": "Needs review", "note": "Proposed only. Tests must pass and the owner promotes one step at a time; no permission changes."}


# ── deterministic replay ─────────────────────────────────────────────────────
def replay_case(spec: dict, case: dict) -> dict:
    """Replay one stored test case against the spec's declared behaviour (no model, no side effects).

    case = {name, input: {text?, fields?: {...}}, expect: {outcome: complete|escalate|needs_information,
            required_evidence?: [..], forbidden_phrases?: [..], must_include_steps?: [..], escalate_on?: str}}
    Derived outcome: a hard-constraint trigger phrase in the input text -> escalate; a required input missing from
    fields -> needs_information; otherwise complete with the spec's evidence_of_completion.
    """
    inp = case.get("input") or {}
    text = str(inp.get("text") or "").lower()
    fields = inp.get("fields") or {}
    expect = case.get("expect") or {}
    reasons: list[str] = []
    tripped = [c for c in spec.get("hard_constraints", []) if c.get("trigger") and c["trigger"] in text]
    missing = [i for i in spec.get("inputs", []) if isinstance(i, str) and i and i.lower() not in {str(k).lower() for k in fields}
               and expect.get("outcome") != "complete" and fields]
    if tripped:
        outcome = "escalate"
    elif fields and missing and expect.get("outcome") == "needs_information":
        outcome = "needs_information"
    else:
        outcome = "complete"
    ok = True
    if "outcome" in expect and expect["outcome"] != outcome:
        ok = False
        reasons.append(f"expected outcome {expect['outcome']}, replay gives {outcome}"
                       + (f" (constraint tripped: {tripped[0]['text']})" if tripped else ""))
    for ph in expect.get("forbidden_phrases", []) or []:
        if any(str(ph).lower() in str(s.get("text", "")).lower() for s in spec.get("normal_path", [])):
            ok = False
            reasons.append(f"normal path contains forbidden phrase {ph!r}")
    for st in expect.get("must_include_steps", []) or []:
        if not any(str(st).lower() in str(s.get("text", "")).lower() for s in spec.get("normal_path", [])):
            ok = False
            reasons.append(f"normal path lacks step {st!r}")
    for ev in expect.get("required_evidence", []) or []:
        if not any(str(ev).lower() in str(e).lower() for e in spec.get("evidence_of_completion", [])):
            ok = False
            reasons.append(f"evidence of completion lacks {ev!r}")
    if expect.get("escalate_on") and not any(str(expect["escalate_on"]).lower() in str(e).lower() for e in spec.get("escalation", [])):
        ok = False
        reasons.append(f"escalation lacks {expect['escalate_on']!r}")
    return {"name": case.get("name") or "case", "ok": ok, "outcome": outcome, "reasons": reasons,
            "tripped": [c["text"] for c in tripped]}


def run_replay(spec: dict, now: datetime) -> dict:
    cases = spec.get("test_cases") or []
    if not cases:
        return {"passed": 0, "failed": 0, "total": 0, "ok": False, "cases": [], "at": now.isoformat(),
                "reason": "no test cases recorded — promotion needs at least one", "spec_hash": spec_hash(spec)}
    results = [replay_case(spec, c) for c in cases]
    passed = sum(1 for r in results if r["ok"])
    return {"passed": passed, "failed": len(results) - passed, "total": len(results), "ok": passed == len(results),
            "cases": results, "at": now.isoformat(), "spec_hash": spec_hash(spec)}


class VersionRef(BaseModel):
    version_id: str
    expected_version: int | None = None
    reason: str | None = None
    permission_id: str | None = None  # bounded_automatic only: an existing owner-enabled standing permission


async def _get_version(ctx: CommandContext, version_id: str, expected_version: int | None) -> tuple[Procedure, ProcedureVersion]:
    v = (await ctx.db.execute(select(ProcedureVersion).where(ProcedureVersion.id == version_id).with_for_update())).scalar_one_or_none()
    if v is None:
        raise NotFound("procedure version not found")
    if expected_version is not None and v.version != expected_version:
        raise Conflict("procedure version changed since you loaded it", current_version=v.version)
    p = (await ctx.db.execute(select(Procedure).where(Procedure.id == v.procedure_id).with_for_update())).scalar_one()
    return p, v


@command("procedures.run_tests", input=VersionRef, perm="knowledge.write", action_class="internal",
         description="Deterministic replay of the version's stored test cases against its spec. A failure blocks promotion.")
async def procedures_run_tests(ctx: CommandContext, inp: VersionRef) -> dict:
    p, v = await _get_version(ctx, inp.version_id, inp.expected_version)
    if v.withdrawn_at:
        raise Blocked("version is withdrawn", stage=v.stage)
    res = run_replay(v.spec or {}, ctx.now)
    v.test_results = res
    v.tests_passed_at = ctx.now if res["ok"] else None
    ctx.touch(v, "procedure_version")
    ctx.record(f"Procedure tests {'passed' if res['ok'] else 'FAILED'}: {p.key} v{v.version_no} ({res['passed']}/{res['total']})",
               entity_kind="procedure", entity_id=p.id, kind="system", state="tests_passed" if res["ok"] else "tests_failed",
               exception=not res["ok"], details={"version_id": v.id, "failed": [c for c in res["cases"] if not c["ok"]], "reason": res.get("reason")})
    ctx.emit("procedure.tests_run", aggregate_type="procedure", aggregate_id=p.id, payload={"version_id": v.id, "ok": res["ok"]})
    return {"version": serialize_version(v), "results": res, "decision": "Allowed" if res["ok"] else "Blocked"}


def _tests_current(v: ProcedureVersion) -> tuple[bool, str | None]:
    tr = v.test_results or {}
    if not tr:
        return False, "tests have not been run for this version"
    if not tr.get("ok"):
        return False, tr.get("reason") or f"{tr.get('failed', 0)} test case(s) failed"
    if tr.get("spec_hash") != (v.spec_hash or spec_hash(v.spec or {})):
        return False, "spec changed since the last passing test run"
    return True, None


@command("procedures.promote", input=VersionRef, perm="knowledge.write", action_class="owner_only", approval_kind="other",
         summary=lambda p: f"Promote procedure version {p.version_id[:8]} one step",
         description="Owner moves a version one step up the ladder (proposed→offline_tested→shadow→supervised→bounded_automatic). "
                     "Tests must pass. Never creates a Permission; bounded_automatic only references an existing one.")
async def procedures_promote(ctx: CommandContext, inp: VersionRef) -> dict:
    p, v = await _get_version(ctx, inp.version_id, inp.expected_version)
    if v.withdrawn_at:
        raise Blocked("version is withdrawn; propose a new version instead", stage=v.stage)
    idx = LADDER.index(v.stage) if v.stage in LADDER else -1
    if idx < 0 or idx >= len(LADDER) - 1:
        raise Blocked(f"version is already {v.stage}", stage=v.stage)
    ok, why = _tests_current(v)
    if not ok:
        raise Blocked(f"promotion blocked: {why}", stage=v.stage, test_results=v.test_results or {})
    target = LADDER[idx + 1]
    if target == "bounded_automatic":
        if v.interpretation == "uncertain":
            raise Blocked("a version derived from a demonstration cannot become automatic until re-taught explicitly", stage=v.stage)
        if not inp.permission_id:
            raise Blocked("bounded automatic needs an owner-enabled standing permission; promotion does not create one",
                          stage=v.stage, needs="permission_id")
        perm = await ctx.db.get(Permission, inp.permission_id)
        if perm is None or perm.status != "active":
            raise Blocked("referenced standing permission is not active", permission_id=inp.permission_id)
        if p.workflow_key and perm.workflow_key and perm.workflow_key != p.workflow_key:
            raise Blocked("standing permission is for a different workflow", permission_id=inp.permission_id)
        v.permission_id = perm.id
    prev_stage = v.stage
    v.stage = target
    v.promoted_at = ctx.now
    v.promoted_by = ctx.actor.user_id
    v.stage_history = list(v.stage_history or []) + [{"from": prev_stage, "to": target, "at": ctx.now.isoformat(), "by": ctx.actor.user_id}]
    ctx.touch(v, "procedure_version")
    # The promoted version becomes current only when it reaches at least the live version's rung: climbing a fresh
    # v2 to `offline_tested` must never quietly replace a v1 already running in shadow/supervised (spec §9.4).
    cur = await ctx.db.get(ProcedureVersion, p.current_version_id) if p.current_version_id and p.current_version_id != v.id else None
    cur_idx = LADDER.index(cur.stage) if cur is not None and cur.withdrawn_at is None and cur.stage in LADDER else -1
    becomes_current = cur is None or (idx + 1) >= cur_idx
    if becomes_current:
        if cur is not None and cur.superseded_at is None:
            cur.superseded_at = ctx.now
        p.current_version_id = v.id
        p.status = target
    p.last_promoted_at = ctx.now
    p.stage_history = list(p.stage_history or []) + [{"version_id": v.id, "from": prev_stage, "to": target, "at": ctx.now.isoformat(),
                                                      "by": ctx.actor.user_id, "became_current": becomes_current}]
    ctx.touch(p, "procedure")
    ctx.record(f"Promoted procedure {p.key} v{v.version_no}: {prev_stage} → {target}"
               + ("" if becomes_current else f" (v{cur.version_no} stays current at {cur.stage})"),
               entity_kind="procedure", entity_id=p.id, kind="system", state=target,
               details={"version_id": v.id, "permission_id": v.permission_id, "permission_created": False,
                        "became_current": becomes_current, "current_version_id": p.current_version_id})
    ctx.emit("procedure.promoted", aggregate_type="procedure", aggregate_id=p.id, aggregate_version=p.version,
             payload={"version_id": v.id, "from": prev_stage, "to": target, "permission_created": False,
                      "became_current": becomes_current})
    return {"procedure": serialize_procedure(p), "version": serialize_version(v), "permission_created": False,
            "became_current": becomes_current}


async def _invalidate_bound_approvals(ctx: CommandContext, version_id: str) -> int:
    """Approvals bound to this procedure version (record_versions.procedure_version) must be reviewed again.
    Contract for other domains: bind `record_versions={"procedure_version": <version id>}` when a draft/action
    was prepared under a procedure version."""
    from .approvals import ApprovalRef, invalidate
    rows = (await ctx.db.execute(select(Approval).where(Approval.status.in_(("pending", "approved", "queued")),
                                                        Approval.record_versions["procedure_version"].as_string() == version_id))).scalars().all()
    for a in rows:
        await invalidate(ctx.child(), ApprovalRef(approval_id=a.id, reason="Procedure version withdrawn — review again"))
    return len(rows)


@command("procedures.rollback", input=VersionRef, perm="knowledge.write", action_class="owner_only", approval_kind="other",
         summary=lambda p: f"Withdraw procedure version {p.version_id[:8]}",
         description="Withdraw a version: history is kept, new runs stop on it, the current version reverts and queued work revalidates.")
async def procedures_rollback(ctx: CommandContext, inp: VersionRef) -> dict:
    p, v = await _get_version(ctx, inp.version_id, inp.expected_version)
    if v.withdrawn_at:
        raise Blocked("version is already withdrawn", stage=v.stage)
    prev_stage = v.stage
    v.withdrawn_at = ctx.now
    v.withdrawn_by = ctx.actor.user_id
    v.withdrawn_reason = inp.reason or "rolled back by owner"
    v.stage = "withdrawn"
    v.permission_id = None
    v.stage_history = list(v.stage_history or []) + [{"from": prev_stage, "to": "withdrawn", "at": ctx.now.isoformat(),
                                                      "by": ctx.actor.user_id, "reason": v.withdrawn_reason}]
    ctx.touch(v, "procedure_version")
    reverted_to = None
    if p.current_version_id == v.id:
        candidates = (await ctx.db.execute(select(ProcedureVersion).where(ProcedureVersion.procedure_id == p.id, ProcedureVersion.id != v.id,
                                                                           ProcedureVersion.withdrawn_at.is_(None),
                                                                           ProcedureVersion.promoted_at.is_not(None))
                                           .order_by(ProcedureVersion.version_no.desc()))).scalars().all()
        prev = candidates[0] if candidates else None
        if prev is not None:
            prev.superseded_at = None
            p.current_version_id = prev.id
            p.status = prev.stage
            reverted_to = prev.id
        else:
            p.current_version_id = None
            p.status = "withdrawn"
            p.withdrawn_at = ctx.now
    p.stage_history = list(p.stage_history or []) + [{"version_id": v.id, "from": prev_stage, "to": "withdrawn", "at": ctx.now.isoformat(),
                                                      "by": ctx.actor.user_id, "reverted_to": reverted_to}]
    ctx.touch(p, "procedure")
    invalidated = await _invalidate_bound_approvals(ctx, v.id)
    ctx.record(f"Withdrew procedure {p.key} v{v.version_no}" + (f" — {inp.reason}" if inp.reason else ""), entity_kind="procedure",
               entity_id=p.id, kind="system", state="withdrawn", exception=True,
               details={"version_id": v.id, "reverted_to": reverted_to, "approvals_invalidated": invalidated})
    ctx.emit("procedure.withdrawn", aggregate_type="procedure", aggregate_id=p.id, aggregate_version=p.version,
             payload={"version_id": v.id, "version_no": v.version_no, "reverted_to": reverted_to, "revalidate": True,
                      "reason": v.withdrawn_reason})
    return {"procedure": serialize_procedure(p), "version": serialize_version(v), "reverted_to": reverted_to,
            "approvals_invalidated": invalidated}


# ── reads ────────────────────────────────────────────────────────────────────
async def list_procedures(db, *, status: str | None = None, workflow_key: str | None = None, limit: int = 100, offset: int = 0) -> dict:
    from sqlalchemy import func
    clauses = []
    if status:
        clauses.append(Procedure.status == status)
    if workflow_key:
        clauses.append(Procedure.workflow_key == workflow_key)
    total = (await db.execute(select(func.count()).select_from(Procedure).where(*clauses))).scalar_one()
    rows = (await db.execute(select(Procedure).where(*clauses).order_by(Procedure.updated_at.desc()).limit(limit).offset(offset))).scalars().all()
    items = []
    for p in rows:
        d = serialize_procedure(p)
        cur = await db.get(ProcedureVersion, p.current_version_id) if p.current_version_id else None
        d["current_version"] = serialize_version(cur) if cur else None
        items.append(d)
    return {"items": items, "total": int(total)}


async def get_procedure(db, procedure_id: str) -> dict:
    p = await db.get(Procedure, procedure_id)
    if p is None:
        p = (await db.execute(select(Procedure).where(Procedure.key == procedure_id))).scalar_one_or_none()
    if p is None:
        raise NotFound("procedure not found")
    versions = (await db.execute(select(ProcedureVersion).where(ProcedureVersion.procedure_id == p.id)
                                 .order_by(ProcedureVersion.version_no.desc()))).scalars().all()
    return serialize_procedure(p, list(versions))


async def current_version_for(db, workflow_key: str) -> ProcedureVersion | None:
    """Used by runtime callers: the version new runs should bind to (None when withdrawn/absent)."""
    p = (await db.execute(select(Procedure).where(Procedure.workflow_key == workflow_key, Procedure.current_version_id.is_not(None)))).scalars().first()
    if p is None:
        return None
    v = await db.get(ProcedureVersion, p.current_version_id)
    return v if v is not None and v.withdrawn_at is None else None
