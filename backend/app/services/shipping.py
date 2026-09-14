"""Shipments, legs, milestones and the adaptive shipping-quote case (spec §8.3, G06–G08, K04 shape).

- Milestones are independent (vessel arrival, discharge, release, carrier booked, pickup, received) and carry
  planned / estimated / completed status with a source. A container-wide notice applies to every member vehicle;
  a per-vehicle row is an exception that survives later container notices. Storage deadlines need source evidence.
- Completed member-vehicle milestones are bridged to the vehicles domain (`vehicles.record_milestone`) when that
  command is registered; otherwise a `milestone.changed` event carries the payload.
- Quote case (Montway journey): start_case gathers only recorded facts (missing ones become needs_information),
  quotes.request is consequential (exact approval shows the data shared and the recipients; scope changes need a
  new approval), replies are matched and ambiguous ones go to clarifying, comparison labels weak evidence, and
  forwarding and booking are separate approvals. The case waits durably with next_check_at.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import select

from ..core.errors import Blocked, Conflict, Denied, DomainError, NotFound, Unsupported, ValidationFailed
from ..core.ids import stable_hash
from ..core.money import parse_amount, quantize
from ..core.time import PHOENIX, TOKYO, ensure_aware, fmt_local
from ..domain import jobs
from ..domain.commands import REGISTRY, CommandContext, command, dispatch
from ..models.contacts import Contact
from ..models.runtime import ExternalAction, Permission
from ..models.shipping import (LEG_KINDS, LEG_STATUSES, MILESTONE_KINDS, MILESTONE_SOURCE_KINDS, MILESTONE_STATUSES,
                               SHIPMENT_STATUSES, Shipment, ShipmentLeg, ShipmentMilestone, ShipmentQuote)
from ..models.tasks import Case
from ..models.vehicles import Vehicle, VehicleFact
from . import approvals as approvals_svc

log = logging.getLogger("azkt.shipping")

# shipment milestone -> vehicle timeline milestone kind (vehicles domain MILESTONE_KINDS)
VEHICLE_MILESTONE_MAP = {"vessel_departed": "on_vessel", "vessel_arrival": "arrived_port", "discharge": "arrived_port",
                         "release": "released", "carrier_booked": None, "pickup": None, "received": "received"}
QUOTE_FOLLOWUP_DAYS = 2
COMPARISON_WINDOW_DAYS = 180
# progress statuses come only from sourced milestones (shipments.record_milestone); never set by hand
MILESTONE_DERIVED_STATUSES = ("in_transit", "at_port", "released", "domestic", "received")
VENDOR_EMAIL_SENDER = None   # adapter hook: async fn(payload: dict, action) -> receipt. None = no vendor email adapter.


def _iso(dt: datetime | None) -> str | None:
    return ensure_aware(dt).isoformat() if dt else None


def _dec(d: Decimal | None) -> str | None:
    """Plain decimal text (never exponent notation from the driver)."""
    return None if d is None else format(d, "f")


def _dual(dt: datetime | None) -> dict:
    if dt is None:
        return {"utc": None, "phoenix": "Not recorded", "tokyo": "Not recorded"}
    return {"utc": _iso(dt), "phoenix": fmt_local(dt, PHOENIX), "tokyo": fmt_local(dt, TOKYO)}


def _dt(v) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return ensure_aware(v)
    from ..core.time import parse_iso
    try:
        return parse_iso(str(v))
    except ValueError:
        raise ValidationFailed(f"invalid datetime {v!r}")


def route_key(frm: str | None, to: str | None) -> str | None:
    if not frm or not to:
        return None
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()  # noqa: E731
    return f"{norm(frm)}|{norm(to)}"


# ── serializers ──────────────────────────────────────────────────────────────
def serialize_shipment(s: Shipment) -> dict:
    return {
        "id": s.id, "version": s.version, "ref": s.ref, "status": s.status, "vehicle_ids": list(s.vehicle_ids or []),
        "container_no": s.container_no, "vessel": s.vessel, "voyage": s.voyage, "route_from": s.route_from, "route_to": s.route_to,
        "exporter_contact_id": s.exporter_contact_id, "eta": _dual(s.eta_at), "eta_source": s.eta_source,
        "storage_deadline": _dual(s.storage_deadline_at), "storage_deadline_source": s.storage_deadline_source,
        "storage_deadline_source_ref": s.storage_deadline_source_ref, "storage_deadline_note": s.storage_deadline_note,
        "case_id": s.case_id, "notes": s.notes, "exception_summary": s.exception_summary, "dedupe_key": s.dedupe_key,
        "extra": dict(s.extra or {}), "created_at": _iso(s.created_at), "updated_at": _iso(s.updated_at),
    }


def serialize_leg(l: ShipmentLeg) -> dict:
    return {
        "id": l.id, "version": l.version, "shipment_id": l.shipment_id, "kind": l.kind, "status": l.status, "vehicle_id": l.vehicle_id,
        "carrier_contact_id": l.carrier_contact_id, "carrier_name": l.carrier_name, "driver_contact": l.driver_contact,
        "booking_ref": l.booking_ref, "booked_at": _iso(l.booked_at), "appointment": _dual(l.appointment_at),
        "pickup_at": _iso(l.pickup_at), "delivered_at": _iso(l.delivered_at),
        "amount": _dec(l.amount), "currency": l.currency, "quote_id": l.quote_id,
        "approval_id": l.approval_id, "evidence": list(l.evidence or []), "conditions": l.conditions,
        "route_from": l.route_from, "route_to": l.route_to, "notes": l.notes, "cancelled_at": _iso(l.cancelled_at),
        "extra": dict(l.extra or {}), "created_at": _iso(l.created_at),
    }


def serialize_milestone(m: ShipmentMilestone) -> dict:
    """K04 shape: kind, planned|estimated|completed, sourced time (or Not recorded), source, scope."""
    return {
        "id": m.id, "version": m.version, "shipment_id": m.shipment_id, "vehicle_id": m.vehicle_id, "kind": m.kind, "status": m.status,
        "at": _dual(m.at), "source_kind": m.source_kind, "source_ref": m.source_ref, "note": m.note,
        "applies_to": m.applies_to, "exception": bool(m.exception), "supersedes_id": m.supersedes_id, "is_current": bool(m.is_current),
        "actor_id": m.actor_id, "recorded_at": _iso(m.recorded_at), "extra": dict(m.extra or {}),
    }


def serialize_quote(q: ShipmentQuote, action: ExternalAction | None = None) -> dict:
    return {
        "id": q.id, "version": q.version, "shipment_id": q.shipment_id, "vehicle_id": q.vehicle_id, "case_id": q.case_id, "leg_id": q.leg_id,
        "buyer_contact_id": q.buyer_contact_id, "vendor_contact_id": q.vendor_contact_id, "vendor_name": q.vendor_name, "status": q.status,
        "route_from": q.route_from, "route_to": q.route_to, "route_key": q.route_key, "service": q.service, "operability": q.operability,
        "dimensions": dict(q.dimensions or {}), "size_class": q.size_class, "timing_window": dict(q.timing_window or {}),
        "needs_information": list(q.needs_information or []), "request_payload": dict(q.request_payload or {}),
        "request_payload_hash": q.request_payload_hash, "recipients": list(q.recipients or []), "channel": q.channel,
        "request_approval_id": q.request_approval_id, "request_action_id": q.request_action_id,
        "request_action_state": action.state if action else None, "request_receipt": dict(action.receipt or {}) if action else {},
        "requested_at": _iso(q.requested_at), "received_at": _iso(q.received_at), "reply_message_id": q.reply_message_id,
        "amount": _dec(q.amount), "currency": q.currency, "binding": q.binding, "scope": q.scope,
        "inclusions": list(q.inclusions or []), "exclusions": list(q.exclusions or []), "timing": q.timing,
        "expires_at": _iso(q.expires_at), "reply_extracted": dict(q.reply_extracted or {}), "clarification_task_id": q.clarification_task_id,
        "comparison": dict(q.comparison or {}), "forward_approval_id": q.forward_approval_id, "forward_action_id": q.forward_action_id,
        "forwarded_at": _iso(q.forwarded_at), "forward_payload": dict(q.forward_payload or {}), "booking_approval_id": q.booking_approval_id,
        "booking_action_id": q.booking_action_id, "booked_at": _iso(q.booked_at), "booking": dict(q.booking or {}),
        "next_check_at": _iso(q.next_check_at), "check_count": q.check_count, "extra": dict(q.extra or {}),
        "created_at": _iso(q.created_at), "updated_at": _iso(q.updated_at),
    }


# ── loaders ──────────────────────────────────────────────────────────────────
async def _shipment(ctx: CommandContext, shipment_id: str, expected_version: int | None = None) -> Shipment:
    s = (await ctx.db.execute(select(Shipment).where(Shipment.id == shipment_id).with_for_update())).scalar_one_or_none()
    if s is None:
        raise NotFound("shipment not found")
    if expected_version is not None and s.version != expected_version:
        raise Conflict("shipment changed since you loaded it", current_version=s.version)
    return s


async def _quote(ctx: CommandContext, quote_id: str, expected_version: int | None = None) -> ShipmentQuote:
    q = (await ctx.db.execute(select(ShipmentQuote).where(ShipmentQuote.id == quote_id).with_for_update())).scalar_one_or_none()
    if q is None:
        raise NotFound("quote not found")
    if expected_version is not None and q.version != expected_version:
        raise Conflict("quote changed since you loaded it", current_version=q.version)
    return q


async def _leg(ctx: CommandContext, leg_id: str, expected_version: int | None = None) -> ShipmentLeg:
    l = (await ctx.db.execute(select(ShipmentLeg).where(ShipmentLeg.id == leg_id).with_for_update())).scalar_one_or_none()
    if l is None:
        raise NotFound("shipment leg not found")
    if expected_version is not None and l.version != expected_version:
        raise Conflict("leg changed since you loaded it", current_version=l.version)
    return l


def _emit_shipment(ctx: CommandContext, s: Shipment, change: str, **extra) -> None:
    ctx.emit("shipment.changed", aggregate_type="shipment", aggregate_id=s.id, aggregate_version=s.version,
             payload={"shipment_id": s.id, "change": change, "status": s.status, "vehicle_ids": list(s.vehicle_ids or []), **extra})


async def _check_vehicles(ctx: CommandContext, ids: list[str]) -> list[str]:
    ids = list(dict.fromkeys(i for i in ids if i))
    if not ids:
        return []
    found = {v.id for v in (await ctx.db.execute(select(Vehicle).where(Vehicle.id.in_(ids)))).scalars().all()}
    missing = [i for i in ids if i not in found]
    if missing:
        raise NotFound("vehicle not found", vehicle_ids=missing)
    return ids


# ── vehicle milestone bridge ────────────────────────────────────────────────
async def bridge_vehicle_milestone(ctx: CommandContext, *, vehicle_id: str, kind: str, status: str, at: datetime | None,
                                   source_kind: str, source_ref: str | None, shipment_id: str | None = None, note: str | None = None) -> dict:
    """Add the matching VehicleMilestone through the vehicles domain when its command exists; otherwise emit
    `milestone.changed` with the same payload (contract: vehicles.record_milestone(vehicle_id, kind, status, at,
    source_kind, source_ref, note, shipment_id))."""
    payload = {"vehicle_id": vehicle_id, "kind": kind, "status": status, "at": _iso(at), "source_kind": source_kind,
               "source_ref": source_ref, "note": note, "shipment_id": shipment_id}
    try:
        from . import vehicles as _vehicles  # noqa: F401 - lazy: registers the command when the module exists
    except Exception:  # noqa: BLE001
        pass
    if "vehicles.record_milestone" in REGISTRY:
        try:
            res = await dispatch(ctx.child(), "vehicles.record_milestone", payload, commit=False)
            return {"bridged": True, "result": res.data}
        except (ValidationFailed, Blocked, Denied) as e:
            # the shipment fact stands; the vehicle timeline refused it (no sourced time, future date, record scope ...)
            ctx.record(f"Vehicle milestone not bridged: {e.message}", entity_kind="vehicle", entity_id=vehicle_id,
                       kind="system", state="unbridged", exception=True, details={**e.detail, "reason": e.code})
    ctx.emit("milestone.changed", aggregate_type="vehicle", aggregate_id=vehicle_id, payload={**payload, "origin": "shipment"})
    return {"bridged": False, "reason": "vehicles.record_milestone not available; milestone.changed emitted"}


# ── shipments ───────────────────────────────────────────────────────────────
class ShipmentCreateIn(BaseModel):
    ref: str | None = None
    vehicle_ids: list[str] = Field(default_factory=list)
    container_no: str | None = None
    vessel: str | None = None
    voyage: str | None = None
    route_from: str | None = None
    route_to: str | None = None
    exporter_contact_id: str | None = None
    eta_at: datetime | None = None
    eta_source: str | None = None
    notes: str = ""
    dedupe_key: str | None = None
    extra: dict = Field(default_factory=dict)


@command("shipments.create", input=ShipmentCreateIn, perm="shipping.write", action_class="internal",
         records=lambda p: [("vehicle", v) for v in p.vehicle_ids],
         description="Create a shipment (container/vessel/voyage, exporter, member vehicles). Same ref/dedupe_key returns the existing one.")
async def shipments_create(ctx: CommandContext, inp: ShipmentCreateIn) -> dict:
    for key, col in ((inp.dedupe_key, Shipment.dedupe_key), (inp.ref, Shipment.ref)):
        if key:
            ex = (await ctx.db.execute(select(Shipment).where(col == key))).scalar_one_or_none()
            if ex is not None:
                return {"shipment": serialize_shipment(ex), "created": False, "matched_by": "dedupe_key" if col is Shipment.dedupe_key else "ref"}
    if inp.eta_at is not None and not inp.eta_source:
        raise Blocked("an ETA needs a source (carrier notice, exporter message ...)")
    ids = await _check_vehicles(ctx, inp.vehicle_ids)
    s = Shipment(ref=inp.ref, status="planned", vehicle_ids=ids, container_no=inp.container_no, vessel=inp.vessel, voyage=inp.voyage,
                 route_from=inp.route_from, route_to=inp.route_to, exporter_contact_id=inp.exporter_contact_id, eta_at=inp.eta_at,
                 eta_source=inp.eta_source, notes=inp.notes, dedupe_key=inp.dedupe_key, extra=inp.extra,
                 created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
    ctx.db.add(s)
    await ctx.db.flush()
    ctx.changed.append({"kind": "shipment", "id": s.id, "version": s.version})
    ctx.record(f"Created shipment {s.ref or s.id[:8]}", entity_kind="shipment", entity_id=s.id, kind="task", state=s.status,
               details={"vehicle_ids": ids, "container_no": s.container_no})
    _emit_shipment(ctx, s, "created")
    return {"shipment": serialize_shipment(s), "created": True}


class ShipmentUpdateIn(BaseModel):
    shipment_id: str
    expected_version: int | None = None
    status: str | None = None
    add_vehicle_ids: list[str] = Field(default_factory=list)
    remove_vehicle_ids: list[str] = Field(default_factory=list)
    container_no: str | None = None
    vessel: str | None = None
    voyage: str | None = None
    route_from: str | None = None
    route_to: str | None = None
    exporter_contact_id: str | None = None
    eta_at: datetime | None = None
    eta_source: str | None = None
    notes: str | None = None
    exception_summary: str | None = None
    extra: dict | None = None


@command("shipments.update", input=ShipmentUpdateIn, perm="shipping.write", action_class="internal",
         records=lambda p: [("vehicle", v) for v in (p.add_vehicle_ids + p.remove_vehicle_ids)],
         description="Edit identifiers, membership, ETA (with source), status and notes.")
async def shipments_update(ctx: CommandContext, inp: ShipmentUpdateIn) -> dict:
    s = await _shipment(ctx, inp.shipment_id, inp.expected_version)
    if inp.status is not None:
        if inp.status not in SHIPMENT_STATUSES:
            raise ValidationFailed(f"status must be one of {SHIPMENT_STATUSES}")
        if inp.status in MILESTONE_DERIVED_STATUSES:
            raise Blocked(f"status {inp.status} is derived from a sourced milestone; record the milestone instead of setting progress by hand",
                          allowed=[x for x in SHIPMENT_STATUSES if x not in MILESTONE_DERIVED_STATUSES])
        if inp.status == "exception" and not (inp.exception_summary or s.exception_summary):
            raise Blocked("an exception needs an exception_summary saying what is wrong")
        s.status = inp.status
        if inp.status != "exception" and inp.exception_summary is None:
            s.exception_summary = None
    if inp.add_vehicle_ids or inp.remove_vehicle_ids:
        add = await _check_vehicles(ctx, inp.add_vehicle_ids)
        ids = [v for v in (s.vehicle_ids or []) if v not in set(inp.remove_vehicle_ids)]
        s.vehicle_ids = ids + [v for v in add if v not in ids]
    if inp.eta_at is not None:
        if not (inp.eta_source or s.eta_source):
            raise Blocked("an ETA needs a source (carrier notice, exporter message ...)")
        s.eta_at, s.eta_source = inp.eta_at, inp.eta_source or s.eta_source
    for f in ("container_no", "vessel", "voyage", "route_from", "route_to", "exporter_contact_id", "notes", "exception_summary"):
        v = getattr(inp, f)
        if v is not None:
            setattr(s, f, v)
    if inp.extra is not None:
        s.extra = {**(s.extra or {}), **inp.extra}
    ctx.touch(s, "shipment")
    ctx.record(f"Updated shipment {s.ref or s.id[:8]}", entity_kind="shipment", entity_id=s.id, kind="task", state=s.status)
    _emit_shipment(ctx, s, "updated")
    return {"shipment": serialize_shipment(s)}


class MilestoneIn(BaseModel):
    shipment_id: str
    kind: str
    status: str = "completed"             # planned|estimated|completed
    at: datetime | None = None
    source_kind: str = "manual"
    source_ref: str | None = None
    vehicle_id: str | None = None         # None = container-wide notice for every member vehicle
    note: str | None = None
    expected_version: int | None = None


def effective_milestones(rows: list[ShipmentMilestone], vehicle_ids: list[str]) -> dict:
    """Per-vehicle effective milestone per kind: a vehicle exception wins over the container notice."""
    current = [m for m in rows if m.is_current]
    container = {m.kind: m for m in current if m.vehicle_id is None}
    per_vehicle: dict[str, dict] = {}
    for vid in vehicle_ids:
        specific = {m.kind: m for m in current if m.vehicle_id == vid}
        eff = {}
        for kind in MILESTONE_KINDS:
            m = specific.get(kind) or container.get(kind)
            eff[kind] = ({**serialize_milestone(m), "scope": "vehicle" if m.vehicle_id else "container"} if m
                         else {"kind": kind, "status": "not_recorded", "at": _dual(None), "source_kind": None, "source_ref": None, "scope": None})
        per_vehicle[vid] = eff
    return {"container": {k: serialize_milestone(m) for k, m in container.items()}, "per_vehicle": per_vehicle}


@command("shipments.record_milestone", input=MilestoneIn, perm="shipping.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)] if p.vehicle_id else [],
         description="Record a planned/estimated/completed milestone with its source. Container-wide applies to every member "
                     "vehicle; a per-vehicle row is an exception that survives later container notices. Completed member milestones "
                     "are bridged to the vehicle timeline.")
async def shipments_record_milestone(ctx: CommandContext, inp: MilestoneIn) -> dict:
    if inp.kind not in MILESTONE_KINDS:
        raise ValidationFailed(f"kind must be one of {MILESTONE_KINDS}")
    if inp.status not in MILESTONE_STATUSES:
        raise ValidationFailed(f"status must be one of {MILESTONE_STATUSES}")
    if inp.source_kind not in MILESTONE_SOURCE_KINDS:
        raise ValidationFailed(f"source_kind must be one of {MILESTONE_SOURCE_KINDS}")
    if inp.status in ("estimated", "completed") and inp.at is None:
        raise Blocked(f"a {inp.status} milestone needs a sourced time; leave it planned/unrecorded instead of inventing a date")
    if inp.status in ("estimated", "completed") and inp.source_kind != "manual" and not inp.source_ref:
        raise Blocked("a sourced milestone needs source_ref (notice / message / document id)")
    s = await _shipment(ctx, inp.shipment_id, inp.expected_version)
    if inp.vehicle_id and inp.vehicle_id not in (s.vehicle_ids or []):
        raise Blocked("vehicle is not a member of this shipment", vehicle_id=inp.vehicle_id)
    scope_vid = inp.vehicle_id
    prior = (await ctx.db.execute(select(ShipmentMilestone).where(
        ShipmentMilestone.shipment_id == s.id, ShipmentMilestone.kind == inp.kind, ShipmentMilestone.is_current.is_(True),
        (ShipmentMilestone.vehicle_id == scope_vid) if scope_vid else ShipmentMilestone.vehicle_id.is_(None)))).scalars().first()
    if prior is not None and prior.status == inp.status and _iso(prior.at) == _iso(inp.at) and prior.source_ref == inp.source_ref:
        return {"milestone": serialize_milestone(prior), "created": False, "idempotent": True,
                "effective": effective_milestones(await _milestone_rows(ctx, s.id), list(s.vehicle_ids or []))}
    m = ShipmentMilestone(shipment_id=s.id, vehicle_id=scope_vid, kind=inp.kind, status=inp.status, at=inp.at, source_kind=inp.source_kind,
                          source_ref=inp.source_ref, note=inp.note, applies_to="vehicle" if scope_vid else "container",
                          exception=bool(scope_vid), supersedes_id=prior.id if prior else None, is_current=True,
                          actor_id=ctx.actor.user_id, recorded_at=ctx.now, created_by=ctx.actor.user_id)
    if prior is not None:
        prior.is_current = False
        ctx.touch(prior, "shipment_milestone")
    ctx.db.add(m)
    await ctx.db.flush()
    ctx.changed.append({"kind": "shipment_milestone", "id": m.id, "version": m.version})
    # exceptions recorded per vehicle stay in force; the container notice covers the rest
    exceptions = set()
    if scope_vid is None:
        exceptions = {x.vehicle_id for x in (await ctx.db.execute(select(ShipmentMilestone).where(
            ShipmentMilestone.shipment_id == s.id, ShipmentMilestone.kind == inp.kind, ShipmentMilestone.is_current.is_(True),
            ShipmentMilestone.vehicle_id.is_not(None)))).scalars().all()}
    affected = [scope_vid] if scope_vid else [v for v in (s.vehicle_ids or []) if v not in exceptions]
    _apply_shipment_status(s, inp.kind, inp.status)
    ctx.touch(s, "shipment")
    ctx.record(f"Milestone {inp.kind.replace('_', ' ')} {inp.status}: {s.ref or s.id[:8]}" + (f" (vehicle {scope_vid[:8]})" if scope_vid else " (container)"),
               entity_kind="shipment", entity_id=s.id, kind="fact", state=inp.status, sources=[inp.source_ref] if inp.source_ref else None,
               details={"milestone_id": m.id, "kind": inp.kind, "affected_vehicle_ids": affected, "preserved_exceptions": sorted(exceptions)})
    _emit_shipment(ctx, s, "milestone", kind=inp.kind, milestone_status=inp.status, affected_vehicle_ids=affected)
    ctx.emit("milestone.changed", aggregate_type="shipment", aggregate_id=s.id, aggregate_version=s.version,
             payload={"shipment_id": s.id, "kind": inp.kind, "status": inp.status, "at": _iso(inp.at), "source_kind": inp.source_kind,
                      "source_ref": inp.source_ref, "vehicle_ids": affected, "scope": m.applies_to})
    bridged = {}
    vkind = VEHICLE_MILESTONE_MAP.get(inp.kind)
    if vkind and inp.status == "completed":
        for vid in affected:
            bridged[vid] = await bridge_vehicle_milestone(ctx, vehicle_id=vid, kind=vkind, status="completed", at=inp.at,
                                                          source_kind=inp.source_kind, source_ref=inp.source_ref, shipment_id=s.id, note=inp.note)
    return {"milestone": serialize_milestone(m), "created": True, "affected_vehicle_ids": affected,
            "preserved_exceptions": sorted(exceptions), "bridged": bridged,
            "effective": effective_milestones(await _milestone_rows(ctx, s.id), list(s.vehicle_ids or []))}


def _apply_shipment_status(s: Shipment, kind: str, status: str) -> None:
    if status != "completed":
        return
    order = {"vessel_departed": "in_transit", "vessel_arrival": "at_port", "discharge": "at_port", "release": "released",
             "carrier_booked": "released", "pickup": "domestic", "received": "received"}
    rank = {k: i for i, k in enumerate(SHIPMENT_STATUSES)}
    target = order.get(kind)
    if target and rank.get(target, -1) > rank.get(s.status, -1) and s.status != "exception":
        s.status = target


async def _milestone_rows(ctx: CommandContext, shipment_id: str) -> list[ShipmentMilestone]:
    return (await ctx.db.execute(select(ShipmentMilestone).where(ShipmentMilestone.shipment_id == shipment_id)
                                 .order_by(ShipmentMilestone.created_at))).scalars().all()


class StorageDeadlineIn(BaseModel):
    shipment_id: str
    at: datetime
    source_kind: str
    source_ref: str = Field(min_length=1)
    note: str | None = None
    expected_version: int | None = None


@command("shipments.set_storage_deadline", input=StorageDeadlineIn, perm="shipping.write", action_class="internal",
         description="Storage/free-time deadline with required source evidence (port notice, carrier message).")
async def set_storage_deadline(ctx: CommandContext, inp: StorageDeadlineIn) -> dict:
    if inp.source_kind not in MILESTONE_SOURCE_KINDS or inp.source_kind == "manual":
        raise Blocked("a storage deadline needs source evidence (document/message/port/carrier/provider), not a manual guess")
    s = await _shipment(ctx, inp.shipment_id, inp.expected_version)
    s.storage_deadline_at, s.storage_deadline_source = inp.at, inp.source_kind
    s.storage_deadline_source_ref, s.storage_deadline_note = inp.source_ref, inp.note
    ctx.touch(s, "shipment")
    ctx.record(f"Storage deadline {fmt_local(inp.at, PHOENIX)}: {s.ref or s.id[:8]}", entity_kind="shipment", entity_id=s.id, kind="fact",
               state=s.status, sources=[inp.source_ref])
    _emit_shipment(ctx, s, "storage_deadline", at=_iso(inp.at), source_ref=inp.source_ref)
    return {"shipment": serialize_shipment(s)}


# ── legs ─────────────────────────────────────────────────────────────────────
class LegAddIn(BaseModel):
    shipment_id: str
    kind: str
    vehicle_id: str | None = None
    status: str = "planned"
    carrier_contact_id: str | None = None
    carrier_name: str | None = None
    driver_contact: str | None = None
    route_from: str | None = None
    route_to: str | None = None
    appointment_at: datetime | None = None
    conditions: str = ""
    notes: str = ""
    extra: dict = Field(default_factory=dict)


@command("shipments.add_leg", input=LegAddIn, perm="shipping.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)] if p.vehicle_id else [],
         description="Add an export/ocean/port/domestic leg. Booking state changes only through quotes.book.")
async def shipments_add_leg(ctx: CommandContext, inp: LegAddIn) -> dict:
    if inp.kind not in LEG_KINDS:
        raise ValidationFailed(f"kind must be one of {LEG_KINDS}")
    if inp.status not in ("planned", "quoted", "in_progress", "complete"):
        raise ValidationFailed("status must be planned|quoted|in_progress|complete (booked comes from quotes.book)")
    s = await _shipment(ctx, inp.shipment_id)
    if inp.vehicle_id and inp.vehicle_id not in (s.vehicle_ids or []):
        raise Blocked("vehicle is not a member of this shipment", vehicle_id=inp.vehicle_id)
    l = ShipmentLeg(shipment_id=s.id, kind=inp.kind, status=inp.status, vehicle_id=inp.vehicle_id, carrier_contact_id=inp.carrier_contact_id,
                    carrier_name=inp.carrier_name, driver_contact=inp.driver_contact, route_from=inp.route_from or s.route_from,
                    route_to=inp.route_to or s.route_to, appointment_at=inp.appointment_at, conditions=inp.conditions, notes=inp.notes,
                    extra=inp.extra, created_by=ctx.actor.user_id)
    ctx.db.add(l)
    await ctx.db.flush()
    ctx.changed.append({"kind": "shipment_leg", "id": l.id, "version": l.version})
    ctx.record(f"Added {inp.kind} leg to shipment {s.ref or s.id[:8]}", entity_kind="shipment", entity_id=s.id, kind="task", state=l.status,
               details={"leg_id": l.id})
    _emit_shipment(ctx, s, "leg_added", leg_id=l.id, kind=inp.kind)
    return {"leg": serialize_leg(l)}


class LegUpdateIn(BaseModel):
    leg_id: str
    expected_version: int | None = None
    status: str | None = None
    carrier_contact_id: str | None = None
    carrier_name: str | None = None
    driver_contact: str | None = None
    appointment_at: datetime | None = None
    pickup_at: datetime | None = None
    delivered_at: datetime | None = None
    evidence: list | None = None
    conditions: str | None = None
    notes: str | None = None
    extra: dict | None = None


@command("shipments.update_leg", input=LegUpdateIn, perm="shipping.write", action_class="internal",
         description="Update leg progress (driver, appointment, pickup/delivery with evidence). Cannot set booked; use quotes.book.")
async def shipments_update_leg(ctx: CommandContext, inp: LegUpdateIn) -> dict:
    l = await _leg(ctx, inp.leg_id, inp.expected_version)
    if inp.status is not None:
        if inp.status not in LEG_STATUSES or inp.status == "booked":
            raise ValidationFailed("status must be planned|quoted|in_progress|complete|cancelled (booked comes from quotes.book)")
        if inp.status == "complete" and not (inp.evidence or l.evidence):
            raise Blocked("completing a leg needs delivery evidence")
        l.status = inp.status
        if inp.status == "cancelled":
            l.cancelled_at = ctx.now
    for f in ("carrier_contact_id", "carrier_name", "driver_contact", "appointment_at", "pickup_at", "delivered_at", "conditions", "notes"):
        v = getattr(inp, f)
        if v is not None:
            setattr(l, f, v)
    if inp.evidence is not None:
        l.evidence = list(l.evidence or []) + list(inp.evidence)
    if inp.extra is not None:
        l.extra = {**(l.extra or {}), **inp.extra}
    ctx.touch(l, "shipment_leg")
    ctx.record(f"Updated {l.kind} leg ({l.status})", entity_kind="shipment", entity_id=l.shipment_id, kind="task", state=l.status,
               details={"leg_id": l.id})
    ctx.emit("shipment.changed", aggregate_type="shipment", aggregate_id=l.shipment_id, payload={"shipment_id": l.shipment_id, "change": "leg_updated", "leg_id": l.id})
    return {"leg": serialize_leg(l)}


class LegRefIn(BaseModel):
    leg_id: str
    reason: str | None = None
    expected_version: int | None = None


@command("shipments.remove_leg", input=LegRefIn, perm="shipping.write", action_class="internal",
         description="Cancel a leg (kept for history). A booked leg cannot be removed silently.")
async def shipments_remove_leg(ctx: CommandContext, inp: LegRefIn) -> dict:
    l = await _leg(ctx, inp.leg_id, inp.expected_version)
    if l.status == "booked":
        raise Blocked("leg is booked; cancel the booking with the carrier first and record it", leg_id=l.id)
    l.status, l.cancelled_at = "cancelled", ctx.now
    l.notes = (l.notes + "\n" if l.notes else "") + f"cancelled: {inp.reason or ''}".strip()
    ctx.touch(l, "shipment_leg")
    ctx.record(f"Cancelled {l.kind} leg", entity_kind="shipment", entity_id=l.shipment_id, kind="task", state="cancelled", details={"leg_id": l.id})
    return {"leg": serialize_leg(l)}


# ── quote case (Montway journey) ────────────────────────────────────────────
DIMENSION_KEYS = ("length_mm", "width_mm", "height_mm", "weight_kg")


async def _vehicle_facts(db, vehicle_id: str) -> dict[str, VehicleFact]:
    rows = (await db.execute(select(VehicleFact).where(VehicleFact.vehicle_id == vehicle_id, VehicleFact.is_current.is_(True)))).scalars().all()
    return {f.key: f for f in rows}


def _size_class(v: Vehicle | None) -> str:
    if v is None:
        return "unknown"
    text = f"{v.make or ''} {v.model or ''} {v.title or ''}".lower()
    if any(k in text for k in ("hijet", "carry", "acty", "sambar", "minicab", "hijet truck", "kei truck")):
        return "kei_van" if "van" in text or "cargo" in text else "kei_truck"
    return "unknown"


async def gather_quote_facts(db, *, vehicle: Vehicle | None, shipment: Shipment | None, destination: str | None, origin: str | None,
                             timing: dict | None) -> tuple[dict, list[dict]]:
    """Only recorded facts. Anything missing is listed in needs_information; nothing is invented (spec §8.3 step 1)."""
    needs: list[dict] = []
    facts: dict = {}
    if vehicle is None:
        needs.append({"field": "vehicle", "reason": "no vehicle linked"})
    else:
        facts["vehicle"] = {"id": vehicle.id, "stock_no": vehicle.stock_no, "title": vehicle.title, "make": vehicle.make, "model": vehicle.model,
                            "model_year": vehicle.model_year, "frame_no": vehicle.frame_no_raw, "size_class": _size_class(vehicle)}
        vf = await _vehicle_facts(db, vehicle.id)
        dims = {}
        for k in DIMENSION_KEYS:
            f = vf.get(k)
            val = (str(f.value_num) if f is not None and f.value_num is not None else (f.value if f is not None else None))
            if val is None:
                val = (vehicle.extra or {}).get(k)
            if val is not None:
                dims[k] = {"value": val, "source": (f.source_kind if f is not None else "vehicle.extra"), "status": (f.status if f is not None else "reported")}
            else:
                needs.append({"field": k, "reason": "dimension not recorded"})
        facts["dimensions"] = dims
        op = vf.get("operable") or vf.get("operability")
        op_val = (op.value if op is not None else (vehicle.extra or {}).get("operability"))
        if op_val is None:
            facts["operability"] = "unknown"
            needs.append({"field": "operability", "reason": "running / inoperable not recorded"})
        else:
            s = str(op_val).lower()
            facts["operability"] = "running" if s in ("true", "yes", "running", "runs", "operable", "1") else ("inoperable" if s in ("false", "no", "inoperable", "0") else s)
        buyer = await db.get(Contact, vehicle.buyer_contact_id) if vehicle.buyer_contact_id else None
        if buyer is None:
            needs.append({"field": "buyer", "reason": "no buyer linked to the vehicle"})
        else:
            facts["buyer"] = {"contact_id": buyer.id, "name": buyer.name}
            dest = destination or (buyer.extra or {}).get("delivery_address") or (buyer.extra or {}).get("address")
            if dest:
                facts["route_to"] = dest
    if "route_to" not in facts:
        if destination:
            facts["route_to"] = destination
        else:
            needs.append({"field": "route_to", "reason": "destination not recorded"})
    frm = origin or (shipment.route_to if shipment else None)
    if frm:
        facts["route_from"] = frm
    else:
        needs.append({"field": "route_from", "reason": "pickup location (port / yard) not recorded"})
    tw = dict(timing or {})
    if not tw:
        if shipment is not None and shipment.storage_deadline_at:
            tw = {"latest": _iso(shipment.storage_deadline_at), "source": f"storage deadline ({shipment.storage_deadline_source})"}
        elif shipment is not None and shipment.eta_at:
            tw = {"earliest": _iso(shipment.eta_at), "source": f"ETA ({shipment.eta_source})"}
    if tw:
        facts["timing"] = tw
    else:
        needs.append({"field": "timing", "reason": "requested timing not recorded"})
    return facts, needs


class QuoteStartIn(BaseModel):
    vehicle_id: str | None = None
    shipment_id: str | None = None
    leg_id: str | None = None
    vendor_contact_id: str | None = None
    vendor_name: str | None = None
    origin: str | None = None
    destination: str | None = None
    service: str | None = None            # open|enclosed
    timing: dict | None = None            # {earliest, latest, source}
    dedupe_key: str | None = None
    note: str | None = None


@command("quotes.start_case", input=QuoteStartIn, perm="shipping.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)] if p.vehicle_id else [],
         description="Open a shipping-quote case and a draft quote from recorded facts (buyer/vehicle/route/dimensions/operability/"
                     "timing). Missing data becomes needs_information; no values are invented.")
async def quotes_start_case(ctx: CommandContext, inp: QuoteStartIn) -> dict:
    if not inp.vehicle_id and not inp.shipment_id:
        raise ValidationFailed("vehicle_id or shipment_id required")
    if inp.dedupe_key:
        ex = (await ctx.db.execute(select(ShipmentQuote).where(ShipmentQuote.dedupe_key == inp.dedupe_key))).scalar_one_or_none()
        if ex is not None:
            return {"quote": serialize_quote(ex), "created": False, "case_id": ex.case_id}
    vehicle = await ctx.db.get(Vehicle, inp.vehicle_id) if inp.vehicle_id else None
    if inp.vehicle_id and vehicle is None:
        raise NotFound("vehicle not found")
    shipment = await ctx.db.get(Shipment, inp.shipment_id) if inp.shipment_id else None
    if inp.shipment_id and shipment is None:
        raise NotFound("shipment not found")
    if shipment is None and vehicle is not None:
        rows = (await ctx.db.execute(select(Shipment).order_by(Shipment.created_at.desc()))).scalars().all()
        shipment = next((s for s in rows if vehicle.id in (s.vehicle_ids or [])), None)
    if vehicle is None and shipment is not None and len(shipment.vehicle_ids or []) == 1:
        vehicle = await ctx.db.get(Vehicle, shipment.vehicle_ids[0])
    facts, needs = await gather_quote_facts(ctx.db, vehicle=vehicle, shipment=shipment, destination=inp.destination, origin=inp.origin, timing=inp.timing)
    if inp.service and inp.service not in ("open", "enclosed"):
        raise ValidationFailed("service must be open or enclosed")
    if not inp.service:
        needs.append({"field": "service", "reason": "open / enclosed not specified"})
    q = ShipmentQuote(shipment_id=shipment.id if shipment else None, vehicle_id=vehicle.id if vehicle else None, leg_id=inp.leg_id,
                      buyer_contact_id=(facts.get("buyer") or {}).get("contact_id"), vendor_contact_id=inp.vendor_contact_id,
                      vendor_name=inp.vendor_name, status="needs_information" if needs else "draft",
                      route_from=facts.get("route_from"), route_to=facts.get("route_to"), route_key=route_key(facts.get("route_from"), facts.get("route_to")),
                      service=inp.service or "unknown", operability=facts.get("operability", "unknown"),
                      dimensions={k: v["value"] for k, v in (facts.get("dimensions") or {}).items()},
                      size_class=(facts.get("vehicle") or {}).get("size_class", "unknown"), timing_window=facts.get("timing") or {},
                      needs_information=needs, request_payload=facts, dedupe_key=inp.dedupe_key, extra={"note": inp.note},
                      created_by=ctx.actor.user_id)
    ctx.db.add(q)
    await ctx.db.flush()
    title = f"Shipping quote: {(facts.get('vehicle') or {}).get('title') or 'vehicle not linked'} → {facts.get('route_to') or 'destination not recorded'}"
    case = await dispatch(ctx.child(), "cases.open", {
        "title": title, "kind": "shipping_quote", "vehicle_id": vehicle.id if vehicle else None, "shipment_id": shipment.id if shipment else None,
        "contact_id": (facts.get("buyer") or {}).get("contact_id"), "summary": "Quote case opened from recorded facts",
        "next_action": ("Collect: " + ", ".join(n["field"] for n in needs)) if needs else "Request a nonbinding quote (approval required)",
        "next_check_at": ctx.now + timedelta(days=1), "extra": {"quote_id": q.id}}, commit=False)
    q.case_id = case.data["case"]["id"]
    ctx.changed.append({"kind": "shipment_quote", "id": q.id, "version": q.version})
    ctx.record(f"Quote case opened: {title}", entity_kind="shipment_quote", entity_id=q.id, kind="task", state=q.status,
               details={"needs_information": needs, "case_id": q.case_id})
    ctx.emit("quote.changed", aggregate_type="shipment_quote", aggregate_id=q.id, aggregate_version=q.version,
             payload={"quote_id": q.id, "change": "opened", "status": q.status, "case_id": q.case_id})
    return {"quote": serialize_quote(q), "created": True, "case_id": q.case_id, "needs_information": needs}


def _shared_from(facts: dict, fields: list[str]) -> dict:
    out: dict = {}
    for f in fields:
        if f in facts and facts[f] not in (None, {}, "unknown"):
            out[f] = facts[f]
    return out


def shared_payload(q: ShipmentQuote, fields: list[str]) -> dict:
    """Exactly the data that will leave the business, from the case's recorded facts only."""
    return _shared_from(dict(q.request_payload or {}), fields)


async def refresh_quote_facts(db, q: ShipmentQuote) -> tuple[dict, list[dict]]:
    """Re-gather the case facts from the records as they are now (vehicle, buyer, dimensions, operability); route and
    timing stay as recorded on the case. Nothing is written here."""
    vehicle = await db.get(Vehicle, q.vehicle_id) if q.vehicle_id else None
    shipment = await db.get(Shipment, q.shipment_id) if q.shipment_id else None
    return await gather_quote_facts(db, vehicle=vehicle, shipment=shipment, destination=q.route_to, origin=q.route_from,
                                    timing=dict(q.timing_window or {}) or None)


def _jsonable(obj):
    import json
    return json.loads(json.dumps(obj, default=str))


async def preview_quote_request(db, quote_id: str, fields: list[str]) -> dict:
    """The exact data a quote request would share right now (read-only; used to bind the approval to it)."""
    q = await db.get(ShipmentQuote, quote_id)
    if q is None:
        raise NotFound("quote not found")
    facts, _ = await refresh_quote_facts(db, q)
    return _jsonable(_shared_from(facts, fields))


DEFAULT_QUOTE_FIELDS = ("vehicle", "dimensions", "operability", "route_from", "route_to", "timing")


class QuoteRequestIn(BaseModel):
    quote_id: str
    recipients: list[str] = Field(min_length=1)
    channel: str = "email"                # email | web_form | manual
    fields: list[str] = Field(default_factory=lambda: list(DEFAULT_QUOTE_FIELDS))
    data: dict | None = None              # the exact data reviewed (preview_quote_request); the approval binds it
    message: str | None = None
    binding: str = "nonbinding"
    follow_up_days: int = Field(default=QUOTE_FOLLOWUP_DAYS, ge=1, le=30)


def _quote_summary(p: QuoteRequestIn) -> str:
    return f"Request {p.binding} shipping quote from {', '.join(p.recipients)} ({p.channel})"


def _quote_consequence(p: QuoteRequestIn) -> dict:
    return {"scope": "vendor quote request", "moves_money": False, "fields_shared": list(p.fields),
            "data_shared": p.data, "message": p.message,
            "targets": {"recipients": list(p.recipients), "channel": p.channel}}


async def standing_field_scope_violation(db, inp: QuoteRequestIn) -> list[str]:
    """Fields outside every active standing permission that covers these recipients. A permission with an empty
    `fields` list is unbounded; a bounded one limits the data that may leave the business (spec §11.2 vendor quote row)."""
    import fnmatch
    rows = (await db.execute(select(Permission).where(Permission.status == "active"))).scalars().all()
    wanted = [r.lower() for r in inp.recipients]
    covering = [p for p in rows if fnmatch.fnmatchcase("quotes.request", p.action_pattern)
                and (not p.recipients or all(r in [x.lower() for x in p.recipients] for r in wanted))]
    if not covering or any(not p.fields for p in covering):
        return []
    allowed = set().union(*[set(p.fields) for p in covering])
    return [f for f in inp.fields if f not in allowed]


async def quote_request_revalidate(ctx: CommandContext, inp: QuoteRequestIn, approval) -> list[str]:
    """Immediately before execution: the quote is still requestable, the recipients/fields are the reviewed ones and the
    recorded facts behind the shared data are still what the reviewer saw (spec §11.4)."""
    q = (await ctx.db.execute(select(ShipmentQuote).where(ShipmentQuote.id == inp.quote_id))).scalar_one_or_none()
    if q is None:
        return ["quote no longer exists"]
    reasons = []
    if q.status not in ("draft", "needs_information", "pending_approval", "requested", "clarifying"):
        reasons.append(f"quote is {q.status}")
    bound = approval.payload or {}
    if sorted(bound.get("fields") or []) != sorted(inp.fields):
        reasons.append("fields to share changed — review again")
    if sorted(r.lower() for r in (bound.get("recipients") or [])) != sorted(r.lower() for r in inp.recipients):
        reasons.append("recipients changed — review again")
    fresh, _ = await refresh_quote_facts(ctx.db, q)
    payload = _jsonable(_shared_from(fresh, inp.fields))
    missing = [f for f in inp.fields if f not in payload]
    if missing:
        reasons.append("data to share is no longer recorded: " + ", ".join(missing))
    reviewed = _jsonable(inp.data) if inp.data is not None else _jsonable(shared_payload(q, inp.fields))
    drift = [f for f in inp.fields if reviewed.get(f) != payload.get(f)]
    if drift:
        reasons.append("recorded facts changed since this scope was reviewed (" + ", ".join(drift) + ") — review again")
    return reasons


@command("quotes.request", input=QuoteRequestIn, perm="shipping.write", action_class="consequential", approval_kind="quote_request",
         records=lambda p: [("shipment_quote", p.quote_id)], summary=_quote_summary, consequence=_quote_consequence,
         limits=lambda p: {"recipients": list(p.recipients), "records": [p.quote_id]}, revalidate=quote_request_revalidate,
         description="Request a nonbinding vendor quote. The approval shows exactly the recorded data shared and the recipients; a new "
                     "recipient or extra data is a new approval. Persists an email intent (never sends inline) and waits with a next check.")
async def quotes_request(ctx: CommandContext, inp: QuoteRequestIn) -> dict:
    q = await _quote(ctx, inp.quote_id)
    if q.status in ("booked", "declined", "expired"):
        raise Blocked(f"quote is {q.status}")
    if inp.channel not in ("email", "web_form", "manual"):
        raise ValidationFailed("channel must be email|web_form|manual")
    # the data shared is always the recorded facts as they are now; the case snapshot is refreshed from the records
    facts, needs = await refresh_quote_facts(ctx.db, q)
    if _jsonable(facts) != _jsonable(q.request_payload or {}):
        q.request_payload, q.needs_information = facts, needs
        q.dimensions = {k: v["value"] for k, v in (facts.get("dimensions") or {}).items()}
        q.operability, q.size_class = facts.get("operability", "unknown"), (facts.get("vehicle") or {}).get("size_class", "unknown")
        q.buyer_contact_id = (facts.get("buyer") or {}).get("contact_id") or q.buyer_contact_id
        ctx.record("Quote case facts refreshed from records", entity_kind="shipment_quote", entity_id=q.id, kind="fact", state=q.status,
                   details={"needs_information": needs})
    payload = _jsonable(_shared_from(facts, inp.fields))
    missing = [f for f in inp.fields if f not in payload]
    if missing:
        raise Blocked("cannot share data that is not recorded; collect it first (no invented values)", missing=missing,
                      needs_information=list(q.needs_information or []))
    if inp.data is not None and _jsonable(inp.data) != payload:
        # the reviewer saw different data than the records hold now: exact approval binds content, never a substitute
        raise Blocked("the reviewed data no longer matches the recorded facts — review again",
                      reviewed=_jsonable(inp.data), recorded=payload, decision="Needs review")
    if ctx.approval is None and ctx.actor.kind != "system":
        outside = await standing_field_scope_violation(ctx.db, inp)
        if outside:
            raise Blocked("data to share is outside the standing permission's field scope; request exact approval for it",
                          fields_outside_scope=outside, decision="Needs review")
    if q.recipients and sorted(r.lower() for r in q.recipients) != sorted(r.lower() for r in inp.recipients):
        # scope change on an already requested quote: the earlier approval does not cover the new recipient set
        q.extra = {**(q.extra or {}), "scope_change": {"previous_recipients": list(q.recipients), "requested": list(inp.recipients),
                                                      "at": ctx.now.isoformat()}}
    payload_hash = stable_hash({"payload": payload, "recipients": sorted(r.lower() for r in inp.recipients), "channel": inp.channel,
                                "message": inp.message or ""})
    prior = await ctx.db.get(ExternalAction, q.request_action_id) if q.request_action_id else None
    if q.request_payload_hash == payload_hash and q.status == "requested" and prior is not None and prior.state != "cancelled":
        return {"quote": serialize_quote(q, prior), "external_action_id": q.request_action_id, "shared": payload, "sent": False,
                "idempotent": True}
    act = await approvals_svc.intend_external_action(
        ctx, command_name="quotes.request", provider="email", entity_kind="shipment_quote", entity_id=q.id,
        dedupe_key=f"quote_request:{q.id}:{payload_hash}",
        payload={"quote_id": q.id, "recipients": list(inp.recipients), "channel": inp.channel, "data": payload, "message": inp.message,
                 "binding": inp.binding, "vendor_name": q.vendor_name})
    if prior is not None and prior.id != act.id and prior.state == "intent":
        # the re-scoped request replaces a request that has not left yet: never two sends for one logical request
        await _cancel_superseded_intent(ctx, prior, reason=f"superseded by re-scoped quote request {act.id[:8]}")
    q.status, q.requested_at = "requested", ctx.now
    q.recipients, q.channel, q.binding = list(inp.recipients), inp.channel, inp.binding
    q.request_payload_hash, q.request_action_id = payload_hash, act.id
    q.request_approval_id = ctx.approval.id if ctx.approval else q.request_approval_id
    q.next_check_at = ctx.now + timedelta(days=inp.follow_up_days)
    q.extra = {**(q.extra or {}), "shared_fields": list(inp.fields)}
    ctx.touch(q, "shipment_quote")
    if q.case_id:
        await dispatch(ctx.child(), "cases.update", {"case_id": q.case_id, "status": "waiting", "waiting_on": "vendor reply",
                                                    "next_action": "Check for the vendor's reply", "next_check_at": q.next_check_at,
                                                    "summary": f"Quote request queued to {', '.join(inp.recipients)}; waiting for reply"}, commit=False)
    ctx.record(f"Quote request queued to {', '.join(inp.recipients)} ({inp.channel}); waiting for reply", entity_kind="shipment_quote",
               entity_id=q.id, kind="automation", state="requested", details={"fields": inp.fields, "external_action_id": act.id})
    ctx.emit("quote.changed", aggregate_type="shipment_quote", aggregate_id=q.id, aggregate_version=q.version,
             payload={"quote_id": q.id, "change": "requested", "next_check_at": _iso(q.next_check_at)})
    return {"quote": serialize_quote(q, act), "external_action_id": act.id, "shared": payload, "sent": False}


async def _cancel_superseded_intent(ctx: CommandContext, prior: ExternalAction, *, reason: str) -> None:
    if prior.approval_id:
        await approvals_svc.invalidate(ctx.child(), approvals_svc.ApprovalRef(approval_id=prior.approval_id, reason=reason))
    prior.state, prior.error = "cancelled", reason
    ctx.record(f"Queued quote request cancelled: {reason}", entity_kind="shipment_quote", entity_id=prior.entity_id, kind="automation",
               state="cancelled", details={"external_action_id": prior.id})


async def _manual_send_task(db, act: ExternalAction, *, title: str, recipients: list[str], content: str) -> dict:
    """No email adapter: the exact content becomes a manual sending task (deduped per action). The receipt records
    that nothing was sent; it never claims delivery."""
    from ..domain.actors import SYSTEM_ACTOR
    payload = dict(act.payload or {})
    q = await db.get(ShipmentQuote, payload.get("quote_id")) if payload.get("quote_id") else None
    ctx = CommandContext(db=db, actor=SYSTEM_ACTOR, channel="worker", correlation_id=act.correlation_id)
    res = await dispatch(ctx, "tasks.create", {
        "title": title, "type": "operational", "priority": "high", "vehicle_id": q.vehicle_id if q else None,
        "shipment_id": q.shipment_id if q else None, "instructions": content,
        "notes": "No email adapter is connected. Send this exact content to " + ", ".join(recipients) +
                 " and attach the confirmation; the system has not sent anything.",
        "evidence_required": [{"kind": "note", "label": "Confirmation the message was sent", "min": 1}],
        "source_kind": "shipping", "source_id": act.id, "dedupe": False}, commit=False)  # one task per fenced action, exact content
    return {"provider": "email", "sent": False, "state": "manual_send_required", "recipients": recipients,
            "task_id": res.data["task"]["id"], "note": "No email adapter is connected; a manual sending task holds the exact content. "
                                                       "This receipt does not claim delivery."}


def _quote_request_text(payload: dict) -> str:
    data = payload.get("data") or {}
    lines = [f"{payload.get('binding', 'nonbinding').title()} shipping quote request" + (f" — {payload['vendor_name']}" if payload.get("vendor_name") else "")]
    for k, v in data.items():
        lines.append(f"{k}: {v}")
    if payload.get("message"):
        lines += ["", payload["message"]]
    return "\n".join(lines)


@approvals_svc.executor("quotes.request")
async def _exec_quote_request(db, act: ExternalAction) -> dict:
    """Stub executor: without a vendor email adapter the intent becomes a manual sending task, never claimed sent."""
    if VENDOR_EMAIL_SENDER is None:
        p = dict(act.payload or {})
        return await _manual_send_task(db, act, title=f"Send shipping quote request to {', '.join(p.get('recipients') or [])}",
                                       recipients=list(p.get("recipients") or []), content=_quote_request_text(p))
    return await VENDOR_EMAIL_SENDER(dict(act.payload or {}), act)


class QuoteReplyIn(BaseModel):
    quote_id: str | None = None
    shipment_id: str | None = None
    vehicle_id: str | None = None
    from_addr: str | None = None
    message_id: str = Field(min_length=1)
    body: str | None = None
    extracted: dict = Field(default_factory=dict)   # {amount, currency, scope, inclusions, exclusions, timing, expires_at, vehicle_refs: []}
    expected_version: int | None = None


@command("quotes.record_reply", input=QuoteReplyIn, perm="shipping.write", action_class="internal",
         description="Match a vendor reply to the waiting quote and record the structured extraction (amount/currency/scope/inclusions/"
                     "exclusions/timing/expiry). A reply that covers several vehicles goes to clarifying with a task.")
async def quotes_record_reply(ctx: CommandContext, inp: QuoteReplyIn) -> dict:
    q: ShipmentQuote | None = None
    if inp.quote_id:
        q = await _quote(ctx, inp.quote_id, inp.expected_version)
    else:
        clauses = [ShipmentQuote.status.in_(("requested", "clarifying"))]
        if inp.shipment_id:
            clauses.append(ShipmentQuote.shipment_id == inp.shipment_id)
        if inp.vehicle_id:
            clauses.append(ShipmentQuote.vehicle_id == inp.vehicle_id)
        rows = (await ctx.db.execute(select(ShipmentQuote).where(*clauses))).scalars().all()
        if inp.from_addr:
            addr = inp.from_addr.lower()
            rows = [r for r in rows if any(addr == x.lower() or addr.split("@")[-1] == x.lower().split("@")[-1] for x in (r.recipients or []))] or rows
        if len(rows) == 1:
            q = rows[0]
        elif not rows:
            raise NotFound("no waiting quote matches this reply")
        else:
            raise Blocked("reply matches several waiting quotes; pick the quote explicitly", quote_ids=[r.id for r in rows])
    if q.reply_message_id == inp.message_id:
        return {"quote": serialize_quote(q), "changed": False, "idempotent": True}
    if q.status not in ("requested", "clarifying", "received"):
        raise Blocked(f"quote is {q.status}", status=q.status)
    ex = dict(inp.extracted or {})
    refs = [str(r) for r in (ex.get("vehicle_refs") or [])]
    ambiguous = len(refs) > 1 or bool(ex.get("ambiguous"))
    q.reply_message_id, q.received_at = inp.message_id, ctx.now
    q.reply_extracted = {**ex, "message_id": inp.message_id, "from": inp.from_addr, "body_excerpt": (inp.body or "")[:2000]}
    if ambiguous:
        q.status = "clarifying"
        task = await dispatch(ctx.child(), "tasks.create", {
            "title": f"Clarify vendor quote: which vehicle does it cover? ({q.vendor_name or inp.from_addr or 'vendor'})",
            "type": "operational", "priority": "high", "vehicle_id": q.vehicle_id, "shipment_id": q.shipment_id,
            "instructions": f"The reply {inp.message_id} mentions {len(refs)} vehicles ({', '.join(refs)}). Confirm which price applies to "
                            f"{(q.request_payload.get('vehicle') or {}).get('title') or 'this vehicle'} before accepting a vehicle-specific quote.",
            "source_kind": "shipping", "source_id": q.id, "dedupe": True}, commit=False)
        q.clarification_task_id = task.data["task"]["id"]
        q.next_check_at = ctx.now + timedelta(days=1)
        if q.case_id:
            await dispatch(ctx.child(), "cases.update", {"case_id": q.case_id, "status": "waiting", "waiting_on": "vendor clarification",
                                                        "next_action": "Get clarification on which vehicle the quote covers",
                                                        "next_check_at": q.next_check_at}, commit=False)
        ctx.touch(q, "shipment_quote")
        ctx.record("Vendor reply ambiguous (several vehicles); clarification requested", entity_kind="shipment_quote", entity_id=q.id,
                   kind="message", state="clarifying", sources=[inp.message_id], exception=True, details={"vehicle_refs": refs})
        ctx.emit("quote.changed", aggregate_type="shipment_quote", aggregate_id=q.id, aggregate_version=q.version,
                 payload={"quote_id": q.id, "change": "clarifying"})
        return {"quote": serialize_quote(q), "changed": True, "clarifying": True, "task_id": q.clarification_task_id}
    amount = ex.get("amount")
    cur = (ex.get("currency") or "").upper() or None
    needs: list[dict] = []
    if amount is None or not cur:
        needs.append({"field": "amount", "reason": "price/currency not stated in the reply"})
        q.amount, q.currency = None, None
    else:
        q.amount, q.currency = quantize(parse_amount(amount), cur), cur
    q.scope = str(ex.get("scope") or "")
    q.inclusions, q.exclusions = list(ex.get("inclusions") or []), list(ex.get("exclusions") or [])
    q.timing = ex.get("timing")
    q.expires_at = _dt(ex.get("expires_at"))
    if ex.get("service") in ("open", "enclosed"):
        q.service = ex["service"]
    if not q.expires_at:
        needs.append({"field": "expires_at", "reason": "validity/expiry not stated"})
    q.status = "received"
    q.needs_information = needs
    q.next_check_at = None
    ctx.touch(q, "shipment_quote")
    if q.case_id:
        await dispatch(ctx.child(), "cases.update", {"case_id": q.case_id, "status": "open", "waiting_on": None,
                                                    "next_action": "Compare and prepare the customer-forward message for review",
                                                    "next_check_at": ctx.now + timedelta(days=1),
                                                    "summary": f"Vendor replied: {_dec(q.amount)} {q.currency}" if q.amount is not None else "Vendor replied without a price"}, commit=False)
    ctx.record(f"Vendor quote received: {_dec(q.amount)} {q.currency}" if q.amount is not None else "Vendor replied (no price stated)",
               entity_kind="shipment_quote", entity_id=q.id, kind="message", state="received", sources=[inp.message_id], visibility="owner")
    ctx.emit("quote.changed", aggregate_type="shipment_quote", aggregate_id=q.id, aggregate_version=q.version,
             payload={"quote_id": q.id, "change": "received"})
    return {"quote": serialize_quote(q), "changed": True, "clarifying": False, "needs_information": needs}


class QuoteRefIn(BaseModel):
    quote_id: str
    expected_version: int | None = None
    note: str | None = None


def comparable(q: ShipmentQuote, other: ShipmentQuote) -> tuple[bool, list[str]]:
    reasons = []
    if not q.route_key or other.route_key != q.route_key:
        reasons.append("different route")
    if other.received_at is None or q.received_at is None or abs((ensure_aware(other.received_at) - ensure_aware(q.received_at)).days) > COMPARISON_WINDOW_DAYS:
        reasons.append("outside date window")
    if (other.operability or "unknown") != (q.operability or "unknown") or q.operability in (None, "unknown"):
        reasons.append("operability differs or unknown")
    if (other.size_class or "unknown") != (q.size_class or "unknown") or q.size_class in (None, "unknown"):
        reasons.append("size class differs or unknown")
    if (other.service or "unknown") != (q.service or "unknown") or q.service in (None, "unknown"):
        reasons.append("service differs or unknown")
    if other.currency != q.currency or other.amount is None:
        reasons.append("no comparable price")
    return not reasons, reasons


@command("quotes.compare", input=QuoteRefIn, perm="shipping.write", action_class="internal",
         description="Compare only genuinely comparable past quotes (route, date window, operability, size, service). Labels weak "
                     "evidence and recommends a second quote when fewer than two comparables exist. No invented price threshold.")
async def quotes_compare(ctx: CommandContext, inp: QuoteRefIn) -> dict:
    q = await _quote(ctx, inp.quote_id, inp.expected_version)
    if q.amount is None:
        raise Blocked("quote has no recorded price to compare")
    others = (await ctx.db.execute(select(ShipmentQuote).where(ShipmentQuote.id != q.id, ShipmentQuote.amount.is_not(None),
                                                              ShipmentQuote.status.in_(("received", "forwarded", "booked"))))).scalars().all()
    comps, rejected = [], []
    for o in others:
        ok, why = comparable(q, o)
        row = {"quote_id": o.id, "vendor_name": o.vendor_name, "amount": _dec(o.amount), "currency": o.currency,
               "received_at": _iso(o.received_at), "status": o.status, "service": o.service, "operability": o.operability}
        (comps if ok else rejected).append(row if ok else {**row, "excluded_because": why})
    amounts = [parse_amount(c["amount"]) for c in comps]
    unknown_attrs = [a for a in ("operability", "size_class", "service") if getattr(q, a) in (None, "unknown")]
    weak = len(comps) < 2 or bool(unknown_attrs)
    labels = []
    if len(comps) == 0:
        labels.append("no comparable quotes on record")
    elif len(comps) == 1:
        labels.append("only one comparable quote")
    if unknown_attrs:
        labels.append("this quote has unknown attributes: " + ", ".join(unknown_attrs))
    result = {
        "quote_amount": _dec(q.amount), "currency": q.currency, "comparables": comps, "excluded": rejected,
        "comparable_count": len(comps), "evidence": "weak" if weak else "adequate", "weakness": labels,
        "range": ({"min": _dec(min(amounts)), "max": _dec(max(amounts))} if amounts else None),
        "position": (None if not amounts else ("above" if q.amount > max(amounts) else ("below" if q.amount < min(amounts) else "within"))),
        "recommendation": ("Request a second quote before forwarding" if len(comps) < 2 else "Comparable evidence available; review the range"),
        "threshold": None, "compared_at": ctx.now.isoformat(),
    }
    q.comparison = result
    ctx.touch(q, "shipment_quote")
    ctx.record(f"Quote compared: {len(comps)} comparable(s), evidence {result['evidence']}", entity_kind="shipment_quote", entity_id=q.id,
               kind="fact", state=q.status, visibility="owner", details={"weakness": labels})
    return {"quote": serialize_quote(q), "comparison": result}


class QuoteForwardIn(BaseModel):
    quote_id: str
    to: list[str] = Field(min_length=1)          # customer recipients
    body: str = Field(min_length=1)
    include_comparison: bool = False


def _forward_summary(p: QuoteForwardIn) -> str:
    return f"Forward quote {p.quote_id[:8]} to {', '.join(p.to)}"


async def quote_forward_revalidate(ctx: CommandContext, inp: QuoteForwardIn, approval) -> list[str]:
    q = (await ctx.db.execute(select(ShipmentQuote).where(ShipmentQuote.id == inp.quote_id))).scalar_one_or_none()
    if q is None:
        return ["quote no longer exists"]
    reasons = []
    if q.status not in ("received", "forwarded"):
        reasons.append(f"quote is {q.status}")
    if q.amount is None:
        reasons.append("quote has no recorded price")
    bound = (approval.payload or {}).get("body")
    if bound is not None and bound != inp.body:
        reasons.append("message text changed — review again")
    if q.expires_at and ensure_aware(q.expires_at) < ctx.now:
        reasons.append("vendor quote expired")
    return reasons


@command("quotes.forward_to_customer", input=QuoteForwardIn, perm="shipping.write", action_class="consequential", approval_kind="send_message",
         records=lambda p: [("shipment_quote", p.quote_id)], summary=_forward_summary, revalidate=quote_forward_revalidate,
         limits=lambda p: {"recipients": list(p.to), "records": [p.quote_id]},
         consequence=lambda p: {"scope": "customer message with vendor quote", "moves_money": False, "targets": {"recipients": list(p.to)}},
         description="Forward the received quote to the customer (separate exact approval). Forwarding never books.")
async def quotes_forward(ctx: CommandContext, inp: QuoteForwardIn) -> dict:
    q = await _quote(ctx, inp.quote_id)
    if q.status not in ("received", "forwarded"):
        raise Blocked(f"quote is {q.status}; only a received quote can be forwarded", status=q.status)
    if q.amount is None:
        raise Blocked("quote has no recorded price to forward")
    payload = {"quote_id": q.id, "to": list(inp.to), "body": inp.body, "amount": _dec(q.amount), "currency": q.currency,
               "comparison": q.comparison if inp.include_comparison else None}
    act = await approvals_svc.intend_external_action(
        ctx, command_name="quotes.forward_to_customer", provider="email", entity_kind="shipment_quote", entity_id=q.id,
        dedupe_key=f"quote_forward:{q.id}:{stable_hash(payload)}", payload=payload)
    q.status, q.forwarded_at = "forwarded", ctx.now
    q.forward_action_id, q.forward_payload = act.id, payload
    q.forward_approval_id = ctx.approval.id if ctx.approval else q.forward_approval_id
    ctx.touch(q, "shipment_quote")
    if q.case_id:
        await dispatch(ctx.child(), "cases.update", {"case_id": q.case_id, "status": "waiting", "waiting_on": "customer decision",
                                                    "next_action": "Booking needs its own approval once the customer accepts",
                                                    "next_check_at": ctx.now + timedelta(days=QUOTE_FOLLOWUP_DAYS)}, commit=False)
    ctx.record(f"Quote forward queued to {', '.join(inp.to)} (not a booking)", entity_kind="shipment_quote", entity_id=q.id, kind="automation",
               state="forwarded", details={"external_action_id": act.id})
    ctx.emit("quote.changed", aggregate_type="shipment_quote", aggregate_id=q.id, aggregate_version=q.version,
             payload={"quote_id": q.id, "change": "forwarded", "booked": False})
    return {"quote": serialize_quote(q, act), "external_action_id": act.id, "booked": False}


@approvals_svc.executor("quotes.forward_to_customer")
async def _exec_quote_forward(db, act: ExternalAction) -> dict:
    if VENDOR_EMAIL_SENDER is None:
        p = dict(act.payload or {})
        return await _manual_send_task(db, act, title=f"Send quote to customer {', '.join(p.get('to') or [])}",
                                       recipients=list(p.get("to") or []), content=str(p.get("body") or ""))
    return await VENDOR_EMAIL_SENDER(dict(act.payload or {}), act)


class QuoteBookIn(BaseModel):
    quote_id: str
    carrier_name: str = Field(min_length=1)
    carrier_contact_id: str | None = None
    route_from: str = Field(min_length=1)
    route_to: str = Field(min_length=1)
    vehicle_id: str = Field(min_length=1)
    amount: Decimal
    currency: str = Field(min_length=3, max_length=3)
    conditions: str = Field(min_length=1)
    customer_acceptance_ref: str | None = None    # evidence the customer accepted (message id) when not yet forwarded
    leg_kind: str = "domestic"


def _book_summary(p: QuoteBookIn) -> str:
    return f"Book {p.carrier_name}: {p.route_from} → {p.route_to} for {p.amount} {p.currency}"


async def _booking_mismatch(q: ShipmentQuote, inp: QuoteBookIn) -> list[str]:
    reasons = []
    if q.status not in ("forwarded", "received"):
        reasons.append(f"quote is {q.status}")
    if q.status == "received" and not inp.customer_acceptance_ref:
        reasons.append("quote was not forwarded/approved by the customer (no acceptance evidence)")
    if q.amount is None or quantize(inp.amount, inp.currency.upper()) != q.amount or inp.currency.upper() != q.currency:
        reasons.append(f"amount {inp.amount} {inp.currency} does not match the quoted {_dec(q.amount)} {q.currency}")
    if q.vehicle_id and inp.vehicle_id != q.vehicle_id:
        reasons.append("vehicle does not match the quoted vehicle")
    if q.route_key and route_key(inp.route_from, inp.route_to) != q.route_key:
        reasons.append("route does not match the quoted route")
    if q.vendor_name and inp.carrier_name.strip().lower() != q.vendor_name.strip().lower():
        reasons.append("carrier does not match the quoting vendor")
    if q.expires_at and ensure_aware(q.expires_at) < datetime.now(timezone.utc):
        reasons.append("vendor quote expired")
    return reasons


async def quote_book_revalidate(ctx: CommandContext, inp: QuoteBookIn, approval) -> list[str]:
    q = (await ctx.db.execute(select(ShipmentQuote).where(ShipmentQuote.id == inp.quote_id))).scalar_one_or_none()
    if q is None:
        return ["quote no longer exists"]
    if q.status == "booked":
        return ["already booked"]
    return await _booking_mismatch(q, inp)


@command("quotes.book", input=QuoteBookIn, perm="shipping.write", action_class="consequential", approval_kind="booking",
         records=lambda p: [("shipment_quote", p.quote_id), ("vehicle", p.vehicle_id)], summary=_book_summary, revalidate=quote_book_revalidate,
         limits=lambda p: {"amount": str(p.amount), "currency": p.currency.upper(), "records": [p.quote_id]},
         consequence=lambda p: {"amount": str(p.amount), "currency": p.currency.upper(), "scope": "transport booking", "moves_money": True,
                                "targets": {"carrier": p.carrier_name, "route": f"{p.route_from} → {p.route_to}", "vehicle_id": p.vehicle_id}},
         description="Book transport: needs a forwarded/accepted quote and the exact carrier/route/vehicle/amount/conditions. Its own "
                     "exact approval — a forward approval never books.")
async def quotes_book(ctx: CommandContext, inp: QuoteBookIn) -> dict:
    q = await _quote(ctx, inp.quote_id)
    if q.status == "booked":
        return {"quote": serialize_quote(q), "booked": False, "idempotent": True, "leg_id": q.leg_id}
    reasons = await _booking_mismatch(q, inp)
    if reasons:
        raise Blocked("booking does not match the approved quote", reasons=reasons)
    if inp.leg_kind not in LEG_KINDS:
        raise ValidationFailed(f"leg_kind must be one of {LEG_KINDS}")
    cur = inp.currency.upper()
    amount = quantize(inp.amount, cur)
    booking = {"carrier_name": inp.carrier_name, "carrier_contact_id": inp.carrier_contact_id, "route_from": inp.route_from,
               "route_to": inp.route_to, "vehicle_id": inp.vehicle_id, "amount": _dec(amount), "currency": cur, "conditions": inp.conditions,
               "customer_acceptance_ref": inp.customer_acceptance_ref, "approved_at": ctx.now.isoformat(),
               "approval_id": ctx.approval.id if ctx.approval else None}
    act = await approvals_svc.intend_external_action(
        ctx, command_name="quotes.book", provider="email", entity_kind="shipment_quote", entity_id=q.id,
        dedupe_key=f"quote_booking:{q.id}:{stable_hash(booking)}", payload={"quote_id": q.id, "booking": booking, "vendor_name": q.vendor_name})
    leg: ShipmentLeg | None = await ctx.db.get(ShipmentLeg, q.leg_id) if q.leg_id else None
    if leg is None and q.shipment_id:
        leg = ShipmentLeg(shipment_id=q.shipment_id, kind=inp.leg_kind, vehicle_id=inp.vehicle_id, created_by=ctx.actor.user_id)
        ctx.db.add(leg)
        await ctx.db.flush()
        ctx.changed.append({"kind": "shipment_leg", "id": leg.id, "version": leg.version})
        q.leg_id = leg.id
    if leg is not None:
        leg.status, leg.booked_at = "booked", ctx.now
        leg.carrier_name, leg.carrier_contact_id = inp.carrier_name, inp.carrier_contact_id
        leg.route_from, leg.route_to = inp.route_from, inp.route_to
        leg.amount, leg.currency, leg.conditions = amount, cur, inp.conditions
        leg.quote_id, leg.approval_id = q.id, booking["approval_id"]
        if leg.id not in [c.get("id") for c in ctx.changed]:
            ctx.touch(leg, "shipment_leg")
    q.status, q.booked_at, q.booking = "booked", ctx.now, booking
    q.booking_action_id = act.id
    q.booking_approval_id = booking["approval_id"] or q.booking_approval_id
    ctx.touch(q, "shipment_quote")
    if q.case_id:
        await dispatch(ctx.child(), "cases.update", {"case_id": q.case_id, "status": "waiting", "waiting_on": "carrier booking confirmation",
                                                    "next_action": "Confirm booking reference and appointment with the carrier",
                                                    "next_check_at": ctx.now + timedelta(days=1)}, commit=False)
    ctx.record(f"Booking queued: {inp.carrier_name} {inp.route_from} → {inp.route_to}, {amount} {cur}", entity_kind="shipment_quote",
               entity_id=q.id, kind="automation", state="booked", visibility="owner", details={"external_action_id": act.id, "leg_id": q.leg_id})
    ctx.emit("quote.changed", aggregate_type="shipment_quote", aggregate_id=q.id, aggregate_version=q.version,
             payload={"quote_id": q.id, "change": "booked", "leg_id": q.leg_id})
    if q.shipment_id:
        s = await ctx.db.get(Shipment, q.shipment_id)
        if s is not None:
            ctx.emit("milestone.changed", aggregate_type="shipment", aggregate_id=s.id,
                     payload={"shipment_id": s.id, "kind": "carrier_booked", "status": "planned", "vehicle_ids": [inp.vehicle_id], "source_kind": "provider"})
    return {"quote": serialize_quote(q, act), "booked": True, "leg_id": q.leg_id, "external_action_id": act.id}


@approvals_svc.executor("quotes.book")
async def _exec_quote_book(db, act: ExternalAction) -> dict:
    if VENDOR_EMAIL_SENDER is None:
        p = dict(act.payload or {})
        b = p.get("booking") or {}
        content = "\n".join([f"Booking request — {b.get('carrier_name')}", f"Route: {b.get('route_from')} → {b.get('route_to')}",
                             f"Vehicle: {b.get('vehicle_id')}", f"Amount: {b.get('amount')} {b.get('currency')}",
                             f"Conditions: {b.get('conditions')}"])
        return await _manual_send_task(db, act, title=f"Send booking request to {b.get('carrier_name') or 'carrier'}",
                                       recipients=[x for x in [b.get("carrier_name")] if x], content=content)
    return await VENDOR_EMAIL_SENDER(dict(act.payload or {}), act)


class QuoteDeclineIn(BaseModel):
    quote_id: str
    reason: str = Field(min_length=1)
    expected_version: int | None = None


@command("quotes.decline", input=QuoteDeclineIn, perm="shipping.write", action_class="internal",
         description="Decline/close a quote with a reason (kept for comparisons only when it had a price).")
async def quotes_decline(ctx: CommandContext, inp: QuoteDeclineIn) -> dict:
    q = await _quote(ctx, inp.quote_id, inp.expected_version)
    if q.status == "booked":
        raise Blocked("quote is booked; cancel the booking with the carrier first")
    q.status = "declined"
    q.next_check_at = None
    q.extra = {**(q.extra or {}), "declined_reason": inp.reason}
    ctx.touch(q, "shipment_quote")
    if q.case_id:
        await dispatch(ctx.child(), "cases.update", {"case_id": q.case_id, "status": "resolved", "summary": f"Declined: {inp.reason}"}, commit=False)
    ctx.record(f"Quote declined: {inp.reason}", entity_kind="shipment_quote", entity_id=q.id, kind="task", state="declined")
    return {"quote": serialize_quote(q)}


# ── durable waiting: follow-up sweep (spec §12.4 case follow-through) ───────
@jobs.sweep("shipping.quote_followups", 300)
async def quote_followups(session_factory) -> dict:
    """Waiting quotes whose next check is due get a follow-up task (deduped) and a new next check.
    The case stays waiting; nothing is sent automatically."""
    from ..domain.actors import SYSTEM_ACTOR
    now = datetime.now(timezone.utc)
    checked = 0
    async with session_factory() as db:
        rows = (await db.execute(select(ShipmentQuote).where(ShipmentQuote.status.in_(("requested", "clarifying")),
                                                            ShipmentQuote.next_check_at.is_not(None),
                                                            ShipmentQuote.next_check_at <= now))).scalars().all()
        for q in rows:
            ctx = CommandContext(db=db, actor=SYSTEM_ACTOR, channel="worker", correlation_id=f"quote-followup:{q.id}")
            try:
                await dispatch(ctx, "tasks.create", {
                    "title": f"Follow up on shipping quote ({q.vendor_name or 'vendor'})", "type": "follow_up", "priority": "normal",
                    "vehicle_id": q.vehicle_id, "shipment_id": q.shipment_id, "source_kind": "shipping", "source_id": q.id,
                    "notes": f"No reply recorded since {_iso(q.requested_at)}; check {', '.join(q.recipients or [])}", "dedupe": True}, commit=False)
                q.check_count = (q.check_count or 0) + 1
                q.next_check_at = now + timedelta(days=QUOTE_FOLLOWUP_DAYS)
                q.bump(None)
                if q.case_id:
                    await dispatch(ctx, "cases.update", {"case_id": q.case_id, "status": "waiting", "next_check_at": q.next_check_at,
                                                         "waiting_on": "vendor reply", "next_action": "Follow up with the vendor"}, commit=False)
                checked += 1
            except DomainError as e:
                log.warning("quote follow-up %s skipped: %s", q.id, e)
        await db.commit()
    return {"checked": checked}
