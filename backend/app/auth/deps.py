"""FastAPI dependencies: resolve the Actor and build CommandContexts."""
from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..domain.actors import Actor
from ..domain.commands import CommandContext
from ..domain.policy import effective_perms, has_perm
from ..models import User
from .passkey import current_user


def actor_for_user(user: User, kind: str = "user") -> Actor:
    return Actor(kind=kind, user_id=user.id, role=user.role, scope=user.scope,
                 perms=effective_perms(user.role, user.perms), display_name=user.display_name or user.handle)


async def current_actor(user: User = Depends(current_user)) -> Actor:
    return actor_for_user(user)


def require(perm: str):
    """Route guard: 403 unless the signed-in person has `perm`."""
    async def dep(actor: Actor = Depends(current_actor)) -> Actor:
        if not has_perm(actor, perm):
            raise HTTPException(403, f"missing permission {perm}")
        return actor
    return dep


def require_owner():
    async def dep(actor: Actor = Depends(current_actor)) -> Actor:
        if not actor.is_owner or actor.kind != "user":
            raise HTTPException(403, "owner only")
        return actor
    return dep


async def command_context(request: Request, actor: Actor = Depends(current_actor),
                          db: AsyncSession = Depends(get_db)) -> CommandContext:
    rid = request.headers.get("Idempotency-Key") or request.headers.get("X-Request-Id")
    return CommandContext(db=db, actor=actor, request_id=rid,
                          correlation_id=request.headers.get("X-Correlation-Id") or str(uuid.uuid4()),
                          channel="web")
