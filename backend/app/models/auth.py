from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, _uuid

ROLES = ("owner", "manager", "mechanic", "sales", "logistics", "books")


class User(Base):
    """A person with access. Roles/perms per spec §11.1; per-person overrides in `perms`."""
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    handle: Mapped[str] = mapped_column(String, unique=True)
    display_name: Mapped[str] = mapped_column(String, default="")
    email: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(String, default="owner", index=True)
    status: Mapped[str] = mapped_column(String, default="active", index=True)  # active|invited|disabled
    scope: Mapped[str] = mapped_column(String, default="all")  # all|assigned
    manager_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    perms: Mapped[dict] = mapped_column(JSON, default=dict)  # per-person overrides {perm_key: bool}
    timezone: Mapped[str] = mapped_column(String, default="America/Phoenix")
    reminder_email: Mapped[str | None] = mapped_column(String, nullable=True)
    reminder_email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notification_prefs: Mapped[dict] = mapped_column(JSON, default=dict)
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ── access administration (spec §3.1 Person / §11.1) ─────────────────
    version: Mapped[int] = mapped_column(Integer, default=1)  # optimistic concurrency for team commands
    grants: Mapped[list] = mapped_column(JSON, default=list)  # [{perm, granted, by, at, note}] explicit grant history
    access_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    access_changed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    disabled_by: Mapped[str | None] = mapped_column(String, nullable=True)
    disabled_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    invited_by: Mapped[str | None] = mapped_column(String, nullable=True)
    invitation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)

    def bump(self, actor_id: str | None = None) -> None:
        """Same contract as BusinessRow.bump so CommandContext.touch() works on people."""
        self.version = (self.version or 1) + 1
        self.updated_by = actor_id


class Credential(Base):
    """A registered WebAuthn passkey (Face ID / platform authenticator)."""
    __tablename__ = "credentials"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    credential_id: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    transports: Mapped[str] = mapped_column(String, default="")
    label: Mapped[str] = mapped_column(String, default="passkey")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Invitation(Base):
    __tablename__ = "invitations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    display_name: Mapped[str] = mapped_column(String, default="")
    role: Mapped[str] = mapped_column(String)
    scope: Mapped[str] = mapped_column(String, default="assigned")
    manager_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    perms: Mapped[dict] = mapped_column(JSON, default=dict)
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    invited_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending|accepted|revoked|expired
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    note: Mapped[str] = mapped_column(Text, default="")
    is_active_flag: Mapped[bool] = mapped_column(Boolean, default=True)
    revoked_by: Mapped[str | None] = mapped_column(String, nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)  # retry-safe invites
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)

    def bump(self, actor_id: str | None = None) -> None:
        self.version = (self.version or 1) + 1
        self.updated_by = actor_id
