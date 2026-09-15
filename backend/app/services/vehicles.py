"""Vehicles domain (spec §2.3 Vehicles / Vehicle detail, §3.1 Vehicle, §3.2 facts, §2.4 milestones).

Independent logistics / recon / commercial / documents states; field-level facts with provenance
(reported / inferred / confirmed / estimated / conflicted / outdated / unknown); planned / estimated /
completed milestones with a source; an editable, versioned "Condition at intake" summary; and a
deterministic health projection recomputed after every command and on `task.changed` events.

Recon stage changes are NOT made here: `shop.move_stage` validates the gates (services/shop.py).
Critical facts (odometer, frame number, title status, purchase amount) are proposed by anyone with
vehicles.write and confirmed only by the owner (`vehicles.confirm_fact`).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field
from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import Blocked, Conflict, Denied, NotFound, ValidationFailed
from ..core.ids import human_ref, new_id, sha256_hex
from ..core.money import parse_amount, quantize
from ..core.time import PHOENIX, ensure_aware
from ..domain.access import can_see_costs, sanitize_money
from ..domain.actors import Actor
from ..domain.commands import CommandContext, command, dispatch
from ..domain.events import on_event
from ..models.assets import Asset, AssetLink
from ..models.legacy import Setting
from ..models.tasks import Task
from ..models.vehicles import (COMMERCIAL_STATES, DOCUMENT_STATES, LOGISTICS_STATES, MILESTONE_KINDS, RECON_STATES,
                               Part, ReconIssue, Vehicle, VehicleFact, VehicleMilestone, WorkOrder)

STOCK_SEQ_KEY = "vehicle_stock_sequence"
ACTIVE_TASK = ("open", "in_progress", "blocked", "waiting", "awaiting_verification")
VIEWS = ("all", "sourcing", "shipping", "shop", "sales")
CRITICAL_FACT_KEYS = ("odometer_km", "frame_no", "title_status", "purchase_amount")
FACT_COLUMNS = {
    "title": "title", "make": "make", "model": "model", "model_year": "model_year", "color": "color", "grade": "grade",
    "location": "location", "odometer_km": "odometer_km", "frame_no": "frame_no_raw", "title_status": "title_status",
    "purchase_amount": "purchase_amount", "acquired_at": "acquired_at",
}
NUMERIC_KEYS = {"odometer_km", "model_year", "purchase_amount"}
IDENTITY_REQUIRED = ("frame_no", "model_year", "purchase_amount")
MONEY_KEYS = ("purchase_amount", "purchase_currency", "landed_cost_usd", "sold_price_usd", "cost_total",
              "cost_items", "cost_allocations")
CONDITION_SOURCES = ("owner_reported", "image_observed", "proposed_check", "inspection", "customer")
MILESTONE_STATUSES = ("planned", "estimated", "completed")
# Recon milestones mirror the gated shop stage (spec §8.4, invariant 10): a completed one is written by the
# shop command that checked the gates, never by hand — otherwise inspected_at / ready_at could turn a factual
# gate green without an inspection, verified recon work, photos or disclosures. Forecasts stay allowed.
SHOP_OWNED_MILESTONES = {"inspected": "shop.log_inspection", "recon_started": "shop.move_stage", "ready": "shop.move_stage"}
MILESTONE_COLUMNS = {"purchased": "acquired_at", "received": "received_at", "inspected": "inspected_at",
                     "ready": "ready_at", "listed": "listed_at", "reserved": "reserved_at", "sold": "sold_at",
                     "delivered": "delivered_at"}
# logistics milestones imply the matching logistics state (forward only)
MILESTONE_LOGISTICS = {"purchased": "purchased", "export_cleared": "export_pending", "on_vessel": "on_vessel",
                       "arrived_port": "at_port", "released": "released", "received": "received"}
STATE_LABELS = {
    "candidate": "Candidate", "purchased": "Purchased", "export_pending": "Export pending", "on_vessel": "On vessel",
    "at_port": "At port", "released": "Released", "domestic_transit": "Domestic transit", "received": "Received",
    "not_applicable": "No shipment", "needs_inspection": "Needs inspection", "in_recon": "In recon",
    "finalization": "Finalization", "ready_for_sale": "Ready for sale", "not_listed": "Not listed", "listed": "Listed",
    "reserved": "Reserved", "sold": "Sold", "delivered": "Delivered", "pending": "Documents pending",
    "complete": "Documents complete", "conflicted": "Documents conflicted", "missing": "Documents missing",
}
SHIPPING_STATES = ("purchased", "export_pending", "on_vessel", "at_port", "released", "domestic_transit")

# Spec §10.9 coverage map: owner UI action -> the command Manager calls. Every owner-editable field in this
# slice has a command route; the agent runtime reads this to build its tool list.
COMMANDS_FOR_MANAGER: dict[str, str] = {
    "vehicles.create_card": "vehicles.create",
    "vehicles.edit_card": "vehicles.update",
    "vehicles.set_states": "vehicles.set_states",
    "vehicles.record_milestone": "vehicles.record_milestone",
    "vehicles.propose_fact": "vehicles.propose_fact",
    "vehicles.confirm_fact": "vehicles.confirm_fact",
    "vehicles.set_condition": "vehicles.set_condition",
    "vehicles.edit_condition_bullet": "vehicles.edit_condition_bullet",
    "vehicles.archive": "vehicles.archive",
    "vehicles.restore": "vehicles.restore",
    "vehicles.set_asking_price": "vehicles.set_asking_price",
    "shop.move_stage": "shop.move_stage",
    "shop.log_inspection": "shop.log_inspection",
    "shop.create_issue": "shop.create_issue",
    "shop.resolve_issue": "shop.resolve_issue",
    "shop.defer_issue": "shop.defer_issue",
    "shop.create_work_order": "shop.create_work_order",
    "shop.request_part": "shop.request_part",
    "shop.order_part": "shop.order_part",
    "shop.mark_part_arrived": "shop.mark_part_arrived",
    "shop.mark_part_installed": "shop.mark_part_installed",
    "shop.verify_part": "shop.verify_part",
    "shop.record_part_payment": "shop.record_part_payment",
    "shop.cancel_part": "shop.cancel_part",
    "assets.attach_photo": "assets.link",
    "assets.classify": "assets.classify",
    "assets.remove_link": "assets.remove_link",
    "intake.start": "intake.start",
    "intake.add_assets": "intake.add_assets",
    "intake.add_note": "intake.add_note",
    "intake.add_transcript": "intake.add_transcript",
    "intake.analyze": "intake.analyze",
    "intake.choose_vehicle": "intake.choose_vehicle",
    "intake.apply": "intake.apply",
    "intake.correct": "intake.correct",
    "intake.undo": "intake.undo",
    "intake.abandon": "intake.abandon",
    "tasks.create": "tasks.create",
    "tasks.update": "tasks.update",
    "tasks.assign": "tasks.assign",
    "tasks.reschedule": "tasks.reschedule",
    "tasks.complete": "tasks.complete",
    "tasks.attach_evidence": "tasks.attach_evidence",
    "tasks.verify": "tasks.verify",
    "tasks.reject_evidence": "tasks.reject_evidence",
    "tasks.cancel": "tasks.cancel",
    "tasks.report_blocker": "tasks.report_blocker",
}


# ── helpers ──────────────────────────────────────────────────────────────────
def normalize_frame(raw: str | None) -> str | None:
    """Uppercase, strip spaces/punctuation only; raw is preserved separately (spec §3.1)."""
    if not raw:
        return None
    v = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    return v or None


def normalize_stock_no(raw: str | None) -> str | None:
    if not raw:
        return None
    v = re.sub(r"[\s_]", "", raw).upper()
    m = re.fullmatch(r"(STK)-?(\d{1,8})", v)
    if m:
        return f"STK-{int(m.group(2)):04d}"
    return v or None


def norm_text(s: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (s or "").lower())).strip()


def iso(dt: datetime | None) -> str | None:
    return ensure_aware(dt).isoformat() if dt else None


def loaded(row, name: str):
    """Read a column without triggering a lazy load (server-side `updated_at` is expired after an UPDATE flush;
    an implicit load inside the async session would fail). Unloaded -> None; recompute() refreshes it."""
    return row.__dict__.get(name)


def money_str(d: Decimal | None) -> str | None:
    return None if d is None else str(d)


def aware(dt: datetime | None, tz: str | None = None) -> datetime | None:
    """Naive inputs are interpreted in `tz` (IANA, default Phoenix); never stored naive."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        try:
            dt = dt.replace(tzinfo=ZoneInfo(tz or PHOENIX))
        except (ZoneInfoNotFoundError, ValueError):
            raise ValidationFailed(f"unknown timezone {tz!r}; use an IANA name like America/Phoenix")
    return dt.astimezone(timezone.utc)


def vehicle_title(v: Vehicle) -> str:
    parts = [str(v.model_year) if v.model_year else None, v.make, v.model, v.color]
    base = " ".join(p for p in parts if p) or (v.title or "")
    return base or (v.stock_no or v.id[:8])


async def get_vehicle(db: AsyncSession, vehicle_id: str, *, expected_version: int | None = None,
                      lock: bool = True) -> Vehicle:
    q = select(Vehicle).where(Vehicle.id == vehicle_id)
    if lock:
        q = q.with_for_update()
    v = (await db.execute(q)).scalar_one_or_none()
    if v is None:
        raise NotFound("vehicle not found")
    if expected_version is not None and v.version != expected_version:
        raise Conflict("vehicle changed since you loaded it", current_version=v.version)
    return v


async def allocate_stock_no(db: AsyncSession, now: datetime, actor_id: str | None) -> tuple[str, int]:
    """STK-#### from a sequence kept in the settings table; never reuses a number already on a card."""
    row = (await db.execute(select(Setting).where(Setting.key == STOCK_SEQ_KEY).with_for_update())).scalar_one_or_none()
    if row is None:
        mx = 0
        for (s,) in (await db.execute(select(Vehicle.stock_no).where(Vehicle.stock_no.is_not(None)))).all():
            m = re.fullmatch(r"STK-?(\d+)", (s or "").strip().upper())
            if m:
                mx = max(mx, int(m.group(1)))
        row = Setting(key=STOCK_SEQ_KEY, value={"next": mx + 1}, updated_at=now, updated_by=actor_id)
        db.add(row)
        await db.flush()
    n = int((row.value or {}).get("next") or 1)
    while True:
        cand = human_ref("STK", n)
        taken = await db.scalar(select(func.count()).select_from(Vehicle).where(Vehicle.stock_no == cand))
        if not taken:
            break
        n += 1
    row.value = {"next": n + 1}
    row.updated_at = now
    row.updated_by = actor_id
    return cand, n


def missing_identity(v: Vehicle) -> list[str]:
    out = []
    if not v.frame_no_raw:
        out.append("frame_no")
    if not v.model_year:
        out.append("model_year")
    if v.purchase_amount is None:
        out.append("purchase_amount")
    return out


def situation_of(v: Vehicle) -> str:
    parts = []
    if v.logistics_state not in ("received", "not_applicable"):
        parts.append(STATE_LABELS.get(v.logistics_state, v.logistics_state))
    parts.append(STATE_LABELS.get(v.recon_state, v.recon_state))
    if v.commercial_state != "not_listed":
        parts.append(STATE_LABELS.get(v.commercial_state, v.commercial_state))
    if v.documents_state in ("conflicted", "missing"):
        parts.append(STATE_LABELS.get(v.documents_state, v.documents_state))
    return " · ".join(parts)


def view_of(v: Vehicle) -> str:
    if v.logistics_state == "candidate" or v.allocation == "candidate":
        return "sourcing"
    if v.logistics_state in SHIPPING_STATES:
        return "shipping"
    if v.recon_state != "ready_for_sale":
        return "shop"
    return "sales"


def view_clause(view: str):
    if view == "sourcing":
        return or_(Vehicle.logistics_state == "candidate", Vehicle.allocation == "candidate")
    if view == "shipping":
        return Vehicle.logistics_state.in_(SHIPPING_STATES)
    if view == "shop":
        return (Vehicle.logistics_state.in_(("received", "not_applicable")) & (Vehicle.recon_state != "ready_for_sale")
                & (Vehicle.allocation != "candidate"))
    if view == "sales":
        return (Vehicle.recon_state == "ready_for_sale") & Vehicle.logistics_state.in_(("received", "not_applicable"))
    return None


# ── health projection (deterministic; spec §3.1 "calculated health") ─────────
async def _publish_failed(db: AsyncSession, vehicle_id: str) -> bool:
    try:
        from ..models.listings import Publication
        n = await db.scalar(select(func.count()).select_from(Publication).where(
            Publication.vehicle_id == vehicle_id, Publication.state == "failed"))
        return bool(n)
    except Exception:  # noqa: BLE001 - listings domain may not be present yet
        return False


async def recompute(db: AsyncSession, v: Vehicle, now: datetime | None = None) -> dict:
    """Derive health / next action / situation from open tasks and states. Derived columns only: the
    row version is not bumped so optimistic edits are not invalidated by a projection refresh."""
    now = ensure_aware(now) or datetime.now(timezone.utc)
    tasks = (await db.execute(select(Task).where(Task.vehicle_id == v.id, Task.status.in_(ACTIVE_TASK))
                              .order_by(Task.due_at.asc().nulls_last(), Task.created_at))).scalars().all()
    reasons: list[str] = []
    health = "ok"
    blocked = [t for t in tasks if t.status == "blocked"]
    overdue = [t for t in tasks if t.due_at and ensure_aware(t.due_at) < now and t.status != "awaiting_verification"]
    unassigned = [t for t in tasks if not t.owner_user_id and not t.is_suggestion and t.status in ("open", "blocked")]
    awaiting = [t for t in tasks if t.status == "awaiting_verification"]
    waiting = [t for t in tasks if t.status == "waiting"]
    docs_deadline = any(t.gate_requirement == "docs_complete" and t.due_at for t in tasks) or \
        v.commercial_state in ("reserved", "sold")
    if blocked:
        health = "blocked"
        reasons.append(f"blocked: {blocked[0].title}" + (f" — {blocked[0].block_reason}" if blocked[0].block_reason else ""))
    if v.documents_state == "conflicted":
        health = "blocked"
        reasons.append("documents conflicted")
    if v.documents_state == "missing" and docs_deadline:
        health = "blocked"
        reasons.append("documents missing with a deadline")
    if health != "blocked":
        if overdue:
            health = "risk"
            reasons.append(f"overdue: {overdue[0].title}")
        if unassigned:
            health = "risk"
            reasons.append(f"unassigned work: {unassigned[0].title}")
        if v.documents_state == "missing":
            health = "risk"
            reasons.append("documents missing")
        if await _publish_failed(db, v.id):
            health = "risk"
            reasons.append("publication failed")
    if health == "ok":
        future_check = [t for t in tasks if t.next_check_at and ensure_aware(t.next_check_at) > now]
        actionable = [t for t in tasks if t.status in ("open", "in_progress")
                      and not (t.next_check_at and ensure_aware(t.next_check_at) > now)]
        if awaiting:
            health = "wait"
            reasons.append(f"awaiting verification: {awaiting[0].title}")
        elif (waiting or future_check) and not actionable:
            health = "wait"
            nxt = (waiting or future_check)[0]
            reasons.append(f"waiting: {nxt.title}")
    nxt_task = None
    for pool in (blocked, overdue, [t for t in tasks if t.status in ("open", "in_progress")], awaiting, waiting):
        if pool:
            nxt_task = pool[0]
            break
    v.health = health
    v.health_reason = "; ".join(reasons) or None
    v.next_action = nxt_task.title if nxt_task else None
    v.next_action_owner_id = nxt_task.owner_user_id if nxt_task else None
    v.next_action_due_at = ensure_aware(nxt_task.due_at) if nxt_task and nxt_task.due_at else None
    v.situation = situation_of(v)
    v.exception_summary = reasons[0] if health in ("blocked", "risk") else None
    await db.flush()
    await db.refresh(v, attribute_names=["updated_at"])
    return {"health": health, "health_reason": v.health_reason, "next_action": v.next_action,
            "next_action_owner_id": v.next_action_owner_id, "next_action_due_at": iso(v.next_action_due_at),
            "situation": v.situation, "exception": v.exception_summary}


@on_event("task.changed")
async def _on_task_changed(db: AsyncSession, ev) -> None:
    vid = (ev.payload or {}).get("vehicle_id")
    if not vid:
        return
    v = await db.get(Vehicle, vid)
    if v is not None:
        await recompute(db, v)


# ── serializers ──────────────────────────────────────────────────────────────
def serialize_fact(f: VehicleFact) -> dict:
    return {"id": f.id, "version": f.version, "vehicle_id": f.vehicle_id, "key": f.key, "value": f.value,
            "value_num": money_str(f.value_num), "unit": f.unit, "currency": f.currency, "status": f.status,
            "observed_at": iso(f.observed_at), "effective_at": iso(f.effective_at), "source_kind": f.source_kind,
            "source_ref": f.source_ref, "actor": f.actor, "confidence_method": f.confidence_method,
            "supersedes_id": f.supersedes_id, "conflict_with_id": f.conflict_with_id, "visibility": f.visibility,
            "is_current": bool(f.is_current), "critical": f.key in CRITICAL_FACT_KEYS, "created_at": iso(f.created_at)}


def serialize_milestone(m: VehicleMilestone) -> dict:
    return {"id": m.id, "version": m.version, "vehicle_id": m.vehicle_id, "kind": m.kind, "status": m.status,
            "at": iso(m.at), "date_label": "Not recorded" if not m.at else None, "source_kind": m.source_kind,
            "source_ref": m.source_ref, "actor_id": m.actor_id, "note": m.note, "supersedes_id": m.supersedes_id,
            "is_current": bool(m.is_current), "shipment_id": m.shipment_id, "created_at": iso(m.created_at)}


def serialize_issue(i: ReconIssue) -> dict:
    return {"id": i.id, "version": i.version, "vehicle_id": i.vehicle_id, "title": i.title, "detail": i.detail,
            "severity": i.severity, "status": i.status, "source_kind": i.source_kind, "source_ref": i.source_ref,
            "intake_observation_id": i.intake_observation_id, "asset_ids": list(i.asset_ids or []),
            "work_order_id": i.work_order_id, "task_id": i.task_id, "disclosure_required": bool(i.disclosure_required),
            "resolved_at": iso(i.resolved_at), "resolved_by": i.resolved_by, "resolution_note": i.resolution_note,
            "deferred_at": iso(i.deferred_at), "deferred_reason": i.deferred_reason, "created_at": iso(i.created_at)}


def serialize_work_order(w: WorkOrder) -> dict:
    return {"id": w.id, "version": w.version, "vehicle_id": w.vehicle_id, "ref": w.ref, "title": w.title,
            "status": w.status, "assignee_user_id": w.assignee_user_id, "recon_issue_id": w.recon_issue_id,
            "cost_item_id": w.cost_item_id, "notes": w.notes, "created_at": iso(w.created_at)}


def serialize_part(p: Part) -> dict:
    return {"id": p.id, "version": p.version, "vehicle_id": p.vehicle_id, "work_order_id": p.work_order_id,
            "name": p.name, "part_no": p.part_no, "vendor_contact_id": p.vendor_contact_id, "quantity": p.quantity,
            "state": p.state, "payment_state": p.payment_state, "requested_by": p.requested_by, "order_ref": p.order_ref,
            "ordered_at": iso(p.ordered_at), "arrived_at": iso(p.arrived_at), "installed_at": iso(p.installed_at),
            "verified_at": iso(p.verified_at), "verified_by": p.verified_by, "paid_at": iso(p.paid_at),
            "cost_item_id": p.cost_item_id, "approval_id": p.approval_id, "external_action_id": p.external_action_id,
            "evidence": list(p.evidence or []), "notes": p.notes, "history": list(p.history or []),
            "physical_label": {"requested": "Requested", "approved": "Approved", "ordered": "Ordered", "arrived": "Arrived",
                               "installed": "Installed", "verified": "Verified", "cancelled": "Cancelled",
                               "returned": "Returned"}.get(p.state, p.state),
            "created_at": iso(p.created_at)}


def current_bullets(v: Vehicle) -> list[dict]:
    return [b for b in (v.condition_summary or []) if not b.get("removed")]


def serialize_vehicle(v: Vehicle) -> dict:
    """Full record (raw money; callers sanitize with sanitize_money for actors without costs.read)."""
    return {
        "id": v.id, "version": v.version, "stock_no": v.stock_no, "title": vehicle_title(v), "title_raw": v.title,
        "frame_no_raw": v.frame_no_raw, "frame_no_norm": v.frame_no_norm, "make": v.make, "model": v.model,
        "model_year": v.model_year, "color": v.color, "grade": v.grade, "odometer_km": v.odometer_km,
        "title_status": v.title_status, "intake_status": v.intake_status,
        "missing_identity_fields": list(v.missing_identity_fields or []),
        "allocation": v.allocation, "buyer_contact_id": v.buyer_contact_id,
        "states": {"logistics": v.logistics_state, "recon": v.recon_state, "commercial": v.commercial_state,
                   "documents": v.documents_state},
        "state_labels": {"logistics": STATE_LABELS.get(v.logistics_state, v.logistics_state),
                         "recon": STATE_LABELS.get(v.recon_state, v.recon_state),
                         "commercial": STATE_LABELS.get(v.commercial_state, v.commercial_state),
                         "documents": STATE_LABELS.get(v.documents_state, v.documents_state)},
        "logistics_state": v.logistics_state, "recon_state": v.recon_state, "commercial_state": v.commercial_state,
        "documents_state": v.documents_state, "health": v.health, "health_reason": v.health_reason,
        "situation": v.situation or situation_of(v), "exception": v.exception_summary,
        "next_action": v.next_action, "next_action_owner_id": v.next_action_owner_id,
        "next_action_due_at": iso(v.next_action_due_at), "location": v.location,
        "condition": current_bullets(v), "condition_version": v.condition_version,
        "condition_history": list(v.condition_history or []),
        "dates": {k: iso(getattr(v, k)) for k in ("acquired_at", "received_at", "inspected_at", "ready_at",
                                                    "listed_at", "reserved_at", "sold_at", "delivered_at")},
        "purchase_amount": money_str(v.purchase_amount), "purchase_currency": v.purchase_currency,
        "landed_cost_usd": v.landed_cost_usd, "sold_price_usd": v.sold_price_usd,
        "asking_price": money_str(v.asking_price), "asking_currency": v.asking_currency,
        "price_approved_at": iso(v.price_approved_at), "origin_candidate_id": v.origin_candidate_id,
        "purchase_evidence": dict(v.purchase_evidence or {}), "active_sale_id": v.active_sale_id,
        "hero_asset_id": v.hero_asset_id, "photo": "No photo yet" if not v.hero_asset_id else None,
        "photo_requirements": list(v.photo_requirements or []), "disclosures": list(v.disclosures or []),
        "state_history": list(v.state_history or []), "archived_at": iso(v.archived_at), "notes": v.notes,
        "view": view_of(v), "created_at": iso(loaded(v, "created_at")), "updated_at": iso(loaded(v, "updated_at")),
        "extra": dict(v.extra or {}),
    }


def serialize_list_item(v: Vehicle) -> dict:
    return {
        "id": v.id, "version": v.version, "stock_no": v.stock_no, "title": vehicle_title(v), "frame_no_raw": v.frame_no_raw,
        "make": v.make, "model": v.model, "model_year": v.model_year, "color": v.color,
        "photo": {"asset_id": v.hero_asset_id, "thumb": f"/api/assets/{v.hero_asset_id}/thumb"} if v.hero_asset_id else None,
        "photo_status": None if v.hero_asset_id else "No photo yet",
        "allocation": v.allocation, "situation": v.situation or situation_of(v),
        "states": {"logistics": v.logistics_state, "recon": v.recon_state, "commercial": v.commercial_state,
                   "documents": v.documents_state},
        "next_action": v.next_action, "next_action_owner_id": v.next_action_owner_id,
        "next_action_due_at": iso(v.next_action_due_at), "exception": v.exception_summary, "health": v.health,
        "health_reason": v.health_reason, "intake_status": v.intake_status,
        "missing_identity_fields": list(v.missing_identity_fields or []), "location": v.location,
        "view": view_of(v), "archived_at": iso(v.archived_at), "updated_at": iso(loaded(v, "updated_at")),
    }


def sanitize_vehicle(actor: Actor, payload: dict) -> dict:
    return sanitize_money(actor, payload, MONEY_KEYS)


def sanitize_command_result(actor: Actor, envelope: dict) -> dict:
    """Strip owner-only money from a CommandResult envelope before it leaves any router (vehicles, shop, intake).
    Handlers return the full record; actors without costs.read never see amounts or money facts."""
    data = envelope.get("data")
    if isinstance(data, dict):
        if isinstance(data.get("vehicle"), dict):
            data["vehicle"] = sanitize_vehicle(actor, data["vehicle"])
        if not can_see_costs(actor):
            if isinstance(data.get("facts"), list):
                data["facts"] = [f for f in data["facts"] if isinstance(f, dict)
                                 and f.get("key") not in ("purchase_amount", "asking_price") and f.get("visibility") != "owner"]
            if isinstance(data.get("fact"), dict) and (data["fact"].get("key") in ("purchase_amount", "asking_price")
                                                       or data["fact"].get("visibility") == "owner"):
                data["fact"] = {"id": data["fact"].get("id"), "key": data["fact"].get("key"), "money_hidden": True}
    return envelope


async def facts_of(db: AsyncSession, vehicle_id: str, *, include_history: bool = False) -> list[VehicleFact]:
    q = select(VehicleFact).where(VehicleFact.vehicle_id == vehicle_id)
    if not include_history:
        q = q.where(or_(VehicleFact.is_current.is_(True), VehicleFact.status == "conflicted"))
    return list((await db.execute(q.order_by(VehicleFact.key, VehicleFact.created_at))).scalars().all())


async def milestones_of(db: AsyncSession, vehicle_id: str, *, include_history: bool = False) -> list[VehicleMilestone]:
    q = select(VehicleMilestone).where(VehicleMilestone.vehicle_id == vehicle_id)
    if not include_history:
        q = q.where(VehicleMilestone.is_current.is_(True))
    return list((await db.execute(q.order_by(VehicleMilestone.at.asc().nulls_last(), VehicleMilestone.created_at))).scalars().all())


async def photo_links(db: AsyncSession, vehicle_id: str) -> list[tuple[AssetLink, Asset]]:
    rows = (await db.execute(select(AssetLink, Asset).join(Asset, Asset.id == AssetLink.asset_id).where(
        AssetLink.entity_kind == "vehicle", AssetLink.entity_id == vehicle_id, AssetLink.removed_at.is_(None))
        .order_by(AssetLink.position, AssetLink.created_at))).all()
    return [(l, a) for l, a in rows]


def serialize_asset_brief(a: Asset, link: AssetLink | None = None) -> dict:
    d = {"id": a.id, "kind": a.kind, "content_type": a.content_type, "size_bytes": a.size_bytes, "sha256": a.sha256,
         "width": a.width, "height": a.height, "captured_at": iso(a.captured_at), "uploaded_at": iso(a.uploaded_at),
         "classification": a.classification, "sensitive": bool(a.sensitive), "public_eligible": bool(a.public_eligible),
         "pre_arrival": bool(a.pre_arrival), "status": a.status, "original_name": a.original_name,
         "derivatives": dict(a.derivatives or {}), "urls": {"original": f"/api/assets/{a.id}/original",
                                                          "web": f"/api/assets/{a.id}/web", "thumb": f"/api/assets/{a.id}/thumb"},
         "transcript": a.transcript if a.kind == "audio" else None}
    if link is not None:
        d["link"] = {"id": link.id, "role": link.role, "slot": link.slot, "position": link.position,
                     "entity_kind": link.entity_kind, "entity_id": link.entity_id, "confirmed_by": link.confirmed_by}
    return d


async def vehicle_detail(db: AsyncSession, actor: Actor, v: Vehicle) -> dict:
    """Overview / Work / Files / Sale / Money tabs (spec §2.3). Money only with costs.read."""
    from .tasks import serialize_task
    core = sanitize_vehicle(actor, serialize_vehicle(v))
    facts = [serialize_fact(f) for f in await facts_of(db, v.id)]
    if not can_see_costs(actor):
        facts = [f for f in facts if f["key"] not in ("purchase_amount",) and f["visibility"] != "owner"]
    milestones = [serialize_milestone(m) for m in await milestones_of(db, v.id)]
    tasks = (await db.execute(select(Task).where(Task.vehicle_id == v.id).order_by(Task.due_at.asc().nulls_last(), Task.created_at))).scalars().all()
    if actor.scope == "assigned" and actor.role != "owner":
        tasks = [t for t in tasks if t.owner_user_id == actor.user_id]
    issues = (await db.execute(select(ReconIssue).where(ReconIssue.vehicle_id == v.id).order_by(ReconIssue.created_at))).scalars().all()
    wos = (await db.execute(select(WorkOrder).where(WorkOrder.vehicle_id == v.id).order_by(WorkOrder.created_at))).scalars().all()
    parts = (await db.execute(select(Part).where(Part.vehicle_id == v.id).order_by(Part.created_at))).scalars().all()
    links = await photo_links(db, v.id)
    photos, documents, audio = [], [], []
    for l, a in links:
        d = serialize_asset_brief(a, l)
        if a.kind == "audio":
            audio.append(d)
        elif a.kind == "document" or a.sensitive or a.classification in ("invoice", "id_document", "shipping_paper"):
            if actor.perms.get("documents.read", False) or actor.role == "owner":
                documents.append(d)
        else:
            photos.append(d)
    filled = {l.slot for l, _ in links if l.slot}
    required = list(v.photo_requirements or [])
    shipments = await _shipments_of(db, v.id)
    sale = await _sale_of(db, v)
    money = await _money_of(db, actor, v)
    return {
        "vehicle": core,
        "tabs": {
            "overview": {"identity": {"stock_no": v.stock_no, "frame_no_raw": v.frame_no_raw, "title": vehicle_title(v),
                                      "intake_status": v.intake_status, "missing_identity_fields": list(v.missing_identity_fields or [])},
                         "health": {"health": v.health, "reason": v.health_reason, "next_action": v.next_action,
                                    "owner_id": v.next_action_owner_id, "due_at": iso(v.next_action_due_at)},
                         "condition": current_bullets(v), "condition_version": v.condition_version,
                         "facts": facts, "milestones": milestones, "states": core["states"], "situation": core["situation"]},
            "work": {"tasks": [serialize_task(t) for t in tasks], "recon_issues": [serialize_issue(i) for i in issues],
                     "work_orders": [serialize_work_order(w) for w in wos], "parts": [serialize_part(p) for p in parts],
                     "shipments": shipments},
            "files": {"photos": photos, "documents": documents, "audio": audio, "hero_asset_id": v.hero_asset_id,
                      "photo_status": None if photos else "No photo yet",
                      "missing_slots": [s for s in required if s not in filled], "required_slots": required},
            "sale": sale,
            "money": money,
        },
    }


async def _shipments_of(db: AsyncSession, vehicle_id: str) -> list[dict]:
    try:
        from ..models.shipping import Shipment
        rows = (await db.execute(select(Shipment).where(cast(Shipment.vehicle_ids, Text).ilike(f'%"{vehicle_id}"%')))).scalars().all()
        return [{"id": s.id, "ref": s.ref, "status": s.status, "eta_at": iso(s.eta_at), "eta_source": s.eta_source,
                 "container_no": s.container_no, "vessel": s.vessel} for s in rows]
    except Exception:  # noqa: BLE001
        return []


async def _sale_of(db: AsyncSession, v: Vehicle) -> dict:
    out: dict = {"commercial_state": v.commercial_state, "allocation": v.allocation, "buyer_contact_id": v.buyer_contact_id,
                 "asking_price": money_str(v.asking_price), "asking_currency": v.asking_currency,
                 "price_approved_at": iso(v.price_approved_at), "active_sale": None, "opportunities": [],
                 "disclosures": list(v.disclosures or [])}
    try:
        from ..models.finance import Sale
        s = (await db.execute(select(Sale).where(Sale.vehicle_id == v.id, Sale.is_active.is_(True)))).scalars().first()
        if s is not None:
            out["active_sale"] = {"id": s.id, "status": s.status, "buyer_contact_id": s.buyer_contact_id,
                                  "reserved_at": iso(s.reserved_at), "reservation_expires_at": iso(s.reservation_expires_at),
                                  "completed_at": iso(s.completed_at), "delivered_at": iso(s.delivered_at)}
    except Exception:  # noqa: BLE001
        pass
    try:
        from ..models.sales import Opportunity
        rows = (await db.execute(select(Opportunity).where(Opportunity.vehicle_id == v.id))).scalars().all()
        out["opportunities"] = [{"id": o.id, "contact_id": o.contact_id, "pipeline": o.pipeline, "stage": o.stage,
                                 "owner_user_id": o.owner_user_id} for o in rows]
    except Exception:  # noqa: BLE001
        pass
    return out


async def _money_of(db: AsyncSession, actor: Actor, v: Vehicle) -> dict:
    if not can_see_costs(actor):
        return {"money_hidden": True}
    out: dict = {"money_hidden": False, "purchase_amount": money_str(v.purchase_amount), "purchase_currency": v.purchase_currency,
                 "asking_price": money_str(v.asking_price), "asking_currency": v.asking_currency,
                 "cost_items": [], "cost_total": {}, "coverage": "unknown"}
    try:
        from ..models.finance import CostAllocation, CostItem
        items = (await db.execute(select(CostItem).where(CostItem.vehicle_id == v.id))).scalars().all()
        totals: dict[str, Decimal] = {}
        for c in items:
            amt = c.active_amount
            out["cost_items"].append({"id": c.id, "category": c.category, "description": c.description, "currency": c.currency,
                                      "amount": money_str(amt), "status": c.status, "is_estimate_only": bool(c.is_estimate_only)})
            if amt is not None:
                totals[c.currency] = totals.get(c.currency, Decimal("0")) + amt
        allocs = (await db.execute(select(CostAllocation).where(CostAllocation.vehicle_id == v.id))).scalars().all()
        for a in allocs:
            totals[a.currency] = totals.get(a.currency, Decimal("0")) + a.amount
        out["cost_total"] = {cur: str(quantize(val, cur)) for cur, val in totals.items()}
        out["coverage"] = "estimated" if any(c.is_estimate_only for c in items) else ("recorded" if items else "none")
    except Exception:  # noqa: BLE001
        out["coverage"] = "unavailable"
    return out


# ── inputs ───────────────────────────────────────────────────────────────────
class VehicleCreateIn(BaseModel):
    title: str | None = None
    make: str | None = None
    model: str | None = None
    model_year: int | None = Field(default=None, ge=1950, le=2100)
    color: str | None = None
    grade: str | None = None
    frame_no_raw: str | None = None
    stock_no: str | None = None
    odometer_km: int | None = Field(default=None, ge=0)
    location: str | None = None
    logistics_state: str = "purchased"
    allocation: str = "inventory"
    purchase_amount: str | None = None
    purchase_currency: str | None = None
    acquired_at: datetime | None = None
    timezone: str | None = None
    notes: str = ""
    source_kind: str = "manual"   # manual|intake|document|auction_sheet|provider
    source_ref: str | None = None
    origin_candidate_id: str | None = None
    create_key: str | None = None  # retry-safe creation (invariant 13)
    create_missing_task: bool = True
    extra: dict = Field(default_factory=dict)


class VehicleUpdateIn(BaseModel):
    vehicle_id: str
    expected_version: int | None = None
    title: str | None = None
    make: str | None = None
    model: str | None = None
    model_year: int | None = Field(default=None, ge=1950, le=2100)
    color: str | None = None
    grade: str | None = None
    location: str | None = None
    notes: str | None = None
    stock_no: str | None = None  # only settable while empty
    hero_asset_id: str | None = None
    photo_requirements: list[str] | None = None
    disclosures: list[str] | None = None
    allocation: str | None = None
    buyer_contact_id: str | None = None
    source_kind: str = "manual"
    source_ref: str | None = None
    confirmed: bool = False  # owner asserts the value is verified (fact status confirmed)
    reason: str | None = None


class StatesIn(BaseModel):
    vehicle_id: str
    expected_version: int | None = None
    logistics_state: str | None = None
    commercial_state: str | None = None
    documents_state: str | None = None
    recon_state: str | None = None  # rejected: use shop.move_stage
    reason: str = Field(min_length=1)


class MilestoneIn(BaseModel):
    vehicle_id: str
    expected_version: int | None = None
    kind: str
    status: str = "completed"
    at: datetime | None = None
    timezone: str | None = None
    source_kind: str = "manual"  # manual|owner_reported|intake|exporter|port|carrier|document|provider|inspection|shop
    source_ref: str | None = None
    note: str | None = None
    shipment_id: str | None = None


class FactProposeIn(BaseModel):
    vehicle_id: str
    key: str
    value: str = Field(min_length=1)
    unit: str | None = None
    currency: str | None = None
    status: str = "reported"  # reported|inferred|estimated
    source_kind: str = "manual"
    source_ref: str | None = None
    observed_at: datetime | None = None
    effective_at: datetime | None = None
    confidence_method: str | None = None
    visibility: str = "all"


class FactConfirmIn(BaseModel):
    vehicle_id: str
    fact_id: str | None = None
    key: str | None = None
    value: str | None = None
    unit: str | None = None
    currency: str | None = None
    source_kind: str = "owner"
    source_ref: str | None = None
    note: str | None = None


class BulletIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    source: str = "owner_reported"
    evidence: list[str] = Field(default_factory=list)
    observation_id: str | None = None
    id: str | None = None


class ConditionIn(BaseModel):
    vehicle_id: str
    expected_version: int | None = None
    bullets: list[BulletIn]
    mode: str = "append"  # append|replace
    reason: str | None = None
    source_ref: str | None = None


class BulletEditIn(BaseModel):
    vehicle_id: str
    bullet_id: str
    text: str | None = None
    remove: bool = False
    restore: bool = False
    reason: str | None = None
    expected_version: int | None = None


class ArchiveIn(BaseModel):
    vehicle_id: str
    reason: str | None = None
    expected_version: int | None = None


class PriceIn(BaseModel):
    vehicle_id: str
    amount: str
    currency: str = "USD"
    note: str | None = None
    expected_version: int | None = None


# ── fact helpers ─────────────────────────────────────────────────────────────
MONEY_FACT_KEYS = ("purchase_amount",)


def _fact_visibility(key: str, requested: str | None = None) -> str:
    """Activity visibility for a fact change: purchase money and owner-only facts never reach the general feed."""
    if key in MONEY_FACT_KEYS:
        return "finance"
    return "owner" if requested == "owner" else "all"


def _fact_norm(key: str, value: str | None) -> str:
    if value is None:
        return ""
    if key == "frame_no":
        return normalize_frame(value) or ""
    if key in NUMERIC_KEYS:
        try:
            return str(parse_amount(value).normalize())
        except (ValueError, InvalidOperation):
            return norm_text(value)
    return norm_text(value)


def _column_value(v: Vehicle, key: str) -> str | None:
    col = FACT_COLUMNS.get(key)
    if not col:
        return None
    val = getattr(v, col)
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        return iso(val)
    return str(val)


def _write_column(v: Vehicle, key: str, value: str, currency: str | None, tz: str | None = None) -> None:
    col = FACT_COLUMNS.get(key)
    if not col:
        return
    if key == "frame_no":
        v.frame_no_raw = value
        v.frame_no_norm = normalize_frame(value)
    elif key in ("odometer_km", "model_year"):
        setattr(v, col, int(parse_amount(value)))
    elif key == "purchase_amount":
        cur = (currency or v.purchase_currency or "USD").upper()
        v.purchase_amount = quantize(parse_amount(value), cur)
        v.purchase_currency = cur
    elif key == "acquired_at":
        v.acquired_at = aware(datetime.fromisoformat(value.replace("Z", "+00:00")), tz)
    else:
        setattr(v, col, value)


def _new_fact(ctx: CommandContext, v: Vehicle, key: str, value: str, *, status: str, source_kind: str,
              source_ref: str | None, unit: str | None = None, currency: str | None = None,
              observed_at: datetime | None = None, effective_at: datetime | None = None,
              confidence_method: str | None = None, visibility: str = "all", supersedes: VehicleFact | None = None,
              conflict_with: VehicleFact | None = None, is_current: bool = True) -> VehicleFact:
    num = None
    if key in NUMERIC_KEYS:
        try:
            num = parse_amount(value)
        except (ValueError, InvalidOperation):
            raise ValidationFailed(f"{key} must be a number")
    f = VehicleFact(vehicle_id=v.id, key=key, value=value, value_num=num, unit=unit,
                    currency=(currency.upper() if currency else None), status=status, observed_at=observed_at,
                    effective_at=effective_at, source_kind=source_kind, source_ref=source_ref,
                    source_hash=sha256_hex(f"{source_kind}|{source_ref or ''}|{value}")[:32],
                    actor=ctx.actor.user_id or ctx.actor.client_id, confidence_method=confidence_method,
                    supersedes_id=supersedes.id if supersedes else None,
                    conflict_with_id=conflict_with.id if conflict_with else None, visibility=visibility,
                    is_current=is_current, created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
    ctx.db.add(f)
    return f


async def _current_fact(db: AsyncSession, vehicle_id: str, key: str) -> VehicleFact | None:
    return (await db.execute(select(VehicleFact).where(VehicleFact.vehicle_id == vehicle_id, VehicleFact.key == key,
                                                       VehicleFact.is_current.is_(True))
                             .order_by(VehicleFact.created_at.desc()))).scalars().first()


def _state_event(ctx: CommandContext, v: Vehicle, dimension: str, old: str | None, new: str, reason: str | None,
                 backward: bool = False) -> None:
    v.state_history = list(v.state_history or []) + [{"dimension": dimension, "from": old, "to": new, "reason": reason,
                                                       "at": ctx.now.isoformat(), "by": ctx.actor.user_id, "backward": backward}]
    ctx.emit("vehicle.state_changed", aggregate_type="vehicle", aggregate_id=v.id, aggregate_version=v.version,
             payload={"vehicle_id": v.id, "dimension": dimension, "from": old, "to": new, "reason": reason})


async def _missing_task(ctx: CommandContext, v: Vehicle) -> dict | None:
    """Focused intake task for missing identity fields (spec §7.4 step 5). Deduped by title."""
    missing = missing_identity(v)
    if not missing:
        return None
    labels = {"frame_no": "frame number", "model_year": "model year", "purchase_amount": "purchase amount"}
    res = await dispatch(ctx.child(), "tasks.create", {
        "title": "Record missing vehicle identity", "type": "operational", "vehicle_id": v.id,
        "notes": "Missing: " + ", ".join(labels[m] for m in missing),
        "source_kind": "intake", "source_id": f"identity:{v.id}", "dedupe": True,
        "extra": {"missing_fields": missing, "intake": True}}, commit=False)
    return res.data


# ── commands ─────────────────────────────────────────────────────────────────
@command("vehicles.create", input=VehicleCreateIn, perm="vehicles.write", action_class="internal",
         description="Create a vehicle card with available facts; allocates STK-#### and marks intake incomplete "
                     "for missing identity fields. Never invents a frame number, price or date.")
async def vehicles_create(ctx: CommandContext, inp: VehicleCreateIn) -> dict:
    if inp.create_key:
        existing = (await ctx.db.execute(select(Vehicle).where(Vehicle.extra["create_key"].as_string() == inp.create_key)
                                         .order_by(Vehicle.created_at))).scalars().first()
        if existing is not None and (existing.extra or {}).get("create_key") == inp.create_key:
            return {"vehicle": serialize_vehicle(existing), "created": False}
    if inp.purchase_amount is not None and not can_see_costs(ctx.actor):
        raise Denied("recording a purchase amount needs costs.read; create the card and let the owner or books add the money")
    if inp.logistics_state not in LOGISTICS_STATES:
        raise ValidationFailed(f"logistics_state must be one of {LOGISTICS_STATES}")
    if inp.allocation not in ("inventory", "reserved", "sold", "candidate"):
        raise ValidationFailed("allocation must be inventory|reserved|sold|candidate")
    frame_norm = normalize_frame(inp.frame_no_raw)
    if frame_norm:
        dup = (await ctx.db.execute(select(Vehicle).where(Vehicle.frame_no_norm == frame_norm, Vehicle.archived_at.is_(None)))).scalars().first()
        if dup is not None:
            raise Conflict("a vehicle with this frame number already exists", vehicle_id=dup.id, stock_no=dup.stock_no)
    stock = normalize_stock_no(inp.stock_no)
    if stock:
        taken = (await ctx.db.execute(select(Vehicle).where(Vehicle.stock_no == stock))).scalars().first()
        if taken is not None:
            raise Conflict("stock number already in use", vehicle_id=taken.id)
        seq = None
    else:
        stock, seq = await allocate_stock_no(ctx.db, ctx.now, ctx.actor.user_id)
    v = Vehicle(title=(inp.title or "").strip(), stock_no=stock, stock_seq=seq, frame_no_raw=inp.frame_no_raw,
                frame_no_norm=frame_norm, make=inp.make, model=inp.model, model_year=inp.model_year, color=inp.color,
                grade=inp.grade, odometer_km=inp.odometer_km, location=inp.location, logistics_state=inp.logistics_state,
                allocation=inp.allocation, notes=inp.notes, origin_candidate_id=inp.origin_candidate_id,
                stage="sourced", extra={**(inp.extra or {}), **({"create_key": inp.create_key} if inp.create_key else {}),
                                        "source_kind": inp.source_kind, "source_ref": inp.source_ref},
                created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
    if inp.purchase_amount is not None:
        cur = (inp.purchase_currency or "USD").upper()
        v.purchase_amount = quantize(parse_amount(inp.purchase_amount), cur)
        v.purchase_currency = cur
    if inp.acquired_at is not None:
        v.acquired_at = aware(inp.acquired_at, inp.timezone)
    ctx.db.add(v)
    await ctx.db.flush()
    # provenance rows for every stated fact
    stated = {"make": inp.make, "model": inp.model, "model_year": inp.model_year, "color": inp.color, "grade": inp.grade,
              "location": inp.location, "frame_no": inp.frame_no_raw, "odometer_km": inp.odometer_km,
              "purchase_amount": money_str(v.purchase_amount), "acquired_at": iso(v.acquired_at)}
    facts = []
    human_owner = ctx.actor.kind == "user" and ctx.actor.role == "owner"
    for k, val in stated.items():
        if val is None or val == "":
            continue
        # a document-sourced value is confirmed, except critical facts, which only the owner confirms (vehicles.confirm_fact)
        status = "confirmed" if inp.source_kind == "document" and (k not in CRITICAL_FACT_KEYS or human_owner) else "reported"
        facts.append(_new_fact(ctx, v, k, str(val), status=status, source_kind=inp.source_kind, source_ref=inp.source_ref,
                               currency=v.purchase_currency if k == "purchase_amount" else None))
    v.missing_identity_fields = missing_identity(v)
    v.intake_status = "incomplete" if v.missing_identity_fields else "complete"
    if v.acquired_at:
        ctx.db.add(VehicleMilestone(vehicle_id=v.id, kind="purchased", status="completed", at=v.acquired_at,
                                    source_kind=inp.source_kind, source_ref=inp.source_ref, actor_id=ctx.actor.user_id,
                                    created_by=ctx.actor.user_id))
    ctx.changed.append({"kind": "vehicle", "id": v.id, "version": v.version})
    ctx.record(f"Created vehicle {v.stock_no}: {vehicle_title(v)}", entity_kind="vehicle", entity_id=v.id, kind="task",
               state="created", details={"missing_identity_fields": v.missing_identity_fields, "source_kind": inp.source_kind})
    _state_event(ctx, v, "created", None, v.logistics_state, inp.source_kind)
    ctx.emit("metrics.invalidated", aggregate_type="vehicle", aggregate_id=v.id, payload={"reason": "vehicle.created"})
    task = await _missing_task(ctx, v) if inp.create_missing_task else None
    await recompute(ctx.db, v, ctx.now)
    return {"vehicle": serialize_vehicle(v), "created": True, "facts": [serialize_fact(f) for f in facts],
            "missing_identity_task": (task or {}).get("task")}


@command("vehicles.update", input=VehicleUpdateIn, perm="vehicles.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)],
         description="Edit non-critical card fields with provenance (a fact row per change). Critical facts go "
                     "through vehicles.propose_fact / vehicles.confirm_fact.")
async def vehicles_update(ctx: CommandContext, inp: VehicleUpdateIn) -> dict:
    v = await get_vehicle(ctx.db, inp.vehicle_id, expected_version=inp.expected_version)
    changes: dict = {}
    facts = []
    human_owner = ctx.actor.kind == "user" and ctx.actor.role == "owner"
    status = "confirmed" if (inp.confirmed and human_owner) or inp.source_kind == "document" else "reported"
    for key in ("title", "make", "model", "model_year", "color", "grade", "location"):
        val = getattr(inp, key)
        if val is None:
            continue
        col = FACT_COLUMNS[key]
        old = getattr(v, col)
        if str(old) == str(val):
            continue
        cur = await _current_fact(ctx.db, v.id, key)
        if cur is not None:
            cur.is_current = False
            cur.status = "outdated" if cur.status != "conflicted" else cur.status
        facts.append(_new_fact(ctx, v, key, str(val), status=status, source_kind=inp.source_kind,
                               source_ref=inp.source_ref, supersedes=cur))
        setattr(v, col, val)
        changes[key] = {"from": old, "to": val}
    if inp.notes is not None and inp.notes != v.notes:
        changes["notes"] = {"from": v.notes, "to": inp.notes}
        v.notes = inp.notes
    if inp.stock_no:
        stock = normalize_stock_no(inp.stock_no)
        if v.stock_no and v.stock_no != stock:
            raise Blocked("stock number is already allocated; it cannot be renamed", stock_no=v.stock_no)
        if not v.stock_no:
            taken = (await ctx.db.execute(select(Vehicle).where(Vehicle.stock_no == stock, Vehicle.id != v.id))).scalars().first()
            if taken is not None:
                raise Conflict("stock number already in use", vehicle_id=taken.id)
            v.stock_no = stock
            changes["stock_no"] = {"from": None, "to": stock}
    if inp.hero_asset_id is not None:
        link = (await ctx.db.execute(select(AssetLink).where(AssetLink.asset_id == inp.hero_asset_id, AssetLink.entity_kind == "vehicle",
                                                             AssetLink.entity_id == v.id, AssetLink.removed_at.is_(None)))).scalars().first()
        if link is None:
            raise ValidationFailed("hero photo must be a photo linked to this vehicle")
        changes["hero_asset_id"] = {"from": v.hero_asset_id, "to": inp.hero_asset_id}
        v.hero_asset_id = inp.hero_asset_id
    if inp.photo_requirements is not None:
        changes["photo_requirements"] = {"from": v.photo_requirements, "to": inp.photo_requirements}
        v.photo_requirements = list(inp.photo_requirements)
    if inp.disclosures is not None:
        v.disclosures = [{"text": t.strip(), "by": ctx.actor.user_id, "at": ctx.now.isoformat()} for t in inp.disclosures if t.strip()]
        changes["disclosures"] = {"count": len(v.disclosures)}
    if inp.allocation is not None and inp.allocation != v.allocation:
        if inp.allocation not in ("inventory", "reserved", "sold", "candidate"):
            raise ValidationFailed("allocation must be inventory|reserved|sold|candidate")
        changes["allocation"] = {"from": v.allocation, "to": inp.allocation}
        v.allocation = inp.allocation
    if inp.buyer_contact_id is not None and inp.buyer_contact_id != v.buyer_contact_id:
        changes["buyer_contact_id"] = {"from": v.buyer_contact_id, "to": inp.buyer_contact_id}
        v.buyer_contact_id = inp.buyer_contact_id or None
    if not changes:
        return {"vehicle": serialize_vehicle(v), "changed": {}, "facts": []}
    v.missing_identity_fields = missing_identity(v)
    v.intake_status = "incomplete" if v.missing_identity_fields else "complete"
    ctx.touch(v, "vehicle")
    ctx.record(f"Updated vehicle {v.stock_no}: {', '.join(changes)}", entity_kind="vehicle", entity_id=v.id, kind="fact",
               state="updated", details={"changes": changes, "source_kind": inp.source_kind, "reason": inp.reason})
    ctx.emit("vehicle.changed", aggregate_type="vehicle", aggregate_id=v.id, aggregate_version=v.version,
             payload={"vehicle_id": v.id, "fields": sorted(changes)})
    await recompute(ctx.db, v, ctx.now)
    return {"vehicle": serialize_vehicle(v), "changed": changes, "facts": [serialize_fact(f) for f in facts]}


@command("vehicles.set_states", input=StatesIn, perm="vehicles.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)],
         description="Change logistics / commercial / documents state with a reason. Recon stage moves use shop.move_stage.")
async def vehicles_set_states(ctx: CommandContext, inp: StatesIn) -> dict:
    if inp.recon_state is not None:
        raise ValidationFailed("recon_state changes go through shop.move_stage (gated)")
    v = await get_vehicle(ctx.db, inp.vehicle_id, expected_version=inp.expected_version)
    changed = []
    for dim, val, allowed in (("logistics", inp.logistics_state, LOGISTICS_STATES),
                              ("commercial", inp.commercial_state, COMMERCIAL_STATES),
                              ("documents", inp.documents_state, DOCUMENT_STATES)):
        if val is None:
            continue
        if val not in allowed:
            raise ValidationFailed(f"{dim}_state must be one of {allowed}")
        col = f"{dim}_state"
        old = getattr(v, col)
        if old == val:
            continue
        backward = allowed.index(val) < allowed.index(old) if old in allowed else False
        setattr(v, col, val)
        _state_event(ctx, v, dim, old, val, inp.reason, backward)
        changed.append({"dimension": dim, "from": old, "to": val, "backward": backward})
    if not changed:
        return {"vehicle": serialize_vehicle(v), "changed": []}
    ctx.touch(v, "vehicle")
    ctx.record(f"State change {v.stock_no}: " + ", ".join(f"{c['dimension']} {c['from']}→{c['to']}" for c in changed)
               + f" — {inp.reason}", entity_kind="vehicle", entity_id=v.id, kind="task", state="state_changed",
               details={"changes": changed, "reason": inp.reason})
    ctx.emit("metrics.invalidated", aggregate_type="vehicle", aggregate_id=v.id, payload={"reason": "vehicle.state_changed"})
    await recompute(ctx.db, v, ctx.now)
    return {"vehicle": serialize_vehicle(v), "changed": changed}


async def apply_milestone(ctx: CommandContext, v: Vehicle, *, kind: str, status: str, at: datetime | None, source_kind: str,
                          source_ref: str | None = None, note: str | None = None, shipment_id: str | None = None) -> dict:
    """Shared milestone writer (used by the command, shop stage moves and intake). Validates the sourced-time rule,
    supersedes the prior current milestone of the same kind, updates the actual-date column and forward logistics state."""
    if kind not in MILESTONE_KINDS:
        raise ValidationFailed(f"kind must be one of {MILESTONE_KINDS}")
    if status not in MILESTONE_STATUSES:
        raise ValidationFailed(f"status must be one of {MILESTONE_STATUSES}")
    src = (source_kind or "").strip().lower()
    if status == "completed":
        if at is None:
            raise ValidationFailed("a completed milestone needs the time it happened (or record it as planned/estimated)")
        if src in ("", "unknown", "assumed", "inferred"):
            raise ValidationFailed("a completed milestone needs a source (owner_reported, exporter, document, ...)")
        if ensure_aware(at) > ctx.now:
            raise ValidationFailed("a completed milestone cannot be in the future")
    at = ensure_aware(at)
    prior = (await ctx.db.execute(select(VehicleMilestone).where(VehicleMilestone.vehicle_id == v.id, VehicleMilestone.kind == kind,
                                                                 VehicleMilestone.is_current.is_(True)))).scalars().first()
    if prior is not None and prior.status == status and ensure_aware(prior.at) == at and prior.source_kind == src and prior.source_ref == source_ref:
        return {"milestone": serialize_milestone(prior), "created": False, "state_changes": []}
    if status in ("planned", "estimated") and prior is not None and prior.status == "completed":
        raise Blocked("a completed milestone cannot be downgraded to planned/estimated; record a correction instead")
    m = VehicleMilestone(vehicle_id=v.id, kind=kind, status=status, at=at, source_kind=src, source_ref=source_ref,
                         actor_id=ctx.actor.user_id, note=note, supersedes_id=prior.id if prior else None,
                         shipment_id=shipment_id, created_by=ctx.actor.user_id)
    if prior is not None:
        prior.is_current = False
    ctx.db.add(m)
    await ctx.db.flush()
    state_changes = []
    if status == "completed":
        col = MILESTONE_COLUMNS.get(kind)
        if col:
            setattr(v, col, at)
        want = MILESTONE_LOGISTICS.get(kind)
        if want and v.logistics_state != "not_applicable" and v.logistics_state != want:
            old = v.logistics_state
            if LOGISTICS_STATES.index(want) > LOGISTICS_STATES.index(old):
                v.logistics_state = want
                _state_event(ctx, v, "logistics", old, want, f"milestone {kind} ({src})")
                state_changes.append({"dimension": "logistics", "from": old, "to": want})
    ctx.changed.append({"kind": "vehicle_milestone", "id": m.id, "version": m.version})
    ctx.record(f"Milestone {kind} {status}: {v.stock_no}" + (f" ({src})" if src else ""), entity_kind="vehicle",
               entity_id=v.id, kind="task", state=status, details={"milestone_id": m.id, "at": iso(at), "source_kind": src,
                                                                    "source_ref": source_ref, "supersedes": m.supersedes_id})
    ctx.emit("milestone.changed", aggregate_type="vehicle", aggregate_id=v.id, aggregate_version=v.version,
             payload={"vehicle_id": v.id, "milestone_id": m.id, "kind": kind, "status": status, "at": iso(at),
                      "source_kind": src, "supersedes_id": m.supersedes_id})
    ctx.emit("metrics.invalidated", aggregate_type="vehicle", aggregate_id=v.id, payload={"reason": "milestone.changed"})
    return {"milestone": serialize_milestone(m), "created": True, "state_changes": state_changes}


@command("vehicles.record_milestone", input=MilestoneIn, perm="vehicles.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)],
         description="Record a planned / estimated / completed milestone with its source; supersedes the prior one of the "
                     "same kind (history kept). A completed milestone needs a sourced time.")
async def vehicles_record_milestone(ctx: CommandContext, inp: MilestoneIn) -> dict:
    if inp.status == "completed" and inp.kind in SHOP_OWNED_MILESTONES:
        raise Blocked(f"a completed {inp.kind} milestone is recorded by {SHOP_OWNED_MILESTONES[inp.kind]} once the shop "
                      f"gates are met; record it there (a planned/estimated date is fine here)",
                      command=SHOP_OWNED_MILESTONES[inp.kind], kind=inp.kind)
    v = await get_vehicle(ctx.db, inp.vehicle_id, expected_version=inp.expected_version)
    out = await apply_milestone(ctx, v, kind=inp.kind, status=inp.status, at=aware(inp.at, inp.timezone),
                                source_kind=inp.source_kind, source_ref=inp.source_ref, note=inp.note, shipment_id=inp.shipment_id)
    if out["created"]:
        ctx.touch(v, "vehicle")
    await recompute(ctx.db, v, ctx.now)
    out["vehicle"] = serialize_vehicle(v)
    return out


@command("vehicles.propose_fact", input=FactProposeIn, perm="vehicles.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)],
         description="Propose a field value with provenance. A different existing value becomes a conflict "
                     "(never overwritten); an empty field is filled as reported. Critical facts need owner confirmation.")
async def vehicles_propose_fact(ctx: CommandContext, inp: FactProposeIn) -> dict:
    if inp.status not in ("reported", "inferred", "estimated"):
        raise ValidationFailed("status must be reported|inferred|estimated (confirmed is owner-only via vehicles.confirm_fact)")
    key = inp.key.strip()
    if key == "frame_no_raw":
        key = "frame_no"
    if key in MONEY_FACT_KEYS and not can_see_costs(ctx.actor):
        # without costs.read the outcome (recorded / restated / conflicted) would itself disclose the hidden amount
        raise Denied(f"recording {key} needs costs.read; the owner or books records purchase money")
    v = await get_vehicle(ctx.db, inp.vehicle_id)
    cur = await _current_fact(ctx.db, v.id, key)
    cur_value = cur.value if cur is not None else _column_value(v, key)
    new_norm = _fact_norm(key, inp.value)
    outcome: str
    if cur_value is not None and _fact_norm(key, cur_value) == new_norm:
        if cur is not None and cur.status == "confirmed":
            ctx.record(f"Fact corroborated {key}={inp.value} ({inp.source_kind})", entity_kind="vehicle", entity_id=v.id,
                       kind="fact", state="corroborated", visibility=_fact_visibility(key, inp.visibility), details={"fact_id": cur.id})
            return {"fact": serialize_fact(cur), "outcome": "corroborated", "conflict": False, "needs_confirmation": False}
        f = _new_fact(ctx, v, key, inp.value, status=inp.status, source_kind=inp.source_kind, source_ref=inp.source_ref,
                      unit=inp.unit, currency=inp.currency, observed_at=aware(inp.observed_at), effective_at=aware(inp.effective_at),
                      confidence_method=inp.confidence_method, visibility=inp.visibility, supersedes=cur)
        if cur is not None:
            cur.is_current = False
            cur.status = "outdated" if cur.status != "conflicted" else cur.status
        outcome = "restated"
    elif cur_value is not None:
        f = _new_fact(ctx, v, key, inp.value, status="conflicted", source_kind=inp.source_kind, source_ref=inp.source_ref,
                      unit=inp.unit, currency=inp.currency, observed_at=aware(inp.observed_at), effective_at=aware(inp.effective_at),
                      confidence_method=inp.confidence_method, visibility=inp.visibility, conflict_with=cur, is_current=False)
        if cur is None:
            # legacy column value without a fact row: materialize it so the conflict has a partner
            base = _new_fact(ctx, v, key, cur_value, status="reported", source_kind="legacy", source_ref=None)
            await ctx.db.flush()
            f.conflict_with_id = base.id
        outcome = "conflicted"
    else:
        f = _new_fact(ctx, v, key, inp.value, status=inp.status, source_kind=inp.source_kind, source_ref=inp.source_ref,
                      unit=inp.unit, currency=inp.currency, observed_at=aware(inp.observed_at), effective_at=aware(inp.effective_at),
                      confidence_method=inp.confidence_method, visibility=inp.visibility)
        if key in FACT_COLUMNS:
            _write_column(v, key, inp.value, inp.currency)
        outcome = "recorded"
    await ctx.db.flush()
    v.missing_identity_fields = missing_identity(v)
    v.intake_status = "incomplete" if v.missing_identity_fields else "complete"
    ctx.touch(v, "vehicle")
    ctx.changed.append({"kind": "vehicle_fact", "id": f.id, "version": f.version})
    ctx.record(f"Fact {outcome}: {key}={inp.value} ({inp.source_kind})", entity_kind="vehicle", entity_id=v.id, kind="fact",
               state=f.status, exception=(outcome == "conflicted"), visibility=_fact_visibility(key, inp.visibility),
               details={"fact_id": f.id, "key": key, "conflict_with_id": f.conflict_with_id, "source_ref": inp.source_ref})
    ctx.emit("vehicle.changed", aggregate_type="vehicle", aggregate_id=v.id, aggregate_version=v.version,
             payload={"vehicle_id": v.id, "fact": key, "status": f.status, "outcome": outcome})
    if key in ("purchase_amount", "acquired_at"):
        ctx.emit("metrics.invalidated", aggregate_type="vehicle", aggregate_id=v.id, payload={"reason": f"fact.{key}"})
    await recompute(ctx.db, v, ctx.now)
    return {"fact": serialize_fact(f), "outcome": outcome, "conflict": outcome == "conflicted",
            "needs_confirmation": outcome == "conflicted" or (key in CRITICAL_FACT_KEYS and f.status != "confirmed"),
            "vehicle": serialize_vehicle(v)}


@command("vehicles.confirm_fact", input=FactConfirmIn, perm="vehicles.write", action_class="owner_only",
         approval_kind="fact_overwrite", records=lambda p: [("vehicle", p.vehicle_id)],
         summary=lambda p: f"Confirm {p.key or 'fact'}" + (f" = {p.value}" if p.value else ""),
         description="Owner confirms a fact (resolves conflicts, marks other values outdated) and writes the card field.")
async def vehicles_confirm_fact(ctx: CommandContext, inp: FactConfirmIn) -> dict:
    v = await get_vehicle(ctx.db, inp.vehicle_id)
    if inp.fact_id:
        f = await ctx.db.get(VehicleFact, inp.fact_id)
        if f is None or f.vehicle_id != v.id:
            raise NotFound("fact not found")
        key, value, unit, currency = f.key, f.value, f.unit, f.currency
    else:
        if not inp.key or inp.value is None:
            raise ValidationFailed("give fact_id or key+value")
        key = "frame_no" if inp.key == "frame_no_raw" else inp.key
        value, unit, currency = inp.value, inp.unit, inp.currency
        f = None
    others = (await ctx.db.execute(select(VehicleFact).where(VehicleFact.vehicle_id == v.id, VehicleFact.key == key))).scalars().all()
    for o in others:
        if f is not None and o.id == f.id:
            continue
        if o.is_current or o.status == "conflicted":
            o.is_current = False
            o.status = "outdated"
            o.conflict_with_id = None
    if f is None:
        f = _new_fact(ctx, v, key, value, status="confirmed", source_kind=inp.source_kind, source_ref=inp.source_ref,
                      unit=unit, currency=currency)
    else:
        f.status = "confirmed"
        f.is_current = True
        f.conflict_with_id = None
        f.bump(ctx.actor.user_id)
    if key in FACT_COLUMNS:
        _write_column(v, key, value, currency)
    await ctx.db.flush()
    v.missing_identity_fields = missing_identity(v)
    v.intake_status = "incomplete" if v.missing_identity_fields else "complete"
    ctx.touch(v, "vehicle")
    ctx.changed.append({"kind": "vehicle_fact", "id": f.id, "version": f.version})
    ctx.record(f"Fact confirmed: {key}={value}" + (f" — {inp.note}" if inp.note else ""), entity_kind="vehicle", entity_id=v.id,
               kind="fact", state="confirmed", visibility=_fact_visibility(key, f.visibility),
               details={"fact_id": f.id, "key": key, "outdated": [o.id for o in others if o.id != f.id]})
    ctx.emit("vehicle.changed", aggregate_type="vehicle", aggregate_id=v.id, aggregate_version=v.version,
             payload={"vehicle_id": v.id, "fact": key, "status": "confirmed"})
    if key in ("purchase_amount", "acquired_at"):
        ctx.emit("metrics.invalidated", aggregate_type="vehicle", aggregate_id=v.id, payload={"reason": f"fact.{key}"})
    await recompute(ctx.db, v, ctx.now)
    return {"fact": serialize_fact(f), "vehicle": serialize_vehicle(v)}


def _bullet_row(ctx: CommandContext, b: BulletIn) -> dict:
    return {"id": b.id or new_id(), "text": b.text.strip(), "source": b.source, "evidence": list(dict.fromkeys(b.evidence)),
            "observation_id": b.observation_id, "observation_ids": [b.observation_id] if b.observation_id else [],
            "added_by": ctx.actor.user_id, "added_at": ctx.now.isoformat(), "version": 1, "removed": False, "history": []}


@command("vehicles.set_condition", input=ConditionIn, perm="intake", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)],
         description="Save/replace 'Condition at intake' bullets with per-bullet provenance and evidence; versions kept. "
                     "Equivalent bullets merge evidence instead of duplicating.")
async def vehicles_set_condition(ctx: CommandContext, inp: ConditionIn) -> dict:
    if inp.mode not in ("append", "replace"):
        raise ValidationFailed("mode must be append|replace")
    for b in inp.bullets:
        if b.source not in CONDITION_SOURCES:
            raise ValidationFailed(f"bullet source must be one of {CONDITION_SOURCES}")
    v = await get_vehicle(ctx.db, inp.vehicle_id, expected_version=inp.expected_version)
    if inp.bullets:
        ids = {a for b in inp.bullets for a in b.evidence}
        if ids:
            found = {a.id for a in (await ctx.db.execute(select(Asset).where(Asset.id.in_(ids)))).scalars().all()}
            missing = sorted(ids - found)
            if missing:
                raise ValidationFailed("evidence asset not found", missing_assets=missing)
    current = [dict(b) for b in (v.condition_summary or [])]
    by_norm = {norm_text(b["text"]): b for b in current if not b.get("removed")}
    by_obs = {o: b for b in current if not b.get("removed") for o in (b.get("observation_ids") or [])}
    added, updated, seen = [], [], set()
    for b in inp.bullets:
        key = norm_text(b.text)
        existing = by_obs.get(b.observation_id) if b.observation_id else None
        existing = existing or by_norm.get(key)
        if existing is not None:
            seen.add(existing["id"])
            ev = list(existing.get("evidence") or [])
            changed = False
            for a in b.evidence:
                if a not in ev:
                    ev.append(a)
                    changed = True
            obs = list(existing.get("observation_ids") or [])
            if b.observation_id and b.observation_id not in obs:
                obs.append(b.observation_id)
                changed = True
            if changed:
                existing["evidence"] = ev
                existing["observation_ids"] = obs
                existing["version"] = int(existing.get("version") or 1) + 1
                updated.append(existing["id"])
            continue
        row = _bullet_row(ctx, b)
        current.append(row)
        by_norm[key] = row
        seen.add(row["id"])
        added.append(row["id"])
    removed = []
    if inp.mode == "replace":
        for b in current:
            if not b.get("removed") and b["id"] not in seen:
                b["removed"] = True
                b["removed_at"] = ctx.now.isoformat()
                b["removed_by"] = ctx.actor.user_id
                b["remove_reason"] = inp.reason or "replaced"
                removed.append(b["id"])
    if not (added or updated or removed):
        return {"bullets": current_bullets(v), "added": [], "updated": [], "removed": [], "condition_version": v.condition_version}
    v.condition_summary = current
    v.condition_version = (v.condition_version or 0) + 1
    v.condition_history = list(v.condition_history or []) + [{
        "version": v.condition_version, "at": ctx.now.isoformat(), "by": ctx.actor.user_id, "reason": inp.reason,
        "source_ref": inp.source_ref, "added": added, "updated": updated, "removed": removed,
        "bullets": [{"id": b["id"], "text": b["text"], "source": b["source"], "evidence": b.get("evidence", [])} for b in current if not b.get("removed")]}]
    ctx.touch(v, "vehicle")
    ctx.record(f"Condition at intake v{v.condition_version}: {len(added)} added, {len(updated)} updated, {len(removed)} removed",
               entity_kind="vehicle", entity_id=v.id, kind="intake", state="saved",
               details={"added": added, "updated": updated, "removed": removed, "source_ref": inp.source_ref})
    ctx.emit("vehicle.condition_changed", aggregate_type="vehicle", aggregate_id=v.id, aggregate_version=v.version,
             payload={"vehicle_id": v.id, "condition_version": v.condition_version, "added": added, "updated": updated, "removed": removed})
    evidence = sorted({a for b in inp.bullets for a in b.evidence})
    if evidence:
        ctx.emit("evidence.saved", aggregate_type="vehicle", aggregate_id=v.id,
                 payload={"vehicle_id": v.id, "asset_ids": evidence, "context": "condition"})
    await recompute(ctx.db, v, ctx.now)
    return {"bullets": current_bullets(v), "added": added, "updated": updated, "removed": removed,
            "condition_version": v.condition_version}


@command("vehicles.edit_condition_bullet", input=BulletEditIn, perm="intake", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)],
         description="Edit, remove (mark removed; evidence kept) or restore one condition bullet; history retained.")
async def vehicles_edit_condition_bullet(ctx: CommandContext, inp: BulletEditIn) -> dict:
    v = await get_vehicle(ctx.db, inp.vehicle_id, expected_version=inp.expected_version)
    current = [dict(b) for b in (v.condition_summary or [])]
    target = next((b for b in current if b["id"] == inp.bullet_id), None)
    if target is None:
        raise NotFound("condition bullet not found")
    change: dict = {}
    hist = list(target.get("history") or [])
    if inp.text is not None and inp.text.strip() and inp.text.strip() != target["text"]:
        hist.append({"at": ctx.now.isoformat(), "by": ctx.actor.user_id, "text_before": target["text"], "reason": inp.reason})
        change["text"] = {"from": target["text"], "to": inp.text.strip()}
        target["text"] = inp.text.strip()
        target["version"] = int(target.get("version") or 1) + 1
    if inp.remove and not target.get("removed"):
        target["removed"] = True
        target["removed_at"] = ctx.now.isoformat()
        target["removed_by"] = ctx.actor.user_id
        target["remove_reason"] = inp.reason
        change["removed"] = True
    if inp.restore and target.get("removed"):
        hist.append({"at": ctx.now.isoformat(), "by": ctx.actor.user_id, "restored": True, "reason": inp.reason})
        target["removed"] = False
        target.pop("removed_at", None)
        target.pop("removed_by", None)
        target.pop("remove_reason", None)
        change["restored"] = True
    if not change:
        return {"bullet": target, "changed": {}, "condition_version": v.condition_version}
    target["history"] = hist
    v.condition_summary = current
    v.condition_version = (v.condition_version or 0) + 1
    v.condition_history = list(v.condition_history or []) + [{
        "version": v.condition_version, "at": ctx.now.isoformat(), "by": ctx.actor.user_id, "reason": inp.reason,
        "bullet_id": target["id"], "change": change,
        "bullets": [{"id": b["id"], "text": b["text"], "source": b["source"], "evidence": b.get("evidence", [])} for b in current if not b.get("removed")]}]
    ctx.touch(v, "vehicle")
    ctx.record(f"Condition bullet {'removed' if inp.remove else 'edited'}: {target['text']}", entity_kind="vehicle", entity_id=v.id,
               kind="intake", state="edited", details={"bullet_id": target["id"], "change": change, "reason": inp.reason})
    ctx.emit("vehicle.condition_changed", aggregate_type="vehicle", aggregate_id=v.id, aggregate_version=v.version,
             payload={"vehicle_id": v.id, "condition_version": v.condition_version, "bullet_id": target["id"], "change": change})
    return {"bullet": target, "changed": change, "condition_version": v.condition_version}


@command("vehicles.archive", input=ArchiveIn, perm="vehicles.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)],
         description="Archive a vehicle (reversible). Queued approvals for it are invalidated and open tasks cancelled (invariant 12).")
async def vehicles_archive(ctx: CommandContext, inp: ArchiveIn) -> dict:
    v = await get_vehicle(ctx.db, inp.vehicle_id, expected_version=inp.expected_version)
    if v.archived_at:
        return {"vehicle": serialize_vehicle(v), "archived": False}
    v.archived_at = ctx.now
    from .approvals import invalidate_for_entity
    n_appr = await invalidate_for_entity(ctx, "vehicle", v.id, f"vehicle archived: {inp.reason or 'no reason given'}")
    tasks = (await ctx.db.execute(select(Task).where(Task.vehicle_id == v.id, Task.status.in_(ACTIVE_TASK)))).scalars().all()
    cancelled = []
    for t in tasks:
        await dispatch(ctx.child(), "tasks.cancel", {"task_id": t.id, "reason": f"vehicle archived: {inp.reason or ''}".strip()}, commit=False)
        cancelled.append(t.id)
    ctx.touch(v, "vehicle")
    ctx.record(f"Archived vehicle {v.stock_no}" + (f" — {inp.reason}" if inp.reason else ""), entity_kind="vehicle", entity_id=v.id,
               kind="task", state="archived", details={"approvals_invalidated": n_appr, "tasks_cancelled": cancelled})
    _state_event(ctx, v, "archived", None, "archived", inp.reason)
    ctx.emit("metrics.invalidated", aggregate_type="vehicle", aggregate_id=v.id, payload={"reason": "vehicle.archived"})
    await recompute(ctx.db, v, ctx.now)
    return {"vehicle": serialize_vehicle(v), "archived": True, "approvals_invalidated": n_appr, "tasks_cancelled": cancelled}


@command("vehicles.restore", input=ArchiveIn, perm="vehicles.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)], description="Restore an archived vehicle (tasks are not reopened automatically).")
async def vehicles_restore(ctx: CommandContext, inp: ArchiveIn) -> dict:
    v = await get_vehicle(ctx.db, inp.vehicle_id, expected_version=inp.expected_version)
    if not v.archived_at:
        return {"vehicle": serialize_vehicle(v), "restored": False}
    v.archived_at = None
    ctx.touch(v, "vehicle")
    ctx.record(f"Restored vehicle {v.stock_no}" + (f" — {inp.reason}" if inp.reason else ""), entity_kind="vehicle", entity_id=v.id,
               kind="task", state="restored")
    _state_event(ctx, v, "archived", "archived", "active", inp.reason)
    ctx.emit("metrics.invalidated", aggregate_type="vehicle", aggregate_id=v.id, payload={"reason": "vehicle.restored"})
    await recompute(ctx.db, v, ctx.now)
    return {"vehicle": serialize_vehicle(v), "restored": True}


@command("vehicles.set_asking_price", input=PriceIn, perm="vehicles.write", action_class="owner_only", approval_kind="price_change",
         records=lambda p: [("vehicle", p.vehicle_id)], summary=lambda p: f"Set asking price {p.amount} {p.currency}",
         consequence=lambda p: {"amount": p.amount, "currency": p.currency, "scope": "asking_price"},
         description="Owner approves the asking price (price_approved_at). Listing packages read this value.")
async def vehicles_set_asking_price(ctx: CommandContext, inp: PriceIn) -> dict:
    cur = inp.currency.upper()
    if len(cur) != 3:
        raise ValidationFailed("currency must be an ISO code")
    try:
        amount = quantize(parse_amount(inp.amount), cur)
    except ValueError:
        raise ValidationFailed("amount must be a decimal")
    if amount < 0:
        raise ValidationFailed("amount cannot be negative")
    v = await get_vehicle(ctx.db, inp.vehicle_id, expected_version=inp.expected_version)
    old = (money_str(v.asking_price), v.asking_currency)
    v.asking_price = amount
    v.asking_currency = cur
    v.price_approved_at = ctx.now
    prior = await _current_fact(ctx.db, v.id, "asking_price")
    if prior is not None:
        prior.is_current = False
        prior.status = "outdated"
    f = _new_fact(ctx, v, "asking_price", str(amount), status="confirmed", source_kind="owner", source_ref=inp.note,
                  currency=cur, supersedes=prior)
    ctx.touch(v, "vehicle")
    ctx.record(f"Asking price approved: {amount} {cur} ({v.stock_no})", entity_kind="vehicle", entity_id=v.id, kind="fact",
               state="approved", visibility="all", details={"from": old, "to": [str(amount), cur], "note": inp.note})
    ctx.emit("vehicle.changed", aggregate_type="vehicle", aggregate_id=v.id, aggregate_version=v.version,
             payload={"vehicle_id": v.id, "fact": "asking_price", "amount": str(amount), "currency": cur})
    ctx.emit("metrics.invalidated", aggregate_type="vehicle", aggregate_id=v.id, payload={"reason": "asking_price"})
    await recompute(ctx.db, v, ctx.now)
    return {"vehicle": serialize_vehicle(v), "fact": serialize_fact(f)}


# ── timeline projection (read) ───────────────────────────────────────────────
async def timeline(db: AsyncSession, v: Vehicle, now: datetime | None = None) -> dict:
    now = ensure_aware(now) or datetime.now(timezone.utc)
    rows = await milestones_of(db, v.id, include_history=True)
    current = [serialize_milestone(m) for m in rows if m.is_current]
    history = [serialize_milestone(m) for m in rows if not m.is_current]
    stage_since = None
    for h in reversed(v.state_history or []):
        if h.get("dimension") == "recon":
            stage_since = h.get("at")
            break
    stage_since_dt = ensure_aware(datetime.fromisoformat(stage_since)) if stage_since else ensure_aware(v.created_at)
    acq = ensure_aware(v.acquired_at)
    upcoming = [m for m in current if m["status"] in ("planned", "estimated") and m["at"] and ensure_aware(datetime.fromisoformat(m["at"])) >= now]
    return {
        "vehicle_id": v.id, "stock_no": v.stock_no, "as_of": now.isoformat(),
        "milestones": current, "history": history,
        "days_since_acquisition": (now - acq).days if acq else None,
        "acquisition_label": None if acq else "Not recorded",
        "current_stage": v.recon_state, "stage_since": stage_since_dt.isoformat() if stage_since_dt else None,
        "days_in_stage": (now - stage_since_dt).days if stage_since_dt else None,
        "next_action": {"title": v.next_action, "owner_id": v.next_action_owner_id, "due_at": iso(v.next_action_due_at)},
        "upcoming_7d": [m for m in upcoming if (ensure_aware(datetime.fromisoformat(m["at"])) - now).days <= 7],
        "upcoming_30d": [m for m in upcoming if (ensure_aware(datetime.fromisoformat(m["at"])) - now).days <= 30],
    }
