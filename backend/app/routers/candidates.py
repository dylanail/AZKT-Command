"""Candidate API (spec §2.3 Candidate detail, §8.2). Detail shows auction identity/snapshot/deadline in UTC + Tokyo +
Phoenix, per-request Pass/Fail/Unknown checks, translation versions, the per-request scoped buyer draft and the exact
bid packet/result. Writes dispatch services/sourcing.py commands (ingest, match, translations, bids). Bid money
(max JPY, FX estimate) obeys costs.read."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import command_context, require
from ..core.errors import NotFound, ValidationFailed
from ..core.time import TOKYO
from ..db import get_db
from ..domain.access import can_see_costs, sanitize_money, visible_vehicle_ids
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..models.runtime import Approval, ExternalAction
from ..models.sourcing import Bid, Candidate, CandidateMatch, ImportRequest, Translation
from ..services.sourcing import bid_gate, serialize_bid, serialize_candidate, serialize_match, serialize_translation

router = APIRouter(prefix="/api/candidates", tags=["candidates"])

BID_MONEY_KEYS = ("max_amount", "fx_estimate", "packet", "result", "result_evidence")  # winning price is cost detail too
MATCH_COMMANDS = {"pass": "candidates.pass", "interest": "candidates.record_interest",
                  "prepare-buyer-message": "candidates.prepare_buyer_message", "mark-sent": "candidates.mark_buyer_message_sent"}
TRANSLATION_COMMANDS = {"detected": "translations.record_detected", "complete": "translations.mark_complete",
                        "revise": "translations.revise"}
BID_COMMANDS = {"submit-for-approval": "bids.submit_for_approval", "record-submitted": "bids.record_submitted",
                "record-result": "bids.record_result", "cancel": "bids.cancel"}
DT_KEYS = ("modified_at", "sent_at", "since")


def _to_utc(value, tz: str = TOKYO):
    if value is None or value == "" or isinstance(value, datetime):
        return value
    from zoneinfo import ZoneInfo
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationFailed(f"invalid datetime {value!r}; use ISO 8601")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(timezone.utc).isoformat()


def _normalize(payload: dict) -> dict:
    out = dict(payload or {})
    for k in DT_KEYS:
        if k in out:
            out[k] = _to_utc(out[k])
    return out


def _bid_view(actor: Actor, b: Bid, gate: list[str] | None = None) -> dict:
    d = serialize_bid(b)
    if not can_see_costs(actor):
        d = sanitize_money(actor, d, BID_MONEY_KEYS)  # deadline/identity stay visible
    if gate is not None:
        d["gate"] = {"decision": "Blocked" if gate else "Allowed", "reasons": gate}
    return d


async def _scope(db: AsyncSession, actor: Actor) -> tuple[set[str] | None, set[str]]:
    """Record scope for a scope=assigned person / record-limited client: (visible vehicle ids or None, visible request ids).
    Such an actor only sees candidates tied to a visible purchased vehicle or matched to a request whose purchase is visible."""
    limit = await visible_vehicle_ids(db, actor)
    if limit is None:
        return None, set()
    rids = {r for (r,) in (await db.execute(select(ImportRequest.id).where(ImportRequest.purchased_vehicle_id.in_(list(limit))))).all()} if limit else set()
    return limit, rids


def _scope_clause(limit: set[str] | None, rids: set[str]):
    if limit is None:
        return None
    return or_(Candidate.vehicle_id.in_(list(limit)),
               Candidate.id.in_(select(CandidateMatch.candidate_id).where(CandidateMatch.import_request_id.in_(list(rids)))))


async def _load(db: AsyncSession, actor: Actor, candidate_id: str) -> tuple[Candidate, set[str] | None, set[str]]:
    limit, rids = await _scope(db, actor)
    clauses = [Candidate.id == candidate_id]
    sc = _scope_clause(limit, rids)
    if sc is not None:
        clauses.append(sc)
    c = (await db.execute(select(Candidate).where(*clauses))).scalar_one_or_none()
    if c is None:
        raise NotFound("candidate not found")
    return c, limit, rids


@router.get("")
async def list_candidates(status: str | None = None, request_id: str | None = None, auction_house: str | None = None,
                          lot_no: str | None = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                          actor: Actor = Depends(require("requests.read")), db: AsyncSession = Depends(get_db)):
    clauses: list = []
    sc = _scope_clause(*(await _scope(db, actor)))
    if sc is not None:
        clauses.append(sc)
    if status:
        clauses.append(Candidate.status == status)
    if auction_house:
        clauses.append(Candidate.auction_house == auction_house)
    if lot_no:
        clauses.append(Candidate.lot_no == lot_no)
    if request_id:
        sub = select(CandidateMatch.candidate_id).where(CandidateMatch.import_request_id == request_id)
        clauses.append(Candidate.id.in_(sub))
    total = (await db.execute(select(func.count()).select_from(Candidate).where(*clauses))).scalar_one()
    rows = (await db.execute(select(Candidate).where(*clauses).order_by(Candidate.auction_at.asc().nulls_last(), Candidate.created_at)
                             .limit(limit).offset(offset))).scalars().all()
    ids = [c.id for c in rows]
    summary: dict[str, dict] = {i: {"requests": 0, "bid_ready": 0, "rejected": 0} for i in ids}
    if ids:
        for cid, ready, st in (await db.execute(select(CandidateMatch.candidate_id, CandidateMatch.bid_ready, CandidateMatch.status)
                                                .where(CandidateMatch.candidate_id.in_(ids)))).all():
            summary[cid]["requests"] += 1
            summary[cid]["bid_ready"] += 1 if ready else 0
            summary[cid]["rejected"] += 1 if st == "rejected" else 0
    items = []
    for c in rows:
        d = serialize_candidate(c)
        d["match_summary"] = summary[c.id]
        items.append(d)
    return {"items": items, "total": int(total)}


@router.get("/{candidate_id}")
async def get_candidate(candidate_id: str, actor: Actor = Depends(require("requests.read")), db: AsyncSession = Depends(get_db)):
    c, limit_ids, visible_rids = await _load(db, actor, candidate_id)
    matches = (await db.execute(select(CandidateMatch).where(CandidateMatch.candidate_id == c.id).order_by(CandidateMatch.created_at))).scalars().all()
    if limit_ids is not None:  # a record-limited actor sees only the matches of requests within their scope
        matches = [m for m in matches if m.import_request_id in visible_rids]
    rids = {m.import_request_id for m in matches}
    reqs_by_id = {r.id: r for r in (await db.execute(select(ImportRequest).where(ImportRequest.id.in_(rids)))).scalars().all()} if rids else {}
    translations = (await db.execute(select(Translation).where(Translation.candidate_id == c.id).order_by(Translation.created_at))).scalars().all()
    action_ids = [t.request_action_id for t in translations if t.request_action_id]
    actions = {a.id: a for a in (await db.execute(select(ExternalAction).where(ExternalAction.id.in_(action_ids)))).scalars().all()} if action_ids else {}
    bids = (await db.execute(select(Bid).where(Bid.candidate_id == c.id).order_by(Bid.created_at))).scalars().all()
    approvals = (await db.execute(select(Approval).where(Approval.entity_kind == "bid", Approval.entity_id.in_([b.id for b in bids]))
                                  .order_by(Approval.created_at.desc()))).scalars().all() if bids else []
    checks = []
    for m in matches:
        r = reqs_by_id.get(m.import_request_id)
        row = serialize_match(m, include_draft=True)
        # scoped: the request's own title/requirement version only — never another buyer's identity, budget or terms
        row["request"] = {"id": m.import_request_id, "title": r.title if r else None, "status": r.status if r else None,
                          "requirements_version": r.requirements_version if r else None}
        row["checks"] = [{"key": o["key"], "tier": o["tier"], "text": o.get("text"), "result": o["result"], "evidence": o.get("evidence") or {}}
                         for o in (m.outcomes or [])]
        row["decision"] = "Blocked" if m.mandatory_fail else ("Needs review" if (m.mandatory_unknown or m.stale) else "Allowed")
        checks.append(row)
    bid_views = []
    ctx = CommandContext(db=db, actor=actor)  # read-only context for the gate evaluation
    for b in bids:
        gate = await bid_gate(ctx, b) if b.status in ("draft", "pending_approval", "approved") else None
        bid_views.append(_bid_view(actor, b, gate))
    return {
        "candidate": serialize_candidate(c),
        "checks": checks,
        "translations": [serialize_translation(t, actions.get(t.request_action_id)) for t in translations],
        "current_translation": next((serialize_translation(t) for t in reversed(translations) if t.status != "invalidated"), None),
        "bids": bid_views,
        "bid_approvals": [{"id": a.id, "status": a.status, "title": a.title, "version": a.approval_version, "entity_id": a.entity_id,
                           "invalidated_reason": a.invalidated_reason, "review_path": a.review_path,
                           "expires_at": a.expires_at.isoformat() if a.expires_at else None} for a in approvals],
        "vehicle_id": c.vehicle_id,
    }


@router.post("/ingest")
async def ingest(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    res = await dispatch(ctx, "candidates.ingest", _normalize(payload))
    return res.to_dict()


@router.post("/match")
async def match(payload: dict | None = Body(None), ctx: CommandContext = Depends(command_context)):
    res = await dispatch(ctx, "candidates.match", payload or {})
    return res.to_dict()


@router.post("/matches/{match_id}/{action}")
async def match_action(match_id: str, action: str, payload: dict | None = Body(None), ctx: CommandContext = Depends(command_context)):
    name = MATCH_COMMANDS.get(action)
    if name is None:
        raise HTTPException(404, f"unknown action {action}; one of {sorted(MATCH_COMMANDS)}")
    res = await dispatch(ctx, name, {**_normalize(payload or {}), "match_id": match_id})
    return res.to_dict()


@router.post("/translations/{translation_id}/{action}")
async def translation_action(translation_id: str, action: str, payload: dict | None = Body(None),
                             ctx: CommandContext = Depends(command_context)):
    name = TRANSLATION_COMMANDS.get(action)
    if name is None:
        raise HTTPException(404, f"unknown action {action}; one of {sorted(TRANSLATION_COMMANDS)}")
    res = await dispatch(ctx, name, {**_normalize(payload or {}), "translation_id": translation_id})
    return res.to_dict()


@router.post("/bids/{bid_id}/{action}")
async def bid_action(bid_id: str, action: str, payload: dict | None = Body(None), ctx: CommandContext = Depends(command_context)):
    name = BID_COMMANDS.get(action)
    if name is None:
        raise HTTPException(404, f"unknown action {action}; one of {sorted(BID_COMMANDS)}")
    res = await dispatch(ctx, name, {**_normalize(payload or {}), "bid_id": bid_id})
    return res.to_dict()


@router.post("/{candidate_id}/translations/request")
async def request_translation(candidate_id: str, payload: dict | None = Body(None), ctx: CommandContext = Depends(command_context)):
    res = await dispatch(ctx, "translations.request", {**(payload or {}), "candidate_id": candidate_id})
    return res.to_dict()


@router.post("/{candidate_id}/bids/prepare")
async def prepare_bid(candidate_id: str, payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    res = await dispatch(ctx, "bids.prepare", {**(payload or {}), "candidate_id": candidate_id})
    return res.to_dict()


@router.post("/{candidate_id}/match")
async def match_one(candidate_id: str, payload: dict | None = Body(None), ctx: CommandContext = Depends(command_context)):
    res = await dispatch(ctx, "candidates.match", {**(payload or {}), "candidate_id": candidate_id})
    return res.to_dict()
