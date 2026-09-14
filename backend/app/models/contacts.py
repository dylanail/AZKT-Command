from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, BusinessRow


class Contact(Base, BusinessRow):
    """A person or company. One contact can hold many roles and opportunities (spec §3.1)."""
    __tablename__ = "contacts"
    __mapper_args__ = {"eager_defaults": True}  # fetch server-generated updated_at via RETURNING (async-safe)
    name: Mapped[str] = mapped_column(String, default="", index=True)
    company: Mapped[str | None] = mapped_column(String, nullable=True)
    roles: Mapped[list] = mapped_column(JSON, default=list)  # buyer|vendor|exporter|importer|carrier|dispatcher|port|other
    primary_email: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    primary_phone: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String, default="active", index=True)  # active|provisional|merged|archived
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    consent: Mapped[dict] = mapped_column(JSON, default=dict)  # {email: bool, sms: bool, opted_out_at}
    source: Mapped[str] = mapped_column(String, default="manual")
    notes: Mapped[str] = mapped_column(Text, default="")
    merged_into_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    search_text: Mapped[str] = mapped_column(Text, default="")
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    legacy_customer_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # added by the contacts domain (add-only): aliases kept after merges, provenance, lifecycle stamps
    aliases: Mapped[list] = mapped_column(JSON, default=list)  # [{name, company, from_contact_id}]
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True, index=True)  # ingestion dedupe key
    provisional_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ContactIdentity(Base, BusinessRow):
    """An email/phone/handle alias. Raw value preserved; normalized form for matching (spec §3.3)."""
    __tablename__ = "contact_identities"
    __mapper_args__ = {"eager_defaults": True}  # fetch server-generated updated_at via RETURNING (async-safe)
    __table_args__ = (UniqueConstraint("kind", "value_norm", "contact_id", name="uq_identity_per_contact"),)
    contact_id: Mapped[str] = mapped_column(ForeignKey("contacts.id"), index=True)
    kind: Mapped[str] = mapped_column(String)  # email|phone|telegram|instagram|other
    value_raw: Mapped[str] = mapped_column(String)
    value_norm: Mapped[str] = mapped_column(String, index=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String, default="manual")
    label: Mapped[str] = mapped_column(String, default="")
    country: Mapped[str | None] = mapped_column(String, nullable=True)  # phones: ISO country used for E.164
    verified_by: Mapped[str | None] = mapped_column(String, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ContactMerge(Base, BusinessRow):
    """Audited, undoable merge history (spec §3.3)."""
    __tablename__ = "contact_merges"
    __mapper_args__ = {"eager_defaults": True}  # fetch server-generated updated_at via RETURNING (async-safe)
    survivor_id: Mapped[str] = mapped_column(ForeignKey("contacts.id"), index=True)
    merged_id: Mapped[str] = mapped_column(ForeignKey("contacts.id"), index=True)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)  # merged contact + relinked ids
    reason: Mapped[str] = mapped_column(Text, default="")
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reverted_by: Mapped[str | None] = mapped_column(String, nullable=True)
    approval_id: Mapped[str | None] = mapped_column(String, nullable=True)
    merged_by: Mapped[str | None] = mapped_column(String, nullable=True)
