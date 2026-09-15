"""Home API (spec §2.2, §2.4). Read-only aggregate over canonical records.

Every endpoint is a GET with no side effect at all (invariant 11, ARCHITECTURE rule 10): these reads write no row,
not even the rebuildable `metric_snapshots` cache — that is written only by the `reporting.recompute` command and
the worker sweep. There is no model call on this path either, so a model outage never hides the deterministic
approval, task and blocker lists (K05, H11).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import current_actor
from ..core.errors import Denied, ValidationFailed
from ..core.time import PHOENIX
from ..db import get_db
from ..domain.actors import Actor
from ..domain.policy import has_perm
from ..services import home as home_svc
from ..services import reporting as rep
from ..services import timeline as tl

router = APIRouter(prefix="/api/home", tags=["home"])


def _day(value: str | None, label: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        raise HTTPException(422, f"{label} must be YYYY-MM-DD")


def _instant(value: str | None, label: str = "as_of") -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, f"{label} must be an ISO timestamp")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@router.get("")
async def get_home(period: str = Query("month"), start: str | None = Query(None), end: str | None = Query(None),
                   tz: str = Query(PHOENIX), horizon_days: int = Query(7, ge=1, le=365),
                   actor: Actor = Depends(current_actor), db: AsyncSession = Depends(get_db)):
    """The whole page: status, business overview, needs decision, needs attention, today, timeline,
    in progress, completed. Sections the person may not see come back as available:false with a reason."""
    try:
        return await home_svc.home(db, actor, period=period, start=_day(start, "start"), end=_day(end, "end"),
                                   tz=tz, horizon_days=horizon_days)
    except ValidationFailed as e:
        raise HTTPException(422, e.message)


@router.get("/metrics")
async def get_metrics(period: str = Query("month"), start: str | None = Query(None), end: str | None = Query(None),
                      tz: str = Query(PHOENIX), as_of: str | None = Query(None),
                      actor: Actor = Depends(current_actor), db: AsyncSession = Depends(get_db)):
    """Period/cohort metrics with completeness, coverage and as-of. Amounts need costs.read; a finance.status
    holder receives counts and timing only; anyone else is denied (K05)."""
    try:
        return await rep.metrics(db, actor, period, _day(start, "start"), _day(end, "end"), tz, now=_instant(as_of, "as_of"))
    except Denied as e:
        raise HTTPException(403, e.message)
    except ValidationFailed as e:
        raise HTTPException(422, e.message)


@router.get("/metrics/drilldown")
async def get_drilldown(metric: str = Query(...), period: str = Query("month"), start: str | None = Query(None),
                        end: str | None = Query(None), tz: str = Query(PHOENIX),
                        actor: Actor = Depends(current_actor), db: AsyncSession = Depends(get_db)):
    """Contributing sale / vehicle / cost-item ids plus the exact Finance filters for the same cohort (K01)."""
    try:
        return await rep.drilldown(db, actor, metric, period, _day(start, "start"), _day(end, "end"), tz)
    except Denied as e:
        raise HTTPException(403, e.message)
    except ValidationFailed as e:
        raise HTTPException(422, e.message)


@router.get("/timeline")
async def get_timeline(vehicle_id: list[str] | None = Query(None), view: str | None = Query(None),
                       owner: str | None = Query(None), blocked_only: bool = Query(False),
                       horizon_days: int = Query(7, ge=1, le=365), tz: str = Query(PHOENIX),
                       compact: bool = Query(False), limit: int = Query(50, ge=1, le=200),
                       period_from: str | None = Query(None), period_to: str | None = Query(None),
                       actor: Actor = Depends(current_actor), db: AsyncSession = Depends(get_db)):
    """Vehicle milestone timelines. Record scope applies; a historical period view never hides current work."""
    # has_perm, not the raw perms dict: an external client's grant is the intersection of the owner's permission
    # and its own scopes (ARCHITECTURE "Actor model", invariant 14) — reading perms directly would skip that.
    if not has_perm(actor, "vehicles.read"):
        raise HTTPException(403, "missing permission vehicles.read")
    try:
        if compact:
            return await tl.compact(db, actor, limit=limit, horizon_days=horizon_days, tz=tz)
        return await tl.timeline(db, actor, vehicle_ids=vehicle_id, view=view, owner=owner,
                                 blocked_only=blocked_only, horizon_days=horizon_days, tz=tz, limit=limit,
                                 period_from=_instant(period_from, "period_from"), period_to=_instant(period_to, "period_to"))
    except ValidationFailed as e:
        raise HTTPException(422, e.message)
