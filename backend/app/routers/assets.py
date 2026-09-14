"""Uploads and private assets (spec §7.1, §7.4 step 2, §13.3).

    POST /api/uploads                 prepare a bounded, expiring slot (limits + allowed types returned first)
    PUT  /api/uploads/{id}            raw bytes; `Content-Range: bytes a-b/total` appends for resumable uploads
    GET  /api/uploads/{id}            slot status and next offset
    POST /api/uploads/{id}/finalize   validate, dedupe, store, derive -> asset id
    GET  /api/assets/{id}/(original|web|thumb)   bytes, only after the linked-entity / record-scope check
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import command_context, current_actor, require
from ..core.errors import Blocked, Denied, NotFound, ValidationFailed
from ..db import get_db
from ..domain.access import assert_vehicle_visible
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..models.assets import Asset
from ..services import assets as svc
from ..services.storage import storage

router = APIRouter(tags=["assets"])

VARIANTS = ("original", "web", "thumb")
ASSET_ACTIONS = {"link": "assets.link", "classify": "assets.classify", "unlink": "assets.remove_link", "transcript": "assets.set_transcript"}
_RANGE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$")


# ── uploads ──────────────────────────────────────────────────────────────────
@router.post("/api/uploads")
async def prepare_upload(payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context)):
    res = await dispatch(ctx, "assets.prepare_upload", payload or {})
    return res.to_dict()


@router.get("/api/uploads/{upload_id}")
async def upload_status(upload_id: str, actor: Actor = Depends(require("intake")), db: AsyncSession = Depends(get_db)):
    s = await svc.get_session(db, actor, upload_id)
    st = storage()
    key = s.storage_key or svc.part_key(s.id)
    on_disk = len(st.get(key)) if s.state in ("open", "received") and st.exists(key) else s.received_bytes
    return {"upload": svc.serialize_upload(s), "next_offset": int(on_disk or 0), "max_bytes": s.max_bytes,
            "allowed_types": list(s.allowed_types or svc.allowed_types())}


@router.put("/api/uploads/{upload_id}")
async def put_bytes(upload_id: str, request: Request, ctx: CommandContext = Depends(command_context)):
    """Raw body. Without Content-Range the body is the whole file (a repeat replaces it). With
    `Content-Range: bytes start-end/total` the chunk must start at the current offset (409 otherwise, with next_offset)."""
    s = await svc.get_session(ctx.db, ctx.actor, upload_id)
    if s.state not in ("open", "received"):
        raise Blocked(f"upload is {s.state}", upload=svc.serialize_upload(s))
    if s.expires_at and s.expires_at < ctx.now:
        raise Blocked("upload slot expired; prepare a new upload")
    data = await request.body()
    if not data:
        raise ValidationFailed("empty body")
    st = storage()
    key = s.storage_key or svc.part_key(s.id)
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
            raise HTTPException(409, {"error": "offset_mismatch", "message": f"expected offset {current}", "next_offset": current})
        if total != "*" and int(total) > s.max_bytes:
            raise HTTPException(413, {"error": "too_large", "message": f"file too large (limit {s.max_bytes} bytes)", "max_bytes": s.max_bytes})
        complete = total != "*" and end + 1 >= int(total)
    else:
        if current:
            st.delete(key)
        current = 0
    if current + len(data) > s.max_bytes:
        raise HTTPException(413, {"error": "too_large", "message": f"file too large (limit {s.max_bytes} bytes)", "max_bytes": s.max_bytes})
    size = st.append_part(key, data)
    res = await dispatch(ctx, "assets.record_chunk", {"upload_id": s.id, "received_bytes": size, "complete": complete})
    out = res.to_dict()
    out["next_offset"] = size
    out["complete"] = complete
    return out


@router.post("/api/uploads/{upload_id}/finalize")
async def finalize_upload(upload_id: str, payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context)):
    body = dict(payload or {})
    body["upload_id"] = upload_id
    res = await dispatch(ctx, "assets.finalize_upload", body)
    return res.to_dict()


# ── assets ───────────────────────────────────────────────────────────────────
async def _asset(db: AsyncSession, actor: Actor, asset_id: str) -> Asset:
    a = await db.get(Asset, asset_id)
    if a is None:
        raise NotFound("asset not found")
    if not await svc.can_access_asset(db, actor, a):
        raise Denied("asset not accessible")
    return a


@router.get("/api/assets")
async def list_links(entity_kind: str = Query(...), entity_id: str = Query(...), actor: Actor = Depends(current_actor),
                     db: AsyncSession = Depends(get_db)):
    if entity_kind == "vehicle":
        await assert_vehicle_visible(db, actor, entity_id)
    rows = await svc.links_of(db, entity_kind, entity_id)
    items = []
    for l, a in rows:
        if entity_kind != "vehicle" and not await svc.can_access_asset(db, actor, a):
            continue
        if a.sensitive and not (actor.perms.get("documents.read") or actor.role == "owner"):
            continue
        d = svc.serialize_asset(a)
        d["link"] = svc.serialize_link(l)
        items.append(d)
    return {"items": items, "total": len(items)}


@router.get("/api/assets/{asset_id}")
async def get_asset(asset_id: str, actor: Actor = Depends(current_actor), db: AsyncSession = Depends(get_db)):
    a = await _asset(db, actor, asset_id)
    links = await svc.active_links(db, a.id)
    return {"asset": svc.serialize_asset(a), "links": [svc.serialize_link(l) for l in links]}


@router.get("/api/assets/{asset_id}/{variant}")
async def get_bytes(asset_id: str, variant: str, actor: Actor = Depends(current_actor), db: AsyncSession = Depends(get_db)):
    if variant not in VARIANTS:
        raise HTTPException(404, f"variant must be one of {VARIANTS}")
    a = await _asset(db, actor, asset_id)
    if a.status != "ready":
        raise NotFound("asset not ready", status=a.status, error=a.error)
    got = svc.variant_bytes(a, variant)
    if got is None:
        d = a.derivatives or {}
        raise NotFound(f"{variant} derivative not available", status=d.get("status", "missing"), reason=d.get("reason"))
    data, ctype = got
    headers = {"Cache-Control": "private, max-age=3600", "Content-Disposition": f'inline; filename="{(a.original_name or a.id)[:120]}"',
               "X-Content-Type-Options": "nosniff"}
    return Response(content=data, media_type=ctype, headers=headers)


@router.post("/api/assets/{asset_id}/{action}")
async def asset_action(asset_id: str, action: str, payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context)):
    name = ASSET_ACTIONS.get(action)
    if name is None:
        raise HTTPException(404, f"unknown asset action {action!r}")
    body = dict(payload or {})
    body["asset_id"] = asset_id
    res = await dispatch(ctx, name, body)
    return res.to_dict()
