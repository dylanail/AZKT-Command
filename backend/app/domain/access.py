"""Read-side record scoping. Every list/detail query applies these filters so a scope=assigned
person or a record-limited external client never sees rows outside their grant (spec §11.1)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import Denied
from ..models.tasks import Task
from .actors import Actor
from .policy import has_perm


async def visible_vehicle_ids(db: AsyncSession, actor: Actor) -> set[str] | None:
    """None = unrestricted. A set = only these vehicle ids are visible."""
    limit: set[str] | None = None
    if actor.kind == "external":
        ids = (actor.client_record_scope or {}).get("vehicle_ids")
        if ids:
            limit = set(ids)
    if actor.kind in ("user", "agent") and (actor.scope == "assigned" or not actor.perms.get("vehicles.all", False)):
        rows = (await db.execute(select(Task.vehicle_id).where(Task.owner_user_id == actor.user_id,
                                                                Task.vehicle_id.is_not(None)))).all()
        assigned = {r[0] for r in rows}
        limit = assigned if limit is None else (limit & assigned)
    return limit


async def assert_vehicle_visible(db: AsyncSession, actor: Actor, vehicle_id: str) -> None:
    ids = await visible_vehicle_ids(db, actor)
    if ids is not None and vehicle_id not in ids:
        raise Denied("record not accessible")


def can_see_costs(actor: Actor) -> bool:
    return has_perm(actor, "costs.read")


def can_see_finance_status(actor: Actor) -> bool:
    return has_perm(actor, "finance.status") or has_perm(actor, "costs.read")


def sanitize_money(actor: Actor, payload: dict, keys: tuple[str, ...]) -> dict:
    """Strip owner-only money fields for actors without costs.read."""
    if can_see_costs(actor):
        return payload
    out = dict(payload)
    for k in keys:
        if k in out:
            out[k] = None
    out["money_hidden"] = True
    return out
