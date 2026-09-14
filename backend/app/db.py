from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .core.config import settings

engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


class BusinessRow:
    """Mixin for every business-data row: stable id, organization scope, audit
    timestamps and an optimistic-concurrency version (spec §3.1, §12.1)."""

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(
        String, nullable=False, default=settings.DEFAULT_BUSINESS_ID, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)

    def bump(self, actor_id: str | None = None) -> None:
        self.version = (self.version or 1) + 1
        self.updated_by = actor_id


async def get_db():
    async with SessionLocal() as s:
        yield s


def configure_engine(url: str) -> None:
    """Used by tests/worker to point at a different database before use."""
    global engine, SessionLocal
    engine = create_async_engine(url, pool_pre_ping=True)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
