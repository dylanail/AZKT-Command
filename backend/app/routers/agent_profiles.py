from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import command_context, require
from ..core.errors import ValidationFailed
from ..db import get_db
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..services import agent_profiles as svc

router = APIRouter(tags=["agent-profiles"])


@router.get("/api/agent-profiles")
async def profiles(actor: Actor = Depends(require("agents.chat")), db: AsyncSession = Depends(get_db)):
    return await svc.list_profiles(db)


@router.get("/api/agent-profiles/{role}")
async def profile(role: str, actor: Actor = Depends(require("agents.chat")), db: AsyncSession = Depends(get_db)):
    return await svc.get_profile(db, role)


@router.post("/api/agent-profiles/{role}/versions")
async def propose(role: str, payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    return (await dispatch(ctx, "agent_profiles.propose", {**payload, "role": role})).to_dict()


@router.post("/api/agent-profile-versions/{version_id}/validate")
async def validate(version_id: str, ctx: CommandContext = Depends(command_context)):
    return (await dispatch(ctx, "agent_profiles.validate", {"version_id": version_id})).to_dict()


@router.post("/api/agent-profile-versions/{version_id}/activate")
async def activate(version_id: str, payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context)):
    return (await dispatch(ctx, "agent_profiles.activate", {"version_id": version_id,
                                                              "reason": (payload or {}).get("reason", "")})).to_dict()


async def _archive(request: Request) -> bytes:
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > svc.MAX_ARCHIVE_BYTES:
            raise ValidationFailed("OpenClaw bundle must be no larger than 10 MB")
    return bytes(data)


@router.post("/api/agent-profiles/import/preview")
async def preview_import(request: Request, actor: Actor = Depends(require("knowledge.write"))):
    bundle = svc.inspect_openclaw_bundle(await _archive(request))
    return {"agents": bundle["agents"], "archive_sha256": bundle["archive_sha256"]}


@router.post("/api/agent-profiles/{role}/import")
async def import_bundle(role: str, request: Request, source_agent: str = Query(..., min_length=1, max_length=80),
                        filename: str = Query("openclaw-agents.zip", max_length=255),
                        ctx: CommandContext = Depends(command_context)):
    bundle = svc.inspect_openclaw_bundle(await _archive(request))
    payload = svc.imported_payload(role, source_agent, bundle, filename)
    return (await dispatch(ctx, "agent_profiles.propose", payload)).to_dict()
