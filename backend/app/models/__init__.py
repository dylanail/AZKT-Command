from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, BusinessRow, _uuid


# ── Auth (single user, passkeys only — no passwords, no magic links) ────────
class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    handle: Mapped[str] = mapped_column(String, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Credential(Base):
    """A registered WebAuthn passkey (Face ID / platform authenticator)."""
    __tablename__ = "credentials"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    credential_id: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    transports: Mapped[str] = mapped_column(String, default="")
    label: Mapped[str] = mapped_column(String, default="passkey")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# ── Business data (Postgres is source of truth; Notion is the mirror) ───────
class Vehicle(Base, BusinessRow):
    __tablename__ = "vehicles"
    notion_page_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    title: Mapped[str] = mapped_column(String, default="")
    stage: Mapped[str] = mapped_column(String, default="sourced", index=True)
    auction_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    sold_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    landed_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    won_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sold_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    days_on_market: Mapped[int | None] = mapped_column(Integer, nullable=True)
    customer_id: Mapped[str | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    # Per-stage entry timestamps stored as discrete fields (not edit history)
    # so "avg time in Customs" is a column query, not a parse.
    stage_timestamps: Mapped[dict] = mapped_column(JSON, default=dict)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)


class StageTransition(Base, BusinessRow):
    __tablename__ = "stage_transitions"
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("vehicles.id"), index=True)
    from_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    to_stage: Mapped[str] = mapped_column(String)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String, default="dashboard")


class Customer(Base, BusinessRow):
    __tablename__ = "customers"
    notion_page_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    name: Mapped[str] = mapped_column(String, default="")
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)


class IRQ(Base, BusinessRow):
    __tablename__ = "irqs"
    notion_page_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    title: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="open", index=True)
    assignee: Mapped[str | None] = mapped_column(String, nullable=True)
    customer_id: Mapped[str | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)


class ApprovalCard(Base, BusinessRow):
    """Unified approval queue — replaces scrolling 5 Telegram bots."""
    __tablename__ = "approval_cards"
    agent_key: Mapped[str] = mapped_column(String, index=True)
    vehicle_id: Mapped[str | None] = mapped_column(ForeignKey("vehicles.id"), nullable=True)
    title: Mapped[str] = mapped_column(String)
    context: Mapped[dict] = mapped_column(JSON, default=dict)  # why, approve vs reject
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UsageEvent(Base, BusinessRow):
    """Token usage ingested from the append-only JSONL ledger."""
    __tablename__ = "usage_events"
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    agent_key: Mapped[str] = mapped_column(String, index=True)
    model: Mapped[str] = mapped_column(String, index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    run_kind: Mapped[str] = mapped_column(String, default="unknown")
    dedupe_key: Mapped[str] = mapped_column(String, unique=True, index=True)


class ShippingEvent(Base, BusinessRow):
    """Intentionally empty in v1. Structure exists so the future customs/
    transport email-parser agent has somewhere to write (Sebastian/Corey)."""
    __tablename__ = "shipping_events"
    vehicle_id: Mapped[str | None] = mapped_column(ForeignKey("vehicles.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String)            # e.g. customs_cleared
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class AgentState(Base, BusinessRow):
    __tablename__ = "agent_state"
    agent_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    last_health: Mapped[dict] = mapped_column(JSON, default=dict)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String, default="unknown")
    error_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotificationLog(Base, BusinessRow):
    __tablename__ = "notification_log"
    rule_id: Mapped[str] = mapped_column(String, index=True)
    tier: Mapped[str] = mapped_column(String)  # push | dashboard
    title: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text, default="")
    dedupe_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    pushed: Mapped[bool] = mapped_column(Boolean, default=False)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)


class SyncState(Base):
    __tablename__ = "sync_state"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Setting(Base):
    """Editable runtime settings, incl. not-connected integration creds
    (GoHighLevel / Twilio / Square) entered later via the UI."""
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class ChatMessage(Base, BusinessRow):
    """Persistent per-agent chat so conversations scroll back."""
    __tablename__ = "chat_messages"
    agent_key: Mapped[str] = mapped_column(String, index=True)
    role: Mapped[str] = mapped_column(String)  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
