"""device enrollment links and passkey last-used

Additive only (spec §11.1 — passkeys only):

* `device_enrollments` — a one-time, short-lived link a signed-in person mints for a second device.
  A passkey cannot leave the device that made it, so a phone has to register its own; until now the
  only way to reach a new device was an owner-issued invitation, which creates a *person*. Only the
  token hash is stored, exactly like `invitations`.
* `credentials.last_used_at` — with several passkeys on one account, "which one is this?" needs a
  last-used stamp; `sign_count` alone is meaningless to a person deciding what to revoke.

Revision ID: a3c8e51f7d24
Revises: c9f4a2b35d71
Create Date: 2026-09-17
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "a3c8e51f7d24"
down_revision = "c9f4a2b35d71"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_enrollments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("credential_id", sa.String(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(), nullable=True),
    )
    op.create_index("ix_device_enrollments_user_id", "device_enrollments", ["user_id"])
    op.create_index("ix_device_enrollments_status", "device_enrollments", ["status"])
    op.create_index("ix_device_enrollments_token_hash", "device_enrollments", ["token_hash"], unique=True)
    op.add_column("credentials", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("credentials", "last_used_at")
    op.drop_index("ix_device_enrollments_token_hash", table_name="device_enrollments")
    op.drop_index("ix_device_enrollments_status", table_name="device_enrollments")
    op.drop_index("ix_device_enrollments_user_id", table_name="device_enrollments")
    op.drop_table("device_enrollments")
