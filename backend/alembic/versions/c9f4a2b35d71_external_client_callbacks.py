"""external agent callbacks: signed delivery of finished delegated work

Additive only (spec §10.8).

`external_callback_deliveries` — one row per terminal transition of a delegated request, for clients whose
owner configured a callback destination. Until now `external_clients.callback_url` was written and never
read: the only way a client learned that long work finished was to keep polling. The row exists so the push
half is as auditable as every other delivery AZKT makes — `dedupe_key` is what makes "exactly one callback
per terminal transition" a database fact, `id` is the idempotency key the receiver sees in
`X-AZKT-Delivery`, and `attempt_log` keeps every attempt with its real outcome (accepted / failed with its
status / unknown) so nothing is ever reported as delivered when the result was lost.

No existing column changes meaning and nothing is dropped, so an older deployment keeps working: a client
with no configured destination is simply polling-only, exactly as before.

Revision ID: c9f4a2b35d71
Revises: b8e3f1a24c60
Create Date: 2026-09-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "c9f4a2b35d71"
down_revision = "b8e3f1a24c60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "external_callback_deliveries",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("business_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("client_id", sa.String(), sa.ForeignKey("external_clients.id"), nullable=False),
        sa.Column("delegated_request_id", sa.String(), nullable=True),
        sa.Column("mission_id", sa.String(), nullable=True),
        sa.Column("event", sa.String(), nullable=False, server_default="work.completed"),
        sa.Column("request_state", sa.String(), nullable=True),
        sa.Column("dedupe_key", sa.String(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("state", sa.String(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_log", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("lease_token", sa.String(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.String(), nullable=True),
    )
    op.create_index(op.f("ix_external_callback_deliveries_business_id"),
                    "external_callback_deliveries", ["business_id"])
    op.create_index(op.f("ix_external_callback_deliveries_client_id"),
                    "external_callback_deliveries", ["client_id"])
    op.create_index(op.f("ix_external_callback_deliveries_delegated_request_id"),
                    "external_callback_deliveries", ["delegated_request_id"])
    op.create_index(op.f("ix_external_callback_deliveries_state"), "external_callback_deliveries", ["state"])
    op.create_index(op.f("ix_external_callback_deliveries_next_attempt_at"),
                    "external_callback_deliveries", ["next_attempt_at"])
    # The uniqueness is the guarantee, not an optimisation: two workers reaching the same terminal
    # transition at once must produce one delivery, not two callbacks for one event.
    op.create_index(op.f("ix_external_callback_deliveries_dedupe_key"), "external_callback_deliveries",
                    ["dedupe_key"], unique=True)


def downgrade() -> None:
    op.drop_table("external_callback_deliveries")
