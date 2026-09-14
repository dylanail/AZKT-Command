"""Sale / reservation records, agreements and documents (spec §5.2 Vehicle Sales handoff, §8.5; invariant 7).

A vehicle has at most one active reservation/sale. `sales.reserve` takes a row lock on the vehicle so two concurrent
reservations serialize: the second one gets an explicit `conflict` outcome plus an owner decision item
(Approval kind `reservation_conflict`) instead of a second active sale (C09). Completion date = sold cohort date (§2.4).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import select

from ..core.errors import Blocked, Conflict, Denied, NotFound, ValidationFailed
from ..core.ids import new_id
from ..core.money import parse_amount, quantize
from ..core.time import ensure_aware
from ..domain.actors import SYSTEM_ACTOR
from ..domain.commands import REGISTRY, CommandContext, command, dispatch
from ..models.contacts import Contact
from ..models.finance import Agreement, Document, Invoice, Sale
from ..models.vehicles import Vehicle

ACTIVE_SALE_STATUSES = ("reserved", "agreed", "paid", "completed")   # is_active True while the sale is live
DOCUMENT_STATUSES = ("pending", "on_file", "conflicted", "missing", "verified")
DOCUMENT_TYPES = ("export_certificate", "bill_of_lading", "title", "invoice", "id", "agreement", "release", "bill_of_sale",
                  "odometer_statement", "other")
AGREEMENT_KINDS = ("import", "reservation", "sale")
ZERO = Decimal("0")


def iso(dt) -> str | None:
    return ensure_aware(dt).isoformat() if dt else None


def money(amount, currency) -> dict | None:
    if amount is None or not currency:
        return None
    return {"amount": str(quantize(parse_amount(amount), currency)), "currency": currency.upper()}


def serialize_sale(s: Sale) -> dict:
    price = s.price
    net = (price - (s.credits or ZERO)) if price is not None else None
    return {"id": s.id, "version": s.version, "vehicle_id": s.vehicle_id, "buyer_contact_id": s.buyer_contact_id,
            "opportunity_id": s.opportunity_id, "agreement_id": s.agreement_id, "price": money(price, s.currency),
            "sales_tax": money(s.sales_tax, s.currency), "pass_through": money(s.pass_through, s.currency),
            "credits": money(s.credits or ZERO, s.currency), "net_sale_value": money(net, s.currency), "currency": s.currency,
            "status": s.status, "is_active": s.is_active, "reserved_at": iso(s.reserved_at),
            "reservation_expires_at": iso(s.reservation_expires_at), "agreed_at": iso(s.agreed_at), "completed_at": iso(s.completed_at),
            "delivered_at": iso(s.delivered_at), "cancelled_at": iso(s.cancelled_at), "cancel_reason": s.cancel_reason,
            "expired_at": iso(s.expired_at), "documents_checklist": s.documents_checklist or [], "aftercare": s.aftercare or [],
            "delivery_appointment_at": iso(s.delivery_appointment_at), "handoff_evidence": s.handoff_evidence or [],
            "restatements": s.restatements or [], "terms": s.terms or {}, "terms_flags": s.terms_flags or [],
            "credits_history": s.credits_history or [], "exceptions": s.exceptions or [], "completion_evidence": s.completion_evidence or {},
            "price_source": s.price_source, "source_ref": s.source_ref, "dedupe_key": s.dedupe_key, "notes": s.notes,
            "created_at": iso(s.created_at)}


def serialize_agreement(a: Agreement) -> dict:
    return {"id": a.id, "version": a.version, "kind": a.kind, "contact_id": a.contact_id, "import_request_id": a.import_request_id,
            "sale_id": a.sale_id, "vehicle_id": a.vehicle_id, "opportunity_id": a.opportunity_id, "agreement_version": a.agreement_version,
            "terms": a.terms or {}, "deposit": money(a.deposit_amount, a.deposit_currency), "price": money(a.price_amount, a.price_currency),
            "exceptions": a.exceptions or [], "status": a.status, "sent_at": iso(a.sent_at), "signed_at": iso(a.signed_at),
            "evidence_asset_id": a.evidence_asset_id, "sent_evidence": a.sent_evidence or {}, "signed_evidence": a.signed_evidence or {},
            "supersedes_id": a.supersedes_id, "created_at": iso(a.created_at)}


def serialize_document(d: Document) -> dict:
    return {"id": d.id, "version": d.version, "entity_kind": d.entity_kind, "entity_id": d.entity_id, "type": d.type, "status": d.status,
            "asset_id": d.asset_id, "visibility": d.visibility, "notes": d.notes, "conflict": d.conflict or {}, "sources": d.sources or [],
            "status_history": d.status_history or [], "created_at": iso(d.created_at)}


async def _sale(ctx: CommandContext, sale_id: str, expected_version: int | None = None) -> Sale:
    s = (await ctx.db.execute(select(Sale).where(Sale.id == sale_id).with_for_update())).scalar_one_or_none()
    if s is None:
        raise NotFound("sale not found")
    if expected_version is not None and s.version != expected_version:
        raise Conflict("sale changed since you loaded it", current_version=s.version)
    return s


async def _lock_vehicle(ctx: CommandContext, vehicle_id: str) -> Vehicle:
    v = (await ctx.db.execute(select(Vehicle).where(Vehicle.id == vehicle_id).with_for_update())).scalar_one_or_none()
    if v is None:
        raise NotFound("vehicle not found")
    return v


def _emit_sale(ctx: CommandContext, s: Sale, change: str, **extra) -> None:
    ctx.emit("sale.changed", aggregate_type="sale", aggregate_id=s.id, aggregate_version=s.version,
             payload={"sale_id": s.id, "vehicle_id": s.vehicle_id, "change": change, "status": s.status, **extra})
    ctx.emit("metrics.invalidated", aggregate_type="sale", aggregate_id=s.id, payload={"reason": f"sale.{change}", "vehicle_id": s.vehicle_id})


async def _set_commercial(ctx: CommandContext, v: Vehicle, state: str, reason: str) -> None:
    """Commercial state through the vehicles command when available (records its own state event); direct fallback."""
    if v.commercial_state == state:
        return
    if "vehicles.set_states" in REGISTRY:
        try:
            await dispatch(ctx.child(), "vehicles.set_states", {"vehicle_id": v.id, "commercial_state": state, "reason": reason}, commit=False)
            return
        except (Denied, Blocked, Conflict, ValidationFailed) as e:
            ctx.record(f"Vehicle state not set through vehicles.set_states: {e.message}", entity_kind="vehicle", entity_id=v.id,
                       kind="task", state="fallback", exception=True)
    old = v.commercial_state
    v.commercial_state = state
    v.state_history = list(v.state_history or []) + [{"dimension": "commercial", "from": old, "to": state, "reason": reason,
                                                       "at": ctx.now.isoformat(), "by": ctx.actor.user_id, "backward": False}]
    ctx.emit("vehicle.state_changed", aggregate_type="vehicle", aggregate_id=v.id, aggregate_version=v.version,
             payload={"vehicle_id": v.id, "dimension": "commercial", "from": old, "to": state, "reason": reason})
    v.bump(ctx.actor.user_id)


async def _terms_for(ctx: CommandContext, agreement_id: str | None, explicit: dict | None) -> tuple[dict, Agreement | None]:
    terms = dict(explicit or {})
    agreement = None
    if agreement_id:
        agreement = await ctx.db.get(Agreement, agreement_id)
        if agreement is None:
            raise NotFound("agreement not found")
        terms = {**(agreement.terms or {}), **terms}
    return terms, agreement


# ── reservation (invariant 7, C09) ───────────────────────────────────────────
class ReserveIn(BaseModel):
    vehicle_id: str
    buyer_contact_id: str
    opportunity_id: str | None = None
    agreement_id: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    terms: dict | None = None             # explicit reservation terms (reservation_days, ...)
    expires_at: datetime | None = None
    source_ref: str | None = None         # payment / invoice that reserved
    dedupe_key: str | None = None
    notes: str = ""


async def _reserve_locked(ctx: CommandContext, v: Vehicle, inp: ReserveIn) -> Sale:
    contact = await ctx.db.get(Contact, inp.buyer_contact_id)
    if contact is None:
        raise NotFound("buyer contact not found")
    terms, agreement = await _terms_for(ctx, inp.agreement_id, inp.terms)
    price, currency, price_source = inp.price, inp.currency, "explicit" if inp.price is not None else None
    if price is None and agreement is not None and agreement.price_amount is not None:
        price, currency, price_source = agreement.price_amount, agreement.price_currency, "agreement"
    currency = (currency or v.asking_currency or "USD").upper()
    flags: list[str] = []
    expires_at = inp.expires_at
    if expires_at is None:
        days = terms.get("reservation_days")
        if days:
            expires_at = ctx.now + timedelta(days=int(days))
        else:
            flags.append("terms not configured")   # reservation without expiry, visibly flagged
    s = Sale(vehicle_id=v.id, buyer_contact_id=inp.buyer_contact_id, opportunity_id=inp.opportunity_id, agreement_id=inp.agreement_id,
             price=quantize(price, currency) if price is not None else None, currency=currency, credits=ZERO, status="reserved",
             is_active=True, reserved_at=ctx.now, reservation_expires_at=expires_at, terms=terms, terms_flags=flags,
             dedupe_key=inp.dedupe_key, source_ref=inp.source_ref, price_source=price_source, notes=inp.notes,
             documents_checklist=[], restatements=[], credits_history=[], exceptions=[], created_by=ctx.actor.user_id,
             updated_by=ctx.actor.user_id)
    ctx.db.add(s)
    await ctx.db.flush()
    ctx.changed.append({"kind": "sale", "id": s.id, "version": s.version})
    v.allocation, v.active_sale_id, v.buyer_contact_id, v.reserved_at = "reserved", s.id, inp.buyer_contact_id, ctx.now
    await _set_commercial(ctx, v, "reserved", f"reserved for {contact.name}")
    ctx.record(f"Reserved {v.stock_no or v.id[:8]} for {contact.name}" + (f" — {', '.join(flags)}" if flags else ""),
               entity_kind="sale", entity_id=s.id, kind="task", state="reserved", exception=bool(flags),
               sources=[inp.source_ref] if inp.source_ref else None,
               details={"vehicle_id": v.id, "expires_at": iso(expires_at), "opportunity_id": inp.opportunity_id})
    _emit_sale(ctx, s, "reserved", buyer_contact_id=s.buyer_contact_id, opportunity_id=s.opportunity_id)
    return s


@command("sales.reserve", input=ReserveIn, perm="sales.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)],
         description="Reserve a vehicle for a buyer (atomic: vehicle row lock). A second buyer on the same vehicle gets an explicit "
                     "conflict outcome and an owner decision item instead of a second active sale (invariant 7).")
async def sales_reserve(ctx: CommandContext, inp: ReserveIn) -> dict:
    if inp.dedupe_key:
        ex = (await ctx.db.execute(select(Sale).where(Sale.dedupe_key == inp.dedupe_key))).scalars().first()
        if ex is not None:
            return {"sale": serialize_sale(ex), "outcome": "existing", "reserved": False}
    v = await _lock_vehicle(ctx, inp.vehicle_id)
    if v.archived_at:
        raise Blocked("vehicle is archived")
    active = (await ctx.db.execute(select(Sale).where(Sale.vehicle_id == v.id, Sale.is_active.is_(True)))).scalars().first()
    if active is not None:
        if active.buyer_contact_id == inp.buyer_contact_id:
            return {"sale": serialize_sale(active), "outcome": "existing", "reserved": False}
        res = await dispatch(ctx.child(), "sales.request_reservation_override",
                             {"vehicle_id": v.id, "buyer_contact_id": inp.buyer_contact_id, "opportunity_id": inp.opportunity_id,
                              "active_sale_id": active.id, "reason": "second reservation attempt while a reservation is active"},
                             commit=False)
        ctx.record(f"Reservation conflict on {v.stock_no or v.id[:8]}: active sale {active.id[:8]} kept; owner decision "
                   f"{res.approval_id or 'requested'}", entity_kind="vehicle", entity_id=v.id, kind="task", state="conflict", exception=True,
                   details={"active_sale_id": active.id, "requested_buyer": inp.buyer_contact_id, "approval_id": res.approval_id})
        ctx.emit("sale.changed", aggregate_type="sale", aggregate_id=active.id, aggregate_version=active.version,
                 payload={"sale_id": active.id, "vehicle_id": v.id, "change": "conflict", "status": active.status,
                          "requested_buyer": inp.buyer_contact_id, "approval_id": res.approval_id})
        return {"sale": None, "outcome": "conflict", "reserved": False, "active_sale_id": active.id,
                "active_buyer_contact_id": active.buyer_contact_id, "approval_id": res.approval_id, "decision": "Needs review",
                "override": res.to_dict()}
    s = await _reserve_locked(ctx, v, inp)
    return {"sale": serialize_sale(s), "outcome": "reserved", "reserved": True}


class OverrideIn(BaseModel):
    vehicle_id: str
    buyer_contact_id: str
    active_sale_id: str
    opportunity_id: str | None = None
    reason: str = Field(min_length=1)


async def _revalidate_override(ctx: CommandContext, inp: OverrideIn, approval) -> list[str]:
    v = await ctx.db.get(Vehicle, inp.vehicle_id)
    if v is None:
        return ["vehicle no longer exists"]
    if v.active_sale_id != inp.active_sale_id:
        return [f"active sale changed since the decision was prepared (now {v.active_sale_id})"]
    return []


@command("sales.request_reservation_override", input=OverrideIn, perm="sales.write", action_class="consequential",
         approval_kind="reservation_conflict", revalidate=_revalidate_override,
         summary=lambda p: f"Reservation conflict: decide buyer for vehicle {p.vehicle_id[:8]}",
         consequence=lambda p: {"targets": {"vehicle_id": p.vehicle_id, "active_sale_id": p.active_sale_id}, "scope": "reservation",
                                "moves_money": False},
         description="Owner decision item for a competing reservation. When approved: cancel the current reservation and reserve "
                     "for the requested buyer (nothing is refunded automatically).")
async def sales_request_override(ctx: CommandContext, inp: OverrideIn) -> dict:
    v = await _lock_vehicle(ctx, inp.vehicle_id)
    active = (await ctx.db.execute(select(Sale).where(Sale.id == inp.active_sale_id).with_for_update())).scalar_one_or_none()
    if active is None:
        raise NotFound("active sale not found")
    if active.is_active:
        active.status, active.is_active, active.cancelled_at = "cancelled", False, ctx.now
        active.cancel_reason = f"owner override: {inp.reason}"
        active.exceptions = list(active.exceptions or []) + [{"kind": "overridden", "at": ctx.now.isoformat(), "reason": inp.reason}]
        active.bump(ctx.actor.user_id)
        ctx.changed.append({"kind": "sale", "id": active.id, "version": active.version})
        ctx.record(f"Reservation overridden by owner: {active.id[:8]} cancelled — {inp.reason}", entity_kind="sale", entity_id=active.id,
                   kind="task", state="cancelled", exception=True)
        _emit_sale(ctx, active, "overridden")
    s = await _reserve_locked(ctx, v, ReserveIn(vehicle_id=v.id, buyer_contact_id=inp.buyer_contact_id, opportunity_id=inp.opportunity_id,
                                                notes=f"reserved by owner decision: {inp.reason}"))
    return {"sale": serialize_sale(s), "cancelled_sale_id": active.id, "outcome": "reserved"}


# ── agree / complete / deliver / cancel / expire ──────────────────────────────
class AgreeIn(BaseModel):
    sale_id: str
    price: Decimal | None = None
    currency: str | None = None
    terms: dict | None = None
    agreement_id: str | None = None
    sales_tax: Decimal | None = None
    pass_through: Decimal | None = None
    expected_version: int | None = None


@command("sales.agree", input=AgreeIn, perm="sales.write", action_class="owner_only", approval_kind="terms",
         summary=lambda p: f"Agree sale price/terms {p.sale_id[:8]}",
         consequence=lambda p: {"amount": str(p.price) if p.price is not None else None, "currency": p.currency, "moves_money": False},
         description="Owner records the approved price/terms for a sale (from the agreement or explicit).")
async def sales_agree(ctx: CommandContext, inp: AgreeIn) -> dict:
    s = await _sale(ctx, inp.sale_id, inp.expected_version)
    if not s.is_active:
        raise Blocked(f"sale is {s.status}")
    terms, agreement = await _terms_for(ctx, inp.agreement_id or s.agreement_id, inp.terms)
    price, currency, source = inp.price, inp.currency, "explicit" if inp.price is not None else None
    if price is None and agreement is not None and agreement.price_amount is not None:
        price, currency, source = agreement.price_amount, agreement.price_currency, "agreement"
    if price is None:
        raise ValidationFailed("a price is required (explicit or from the agreement)")
    cur = (currency or s.currency).upper()
    s.price, s.currency, s.price_source = quantize(price, cur), cur, source
    if inp.sales_tax is not None:
        s.sales_tax = quantize(inp.sales_tax, cur)
    if inp.pass_through is not None:
        s.pass_through = quantize(inp.pass_through, cur)
    s.terms = {**(s.terms or {}), **terms}
    s.terms_flags = [f for f in (s.terms_flags or []) if f != "terms not configured"] if s.terms else s.terms_flags
    if inp.agreement_id:
        s.agreement_id = inp.agreement_id
    if s.status == "reserved":
        s.status = "agreed"
    s.agreed_at = ctx.now
    ctx.touch(s, "sale")
    ctx.record(f"Sale terms agreed: {s.price} {cur}", entity_kind="sale", entity_id=s.id, kind="payment", state=s.status, visibility="finance")
    _emit_sale(ctx, s, "agreed")
    return {"sale": serialize_sale(s)}


class CompleteIn(BaseModel):
    sale_id: str
    completed_at: datetime | None = None
    source_ref: str | None = None
    note: str | None = None
    expected_version: int | None = None


@command("sales.mark_completed", input=CompleteIn, perm="sales.write", action_class="owner_only", approval_kind="sale",
         summary=lambda p: f"Mark sale completed {p.sale_id[:8]}",
         description="Owner marks the sale completed: the completion date is the sold-cohort date (§2.4). Requires price and buyer; "
                     "vehicle becomes sold (shipping/recon/document work continues independently).")
async def sales_mark_completed(ctx: CommandContext, inp: CompleteIn) -> dict:
    s = await _sale(ctx, inp.sale_id, inp.expected_version)
    if s.status in ("cancelled", "expired"):
        raise Blocked(f"sale is {s.status}")
    if s.status in ("completed", "delivered"):
        return {"sale": serialize_sale(s), "completed": False, "idempotent": True}
    if s.price is None:
        raise Blocked("sale has no agreed price; record it with sales.agree first")
    if not s.buyer_contact_id:
        raise Blocked("sale has no buyer")
    at = ensure_aware(inp.completed_at) or ctx.now
    if at > ctx.now:
        raise ValidationFailed("completion date cannot be in the future")
    s.status, s.completed_at, s.is_active = "completed", at, True
    s.completion_evidence = {"source_ref": inp.source_ref, "note": inp.note, "by": ctx.actor.user_id, "at": ctx.now.isoformat()}
    v = await _lock_vehicle(ctx, s.vehicle_id)
    v.allocation, v.sold_at, v.active_sale_id, v.buyer_contact_id = "sold", at, s.id, s.buyer_contact_id
    await _set_commercial(ctx, v, "sold", f"sale {s.id[:8]} completed")
    if "vehicles.record_milestone" in REGISTRY:
        try:
            await dispatch(ctx.child(), "vehicles.record_milestone", {"vehicle_id": v.id, "kind": "sold", "status": "completed",
                                                                     "at": at.isoformat(), "source_kind": "sale", "source_ref": s.id}, commit=False)
        except (Denied, Blocked, Conflict, ValidationFailed) as e:
            ctx.record(f"Sold milestone not recorded through vehicles.record_milestone: {e.message}", entity_kind="vehicle", entity_id=v.id,
                       kind="task", state="fallback", exception=True)
    ctx.touch(s, "sale")
    ctx.record(f"Sale completed: {v.stock_no or v.id[:8]} {s.price} {s.currency} on {at.date().isoformat()}", entity_kind="sale",
               entity_id=s.id, kind="payment", state="completed", visibility="finance", sources=[inp.source_ref] if inp.source_ref else None,
               details={"cohort_date": at.isoformat()})
    _emit_sale(ctx, s, "completed", cohort_date=at.isoformat())
    return {"sale": serialize_sale(s), "completed": True}


class DeliveryIn(BaseModel):
    sale_id: str
    delivered_at: datetime | None = None
    asset_ids: list[str] = Field(default_factory=list)
    note: str | None = None
    appointment_at: datetime | None = None
    expected_version: int | None = None


@command("sales.record_delivery", input=DeliveryIn, perm="sales.write", action_class="internal",
         description="Record the delivery appointment and/or handoff evidence; delivery needs at least a note or a photo.")
async def sales_record_delivery(ctx: CommandContext, inp: DeliveryIn) -> dict:
    s = await _sale(ctx, inp.sale_id, inp.expected_version)
    if s.status in ("cancelled", "expired"):
        raise Blocked(f"sale is {s.status}")
    if inp.appointment_at is not None:
        s.delivery_appointment_at = inp.appointment_at
    delivered = inp.delivered_at is not None or bool(inp.asset_ids) or bool(inp.note)
    if delivered:
        if not inp.asset_ids and not inp.note:
            raise Blocked("delivery evidence required (photo or note)")
        if s.status != "completed":
            raise Blocked("delivery is recorded after the sale is completed", status=s.status)
        at = ensure_aware(inp.delivered_at) or ctx.now
        s.handoff_evidence = list(s.handoff_evidence or []) + [{"asset_ids": inp.asset_ids, "note": inp.note, "by": ctx.actor.user_id,
                                                                "at": at.isoformat()}]
        s.status, s.delivered_at = "delivered", at
        v = await _lock_vehicle(ctx, s.vehicle_id)
        v.delivered_at = at
        await _set_commercial(ctx, v, "delivered", f"delivered ({'photo' if inp.asset_ids else 'note'})")
    ctx.touch(s, "sale")
    ctx.record(("Delivered: " if delivered else "Delivery appointment: ") + (s.vehicle_id[:8]), entity_kind="sale", entity_id=s.id,
               kind="task", state=s.status, details={"asset_ids": inp.asset_ids, "appointment_at": iso(inp.appointment_at)})
    _emit_sale(ctx, s, "delivered" if delivered else "appointment")
    return {"sale": serialize_sale(s)}


async def _release_vehicle(ctx: CommandContext, s: Sale, reason: str) -> None:
    v = await _lock_vehicle(ctx, s.vehicle_id)
    if v.active_sale_id == s.id:
        v.active_sale_id, v.buyer_contact_id = None, None
        if v.allocation in ("reserved", "sold"):
            v.allocation = "inventory"
        await _set_commercial(ctx, v, "listed" if v.listed_at else "not_listed", reason)
        v.bump(ctx.actor.user_id)


class CancelIn(BaseModel):
    sale_id: str
    reason: str = Field(min_length=1)
    expected_version: int | None = None


@command("sales.cancel", input=CancelIn, perm="sales.write", action_class="internal",
         description="Cancel a reservation/sale with a reason. Allocated payments become a visible reconciliation exception; "
                     "nothing is refunded automatically (§8.5).")
async def sales_cancel(ctx: CommandContext, inp: CancelIn) -> dict:
    s = await _sale(ctx, inp.sale_id, inp.expected_version)
    if s.status in ("cancelled", "expired"):
        return {"sale": serialize_sale(s), "cancelled": False, "idempotent": True}
    s.status, s.is_active, s.cancelled_at, s.cancel_reason = "cancelled", False, ctx.now, inp.reason
    invoices = (await ctx.db.execute(select(Invoice).where((Invoice.sale_id == s.id) |
                                                           ((Invoice.opportunity_id == s.opportunity_id) if s.opportunity_id else False)))).scalars().all()
    allocated = [i for i in invoices if (i.amount_allocated or ZERO) > 0]
    exception = None
    if allocated:
        exception = {"kind": "reconciliation_required", "at": ctx.now.isoformat(), "reason": inp.reason,
                     "invoices": [{"invoice_id": i.id, "kind": i.kind, "allocated": str(i.amount_allocated), "currency": i.currency} for i in allocated],
                     "note": "payments stay allocated until the owner reverses/refunds explicitly"}
        s.exceptions = list(s.exceptions or []) + [exception]
        for i in allocated:
            i.exceptions = list(i.exceptions or []) + [{"kind": "sale_cancelled", "at": ctx.now.isoformat(), "sale_id": s.id}]
            i.bump(ctx.actor.user_id)
    await _release_vehicle(ctx, s, f"sale cancelled: {inp.reason}")
    ctx.touch(s, "sale")
    ctx.record(f"Sale cancelled: {inp.reason}" + (" — reconciliation required" if exception else ""), entity_kind="sale", entity_id=s.id,
               kind="task", state="cancelled", exception=bool(exception), details=exception or {})
    _emit_sale(ctx, s, "cancelled", reconciliation=bool(exception))
    return {"sale": serialize_sale(s), "cancelled": True, "reconciliation_exception": exception}


class SaleRefIn(BaseModel):
    sale_id: str
    expected_version: int | None = None


@command("sales.expire_reservation", input=SaleRefIn, perm="sales.write", action_class="internal",
         description="Reservation expiry (worker sweep candidate via expire_due): frees the vehicle, keeps the record.")
async def sales_expire_reservation(ctx: CommandContext, inp: SaleRefIn) -> dict:
    s = await _sale(ctx, inp.sale_id, inp.expected_version)
    if s.status != "reserved" or not s.is_active:
        return {"sale": serialize_sale(s), "expired": False}
    if s.reservation_expires_at is None:
        raise Blocked("reservation has no expiry (terms not configured)")
    s.status, s.is_active, s.expired_at = "expired", False, ctx.now
    await _release_vehicle(ctx, s, "reservation expired")
    ctx.touch(s, "sale")
    ctx.record("Reservation expired", entity_kind="sale", entity_id=s.id, kind="task", state="expired")
    _emit_sale(ctx, s, "expired")
    return {"sale": serialize_sale(s), "expired": True}


async def expire_due(session_factory) -> int:
    """Stage-2 sweep body: expire reservations past their expiry (registered by the worker in stage 2)."""
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        ids = (await db.execute(select(Sale.id).where(Sale.status == "reserved", Sale.is_active.is_(True),
                                                      Sale.reservation_expires_at.is_not(None), Sale.reservation_expires_at <= now))).scalars().all()
    n = 0
    for sid in ids:
        async with session_factory() as db:
            ctx = CommandContext(db=db, actor=SYSTEM_ACTOR, channel="worker", correlation_id=f"expire:{sid}")
            try:
                res = await dispatch(ctx, "sales.expire_reservation", {"sale_id": sid})
                n += 1 if res.data.get("expired") else 0
            except (Blocked, Conflict, NotFound, Denied):
                await db.rollback()
    return n


class CreditIn(BaseModel):
    sale_id: str
    amount: Decimal
    reason: str = Field(min_length=1)
    evidence_ref: str | None = None
    expected_version: int | None = None


@command("sales.record_credit", input=CreditIn, perm="finance.write", action_class="owner_only", approval_kind="price_change",
         summary=lambda p: f"Sale credit {p.amount}",
         description="Price credit / return on a sale: reduces net sale value; a completed sale gets a restatement note on its "
                     "original cohort (K03). Cash refunds are separate payment records.")
async def sales_record_credit(ctx: CommandContext, inp: CreditIn) -> dict:
    s = await _sale(ctx, inp.sale_id, inp.expected_version)
    amt = quantize(inp.amount, s.currency)
    if amt <= 0:
        raise ValidationFailed("credit amount must be positive")
    if s.price is not None and (s.credits or ZERO) + amt > s.price:
        raise ValidationFailed("credits exceed the sale price")
    old = s.credits or ZERO
    s.credits = old + amt
    entry = {"credit_id": new_id(), "amount": str(amt), "reason": inp.reason, "at": ctx.now.isoformat(), "by": ctx.actor.user_id,
             "evidence_ref": inp.evidence_ref}
    s.credits_history = list(s.credits_history or []) + [entry]
    restated = False
    if s.completed_at is not None:
        s.restatements = list(s.restatements or []) + [{"at": ctx.now.isoformat(), "by": ctx.actor.user_id, "field": "credits", "old": str(old),
                                                        "new": str(s.credits), "note": f"sale credit: {inp.reason}", "kind": "sale_credit",
                                                        "cohort_date": iso(s.completed_at)}]
        restated = True
    ctx.touch(s, "sale")
    ctx.record(f"Sale credit {amt} {s.currency}: {inp.reason}" + (" (cohort restated)" if restated else ""), entity_kind="sale",
               entity_id=s.id, kind="payment", state=s.status, visibility="finance", sources=[inp.evidence_ref] if inp.evidence_ref else None,
               exception=restated, details=entry)
    _emit_sale(ctx, s, "credited", restated=restated, cohort_date=iso(s.completed_at))
    return {"sale": serialize_sale(s), "credit": entry, "restated": restated}


# ── documents ────────────────────────────────────────────────────────────────
def _checklist_entry(s: Sale, d: Document) -> None:
    cl = [c for c in (s.documents_checklist or []) if c.get("type") != d.type]
    cl.append({"type": d.type, "status": d.status, "document_id": d.id, "required": next((c.get("required", True) for c in (s.documents_checklist or [])
                                                                                          if c.get("type") == d.type), True)})
    s.documents_checklist = cl


class SaleDocumentIn(BaseModel):
    sale_id: str
    type: str
    status: str = "pending"
    asset_id: str | None = None
    source: dict | None = None            # {kind, ref, value}
    notes: str = ""
    expected_version: int | None = None


@command("sales.add_document", input=SaleDocumentIn, perm="documents.write", action_class="internal",
         description="Add/update a document on the sale checklist (one per type).")
async def sales_add_document(ctx: CommandContext, inp: SaleDocumentIn) -> dict:
    if inp.type not in DOCUMENT_TYPES:
        raise ValidationFailed(f"type must be one of {DOCUMENT_TYPES}")
    if inp.status not in DOCUMENT_STATUSES:
        raise ValidationFailed(f"status must be one of {DOCUMENT_STATUSES}")
    s = await _sale(ctx, inp.sale_id, inp.expected_version)
    d = (await ctx.db.execute(select(Document).where(Document.entity_kind == "sale", Document.entity_id == s.id,
                                                     Document.type == inp.type).with_for_update())).scalars().first()
    created = d is None
    if created:
        d = Document(entity_kind="sale", entity_id=s.id, type=inp.type, status="pending", sources=[], status_history=[],
                     created_by=ctx.actor.user_id)
        ctx.db.add(d)
        await ctx.db.flush()
    if inp.source:
        d.sources = list(d.sources or []) + [{**inp.source, "at": ctx.now.isoformat(), "by": ctx.actor.user_id}]
    _apply_document_status(ctx, d, inp.status, inp.asset_id, inp.notes)
    if not created:
        d.bump(ctx.actor.user_id)
    ctx.changed.append({"kind": "document", "id": d.id, "version": d.version})
    _checklist_entry(s, d)
    ctx.touch(s, "sale")
    ctx.record(f"Document {d.type}: {d.status}", entity_kind="sale", entity_id=s.id, kind="task", state=d.status,
               details={"document_id": d.id, "asset_id": d.asset_id})
    _emit_sale(ctx, s, "document", document_type=d.type, document_status=d.status)
    return {"document": serialize_document(d), "sale": serialize_sale(s), "created": created}


def _apply_document_status(ctx: CommandContext, d: Document, status: str, asset_id: str | None, notes: str | None) -> None:
    if asset_id:
        d.asset_id = asset_id
    if status == "on_file" and not (d.asset_id or d.sources):
        raise Blocked("on_file needs a filed asset or a recorded source")
    if status == "conflicted" and len(d.sources or []) < 2:
        raise Blocked("a conflict needs both conflicting sources recorded", sources=len(d.sources or []))
    if status == "conflicted":
        d.conflict = {"sources": d.sources, "at": ctx.now.isoformat(), "by": ctx.actor.user_id}
    elif d.conflict and status in ("on_file", "verified"):
        d.conflict = {**d.conflict, "resolved_at": ctx.now.isoformat(), "resolved_by": ctx.actor.user_id, "resolved_as": status}
    d.status_history = list(d.status_history or []) + [{"from": d.status, "to": status, "at": ctx.now.isoformat(), "by": ctx.actor.user_id}]
    d.status = status
    if notes:
        d.notes = notes
    d.updated_by = ctx.actor.user_id


class ChecklistIn(BaseModel):
    sale_id: str
    checklist: list[dict]                 # [{type, required}]
    expected_version: int | None = None


@command("sales.update_checklist", input=ChecklistIn, perm="documents.write", action_class="internal",
         description="Set which documents the sale requires (from versioned business configuration, never model-invented).")
async def sales_update_checklist(ctx: CommandContext, inp: ChecklistIn) -> dict:
    s = await _sale(ctx, inp.sale_id, inp.expected_version)
    existing = {c.get("type"): c for c in (s.documents_checklist or [])}
    out = []
    for row in inp.checklist:
        t = row.get("type")
        if t not in DOCUMENT_TYPES:
            raise ValidationFailed(f"type must be one of {DOCUMENT_TYPES}")
        cur = existing.get(t, {"type": t, "status": "pending", "document_id": None})
        out.append({**cur, "required": bool(row.get("required", True))})
    s.documents_checklist = out
    ctx.touch(s, "sale")
    ctx.record("Document checklist updated", entity_kind="sale", entity_id=s.id, kind="task", state=s.status)
    return {"sale": serialize_sale(s)}


class DocumentStatusIn(BaseModel):
    document_id: str
    status: str
    asset_id: str | None = None
    source: dict | None = None
    notes: str | None = None
    expected_version: int | None = None


@command("documents.set_status", input=DocumentStatusIn, perm="documents.write", action_class="internal",
         description="Set a document's status (pending|on_file|conflicted|missing|verified). Conflicted requires both sources.")
async def documents_set_status(ctx: CommandContext, inp: DocumentStatusIn) -> dict:
    if inp.status not in DOCUMENT_STATUSES:
        raise ValidationFailed(f"status must be one of {DOCUMENT_STATUSES}")
    d = (await ctx.db.execute(select(Document).where(Document.id == inp.document_id).with_for_update())).scalar_one_or_none()
    if d is None:
        raise NotFound("document not found")
    if inp.expected_version is not None and d.version != inp.expected_version:
        raise Conflict("document changed since you loaded it", current_version=d.version)
    if inp.source:
        d.sources = list(d.sources or []) + [{**inp.source, "at": ctx.now.isoformat(), "by": ctx.actor.user_id}]
    _apply_document_status(ctx, d, inp.status, inp.asset_id, inp.notes)
    ctx.touch(d, "document")
    sale = None
    if d.entity_kind == "sale":
        s = await _sale(ctx, d.entity_id)
        _checklist_entry(s, d)
        s.bump(ctx.actor.user_id)
        sale = serialize_sale(s)
    ctx.record(f"Document {d.type} → {d.status}", entity_kind=d.entity_kind, entity_id=d.entity_id, kind="fact", state=d.status,
               exception=d.status in ("conflicted", "missing"), details={"document_id": d.id})
    ctx.emit("evidence.saved", aggregate_type="document", aggregate_id=d.id, aggregate_version=d.version,
             payload={"document_id": d.id, "entity_kind": d.entity_kind, "entity_id": d.entity_id, "status": d.status})
    return {"document": serialize_document(d), "sale": sale}


# ── agreements ───────────────────────────────────────────────────────────────
class AgreementCreateIn(BaseModel):
    kind: str = "sale"
    contact_id: str
    import_request_id: str | None = None
    sale_id: str | None = None
    vehicle_id: str | None = None
    opportunity_id: str | None = None
    terms: dict = Field(default_factory=dict)
    deposit_amount: Decimal | None = None
    deposit_currency: str | None = None
    price_amount: Decimal | None = None
    price_currency: str | None = None
    exceptions: list = Field(default_factory=list)


@command("agreements.create", input=AgreementCreateIn, perm="sales.write", action_class="internal",
         description="Draft an agreement (import/reservation/sale) with its deposit rule, price and customer-specific exceptions.")
async def agreements_create(ctx: CommandContext, inp: AgreementCreateIn) -> dict:
    if inp.kind not in AGREEMENT_KINDS:
        raise ValidationFailed(f"kind must be one of {AGREEMENT_KINDS}")
    if await ctx.db.get(Contact, inp.contact_id) is None:
        raise NotFound("contact not found")
    if (inp.deposit_amount is None) != (inp.deposit_currency is None):
        raise ValidationFailed("deposit needs both amount and currency")
    if (inp.price_amount is None) != (inp.price_currency is None):
        raise ValidationFailed("price needs both amount and currency")
    prior = (await ctx.db.execute(select(Agreement).where(Agreement.contact_id == inp.contact_id, Agreement.kind == inp.kind,
                                                          Agreement.status.in_(("draft", "sent", "signed")),
                                                          (Agreement.sale_id == inp.sale_id) if inp.sale_id else
                                                          (Agreement.import_request_id == inp.import_request_id) if inp.import_request_id else True)
                                  .order_by(Agreement.agreement_version.desc()))).scalars().first()
    a = Agreement(kind=inp.kind, contact_id=inp.contact_id, import_request_id=inp.import_request_id, sale_id=inp.sale_id,
                  vehicle_id=inp.vehicle_id, opportunity_id=inp.opportunity_id, terms=inp.terms,
                  deposit_amount=quantize(inp.deposit_amount, inp.deposit_currency) if inp.deposit_amount is not None else None,
                  deposit_currency=inp.deposit_currency.upper() if inp.deposit_currency else None,
                  price_amount=quantize(inp.price_amount, inp.price_currency) if inp.price_amount is not None else None,
                  price_currency=inp.price_currency.upper() if inp.price_currency else None, exceptions=inp.exceptions, status="draft",
                  agreement_version=(prior.agreement_version + 1) if prior is not None else 1, supersedes_id=prior.id if prior else None,
                  created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
    ctx.db.add(a)
    await ctx.db.flush()
    ctx.changed.append({"kind": "agreement", "id": a.id, "version": a.version})
    ctx.record(f"Agreement drafted ({a.kind} v{a.agreement_version})", entity_kind="agreement", entity_id=a.id, kind="task", state="draft")
    ctx.emit("agreement.changed", aggregate_type="agreement", aggregate_id=a.id, payload={"agreement_id": a.id, "status": "draft"})
    return {"agreement": serialize_agreement(a), "created": True}


async def _agreement(ctx: CommandContext, agreement_id: str, expected_version: int | None) -> Agreement:
    a = (await ctx.db.execute(select(Agreement).where(Agreement.id == agreement_id).with_for_update())).scalar_one_or_none()
    if a is None:
        raise NotFound("agreement not found")
    if expected_version is not None and a.version != expected_version:
        raise Conflict("agreement changed since you loaded it", current_version=a.version)
    return a


class AgreementSentIn(BaseModel):
    agreement_id: str
    source_ref: str = Field(min_length=1)    # sent message / provider ref (evidence)
    sent_at: datetime | None = None
    expected_version: int | None = None


@command("agreements.mark_sent", input=AgreementSentIn, perm="sales.write", action_class="internal",
         description="Record that the agreement was sent (evidence reference required). Sending itself is a separate approved action.")
async def agreements_mark_sent(ctx: CommandContext, inp: AgreementSentIn) -> dict:
    a = await _agreement(ctx, inp.agreement_id, inp.expected_version)
    if a.status not in ("draft", "sent"):
        raise Blocked(f"agreement is {a.status}")
    a.status, a.sent_at = "sent", ensure_aware(inp.sent_at) or ctx.now
    a.sent_evidence = {"source_ref": inp.source_ref, "at": a.sent_at.isoformat(), "by": ctx.actor.user_id}
    ctx.touch(a, "agreement")
    ctx.record("Agreement sent", entity_kind="agreement", entity_id=a.id, kind="message", state="sent", sources=[inp.source_ref])
    ctx.emit("agreement.changed", aggregate_type="agreement", aggregate_id=a.id, payload={"agreement_id": a.id, "status": "sent"})
    return {"agreement": serialize_agreement(a)}


class AgreementSignedIn(BaseModel):
    agreement_id: str
    evidence_asset_id: str = Field(min_length=1)
    signed_at: datetime | None = None
    expected_version: int | None = None


@command("agreements.mark_signed", input=AgreementSignedIn, perm="sales.write", action_class="internal",
         description="Record the signed agreement (a filed evidence asset is required). Supersedes earlier signed versions.")
async def agreements_mark_signed(ctx: CommandContext, inp: AgreementSignedIn) -> dict:
    a = await _agreement(ctx, inp.agreement_id, inp.expected_version)
    if a.status in ("superseded", "cancelled"):
        raise Blocked(f"agreement is {a.status}")
    from ..models.assets import Asset
    asset = await ctx.db.get(Asset, inp.evidence_asset_id)
    if asset is None or asset.status != "ready":
        raise Blocked("signed agreement needs a filed evidence asset", asset_id=inp.evidence_asset_id)
    a.status, a.signed_at, a.evidence_asset_id = "signed", ensure_aware(inp.signed_at) or ctx.now, inp.evidence_asset_id
    a.signed_evidence = {"asset_id": inp.evidence_asset_id, "signed_at": a.signed_at.isoformat(), "by": ctx.actor.user_id}
    q = select(Agreement).where(Agreement.id != a.id, Agreement.status == "signed", Agreement.contact_id == a.contact_id, Agreement.kind == a.kind)
    if a.sale_id:
        q = q.where(Agreement.sale_id == a.sale_id)
    elif a.import_request_id:
        q = q.where(Agreement.import_request_id == a.import_request_id)
    for prev in (await ctx.db.execute(q)).scalars().all():
        prev.status = "superseded"
        prev.bump(ctx.actor.user_id)
    if a.sale_id:
        s = await _sale(ctx, a.sale_id)
        s.agreement_id = a.id
        if a.price_amount is not None and s.price is None:
            s.price, s.currency, s.price_source = a.price_amount, a.price_currency, "agreement"
        s.terms = {**(s.terms or {}), **(a.terms or {})}
        s.bump(ctx.actor.user_id)
    ctx.touch(a, "agreement")
    ctx.record("Agreement signed (evidence filed)", entity_kind="agreement", entity_id=a.id, kind="fact", state="signed",
               sources=[f"asset:{inp.evidence_asset_id}"])
    ctx.emit("agreement.changed", aggregate_type="agreement", aggregate_id=a.id, payload={"agreement_id": a.id, "status": "signed"})
    return {"agreement": serialize_agreement(a)}
