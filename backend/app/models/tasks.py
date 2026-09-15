from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, BusinessRow

TASK_TYPES = ("call", "meeting", "follow_up", "operational")
TASK_STATUSES = ("open", "in_progress", "blocked", "waiting", "awaiting_verification", "completed", "cancelled")
REMINDER_KINDS = ("at", "15m", "1h", "1d", "custom")


class Task(Base, BusinessRow):
    """One assigned step of work. Overdue is derived (spec §5.3)."""
    __tablename__ = "tasks"
    title: Mapped[str] = mapped_column(String)
    type: Mapped[str] = mapped_column(String, default="operational", index=True)
    status: Mapped[str] = mapped_column(String, default="open", index=True)
    priority: Mapped[str] = mapped_column(String, default="normal")  # low|normal|high|urgent
    owner_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    assigned_by: Mapped[str | None] = mapped_column(String, nullable=True)
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id"), nullable=True, index=True)
    opportunity_id: Mapped[str | None] = mapped_column(ForeignKey("opportunities.id"), nullable=True, index=True)
    vehicle_id: Mapped[str | None] = mapped_column(ForeignKey("vehicles.id"), nullable=True, index=True)
    case_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    shipment_id: Mapped[str | None] = mapped_column(String, nullable=True)
    import_request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    recon_issue_id: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    instructions: Mapped[str] = mapped_column(Text, default="")
    # scheduling
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    timezone: Mapped[str] = mapped_column(String, default="America/Phoenix")
    reminder_kind: Mapped[str | None] = mapped_column(String, nullable=True)  # at|15m|1h|1d|custom
    reminder_custom_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schedule_revision: Mapped[int] = mapped_column(Integer, default=1)  # bumps on reschedule/cancel
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    next_check_label: Mapped[str | None] = mapped_column(String, nullable=True)  # "AZKT reminder" vs external
    # evidence / verification (spec §8.4)
    evidence_required: Mapped[list] = mapped_column(JSON, default=list)  # [{kind: photo|note|receipt|reading, label, min}]
    evidence: Mapped[list] = mapped_column(JSON, default=list)  # [{asset_id?, note?, by, at}]
    block_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verification_status: Mapped[str] = mapped_column(String, default="none")  # none|awaiting|verified|rejected
    verified_by: Mapped[str | None] = mapped_column(String, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    # provenance / dedupe (spec §7.4, invariant 13)
    source_kind: Mapped[str | None] = mapped_column(String, nullable=True)  # intake|gate|inbox|manual|agent|telegram|mcp
    source_id: Mapped[str | None] = mapped_column(String, nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    gate_requirement: Mapped[str | None] = mapped_column(String, nullable=True)
    is_suggestion: Mapped[bool] = mapped_column(Boolean, default=False)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)


class Case(Base, BusinessRow):
    """An ongoing outcome that outlives individual runs/tasks (spec §3.1, §10.4)."""
    __tablename__ = "cases"
    title: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, index=True)  # shipping_quote|shipment|recon|sale|dispute|listing_cleanup|sourcing|reply|other
    status: Mapped[str] = mapped_column(String, default="open", index=True)  # open|waiting|blocked|needs_owner|resolved|cancelled
    owner_role: Mapped[str] = mapped_column(String, default="manager")
    owner_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    vehicle_id: Mapped[str | None] = mapped_column(ForeignKey("vehicles.id"), nullable=True, index=True)
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    opportunity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    shipment_id: Mapped[str | None] = mapped_column(String, nullable=True)
    import_request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    mission_id: Mapped[str | None] = mapped_column(String, nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    waiting_on: Mapped[str | None] = mapped_column(String, nullable=True)
    next_action: Mapped[str | None] = mapped_column(String, nullable=True)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)


class Commitment(Base, BusinessRow):
    """A promise made to a person, established from confirmed sends or an authorized record."""
    __tablename__ = "commitments"
    text: Mapped[str] = mapped_column(Text)
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id"), nullable=True, index=True)
    vehicle_id: Mapped[str | None] = mapped_column(ForeignKey("vehicles.id"), nullable=True)
    opportunity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    made_by: Mapped[str | None] = mapped_column(String, nullable=True)
    made_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String, default="open")  # proposed|open|met|missed|withdrawn
    source_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    source_id: Mapped[str | None] = mapped_column(String, nullable=True)
