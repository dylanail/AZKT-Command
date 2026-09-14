from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, BusinessRow

LOGISTICS_STATES = ("candidate", "purchased", "export_pending", "on_vessel", "at_port", "released",
                    "domestic_transit", "received", "not_applicable")
RECON_STATES = ("needs_inspection", "in_recon", "finalization", "ready_for_sale")
COMMERCIAL_STATES = ("not_listed", "listed", "reserved", "sold", "delivered")
DOCUMENT_STATES = ("pending", "complete", "conflicted", "missing")
HEALTH = ("blocked", "risk", "ok", "wait")
FACT_STATUSES = ("reported", "inferred", "confirmed", "estimated", "conflicted", "outdated", "unknown")
MILESTONE_KINDS = ("purchased", "export_cleared", "on_vessel", "arrived_port", "released", "received",
                   "inspected", "recon_started", "ready", "listed", "reserved", "sold", "delivered")


class Vehicle(Base, BusinessRow):
    """Canonical vehicle. Independent logistics/recon/commercial/documents states (spec §2.3, §3.1).
    Legacy droplet columns (stage, sold_price_usd, ...) are retained for the Notion mirror."""
    __tablename__ = "vehicles"
    # legacy / mirror
    notion_page_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    stage: Mapped[str] = mapped_column(String, default="sourced", index=True)  # legacy single-stage view
    auction_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    sold_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    landed_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    won_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sold_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    days_on_market: Mapped[int | None] = mapped_column(Integer, nullable=True)
    customer_id: Mapped[str | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    stage_timestamps: Mapped[dict] = mapped_column(JSON, default=dict)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    # identity (spec §3.1: preserve raw frame id; normalized search form separate)
    title: Mapped[str] = mapped_column(String, default="")
    stock_no: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)  # STK-0412 (human ref)
    frame_no_raw: Mapped[str | None] = mapped_column(String, nullable=True)
    frame_no_norm: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    make: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    model_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    color: Mapped[str | None] = mapped_column(String, nullable=True)
    grade: Mapped[str | None] = mapped_column(String, nullable=True)
    odometer_km: Mapped[int | None] = mapped_column(Integer, nullable=True)
    intake_status: Mapped[str] = mapped_column(String, default="complete")  # complete|incomplete
    missing_identity_fields: Mapped[list] = mapped_column(JSON, default=list)
    # allocation / states
    allocation: Mapped[str] = mapped_column(String, default="inventory", index=True)  # inventory|reserved|sold|candidate
    buyer_contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    logistics_state: Mapped[str] = mapped_column(String, default="purchased", index=True)
    recon_state: Mapped[str] = mapped_column(String, default="needs_inspection", index=True)
    commercial_state: Mapped[str] = mapped_column(String, default="not_listed", index=True)
    documents_state: Mapped[str] = mapped_column(String, default="pending", index=True)
    health: Mapped[str] = mapped_column(String, default="ok", index=True)
    health_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    situation: Mapped[str | None] = mapped_column(String, nullable=True)
    exception_summary: Mapped[str | None] = mapped_column(String, nullable=True)
    next_action: Mapped[str | None] = mapped_column(String, nullable=True)
    next_action_owner_id: Mapped[str | None] = mapped_column(String, nullable=True)
    next_action_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    # condition at intake (spec §7.4)
    condition_summary: Mapped[list] = mapped_column(JSON, default=list)  # [{text, source, evidence:[asset ids], observation_id}]
    condition_version: Mapped[int] = mapped_column(Integer, default=0)
    condition_history: Mapped[list] = mapped_column(JSON, default=list)
    # dates (sourced; None = Not recorded)
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    listed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # money summary (canonical amounts live in cost_items / sales)
    purchase_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    purchase_currency: Mapped[str | None] = mapped_column(String, nullable=True)
    asking_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    asking_currency: Mapped[str | None] = mapped_column(String, nullable=True)
    price_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # links
    origin_candidate_id: Mapped[str | None] = mapped_column(String, nullable=True)
    purchase_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    active_sale_id: Mapped[str | None] = mapped_column(String, nullable=True)
    hero_asset_id: Mapped[str | None] = mapped_column(String, nullable=True)
    photo_requirements: Mapped[list] = mapped_column(JSON, default=list)  # e.g. ["front_34","interior","rear","side","bed","engine"]
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    # added by the vehicles domain (add-only): critical-fact columns, gate facts, state history
    title_status: Mapped[str | None] = mapped_column(String, nullable=True)  # critical fact (owner-confirmed)
    inspected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disclosures: Mapped[list] = mapped_column(JSON, default=list)  # [{text, by, at}] for the disclosures_written gate
    state_history: Mapped[list] = mapped_column(JSON, default=list)  # [{dimension, from, to, reason, at, by, backward}]
    stock_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)  # numeric part of an allocated STK-####


class VehicleFact(Base, BusinessRow):
    """Field-level fact with provenance (spec §3.2)."""
    __tablename__ = "vehicle_facts"
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("vehicles.id"), index=True)
    key: Mapped[str] = mapped_column(String, index=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_num: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    unit: Mapped[str | None] = mapped_column(String, nullable=True)
    currency: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="reported")
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_kind: Mapped[str] = mapped_column(String, default="manual")  # manual|message|document|auction_sheet|intake|image|ledger|provider
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    source_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    actor: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence_method: Mapped[str | None] = mapped_column(String, nullable=True)
    supersedes_id: Mapped[str | None] = mapped_column(String, nullable=True)
    conflict_with_id: Mapped[str | None] = mapped_column(String, nullable=True)
    visibility: Mapped[str] = mapped_column(String, default="all")  # all|owner
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class VehicleMilestone(Base, BusinessRow):
    """Timeline milestone: planned / estimated / completed with source (spec §2.4)."""
    __tablename__ = "vehicle_milestones"
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("vehicles.id"), index=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, default="completed")  # planned|estimated|completed
    at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_kind: Mapped[str] = mapped_column(String, default="manual")
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    supersedes_id: Mapped[str | None] = mapped_column(String, nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    shipment_id: Mapped[str | None] = mapped_column(String, nullable=True)


class ReconIssue(Base, BusinessRow):
    """Inspection finding / reported issue (spec §8.4). Distinct from the task that fixes it."""
    __tablename__ = "recon_issues"
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("vehicles.id"), index=True)
    title: Mapped[str] = mapped_column(String)
    detail: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String, default="normal")
    status: Mapped[str] = mapped_column(String, default="open", index=True)  # open|in_progress|resolved|wont_fix|rejected
    source_kind: Mapped[str] = mapped_column(String, default="owner_reported")  # owner_reported|image_observed|inspection|proposed_check|customer
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    intake_observation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    asset_ids: Mapped[list] = mapped_column(JSON, default=list)
    work_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    disclosure_required: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # added by the vehicles domain (add-only): status also allows `deferred` (explicitly deferred work)
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    deferred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deferred_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    deferred_by: Mapped[str | None] = mapped_column(String, nullable=True)


class WorkOrder(Base, BusinessRow):
    __tablename__ = "work_orders"
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("vehicles.id"), index=True)
    ref: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="open")  # open|in_progress|done|verified|cancelled
    assignee_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    recon_issue_id: Mapped[str | None] = mapped_column(String, nullable=True)
    cost_item_id: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")


class Part(Base, BusinessRow):
    """Parts keep physical state separate from payment state (spec §8.4)."""
    __tablename__ = "parts"
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("vehicles.id"), index=True)
    work_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    name: Mapped[str] = mapped_column(String)
    part_no: Mapped[str | None] = mapped_column(String, nullable=True)
    vendor_contact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String, default="requested", index=True)  # requested|approved|ordered|arrived|installed|verified|cancelled|returned
    requested_by: Mapped[str | None] = mapped_column(String, nullable=True)
    order_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    ordered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    arrived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    installed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by: Mapped[str | None] = mapped_column(String, nullable=True)
    cost_item_id: Mapped[str | None] = mapped_column(String, nullable=True)
    approval_id: Mapped[str | None] = mapped_column(String, nullable=True)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    # added by the vehicles domain (add-only): payment state is separate from physical state (spec §8.4, G10)
    payment_state: Mapped[str] = mapped_column(String, default="unpaid")  # unpaid|paid|refunded|unknown
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payment_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_action_id: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    history: Mapped[list] = mapped_column(JSON, default=list)  # [{from, to, at, by, note}]


class ShopGateRule(Base, BusinessRow):
    """Configurable gate requirements per target recon stage (spec §8.4, F1)."""
    __tablename__ = "shop_gate_rules"
    to_state: Mapped[str] = mapped_column(String, index=True)  # in_recon|finalization|ready_for_sale
    requirement: Mapped[str] = mapped_column(String)  # photos_min|recon_verified|disclosures_written|inspection_logged|docs_complete
    label: Mapped[str] = mapped_column(String)
    param: Mapped[dict] = mapped_column(JSON, default=dict)  # e.g. {"min": 6}
    overridable: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
