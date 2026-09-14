from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, BusinessRow


class SiteProfile(Base, BusinessRow):
    """Versioned website adapter profile (spec §7.2)."""
    __tablename__ = "site_profiles"
    connection_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provider: Mapped[str] = mapped_column(String, default="woocommerce")
    base_url: Mapped[str] = mapped_column(String, default="")
    profile_version: Mapped[int] = mapped_column(Integer, default=1)
    content_type: Mapped[str] = mapped_column(String, default="product")  # product|post|custom
    discovered: Mapped[dict] = mapped_column(JSON, default=dict)  # versions, taxonomy, fields, auth caps
    field_map: Mapped[dict] = mapped_column(JSON, default=dict)
    field_ownership: Mapped[dict] = mapped_column(JSON, default=dict)  # field -> azkt|editor
    media_rules: Mapped[dict] = mapped_column(JSON, default=dict)
    availability_map: Mapped[dict] = mapped_column(JSON, default=dict)
    validation: Mapped[dict] = mapped_column(JSON, default=dict)
    supported_ops: Mapped[list] = mapped_column(JSON, default=list)
    limitations: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String, default="draft", index=True)  # draft|validated|active|drift|superseded
    drift: Mapped[dict] = mapped_column(JSON, default=dict)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ListingPackage(Base, BusinessRow):
    """Canonical versioned listing package (spec §7.3)."""
    __tablename__ = "listing_packages"
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("vehicles.id"), index=True)
    package_version: Mapped[int] = mapped_column(Integer, default=1)
    listing_class: Mapped[str] = mapped_column(String, default="ready_for_sale")  # en_route|ready_for_sale
    headline: Mapped[str] = mapped_column(String, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    short_description: Mapped[str] = mapped_column(Text, default="")
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String, default="USD")
    specs: Mapped[list] = mapped_column(JSON, default=list)  # [{key, value, evidence, status}]
    disclosures: Mapped[list] = mapped_column(JSON, default=list)
    media: Mapped[list] = mapped_column(JSON, default=list)  # ordered asset ids
    availability: Mapped[str] = mapped_column(String, default="available")  # available|reserved|sold|en_route
    profile_id: Mapped[str | None] = mapped_column(String, nullable=True)
    profile_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    package_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    diff: Mapped[dict] = mapped_column(JSON, default=dict)
    readiness: Mapped[list] = mapped_column(JSON, default=list)  # gate results
    status: Mapped[str] = mapped_column(String, default="draft", index=True)  # draft|review|approved|published|superseded|invalidated
    approval_id: Mapped[str | None] = mapped_column(String, nullable=True)
    supersedes_id: Mapped[str | None] = mapped_column(String, nullable=True)


class Publication(Base, BusinessRow):
    __tablename__ = "publications"
    package_id: Mapped[str | None] = mapped_column(ForeignKey("listing_packages.id"), nullable=True, index=True)
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("vehicles.id"), index=True)
    channel: Mapped[str] = mapped_column(String, default="website", index=True)  # website|facebook_marketplace|manual:<name>
    external_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    external_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    desired_state: Mapped[str] = mapped_column(String, default="published")  # published|available|reserved|sold|unpublished
    observed_state: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str] = mapped_column(String, default="queued", index=True)  # queued|accepted|published|verified|pending_verification|mismatch|failed|unsupported|cleanup_pending|unpublished
    external_action_id: Mapped[str | None] = mapped_column(String, nullable=True)
    receipt: Mapped[dict] = mapped_column(JSON, default=dict)
    media_map: Mapped[dict] = mapped_column(JSON, default=dict)  # asset sha → wp media id
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
