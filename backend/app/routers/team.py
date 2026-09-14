"""Team & access HTTP surface (spec §2.3 Settings/Team, §11.1).

Owner: everyone, with permission detail. Manager: a scoped "People" list (active people who report
to them) without permission detail. Everyone else: 403 with no detail. All writes dispatch commands.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import command_context, current_actor
from ..db import get_db
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..domain.policy import has_perm
from ..models.auth import Invitation, User
from ..services import team as team_svc

router = APIRouter(prefix="/api/team", tags=["team"])


def _owner_guard(perm: str = "team"):
    async def dep(actor: Actor = Depends(current_actor)) -> Actor:
        if actor.kind != "user" or not has_perm(actor, perm):
            raise HTTPException(403, "not allowed")
        return actor
    return dep


@router.get("")
async def list_team(actor: Actor = Depends(current_actor), db: AsyncSession = Depends(get_db)):
    if actor.kind == "user" and has_perm(actor, "team"):
        rows = await team_svc.list_people(db, actor.user_id, full=True)
        return {"items": [team_svc.serialize_person(u, detail=True) for u in rows], "total": len(rows), "scope": "all"}
    if actor.kind == "user" and has_perm(actor, "tasks.assign"):
        rows = await team_svc.list_people(db, actor.user_id, full=False)
        return {"items": [team_svc.serialize_person(u) for u in rows], "total": len(rows), "scope": "reports"}
    raise HTTPException(403, "not allowed")


@router.get("/invitations")
async def list_invitations(status: str | None = None, actor: Actor = Depends(_owner_guard()), db: AsyncSession = Depends(get_db)):
    q = select(Invitation).order_by(Invitation.created_at.desc())
    if status:
        q = q.where(Invitation.status == status)
    rows = (await db.execute(q)).scalars().all()
    return {"items": [team_svc.serialize_invitation(i) for i in rows], "total": len(rows)}


@router.post("/invite")
async def invite(body: team_svc.TeamInviteIn, actor: Actor = Depends(_owner_guard()),
                 ctx: CommandContext = Depends(command_context)):
    return (await dispatch(ctx, "team.invite", body)).to_dict()


class _Reason(BaseModel):
    expected_version: int | None = None
    reason: str | None = None


@router.post("/invitations/{invitation_id}/revoke")
async def revoke_invitation(invitation_id: str, body: _Reason | None = None, actor: Actor = Depends(_owner_guard()),
                            ctx: CommandContext = Depends(command_context)):
    body = body or _Reason()
    return (await dispatch(ctx, "team.revoke_invitation", {"invitation_id": invitation_id, **body.model_dump()})).to_dict()


@router.get("/{user_id}")
async def get_person(user_id: str, actor: Actor = Depends(current_actor), db: AsyncSession = Depends(get_db)):
    u = await db.get(User, user_id)
    if u is None:
        raise HTTPException(404, "person not found")
    if actor.kind == "user" and has_perm(actor, "team"):
        return team_svc.serialize_person(u, detail=True)
    if actor.kind == "user" and has_perm(actor, "tasks.assign") and u.status == "active" and (
            u.manager_id == actor.user_id or (u.manager_id is None and u.role == "mechanic") or u.id == actor.user_id):
        return team_svc.serialize_person(u)
    raise HTTPException(403, "not allowed")


class _PersonPatch(BaseModel):
    expected_version: int | None = None
    display_name: str | None = None
    email: str | None = None
    phone: str | None = None
    role: str | None = None
    scope: str | None = None
    manager_id: str | None = None
    clear_manager: bool = False
    perms: dict | None = None
    reason: str | None = None


@router.post("/{user_id}/update")
async def update_person(user_id: str, body: _PersonPatch, actor: Actor = Depends(_owner_guard()),
                        ctx: CommandContext = Depends(command_context)):
    return (await dispatch(ctx, "team.update_person", {"user_id": user_id, **body.model_dump()})).to_dict()


@router.post("/{user_id}/disable")
async def disable_person(user_id: str, body: _Reason | None = None, actor: Actor = Depends(_owner_guard()),
                         ctx: CommandContext = Depends(command_context)):
    body = body or _Reason()
    return (await dispatch(ctx, "team.disable_person", {"user_id": user_id, **body.model_dump()})).to_dict()


@router.post("/{user_id}/enable")
async def enable_person(user_id: str, body: _Reason | None = None, actor: Actor = Depends(_owner_guard()),
                        ctx: CommandContext = Depends(command_context)):
    body = body or _Reason()
    return (await dispatch(ctx, "team.enable_person", {"user_id": user_id, **body.model_dump()})).to_dict()


class _Grant(BaseModel):
    perm: str
    expected_version: int | None = None
    note: str | None = None


@router.post("/{user_id}/grant")
async def grant(user_id: str, body: _Grant, actor: Actor = Depends(_owner_guard("permissions")),
                ctx: CommandContext = Depends(command_context)):
    return (await dispatch(ctx, "team.grant", {"user_id": user_id, **body.model_dump()})).to_dict()


@router.post("/{user_id}/revoke-grant")
async def revoke_grant(user_id: str, body: _Grant, actor: Actor = Depends(_owner_guard("permissions")),
                       ctx: CommandContext = Depends(command_context)):
    return (await dispatch(ctx, "team.revoke_grant", {"user_id": user_id, **body.model_dump()})).to_dict()
