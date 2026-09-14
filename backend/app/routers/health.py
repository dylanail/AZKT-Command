"""System health for Settings → Recovery and the runbook: dependency-aware readiness, worker
heartbeat, queue depth, outbox lag, unknown external actions, provider freshness, model budget."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters import model as model_adapter
from ..auth.deps import current_actor
from ..core.config import settings
from ..db import get_db
from ..domain.actors import Actor
from ..models.legacy import SyncState
from ..models.notify import ScheduledDelivery
from ..models.runtime import Event, ExternalAction, Job
from ..services import connections as conn_svc

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("")
async def health(actor: Actor = Depends(current_actor), db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    hb = (await db.execute(select(SyncState).where(SyncState.key == "worker_heartbeat"))).scalar_one_or_none()
    hb_at = hb.updated_at if hb else None
    worker_ok = bool(hb_at and (now - hb_at).total_seconds() < max(30, settings.WORKER_POLL_SECONDS * 10))
    jobs = {s: int(c) for s, c in (await db.execute(text("select state, count(*) from jobs group by state"))).all()}
    oldest_queued = await db.scalar(select(func.min(Job.run_at)).where(Job.state == "queued", Job.run_at <= now))
    outbox_pending = await db.scalar(select(func.count()).select_from(Event).where(Event.processed_at.is_(None)))
    oldest_event = await db.scalar(select(func.min(Event.happened_at)).where(Event.processed_at.is_(None)))
    unknown = await db.scalar(select(func.count()).select_from(ExternalAction).where(ExternalAction.state == "unknown"))
    failed = await db.scalar(select(func.count()).select_from(ExternalAction).where(ExternalAction.state == "failed"))
    late = await db.scalar(select(func.count()).select_from(ScheduledDelivery).where(
        ScheduledDelivery.state == "scheduled", ScheduledDelivery.deliver_at < now))
    conns = await conn_svc.overview(db)
    if not actor.perms.get("connections"):
        conns = [{"provider": c["provider"], "label": c["label"], "freshness": c["freshness"]} for c in conns]
    out = {
        "ok": True, "environment": settings.ENV, "as_of": now.isoformat(),
        "database": {"ok": True},
        "worker": {"ok": worker_ok, "last_heartbeat_at": hb_at.isoformat() if hb_at else None,
                   "detail": (hb.value if hb else {})},
        "jobs": {"by_state": jobs, "lag_seconds": int((now - oldest_queued).total_seconds()) if oldest_queued else 0},
        "outbox": {"pending": int(outbox_pending or 0), "lag_seconds": int((now - oldest_event).total_seconds()) if oldest_event else 0},
        "external_actions": {"unknown": int(unknown or 0), "failed": int(failed or 0)},
        "reminders": {"overdue_deliveries": int(late or 0)},
        "connections": conns,
        "model": await model_adapter.budget_state(db) if actor.perms.get("settings") else {"available": model_adapter.available()},
        "storage": {"backend": settings.STORAGE_BACKEND},
    }
    out["ok"] = worker_ok and int(unknown or 0) == 0
    return out
