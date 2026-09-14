"""Outbox dispatch: handlers run in the worker after the producing transaction committed."""
from __future__ import annotations

import fnmatch
import logging
import traceback
from datetime import datetime, timezone
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..models.runtime import Event

log = logging.getLogger("azkt.events")
HANDLERS: list[tuple[str, Callable[..., Awaitable]]] = []


def on_event(pattern: str):
    def deco(fn):
        HANDLERS.append((pattern, fn))
        return fn
    return deco


async def dispatch_pending(session_factory, limit: int | None = None) -> int:
    n = 0
    async with session_factory() as db:
        rows = (await db.execute(
            select(Event).where(Event.processed_at.is_(None)).order_by(Event.happened_at, Event.created_at)
            .limit(limit or settings.OUTBOX_BATCH).with_for_update(skip_locked=True)
        )).scalars().all()
        for ev in rows:
            errors = []
            for pattern, fn in HANDLERS:
                if fnmatch.fnmatchcase(ev.type, pattern):
                    try:
                        await fn(db, ev)
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"{fn.__name__}: {type(e).__name__}: {e}")
                        log.exception("event handler %s failed for %s", fn.__name__, ev.type)
            ev.attempts = (ev.attempts or 0) + 1
            if errors and ev.attempts < 5:
                ev.error = "; ".join(errors)[:2000]
            else:
                ev.processed_at = datetime.now(timezone.utc)
                ev.error = "; ".join(errors)[:2000] if errors else None
            n += 1
        await db.commit()
    return n


async def record_provider_event(db: AsyncSession, provider: str, connection_key: str, provider_event_id: str,
                                payload: dict, event_type: str | None = None, signature_ok: bool = True):
    """Store an inbound provider event durably; returns (row, is_new)."""
    from sqlalchemy.exc import IntegrityError
    from ..models.comms import ProviderEvent
    row = ProviderEvent(provider=provider, connection_key=connection_key or "", provider_event_id=provider_event_id,
                        payload=payload, event_type=event_type, received_at=datetime.now(timezone.utc),
                        signature_ok=signature_ok)
    db.add(row)
    try:
        await db.flush()
        return row, True
    except IntegrityError:
        await db.rollback()
        existing = (await db.execute(select(ProviderEvent).where(
            ProviderEvent.provider == provider, ProviderEvent.connection_key == (connection_key or ""),
            ProviderEvent.provider_event_id == provider_event_id))).scalar_one()
        return existing, False
