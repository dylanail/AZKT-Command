"""Manager chat, mission/run progress and the owner capability map (spec §12.3).

    POST /api/agent/chat              JSON, or text/event-stream when the client asks for it
    GET  /api/agent/threads/{role}    the shared conversation (web + Telegram)
    GET  /api/agent/coverage          owner: owner UI command -> Manager tool (spec §10.9, I06)
    GET  /api/agent/status            per role: health and what is actually running
    GET  /api/missions/{id}           mission contract + result
    GET  /api/runs/{id}               run state
    GET  /api/runs/{id}/events        SSE of mission updates from a cursor; losing the connection does not
                                      end the durable mission
    POST /api/runs/{id}/cancel        stop remaining work; completed effects are kept
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db as dbmod
from ..agent import coverage as coverage_mod
from ..agent import manager as manager_mod
from ..agent import runtime as rt
from ..auth.deps import command_context, current_actor
from ..core.errors import Denied, NotFound
from ..db import get_db
from ..domain.actors import Actor
from ..domain.commands import CommandContext
from ..models.runtime import Mission, Run

log = logging.getLogger("azkt.routers.agent")
router = APIRouter(tags=["agent"])

SSE_POLL_SECONDS = 0.5
SSE_MAX_SECONDS = 120


def _sse(event: str, data) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode()


async def _mission_for(db: AsyncSession, actor: Actor, mission_id: str) -> Mission:
    m = await db.get(Mission, mission_id)
    if m is None:
        raise NotFound("mission not found")
    _assert_can_read(actor, m)
    return m


def _assert_can_read(actor: Actor, m: Mission) -> None:
    """An unauthorized mission id is not confirmed: the caller gets 'not found', not 'forbidden'."""
    if actor.kind == "user" and actor.role == "owner":
        return
    if actor.kind == "external":
        if m.client_id == actor.client_id:
            return
        raise NotFound("mission not found")
    if m.responsible_user_id and m.responsible_user_id == actor.user_id:
        return
    raise NotFound("mission not found")


# ── chat ─────────────────────────────────────────────────────────────────────
@router.post("/api/agent/chat")
async def chat(request: Request, payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context)):
    message = (payload.get("message") or "").strip()
    role = payload.get("role") or "manager"
    context = payload.get("context") or {}
    attachments = payload.get("attachments") or []
    request_id = payload.get("request_id") or ctx.request_id
    if not isinstance(context, dict):
        raise HTTPException(422, "context must be an object")
    if not isinstance(attachments, list):
        raise HTTPException(422, "attachments must be a list of asset ids")

    wants_stream = "text/event-stream" in (request.headers.get("accept") or "")
    if not wants_stream:
        out = await manager_mod.handle_message(ctx.db, ctx.actor, message, channel="web", context=context,
                                               attachments=attachments, role=role, request_id=request_id)
        return out

    actor, chan = ctx.actor, "web"

    async def gen():
        async with dbmod.SessionLocal() as db:
            try:
                async for ev in manager_mod.stream_message(db, actor, message, channel=chan, context=context,
                                                           attachments=attachments, role=role,
                                                           request_id=request_id):
                    yield _sse(ev["event"], ev["data"])
            except Exception as e:  # noqa: BLE001
                log.exception("chat stream failed")
                yield _sse("error", {"error": f"{type(e).__name__}: {str(e)[:300]}"})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/agent/threads/{role}")
async def get_thread(role: str, limit: int = Query(50, ge=1, le=200), actor: Actor = Depends(current_actor),
                     db: AsyncSession = Depends(get_db)):
    return await manager_mod.thread(db, actor, role, limit=limit)


@router.get("/api/agent/coverage")
async def coverage(actor: Actor = Depends(current_actor)):
    """The capability map. The owner sees the whole map; anyone else sees it annotated for their own grant."""
    if actor.kind != "user":
        raise HTTPException(403, "signed-in access required")
    return coverage_mod.for_actor(actor)


@router.get("/api/agent/status")
async def status(actor: Actor = Depends(current_actor), db: AsyncSession = Depends(get_db)):
    return await manager_mod.status(db, actor)


# ── missions and runs ────────────────────────────────────────────────────────
@router.get("/api/missions/{mission_id}")
async def get_mission(mission_id: str, cursor: int = Query(0, ge=0), actor: Actor = Depends(current_actor),
                      db: AsyncSession = Depends(get_db)):
    m = await _mission_for(db, actor, mission_id)
    from sqlalchemy import select
    runs = (await db.execute(select(Run).where(Run.mission_id == m.id).order_by(Run.created_at))).scalars().all()
    return {"mission": rt.brief(m), "updates": rt.updates_since(m, cursor),
            "runs": [rt.run_brief(r) for r in runs]}


@router.get("/api/runs/{run_id}")
async def get_run(run_id: str, actor: Actor = Depends(current_actor), db: AsyncSession = Depends(get_db)):
    r = await db.get(Run, run_id)
    if r is None:
        raise NotFound("run not found")
    m = await _mission_for(db, actor, r.mission_id)
    from sqlalchemy import select
    from ..models.runtime import RunStep
    steps = (await db.execute(select(RunStep).where(RunStep.run_id == r.id).order_by(RunStep.seq,
                                                                                     RunStep.created_at))).scalars().all()
    return {"run": rt.run_brief(r), "mission": rt.brief(m),
            "steps": [{"seq": s.seq, "kind": s.kind, "tool": s.tool_name, "ok": s.ok, "decision": s.decision,
                       "started_at": s.started_at.isoformat() if s.started_at else None,
                       "output": s.output} for s in steps]}


@router.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, cursor: int = Query(0, ge=0), actor: Actor = Depends(current_actor),
                     db: AsyncSession = Depends(get_db)):
    """Authenticated progress stream. The mission is durable: closing this stream does not stop it."""
    r = await db.get(Run, run_id)
    if r is None:
        raise NotFound("run not found")
    mission_id = r.mission_id
    await _mission_for(db, actor, mission_id)

    async def gen():
        seen = cursor
        started = datetime.now(timezone.utc)
        async with dbmod.SessionLocal() as s:
            yield _sse("open", {"run_id": run_id, "mission_id": mission_id, "cursor": seen})
            while True:
                m = await s.get(Mission, mission_id)
                if m is not None:
                    s.expire(m)
                    m = await s.get(Mission, mission_id)
                if m is None:
                    yield _sse("error", {"error": "mission disappeared"})
                    return
                for u in rt.updates_since(m, seen):
                    seen = int(u.get("seq") or seen)
                    yield _sse("update", u)
                run = await s.get(Run, run_id)
                if run is not None:
                    s.expire(run)
                    run = await s.get(Run, run_id)
                if m.status in rt.TERMINAL_MISSION or (run is not None and run.status in
                                                       ("succeeded", "failed", "cancelled", "needs_information",
                                                        "waiting_approval", "waiting_external", "waiting_until")):
                    yield _sse("done", {"mission": rt.brief(m), "run": rt.run_brief(run) if run else None,
                                        "cursor": seen})
                    return
                if (datetime.now(timezone.utc) - started).total_seconds() > SSE_MAX_SECONDS:
                    yield _sse("idle", {"cursor": seen, "note": "still running; reconnect with this cursor"})
                    return
                await asyncio.sleep(SSE_POLL_SECONDS)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str, payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context)):
    r = await ctx.db.get(Run, run_id)
    if r is None:
        raise NotFound("run not found")
    m = await _mission_for(ctx.db, ctx.actor, r.mission_id)
    if ctx.actor.kind == "user" and ctx.actor.role != "owner" and m.responsible_user_id != ctx.actor.user_id:
        raise Denied("only the owner or the person the mission acts for can cancel it")
    return await rt.cancel_mission(ctx.db, ctx.actor, m.id,
                                   reason=(payload or {}).get("reason") or "cancelled from the run view")


@router.post("/api/missions/{mission_id}/cancel")
async def cancel_mission(mission_id: str, payload: dict = Body(default={}),
                         ctx: CommandContext = Depends(command_context)):
    m = await _mission_for(ctx.db, ctx.actor, mission_id)
    if ctx.actor.kind == "user" and ctx.actor.role != "owner" and m.responsible_user_id != ctx.actor.user_id:
        raise Denied("only the owner or the person the mission acts for can cancel it")
    return await rt.cancel_mission(ctx.db, ctx.actor, m.id,
                                   reason=(payload or {}).get("reason") or "cancelled by the owner")
