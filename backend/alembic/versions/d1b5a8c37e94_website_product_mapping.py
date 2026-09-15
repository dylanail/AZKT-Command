"""website product mapping: shop categories and product attributes

Additive only (spec §7.2). A WooCommerce product created with no category never appears on a shop
category page, and specs kept only in AZKT meta are invisible to the theme. Both are now mapped on the
site profile, from taxonomy that discovery actually found on the site.

Empty is the default and a valid answer: AZKT then sends neither field, so updating a product the shop
already categorised never wipes that work.

Revision ID: d1b5a8c37e94
Revises: a7d1c4e90b52
Create Date: 2026-09-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "d1b5a8c37e94"
down_revision = "a7d1c4e90b52"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("site_profiles", sa.Column("category_ids", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("site_profiles", sa.Column("attribute_map", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("site_profiles", "attribute_map")
    op.drop_column("site_profiles", "category_ids")
