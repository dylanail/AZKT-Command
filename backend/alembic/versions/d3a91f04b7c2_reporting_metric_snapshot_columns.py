"""reporting slice: additive columns on metric_snapshots

Revision ID: d3a91f04b7c2
Revises: b1d4f7a20c31
Create Date: 2026-09-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "d3a91f04b7c2"
down_revision = "b1d4f7a20c31"  # chained after the inbox slice so the history stays a single head
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("metric_snapshots", sa.Column("period_kind", sa.String(), nullable=True))
    op.add_column("metric_snapshots", sa.Column("cohort_hash", sa.String(), nullable=True))
    op.add_column("metric_snapshots", sa.Column("restatements", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("metric_snapshots", sa.Column("last_error", sa.String(), nullable=True))
    op.create_index("ix_metric_snapshots_cohort_hash", "metric_snapshots", ["cohort_hash"])


def downgrade() -> None:
    op.drop_index("ix_metric_snapshots_cohort_hash", table_name="metric_snapshots")
    op.drop_column("metric_snapshots", "last_error")
    op.drop_column("metric_snapshots", "restatements")
    op.drop_column("metric_snapshots", "cohort_hash")
    op.drop_column("metric_snapshots", "period_kind")
