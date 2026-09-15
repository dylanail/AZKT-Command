from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint
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
    # added by the integrations domain (add-only): validation on staging, listing gates, drift pause
    staging_url: Mapped[str | None] = mapped_column(Text, nullable=True)      # preview target; never the live site
    preview: Mapped[dict] = mapped_column(JSON, default=dict)                  # last rendered preview (no write)
    listing_gates: Mapped[dict] = mapped_column(JSON, default=dict)            # {listing_class: [{requirement, label, param}]}
    writes_paused: Mapped[bool] = mapped_column(Boolean, default=False)        # drift pauses writes for this channel (F08)
    pause_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    drift_detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_by: Mapped[str | None] = mapped_column(String, nullable=True)
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # How the site's SKU column is treated. The live catalogue numbers its products its own way, so
    # `preserve` (the default) never writes that column and identifies listings by `azkt_vehicle_id`.
    sku_strategy: Mapped[str] = mapped_column(String, default="preserve")   # preserve|stock_no|prefix
    sku_prefix: Mapped[str | None] = mapped_column(String, nullable=True)
    # Which shop categories a published vehicle belongs to, and which recorded specs become product
    # attributes. Empty means AZKT sends neither key, so an editor's own values are never wiped.
    category_ids: Mapped[list] = mapped_column(JSON, default=list)          # WooCommerce category ids
    attribute_map: Mapped[dict] = mapped_column(JSON, default=dict)         # spec key -> {id|name, visible}


class SiteMedia(Base, BusinessRow):
    """One image AZKT uploaded into the site's media library, addressed by its checksum.

    The map lives per site (`site_key` is the site's base URL), not per publication: republishing a
    vehicle, rebuilding a package, or publishing a second vehicle that shares a photo all reuse the
    upload instead of filling the library with duplicates.
    """
    __tablename__ = "site_media"
    __table_args__ = (UniqueConstraint("site_key", "sha256", name="uq_site_media"),)
    site_key: Mapped[str] = mapped_column(String, index=True)
    sha256: Mapped[str] = mapped_column(String, index=True)
    media_id: Mapped[str] = mapped_column(String)
    asset_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    filename: Mapped[str | None] = mapped_column(String, nullable=True)
    alt: Mapped[str | None] = mapped_column(Text, nullable=True)
    bytes_len: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    missing: Mapped[bool] = mapped_column(Boolean, default=False)   # the site no longer has this id


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
    # added by the integrations domain (add-only): evidence, readiness outcome and copy provenance
    channel: Mapped[str] = mapped_column(String, default="website", index=True)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)          # {price, specs, disclosures, eta, media}
    media_detail: Mapped[list] = mapped_column(JSON, default=list)      # [{asset_id, sha256, slot, position, pre_arrival}]
    generated_by: Mapped[str] = mapped_column(String, default="template")  # template|model
    blocked_reasons: Mapped[list] = mapped_column(JSON, default=list)   # unmet or unconfigured gates
    ready: Mapped[bool] = mapped_column(Boolean, default=False)
    built_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Publication(Base, BusinessRow):
    __tablename__ = "publications"
    package_id: Mapped[str | None] = mapped_column(ForeignKey("listing_packages.id"), nullable=True, index=True)
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("vehicles.id"), index=True)
    channel: Mapped[str] = mapped_column(String, default="website", index=True)  # website|facebook_marketplace|manual:<name>
    external_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    external_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    desired_state: Mapped[str] = mapped_column(String, default="published")  # published|available|reserved|sold|unpublished
    observed_state: Mapped[str | None] = mapped_column(String, nullable=True)
    # queued|accepted|published|verified|pending_verification|mismatch|needs_review|unknown|failed|
    # unsupported|cleanup_pending|unpublished  — `unknown` is a lost result (never a claim that the
    # site was written), `needs_review` waits for a person to confirm an existing site listing.
    state: Mapped[str] = mapped_column(String, default="queued", index=True)
    external_action_id: Mapped[str | None] = mapped_column(String, nullable=True)
    receipt: Mapped[dict] = mapped_column(JSON, default=dict)
    media_map: Mapped[dict] = mapped_column(JSON, default=dict)  # asset sha → wp media id
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # added by the integrations domain (add-only): binding, verification and cleanup bookkeeping
    profile_id: Mapped[str | None] = mapped_column(String, nullable=True)
    profile_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    package_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    package_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    verification: Mapped[dict] = mapped_column(JSON, default=dict)      # {checked_at, fields:{field:{expected,observed,ok}}}
    history: Mapped[list] = mapped_column(JSON, default=list)           # [{state, at, detail}]
    unsupported_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cleanup_required: Mapped[bool] = mapped_column(Boolean, default=False)   # stays true until verified (F10)
    error_kind: Mapped[str | None] = mapped_column(String, nullable=True)    # typed adapter error kind


class DriveFile(Base, BusinessRow):
    """Index of one file or folder inside the selected importer root (spec §7.1).

    Row number/name are never an identity: the Drive file id is. `in_root` records the ancestry check
    after a move, `retrieval_eligible` is cleared when the file leaves the root or access is revoked,
    and `import_status` keeps interrupted media processing recoverable (F03).
    """
    __tablename__ = "drive_files"
    __table_args__ = (UniqueConstraint("connection_id", "file_id", name="uq_drive_file"),)
    connection_id: Mapped[str] = mapped_column(String, index=True)
    file_id: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String, default="")
    mime_type: Mapped[str] = mapped_column(String, default="")
    is_folder: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    parent_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    path: Mapped[str] = mapped_column(Text, default="")
    owner_email: Mapped[str | None] = mapped_column(String, nullable=True)
    modified_time: Mapped[str | None] = mapped_column(String, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String, nullable=True, index=True)   # Drive md5Checksum
    revision: Mapped[str | None] = mapped_column(String, nullable=True)               # Drive file version
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    web_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    in_root: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    removed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    retrieval_eligible: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    ineligible_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    vehicle_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    match_state: Mapped[str] = mapped_column(String, default="unmatched", index=True)  # matched|proposed|ambiguous|unmatched|confirmed|corrected|left
    match_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    confirmed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    identity_signature: Mapped[str | None] = mapped_column(String, nullable=True)  # recheck when contents change
    asset_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    classification: Mapped[str | None] = mapped_column(String, nullable=True)
    import_status: Mapped[str] = mapped_column(String, default="pending", index=True)  # pending|imported|failed|skipped|deduplicated
    import_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    import_attempts: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
