"""The two sales pipelines (spec §5.1) and the opportunity side of the deposit handoff (§5.2).

Stages: new → conversation → awaiting_deposit → deposit_paid, with lost outside the active flow.
Calls/meetings/follow-ups are optional timed tasks (services/tasks.py), never stages.
`deposit_paid` is evidence-backed: it is set only by `sales.set_conversion` (finance's deposit handoff),
never by `sales.move_stage`. Exactly one conversion link per opportunity (invariant 6).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import select

from ..core.errors import Blocked, Conflict, NotFound, ValidationFailed
from ..core.ids import sha256_hex
from ..core.money import quantize
from ..core.time import PHOENIX, ensure_aware, fmt_local
from ..domain.commands import CommandContext, command, dispatch
from ..models.sales import PIPELINES, STAGES, Opportunity
from ..models.tasks import Task
from ..models.vehicles import Vehicle

STAGE_LABELS = {"new": "New Lead", "conversation": "In Conversation", "awaiting_deposit": "Awaiting Deposit",
                "deposit_paid": "Deposit Paid", "lost": "Lost / Not moving forward"}
BOARD_STAGES = ("new", "conversation", "awaiting_deposit", "deposit_paid")
OPEN_STAGES = ("new", "conversation", "awaiting_deposit")
HAND_SETTABLE_STAGES = ("new", "conversation", "awaiting_deposit", "lost")
DEPOSIT_PAID_BLOCK_REASON = "Deposit Paid is confirmed from payment evidence; use Record payment"
CONVERSION_KINDS = ("import_request", "sale")
ACTIVE_TASK_STATUSES = ("open", "in_progress", "blocked", "waiting", "awaiting_verification")
assert "meeting" not in STAGES  # spec §5.1: no mandatory meeting stage


# ── serializers / projections ────────────────────────────────────────────────
def enquiry_key(text: str | None) -> str:
    words = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower()).split()
    return sha256_hex(" ".join(words[:12]))[:32] if words else "-"


def _hist(o: Opportunity) -> list:
    return list(o.stage_history or [])


def serialize_opportunity(o: Opportunity) -> dict:
    return {
        "id": o.id, "version": o.version, "contact_id": o.contact_id, "pipeline": o.pipeline, "stage": o.stage,
        "stage_label": STAGE_LABELS.get(o.stage, o.stage), "vehicle_id": o.vehicle_id,
        "import_request_id": o.import_request_id, "enquiry": o.enquiry,
        "budget_amount": str(o.budget_amount) if o.budget_amount is not None else None,
        "budget_currency": o.budget_currency, "source": o.source, "source_ref": o.source_ref,
        "owner_user_id": o.owner_user_id, "notes": o.notes, "next_action": o.next_action,
        "lost_reason": o.lost_reason, "lost_at": o.lost_at.isoformat() if o.lost_at else None,
        "deposit_confirmed_at": o.deposit_confirmed_at.isoformat() if o.deposit_confirmed_at else None,
        "deposit_payment_id": o.deposit_payment_id, "converted_kind": o.converted_kind, "converted_id": o.converted_id,
        "converted_at": o.converted_at.isoformat() if o.converted_at else None,
        "conversion_source_ref": o.conversion_source_ref, "conversation_ids": list(o.conversation_ids or []),
        "stage_history": _hist(o), "stage_changed_at": o.stage_changed_at.isoformat() if o.stage_changed_at else None,
        "reopened_at": o.reopened_at.isoformat() if o.reopened_at else None, "extra": dict(o.extra or {}),
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "updated_at": o.updated_at.isoformat() if o.updated_at else None,
    }


def vehicle_title(v: Vehicle | None) -> str | None:
    if v is None:
        return None
    parts = [str(v.model_year) if v.model_year else None, v.make, v.model, v.color]
    base = " ".join(p for p in parts if p) or (v.title or "")
    return f"{base} · {v.stock_no}" if v.stock_no else (base or v.id[:8])


def opportunity_subject(o: Opportunity, v: Vehicle | None) -> str:
    if o.pipeline == "vehicle":
        return vehicle_title(v) or "Vehicle not recorded"
    return (o.enquiry or "").strip() or "Import enquiry (not recorded)"


def human_age(created_at: datetime | None, now: datetime) -> str:
    if not created_at:
        return "Not recorded"
    delta = now - ensure_aware(created_at)
    secs = int(delta.total_seconds())
    if secs < 3600:
        return f"{max(secs // 60, 0)}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _task_brief(t: Task | None, now: datetime) -> dict | None:
    if t is None:
        return None
    due = ensure_aware(t.due_at)
    overdue = bool(due and t.status not in ("completed", "cancelled") and due < now)
    return {"id": t.id, "title": t.title, "type": t.type, "status": t.status, "owner_user_id": t.owner_user_id,
            "due_at": due.isoformat() if due else None, "timezone": t.timezone,
            "local_due": fmt_local(due, PHOENIX) if due else "Not scheduled",
            "reminder_kind": t.reminder_kind, "reminder_custom_minutes": t.reminder_custom_minutes,
            "overdue": overdue}


def lead_card(o: Opportunity, contact, vehicle: Vehicle | None, next_task: Task | None, owner_user=None,
              now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    nt = _task_brief(next_task, now)
    return {
        "id": o.id, "version": o.version, "pipeline": o.pipeline, "stage": o.stage,
        "stage_label": STAGE_LABELS.get(o.stage, o.stage), "contact_id": o.contact_id,
        "name": getattr(contact, "name", None) or "Contact not recorded",
        "company": getattr(contact, "company", None),
        "subject": opportunity_subject(o, vehicle), "vehicle_id": o.vehicle_id,
        "stock_no": getattr(vehicle, "stock_no", None), "import_request_id": o.import_request_id,
        "age": human_age(o.created_at, now), "created_at": o.created_at.isoformat() if o.created_at else None,
        "stage_age": human_age(o.stage_changed_at or o.created_at, now),
        "owner_user_id": o.owner_user_id,
        "owner": ({"id": owner_user.id, "name": owner_user.display_name or owner_user.handle} if owner_user else None),
        "next_task": nt, "next_action_label": (nt["title"] if nt else (o.next_action or "No next action")),
        "overdue": bool(nt and nt["overdue"]), "source": o.source, "lost_reason": o.lost_reason,
        "converted_kind": o.converted_kind, "converted_id": o.converted_id,
    }


async def next_tasks_for(db, opportunity_ids: list[str]) -> dict[str, Task]:
    """Soonest open task per opportunity (due tasks first, then undated by creation)."""
    if not opportunity_ids:
        return {}
    rows = (await db.execute(select(Task).where(Task.opportunity_id.in_(opportunity_ids),
                                                Task.status.in_(ACTIVE_TASK_STATUSES))
                             .order_by(Task.opportunity_id, Task.due_at.asc().nulls_last(), Task.created_at))).scalars().all()
    out: dict[str, Task] = {}
    for t in rows:
        out.setdefault(t.opportunity_id, t)
    return out


# ── helpers ──────────────────────────────────────────────────────────────────
async def _get(ctx: CommandContext, opportunity_id: str, expected_version: int | None = None) -> Opportunity:
    o = (await ctx.db.execute(select(Opportunity).where(Opportunity.id == opportunity_id).with_for_update())).scalar_one_or_none()
    if o is None:
        raise NotFound("opportunity not found")
    if expected_version is not None and o.version != expected_version:
        raise Conflict("opportunity changed since you loaded it", current_version=o.version)
    return o


def _emit(ctx: CommandContext, o: Opportunity, change: str, **extra) -> None:
    ctx.emit("opportunity.changed", aggregate_type="opportunity", aggregate_id=o.id, aggregate_version=o.version,
             payload={"opportunity_id": o.id, "change": change, "stage": o.stage, "pipeline": o.pipeline,
                      "contact_id": o.contact_id, "vehicle_id": o.vehicle_id, **extra})


def _clear_lost(ctx: CommandContext, o: Opportunity) -> None:
    """Leaving `lost`: the reason moves to extra.lost_history so an active lead never carries a lost reason."""
    o.extra = {**(o.extra or {}), "lost_history": list((o.extra or {}).get("lost_history", [])) + [
        {"reason": o.lost_reason, "lost_at": o.lost_at.isoformat() if o.lost_at else None, "reopened_at": ctx.now.isoformat()}]}
    o.lost_reason, o.lost_at = None, None
    o.reopened_at = ctx.now


def _push_stage(ctx: CommandContext, o: Opportunity, stage: str, note: str | None = None, source: str | None = None) -> None:
    hist = _hist(o)
    hist.append({"from": o.stage, "stage": stage, "at": ctx.now.isoformat(), "by": ctx.actor.user_id,
                 "by_kind": ctx.actor.kind, "by_name": ctx.actor.display_name, "note": note, "source": source})
    o.stage_history = hist
    o.stage = stage
    o.stage_changed_at = ctx.now


async def check_vehicle_for_opportunity(ctx: CommandContext, vehicle_id: str) -> Vehicle:
    """A sold vehicle is never revived by a new inquiry (spec §5.1). Returns a structured hint."""
    v = await ctx.db.get(Vehicle, vehicle_id)
    if v is None:
        raise NotFound("vehicle not found")
    if v.commercial_state in ("sold", "delivered") or v.allocation == "sold":
        title = vehicle_title(v)
        raise Blocked(f"{title} is {v.commercial_state}; a sold vehicle is not revived by a new inquiry. "
                      "Create an IRQ opportunity or link a different vehicle.",
                      hint={"reason": "vehicle_sold", "vehicle_id": v.id, "stock_no": v.stock_no,
                            "commercial_state": v.commercial_state,
                            "options": [{"pipeline": "irq", "label": "Open an import request (IRQ) opportunity"},
                                        {"pipeline": "vehicle", "label": "Link an alternative in-stock vehicle",
                                         "requires": "different vehicle_id"}]})
    return v


def _budget(inp) -> tuple[Decimal | None, str | None]:
    if inp.budget_amount is None:
        return None, inp.budget_currency.upper() if inp.budget_currency else None
    if not inp.budget_currency:
        raise ValidationFailed("budget_currency is required with budget_amount")
    cur = inp.budget_currency.upper()
    return quantize(Decimal(str(inp.budget_amount)), cur), cur


# ── commands ─────────────────────────────────────────────────────────────────
class InlineContactIn(BaseModel):
    name: str = Field(min_length=1)
    company: str | None = None
    roles: list[str] = Field(default_factory=lambda: ["buyer"])
    identities: list[dict] = Field(default_factory=list)
    source: str | None = None
    source_ref: str | None = None
    status: str = "active"


class OpportunityCreateIn(BaseModel):
    contact_id: str | None = None
    contact: InlineContactIn | None = None
    pipeline: str
    vehicle_id: str | None = None
    import_request_id: str | None = None
    enquiry: str = ""
    budget_amount: Decimal | None = None
    budget_currency: str | None = None
    source: str = "manual"
    source_ref: str | None = None         # message/provider ref: repeated ingestion returns the same opportunity
    owner_user_id: str | None = None
    notes: str = ""
    next_action: str | None = None
    stage: str = "new"
    conversation_id: str | None = None
    extra: dict = Field(default_factory=dict)


@command("sales.create_opportunity", input=OpportunityCreateIn, perm="sales.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)] if p.vehicle_id else [],
         description="Create an IRQ or Vehicle Sales opportunity; repeated ingestion returns the existing open one.")
async def sales_create_opportunity(ctx: CommandContext, inp: OpportunityCreateIn) -> dict:
    if inp.pipeline not in PIPELINES:
        raise ValidationFailed(f"pipeline must be one of {PIPELINES}")
    if inp.stage not in OPEN_STAGES:
        raise ValidationFailed(f"a new opportunity starts in one of {OPEN_STAGES}; Deposit Paid comes from payment evidence")
    if inp.pipeline == "vehicle" and not inp.vehicle_id:
        raise ValidationFailed("vehicle pipeline needs vehicle_id (use pipeline irq for an import enquiry)")
    if not inp.contact_id and not inp.contact:
        raise ValidationFailed("contact_id or an inline contact is required")
    amount, currency = _budget(inp)

    if inp.source_ref:
        # The same message replayed returns the same lead. One thread may still open several interests
        # (spec §5.1): a second vehicle from the same message is a separate opportunity, not a replay.
        q = select(Opportunity).where(Opportunity.source_ref == inp.source_ref, Opportunity.pipeline == inp.pipeline)
        if inp.pipeline == "vehicle":
            q = q.where(Opportunity.vehicle_id == inp.vehicle_id)
        existing = (await ctx.db.execute(q.order_by(Opportunity.created_at))).scalars().first()
        if existing is not None:
            return {"opportunity": serialize_opportunity(existing), "created": False, "matched_by": "source_ref"}

    vehicle = await check_vehicle_for_opportunity(ctx, inp.vehicle_id) if inp.vehicle_id else None
    if inp.owner_user_id:
        from ..models import User
        u = await ctx.db.get(User, inp.owner_user_id)
        if u is None or u.status != "active":
            raise ValidationFailed("owner is not an active person")

    contact_created = False
    contact_id = inp.contact_id
    if not contact_id:
        c = inp.contact
        res = await dispatch(ctx.child(), "contacts.create", {
            "name": c.name, "company": c.company, "roles": c.roles, "identities": c.identities,
            "source": c.source or inp.source, "source_ref": c.source_ref, "status": c.status}, commit=False)
        contact_id = res.data["contact"]["id"]
        contact_created = bool(res.data.get("created"))
    else:
        from ..models.contacts import Contact
        c_row = await ctx.db.get(Contact, contact_id)
        if c_row is None:
            raise NotFound("contact not found")
        if c_row.status == "merged" and c_row.merged_into_id:
            contact_id = c_row.merged_into_id
        elif c_row.status == "archived":
            raise Blocked("contact is archived; restore it first")

    key = enquiry_key(inp.enquiry)
    q = select(Opportunity).where(Opportunity.contact_id == contact_id, Opportunity.pipeline == inp.pipeline,
                                  Opportunity.stage.in_(OPEN_STAGES))
    q = q.where(Opportunity.vehicle_id == inp.vehicle_id) if inp.pipeline == "vehicle" else q.where(Opportunity.enquiry_key == key)
    existing = (await ctx.db.execute(q.order_by(Opportunity.created_at))).scalars().first()
    if existing is not None:
        changed = False
        ex = dict(existing.extra or {})
        if inp.source_ref:
            also = list(ex.get("also_from", []))
            if inp.source_ref not in also:
                also.append(inp.source_ref)
                ex["also_from"] = also
                changed = True
        if inp.conversation_id and inp.conversation_id not in (existing.conversation_ids or []):
            existing.conversation_ids = list(existing.conversation_ids or []) + [inp.conversation_id]
            changed = True
        if inp.notes and inp.notes not in (existing.notes or ""):
            existing.notes = (existing.notes + "\n" if existing.notes else "") + inp.notes
            changed = True
        if changed:
            existing.extra = ex
            ctx.touch(existing, "opportunity")
            _emit(ctx, existing, "merged_duplicate")
        return {"opportunity": serialize_opportunity(existing), "created": False, "matched_by": "open_duplicate"}

    o = Opportunity(contact_id=contact_id, pipeline=inp.pipeline, stage=inp.stage, vehicle_id=inp.vehicle_id,
                    import_request_id=inp.import_request_id, enquiry=inp.enquiry or "", enquiry_key=key,
                    budget_amount=amount, budget_currency=currency, source=inp.source, source_ref=inp.source_ref,
                    owner_user_id=inp.owner_user_id, notes=inp.notes or "", next_action=inp.next_action,
                    conversation_ids=[inp.conversation_id] if inp.conversation_id else [], stage_history=[],
                    extra=dict(inp.extra or {}), created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
    _push_stage(ctx, o, inp.stage, note="created", source=inp.source)
    ctx.db.add(o)
    await ctx.db.flush()
    ctx.changed.append({"kind": "opportunity", "id": o.id, "version": o.version})
    ctx.record(f"New {STAGE_LABELS['new'].lower()} ({o.pipeline}): {opportunity_subject(o, vehicle)}",
               entity_kind="opportunity", entity_id=o.id, kind="task", state=o.stage,
               details={"contact_id": contact_id, "vehicle_id": o.vehicle_id, "source": o.source})
    _emit(ctx, o, "created")
    return {"opportunity": serialize_opportunity(o), "created": True, "contact_created": contact_created}


class OpportunityUpdateIn(BaseModel):
    opportunity_id: str
    expected_version: int | None = None
    enquiry: str | None = None
    budget_amount: Decimal | None = None
    budget_currency: str | None = None
    clear_budget: bool = False
    source: str | None = None
    owner_user_id: str | None = None
    unassign: bool = False
    notes: str | None = None
    next_action: str | None = None
    extra: dict | None = None


@command("sales.update_opportunity", input=OpportunityUpdateIn, perm="sales.write", action_class="internal",
         description="Edit enquiry, budget, source, owner, notes or next action (not the stage).")
async def sales_update_opportunity(ctx: CommandContext, inp: OpportunityUpdateIn) -> dict:
    o = await _get(ctx, inp.opportunity_id, inp.expected_version)
    if inp.enquiry is not None:
        o.enquiry = inp.enquiry
        o.enquiry_key = enquiry_key(inp.enquiry)
    if inp.clear_budget:
        o.budget_amount, o.budget_currency = None, None
    elif inp.budget_amount is not None or inp.budget_currency is not None:
        amount, currency = _budget(inp) if inp.budget_amount is not None else (o.budget_amount, inp.budget_currency.upper())
        if inp.budget_amount is not None:
            o.budget_amount = amount
        o.budget_currency = currency
    if inp.source is not None:
        o.source = inp.source
    if inp.unassign:
        o.owner_user_id = None
    elif inp.owner_user_id is not None:
        from ..models import User
        u = await ctx.db.get(User, inp.owner_user_id)
        if u is None or u.status != "active":
            raise ValidationFailed("owner is not an active person")
        o.owner_user_id = inp.owner_user_id
    if inp.notes is not None:
        o.notes = inp.notes
    if inp.next_action is not None:
        o.next_action = inp.next_action or None
    if inp.extra is not None:
        o.extra = {**(o.extra or {}), **inp.extra}
    ctx.touch(o, "opportunity")
    ctx.record(f"Updated opportunity: {o.enquiry or o.id[:8]}", entity_kind="opportunity", entity_id=o.id, kind="task", state=o.stage)
    _emit(ctx, o, "updated")
    return {"opportunity": serialize_opportunity(o)}


class MoveStageIn(BaseModel):
    opportunity_id: str
    stage: str
    expected_version: int | None = None
    note: str | None = None
    reason: str | None = None     # required when stage == lost


@command("sales.move_stage", input=MoveStageIn, perm="sales.write", action_class="internal",
         description="Move a lead between New / In Conversation / Awaiting Deposit / Lost. Deposit Paid is evidence-backed.")
async def sales_move_stage(ctx: CommandContext, inp: MoveStageIn) -> dict:
    if inp.stage == "deposit_paid":
        raise Blocked(DEPOSIT_PAID_BLOCK_REASON, reason=DEPOSIT_PAID_BLOCK_REASON, action="record_payment",
                      opportunity_id=inp.opportunity_id)
    if inp.stage not in HAND_SETTABLE_STAGES:
        raise ValidationFailed(f"stage must be one of {HAND_SETTABLE_STAGES}")
    o = await _get(ctx, inp.opportunity_id, inp.expected_version)
    if o.stage == "deposit_paid":
        raise Blocked("Deposit Paid is evidence-backed; refunds/disputes are recorded in Finance and reopen it there",
                      reason="converted", converted_kind=o.converted_kind, converted_id=o.converted_id)
    if o.stage == inp.stage:
        return {"opportunity": serialize_opportunity(o), "moved": False}
    if inp.stage == "lost":
        if not (inp.reason or "").strip():
            raise ValidationFailed("a lost reason is required")
        o.lost_reason = inp.reason.strip()
        o.lost_at = ctx.now
    elif o.stage == "lost":
        _clear_lost(ctx, o)   # dragging a lost lead back onto the board is a reopen: the lost reason stays in history
    was = o.stage
    _push_stage(ctx, o, inp.stage, note=inp.note or inp.reason)
    ctx.touch(o, "opportunity")
    ctx.record(f"Lead moved {STAGE_LABELS.get(was, was)} → {STAGE_LABELS.get(o.stage, o.stage)}",
               entity_kind="opportunity", entity_id=o.id, kind="task", state=o.stage, details={"note": inp.note, "reason": inp.reason})
    _emit(ctx, o, "stage", from_stage=was)
    return {"opportunity": serialize_opportunity(o), "moved": True}


class LostIn(BaseModel):
    opportunity_id: str
    reason: str = Field(min_length=1)
    expected_version: int | None = None


@command("sales.mark_lost", input=LostIn, perm="sales.write", action_class="internal",
         description="Mark a lead Lost / Not moving forward with a reason.")
async def sales_mark_lost(ctx: CommandContext, inp: LostIn) -> dict:
    return await sales_move_stage(ctx, MoveStageIn(opportunity_id=inp.opportunity_id, stage="lost",
                                                   expected_version=inp.expected_version, reason=inp.reason))


class ReopenIn(BaseModel):
    opportunity_id: str
    expected_version: int | None = None
    stage: str | None = None      # default: the stage it was in before it was lost
    note: str | None = None


@command("sales.reopen", input=ReopenIn, perm="sales.write", action_class="internal",
         description="Reopen a lost lead into an active stage; the lost reason stays in history.")
async def sales_reopen(ctx: CommandContext, inp: ReopenIn) -> dict:
    o = await _get(ctx, inp.opportunity_id, inp.expected_version)
    if o.stage != "lost":
        raise Blocked("only a lost lead can be reopened", stage=o.stage)
    target = inp.stage
    if target is None:
        prev = [h.get("from") for h in reversed(_hist(o)) if h.get("stage") == "lost"]
        target = prev[0] if prev and prev[0] in OPEN_STAGES else "new"
    if target not in OPEN_STAGES:
        raise ValidationFailed(f"stage must be one of {OPEN_STAGES}")
    _clear_lost(ctx, o)
    _push_stage(ctx, o, target, note=inp.note or "reopened")
    ctx.touch(o, "opportunity")
    ctx.record(f"Lead reopened → {STAGE_LABELS[target]}", entity_kind="opportunity", entity_id=o.id, kind="task", state=o.stage)
    _emit(ctx, o, "reopened")
    return {"opportunity": serialize_opportunity(o)}


class LinkVehicleIn(BaseModel):
    opportunity_id: str
    vehicle_id: str | None
    expected_version: int | None = None


@command("sales.link_vehicle", input=LinkVehicleIn, perm="sales.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)] if p.vehicle_id else [],
         description="Link (or unlink) the vehicle a lead is about; sold vehicles cannot be linked.")
async def sales_link_vehicle(ctx: CommandContext, inp: LinkVehicleIn) -> dict:
    o = await _get(ctx, inp.opportunity_id, inp.expected_version)
    if o.vehicle_id == inp.vehicle_id:
        return {"opportunity": serialize_opportunity(o), "linked": False}
    if o.converted_id:
        raise Blocked("this lead is converted; its vehicle is fixed by the sale/reservation record",
                      converted_kind=o.converted_kind, converted_id=o.converted_id)
    if inp.vehicle_id:
        await check_vehicle_for_opportunity(ctx, inp.vehicle_id)
    elif o.pipeline == "vehicle":
        raise ValidationFailed("a Vehicle Sales lead needs a vehicle; move it to the IRQ pipeline instead")
    o.vehicle_id = inp.vehicle_id
    ctx.touch(o, "opportunity")
    ctx.record("Linked vehicle to lead" if inp.vehicle_id else "Unlinked vehicle from lead", entity_kind="opportunity",
               entity_id=o.id, kind="task", state=o.stage, details={"vehicle_id": inp.vehicle_id})
    _emit(ctx, o, "vehicle_linked")
    return {"opportunity": serialize_opportunity(o), "linked": True}


class LinkRequestIn(BaseModel):
    opportunity_id: str
    import_request_id: str | None
    expected_version: int | None = None


@command("sales.link_request", input=LinkRequestIn, perm="sales.write", action_class="internal",
         description="Link (or unlink) the pre-deposit ImportRequest projected by this IRQ lead.")
async def sales_link_request(ctx: CommandContext, inp: LinkRequestIn) -> dict:
    o = await _get(ctx, inp.opportunity_id, inp.expected_version)
    if o.import_request_id == inp.import_request_id:
        return {"opportunity": serialize_opportunity(o), "linked": False}
    if o.converted_kind == "import_request" and o.converted_id and inp.import_request_id != o.converted_id:
        raise Conflict("this lead already converted into an import request; the link cannot be changed (invariant 6)",
                       converted_id=o.converted_id, requested_id=inp.import_request_id)
    if inp.import_request_id:
        other = (await ctx.db.execute(select(Opportunity).where(Opportunity.import_request_id == inp.import_request_id,
                                                                Opportunity.id != o.id))).scalars().first()
        if other is not None:
            raise Conflict("that import request is already linked to another opportunity", opportunity_id=other.id)
    o.import_request_id = inp.import_request_id
    ctx.touch(o, "opportunity")
    ctx.record("Linked import request to lead" if inp.import_request_id else "Unlinked import request",
               entity_kind="opportunity", entity_id=o.id, kind="task", state=o.stage,
               details={"import_request_id": inp.import_request_id})
    _emit(ctx, o, "request_linked")
    return {"opportunity": serialize_opportunity(o)}


class LinkConversationIn(BaseModel):
    opportunity_id: str
    conversation_id: str
    expected_version: int | None = None


@command("sales.link_conversation", input=LinkConversationIn, perm="sales.write", action_class="internal",
         description="Link an email thread to a lead; one thread may link to several interests.")
async def sales_link_conversation(ctx: CommandContext, inp: LinkConversationIn) -> dict:
    o = await _get(ctx, inp.opportunity_id, inp.expected_version)
    if inp.conversation_id in (o.conversation_ids or []):
        return {"opportunity": serialize_opportunity(o), "linked": False}
    o.conversation_ids = list(o.conversation_ids or []) + [inp.conversation_id]
    ctx.touch(o, "opportunity")
    ctx.record("Linked conversation to lead", entity_kind="opportunity", entity_id=o.id, kind="task", state=o.stage,
               details={"conversation_id": inp.conversation_id})
    _emit(ctx, o, "conversation_linked")
    return {"opportunity": serialize_opportunity(o), "linked": True}


class SetConversionIn(BaseModel):
    opportunity_id: str
    converted_kind: str              # import_request | sale
    converted_id: str
    source_ref: str | None = None    # payment / provider event that confirmed the deposit
    deposit_payment_id: str | None = None
    confirmed_at: datetime | None = None
    expected_version: int | None = None


@command("sales.set_conversion", input=SetConversionIn, perm="finance.write", action_class="internal",
         description="Deposit handoff: record the single conversion link and the evidence-backed Deposit Paid stage. "
                     "Idempotent for the same target; a different target conflicts (invariant 6).")
async def sales_set_conversion(ctx: CommandContext, inp: SetConversionIn) -> dict:
    if inp.converted_kind not in CONVERSION_KINDS:
        raise ValidationFailed(f"converted_kind must be one of {CONVERSION_KINDS}")
    o = await _get(ctx, inp.opportunity_id, inp.expected_version)
    if o.converted_id:
        if o.converted_kind == inp.converted_kind and o.converted_id == inp.converted_id:
            return {"opportunity": serialize_opportunity(o), "converted": False, "idempotent": True}
        raise Conflict("opportunity already has a conversion link", converted_kind=o.converted_kind,
                       converted_id=o.converted_id, requested_kind=inp.converted_kind, requested_id=inp.converted_id)
    other = (await ctx.db.execute(select(Opportunity).where(Opportunity.converted_id == inp.converted_id,
                                                            Opportunity.id != o.id))).scalars().first()
    if other is not None:
        raise Conflict("that record is already the conversion of another opportunity", opportunity_id=other.id)
    o.converted_kind = inp.converted_kind
    o.converted_id = inp.converted_id
    o.converted_at = ctx.now
    o.conversion_source_ref = inp.source_ref
    o.deposit_payment_id = inp.deposit_payment_id or o.deposit_payment_id
    o.deposit_confirmed_at = ensure_aware(inp.confirmed_at) or ctx.now
    if inp.converted_kind == "import_request" and not o.import_request_id:
        o.import_request_id = inp.converted_id
    if o.stage != "deposit_paid":
        _push_stage(ctx, o, "deposit_paid", note="deposit confirmed from payment evidence", source=inp.source_ref)
    ctx.touch(o, "opportunity")
    ctx.record(f"Deposit Paid: converted to {inp.converted_kind}", entity_kind="opportunity", entity_id=o.id, kind="payment",
               state="deposit_paid", sources=[inp.source_ref] if inp.source_ref else None,
               details={"converted_kind": inp.converted_kind, "converted_id": inp.converted_id,
                        "deposit_payment_id": inp.deposit_payment_id})
    _emit(ctx, o, "converted", converted_kind=inp.converted_kind, converted_id=inp.converted_id)
    return {"opportunity": serialize_opportunity(o), "converted": True}
