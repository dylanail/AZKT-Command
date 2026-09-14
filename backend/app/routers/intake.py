"""Vehicle intake API (spec §7.4): start / continue / status / analyze / choose / apply / correct / undo / abandon.
Status returns per-item saved/failed so the composer can show saved-on-device versus saved-to-AZKT truthfully."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import command_context, require
from ..db import get_db
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..services import intake as svc

router = APIRouter(prefix="/api/vehicle-intakes", tags=["intake"])

ACTIONS = {"assets": "intake.add_assets", "notes": "intake.add_note", "transcript": "intake.add_transcript", "analyze": "intake.analyze",
           "choose": "intake.choose_vehicle", "apply": "intake.apply", "correct": "intake.correct", "undo": "intake.undo",
           "abandon": "intake.abandon"}


@router.post("")
async def start(payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context)):
    res = await dispatch(ctx, "intake.start", payload or {})
    return res.to_dict()


@router.get("")
async def list_intakes(status: str | None = None, vehicle_id: str | None = None, limit: int = Query(50, ge=1, le=200),
                       offset: int = Query(0, ge=0), actor: Actor = Depends(require("intake")), db: AsyncSession = Depends(get_db)):
    rows, total = await svc.list_intakes(db, actor, status=status, vehicle_id=vehicle_id, limit=limit, offset=offset)
    return {"items": [svc.serialize_intake(it) for it in rows], "total": total}


@router.get("/{intake_id}")
async def status(intake_id: str, actor: Actor = Depends(require("intake")), db: AsyncSession = Depends(get_db)):
    it = await svc.load_intake(db, actor, intake_id)
    return await svc.intake_status(db, actor, it)


@router.post("/{intake_id}/continue")
async def continue_intake(intake_id: str, payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context)):
    """Add more evidence in one call: photos (asset_ids / upload_ids / failed), text, and/or a voice note + transcript.
    Each part is its own command; the Idempotency-Key covers the whole continuation."""
    body = dict(payload or {})
    rid = ctx.request_id
    out: dict = {"intake_id": intake_id, "parts": {}}
    steps = 0
    if body.get("asset_ids") or body.get("upload_ids") or body.get("failed") or body.get("clear_failed"):
        sub = ctx if steps == 0 else ctx.child(request_id=f"{rid}:assets" if rid else None)
        res = await dispatch(sub, "intake.add_assets", {"intake_id": intake_id, "asset_ids": body.get("asset_ids") or [],
                                                        "upload_ids": body.get("upload_ids") or [], "failed": body.get("failed") or [],
                                                        "clear_failed": body.get("clear_failed") or []})
        out["parts"]["assets"] = res.to_dict()
        steps += 1
    if body.get("text"):
        sub = ctx if steps == 0 else ctx.child(request_id=f"{rid}:note" if rid else None)
        res = await dispatch(sub, "intake.add_note", {"intake_id": intake_id, "text": body["text"]})
        out["parts"]["note"] = res.to_dict()
        steps += 1
    if body.get("transcript") or body.get("audio_asset_id"):
        sub = ctx if steps == 0 else ctx.child(request_id=f"{rid}:transcript" if rid else None)
        res = await dispatch(sub, "intake.add_transcript", {"intake_id": intake_id, "text": body.get("transcript"),
                                                            "audio_asset_id": body.get("audio_asset_id"), "source": body.get("source") or "typed"})
        out["parts"]["transcript"] = res.to_dict()
        steps += 1
    if steps == 0:
        raise HTTPException(422, "nothing to add: give asset_ids/upload_ids, text, transcript or audio_asset_id")
    it = await svc.load_intake(ctx.db, ctx.actor, intake_id)
    out["status"] = await svc.intake_status(ctx.db, ctx.actor, it)
    return out


@router.post("/{intake_id}/{action}")
async def intake_action(intake_id: str, action: str, payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context)):
    name = ACTIONS.get(action)
    if name is None:
        raise HTTPException(404, f"unknown intake action {action!r}")
    body = dict(payload or {})
    body["intake_id"] = intake_id
    res = await dispatch(ctx, name, body)
    return res.to_dict()
