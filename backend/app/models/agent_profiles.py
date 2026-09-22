from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, BusinessRow


class AgentProfile(Base, BusinessRow):
    """The editable, business-facing instructions for one runtime role.

    The invariant/safety prompt and command permissions do not live here. A profile can shape how a
    role works, but it can never create a tool or widen authority.
    """

    __tablename__ = "agent_profiles"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (UniqueConstraint("business_id", "role", name="uq_agent_profile_role"),)

    role: Mapped[str] = mapped_column(String, index=True)
    label: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="draft", index=True)  # draft|active
    current_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    change_history: Mapped[list] = mapped_column(JSON, default=list)


class AgentProfileVersion(Base, BusinessRow):
    __tablename__ = "agent_profile_versions"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (UniqueConstraint("profile_id", "version_no", name="uq_agent_profile_version"),)

    profile_id: Mapped[str] = mapped_column(ForeignKey("agent_profiles.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String, index=True)
    stage: Mapped[str] = mapped_column(String, default="draft", index=True)  # draft|validated|active|superseded
    validation: Mapped[dict] = mapped_column(JSON, default=dict)
    source_kind: Mapped[str] = mapped_column(String, default="manual")  # manual|manager|openclaw_import
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    source_manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    change_note: Mapped[str] = mapped_column(Text, default="")
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_by: Mapped[str | None] = mapped_column(String, nullable=True)
