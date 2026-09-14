"""Approvals queue and exact detail over services/approvals.py (spec §2.3 Approvals, §11.4).

- The queue and detail require the `approve` permission (owner). Non-owners get a bare 403.
- Approve/decline/edit are POSTs that dispatch the owner-only commands; approve/decline require
  `expected_version` so a stale review can never authorize a newer payload.
- GET /api/approvals/{id}/review is the deep-link target for email/Telegram. It renders the same
  exact detail for a signed-in owner and never mutates (invariant 11); a `token` query parameter is
  accepted only so scanners/links don't 422, and it is ignored for authorization.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import command_context, current_actor
from ..db import get_db
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch, spec_for
from ..domain.policy import has_perm
from ..models.runtime import APPROVAL_STATES, Approval, ExternalAction
from ..services import approvals as approvals_svc

router = APIRouter(prefix="/api/approvals", tags=["approvals"])

ACTIVE = ("approved", "queued", "executing")


async def _owner(actor: Actor = Depends(current_actor)) -> Actor:
    if actor.kind != "user" or not has_perm(actor, "approve"):
        raise HTTPException(403, "not allowed")
    return actor


def _row(a: Approval) -> dict:
    return {"id": a.id, "kind": a.kind, "title": a.title, "status": a.status, "version": a.approval_version,
            "command_name": a.command_name, "consequence": a.consequence or {}, "targets": a.targets or {},
            "entity_kind": a.entity_kind, "entity_id": a.entity_id, "requested_by": a.requested_by or {},
            "expires_at": a.expires_at.isoformat() if a.expires_at else None,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "decided_at": a.decided_at.isoformat() if a.decided_at else None, "review_path": a.review_path,
            "mission_id": a.mission_id, "invalidated_reason": a.invalidated_reason,
            "superseded_by_id": a.superseded_by_id, "supersedes_id": a.supersedes_id}


async def _detail(db: AsyncSession, a: Approval) -> dict:
    d = approvals_svc._brief(a)
    spec = None
    try:
        spec = spec_for(a.command_name)
    except Exception:  # noqa: BLE001
        pass
    d.update({
        "action_class": spec.action_class if spec else None, "description": spec.description if spec else a.summary,
        "recommendation": a.recommendation, "conditions": a.conditions or {}, "record_versions": a.record_versions or {},
        "executor": a.executor, "decision_note": a.decision_note, "mission_id": a.mission_id, "run_id": a.run_id,
        "invalidation": {"invalidated": a.status == "invalidated", "reason": a.invalidated_reason,
                         "superseded_by_id": a.superseded_by_id, "supersedes_id": a.supersedes_id},
        "external_action": None,
        "can_decide": a.status == "pending",
    })
    if a.external_action_id:
        act = await db.get(ExternalAction, a.external_action_id)
        if act is not None:
            d["external_action"] = {"id": act.id, "state": act.state, "provider": act.provider, "provider_ref": act.provider_ref,
                                    "attempts": act.attempts, "error": act.error, "receipt": act.receipt or {},
                                    "executed_at": act.executed_at.isoformat() if act.executed_at else None}
    if a.supersedes_id:
        prev = await db.get(Approval, a.supersedes_id)
        if prev is not None:
            d["previous_version"] = _row(prev)
    return d


@router.get("")
async def list_approvals(status: str | None = "pending", kind: str | None = None, entity_kind: str | None = None,
                         entity_id: str | None = None, mission_id: str | None = None,
                         limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                         actor: Actor = Depends(_owner), db: AsyncSession = Depends(get_db)):
    stmt = select(Approval)
    if status and status != "all":
        wanted = [s.strip() for s in status.split(",") if s.strip()]
        bad = [s for s in wanted if s not in APPROVAL_STATES]
        if bad:
            raise HTTPException(422, f"unknown status {bad}")
        stmt = stmt.where(Approval.status.in_(wanted))
    if kind:
        stmt = stmt.where(Approval.kind == kind)
    if entity_kind:
        stmt = stmt.where(Approval.entity_kind == entity_kind)
    if entity_id:
        stmt = stmt.where(Approval.entity_id == entity_id)
    if mission_id:
        stmt = stmt.where(Approval.mission_id == mission_id)
    total = int(await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (await db.execute(stmt.order_by(Approval.created_at.desc()).offset(offset).limit(limit))).scalars().all()
    return {"items": [_row(a) for a in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/summary")
async def summary(actor: Actor = Depends(_owner), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Approval.status, func.count()).group_by(Approval.status))).all()
    by = {s: int(n) for s, n in rows}
    return {"pending": by.get("pending", 0), "executing": sum(by.get(s, 0) for s in ACTIVE),
            "unknown": by.get("result_unknown", 0), "failed": by.get("failed", 0), "by_status": by}


@router.get("/{approval_id}")
async def get_approval(approval_id: str, actor: Actor = Depends(_owner), db: AsyncSession = Depends(get_db)):
    a = await db.get(Approval, approval_id)
    if a is None:
        raise HTTPException(404, "approval not found")
    return await _detail(db, a)


@router.get("/{approval_id}/review")
async def review(approval_id: str, token: str | None = Query(None), actor: Actor = Depends(_owner),
                 db: AsyncSession = Depends(get_db)):
    """Deep-link target. Renders only; nothing here mutates (spec §5.4, §11.4, invariant 11)."""
    a = await db.get(Approval, approval_id)
    if a is None:
        raise HTTPException(404, "approval not found")
    d = await _detail(db, a)
    d["mutation"] = "none"
    d["note"] = "Opening this link changes nothing. Approve, edit or decline with a signed-in POST."
    d["actions"] = {"approve": f"/api/approvals/{a.id}/approve", "decline": f"/api/approvals/{a.id}/decline",
                    "edit": f"/api/approvals/{a.id}/edit", "expected_version": a.approval_version}
    return d


class _Decision(BaseModel):
    expected_version: int = Field(ge=1)
    note: str | None = None


@router.post("/{approval_id}/approve")
async def approve(approval_id: str, body: _Decision, actor: Actor = Depends(_owner), ctx: CommandContext = Depends(command_context)):
    return (await dispatch(ctx, "approvals.approve", {"approval_id": approval_id, **body.model_dump()})).to_dict()


@router.post("/{approval_id}/decline")
async def decline(approval_id: str, body: _Decision, actor: Actor = Depends(_owner), ctx: CommandContext = Depends(command_context)):
    return (await dispatch(ctx, "approvals.decline", {"approval_id": approval_id, **body.model_dump()})).to_dict()


class _Edit(BaseModel):
    payload: dict
    note: str | None = None


@router.post("/{approval_id}/edit")
async def edit(approval_id: str, body: _Edit, actor: Actor = Depends(_owner), ctx: CommandContext = Depends(command_context)):
    return (await dispatch(ctx, "approvals.edit", {"approval_id": approval_id, **body.model_dump()})).to_dict()
