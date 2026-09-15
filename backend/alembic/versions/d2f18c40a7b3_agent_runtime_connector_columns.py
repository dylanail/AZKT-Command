"""agent runtime / external connector slice: additive columns on runs and external_clients

Revision ID: d2f18c40a7b3
Revises: b41d7e9c2a10
Create Date: 2026-09-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "d2f18c40a7b3"
down_revision = "b41d7e9c2a10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # runs: monotonic fencing token + owning worker, so a stale lease holder cannot commit (H01, invariant 2)
    op.add_column("runs", sa.Column("fencing_token", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("runs", sa.Column("worker_id", sa.String(), nullable=True))

    # external_clients: per-minute quota counter {"minute": "...", "count": n} (spec §10.8)
    op.add_column("external_clients", sa.Column("usage_window", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("external_clients", "usage_window")
    op.drop_column("runs", "worker_id")
    op.drop_column("runs", "fencing_token")
