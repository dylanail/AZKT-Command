"""Tables carried over from the droplet-era build (Notion mirror, OpenClaw shims,
token ledger). Retained as legacy adapters; the v4 domains live in their own modules."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, BusinessRow


class StageTransition(Base, BusinessRow):
    __tablename__ = "stage_transitions"
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("vehicles.id"), index=True)
    from_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    to_stage: Mapped[str] = mapped_column(String)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String, default="dashboard")


class Customer(Base, BusinessRow):
    """Legacy Notion-mirrored customer. Canonical contacts live in `contacts`."""
    __tablename__ = "customers"
    notion_page_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    name: Mapped[str] = mapped_column(String, default="")
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    contact_id: Mapped[str | None] = mapped_column(String, nullable=True)  # link to canonical contact
    extra: Mapped[dict] = mapped_column(JSON, default=dict)


class IRQ(Base, BusinessRow):
    """Legacy Notion-mirrored import request. Canonical requests live in `import_requests`."""
    __tablename__ = "irqs"
    notion_page_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    title: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="open", index=True)
    assignee: Mapped[str | None] = mapped_column(String, nullable=True)
    customer_id: Mapped[str | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    import_request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)


class UsageEvent(Base, BusinessRow):
    """Token usage ingested from the append-only JSONL ledger (OpenClaw agents)."""
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
    __tablename__ = "shipping_events"
    vehicle_id: Mapped[str | None] = mapped_column(ForeignKey("vehicles.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String)
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
    tier: Mapped[str] = mapped_column(String)
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
    """Editable runtime settings (reminder preferences, automation caps, gate rules...)."""
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)


class ChatMessage(Base, BusinessRow):
    """Legacy per-OpenClaw-agent chat. Manager chat lives in `chat_turns`."""
    __tablename__ = "chat_messages"
    agent_key: Mapped[str] = mapped_column(String, index=True)
    role: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
