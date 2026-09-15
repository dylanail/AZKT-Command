from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, BusinessRow

DELIVERY_KINDS = ("task_reminder", "overdue", "digest", "deposit_confirmed", "case_update", "connection_issue")
CHANNELS = ("email", "telegram", "inapp")


class ScheduledDelivery(Base, BusinessRow):
    """A durable, worker-claimed notification (spec §5.6). Unique per
    (task, revision, kind, recipient, channel) via dedupe_key."""
    __tablename__ = "scheduled_deliveries"
    kind: Mapped[str] = mapped_column(String, index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), nullable=True, index=True)
    task_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    entity_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    recipient_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    channel: Mapped[str] = mapped_column(String, index=True)  # email|telegram|inapp
    deliver_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    state: Mapped[str] = mapped_column(String, default="scheduled", index=True)  # scheduled|claimed|sent|accepted|delivered|failed|cancelled|expired|superseded
    dedupe_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    receipt: Mapped[dict] = mapped_column(JSON, default=dict)
    late: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fallback_of_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # added by the reminders domain (add-only)
    provider_ref: Mapped[str | None] = mapped_column(String, nullable=True, index=True)  # reconcile an unknown send
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String, nullable=True)  # why an obsolete delivery was suppressed


class Notification(Base, BusinessRow):
    """In-app bell / Home / mobile sheet item. One source for all surfaces (spec §5.4)."""
    __tablename__ = "notifications"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    urgency: Mapped[str] = mapped_column(String, default="later")  # high|today|later
    title: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text, default="")
    entity_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    deep_link: Mapped[str | None] = mapped_column(String, nullable=True)
    group_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    dedupe_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    state: Mapped[str] = mapped_column(String, default="unread", index=True)  # unread|read|acknowledged|snoozed|resolved
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # added by the reminders domain (add-only): repeated facts about one problem collapse into one row
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class TelegramPairing(Base, BusinessRow):
    """Owner ↔ private Telegram chat binding (spec §5.5). Immutable IDs, not usernames."""
    __tablename__ = "telegram_pairings"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)  # pending|active|revoked|expired
    start_token_hash: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    username_snapshot: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmed_in_app_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_in_chat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_failures: Mapped[int] = mapped_column(Integer, default=0)
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    context: Mapped[dict] = mapped_column(JSON, default=dict)  # pinned vehicle/case context for the chat
    # added by the reminders/telegram domain (add-only)
    token_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
