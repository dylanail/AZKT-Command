"""versioned, editable agent profiles

Revision ID: e9a4c2d71f06
Revises: a3c8e51f7d24
Create Date: 2026-09-19
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "e9a4c2d71f06"
down_revision = "a3c8e51f7d24"
branch_labels = None
depends_on = None


def _business_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("business_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "agent_profiles",
        *_business_columns(),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="draft"),
        sa.Column("current_version_id", sa.String(), nullable=True),
        sa.Column("change_history", sa.JSON(), nullable=False, server_default="[]"),
        sa.UniqueConstraint("business_id", "role", name="uq_agent_profile_role"),
    )
    op.create_index(op.f("ix_agent_profiles_business_id"), "agent_profiles", ["business_id"])
    op.create_index(op.f("ix_agent_profiles_role"), "agent_profiles", ["role"])
    op.create_index(op.f("ix_agent_profiles_status"), "agent_profiles", ["status"])

    op.create_table(
        "agent_profile_versions",
        *_business_columns(),
        sa.Column("profile_id", sa.String(), sa.ForeignKey("agent_profiles.id"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("content", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("stage", sa.String(), nullable=False, server_default="draft"),
        sa.Column("validation", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("source_kind", sa.String(), nullable=False, server_default="manual"),
        sa.Column("source_ref", sa.String(), nullable=True),
        sa.Column("source_manifest", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("change_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_by", sa.String(), nullable=True),
        sa.UniqueConstraint("profile_id", "version_no", name="uq_agent_profile_version"),
    )
    op.create_index(op.f("ix_agent_profile_versions_business_id"), "agent_profile_versions", ["business_id"])
    op.create_index(op.f("ix_agent_profile_versions_profile_id"), "agent_profile_versions", ["profile_id"])
    op.create_index(op.f("ix_agent_profile_versions_content_hash"), "agent_profile_versions", ["content_hash"])
    op.create_index(op.f("ix_agent_profile_versions_stage"), "agent_profile_versions", ["stage"])
    op.add_column("missions", sa.Column("agent_profile_version", sa.String(), nullable=True))
    op.create_index(op.f("ix_missions_agent_profile_version"), "missions", ["agent_profile_version"])


def downgrade() -> None:
    op.drop_index(op.f("ix_missions_agent_profile_version"), table_name="missions")
    op.drop_column("missions", "agent_profile_version")
    op.drop_table("agent_profile_versions")
    op.drop_table("agent_profiles")
