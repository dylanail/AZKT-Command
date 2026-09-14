from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, BusinessRow


class Asset(Base, BusinessRow):
    """A stored file (photo/document/audio) with lineage (spec §7.1, §7.4)."""
    __tablename__ = "assets"
    kind: Mapped[str] = mapped_column(String, default="photo", index=True)  # photo|document|audio|other
    storage_key: Mapped[str] = mapped_column(String)
    original_name: Mapped[str | None] = mapped_column(String, nullable=True)
    content_type: Mapped[str] = mapped_column(String, default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    uploaded_by: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, default="upload")  # upload|drive|telegram|mcp|email
    provider_ref: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    provider_revision: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    classification: Mapped[str] = mapped_column(String, default="unknown")  # listing_photo|invoice|id_document|shipping_paper|screenshot|unrelated|unknown|voice_note
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    public_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    pre_arrival: Mapped[bool] = mapped_column(Boolean, default=False)
    derivatives: Mapped[dict] = mapped_column(JSON, default=dict)  # {thumb: key, web: key}
    metadata_stripped: Mapped[bool] = mapped_column(Boolean, default=False)
    exif: Mapped[dict] = mapped_column(JSON, default=dict)
    owner_client_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)  # external client ownership
    visibility: Mapped[str] = mapped_column(String, default="internal")  # internal|owner|public_candidate
    status: Mapped[str] = mapped_column(String, default="ready", index=True)  # pending|ready|failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)  # for audio assets
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)  # model observations for photos


class AssetLink(Base, BusinessRow):
    __tablename__ = "asset_links"
    __table_args__ = (UniqueConstraint("asset_id", "entity_kind", "entity_id", "role", name="uq_asset_link"),)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), index=True)
    entity_kind: Mapped[str] = mapped_column(String, index=True)  # vehicle|task|recon_issue|intake|shipment|cost_item|message|listing_package
    entity_id: Mapped[str] = mapped_column(String, index=True)
    role: Mapped[str] = mapped_column(String, default="photo")  # photo|evidence|source|gallery|document|voice
    slot: Mapped[str | None] = mapped_column(String, nullable=True)  # front_34|interior|rear|...
    position: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    match_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    # added by the assets domain (add-only): links are retired, never deleted (evidence is kept)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    remove_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    linked_by: Mapped[str | None] = mapped_column(String, nullable=True)


class UploadSession(Base, BusinessRow):
    """Bounded, expiring upload slot; finalize validates checksum/type before an asset id exists."""
    __tablename__ = "upload_sessions"
    created_by_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    client_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    purpose: Mapped[str] = mapped_column(String, default="intake")
    intake_id: Mapped[str | None] = mapped_column(String, nullable=True)
    expected_content_type: Mapped[str | None] = mapped_column(String, nullable=True)
    max_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String, default="open", index=True)  # open|received|finalized|expired|failed
    storage_key: Mapped[str | None] = mapped_column(String, nullable=True)
    received_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    asset_id: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # added by the assets domain (add-only)
    original_name: Mapped[str | None] = mapped_column(String, nullable=True)
    detected_content_type: Mapped[str | None] = mapped_column(String, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deduplicated: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_types: Mapped[list] = mapped_column(JSON, default=list)
