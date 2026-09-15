"""Shop board and physical evidence (spec §8.4, invariant 10; acceptance F1, G09, G10).

Needs inspection → In recon → Finalization → Ready for sale. One command (`shop.move_stage`) validates drag,
button, keyboard and agent requests. Unmet gates keep the stage, create linked missing-work tasks (deduped) and
return a structured **Blocked** decision. Factual gates are never turned green by an override.

Parts keep physical state (requested → approved/ordered → arrived → installed → verified, with cancelled /
returned branches) separate from payment state: "paid" never sets arrived or installed.
`shop.order_part` is consequential: it persists an ExternalAction intent (provider "manual") and the
registered executor only records the order; nothing is purchased inline.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import Blocked, Conflict, NotFound, ValidationFailed
from ..core.ids import human_ref, sha256_hex
from ..core.money import parse_amount, quantize
from ..domain.access import visible_vehicle_ids
from ..domain.actors import Actor
from ..domain.commands import CommandContext, command, dispatch
from ..models.assets import Asset, AssetLink
from ..models.tasks import Task
from ..models.vehicles import RECON_STATES, Part, ReconIssue, Vehicle, WorkOrder
from . import approvals as approvals_svc
from .vehicles import (STATE_LABELS, _state_event, apply_milestone, aware, get_vehicle, iso, norm_text, recompute,
                       serialize_issue, serialize_list_item, serialize_part, serialize_vehicle, serialize_work_order)

BOARD_COLUMNS = tuple(RECON_STATES)  # fixed four columns
ACTIVE_TASK = ("open", "in_progress", "blocked", "waiting", "awaiting_verification")
OPEN_ISSUE = ("open", "in_progress")
PART_PHYSICAL = ("requested", "approved", "ordered", "arrived", "installed", "verified", "cancelled", "returned")
PART_UNFINISHED = ("requested", "approved", "ordered", "arrived", "installed")
RECON_TASK_EVIDENCE = [{"kind": "photo", "label": "Photo of the completed work", "min": 1}]
GATE_TASK_TITLES = {
    "inspection_logged": "Log inspection findings",
    "photos_min": "Take required vehicle photos",
    "recon_verified": "Complete and verify open recon work",
    "disclosures_written": "Write disclosures for listing",
    "docs_complete": "Complete vehicle documents",
    "issues_resolved": "Resolve or defer open recon issues",
}
# Domain defaults used when the settings domain has no rule at all for a stage (spec §8.4).
SHOP_DEFAULT_GATES: dict[str, list[dict]] = {
    "in_recon": [{"requirement": "inspection_logged", "label": "Inspection logged", "param": {}, "overridable": False}],
    "finalization": [{"requirement": "issues_resolved", "label": "Open recon issues resolved or explicitly deferred", "param": {}, "overridable": False}],
    "ready_for_sale": [
        {"requirement": "photos_min", "label": "At least 6 photos", "param": {"min": 6}, "overridable": False},
        {"requirement": "recon_verified", "label": "Recon work verified by owner", "param": {}, "overridable": False},
        {"requirement": "disclosures_written", "label": "Disclosures written", "param": {}, "overridable": False},
    ],
}
FACTUAL = {"photos_min", "recon_verified", "inspection_logged", "docs_complete", "issues_resolved"}


# ── gates ────────────────────────────────────────────────────────────────────
async def rules_for(db: AsyncSession, to_state: str) -> list[dict]:
    rules: list[dict] = []
    try:
        from .settings_store import effective_gate_rules
        rules = [r for r in await effective_gate_rules(db, to_state) if r.get("active", True)]
    except Exception:  # noqa: BLE001 - settings domain unavailable: domain defaults apply
        rules = []
    if not rules:
        rules = [{**d, "to_state": to_state, "source": "shop_default", "factual": d["requirement"] in FACTUAL}
                 for d in SHOP_DEFAULT_GATES.get(to_state, [])]
    for r in rules:
        r["factual"] = r.get("requirement") in FACTUAL
        if r["factual"]:
            r["overridable"] = False  # invariant 10: factual gates are never overridable
    return rules


async def photo_count(db: AsyncSession, vehicle_id: str) -> tuple[int, set[str]]:
    rows = (await db.execute(select(AssetLink, Asset).join(Asset, Asset.id == AssetLink.asset_id).where(
        AssetLink.entity_kind == "vehicle", AssetLink.entity_id == vehicle_id, AssetLink.removed_at.is_(None),
        AssetLink.role.in_(("photo", "gallery")), Asset.kind == "photo", Asset.status == "ready"))).all()
    return len(rows), {l.slot for l, _ in rows if l.slot}


async def recon_status(db: AsyncSession, vehicle_id: str) -> dict:
    issues = (await db.execute(select(ReconIssue).where(ReconIssue.vehicle_id == vehicle_id))).scalars().all()
    parts = (await db.execute(select(Part).where(Part.vehicle_id == vehicle_id))).scalars().all()
    tasks = (await db.execute(select(Task).where(Task.vehicle_id == vehicle_id, Task.status.in_(ACTIVE_TASK)))).scalars().all()
    open_issues = [i for i in issues if i.status in OPEN_ISSUE]
    deferred = [i for i in issues if i.status == "deferred"]
    unfinished_parts = [p for p in parts if p.state in PART_UNFINISHED]
    issue_ids = {i.id for i in issues}
    recon_tasks = [t for t in tasks if (t.recon_issue_id and t.recon_issue_id in issue_ids) or (t.extra or {}).get("recon")
                   or t.gate_requirement == "recon_verified"]
    return {"open_issues": open_issues, "deferred_issues": deferred, "unfinished_parts": unfinished_parts,
            "open_recon_tasks": recon_tasks, "issues": issues, "parts": parts}


async def evaluate_gate(db: AsyncSession, v: Vehicle, rule: dict) -> dict:
    req = rule["requirement"]
    ok, detail, items = True, "", []
    if req == "inspection_logged":
        ok = v.inspected_at is not None
        detail = "Inspection logged" if ok else "No inspection logged yet"
    elif req == "photos_min":
        need = int((rule.get("param") or {}).get("min", 6))
        n, slots = await photo_count(db, v.id)
        missing = [s for s in (v.photo_requirements or []) if s not in slots]
        ok = n >= need and not missing
        detail = f"{n} of {need} photos" + (f"; missing slots: {', '.join(missing)}" if missing else "")
        items = missing
    elif req == "recon_verified":
        st = await recon_status(db, v.id)
        for i in st["open_issues"]:
            items.append(f"issue open: {i.title}")
        for p in st["unfinished_parts"]:
            items.append(f"part {p.state}: {p.name}" + (" (paid, not installed)" if p.payment_state == "paid" and p.state != "installed" else ""))
        for t in st["open_recon_tasks"]:
            items.append(f"task {t.status}: {t.title}")
        ok = not items
        detail = "All recon work verified" if ok else "; ".join(items)
    elif req == "issues_resolved":
        st = await recon_status(db, v.id)
        items = [f"issue open: {i.title}" for i in st["open_issues"]]
        ok = not items
        detail = "No open recon issues" if ok else "; ".join(items)
    elif req == "disclosures_written":
        ok = bool(v.disclosures)
        detail = f"{len(v.disclosures or [])} disclosure(s)" if ok else "No disclosures written"
    elif req == "docs_complete":
        ok = v.documents_state == "complete"
        detail = STATE_LABELS.get(v.documents_state, v.documents_state)
    else:
        ok, detail = False, f"unknown gate requirement {req}"
    return {"requirement": req, "label": rule.get("label") or req, "to_state": rule.get("to_state"), "ok": ok,
            "detail": detail, "items": items, "overridable": bool(rule.get("overridable")) and req not in FACTUAL,
            "factual": req in FACTUAL, "param": rule.get("param") or {}, "source": rule.get("source", "persisted")}


async def evaluate_gates(db: AsyncSession, v: Vehicle, to_state: str, from_state: str | None = None) -> list[dict]:
    """Every rule for each stage strictly after `from_state` up to and including `to_state`."""
    from_state = from_state or v.recon_state
    i0, i1 = RECON_STATES.index(from_state), RECON_STATES.index(to_state)
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for state in RECON_STATES[i0 + 1:i1 + 1]:
        for rule in await rules_for(db, state):
            key = (state, rule["requirement"])
            if key in seen:
                continue
            seen.add(key)
            out.append(await evaluate_gate(db, v, {**rule, "to_state": state}))
    return out


async def _gate_task(ctx: CommandContext, v: Vehicle, gate: dict) -> dict:
    title = GATE_TASK_TITLES.get(gate["requirement"], f"Meet gate: {gate['label']}")
    res = await dispatch(ctx.child(), "tasks.create", {
        "title": title, "type": "operational", "vehicle_id": v.id, "notes": gate["label"],
        "gate_requirement": gate["requirement"], "source_kind": "gate",
        "source_id": f"gate:{v.id}:{gate['to_state']}:{gate['requirement']}", "dedupe": True,
        "extra": {"gate": gate["requirement"], "to_state": gate["to_state"], "detail": gate["detail"]}}, commit=False)
    return res.data or {}


# ── inputs ───────────────────────────────────────────────────────────────────
class MoveStageIn(BaseModel):
    vehicle_id: str
    to_state: str
    reason: str | None = None
    expected_version: int | None = None
    overrides: dict[str, str] = Field(default_factory=dict)  # requirement -> reason (overridable gates only)
    source: str = "button"  # button|drag|keyboard|agent|api (informational; identical rules)


class FindingIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    detail: str = ""
    severity: str = "normal"  # info|normal|major|safety
    asset_ids: list[str] = Field(default_factory=list)
    disclosure_required: bool = False
    task_title: str | None = None
    assignee_user_id: str | None = None
    priority: str = "normal"
    create_task: bool = True


class InspectionIn(BaseModel):
    vehicle_id: str
    findings: list[FindingIn] = Field(default_factory=list)
    note: str = ""
    asset_ids: list[str] = Field(default_factory=list)
    inspected_at: datetime | None = None
    timezone: str | None = None
    expected_version: int | None = None


class IssueCreateIn(BaseModel):
    vehicle_id: str
    title: str = Field(min_length=1, max_length=200)
    detail: str = ""
    severity: str = "normal"
    source_kind: str = "owner_reported"  # owner_reported|image_observed|inspection|proposed_check|customer
    source_ref: str | None = None
    intake_observation_id: str | None = None
    asset_ids: list[str] = Field(default_factory=list)
    disclosure_required: bool = False
    create_task: bool = True
    task_title: str | None = None
    assignee_user_id: str | None = None
    priority: str = "normal"
    task_source_kind: str | None = None
    task_source_id: str | None = None
    evidence_required: list | None = None


class IssueRefIn(BaseModel):
    issue_id: str
    vehicle_id: str
    note: str | None = None
    asset_ids: list[str] = Field(default_factory=list)
    expected_version: int | None = None


class IssueDeferIn(BaseModel):
    issue_id: str
    vehicle_id: str
    reason: str = Field(min_length=1)
    expected_version: int | None = None


class WorkOrderIn(BaseModel):
    vehicle_id: str
    title: str = Field(min_length=1, max_length=200)
    recon_issue_id: str | None = None
    assignee_user_id: str | None = None
    notes: str = ""


class PartRequestIn(BaseModel):
    vehicle_id: str
    name: str = Field(min_length=1, max_length=200)
    part_no: str | None = None
    quantity: int = Field(default=1, ge=1)
    work_order_id: str | None = None
    vendor_contact_id: str | None = None
    notes: str = ""
    request_key: str | None = None


class PartOrderIn(BaseModel):
    part_id: str
    vehicle_id: str
    part_name: str | None = None
    vendor_contact_id: str | None = None
    amount: str | None = None
    currency: str = "USD"
    order_ref: str | None = None
    note: str | None = None


class PartRefIn(BaseModel):
    part_id: str
    vehicle_id: str
    note: str | None = None
    asset_ids: list[str] = Field(default_factory=list)
    expected_version: int | None = None


class PartPaymentIn(BaseModel):
    part_id: str
    vehicle_id: str
    cost_item_id: str | None = None
    paid_at: datetime | None = None
    note: str | None = None
    expected_version: int | None = None


class PartCancelIn(BaseModel):
    part_id: str
    vehicle_id: str
    reason: str = Field(min_length=1)
    returned: bool = False
    expected_version: int | None = None


# ── helpers ──────────────────────────────────────────────────────────────────
async def _issue(ctx: CommandContext, issue_id: str, vehicle_id: str, expected_version: int | None = None) -> ReconIssue:
    i = (await ctx.db.execute(select(ReconIssue).where(ReconIssue.id == issue_id).with_for_update())).scalar_one_or_none()
    if i is None or i.vehicle_id != vehicle_id:
        raise NotFound("recon issue not found")
    if expected_version is not None and i.version != expected_version:
        raise Conflict("issue changed since you loaded it", current_version=i.version)
    return i


async def _part(ctx: CommandContext, part_id: str, vehicle_id: str, expected_version: int | None = None) -> Part:
    p = (await ctx.db.execute(select(Part).where(Part.id == part_id).with_for_update())).scalar_one_or_none()
    if p is None or p.vehicle_id != vehicle_id:
        raise NotFound("part not found")
    if expected_version is not None and p.version != expected_version:
        raise Conflict("part changed since you loaded it", current_version=p.version)
    return p


def _part_hist(ctx: CommandContext, p: Part, to: str, note: str | None = None) -> None:
    p.history = list(p.history or []) + [{"from": p.state, "to": to, "at": ctx.now.isoformat(), "by": ctx.actor.user_id, "note": note}]
    p.state = to


async def _ready_assets(db: AsyncSession, asset_ids: list[str]) -> list[Asset]:
    if not asset_ids:
        return []
    rows = (await db.execute(select(Asset).where(Asset.id.in_(asset_ids)))).scalars().all()
    found = {a.id: a for a in rows}
    missing = [a for a in asset_ids if a not in found or found[a].status != "ready"]
    if missing:
        raise Blocked("evidence upload not complete", missing_assets=missing)
    return [found[a] for a in asset_ids]


def issue_dedupe_key(vehicle_id: str, title: str) -> str:
    return sha256_hex(f"issue|{vehicle_id}|{norm_text(title)}")[:32]


async def _create_issue(ctx: CommandContext, v: Vehicle, inp: IssueCreateIn) -> dict:
    key = issue_dedupe_key(v.id, inp.title)
    existing = (await ctx.db.execute(select(ReconIssue).where(ReconIssue.dedupe_key == key, ReconIssue.status.in_((*OPEN_ISSUE, "deferred"))))).scalars().first()
    created = existing is None
    if existing is not None:
        i = existing
        changed = False
        ids = list(i.asset_ids or [])
        added_assets = [a for a in inp.asset_ids if a not in ids]
        if added_assets:
            i.asset_ids = [*ids, *added_assets]
            changed = True
        if inp.detail and inp.detail not in (i.detail or ""):
            i.detail = (i.detail + "\n" if i.detail else "") + inp.detail
            changed = True
        if inp.intake_observation_id and not i.intake_observation_id:
            i.intake_observation_id = inp.intake_observation_id
            changed = True
        if changed:
            ctx.touch(i, "recon_issue")
            ctx.record(f"Recon issue re-reported: {i.title} (merged, still {i.status})", entity_kind="vehicle", entity_id=v.id,
                       kind="task", state=i.status, exception=(i.status == "deferred"),
                       details={"issue_id": i.id, "source_kind": inp.source_kind, "asset_ids": list(i.asset_ids or []),
                                "observation_id": inp.intake_observation_id, "merged": True})
            if added_assets:
                ctx.emit("evidence.saved", aggregate_type="recon_issue", aggregate_id=i.id,
                         payload={"vehicle_id": v.id, "asset_ids": added_assets, "context": "recon_issue"})
    else:
        i = ReconIssue(vehicle_id=v.id, title=inp.title.strip(), detail=inp.detail, severity=inp.severity, status="open",
                       source_kind=inp.source_kind, source_ref=inp.source_ref, intake_observation_id=inp.intake_observation_id,
                       asset_ids=list(dict.fromkeys(inp.asset_ids)), dedupe_key=key, disclosure_required=inp.disclosure_required,
                       created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
        ctx.db.add(i)
        await ctx.db.flush()
        ctx.changed.append({"kind": "recon_issue", "id": i.id, "version": i.version})
        ctx.record(f"Recon issue: {i.title} ({i.source_kind})", entity_kind="vehicle", entity_id=v.id, kind="task", state="open",
                   details={"issue_id": i.id, "severity": i.severity, "asset_ids": i.asset_ids, "observation_id": inp.intake_observation_id})
        if i.asset_ids:
            ctx.emit("evidence.saved", aggregate_type="recon_issue", aggregate_id=i.id,
                     payload={"vehicle_id": v.id, "asset_ids": i.asset_ids, "context": "recon_issue"})
    task = None
    if inp.create_task:
        title = (inp.task_title or "").strip() or f"Repair {inp.title.strip()}"
        res = await dispatch(ctx.child(), "tasks.create", {
            "title": title, "type": "operational", "vehicle_id": v.id, "recon_issue_id": i.id, "owner_user_id": inp.assignee_user_id,
            "priority": inp.priority, "notes": inp.detail or "", "evidence_required": inp.evidence_required if inp.evidence_required is not None else RECON_TASK_EVIDENCE,
            "source_kind": inp.task_source_kind or inp.source_kind, "source_id": inp.task_source_id or f"issue:{i.id}", "dedupe": True,
            "extra": {"recon": True, "issue_id": i.id}}, commit=False)
        task = res.data or {}
        tid = (task.get("task") or {}).get("id")
        if tid and i.task_id != tid:
            i.task_id = tid
    return {"issue": serialize_issue(i), "created": created, "task": task}


# ── commands ─────────────────────────────────────────────────────────────────
@command("shop.move_stage", input=MoveStageIn, perm="vehicles.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)],
         description="Move a vehicle on the shop board. Same rules for drag, button, keyboard and agent: forward moves must "
                     "pass the configured gates (unmet gates create missing-work tasks and return Blocked); backward moves need a reason.")
async def shop_move_stage(ctx: CommandContext, inp: MoveStageIn) -> dict:
    if inp.to_state not in RECON_STATES:
        raise ValidationFailed(f"to_state must be one of {RECON_STATES}")
    v = await get_vehicle(ctx.db, inp.vehicle_id, expected_version=inp.expected_version)
    if v.archived_at:
        raise Blocked("vehicle is archived")
    frm = v.recon_state
    if frm == inp.to_state:
        return {"decision": "Allowed", "moved": False, "from": frm, "to": inp.to_state, "gates": [], "tasks": [],
                "reasons": ["already in that stage"], "vehicle": serialize_vehicle(v)}
    backward = RECON_STATES.index(inp.to_state) < RECON_STATES.index(frm)
    if backward:
        if not (inp.reason or "").strip():
            raise ValidationFailed("a backward move needs a reason")
        v.recon_state = inp.to_state
        _state_event(ctx, v, "recon", frm, inp.to_state, inp.reason, backward=True)
        ctx.touch(v, "vehicle")
        ctx.record(f"Moved back {v.stock_no}: {STATE_LABELS[frm]} → {STATE_LABELS[inp.to_state]} — {inp.reason}",
                   entity_kind="vehicle", entity_id=v.id, kind="task", state=inp.to_state,
                   details={"from": frm, "to": inp.to_state, "reason": inp.reason, "source": inp.source, "backward": True})
        ctx.emit("metrics.invalidated", aggregate_type="vehicle", aggregate_id=v.id, payload={"reason": "recon.moved_back"})
        await recompute(ctx.db, v, ctx.now)
        return {"decision": "Allowed", "moved": True, "from": frm, "to": inp.to_state, "gates": [], "tasks": [],
                "reasons": [f"backward move: {inp.reason}"], "vehicle": serialize_vehicle(v)}
    gates = await evaluate_gates(ctx.db, v, inp.to_state, frm)
    blocking, overridden = [], []
    for g in gates:
        if g["ok"]:
            continue
        why = (inp.overrides or {}).get(g["requirement"])
        if g["overridable"] and why and why.strip():
            g["overridden"] = {"by": ctx.actor.user_id, "reason": why.strip()}
            overridden.append(g)
        else:
            if why and not g["overridable"]:
                g["override_refused"] = "factual gate; cannot be overridden"
            blocking.append(g)
    if blocking:
        tasks = []
        for g in blocking:
            t = await _gate_task(ctx, v, g)
            tasks.append({"gate": g["requirement"], "task_id": (t.get("task") or {}).get("id"), "created": t.get("created", False),
                          "title": (t.get("task") or {}).get("title")})
        reasons = [f"{g['label']}: {g['detail']}" for g in blocking]
        ctx.record(f"Blocked move {v.stock_no}: {STATE_LABELS[frm]} → {STATE_LABELS[inp.to_state]} ({len(blocking)} gate(s) unmet)",
                   entity_kind="vehicle", entity_id=v.id, kind="task", state="blocked", exception=True,
                   details={"from": frm, "to": inp.to_state, "gates": [g["requirement"] for g in blocking], "reasons": reasons,
                            "source": inp.source, "tasks": tasks})
        await recompute(ctx.db, v, ctx.now)
        return {"decision": "Blocked", "moved": False, "from": frm, "to": inp.to_state, "gates": gates, "tasks": tasks,
                "reasons": reasons, "vehicle": serialize_vehicle(v)}
    v.recon_state = inp.to_state
    _state_event(ctx, v, "recon", frm, inp.to_state, inp.reason)
    milestone = None
    if inp.to_state == "in_recon":
        milestone = await apply_milestone(ctx, v, kind="recon_started", status="completed", at=ctx.now, source_kind="shop",
                                          source_ref=f"move_stage:{inp.source}", note=inp.reason)
    elif inp.to_state == "ready_for_sale":
        milestone = await apply_milestone(ctx, v, kind="ready", status="completed", at=ctx.now, source_kind="shop",
                                          source_ref=f"move_stage:{inp.source}", note=inp.reason)
    ctx.touch(v, "vehicle")
    ctx.record(f"Moved {v.stock_no}: {STATE_LABELS[frm]} → {STATE_LABELS[inp.to_state]}" + (f" — {inp.reason}" if inp.reason else ""),
               entity_kind="vehicle", entity_id=v.id, kind="task", state=inp.to_state,
               details={"from": frm, "to": inp.to_state, "reason": inp.reason, "source": inp.source,
                        "gates": [{"requirement": g["requirement"], "ok": g["ok"], "overridden": g.get("overridden")} for g in gates]})
    ctx.emit("metrics.invalidated", aggregate_type="vehicle", aggregate_id=v.id, payload={"reason": "recon.moved"})
    await recompute(ctx.db, v, ctx.now)
    return {"decision": "Allowed", "moved": True, "from": frm, "to": inp.to_state, "gates": gates, "tasks": [],
            "reasons": [f"override: {g['label']} — {g['overridden']['reason']}" for g in overridden] or ["gates satisfied"],
            "milestone": (milestone or {}).get("milestone"), "vehicle": serialize_vehicle(v)}


@command("shop.log_inspection", input=InspectionIn, perm="tasks.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)],
         description="Log an inspection: findings become recon issues with specific tasks; photos are linked as evidence; "
                     "the inspected milestone is recorded with source inspection.")
async def shop_log_inspection(ctx: CommandContext, inp: InspectionIn) -> dict:
    v = await get_vehicle(ctx.db, inp.vehicle_id, expected_version=inp.expected_version)
    await _ready_assets(ctx.db, inp.asset_ids)
    at = aware(inp.inspected_at, inp.timezone) or ctx.now
    if at > ctx.now:
        raise ValidationFailed("inspected_at cannot be in the future")
    issues = []
    for f in inp.findings:
        await _ready_assets(ctx.db, f.asset_ids)
        issues.append(await _create_issue(ctx, v, IssueCreateIn(
            vehicle_id=v.id, title=f.title, detail=f.detail, severity=f.severity, source_kind="inspection",
            source_ref=f"inspection:{ctx.actor.user_id}:{at.isoformat()}", asset_ids=f.asset_ids,
            disclosure_required=f.disclosure_required, create_task=f.create_task, task_title=f.task_title,
            assignee_user_id=f.assignee_user_id, priority=f.priority, task_source_kind="inspection")))
    links = []
    for a in inp.asset_ids:
        res = await dispatch(ctx.child(), "assets.link", {"asset_id": a, "entity_kind": "vehicle", "entity_id": v.id,
                                                          "role": "evidence"}, commit=False)
        links.append((res.data or {}).get("link"))
    v.inspected_at = at
    ms = await apply_milestone(ctx, v, kind="inspected", status="completed", at=at, source_kind="inspection",
                               source_ref=f"inspection:{ctx.actor.user_id}", note=inp.note or None)
    ctx.touch(v, "vehicle")
    ctx.record(f"Inspection logged {v.stock_no}: {len(inp.findings)} finding(s)" + (f" — {inp.note}" if inp.note else ""),
               entity_kind="vehicle", entity_id=v.id, kind="task", state="inspected",
               details={"issues": [i["issue"]["id"] for i in issues], "asset_ids": inp.asset_ids, "at": iso(at)})
    if inp.asset_ids:
        ctx.emit("evidence.saved", aggregate_type="vehicle", aggregate_id=v.id,
                 payload={"vehicle_id": v.id, "asset_ids": inp.asset_ids, "context": "inspection"})
    await recompute(ctx.db, v, ctx.now)
    return {"vehicle": serialize_vehicle(v), "issues": issues, "links": links, "milestone": ms.get("milestone")}


@command("shop.create_issue", input=IssueCreateIn, perm="tasks.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)],
         description="Record a recon issue (reported/observed/inspection) and its verb-first task; equivalent open issues merge.")
async def shop_create_issue(ctx: CommandContext, inp: IssueCreateIn) -> dict:
    if inp.source_kind not in ("owner_reported", "image_observed", "inspection", "proposed_check", "customer", "manual"):
        raise ValidationFailed("source_kind must be owner_reported|image_observed|inspection|proposed_check|customer|manual")
    v = await get_vehicle(ctx.db, inp.vehicle_id)
    await _ready_assets(ctx.db, inp.asset_ids)
    out = await _create_issue(ctx, v, inp)
    await recompute(ctx.db, v, ctx.now)
    return out


@command("shop.resolve_issue", input=IssueRefIn, perm="tasks.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)],
         description="Mark a recon issue resolved with a note/evidence (the linked task still needs owner verification).")
async def shop_resolve_issue(ctx: CommandContext, inp: IssueRefIn) -> dict:
    i = await _issue(ctx, inp.issue_id, inp.vehicle_id, inp.expected_version)
    if i.status == "resolved":
        return {"issue": serialize_issue(i), "changed": False}
    if not (inp.note or inp.asset_ids):
        raise Blocked("resolving an issue needs a note or evidence")
    await _ready_assets(ctx.db, inp.asset_ids)
    i.status = "resolved"
    i.resolved_at = ctx.now
    i.resolved_by = ctx.actor.user_id
    i.resolution_note = inp.note
    i.asset_ids = list(dict.fromkeys([*(i.asset_ids or []), *inp.asset_ids]))
    ctx.touch(i, "recon_issue")
    ctx.record(f"Issue resolved: {i.title}" + (f" — {inp.note}" if inp.note else ""), entity_kind="vehicle", entity_id=i.vehicle_id,
               kind="task", state="resolved", details={"issue_id": i.id, "asset_ids": inp.asset_ids})
    if inp.asset_ids:
        ctx.emit("evidence.saved", aggregate_type="recon_issue", aggregate_id=i.id,
                 payload={"vehicle_id": i.vehicle_id, "asset_ids": inp.asset_ids, "context": "issue_resolved"})
    v = await get_vehicle(ctx.db, i.vehicle_id, lock=False)
    await recompute(ctx.db, v, ctx.now)
    return {"issue": serialize_issue(i), "changed": True}


class IssueUpdateIn(BaseModel):
    issue_id: str
    vehicle_id: str
    title: str | None = None
    detail: str | None = None
    severity: str | None = None
    status: str | None = None  # open|in_progress|rejected (resolve/defer have their own commands)
    reason: str | None = None
    disclosure_required: bool | None = None
    expected_version: int | None = None


@command("shop.update_issue", input=IssueUpdateIn, perm="tasks.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)],
         description="Edit a recon issue's text/severity, reopen it or reject a mistaken finding (history kept; evidence retained).")
async def shop_update_issue(ctx: CommandContext, inp: IssueUpdateIn) -> dict:
    i = await _issue(ctx, inp.issue_id, inp.vehicle_id, inp.expected_version)
    change: dict = {}
    if inp.title is not None and inp.title.strip() and inp.title.strip() != i.title:
        change["title"] = {"from": i.title, "to": inp.title.strip()}
        i.title = inp.title.strip()
        i.dedupe_key = issue_dedupe_key(i.vehicle_id, i.title)
    if inp.detail is not None and inp.detail != i.detail:
        change["detail"] = {"from": i.detail, "to": inp.detail}
        i.detail = inp.detail
    if inp.severity is not None and inp.severity != i.severity:
        change["severity"] = {"from": i.severity, "to": inp.severity}
        i.severity = inp.severity
    if inp.disclosure_required is not None and inp.disclosure_required != i.disclosure_required:
        change["disclosure_required"] = {"from": i.disclosure_required, "to": inp.disclosure_required}
        i.disclosure_required = inp.disclosure_required
    if inp.status is not None and inp.status != i.status:
        if inp.status not in ("open", "in_progress", "rejected"):
            raise ValidationFailed("status must be open|in_progress|rejected (use resolve_issue / defer_issue otherwise)")
        if inp.status == "rejected" and not (inp.reason or "").strip():
            raise ValidationFailed("rejecting a finding needs a reason")
        change["status"] = {"from": i.status, "to": inp.status}
        i.status = inp.status
        if inp.status == "rejected":
            i.resolution_note = inp.reason
            i.resolved_at = ctx.now
            i.resolved_by = ctx.actor.user_id
    if not change:
        return {"issue": serialize_issue(i), "changed": {}}
    ctx.touch(i, "recon_issue")
    ctx.record(f"Issue updated: {i.title}" + (f" — {inp.reason}" if inp.reason else ""), entity_kind="vehicle", entity_id=i.vehicle_id,
               kind="task", state=i.status, details={"issue_id": i.id, "change": change, "reason": inp.reason})
    v = await get_vehicle(ctx.db, i.vehicle_id, lock=False)
    await recompute(ctx.db, v, ctx.now)
    return {"issue": serialize_issue(i), "changed": change}


@command("shop.defer_issue", input=IssueDeferIn, perm="tasks.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)],
         description="Explicitly defer a recon issue with a reason (its open task is cancelled; disclosure may be required).")
async def shop_defer_issue(ctx: CommandContext, inp: IssueDeferIn) -> dict:
    i = await _issue(ctx, inp.issue_id, inp.vehicle_id, inp.expected_version)
    if i.status not in OPEN_ISSUE:
        raise Blocked(f"issue is {i.status}")
    i.status = "deferred"
    i.deferred_at = ctx.now
    i.deferred_by = ctx.actor.user_id
    i.deferred_reason = inp.reason
    cancelled = None
    if i.task_id:
        t = await ctx.db.get(Task, i.task_id)
        if t is not None and t.status not in ("completed", "cancelled"):
            await dispatch(ctx.child(), "tasks.cancel", {"task_id": t.id, "reason": f"issue deferred: {inp.reason}"}, commit=False)
            cancelled = t.id
    ctx.touch(i, "recon_issue")
    ctx.record(f"Issue deferred: {i.title} — {inp.reason}", entity_kind="vehicle", entity_id=i.vehicle_id, kind="task",
               state="deferred", details={"issue_id": i.id, "task_cancelled": cancelled, "disclosure_required": i.disclosure_required})
    v = await get_vehicle(ctx.db, i.vehicle_id, lock=False)
    await recompute(ctx.db, v, ctx.now)
    return {"issue": serialize_issue(i), "task_cancelled": cancelled}


@command("shop.create_work_order", input=WorkOrderIn, perm="tasks.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)], description="Open a work order (optionally for a recon issue).")
async def shop_create_work_order(ctx: CommandContext, inp: WorkOrderIn) -> dict:
    v = await get_vehicle(ctx.db, inp.vehicle_id, lock=False)
    n = await ctx.db.scalar(select(func.count()).select_from(WorkOrder))
    w = WorkOrder(vehicle_id=v.id, ref=human_ref("WO", int(n or 0) + 1), title=inp.title.strip(), status="open",
                  assignee_user_id=inp.assignee_user_id, recon_issue_id=inp.recon_issue_id, notes=inp.notes,
                  created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
    ctx.db.add(w)
    await ctx.db.flush()
    if inp.recon_issue_id:
        i = await ctx.db.get(ReconIssue, inp.recon_issue_id)
        if i is not None and i.vehicle_id == v.id:
            i.work_order_id = w.id
            if i.status == "open":
                i.status = "in_progress"
    ctx.changed.append({"kind": "work_order", "id": w.id, "version": w.version})
    ctx.record(f"Work order {w.ref}: {w.title}", entity_kind="vehicle", entity_id=v.id, kind="task", state="open",
               details={"work_order_id": w.id, "recon_issue_id": inp.recon_issue_id})
    return {"work_order": serialize_work_order(w)}


@command("shop.request_part", input=PartRequestIn, perm="parts.request", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)],
         description="Request a part (mechanic allowed). Requesting never orders or pays; equivalent open requests merge.")
async def shop_request_part(ctx: CommandContext, inp: PartRequestIn) -> dict:
    v = await get_vehicle(ctx.db, inp.vehicle_id, lock=False)
    existing = (await ctx.db.execute(select(Part).where(Part.vehicle_id == v.id, Part.state.in_(("requested", "approved", "ordered"))))).scalars().all()
    for p in existing:
        if norm_text(p.name) == norm_text(inp.name) and (p.part_no or "") == (inp.part_no or ""):
            return {"part": serialize_part(p), "created": False}
    p = Part(vehicle_id=v.id, work_order_id=inp.work_order_id, name=inp.name.strip(), part_no=inp.part_no,
             vendor_contact_id=inp.vendor_contact_id, quantity=inp.quantity, state="requested", requested_by=ctx.actor.user_id,
             notes=inp.notes, payment_state="unpaid",
             history=[{"from": None, "to": "requested", "at": ctx.now.isoformat(), "by": ctx.actor.user_id, "note": inp.notes or None}],
             created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
    ctx.db.add(p)
    await ctx.db.flush()
    ctx.changed.append({"kind": "part", "id": p.id, "version": p.version})
    ctx.record(f"Part requested: {p.name} ×{p.quantity} ({v.stock_no})", entity_kind="vehicle", entity_id=v.id, kind="task",
               state="requested", details={"part_id": p.id, "work_order_id": p.work_order_id})
    await recompute(ctx.db, v, ctx.now)
    return {"part": serialize_part(p), "created": True}


@command("shop.order_part", input=PartOrderIn, perm="parts.order", action_class="consequential", approval_kind="parts_order",
         records=lambda p: [("vehicle", p.vehicle_id)],
         summary=lambda p: f"Order part: {p.part_name or p.part_id}" + (f" — {p.amount} {p.currency}" if p.amount else ""),
         limits=lambda p: {"amount": p.amount, "currency": p.currency, "records": [p.vehicle_id]},
         consequence=lambda p: {"amount": p.amount, "currency": p.currency, "scope": "parts_order", "moves_money": True,
                                "targets": {"vendor_contact_id": p.vendor_contact_id, "part_id": p.part_id}},
         description="Order a requested part (exact approval or a standing parts permission). Persists an external action "
                     "intent (provider manual); the executor only records the order — nothing is bought inline.")
async def shop_order_part(ctx: CommandContext, inp: PartOrderIn) -> dict:
    p = await _part(ctx, inp.part_id, inp.vehicle_id)
    if p.state not in ("requested", "approved"):
        raise Blocked(f"part is {p.state}; only requested/approved parts can be ordered")
    if inp.amount is not None:
        try:
            quantize(parse_amount(inp.amount), inp.currency.upper())
        except ValueError:
            raise ValidationFailed("amount must be a decimal")
    if p.state == "requested":
        _part_hist(ctx, p, "approved", "order authorized")
    if ctx.approval is not None:
        p.approval_id = ctx.approval.id
    if inp.vendor_contact_id:
        p.vendor_contact_id = inp.vendor_contact_id
    act = await approvals_svc.intend_external_action(
        ctx, command_name="shop.order_part", payload=inp.model_dump(mode="json"), dedupe_key=f"parts.order:{p.id}",
        provider="manual", entity_kind="part", entity_id=p.id)
    p.external_action_id = act.id
    ctx.touch(p, "part")
    ctx.record(f"Part order authorized: {p.name}" + (f" {inp.amount} {inp.currency}" if inp.amount else ""), entity_kind="vehicle",
               entity_id=p.vehicle_id, kind="approval", state="queued", details={"part_id": p.id, "external_action_id": act.id})
    return {"part": serialize_part(p), "external_action_id": act.id}


@approvals_svc.executor("shop.order_part")
async def _execute_order_part(db: AsyncSession, act) -> dict:
    """Provider `manual`: the order is placed by a person; the system only records the authorized order."""
    from datetime import timezone
    p = (await db.execute(select(Part).where(Part.id == act.entity_id).with_for_update())).scalar_one_or_none()
    if p is None:
        raise approvals_svc.UnknownResult("part row missing at execution time")
    now = datetime.now(timezone.utc)
    payload = act.payload or {}
    if p.state in ("requested", "approved"):
        p.history = list(p.history or []) + [{"from": p.state, "to": "ordered", "at": now.isoformat(), "by": None,
                                              "note": "recorded by manual order executor"}]
        p.state = "ordered"
        p.ordered_at = now
    p.order_ref = payload.get("order_ref") or p.order_ref
    p.bump(None)
    return {"provider": "manual", "recorded": True, "part_id": p.id, "order_ref": p.order_ref, "at": now.isoformat(),
            "note": "order recorded; physical arrival and installation need their own evidence"}


@command("shop.mark_part_arrived", input=PartRefIn, perm="parts.request", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)], description="Record that a part physically arrived (photo/receipt evidence optional).")
async def shop_mark_part_arrived(ctx: CommandContext, inp: PartRefIn) -> dict:
    p = await _part(ctx, inp.part_id, inp.vehicle_id, inp.expected_version)
    if p.state not in ("requested", "approved", "ordered"):
        raise Blocked(f"part is {p.state}")
    await _ready_assets(ctx.db, inp.asset_ids)
    _part_hist(ctx, p, "arrived", inp.note)
    p.arrived_at = ctx.now
    if inp.asset_ids or inp.note:
        p.evidence = list(p.evidence or []) + [{"kind": "arrived", "asset_ids": inp.asset_ids, "note": inp.note, "by": ctx.actor.user_id, "at": ctx.now.isoformat()}]
    ctx.touch(p, "part")
    ctx.record(f"Part arrived: {p.name}", entity_kind="vehicle", entity_id=p.vehicle_id, kind="task", state="arrived",
               details={"part_id": p.id, "asset_ids": inp.asset_ids})
    return {"part": serialize_part(p)}


@command("shop.mark_part_installed", input=PartRefIn, perm="tasks.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)], description="Record installation; requires at least one photo/evidence asset.")
async def shop_mark_part_installed(ctx: CommandContext, inp: PartRefIn) -> dict:
    p = await _part(ctx, inp.part_id, inp.vehicle_id, inp.expected_version)
    if p.state != "arrived":
        raise Blocked(f"part is {p.state}; it must be marked arrived before installed")
    if not inp.asset_ids:
        raise Blocked("installation needs evidence (photo of the installed part)")
    await _ready_assets(ctx.db, inp.asset_ids)
    _part_hist(ctx, p, "installed", inp.note)
    p.installed_at = ctx.now
    p.evidence = list(p.evidence or []) + [{"kind": "installed", "asset_ids": inp.asset_ids, "note": inp.note, "by": ctx.actor.user_id, "at": ctx.now.isoformat()}]
    ctx.touch(p, "part")
    ctx.record(f"Part installed: {p.name}", entity_kind="vehicle", entity_id=p.vehicle_id, kind="task", state="installed",
               details={"part_id": p.id, "asset_ids": inp.asset_ids})
    ctx.emit("evidence.saved", aggregate_type="part", aggregate_id=p.id,
             payload={"vehicle_id": p.vehicle_id, "asset_ids": inp.asset_ids, "context": "part_installed"})
    return {"part": serialize_part(p)}


@command("shop.verify_part", input=PartRefIn, perm="tasks.verify", action_class="owner_only",
         records=lambda p: [("vehicle", p.vehicle_id)], description="Owner verifies an installed part.")
async def shop_verify_part(ctx: CommandContext, inp: PartRefIn) -> dict:
    p = await _part(ctx, inp.part_id, inp.vehicle_id, inp.expected_version)
    if p.state != "installed":
        raise Blocked(f"part is {p.state}; only installed parts can be verified")
    _part_hist(ctx, p, "verified", inp.note)
    p.verified_at = ctx.now
    p.verified_by = ctx.actor.user_id
    ctx.touch(p, "part")
    ctx.record(f"Part verified: {p.name}", entity_kind="vehicle", entity_id=p.vehicle_id, kind="task", state="verified",
               details={"part_id": p.id})
    ctx.emit("work.verified", aggregate_type="part", aggregate_id=p.id, payload={"vehicle_id": p.vehicle_id, "part_id": p.id})
    v = await get_vehicle(ctx.db, p.vehicle_id, lock=False)
    await recompute(ctx.db, v, ctx.now)
    return {"part": serialize_part(p)}


@command("shop.record_part_payment", input=PartPaymentIn, perm="finance.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)],
         description="Record that a part was paid for (links the cost item). Payment state only: never sets arrived or installed.")
async def shop_record_part_payment(ctx: CommandContext, inp: PartPaymentIn) -> dict:
    p = await _part(ctx, inp.part_id, inp.vehicle_id, inp.expected_version)
    physical_before = p.state
    p.payment_state = "paid"
    p.paid_at = aware(inp.paid_at) or ctx.now
    p.payment_note = inp.note
    if inp.cost_item_id:
        p.cost_item_id = inp.cost_item_id
    ctx.touch(p, "part")
    ctx.record(f"Part paid: {p.name} (physical state unchanged: {physical_before})", entity_kind="vehicle", entity_id=p.vehicle_id,
               kind="payment", state="paid", visibility="finance", details={"part_id": p.id, "cost_item_id": inp.cost_item_id})
    assert p.state == physical_before
    return {"part": serialize_part(p), "physical_state": p.state}


@command("shop.cancel_part", input=PartCancelIn, perm="parts.request", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)], description="Cancel (or mark returned) a part that is not installed.")
async def shop_cancel_part(ctx: CommandContext, inp: PartCancelIn) -> dict:
    p = await _part(ctx, inp.part_id, inp.vehicle_id, inp.expected_version)
    if p.state in ("installed", "verified"):
        raise Blocked(f"part is {p.state}; installed parts cannot be cancelled")
    _part_hist(ctx, p, "returned" if inp.returned else "cancelled", inp.reason)
    p.cancel_reason = inp.reason
    ctx.touch(p, "part")
    ctx.record(f"Part {p.state}: {p.name} — {inp.reason}", entity_kind="vehicle", entity_id=p.vehicle_id, kind="task", state=p.state,
               details={"part_id": p.id})
    v = await get_vehicle(ctx.db, p.vehicle_id, lock=False)
    await recompute(ctx.db, v, ctx.now)
    return {"part": serialize_part(p)}


# ── reads ────────────────────────────────────────────────────────────────────
async def board(db: AsyncSession, actor: Actor) -> dict:
    limit = await visible_vehicle_ids(db, actor)
    q = select(Vehicle).where(Vehicle.archived_at.is_(None), Vehicle.allocation != "candidate")
    if limit is not None:
        q = q.where(Vehicle.id.in_(list(limit)))
    rows = (await db.execute(q.order_by(Vehicle.updated_at.desc()))).scalars().all()
    cols = {s: [] for s in BOARD_COLUMNS}
    for v in rows:
        cols.setdefault(v.recon_state, []).append(serialize_list_item(v))
    return {"columns": [{"state": s, "label": STATE_LABELS[s], "vehicles": cols.get(s, []), "count": len(cols.get(s, []))}
                        for s in BOARD_COLUMNS], "total": len(rows)}


async def work_of(db: AsyncSession, vehicle_id: str) -> dict:
    issues = (await db.execute(select(ReconIssue).where(ReconIssue.vehicle_id == vehicle_id).order_by(ReconIssue.created_at))).scalars().all()
    wos = (await db.execute(select(WorkOrder).where(WorkOrder.vehicle_id == vehicle_id).order_by(WorkOrder.created_at))).scalars().all()
    parts = (await db.execute(select(Part).where(Part.vehicle_id == vehicle_id).order_by(Part.created_at))).scalars().all()
    return {"recon_issues": [serialize_issue(i) for i in issues], "work_orders": [serialize_work_order(w) for w in wos],
            "parts": [serialize_part(p) for p in parts]}
