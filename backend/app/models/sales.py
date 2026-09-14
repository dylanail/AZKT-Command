from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, JSON, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, BusinessRow

PIPELINES = ("irq", "vehicle")
STAGES = ("new", "conversation", "awaiting_deposit", "deposit_paid", "lost")


class Opportunity(Base, BusinessRow):
    """A sales opportunity in the IRQ or Vehicle Sales pipeline (spec §5.1)."""
    __tablename__ = "opportunities"
    contact_id: Mapped[str] = mapped_column(ForeignKey("contacts.id"), index=True)
    pipeline: Mapped[str] = mapped_column(String, index=True)  # irq|vehicle
    stage: Mapped[str] = mapped_column(String, default="new", index=True)
    vehicle_id: Mapped[str | None] = mapped_column(ForeignKey("vehicles.id"), nullable=True, index=True)
    import_request_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    enquiry: Mapped[str] = mapped_column(Text, default="")
    budget_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    budget_currency: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, default="manual")
    owner_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    next_action: Mapped[str | None] = mapped_column(String, nullable=True)
    lost_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    lost_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deposit_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deposit_payment_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # exactly one conversion link (invariant 6)
    converted_kind: Mapped[str | None] = mapped_column(String, nullable=True)  # import_request|sale
    converted_id: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    conversation_ids: Mapped[list] = mapped_column(JSON, default=list)
    stage_history: Mapped[list] = mapped_column(JSON, default=list)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
