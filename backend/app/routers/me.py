"""The signed-in person: profile, effective permissions, preferences, pairing status.

PATCH /api/me/prefs only accepts preferences. Any attempt to send role/scope/perms/status here is
refused server-side (A01): those are owner-only team commands and never self-service.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import command_context, current_actor
from ..auth.passkey import current_user
from ..db import get_db
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..domain.policy import effective_perms
from ..models.auth import User
from ..services import team as team_svc

router = APIRouter(prefix="/api/me", tags=["me"])

_ACCESS_KEYS = {"role", "scope", "perms", "status", "manager_id", "grants", "session_version", "user_id", "id", "overrides"}


@router.get("")
async def me(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return {
        "id": user.id, "version": user.version or 1, "handle": user.handle, "display_name": user.display_name or user.handle,
        "email": user.email, "phone": user.phone, "role": user.role, "scope": user.scope, "status": user.status,
        "manager_id": user.manager_id,
        "perms": effective_perms(user.role, user.perms),
        "grants": list(user.grants or []),
        "prefs": team_svc.serialize_prefs(user),
        "pairing": {"telegram": await team_svc.telegram_pairing_status(db, user.id)},
        "last_seen_at": user.last_seen_at.isoformat() if user.last_seen_at else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.patch("/prefs")
async def update_prefs(request: Request, actor: Actor = Depends(current_actor), ctx: CommandContext = Depends(command_context)):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(422, "expected an object")
    if _ACCESS_KEYS & set(body):
        # Self-service can never touch access. Refuse before anything is validated or written.
        raise HTTPException(403, "not allowed")
    return (await dispatch(ctx, "me.update_prefs", body)).to_dict()
