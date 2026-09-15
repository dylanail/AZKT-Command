"""Settings → Connections → External agents (spec §10.8).

    GET  /api/settings/external-clients            registered clients, grants, last use
    GET  /api/settings/external-clients/health     per client: health, in-flight missions, request totals
    POST /api/settings/external-clients            register (returns the bearer token exactly once)
    POST /api/settings/external-clients/{id}/{action}   rotate | revoke | set-callback
    POST /api/settings/external-clients/{id}/test-callback  one signed test POST to the stored destination
    GET  /api/settings/external-clients/{id}/requests   the client's delegated requests
    GET  /api/settings/external-clients/{id}/callbacks  recent signed callback deliveries and every attempt

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
from ..services import external_callbacks as callbacks_svc
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


@router.get("/{client_id}/callbacks")
async def client_callbacks(client_id: str, limit: int = Query(25, ge=1, le=200),
                           actor: Actor = Depends(require_owner()), db: AsyncSession = Depends(get_db)):
    """Recent signed callback deliveries for one client, with every attempt and its outcome.

    Truthful by construction: `accepted` is a 2xx the endpoint actually returned, `unknown` is a connection
    that broke after the body was sent, and neither the signing secret nor any signature appears here.
    """
    c = await db.get(ExternalClient, client_id)
    if c is None:
        raise NotFound("external client not found")
    out = await callbacks_svc.recent(db, client_id, limit=limit)
    return {**out, "client": svc.serialize_client(c),
            "polling_only": not callbacks_svc.callback_configured(c),
            "verification": svc.openapi_lite()["callbacks"]}


@router.post("/{client_id}/test-callback")
async def test_callback(client_id: str, actor: Actor = Depends(require_owner()),
                        ctx: CommandContext = Depends(command_context)):
    """Prove the owner's endpoint before real work depends on it: one signed POST with no business data,
    sent to the stored destination and reported back with whatever really happened — including a
    non-production destination refusal (H08)."""
    res = await dispatch(ctx, "external_clients.test_callback", {"client_id": client_id})
    delivery = ((res.data or {}).get("delivery") or {}) if res.status == "ok" else {}
    if not delivery.get("id"):
        return res.to_dict()
    out = await callbacks_svc.run_test_callback(ctx.db, delivery["id"])
    return {**res.to_dict(), "data": {**(res.data or {}), **out}}


@router.post("/{client_id}/{action}")
async def client_action(client_id: str, action: str, payload: dict = Body(default={}),
                        actor: Actor = Depends(require_owner()), ctx: CommandContext = Depends(command_context)):
    name = ACTIONS.get(action)
    if name is None:
        raise HTTPException(404, f"unknown action {action}; expected one of {sorted(ACTIONS)}")
    res = await dispatch(ctx, name, {**(payload or {}), "client_id": client_id})
    return res.to_dict()
