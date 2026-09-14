from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, BusinessRow

PROVIDERS = ("gmail_business", "gmail_personal", "drive", "sheets", "square", "telegram", "wordpress",
             "woocommerce", "model", "smtp", "sms", "calendar", "legacy_notion", "legacy_openclaw")


class Connection(Base, BusinessRow):
    """A provider account connection with freshness tracking (spec §3.1, §12.4)."""
    __tablename__ = "connections"
    provider: Mapped[str] = mapped_column(String, index=True)
    label: Mapped[str] = mapped_column(String, default="")
    account_identity: Mapped[str | None] = mapped_column(String, nullable=True)  # email / merchant id / site url
    status: Mapped[str] = mapped_column(String, default="disconnected", index=True)  # disconnected|connected|warn|degraded|expired|error
    config: Mapped[dict] = mapped_column(JSON, default=dict)  # non-secret: folder id, allowlist, site url, scopes
    secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # Fernet-encrypted JSON (tokens)
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    granted_scopes: Mapped[list] = mapped_column(JSON, default=list)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    coverage_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    coverage_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    watch_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure: Mapped[dict] = mapped_column(JSON, default=dict)
    dependent_workflows: Mapped[list] = mapped_column(JSON, default=list)
    environment: Mapped[str] = mapped_column(String, default="development")
    connected_by: Mapped[str | None] = mapped_column(String, nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SyncCursor(Base, BusinessRow):
    __tablename__ = "sync_cursors"
    __table_args__ = (UniqueConstraint("connection_id", "name", name="uq_sync_cursor"),)
    connection_id: Mapped[str] = mapped_column(ForeignKey("connections.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    advanced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProviderEvent(Base, BusinessRow):
    """Durably stored inbound provider event, deduplicated by provider identity (spec §4.2, §6.3, §5.5)."""
    __tablename__ = "provider_events"
    __table_args__ = (UniqueConstraint("provider", "connection_key", "provider_event_id", name="uq_provider_event"),)
    provider: Mapped[str] = mapped_column(String, index=True)
    connection_key: Mapped[str] = mapped_column(String, default="")  # connection id / bot id / merchant id
    provider_event_id: Mapped[str] = mapped_column(String)
    event_type: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    signature_ok: Mapped[bool] = mapped_column(Boolean, default=True)


class Conversation(Base, BusinessRow):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("connection_id", "provider_thread_id", name="uq_conversation_thread"),)
    connection_id: Mapped[str | None] = mapped_column(ForeignKey("connections.id"), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String, default="email", index=True)  # email|sms|telegram|web
    provider_thread_id: Mapped[str | None] = mapped_column(String, nullable=True)
    subject: Mapped[str] = mapped_column(String, default="")
    participants: Mapped[list] = mapped_column(JSON, default=list)
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id"), nullable=True, index=True)
    contact_match: Mapped[str] = mapped_column(String, default="unmatched")  # matched|proposed|ambiguous|unmatched
    links: Mapped[list] = mapped_column(JSON, default=list)  # [{kind: vehicle|opportunity|import_request|shipment, id, match: matched|proposed}]
    classification: Mapped[str] = mapped_column(String, default="unmatched", index=True)  # customer|supplier|logistics|payment|newsletter|spam|automated|unmatched|personal_allowlisted
    state: Mapped[str] = mapped_column(String, default="needs_reply", index=True)  # needs_reply|drafting|blocked|awaiting_approval|replied|taken_over|unmatched|archived|no_reply_needed
    language: Mapped[str | None] = mapped_column(String, nullable=True)
    takeover_by: Mapped[str | None] = mapped_column(String, nullable=True)
    takeover_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    send_decision_version: Mapped[int] = mapped_column(Integer, default=0)  # invariant 3
    spam_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    case_id: Mapped[str | None] = mapped_column(String, nullable=True)


class Message(Base, BusinessRow):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("connection_id", "provider_message_id", name="uq_message_provider"),)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    connection_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    provider_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    direction: Mapped[str] = mapped_column(String, default="in")  # in|out
    from_addr: Mapped[str | None] = mapped_column(String, nullable=True)
    to_addrs: Mapped[list] = mapped_column(JSON, default=list)
    cc_addrs: Mapped[list] = mapped_column(JSON, default=list)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    subject: Mapped[str] = mapped_column(String, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    body_new_text: Mapped[str] = mapped_column(Text, default="")  # quotes/signatures stripped
    snippet: Mapped[str] = mapped_column(String, default="")
    attachments: Mapped[list] = mapped_column(JSON, default=list)  # asset ids + names
    headers: Mapped[dict] = mapped_column(JSON, default=dict)
    is_automated: Mapped[bool] = mapped_column(Boolean, default=False)
    classification: Mapped[str | None] = mapped_column(String, nullable=True)
    extracted: Mapped[dict] = mapped_column(JSON, default=dict)  # questions, items, amounts, promises
    sent_by: Mapped[str | None] = mapped_column(String, nullable=True)  # user id or 'azkt'
    draft_id: Mapped[str | None] = mapped_column(String, nullable=True)
    receipt: Mapped[dict] = mapped_column(JSON, default=dict)


class Draft(Base, BusinessRow):
    """A versioned outbound reply (spec §4.3–4.4). New version invalidates the old approval."""
    __tablename__ = "drafts"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    reply_to_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    draft_version: Mapped[int] = mapped_column(Integer, default=1)
    to_addrs: Mapped[list] = mapped_column(JSON, default=list)
    cc_addrs: Mapped[list] = mapped_column(JSON, default=list)
    subject: Mapped[str] = mapped_column(String, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    answer_plan: Mapped[list] = mapped_column(JSON, default=list)  # [{question, facts_needed, answered, source}]
    sources: Mapped[list] = mapped_column(JSON, default=list)
    checks: Mapped[list] = mapped_column(JSON, default=list)  # [{key, ok, label, remediation}]
    status: Mapped[str] = mapped_column(String, default="draft", index=True)  # draft|blocked|pending_approval|approved|sending|sent|invalidated|superseded|declined
    blocked_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    approval_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_draft_id: Mapped[str | None] = mapped_column(String, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by_role: Mapped[str] = mapped_column(String, default="customer_sales")
    invalidated_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    sent_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    account_connection_id: Mapped[str | None] = mapped_column(String, nullable=True)
    supersedes_id: Mapped[str | None] = mapped_column(String, nullable=True)
    edit_history: Mapped[list] = mapped_column(JSON, default=list)
