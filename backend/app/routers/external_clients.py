"""Settings → Connections → External agents (spec §10.8).

    GET  /api/settings/external-clients            registered clients, grants, last use
    GET  /api/settings/external-clients/health     per client: health, in-flight missions, request totals
    POST /api/settings/external-clients            register (returns the bearer token exactly once)
    POST /api/settings/external-clients/{id}/{action}   rotate | revoke | set-callback
    GET  /api/settings/external-clients/{id}/requests   the client's delegated requests

Owner only. The plain token is returned once, at registration or rotation, and is never stored or
re-displayed; the rest of the surface shows only its prefix.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import command_context, require_owner
from ..core.errors import NotFound
from ..db import get_db
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..models.external import CLIENT_SCOPES, DelegatedRequest, ExternalClient
from ..services import external_clients as svc

router = APIRouter(prefix="/api/settings/external-clients", tags=["external-agents"])

ACTIONS = {"rotate": "external_clients.rotate", "revoke": "external_clients.revoke",
           "set-callback": "external_clients.set_callback"}


@router.get("")
async def list_clients(actor: Actor = Depends(require_owner()), db: AsyncSession = Depends(get_db)):
    from ..agent.mcp_server import tool_catalog
    out = await svc.list_clients(db)
    return {**out, "connect": svc.openapi_lite()["base_url"], "mcp_url": svc.openapi_lite()["mcp_url"],
            "scopes": list(CLIENT_SCOPES), "mcp_tools": tool_catalog()}


@router.get("/health")
async def health(client_id: str | None = Query(default=None), actor: Actor = Depends(require_owner()),
                 db: AsyncSession = Depends(get_db)):
    return await svc.health(db, client_id)


@router.post("")
async def register(payload: dict = Body(...), actor: Actor = Depends(require_owner()),
                   ctx: CommandContext = Depends(command_context)):
    res = await dispatch(ctx, "external_clients.register", payload)
    return res.to_dict()


@router.get("/{client_id}/requests")
async def client_requests(client_id: str, limit: int = Query(25, ge=1, le=200),
                          actor: Actor = Depends(require_owner()), db: AsyncSession = Depends(get_db)):
    c = await db.get(ExternalClient, client_id)
    if c is None:
        raise NotFound("external client not found")
    rows = (await db.execute(select(DelegatedRequest).where(DelegatedRequest.client_id == client_id)
                             .order_by(DelegatedRequest.created_at.desc()).limit(limit))).scalars().all()
    return {"client": svc.serialize_client(c),
            "items": [{"id": r.id, "request_key": r.request_key, "kind": r.kind, "status": r.status,
                       "mission_id": r.mission_id, "message": (r.message or "")[:400], "cursor": r.cursor,
                       "depth": r.depth, "error": r.error, "correlation_id": r.correlation_id,
                       "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows],
            "count": len(rows)}


@router.post("/{client_id}/{action}")
async def client_action(client_id: str, action: str, payload: dict = Body(default={}),
                        actor: Actor = Depends(require_owner()), ctx: CommandContext = Depends(command_context)):
    name = ACTIONS.get(action)
    if name is None:
        raise HTTPException(404, f"unknown action {action}; expected one of {sorted(ACTIONS)}")
    res = await dispatch(ctx, name, {**(payload or {}), "client_id": client_id})
    return res.to_dict()
