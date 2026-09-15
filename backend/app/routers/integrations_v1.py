"""Versioned HTTP connector for external agents without MCP (spec §10.8, §12.3).

Every operation here is the HTTP face of `services.external_clients`, the same service the `/mcp` tools
call. Authorization is a per-client bearer token — never a browser session cookie, never a shared provider
secret. Reads never start work; long work persists immediately and answers 202 so a dropped connection
neither loses nor duplicates it.
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import Blocked, DomainError, ValidationFailed
from ..db import get_db
from ..domain.actors import Actor
from ..models.external import ExternalClient
from ..services import external_clients as ec

log = logging.getLogger("azkt.routers.integrations")
router = APIRouter(prefix="/api/integrations/v1", tags=["external-agents"])

_RANGE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$")


class Caller:
    def __init__(self, client: ExternalClient, actor: Actor, db: AsyncSession):
        self.client, self.actor, self.db = client, actor, db


async def caller(request: Request, db: AsyncSession = Depends(get_db),
                 authorization: str | None = Header(default=None),
                 x_azkt_delegation_depth: str | None = Header(default=None)) -> Caller:
    try:
        client, actor = await ec.authenticate(db, authorization,
                                              delegation_depth_header=x_azkt_delegation_depth, transport="http")
    except DomainError as e:
        # A rejected credential is 401 with a challenge. A valid credential carrying an out-of-bounds
        # delegation depth is a refusal of that chain, not an authentication problem.
        if (e.detail or {}).get("delegation_depth") is not None:
            raise HTTPException(status_code=403, detail=e.to_dict())
        raise HTTPException(status_code=401 if e.code == "denied" else e.status_code,
                            detail=e.to_dict(), headers={"WWW-Authenticate": 'Bearer realm="AZKT"'})
    try:
        await ec.check_rate(db, client)          # every inbound request is metered, reads included
    except ec.QuotaExceeded as e:
        raise HTTPException(429, detail=e.to_dict(),
                            headers={"Retry-After": str((e.detail or {}).get("retry_after") or 60)})
    return Caller(client, actor, db)


def _quota_guard(e: Exception):
    if isinstance(e, ec.QuotaExceeded):
        return HTTPException(429, detail=e.to_dict(),
                             headers={"Retry-After": str((e.detail or {}).get("retry_after") or 60)})
    return None


async def _guard(coro):
    try:
        return await coro
    except ec.QuotaExceeded as e:
        raise _quota_guard(e)


# ── contract ─────────────────────────────────────────────────────────────────
@router.get("/openapi-lite")
async def openapi_lite():
    """Public, data-free contract description so a client can discover the surface before authenticating."""
    from ..agent.mcp_server import tool_catalog
    return {**ec.openapi_lite(), "mcp_tool_catalog": tool_catalog()}


# ── ask / status / reply / cancel ────────────────────────────────────────────
@router.post("/ask")
async def ask(request: Request, payload: dict = Body(default={}), c: Caller = Depends(caller)):
    message = (payload.get("message") or "").strip()
    request_key = (payload.get("request_key") or "").strip()
    if not message:
        raise ValidationFailed("message is required")
    if not request_key:
        raise ValidationFailed("request_key is required so a retry maps to the same logical request")
    entity_refs = payload.get("entity_refs") or []
    asset_ids = payload.get("asset_ids") or []
    if not isinstance(entity_refs, list) or not isinstance(asset_ids, list):
        raise ValidationFailed("entity_refs and asset_ids must be lists")
    # A callback/file URL supplied inside a request is ignored by design (spec §10.8): destinations are
    # owner-configured only. We record that it was seen so the owner can audit the attempt.
    ignored = [k for k in ("callback_url", "webhook_url", "file_url", "fetch_url") if payload.get(k)]
    env, accepted = await _guard(ec.ask(c.db, c.actor, c.client, message=message, request_key=request_key,
                                        entity_refs=entity_refs, asset_ids=asset_ids,
                                        conversation_id=payload.get("conversation_id"), channel="http"))
    body = {**env, "accepted": accepted}
    if ignored:
        body["ignored_fields"] = {k: "AZKT never uses a destination or file URL supplied in a request; the owner "
                                     "configures callbacks in Settings" for k in ignored}
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=202 if accepted else 200, content=_jsonable(body))


@router.get("/work/{request_id}")
async def work(request_id: str, cursor: int = Query(0, ge=0), c: Caller = Depends(caller)):
    return await ec.work_status(c.db, c.actor, c.client, request_id, cursor=cursor)


@router.post("/work/{request_id}/reply")
async def reply(request_id: str, payload: dict = Body(default={}), c: Caller = Depends(caller)):
    if payload.get("approve") or payload.get("approval_id"):
        raise Blocked("a reply can never approve an action; the owner approves in a signed-in AZKT review",
                      review_url=f"{_origin()}/approvals")
    return await _guard(ec.reply(c.db, c.actor, c.client, request_id,
                                 message=(payload.get("message") or "").strip(),
                                 asset_ids=payload.get("asset_ids") or [], channel="http"))


@router.post("/work/{request_id}/cancel")
async def cancel(request_id: str, c: Caller = Depends(caller)):
    return await ec.cancel(c.db, c.actor, c.client, request_id)


# ── records ──────────────────────────────────────────────────────────────────
@router.get("/records/search")
async def records_search(q: str = Query(..., min_length=1), limit: int = Query(10, ge=1, le=50),
                         c: Caller = Depends(caller)):
    """A read. It never starts work and never returns records outside the client's grant."""
    return await ec.find_records(c.db, c.actor, q, limit=limit)


# ── uploads ──────────────────────────────────────────────────────────────────
@router.post("/uploads/prepare")
async def uploads_prepare(payload: dict = Body(default={}), c: Caller = Depends(caller)):
    out = await ec.prepare_upload(c.db, c.actor, content_type=payload.get("content_type"),
                                  size_bytes=payload.get("size_bytes"), filename=payload.get("filename"),
                                  purpose=payload.get("purpose") or "intake")
    await c.db.commit()
    return out


@router.put("/uploads/{upload_id}")
async def upload_bytes(upload_id: str, request: Request, c: Caller = Depends(caller)):
    """Raw bytes for a prepared slot. Only the client that opened the slot may write to it."""
    from ..domain.commands import CommandContext, dispatch
    from ..services import assets as assets_svc
    from ..services.storage import storage
    from datetime import datetime, timezone
    s = await assets_svc.get_session(c.db, c.actor, upload_id)      # 404 for another client's slot
    if s.state not in ("open", "received"):
        raise Blocked(f"upload is {s.state}", upload=assets_svc.serialize_upload(s))
    if s.expires_at and s.expires_at < datetime.now(timezone.utc):
        raise Blocked("upload slot expired; prepare a new upload")
    data = await request.body()
    if not data:
        raise ValidationFailed("empty body")
    st = storage()
    key = s.storage_key or assets_svc.part_key(s.id)
    current = len(st.get(key)) if st.exists(key) else 0
    rng = request.headers.get("Content-Range")
    complete = True
    if rng:
        m = _RANGE.match(rng.strip())
        if not m:
            raise ValidationFailed("Content-Range must be 'bytes start-end/total'")
        start, end, total = int(m.group(1)), int(m.group(2)), m.group(3)
        if end - start + 1 != len(data):
            raise ValidationFailed("Content-Range length does not match the body")
        if start == 0 and current:
            st.delete(key)
            current = 0
        if start != current:
            raise HTTPException(409, detail={"error": "offset_mismatch", "next_offset": current})
        if total != "*" and int(total) > s.max_bytes:
            raise HTTPException(413, detail={"error": "too_large", "max_bytes": s.max_bytes})
        complete = total != "*" and end + 1 >= int(total)
    else:
        if current:
            st.delete(key)
        current = 0
    # the bound `prepare_upload` promised is enforced here, exactly as on the signed-in route: a client
    # cannot stream past its slot's limit (spec §10.8 "bounded, expiring upload session")
    if current + len(data) > s.max_bytes:
        raise HTTPException(413, detail={"error": "too_large", "max_bytes": s.max_bytes})
    size = st.append_part(key, data)
    ctx = CommandContext(db=c.db, actor=c.actor, channel="http")
    res = await dispatch(ctx, "assets.record_chunk", {"upload_id": upload_id, "received_bytes": size,
                                                      "complete": complete})
    return {"next_offset": size, "complete": complete, "upload": (res.data or {}).get("upload")}


@router.post("/uploads/{upload_id}/finalize")
async def uploads_finalize(upload_id: str, payload: dict = Body(default={}), c: Caller = Depends(caller)):
    out = await ec.finalize_upload(c.db, c.actor, upload_id, sha256=payload.get("sha256"))
    await c.db.commit()
    return out


def _origin() -> str:
    from ..core.config import settings
    return settings.PUBLIC_ORIGIN


def _jsonable(obj):
    import json
    return json.loads(json.dumps(obj, default=str))
