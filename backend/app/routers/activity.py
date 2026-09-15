"""Activity feed (spec §2.3 Activity): filterable, append-only, role-safe.

Visibility: entries marked `owner` are only for the owner; `finance` requires costs.read.
Record scope: scope=assigned people (and anyone without activity.read who can read tasks) only see
entries on their assigned vehicles/tasks or their own actions. Money-like values are scrubbed from
detail payloads for actors without costs.read. The list is compact; receipt/sources/run ids are only
returned by the detail endpoint. Reads never mutate.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import current_actor
from ..core.time import ensure_aware
from ..db import get_db
from ..domain.access import can_see_costs
from ..domain.actors import Actor
from ..domain.policy import has_perm
from ..models.runtime import ActivityEntry
from ..models.tasks import Task

router = APIRouter(prefix="/api/activity", tags=["activity"])

MONEY_KEYS = ("amount", "price", "cost", "total", "margin", "profit", "landed", "asking", "sold_price", "purchase_amount",
              "balance", "fee", "fees", "subtotal", "net", "gross", "payout", "deposit_amount", "per_action", "cumulative")


NOT_MONEY_SUFFIXES = ("_id", "_ids", "_kind", "_key", "_state", "_status", "_count", "_ref", "_name", "_label")


def _money_like(key: str) -> bool:
    k = key.lower()
    if k.endswith(NOT_MONEY_SUFFIXES):
        return False  # cost_item_id, payment_state, fee_kind ... are references, not amounts
    return any(k == m or k.endswith("_" + m) or k.startswith(m + "_") or m in k.split("_") for m in MONEY_KEYS)


def scrub_money(obj: Any) -> Any:
    """Remove money-like values recursively for actors without costs.read (A02)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _money_like(str(k)):
                out[k] = None
            else:
                out[k] = scrub_money(v)
        if any(_money_like(str(k)) for k in obj):
            out["money_hidden"] = True
        return out
    if isinstance(obj, list):
        return [scrub_money(x) for x in obj]
    return obj


async def _scope_sets(db: AsyncSession, actor: Actor) -> tuple[set[str], set[str]] | None:
    """None = unrestricted; otherwise (vehicle ids, task ids) this person may see."""
    restricted = actor.scope == "assigned" or not has_perm(actor, "activity.read")
    if not restricted:
        return None
    rows = (await db.execute(select(Task.id, Task.vehicle_id).where(Task.owner_user_id == actor.user_id))).all()
    return {v for _, v in rows if v}, {t for t, _ in rows}


def _allowed_visibilities(actor: Actor) -> list[str] | None:
    """None = the owner (or the AI Manager acting for the owner) sees every row, whatever its visibility
    label. Everyone else is allow-listed, so a label this module does not know fails closed."""
    if actor.is_owner:
        return None
    vis = ["all"]
    if can_see_costs(actor):
        vis.append("finance")
    return vis


def _visibility_ok(actor: Actor, vis: str | None) -> bool:
    allowed = _allowed_visibilities(actor)
    return allowed is None or (vis or "all") in allowed


def _record_ok(actor: Actor, scope: tuple[set[str], set[str]] | None, e: ActivityEntry) -> bool:
    if scope is None:
        return True
    vehicles, tasks = scope
    if (e.actor or {}).get("user_id") == actor.user_id:
        return True
    if e.entity_kind == "vehicle" and e.entity_id in vehicles:
        return True
    if e.entity_kind == "task" and e.entity_id in tasks:
        return True
    return False


def _brief(e: ActivityEntry, actor: Actor) -> dict:
    a = e.actor or {}
    return {
        "id": e.id, "at": e.at.isoformat() if e.at else None,
        "actor": {"kind": a.get("kind"), "user_id": a.get("user_id"), "display_name": a.get("display_name"),
                  "role": a.get("role"), "client_id": a.get("client_id"), "client_name": a.get("client_name"),
                  "agent_role": a.get("agent_role")},
        "what": e.what, "entity_kind": e.entity_kind, "entity_id": e.entity_id, "kind": e.kind, "state": e.state,
        "exception": bool(e.exception), "visibility": e.visibility,
        "has_receipt": bool(e.receipt), "has_sources": bool(e.sources), "has_details": bool(e.details),
        "mission_id": e.mission_id, "run_id": e.run_id,
    }


def _detail(e: ActivityEntry, actor: Actor) -> dict:
    d = _brief(e, actor)
    receipt, details, sources = e.receipt or {}, e.details or {}, list(e.sources or [])
    if not can_see_costs(actor):
        receipt, details, sources = scrub_money(receipt), scrub_money(details), scrub_money(sources)
    d.update({"receipt": receipt, "details": details, "sources": sources, "command_name": e.command_name,
              "correlation_id": e.correlation_id, "policy_version": e.policy_version, "version": e.version,
              "created_at": e.created_at.isoformat() if e.created_at else None})
    return d


async def _guard(actor: Actor) -> None:
    if actor.kind not in ("user", "agent"):
        raise HTTPException(403, "not allowed")
    if not (has_perm(actor, "activity.read") or has_perm(actor, "tasks.read")):
        raise HTTPException(403, "not allowed")


@router.get("")
async def list_activity(
    kind: str | None = None, entity_kind: str | None = None, entity_id: str | None = None, actor_id: str | None = Query(None, alias="actor"),
    since: datetime | None = None, until: datetime | None = None, exceptions_only: bool = False, mission_id: str | None = None,
    run_id: str | None = None, correlation_id: str | None = None, q: str | None = None,
    limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
    actor: Actor = Depends(current_actor), db: AsyncSession = Depends(get_db),
):
    await _guard(actor)
    scope = await _scope_sets(db, actor)
    stmt = select(ActivityEntry)
    if kind:
        stmt = stmt.where(ActivityEntry.kind == kind)
    if entity_kind:
        stmt = stmt.where(ActivityEntry.entity_kind == entity_kind)
    if entity_id:
        stmt = stmt.where(ActivityEntry.entity_id == entity_id)
    if actor_id:
        stmt = stmt.where((ActivityEntry.actor["user_id"].as_string() == actor_id) | (ActivityEntry.actor["client_id"].as_string() == actor_id))
    if since:
        stmt = stmt.where(ActivityEntry.at >= ensure_aware(since))
    if until:
        stmt = stmt.where(ActivityEntry.at < ensure_aware(until))
    if exceptions_only:
        stmt = stmt.where(ActivityEntry.exception.is_(True))
    if mission_id:
        stmt = stmt.where(ActivityEntry.mission_id == mission_id)
    if run_id:
        stmt = stmt.where(ActivityEntry.run_id == run_id)
    if correlation_id:
        stmt = stmt.where(ActivityEntry.correlation_id == correlation_id)
    if q:
        stmt = stmt.where(ActivityEntry.what.ilike(f"%{q.strip()}%"))
    # visibility is enforced in SQL so hidden rows never leave the database (same rule as the detail endpoint)
    allowed_vis = _allowed_visibilities(actor)
    if allowed_vis is not None:
        stmt = stmt.where(ActivityEntry.visibility.in_(allowed_vis))
    if scope is not None:
        vehicles, tasks = scope
        cond = ActivityEntry.actor["user_id"].as_string() == actor.user_id
        if vehicles:
            cond = cond | ((ActivityEntry.entity_kind == "vehicle") & ActivityEntry.entity_id.in_(vehicles))
        if tasks:
            cond = cond | ((ActivityEntry.entity_kind == "task") & ActivityEntry.entity_id.in_(tasks))
        stmt = stmt.where(cond)
    total = int(await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (await db.execute(stmt.order_by(ActivityEntry.at.desc(), ActivityEntry.created_at.desc()).offset(offset).limit(limit))).scalars().all()
    return {"items": [_brief(e, actor) for e in rows], "total": total, "limit": limit, "offset": offset,
            "money_hidden": not can_see_costs(actor), "scoped": scope is not None}


@router.get("/{entry_id}")
async def get_activity(entry_id: str, actor: Actor = Depends(current_actor), db: AsyncSession = Depends(get_db)):
    await _guard(actor)
    e = await db.get(ActivityEntry, entry_id)
    if e is None or not _visibility_ok(actor, e.visibility):
        raise HTTPException(404, "not found")
    scope = await _scope_sets(db, actor)
    if not _record_ok(actor, scope, e):
        raise HTTPException(404, "not found")
    return _detail(e, actor)
