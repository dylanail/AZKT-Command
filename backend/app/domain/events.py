"""Outbox dispatch: handlers run in the worker after the producing transaction committed."""
from __future__ import annotations

import fnmatch
import logging
import traceback
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..models.runtime import Event

log = logging.getLogger("azkt.events")
HANDLERS: list[tuple[str, Callable[..., Awaitable]]] = []
MAX_ATTEMPTS = 5
BACKOFF_SECONDS = (30, 120, 600, 1800)   # after attempt 1, 2, 3, 4+


def on_event(pattern: str):
    def deco(fn):
        HANDLERS.append((pattern, fn))
        return fn
    return deco


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=BACKOFF_SECONDS[min(max(attempts, 1), len(BACKOFF_SECONDS)) - 1])


async def dispatch_pending(session_factory, limit: int | None = None) -> int:
    """One pass over the outbox.

    Each handler runs inside its own SAVEPOINT: a handler that raises has *its* writes rolled back
    and the event is retried later with backoff, while the handlers that succeeded keep their
    effect. Half-applied state from a failed handler is never committed.
    """
    n = 0
    async with session_factory() as db:
        now = datetime.now(timezone.utc)
        rows = (await db.execute(
            select(Event)
            .where(Event.processed_at.is_(None),
                   or_(Event.next_attempt_at.is_(None), Event.next_attempt_at <= now))
            .order_by(Event.happened_at, Event.created_at)
            .limit(limit or settings.OUTBOX_BATCH).with_for_update(skip_locked=True)
        )).scalars().all()
        for ev in rows:
            ev_id = ev.id
            errors = []
            for pattern, fn in HANDLERS:
                if not fnmatch.fnmatchcase(ev.type, pattern):
                    continue
                sp = await db.begin_nested()
                try:
                    await fn(db, ev)
                    await sp.commit()
                except Exception as e:  # noqa: BLE001
                    try:
                        await sp.rollback()
                    except Exception:  # noqa: BLE001 - savepoint already gone
                        pass
                    errors.append(f"{fn.__name__}: {type(e).__name__}: {e}")
                    log.exception("event handler %s failed for %s", fn.__name__, ev.type)
            ev = await db.get(Event, ev_id)     # a savepoint rollback may have expired the row
            ev.attempts = (ev.attempts or 0) + 1
            if errors and ev.attempts < MAX_ATTEMPTS:
                ev.error = "; ".join(errors)[:2000]
                ev.next_attempt_at = datetime.now(timezone.utc) + _backoff(ev.attempts)
            else:
                ev.processed_at = datetime.now(timezone.utc)
                ev.error = "; ".join(errors)[:2000] if errors else None
                ev.next_attempt_at = None
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
