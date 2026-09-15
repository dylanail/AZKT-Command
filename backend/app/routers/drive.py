"""Importer Drive setup and review API (spec §7.1, §12.3).

GET endpoints have no side effects: the folder picker lists candidates from the connected account,
the status endpoint reports index coverage and freshness, and the matches endpoint shows proposals
with their evidence. Every write goes through a command.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters import drive as drive_adapter
from ..auth.deps import command_context, current_actor, require
from ..core.errors import Unsupported
from ..db import get_db
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..services import drive_assets as svc

router = APIRouter(prefix="/api/drive", tags=["drive"])


@router.get("/folders")
async def folder_candidates(q: str | None = Query(default=None, description="folder name to search for"),
                            db: AsyncSession = Depends(get_db), actor: Actor = Depends(require("connections"))) -> dict:
    """Folder picker: candidates by name inside the connected account. Duplicate names are returned
    together so the owner chooses explicitly (A09)."""
    conn = await svc.drive_connection(db)
    try:
        ad = svc.adapter(db, conn)
    except Unsupported as e:
        return {"connected": False, "setup_blocked": e.message, "candidates": []}
    rows = await ad.search_folders(q or "Dylan Nail Shipments")
    seen: dict[str, int] = {}
    for r in rows:
        seen[r.get("name") or ""] = seen.get(r.get("name") or "", 0) + 1
    return {"connected": True, "query": q or "Dylan Nail Shipments", "candidates": [
        {"id": r["id"], "name": r.get("name"), "link": r.get("webViewLink"),
         "owner": ((r.get("owners") or [{}])[0]).get("emailAddress"), "modified_time": r.get("modifiedTime"),
         "duplicate_name": seen.get(r.get("name") or "", 0) > 1} for r in rows],
        "selected": svc.root_id_of(conn)}


@router.post("/root")
async def select_root(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)) -> dict:
    res = await dispatch(ctx, "drive.select_root", payload)
    return res.to_dict()


@router.get("/status")
async def status(db: AsyncSession = Depends(get_db), actor: Actor = Depends(require("connections"))) -> dict:
    return await svc.coverage(db)


@router.get("/files")
async def files(folder_id: str | None = Query(default=None), limit: int = Query(default=200, ge=1, le=1000),
                db: AsyncSession = Depends(get_db), actor: Actor = Depends(require("connections"))) -> dict:
    return {"items": await svc.files_in(db, folder_id, limit=limit)}


@router.get("/matches")
async def matches(include_matched: bool = Query(default=False), db: AsyncSession = Depends(get_db),
                  actor: Actor = Depends(require("vehicles.read"))) -> dict:
    states = ("proposed", "ambiguous", "matched") if include_matched else ("proposed", "ambiguous")
    return {"items": await svc.proposed_matches(db, states=states)}


@router.post("/scan")
async def scan(payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context)) -> dict:
    res = await dispatch(ctx, "drive.scan", payload or {})
    return res.to_dict()


@router.post("/import")
async def import_assets(payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context)) -> dict:
    res = await dispatch(ctx, "drive.import_assets", payload or {})
    return res.to_dict()


@router.post("/matches/{file_id}/{decision}")
async def decide_match(file_id: str, decision: str, payload: dict = Body(default={}),
                       ctx: CommandContext = Depends(command_context)) -> dict:
    if decision not in ("confirm", "correct", "leave"):
        raise HTTPException(404, "unknown decision")
    res = await dispatch(ctx, "drive.confirm_match", {**(payload or {}), "file_id": file_id, "decision": decision})
    return res.to_dict()
