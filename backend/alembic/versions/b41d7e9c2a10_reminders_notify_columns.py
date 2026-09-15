"""reminders/telegram slice: additive columns on scheduled_deliveries, notifications, telegram_pairings

Revision ID: b41d7e9c2a10
Revises: c7cc54678ed5
Create Date: 2026-09-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "b41d7e9c2a10"
down_revision = "c7cc54678ed5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scheduled_deliveries", sa.Column("provider_ref", sa.String(), nullable=True))
    op.add_column("scheduled_deliveries", sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("scheduled_deliveries", sa.Column("cancel_reason", sa.String(), nullable=True))
    op.create_index("ix_scheduled_deliveries_provider_ref", "scheduled_deliveries", ["provider_ref"])

    op.add_column("notifications", sa.Column("occurrences", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("notifications", sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("notifications", sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"))

    op.add_column("telegram_pairings", sa.Column("token_used_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("telegram_pairings", sa.Column("revoke_reason", sa.String(), nullable=True))
    op.add_column("telegram_pairings", sa.Column("last_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("telegram_pairings", "last_error")
    op.drop_column("telegram_pairings", "revoke_reason")
    op.drop_column("telegram_pairings", "token_used_at")
    op.drop_column("notifications", "payload")
    op.drop_column("notifications", "last_event_at")
    op.drop_column("notifications", "occurrences")
    op.drop_index("ix_scheduled_deliveries_provider_ref", table_name="scheduled_deliveries")
    op.drop_column("scheduled_deliveries", "cancel_reason")
    op.drop_column("scheduled_deliveries", "delivered_at")
    op.drop_column("scheduled_deliveries", "provider_ref")
