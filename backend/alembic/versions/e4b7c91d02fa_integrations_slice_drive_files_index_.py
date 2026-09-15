"""integrations slice: drive_files index, site profile / listing package / publication columns

Additive only (spec §12.1): one new table for the importer Drive index and new columns on the
listing tables. Every non-nullable column carries a server default so the upgrade also applies to a
database that already holds rows; nothing is renamed or dropped.

Revision ID: e4b7c91d02fa
Revises: d3a91f04b7c2
Create Date: 2026-09-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "e4b7c91d02fa"
down_revision = "d3a91f04b7c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── importer Drive index (spec §7.1): identity is the Drive file id, never the name or position ──
    op.create_table(
        "drive_files",
        sa.Column("connection_id", sa.String(), nullable=False),
        sa.Column("file_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False, server_default=""),
        sa.Column("mime_type", sa.String(), nullable=False, server_default=""),
        sa.Column("is_folder", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("parent_id", sa.String(), nullable=True),
        sa.Column("path", sa.Text(), nullable=False, server_default=""),
        sa.Column("owner_email", sa.String(), nullable=True),
        sa.Column("modified_time", sa.String(), nullable=True),
        sa.Column("checksum", sa.String(), nullable=True),
        sa.Column("revision", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("web_link", sa.Text(), nullable=True),
        sa.Column("in_root", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("removed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("retrieval_eligible", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("ineligible_reason", sa.Text(), nullable=True),
        sa.Column("vehicle_id", sa.String(), nullable=True),
        sa.Column("match_state", sa.String(), nullable=False, server_default="unmatched"),
        sa.Column("match_evidence", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("confirmed_by", sa.String(), nullable=True),
        sa.Column("identity_signature", sa.String(), nullable=True),
        sa.Column("asset_id", sa.String(), nullable=True),
        sa.Column("classification", sa.String(), nullable=True),
        sa.Column("import_status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("import_error", sa.Text(), nullable=True),
        sa.Column("import_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("business_id", sa.String(), nullable=False, server_default="AZKT"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "file_id", name="uq_drive_file"),
    )
    for col in ("asset_id", "business_id", "checksum", "connection_id", "file_id", "import_status", "in_root",
                "is_folder", "match_state", "parent_id", "removed", "retrieval_eligible", "vehicle_id"):
        op.create_index(op.f(f"ix_drive_files_{col}"), "drive_files", [col], unique=False)

    # ── listing packages: evidence, readiness outcome and copy provenance (spec §7.3) ──
    op.add_column("listing_packages", sa.Column("channel", sa.String(), nullable=False, server_default="website"))
    op.add_column("listing_packages", sa.Column("evidence", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("listing_packages", sa.Column("media_detail", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("listing_packages", sa.Column("generated_by", sa.String(), nullable=False, server_default="template"))
    op.add_column("listing_packages", sa.Column("blocked_reasons", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("listing_packages", sa.Column("ready", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("listing_packages", sa.Column("built_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_listing_packages_channel"), "listing_packages", ["channel"], unique=False)

    # ── publications: approval binding, verification and cleanup bookkeeping (F06–F10) ──
    op.add_column("publications", sa.Column("profile_id", sa.String(), nullable=True))
    op.add_column("publications", sa.Column("profile_version", sa.Integer(), nullable=True))
    op.add_column("publications", sa.Column("package_version", sa.Integer(), nullable=True))
    op.add_column("publications", sa.Column("package_hash", sa.String(), nullable=True))
    op.add_column("publications", sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("publications", sa.Column("verification", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("publications", sa.Column("history", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("publications", sa.Column("unsupported_reason", sa.Text(), nullable=True))
    op.add_column("publications", sa.Column("cleanup_required", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("publications", sa.Column("error_kind", sa.String(), nullable=True))

    # ── site profiles: staging preview, listing gates and the drift write pause (F04, F08) ──
    op.add_column("site_profiles", sa.Column("staging_url", sa.Text(), nullable=True))
    op.add_column("site_profiles", sa.Column("preview", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("site_profiles", sa.Column("listing_gates", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("site_profiles", sa.Column("writes_paused", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("site_profiles", sa.Column("pause_reason", sa.Text(), nullable=True))
    op.add_column("site_profiles", sa.Column("drift_detected_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("site_profiles", sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("site_profiles", sa.Column("activated_by", sa.String(), nullable=True))
    op.add_column("site_profiles", sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for col in ("discovered_at", "activated_by", "activated_at", "drift_detected_at", "pause_reason",
                "writes_paused", "listing_gates", "preview", "staging_url"):
        op.drop_column("site_profiles", col)
    for col in ("error_kind", "cleanup_required", "unsupported_reason", "history", "verification", "attempts",
                "package_hash", "package_version", "profile_version", "profile_id"):
        op.drop_column("publications", col)
    op.drop_index(op.f("ix_listing_packages_channel"), table_name="listing_packages")
    for col in ("built_at", "ready", "blocked_reasons", "generated_by", "media_detail", "evidence", "channel"):
        op.drop_column("listing_packages", col)
    for col in ("vehicle_id", "retrieval_eligible", "removed", "parent_id", "match_state", "is_folder", "in_root",
                "import_status", "file_id", "connection_id", "checksum", "business_id", "asset_id"):
        op.drop_index(op.f(f"ix_drive_files_{col}"), table_name="drive_files")
    op.drop_table("drive_files")
