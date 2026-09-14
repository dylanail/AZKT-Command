"""Settings → Connections: status/freshness for every provider, Google OAuth start/callback,
disconnect. Owner-only (perm 'connections'). Secrets never leave the server."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters import google_oauth
from ..auth.deps import current_actor, require
from ..core.config import settings
from ..core.errors import NotFound, ProviderError
from ..db import get_db
from ..domain.actors import Actor
from ..models.runtime import ActivityEntry
from ..services import connections as conn_svc
from datetime import datetime, timezone

router = APIRouter(prefix="/api/connections", tags=["connections"])


@router.get("")
async def list_connections(actor: Actor = Depends(current_actor), db: AsyncSession = Depends(get_db)):
    items = await conn_svc.overview(db)
    if not actor.perms.get("connections"):
        # non-owners only see freshness labels, never account identities or scopes
        items = [{"provider": i["provider"], "label": i["label"], "freshness": i["freshness"], "status": i["status"]} for i in items]
    ok, stale = conn_svc.all_clear_possible(items)
    return {"items": items, "total": len(items), "all_clear_possible": ok, "stale": stale,
            "google_oauth_configured": google_oauth.configured(), "environment": settings.ENV}


@router.get("/{provider}")
async def get_connection(provider: str, actor: Actor = Depends(require("connections")), db: AsyncSession = Depends(get_db)):
    conn = await conn_svc.get(db, provider)
    return conn_svc.serialize(conn, provider)


@router.post("/google/{provider}/start")
async def google_start(provider: str, request: Request, actor: Actor = Depends(require("connections")),
                       db: AsyncSession = Depends(get_db)):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    extra = []
    if provider == "gmail_business" and body.get("enable_send"):
        extra += google_oauth.SCOPES["gmail_business_send"]
    if provider == "gmail_business" and body.get("enable_modify"):
        extra += google_oauth.SCOPES["gmail_business_modify"]
    conn = await conn_svc.get(db, provider, create=True)
    if provider == "gmail_personal" and body.get("expected_identity"):
        conn.config = {**(conn.config or {}), "expected_identity": body["expected_identity"]}
    await db.commit()
    res = google_oauth.start(provider, actor.user_id, extra)
    return {"url": res["url"], "scopes": res["scopes"], "redirect_uri": google_oauth.redirect_uri()}


@router.get("/google/callback")
async def google_callback(state: str = Query(...), code: str | None = None, error: str | None = None,
                          db: AsyncSession = Depends(get_db)):
    target = f"{settings.PUBLIC_ORIGIN}/settings/connections"
    if error or not code:
        return RedirectResponse(f"{target}?google=denied")
    try:
        conn = await google_oauth.exchange(db, state, code)
        db.add(ActivityEntry(at=datetime.now(timezone.utc), actor={"kind": "user", "user_id": conn.connected_by},
                             what=f"Connected {conn_svc.PROVIDER_LABELS.get(conn.provider, conn.provider)} as {conn.account_identity}",
                             entity_kind="connection", entity_id=conn.id, kind="connection", state="connected",
                             details={"granted_scopes": conn.granted_scopes}))
        await db.commit()
        return RedirectResponse(f"{target}?google=connected&provider={conn.provider}")
    except ProviderError as e:
        await db.commit()
        return RedirectResponse(f"{target}?google=error&message={e.message[:120]}")


@router.post("/{provider}/disconnect")
async def disconnect(provider: str, actor: Actor = Depends(require("connections")), db: AsyncSession = Depends(get_db)):
    conn = await conn_svc.get(db, provider)
    if conn is None:
        raise NotFound("not connected")
    if provider in ("gmail_business", "gmail_personal", "drive", "sheets"):
        await google_oauth.revoke(db, conn)
    else:
        conn.secret_enc = None
        conn.status = "disconnected"
        conn.disconnected_at = datetime.now(timezone.utc)
    db.add(ActivityEntry(at=datetime.now(timezone.utc), actor=actor.snapshot(), what=f"Disconnected {provider}",
                         entity_kind="connection", entity_id=conn.id, kind="connection", state="disconnected"))
    await db.commit()
    return conn_svc.serialize(conn, provider)


@router.post("/{provider}/config")
async def set_config(provider: str, request: Request, actor: Actor = Depends(require("connections")),
                     db: AsyncSession = Depends(get_db)):
    """Non-secret configuration: selected folder id, ledger sheet id, personal allowlist, site url."""
    body = await request.json()
    conn = await conn_svc.get(db, provider, create=True)
    conn.config = {**(conn.config or {}), **{k: v for k, v in body.items() if not str(k).startswith("_")}}
    db.add(ActivityEntry(at=datetime.now(timezone.utc), actor=actor.snapshot(), what=f"Updated {provider} configuration",
                         entity_kind="connection", entity_id=conn.id, kind="connection", state=conn.status,
                         details={"keys": list(body.keys())}))
    await db.commit()
    return conn_svc.serialize(conn, provider)


@router.post("/{provider}/secret")
async def set_secret(provider: str, request: Request, actor: Actor = Depends(require("connections")),
                     db: AsyncSession = Depends(get_db)):
    """Token-style credentials entered in Settings (Square access token, WordPress app password, Telegram bot token).
    Stored encrypted; never echoed back."""
    body = await request.json()
    conn = await conn_svc.get(db, provider, create=True)
    conn_svc.set_secret(conn, {k: v for k, v in body.items() if v})
    conn.status = "connected" if any(body.values()) else "disconnected"
    conn.connected_by = actor.user_id
    conn.connected_at = datetime.now(timezone.utc)
    conn.environment = settings.ENV
    db.add(ActivityEntry(at=datetime.now(timezone.utc), actor=actor.snapshot(), what=f"Stored credentials for {provider}",
                         entity_kind="connection", entity_id=conn.id, kind="connection", state=conn.status))
    await db.commit()
    return conn_svc.serialize(conn, provider)
