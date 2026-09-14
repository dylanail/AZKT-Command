from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, BusinessRow


class MetricSnapshot(Base, BusinessRow):
    """Cached, rebuildable reporting output (spec §2.4)."""
    __tablename__ = "metric_snapshots"
    key: Mapped[str] = mapped_column(String, index=True)  # home_metrics:<period>:<hash>
    period_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    timezone: Mapped[str] = mapped_column(String, default="America/Phoenix")
    cohort: Mapped[dict] = mapped_column(JSON, default=dict)
    values: Mapped[dict] = mapped_column(JSON, default=dict)
    contributing_ids: Mapped[dict] = mapped_column(JSON, default=dict)
    computation_version: Mapped[int] = mapped_column(Integer, default=1)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    coverage: Mapped[dict] = mapped_column(JSON, default=dict)
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
