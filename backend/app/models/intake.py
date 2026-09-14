from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, BusinessRow


class VehicleIntake(Base, BusinessRow):
    """Durable photo/voice intake session (spec §7.4)."""
    __tablename__ = "vehicle_intakes"
    __table_args__ = (UniqueConstraint("client_key", "request_key", name="uq_intake_request"),)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    channel: Mapped[str] = mapped_column(String, default="web")  # web|telegram|mcp|http
    client_key: Mapped[str] = mapped_column(String, default="")  # user id / external client id
    request_key: Mapped[str | None] = mapped_column(String, nullable=True)  # idempotency
    target_mode: Mapped[str] = mapped_column(String, default="find")  # new|existing|find
    vehicle_id: Mapped[str | None] = mapped_column(ForeignKey("vehicles.id"), nullable=True, index=True)
    candidate_vehicle_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String, default="open", index=True)  # open|analyzing|needs_choice|needs_info|applied|partially_applied|failed|abandoned
    revision: Mapped[int] = mapped_column(Integer, default=1)
    text_notes: Mapped[str] = mapped_column(Text, default="")
    transcript: Mapped[str] = mapped_column(Text, default="")
    transcript_asset_ids: Mapped[list] = mapped_column(JSON, default=list)
    asset_ids: Mapped[list] = mapped_column(JSON, default=list)
    failed_asset_ids: Mapped[list] = mapped_column(JSON, default=list)
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)  # raw model output, per revision
    result: Mapped[dict] = mapped_column(JSON, default=dict)  # {vehicle_id, created, bullets, tasks_created, tasks_updated, photos_saved, missing, failures}
    missing_fields: Mapped[list] = mapped_column(JSON, default=list)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    mission_id: Mapped[str | None] = mapped_column(String, nullable=True)
    telegram_media_group_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    device_draft: Mapped[dict] = mapped_column(JSON, default=dict)  # saved-on-device bookkeeping echoed back


class IntakeObservation(Base, BusinessRow):
    """One extracted observation with provenance; applied once (invariant 13)."""
    __tablename__ = "intake_observations"
    intake_id: Mapped[str] = mapped_column(ForeignKey("vehicle_intakes.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[str] = mapped_column(String)  # condition|request|identifier|milestone|priority|assignee|note
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String, default="owner_text")  # owner_text|owner_voice|image|proposed_check
    asset_id: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[str] = mapped_column(String, default="stated")  # stated|observed|uncertain
    field: Mapped[str | None] = mapped_column(String, nullable=True)  # for identifiers: frame_no|stock_no|odometer|...
    value: Mapped[str | None] = mapped_column(String, nullable=True)
    applied_kind: Mapped[str | None] = mapped_column(String, nullable=True)  # recon_issue|task|fact|condition_bullet|milestone
    applied_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending|applied|skipped|rejected|failed|needs_confirmation
    dedupe_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    removed: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
