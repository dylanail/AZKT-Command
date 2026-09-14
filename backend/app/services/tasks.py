"""Shared task / case / commitment commands (spec §5.3, §8.4). Every domain creates work
through these commands; reminders subscribe to the emitted `task.changed` events."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field
from sqlalchemy import select

from ..core.errors import Blocked, Conflict, NotFound, ValidationFailed
from ..core.ids import sha256_hex
from ..core.time import ensure_aware, reminder_fire_at
from ..domain.commands import CommandContext, command
from ..models.tasks import REMINDER_KINDS, TASK_STATUSES, TASK_TYPES, Case, Commitment, Task


def task_dedupe_key(vehicle_id: str | None, title: str) -> str:
    norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    norm = re.sub(r"\b(please|the|a|an|and|to|on|for|of)\b", "", norm)
    norm = re.sub(r"\s+", " ", norm).strip()
    return sha256_hex(f"{vehicle_id or ''}|{norm}")[:32]


def serialize_task(t: Task) -> dict:
    now = datetime.now(timezone.utc)
    overdue = bool(t.due_at and t.status not in ("completed", "cancelled") and ensure_aware(t.due_at) < now)
    return {
        "id": t.id, "version": t.version, "title": t.title, "type": t.type, "status": t.status, "priority": t.priority,
        "owner_user_id": t.owner_user_id, "assigned_by": t.assigned_by, "contact_id": t.contact_id,
        "opportunity_id": t.opportunity_id, "vehicle_id": t.vehicle_id, "case_id": t.case_id,
        "shipment_id": t.shipment_id, "import_request_id": t.import_request_id, "recon_issue_id": t.recon_issue_id,
        "notes": t.notes, "instructions": t.instructions,
        "due_at": t.due_at.isoformat() if t.due_at else None, "start_at": t.start_at.isoformat() if t.start_at else None,
        "end_at": t.end_at.isoformat() if t.end_at else None, "timezone": t.timezone,
        "reminder_kind": t.reminder_kind, "reminder_custom_minutes": t.reminder_custom_minutes,
        "schedule_revision": t.schedule_revision, "snoozed_until": t.snoozed_until.isoformat() if t.snoozed_until else None,
        "next_check_at": t.next_check_at.isoformat() if t.next_check_at else None, "next_check_label": t.next_check_label,
        "evidence_required": t.evidence_required, "evidence": t.evidence, "block_reason": t.block_reason,
        "verification_status": t.verification_status, "verified_by": t.verified_by,
        "verified_at": t.verified_at.isoformat() if t.verified_at else None, "rejection_reason": t.rejection_reason,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None, "completed_by": t.completed_by,
        "cancelled_at": t.cancelled_at.isoformat() if t.cancelled_at else None, "cancel_reason": t.cancel_reason,
        "source_kind": t.source_kind, "source_id": t.source_id, "gate_requirement": t.gate_requirement,
        "is_suggestion": t.is_suggestion, "overdue": overdue, "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None, "extra": t.extra or {},
    }


def _emit_task(ctx: CommandContext, t: Task, change: str, **extra) -> None:
    ctx.emit("task.changed", aggregate_type="task", aggregate_id=t.id, aggregate_version=t.version,
             payload={"task_id": t.id, "change": change, "schedule_revision": t.schedule_revision,
                      "owner_user_id": t.owner_user_id, "vehicle_id": t.vehicle_id, "status": t.status, **extra})


async def _get(ctx: CommandContext, task_id: str, expected_version: int | None = None) -> Task:
    t = (await ctx.db.execute(select(Task).where(Task.id == task_id).with_for_update())).scalar_one_or_none()
    if t is None:
        raise NotFound("task not found")
    if expected_version is not None and t.version != expected_version:
        raise Conflict("task changed since you loaded it", current_version=t.version)
    return t


class TaskCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    type: str = "operational"
    priority: str = "normal"
    owner_user_id: str | None = None
    contact_id: str | None = None
    opportunity_id: str | None = None
    vehicle_id: str | None = None
    case_id: str | None = None
    shipment_id: str | None = None
    import_request_id: str | None = None
    recon_issue_id: str | None = None
    notes: str = ""
    instructions: str = ""
    due_at: datetime | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    timezone: str = "America/Phoenix"
    reminder_kind: str | None = None
    reminder_custom_minutes: int | None = None
    next_check_at: datetime | None = None
    next_check_label: str | None = None
    evidence_required: list = Field(default_factory=list)
    source_kind: str | None = None
    source_id: str | None = None
    gate_requirement: str | None = None
    is_suggestion: bool = False
    dedupe: bool = True
    extra: dict = Field(default_factory=dict)


def _validate_schedule(inp) -> None:
    if inp.type not in TASK_TYPES:
        raise ValidationFailed(f"type must be one of {TASK_TYPES}")
    if inp.reminder_kind is not None and inp.reminder_kind not in REMINDER_KINDS:
        raise ValidationFailed(f"reminder_kind must be one of {REMINDER_KINDS}")
    if inp.reminder_kind and not inp.due_at:
        raise ValidationFailed("a reminder needs a due time")
    if inp.reminder_kind == "custom" and not inp.reminder_custom_minutes:
        raise ValidationFailed("custom reminder needs reminder_custom_minutes")


@command("tasks.create", input=TaskCreateIn, perm="tasks.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)] if p.vehicle_id else [],
         description="Create a task (call/meeting/follow-up/operational) with optional reminder; deduplicates equivalent open work.")
async def tasks_create(ctx: CommandContext, inp: TaskCreateIn) -> dict:
    _validate_schedule(inp)
    key = task_dedupe_key(inp.vehicle_id, inp.title) if inp.dedupe else None
    if key:
        existing = (await ctx.db.execute(select(Task).where(
            Task.dedupe_key == key, Task.status.notin_(("completed", "cancelled"))))).scalars().first()
        if existing is not None:
            # equivalent open work: append evidence/notes instead of multiplying tasks (invariant 13)
            changed = False
            if inp.notes and inp.notes not in (existing.notes or ""):
                existing.notes = (existing.notes + "\n" if existing.notes else "") + inp.notes
                changed = True
            if inp.source_id and inp.source_id != existing.source_id:
                ex = dict(existing.extra or {})
                ex.setdefault("also_from", [])
                if inp.source_id not in ex["also_from"]:
                    ex["also_from"].append(inp.source_id)
                    existing.extra = ex
                    changed = True
            if inp.owner_user_id and not existing.owner_user_id:
                existing.owner_user_id = inp.owner_user_id
                changed = True
            if changed:
                ctx.touch(existing, "task")
                _emit_task(ctx, existing, "merged")
            return {"task": serialize_task(existing), "created": False, "merged_into": existing.id}
    t = Task(title=inp.title.strip(), type=inp.type, priority=inp.priority, owner_user_id=inp.owner_user_id,
             assigned_by=ctx.actor.user_id if inp.owner_user_id else None, contact_id=inp.contact_id,
             opportunity_id=inp.opportunity_id, vehicle_id=inp.vehicle_id, case_id=inp.case_id,
             shipment_id=inp.shipment_id, import_request_id=inp.import_request_id, recon_issue_id=inp.recon_issue_id,
             notes=inp.notes, instructions=inp.instructions, due_at=inp.due_at, start_at=inp.start_at, end_at=inp.end_at,
             timezone=inp.timezone, reminder_kind=inp.reminder_kind, reminder_custom_minutes=inp.reminder_custom_minutes,
             next_check_at=inp.next_check_at, next_check_label=inp.next_check_label,
             evidence_required=inp.evidence_required, source_kind=inp.source_kind or ctx.channel, source_id=inp.source_id,
             gate_requirement=inp.gate_requirement, is_suggestion=inp.is_suggestion, dedupe_key=key, extra=inp.extra,
             created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
    if not t.owner_user_id and t.type == "operational":
        t.status = "open"
    ctx.db.add(t)
    await ctx.db.flush()
    ctx.changed.append({"kind": "task", "id": t.id, "version": t.version})
    ctx.record(f"Created task: {t.title}", entity_kind="task", entity_id=t.id, kind="task", state=t.status,
               details={"vehicle_id": t.vehicle_id, "owner_user_id": t.owner_user_id})
    _emit_task(ctx, t, "created")
    return {"task": serialize_task(t), "created": True}


class TaskUpdateIn(BaseModel):
    task_id: str
    expected_version: int | None = None
    title: str | None = None
    notes: str | None = None
    instructions: str | None = None
    priority: str | None = None
    evidence_required: list | None = None
    contact_id: str | None = None
    vehicle_id: str | None = None
    next_check_at: datetime | None = None
    next_check_label: str | None = None
    extra: dict | None = None


@command("tasks.update", input=TaskUpdateIn, perm="tasks.write", action_class="internal",
         records=lambda p: [("task", p.task_id)], description="Edit task text/links (not schedule).")
async def tasks_update(ctx: CommandContext, inp: TaskUpdateIn) -> dict:
    t = await _get(ctx, inp.task_id, inp.expected_version)
    for f in ("title", "notes", "instructions", "priority", "evidence_required", "contact_id", "vehicle_id",
              "next_check_at", "next_check_label"):
        v = getattr(inp, f)
        if v is not None:
            setattr(t, f, v)
    if inp.extra is not None:
        t.extra = {**(t.extra or {}), **inp.extra}
    if inp.title:
        t.dedupe_key = task_dedupe_key(t.vehicle_id, t.title)
    ctx.touch(t, "task")
    ctx.record(f"Updated task: {t.title}", entity_kind="task", entity_id=t.id, kind="task", state=t.status)
    _emit_task(ctx, t, "updated")
    return {"task": serialize_task(t)}


class TaskRescheduleIn(BaseModel):
    task_id: str
    expected_version: int | None = None
    due_at: datetime | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    timezone: str | None = None
    reminder_kind: str | None = None
    reminder_custom_minutes: int | None = None
    clear_reminder: bool = False


@command("tasks.reschedule", input=TaskRescheduleIn, perm="tasks.write", action_class="internal",
         records=lambda p: [("task", p.task_id)],
         description="Change when a task is due and/or its reminder; invalidates old scheduled deliveries.")
async def tasks_reschedule(ctx: CommandContext, inp: TaskRescheduleIn) -> dict:
    t = await _get(ctx, inp.task_id, inp.expected_version)
    if t.status in ("completed", "cancelled"):
        raise Blocked(f"task is {t.status}")
    if inp.reminder_kind is not None and inp.reminder_kind not in REMINDER_KINDS:
        raise ValidationFailed(f"reminder_kind must be one of {REMINDER_KINDS}")
    old = (t.due_at, t.reminder_kind, t.reminder_custom_minutes)
    if inp.due_at is not None:
        t.due_at = inp.due_at
    if inp.start_at is not None:
        t.start_at = inp.start_at
    if inp.end_at is not None:
        t.end_at = inp.end_at
    if inp.timezone:
        t.timezone = inp.timezone
    if inp.clear_reminder:
        t.reminder_kind = None
        t.reminder_custom_minutes = None
    elif inp.reminder_kind is not None:
        t.reminder_kind = inp.reminder_kind
        t.reminder_custom_minutes = inp.reminder_custom_minutes if inp.reminder_kind == "custom" else None
    t.snoozed_until = None
    t.schedule_revision = (t.schedule_revision or 1) + 1
    ctx.touch(t, "task")
    ctx.record(f"Rescheduled task: {t.title}", entity_kind="task", entity_id=t.id, kind="task", state=t.status,
               details={"from": str(old[0]), "to": str(t.due_at)})
    _emit_task(ctx, t, "rescheduled")
    return {"task": serialize_task(t)}


class TaskSnoozeIn(BaseModel):
    task_id: str
    minutes: int = Field(default=10, ge=1, le=24 * 60 * 7)
    expected_version: int | None = None


@command("tasks.snooze", input=TaskSnoozeIn, perm="tasks.write", action_class="internal",
         records=lambda p: [("task", p.task_id)], description="Delay the reminder without changing the meeting time.")
async def tasks_snooze(ctx: CommandContext, inp: TaskSnoozeIn) -> dict:
    t = await _get(ctx, inp.task_id, inp.expected_version)
    if t.status in ("completed", "cancelled"):
        raise Blocked(f"task is {t.status}")
    t.snoozed_until = ctx.now + timedelta(minutes=inp.minutes)
    t.schedule_revision = (t.schedule_revision or 1) + 1
    ctx.touch(t, "task")
    ctx.record(f"Snoozed reminder {inp.minutes} min: {t.title}", entity_kind="task", entity_id=t.id, kind="task", state=t.status)
    _emit_task(ctx, t, "snoozed", snoozed_until=t.snoozed_until.isoformat())
    return {"task": serialize_task(t)}


class TaskAssignIn(BaseModel):
    task_id: str
    owner_user_id: str | None
    expected_version: int | None = None


@command("tasks.assign", input=TaskAssignIn, perm="tasks.assign", action_class="internal",
         records=lambda p: [("task", p.task_id)], description="Assign or unassign a task.")
async def tasks_assign(ctx: CommandContext, inp: TaskAssignIn) -> dict:
    t = await _get(ctx, inp.task_id, inp.expected_version)
    if inp.owner_user_id:
        from ..models import User
        u = await ctx.db.get(User, inp.owner_user_id)
        if u is None or u.status != "active":
            raise ValidationFailed("assignee is not an active person")
        if ctx.actor.kind == "user" and ctx.actor.role == "manager" and u.manager_id not in (ctx.actor.user_id, None) and u.role != "mechanic":
            raise Blocked("you can only assign people who report to you")
    t.owner_user_id = inp.owner_user_id
    t.assigned_by = ctx.actor.user_id
    if t.status == "open" and t.owner_user_id:
        pass
    ctx.touch(t, "task")
    ctx.record(f"Assigned task: {t.title}", entity_kind="task", entity_id=t.id, kind="task", state=t.status,
               details={"owner_user_id": t.owner_user_id})
    _emit_task(ctx, t, "assigned")
    return {"task": serialize_task(t)}


class TaskStatusIn(BaseModel):
    task_id: str
    expected_version: int | None = None
    note: str | None = None


@command("tasks.start", input=TaskStatusIn, perm="tasks.write", action_class="internal",
         records=lambda p: [("task", p.task_id)], description="Mark a task in progress.")
async def tasks_start(ctx: CommandContext, inp: TaskStatusIn) -> dict:
    t = await _get(ctx, inp.task_id, inp.expected_version)
    if t.status not in ("open", "blocked", "waiting"):
        raise Blocked(f"task is {t.status}")
    t.status = "in_progress"
    t.block_reason = None
    ctx.touch(t, "task")
    _emit_task(ctx, t, "started")
    return {"task": serialize_task(t)}


class TaskEvidenceIn(BaseModel):
    task_id: str
    asset_ids: list[str] = Field(default_factory=list)
    note: str | None = None
    reading: dict | None = None
    expected_version: int | None = None


@command("tasks.attach_evidence", input=TaskEvidenceIn, perm="tasks.write", action_class="internal",
         records=lambda p: [("task", p.task_id)], description="Attach saved photos/notes/readings as evidence.")
async def tasks_attach_evidence(ctx: CommandContext, inp: TaskEvidenceIn) -> dict:
    t = await _get(ctx, inp.task_id, inp.expected_version)
    if t.status in ("completed", "cancelled"):
        raise Blocked(f"task is {t.status}")
    if inp.asset_ids:
        from ..models.assets import Asset
        rows = (await ctx.db.execute(select(Asset).where(Asset.id.in_(inp.asset_ids)))).scalars().all()
        found = {a.id: a for a in rows}
        missing = [a for a in inp.asset_ids if a not in found or found[a].status != "ready"]
        if missing:
            raise Blocked("evidence upload not complete", missing_assets=missing)
    ev = list(t.evidence or [])
    ev.append({"asset_ids": inp.asset_ids, "note": inp.note, "reading": inp.reading, "by": ctx.actor.user_id,
               "at": ctx.now.isoformat()})
    t.evidence = ev
    ctx.touch(t, "task")
    ctx.record(f"Evidence saved: {t.title}", entity_kind="task", entity_id=t.id, kind="task", state=t.status,
               details={"asset_ids": inp.asset_ids})
    _emit_task(ctx, t, "evidence")
    return {"task": serialize_task(t)}


def _evidence_satisfied(t: Task) -> list[str]:
    missing = []
    for req in t.evidence_required or []:
        kind = req.get("kind") if isinstance(req, dict) else str(req)
        need = int(req.get("min", 1)) if isinstance(req, dict) else 1
        have = 0
        for e in t.evidence or []:
            if kind == "photo":
                have += len(e.get("asset_ids") or [])
            elif kind == "note" and e.get("note"):
                have += 1
            elif kind == "reading" and e.get("reading"):
                have += 1
            elif kind == "receipt":
                have += len(e.get("asset_ids") or [])
        if have < need:
            missing.append(f"{kind} ({have}/{need})")
    return missing


@command("tasks.complete", input=TaskStatusIn, perm="tasks.write", action_class="internal",
         records=lambda p: [("task", p.task_id)],
         description="Complete a task. With required evidence it becomes Awaiting verification instead.")
async def tasks_complete(ctx: CommandContext, inp: TaskStatusIn) -> dict:
    t = await _get(ctx, inp.task_id, inp.expected_version)
    if t.status in ("completed", "cancelled"):
        raise Blocked(f"task is already {t.status}")
    missing = _evidence_satisfied(t)
    if missing:
        raise Blocked("required evidence missing", missing=missing)
    if t.evidence_required:
        t.status = "awaiting_verification"
        t.verification_status = "awaiting"
        state = "awaiting_verification"
        what = f"Awaiting verification: {t.title}"
    else:
        t.status = "completed"
        t.completed_at = ctx.now
        t.completed_by = ctx.actor.user_id
        state = "completed"
        what = f"Completed task: {t.title}"
    t.schedule_revision = (t.schedule_revision or 1) + 1
    if inp.note:
        t.notes = (t.notes + "\n" if t.notes else "") + inp.note
    ctx.touch(t, "task")
    ctx.record(what, entity_kind="task", entity_id=t.id, kind="task", state=state)
    _emit_task(ctx, t, state)
    return {"task": serialize_task(t)}


@command("tasks.verify", input=TaskStatusIn, perm="tasks.verify", action_class="owner_only",
         records=lambda p: [("task", p.task_id)],
         description="Owner verifies submitted evidence; releases configured gates.")
async def tasks_verify(ctx: CommandContext, inp: TaskStatusIn) -> dict:
    t = await _get(ctx, inp.task_id, inp.expected_version)
    if t.status != "awaiting_verification":
        raise Blocked("task is not awaiting verification", status=t.status)
    t.status = "completed"
    t.verification_status = "verified"
    t.verified_by = ctx.actor.user_id
    t.verified_at = ctx.now
    t.completed_at = ctx.now
    t.completed_by = t.completed_by or ctx.actor.user_id
    ctx.touch(t, "task")
    ctx.record(f"Verified: {t.title}", entity_kind="task", entity_id=t.id, kind="task", state="verified")
    _emit_task(ctx, t, "verified")
    ctx.emit("work.verified", aggregate_type="task", aggregate_id=t.id, payload={"task_id": t.id, "vehicle_id": t.vehicle_id,
                                                                                 "recon_issue_id": t.recon_issue_id})
    return {"task": serialize_task(t)}


class TaskRejectIn(BaseModel):
    task_id: str
    reason: str = Field(min_length=1)
    expected_version: int | None = None


@command("tasks.reject_evidence", input=TaskRejectIn, perm="tasks.verify", action_class="owner_only",
         records=lambda p: [("task", p.task_id)], description="Reject submitted evidence; task reopens with reason.")
async def tasks_reject(ctx: CommandContext, inp: TaskRejectIn) -> dict:
    t = await _get(ctx, inp.task_id, inp.expected_version)
    if t.status != "awaiting_verification":
        raise Blocked("task is not awaiting verification", status=t.status)
    t.status = "open"
    t.verification_status = "rejected"
    t.rejection_reason = inp.reason
    ctx.touch(t, "task")
    ctx.record(f"Evidence rejected: {t.title} — {inp.reason}", entity_kind="task", entity_id=t.id, kind="task", state="open", exception=True)
    _emit_task(ctx, t, "rejected")
    return {"task": serialize_task(t)}


class TaskBlockIn(BaseModel):
    task_id: str
    reason: str = Field(min_length=1)
    expected_version: int | None = None


@command("tasks.report_blocker", input=TaskBlockIn, perm="tasks.write", action_class="internal",
         records=lambda p: [("task", p.task_id)], description="I'm blocked: task stays open; owner/manager notified once.")
async def tasks_block(ctx: CommandContext, inp: TaskBlockIn) -> dict:
    t = await _get(ctx, inp.task_id, inp.expected_version)
    if t.status in ("completed", "cancelled"):
        raise Blocked(f"task is {t.status}")
    t.status = "blocked"
    t.block_reason = inp.reason
    t.blocked_at = ctx.now
    ctx.touch(t, "task")
    ctx.record(f"Blocked: {t.title} — {inp.reason}", entity_kind="task", entity_id=t.id, kind="task", state="blocked", exception=True)
    _emit_task(ctx, t, "blocked", reason=inp.reason)
    return {"task": serialize_task(t)}


class TaskCancelIn(BaseModel):
    task_id: str
    reason: str | None = None
    expected_version: int | None = None


@command("tasks.cancel", input=TaskCancelIn, perm="tasks.write", action_class="internal",
         records=lambda p: [("task", p.task_id)], description="Cancel a task; scheduled reminders are invalidated.")
async def tasks_cancel(ctx: CommandContext, inp: TaskCancelIn) -> dict:
    t = await _get(ctx, inp.task_id, inp.expected_version)
    if t.status in ("completed", "cancelled"):
        raise Blocked(f"task is already {t.status}")
    t.status = "cancelled"
    t.cancelled_at = ctx.now
    t.cancel_reason = inp.reason
    t.schedule_revision = (t.schedule_revision or 1) + 1
    ctx.touch(t, "task")
    ctx.record(f"Cancelled task: {t.title}", entity_kind="task", entity_id=t.id, kind="task", state="cancelled")
    _emit_task(ctx, t, "cancelled")
    return {"task": serialize_task(t)}


@command("tasks.reopen", input=TaskStatusIn, perm="tasks.write", action_class="internal",
         records=lambda p: [("task", p.task_id)], description="Reopen a completed/cancelled task.")
async def tasks_reopen(ctx: CommandContext, inp: TaskStatusIn) -> dict:
    t = await _get(ctx, inp.task_id, inp.expected_version)
    t.status = "open"
    t.completed_at = None
    t.cancelled_at = None
    t.verification_status = "none"
    t.schedule_revision = (t.schedule_revision or 1) + 1
    ctx.touch(t, "task")
    ctx.record(f"Reopened task: {t.title}", entity_kind="task", entity_id=t.id, kind="task", state="open")
    _emit_task(ctx, t, "reopened")
    return {"task": serialize_task(t)}


# ── Cases ────────────────────────────────────────────────────────────────────
def serialize_case(c: Case) -> dict:
    return {"id": c.id, "version": c.version, "title": c.title, "kind": c.kind, "status": c.status,
            "owner_role": c.owner_role, "owner_user_id": c.owner_user_id, "vehicle_id": c.vehicle_id,
            "contact_id": c.contact_id, "opportunity_id": c.opportunity_id, "shipment_id": c.shipment_id,
            "import_request_id": c.import_request_id, "conversation_id": c.conversation_id, "mission_id": c.mission_id,
            "summary": c.summary, "waiting_on": c.waiting_on, "next_action": c.next_action,
            "next_check_at": c.next_check_at.isoformat() if c.next_check_at else None,
            "resolved_at": c.resolved_at.isoformat() if c.resolved_at else None, "evidence": c.evidence,
            "extra": c.extra or {}, "created_at": c.created_at.isoformat() if c.created_at else None}


class CaseOpenIn(BaseModel):
    title: str
    kind: str = "other"
    owner_role: str = "manager"
    owner_user_id: str | None = None
    vehicle_id: str | None = None
    contact_id: str | None = None
    opportunity_id: str | None = None
    shipment_id: str | None = None
    import_request_id: str | None = None
    conversation_id: str | None = None
    mission_id: str | None = None
    summary: str = ""
    next_action: str | None = None
    next_check_at: datetime | None = None
    waiting_on: str | None = None
    extra: dict = Field(default_factory=dict)


@command("cases.open", input=CaseOpenIn, perm="tasks.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)] if p.vehicle_id else [], description="Open an ongoing case.")
async def cases_open(ctx: CommandContext, inp: CaseOpenIn) -> dict:
    c = Case(**inp.model_dump(), status="waiting" if inp.waiting_on else "open", created_by=ctx.actor.user_id)
    ctx.db.add(c)
    await ctx.db.flush()
    ctx.changed.append({"kind": "case", "id": c.id, "version": c.version})
    ctx.record(f"Opened case: {c.title}", entity_kind="case", entity_id=c.id, kind="task", state=c.status)
    ctx.emit("case.changed", aggregate_type="case", aggregate_id=c.id, payload={"case_id": c.id, "change": "opened"})
    return {"case": serialize_case(c)}


class CaseUpdateIn(BaseModel):
    case_id: str
    expected_version: int | None = None
    status: str | None = None
    summary: str | None = None
    next_action: str | None = None
    next_check_at: datetime | None = None
    waiting_on: str | None = None
    owner_user_id: str | None = None
    evidence: list | None = None
    extra: dict | None = None


@command("cases.update", input=CaseUpdateIn, perm="tasks.write", action_class="internal",
         description="Update case status / next check / waiting condition. Resolving requires evidence or a note.")
async def cases_update(ctx: CommandContext, inp: CaseUpdateIn) -> dict:
    c = (await ctx.db.execute(select(Case).where(Case.id == inp.case_id).with_for_update())).scalar_one_or_none()
    if c is None:
        raise NotFound("case not found")
    if inp.expected_version is not None and c.version != inp.expected_version:
        raise Conflict("case changed", current_version=c.version)
    if inp.status == "resolved" and not (inp.evidence or c.evidence or inp.summary or c.summary):
        raise Blocked("resolving a case needs evidence or a summary")
    for f in ("status", "summary", "next_action", "next_check_at", "waiting_on", "owner_user_id"):
        v = getattr(inp, f)
        if v is not None:
            setattr(c, f, v)
    if inp.evidence is not None:
        c.evidence = list(c.evidence or []) + list(inp.evidence)
    if inp.extra is not None:
        c.extra = {**(c.extra or {}), **inp.extra}
    if c.status == "resolved":
        c.resolved_at = ctx.now
    if c.status in ("open", "waiting", "blocked", "needs_owner") and not c.next_check_at and not c.next_action:
        c.extra = {**(c.extra or {}), "exception": "no next action or check"}
    ctx.touch(c, "case")
    ctx.record(f"Case {c.status}: {c.title}", entity_kind="case", entity_id=c.id, kind="task", state=c.status)
    ctx.emit("case.changed", aggregate_type="case", aggregate_id=c.id, payload={"case_id": c.id, "change": "updated", "status": c.status})
    return {"case": serialize_case(c)}


class CommitmentIn(BaseModel):
    text: str
    contact_id: str | None = None
    vehicle_id: str | None = None
    opportunity_id: str | None = None
    due_at: datetime | None = None
    source_kind: str | None = None
    source_id: str | None = None
    status: str = "open"


@command("commitments.record", input=CommitmentIn, perm="tasks.write", action_class="internal",
         description="Record a promise made to a person (from a confirmed send or an authorized record).")
async def commitments_record(ctx: CommandContext, inp: CommitmentIn) -> dict:
    c = Commitment(**inp.model_dump(), made_by=ctx.actor.user_id, made_at=ctx.now)
    ctx.db.add(c)
    await ctx.db.flush()
    ctx.changed.append({"kind": "commitment", "id": c.id, "version": c.version})
    ctx.record(f"Commitment recorded: {c.text}", entity_kind="commitment", entity_id=c.id, kind="message", state=c.status)
    return {"commitment": {"id": c.id, "text": c.text, "status": c.status, "due_at": c.due_at.isoformat() if c.due_at else None,
                           "contact_id": c.contact_id, "vehicle_id": c.vehicle_id}}


def reminder_time(t: Task) -> datetime | None:
    if not t.due_at or not t.reminder_kind:
        return None
    return reminder_fire_at(t.due_at, t.reminder_kind, t.reminder_custom_minutes)
