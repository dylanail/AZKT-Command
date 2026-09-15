"""website media library ids and the SKU strategy

Additive only (spec §7.2, §7.3):

* `site_media` — one row per image AZKT uploaded into the site's WordPress media library, addressed by
  the asset checksum. Listing photos used to be sent to WooCommerce as `/api/assets/...` URLs, which a
  live site can never fetch (the dashboard requires a signed-in session), so a published product had no
  images. Photos are now uploaded once and referenced by media id; this table is what makes the upload
  reusable across packages, publications and vehicles that share a photo.
* `site_profiles.sku_strategy` / `sku_prefix` — the installed shop numbers its own products, so the
  default `preserve` never writes that column and identifies AZKT listings by the `azkt_vehicle_id`
  meta instead. Existing rows keep the old behaviour explicitly (`stock_no`) rather than silently
  changing what a live site would be written with.

Revision ID: a7d1c4e90b52
Revises: f2a5c81e34b9
Create Date: 2026-09-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "a7d1c4e90b52"
down_revision = "f2a5c81e34b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("site_profiles", sa.Column("sku_strategy", sa.String(), nullable=False,
                                             server_default="preserve"))
    op.add_column("site_profiles", sa.Column("sku_prefix", sa.String(), nullable=True))
    # a profile that already exists was written under the old rule, where AZKT owned the SKU column
    op.execute("UPDATE site_profiles SET sku_strategy = 'stock_no'")

    op.create_table(
        "site_media",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("business_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("site_key", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("media_id", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("filename", sa.String(), nullable=True),
        sa.Column("alt", sa.Text(), nullable=True),
        sa.Column("bytes_len", sa.Integer(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("missing", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("site_key", "sha256", name="uq_site_media"),
    )
    op.create_index(op.f("ix_site_media_business_id"), "site_media", ["business_id"])
    op.create_index(op.f("ix_site_media_site_key"), "site_media", ["site_key"])
    op.create_index(op.f("ix_site_media_sha256"), "site_media", ["sha256"])
    op.create_index(op.f("ix_site_media_asset_id"), "site_media", ["asset_id"])


def downgrade() -> None:
    op.drop_table("site_media")
    op.drop_column("site_profiles", "sku_prefix")
    op.drop_column("site_profiles", "sku_strategy")
