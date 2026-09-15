from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, BusinessRow

CLIENT_SCOPES = (
    "read:vehicles", "read:tasks", "read:contacts", "read:sales", "read:requests", "read:shipping",
    "read:costs", "read:photos", "read:sources", "read:activity",
    "write:tasks", "write:vehicles", "write:contacts", "write:sales", "write:requests", "write:notes",
    "intake", "draft:messages", "ask",
)


class ExternalClient(Base, BusinessRow):
    """A registered external agent client with a bounded grant (spec §10.8)."""
    __tablename__ = "external_clients"
    name: Mapped[str] = mapped_column(String)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    transport: Mapped[str] = mapped_column(String, default="both")  # mcp|http|both
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    token_prefix: Mapped[str] = mapped_column(String, default="")
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    record_scope: Mapped[dict] = mapped_column(JSON, default=dict)  # {} = all owner records; {"vehicle_ids": [...]} limits
    status: Mapped[str] = mapped_column(String, default="active", index=True)  # active|revoked|expired|paused
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    quota: Mapped[dict] = mapped_column(JSON, default=dict)  # {per_minute: 60, concurrent: 4, per_day: 2000}
    rotated_from_id: Mapped[str | None] = mapped_column(String, nullable=True)
    callback_url: Mapped[str | None] = mapped_column(Text, nullable=True)  # owner-configured only
    callback_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    health: Mapped[dict] = mapped_column(JSON, default=dict)
    # Per-minute rate counter: {"minute": "2026-09-15T12:34", "count": n} (spec §10.8 quotas).
    usage_window: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")


class DelegatedRequest(Base, BusinessRow):
    """One logical request from an external client; retries map to the same row (spec §10.8)."""
    __tablename__ = "delegated_requests"
    __table_args__ = (UniqueConstraint("client_id", "request_key", name="uq_delegated_request"),)
    client_id: Mapped[str] = mapped_column(ForeignKey("external_clients.id"), index=True)
    request_key: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, default="ask")  # ask|reply|find|upload
    message: Mapped[str] = mapped_column(Text, default="")
    entity_refs: Mapped[list] = mapped_column(JSON, default=list)
    asset_ids: Mapped[list] = mapped_column(JSON, default=list)
    mission_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String, default="accepted", index=True)  # accepted|answered|running|waiting|needs_input|done|failed|cancelled|denied
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    correlation_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    causation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


CALLBACK_STATES = ("pending", "sending", "accepted", "failed", "unknown", "cancelled")


class CallbackDelivery(Base, BusinessRow):
    """One attempt-bounded push of a finished delegated request to the owner-configured destination.

    Polling stays the baseline (spec §10.8); this row exists so the optional signed callback is as
    auditable as any other delivery AZKT makes. `dedupe_key` is what makes "exactly one callback per
    terminal transition" a database fact rather than a hope, and `id` travels in the delivery header so
    a retried POST is recognisable as the same delivery instead of a second event.

    `state` is truthful about what the far side actually did:
        pending -> sending -> accepted (2xx)
                           -> failed   (a permanent 4xx, or the retry bound exhausted)
                           -> unknown  (the connection broke after the request was sent: it may have
                                        arrived, so it is never called delivered and never re-sent)
                           -> cancelled (the owner cleared the destination, or the client was revoked)
    """
    __tablename__ = "external_callback_deliveries"
    client_id: Mapped[str] = mapped_column(ForeignKey("external_clients.id"), index=True)
    delegated_request_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    mission_id: Mapped[str | None] = mapped_column(String, nullable=True)
    event: Mapped[str] = mapped_column(String, default="work.completed")  # work.* | callback.test
    request_state: Mapped[str | None] = mapped_column(String, nullable=True)  # done|failed|needs_input|test
    dedupe_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    url: Mapped[str] = mapped_column(Text, default="")        # the destination actually used, for the audit trail
    payload: Mapped[dict] = mapped_column(JSON, default=dict)  # the exact envelope that was signed and sent
    state: Mapped[str] = mapped_column(String, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    # Every attempt, with what actually happened: [{n, at, outcome, status, error, duration_ms, url}]
    attempt_log: Mapped[list] = mapped_column(JSON, default=list)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String, nullable=True)
