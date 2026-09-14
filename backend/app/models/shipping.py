from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, JSON, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, BusinessRow


class Shipment(Base, BusinessRow):
    """Container/vessel-level shipment; vehicles are members (spec §8.3)."""
    __tablename__ = "shipments"
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


class ShipmentLeg(Base, BusinessRow):
    __tablename__ = "shipment_legs"
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


class ShipmentMilestone(Base, BusinessRow):
    __tablename__ = "shipment_milestones"
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.id"), index=True)
    vehicle_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String)  # vessel_departed|vessel_arrival|discharge|release|carrier_booked|pickup|received
    status: Mapped[str] = mapped_column(String, default="completed")  # planned|estimated|completed
    at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_kind: Mapped[str] = mapped_column(String, default="manual")
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class ShipmentQuote(Base, BusinessRow):
    """A vendor quote; distinct from forwarding and booking decisions (spec §8.3)."""
    __tablename__ = "shipment_quotes"
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
