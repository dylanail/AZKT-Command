from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, BusinessRow

SHIPMENT_STATUSES = ("planned", "in_transit", "at_port", "released", "domestic", "received", "complete", "exception")
LEG_KINDS = ("export", "ocean", "port", "domestic")
LEG_STATUSES = ("planned", "quoted", "booked", "in_progress", "complete", "cancelled")
MILESTONE_KINDS = ("vessel_departed", "vessel_arrival", "discharge", "release", "carrier_booked", "pickup", "received")
MILESTONE_STATUSES = ("planned", "estimated", "completed")
MILESTONE_SOURCE_KINDS = ("manual", "document", "message", "carrier", "port", "provider", "exporter", "customs")
QUOTE_STATUSES = ("draft", "needs_information", "pending_approval", "requested", "received", "clarifying",
                  "forwarded", "booked", "declined", "expired")


class Shipment(Base, BusinessRow):
    """Container/vessel-level shipment; vehicles are members (spec §8.3)."""
    __tablename__ = "shipments"
    __mapper_args__ = {"eager_defaults": True}  # fetch server-generated updated_at via RETURNING (async-safe)
    ref: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)  # SHP-0028
    status: Mapped[str] = mapped_column(String, default="planned", index=True)  # planned|in_transit|at_port|released|domestic|received|complete|exception
    vehicle_ids: Mapped[list] = mapped_column(JSON, default=list)
    container_no: Mapped[str | None] = mapped_column(String, nullable=True)
    vessel: Mapped[str | None] = mapped_column(String, nullable=True)
    voyage: Mapped[str | None] = mapped_column(String, nullable=True)
    route_from: Mapped[str | None] = mapped_column(String, nullable=True)
    route_to: Mapped[str | None] = mapped_column(String, nullable=True)
    exporter_contact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    eta_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    eta_source: Mapped[str | None] = mapped_column(String, nullable=True)
    storage_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    storage_deadline_source: Mapped[str | None] = mapped_column(String, nullable=True)
    case_id: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    # added by the shipping domain (add-only): idempotent creation, sourced deadline detail
    dedupe_key: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    storage_deadline_source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    storage_deadline_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    exception_summary: Mapped[str | None] = mapped_column(String, nullable=True)


class ShipmentLeg(Base, BusinessRow):
    __tablename__ = "shipment_legs"
    __mapper_args__ = {"eager_defaults": True}  # fetch server-generated updated_at via RETURNING (async-safe)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.id"), index=True)
    kind: Mapped[str] = mapped_column(String)  # export|ocean|port|domestic
    status: Mapped[str] = mapped_column(String, default="planned")  # planned|quoted|booked|in_progress|complete|cancelled
    vehicle_id: Mapped[str | None] = mapped_column(String, nullable=True)
    carrier_contact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    driver_contact: Mapped[str | None] = mapped_column(String, nullable=True)
    booking_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    booked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    appointment_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pickup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String, nullable=True)
    quote_id: Mapped[str | None] = mapped_column(String, nullable=True)
    approval_id: Mapped[str | None] = mapped_column(String, nullable=True)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    conditions: Mapped[str] = mapped_column(Text, default="")
    # added by the shipping domain (add-only)
    carrier_name: Mapped[str | None] = mapped_column(String, nullable=True)
    route_from: Mapped[str | None] = mapped_column(String, nullable=True)
    route_to: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)


class ShipmentMilestone(Base, BusinessRow):
    __tablename__ = "shipment_milestones"
    __mapper_args__ = {"eager_defaults": True}  # fetch server-generated updated_at via RETURNING (async-safe)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.id"), index=True)
    vehicle_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String)  # vessel_departed|vessel_arrival|discharge|release|carrier_booked|pickup|received
    status: Mapped[str] = mapped_column(String, default="completed")  # planned|estimated|completed
    at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_kind: Mapped[str] = mapped_column(String, default="manual")
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # added by the shipping domain (add-only): container-wide vs per-vehicle exception, supersession history
    applies_to: Mapped[str] = mapped_column(String, default="container")  # container|vehicle
    exception: Mapped[bool] = mapped_column(Boolean, default=False)  # a per-vehicle deviation from the container notice
    supersedes_id: Mapped[str | None] = mapped_column(String, nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    actor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)


class ShipmentQuote(Base, BusinessRow):
    """A vendor quote; distinct from forwarding and booking decisions (spec §8.3)."""
    __tablename__ = "shipment_quotes"
    __mapper_args__ = {"eager_defaults": True}  # fetch server-generated updated_at via RETURNING (async-safe)
    shipment_id: Mapped[str | None] = mapped_column(ForeignKey("shipments.id"), nullable=True, index=True)
    vehicle_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    case_id: Mapped[str | None] = mapped_column(String, nullable=True)
    vendor_contact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    vendor_name: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft", index=True)  # draft|pending_approval|requested|received|clarifying|forwarded|booked|declined|expired
    route_from: Mapped[str | None] = mapped_column(String, nullable=True)
    route_to: Mapped[str | None] = mapped_column(String, nullable=True)
    request_payload: Mapped[dict] = mapped_column(JSON, default=dict)  # data shared with vendor
    request_action_id: Mapped[str | None] = mapped_column(String, nullable=True)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reply_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String, nullable=True)
    binding: Mapped[str] = mapped_column(String, default="nonbinding")
    scope: Mapped[str] = mapped_column(Text, default="")
    inclusions: Mapped[list] = mapped_column(JSON, default=list)
    exclusions: Mapped[list] = mapped_column(JSON, default=list)
    timing: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    comparison: Mapped[dict] = mapped_column(JSON, default=dict)  # comparable quotes + weakness label
    forward_approval_id: Mapped[str | None] = mapped_column(String, nullable=True)
    booking_approval_id: Mapped[str | None] = mapped_column(String, nullable=True)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # added by the shipping domain (add-only): the adaptive quote case (spec §8.3 Montway journey)
    dedupe_key: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    leg_id: Mapped[str | None] = mapped_column(String, nullable=True)
    buyer_contact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    route_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)  # normalized from|to for comparisons
    service: Mapped[str | None] = mapped_column(String, nullable=True)  # open|enclosed|unknown
    operability: Mapped[str | None] = mapped_column(String, nullable=True)  # running|inoperable|unknown
    dimensions: Mapped[dict] = mapped_column(JSON, default=dict)  # {length_mm, width_mm, height_mm, weight_kg} recorded only
    size_class: Mapped[str | None] = mapped_column(String, nullable=True)  # kei_truck|kei_van|other|unknown
    timing_window: Mapped[dict] = mapped_column(JSON, default=dict)  # {earliest, latest, source}
    needs_information: Mapped[list] = mapped_column(JSON, default=list)  # [{field, reason}] never invented
    recipients: Mapped[list] = mapped_column(JSON, default=list)  # recipients bound to the approved request scope
    channel: Mapped[str | None] = mapped_column(String, nullable=True)  # email|web_form|manual
    request_payload_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    request_approval_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reply_extracted: Mapped[dict] = mapped_column(JSON, default=dict)
    clarification_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    forward_action_id: Mapped[str | None] = mapped_column(String, nullable=True)
    forwarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    forward_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    booking_action_id: Mapped[str | None] = mapped_column(String, nullable=True)
    booked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    booking: Mapped[dict] = mapped_column(JSON, default=dict)  # exact carrier/route/vehicle/amount/conditions approved
    check_count: Mapped[int] = mapped_column(Integer, default=0)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
