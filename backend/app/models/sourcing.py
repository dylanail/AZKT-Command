from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, BusinessRow

IR_LIFECYCLE = ("inquiry", "qualification", "deposit_pending", "active_search", "purchased", "delivered", "closed")
CANDIDATE_MATCH_STATES = ("discovered", "evaluated", "translation_requested", "translation_incomplete",
                          "translation_complete", "reevaluated", "buyer_review", "bid_decision",
                          "won", "lost", "passed", "rejected")


class ImportRequest(Base, BusinessRow):
    """Import request lifecycle (spec §8.1). The Sales IRQ card is a projection of this."""
    __tablename__ = "import_requests"
    contact_id: Mapped[str] = mapped_column(ForeignKey("contacts.id"), index=True)
    opportunity_id: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    title: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="inquiry", index=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    paused_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    requirements: Mapped[list] = mapped_column(JSON, default=list)  # [{tier: must|prefer|avoid, text, key}]
    requirements_version: Mapped[int] = mapped_column(Integer, default=1)
    requirements_history: Mapped[list] = mapped_column(JSON, default=list)  # [{version, requirements, evidence, at, by}]
    budget_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    budget_currency: Mapped[str | None] = mapped_column(String, nullable=True)
    agreement_id: Mapped[str | None] = mapped_column(String, nullable=True)
    agreement_status: Mapped[str] = mapped_column(String, default="none")  # none|sent|signed
    deposit_rule: Mapped[dict] = mapped_column(JSON, default=dict)  # {amount, currency} or empty = unset
    deposit_status: Mapped[str] = mapped_column(String, default="unset")  # unset|pending|confirmed|refunded
    deposit_invoice_id: Mapped[str | None] = mapped_column(String, nullable=True)
    purchased_vehicle_id: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    exclusions: Mapped[list] = mapped_column(JSON, default=list)  # rejected candidate ids + reasons
    notes: Mapped[str] = mapped_column(Text, default="")
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    legacy_irq_id: Mapped[str | None] = mapped_column(String, nullable=True)


class Candidate(Base, BusinessRow):
    """An auction candidate; identity is provider+lot+auction date (spec §8.2)."""
    __tablename__ = "candidates"
    __table_args__ = (UniqueConstraint("provider", "auction_house", "lot_no", "auction_at", name="uq_candidate_identity"),)
    provider: Mapped[str] = mapped_column(String, default="manual")
    auction_house: Mapped[str | None] = mapped_column(String, nullable=True)
    lot_no: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    auction_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(String, default="")
    frame_raw: Mapped[str | None] = mapped_column(String, nullable=True)
    specs: Mapped[dict] = mapped_column(JSON, default=dict)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    images: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String, default="discovered", index=True)  # discovered|active|won|lost|expired|withdrawn
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    vehicle_id: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)  # one purchased vehicle per win
    result: Mapped[dict] = mapped_column(JSON, default=dict)


class CandidateMatch(Base, BusinessRow):
    __tablename__ = "candidate_matches"
    __table_args__ = (UniqueConstraint("candidate_id", "import_request_id", name="uq_candidate_request"),)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"), index=True)
    import_request_id: Mapped[str] = mapped_column(ForeignKey("import_requests.id"), index=True)
    requirements_version: Mapped[int] = mapped_column(Integer, default=1)
    outcomes: Mapped[list] = mapped_column(JSON, default=list)  # [{key, tier, result: pass|fail|unknown, evidence}]
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mandatory_fail: Mapped[bool] = mapped_column(Boolean, default=False)
    mandatory_unknown: Mapped[bool] = mapped_column(Boolean, default=False)
    bid_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String, default="discovered", index=True)
    rejected_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    buyer_draft_id: Mapped[str | None] = mapped_column(String, nullable=True)
    buyer_message_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    translation_id: Mapped[str | None] = mapped_column(String, nullable=True)


class Translation(Base, BusinessRow):
    __tablename__ = "translations"
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"), index=True)
    status: Mapped[str] = mapped_column(String, default="draft", index=True)  # draft|pending_approval|requested|detected|incomplete|complete|revised|invalidated
    request_channel: Mapped[str | None] = mapped_column(String, nullable=True)  # teams|manual
    request_action_id: Mapped[str | None] = mapped_column(String, nullable=True)
    manual_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    doc_provider: Mapped[str | None] = mapped_column(String, nullable=True)  # google_docs
    doc_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    doc_revision: Mapped[str | None] = mapped_column(String, nullable=True)
    revision_no: Mapped[int] = mapped_column(Integer, default=0)
    completeness: Mapped[dict] = mapped_column(JSON, default=dict)  # required sections → present?
    excerpts: Mapped[list] = mapped_column(JSON, default=list)  # [{section, ja, en, doc_revision}]
    findings: Mapped[dict] = mapped_column(JSON, default=dict)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Bid(Base, BusinessRow):
    __tablename__ = "bids"
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"), index=True)
    import_request_id: Mapped[str | None] = mapped_column(ForeignKey("import_requests.id"), nullable=True, index=True)
    max_amount: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    currency: Mapped[str] = mapped_column(String, default="JPY")
    fee_basis: Mapped[str] = mapped_column(Text, default="")
    fx_estimate: Mapped[dict] = mapped_column(JSON, default=dict)  # {rate, source, date, usd}
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    packet: Mapped[dict] = mapped_column(JSON, default=dict)  # exact reviewable packet snapshot
    packet_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    approval_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft", index=True)  # draft|pending_approval|approved|submitted|won|lost|invalidated|cancelled|expired
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    result_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    invalidated_reason: Mapped[str | None] = mapped_column(String, nullable=True)
