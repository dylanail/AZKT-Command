"""Import requests API (spec §2.3 Import requests, §8.1, §5.2 IRQ side). Reads are projections with the
structured Active Search gate list, requirement tiers, per-request candidate checks, translations, bids and
activity; every write dispatches a services/sourcing.py command. Budget (money) obeys costs.read; auction
and deadline instants are shown in UTC, Tokyo and Phoenix."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import command_context, require
from ..core.errors import NotFound, ValidationFailed
from ..core.time import PHOENIX
from ..db import get_db
from ..domain.access import can_see_costs, can_see_finance_status, sanitize_money, visible_vehicle_ids
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..models.contacts import Contact
from ..models.runtime import ActivityEntry, Approval
from ..models.sourcing import IR_LIFECYCLE, Bid, Candidate, CandidateMatch, ImportRequest, Translation
from ..models.tasks import Task
from ..models.vehicles import Vehicle
from ..services import requirements as reqs
from ..services.sourcing import (OPEN_STATUSES, serialize_bid, serialize_candidate, serialize_match, serialize_request,
                                 serialize_translation)

router = APIRouter(prefix="/api/import-requests", tags=["import-requests"])

MONEY_KEYS = ("budget_amount", "budget_currency")
BID_MONEY_KEYS = ("max_amount", "fx_estimate", "packet", "result", "result_evidence")  # winning price is cost detail too
AMOUNT_KEYS = ("amount", "currency", "remaining", "overpaid", "required", "received")
COMMANDS = {
    "update": "import_requests.update", "revise-requirements": "import_requests.revise_requirements",
    "attach-opportunity": "import_requests.attach_opportunity", "set-agreement": "import_requests.set_agreement",
    "set-deposit-rule": "import_requests.set_deposit_rule", "confirm-deposit": "import_requests.confirm_deposit",
    "pause": "import_requests.pause", "resume": "import_requests.resume", "record-purchase": "import_requests.record_purchase",
    "close": "import_requests.close",
}
DT_KEYS = ("next_check_at", "signed_at", "confirmed_at")


def _to_utc(value, tz: str = PHOENIX):
    """ISO with offset -> UTC; a naive value is interpreted in `tz` (never stored naive)."""
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
            out[k] = _to_utc(out[k], out.get("timezone") or PHOENIX)
    return out


def _strip_amounts(obj):
    """Blank amount-like keys inside evidence dicts (recursively) while keeping ids, sources and statuses."""
    if isinstance(obj, dict):
        return {k: (None if k in AMOUNT_KEYS else _strip_amounts(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_amounts(x) for x in obj]
    return obj


def _request_view(actor: Actor, r: ImportRequest) -> dict:
    """Budget and purchase price obey costs.read; the deposit amounts are finance status (finance.status or costs.read).
    Statuses, evidence references and the gate decision stay visible to everyone with requests.read."""
    d = serialize_request(r)
    if not can_see_costs(actor):
        d = sanitize_money(actor, d, MONEY_KEYS)
        d["purchase_evidence"] = _strip_amounts(d["purchase_evidence"])
    if not can_see_finance_status(actor):
        d["deposit_rule"] = _strip_amounts(d["deposit_rule"])
        d["deposit_evidence"] = _strip_amounts(d["deposit_evidence"])
        gate = d["active_search_gate"]
        gate["gates"] = [({**g, "reason": (f"Deposit {g['status']}" if not g["ok"] and g["status"] != "unset" else g["reason"])}
                          if g["key"] == "deposit" else g) for g in gate["gates"]]
        gate["reasons"] = [g["reason"] for g in gate["gates"] if not g["ok"]]
        d["money_hidden"] = True
    return d


def _bid_view(actor: Actor, b: Bid) -> dict:
    d = serialize_bid(b)
    return d if can_see_costs(actor) else sanitize_money(actor, d, BID_MONEY_KEYS)  # deadline/identity stay visible


async def _scope_clause(db: AsyncSession, actor: Actor):
    """Record scope: a record-limited client / assigned-scope person only sees requests whose purchased vehicle is visible."""
    limit = await visible_vehicle_ids(db, actor)
    if limit is None:
        return None
    return ImportRequest.purchased_vehicle_id.in_(list(limit))


async def _load(db: AsyncSession, actor: Actor, request_id: str) -> ImportRequest:
    clauses = [ImportRequest.id == request_id]
    scope = await _scope_clause(db, actor)
    if scope is not None:
        clauses.append(scope)
    r = (await db.execute(select(ImportRequest).where(*clauses))).scalar_one_or_none()
    if r is None:
        raise NotFound("import request not found")
    return r


def _contact_brief(c: Contact | None) -> dict | None:
    if c is None:
        return None
    return {"id": c.id, "name": c.name, "company": c.company, "status": c.status}


def _vehicle_brief(v: Vehicle | None) -> dict | None:
    if v is None:
        return None
    return {"id": v.id, "title": v.title, "stock_no": v.stock_no, "logistics_state": v.logistics_state,
            "recon_state": v.recon_state, "commercial_state": v.commercial_state, "health": v.health,
            "photo": None if v.hero_asset_id else "No photo yet", "hero_asset_id": v.hero_asset_id}


async def _candidate_rows(db: AsyncSession, r: ImportRequest) -> list[dict]:
    """Per-request candidate view: the match (per-requirement outcomes, private draft) + candidate identity/deadline."""
    matches = (await db.execute(select(CandidateMatch).where(CandidateMatch.import_request_id == r.id)
                                .order_by(CandidateMatch.created_at))).scalars().all()
    cids = {m.candidate_id for m in matches}
    cands = {c.id: c for c in (await db.execute(select(Candidate).where(Candidate.id.in_(cids)))).scalars().all()} if cids else {}
    out = []
    for m in matches:
        c = cands.get(m.candidate_id)
        row = serialize_match(m, include_draft=True)
        row["candidate"] = serialize_candidate(c) if c else None
        row["checks"] = [{"key": o["key"], "tier": o["tier"], "text": o.get("text"), "result": o["result"],
                          "evidence": o.get("evidence") or {}} for o in (m.outcomes or [])]
        row["decision"] = ("Blocked" if m.mandatory_fail else ("Needs review" if m.mandatory_unknown or m.stale else "Allowed"))
        out.append(row)
    return out


@router.get("")
async def list_requests(status: str | None = None, paused: bool | None = None, contact_id: str | None = None,
                        q: str | None = None, include_closed: bool = False, limit: int = Query(100, ge=1, le=500),
                        offset: int = Query(0, ge=0), actor: Actor = Depends(require("requests.read")),
                        db: AsyncSession = Depends(get_db)):
    clauses: list = []
    scope = await _scope_clause(db, actor)
    if scope is not None:
        clauses.append(scope)
    if status:
        if status not in IR_LIFECYCLE:
            raise HTTPException(422, f"status must be one of {IR_LIFECYCLE}")
        clauses.append(ImportRequest.status == status)
    elif not include_closed:
        clauses.append(ImportRequest.status.in_(OPEN_STATUSES + ("purchased",)))
    if paused is not None:
        clauses.append(ImportRequest.paused.is_(paused))
    if contact_id:
        clauses.append(ImportRequest.contact_id == contact_id)
    if q:
        clauses.append(or_(ImportRequest.title.ilike(f"%{q}%"), ImportRequest.notes.ilike(f"%{q}%")))
    total = (await db.execute(select(func.count()).select_from(ImportRequest).where(*clauses))).scalar_one()
    rows = (await db.execute(select(ImportRequest).where(*clauses).order_by(ImportRequest.updated_at.desc())
                             .limit(limit).offset(offset))).scalars().all()
    ids = [r.id for r in rows]
    counts: dict[str, dict] = {i: {"candidates": 0, "bid_ready": 0, "rejected": 0} for i in ids}
    if ids:
        for cid, rid, ready, st in (await db.execute(select(CandidateMatch.candidate_id, CandidateMatch.import_request_id,
                                                            CandidateMatch.bid_ready, CandidateMatch.status)
                                                     .where(CandidateMatch.import_request_id.in_(ids)))).all():
            counts[rid]["candidates"] += 1
            counts[rid]["bid_ready"] += 1 if ready else 0
            counts[rid]["rejected"] += 1 if st == "rejected" else 0
    contacts = {c.id: c for c in (await db.execute(select(Contact).where(Contact.id.in_({r.contact_id for r in rows})))).scalars().all()} if rows else {}
    items = []
    for r in rows:
        d = _request_view(actor, r)
        d["contact"] = _contact_brief(contacts.get(r.contact_id))
        d["candidate_counts"] = counts[r.id]
        d["lifecycle_index"] = IR_LIFECYCLE.index(r.status) if r.status in IR_LIFECYCLE else None
        items.append(d)
    return {"items": items, "total": int(total), "lifecycle": list(IR_LIFECYCLE)}


@router.get("/{request_id}")
async def get_request(request_id: str, actor: Actor = Depends(require("requests.read")), db: AsyncSession = Depends(get_db)):
    r = await _load(db, actor, request_id)
    contact = await db.get(Contact, r.contact_id)
    vehicle = await db.get(Vehicle, r.purchased_vehicle_id) if r.purchased_vehicle_id else None
    candidates = await _candidate_rows(db, r)
    tids = {c["translation_id"] for c in candidates if c.get("translation_id")}
    translations = (await db.execute(select(Translation).where(Translation.id.in_(tids)))).scalars().all() if tids else []
    bids = (await db.execute(select(Bid).where(Bid.import_request_id == r.id).order_by(Bid.created_at))).scalars().all()
    entity_ids = [r.id] + [c["id"] for c in candidates] + [b.id for b in bids]
    acts = (await db.execute(select(ActivityEntry).where(ActivityEntry.entity_id.in_(entity_ids),
                                                          ActivityEntry.visibility.in_(("all",) + (("owner", "finance") if can_see_costs(actor) else ())))
                             .order_by(ActivityEntry.at.desc()).limit(200))).scalars().all()
    tasks = (await db.execute(select(Task).where(Task.import_request_id == r.id, Task.status.notin_(("completed", "cancelled")))
                              .order_by(Task.due_at.asc().nulls_last()))).scalars().all()
    approvals = (await db.execute(select(Approval).where(Approval.entity_kind == "bid", Approval.entity_id.in_([b.id for b in bids]))
                                  .order_by(Approval.created_at.desc()))).scalars().all() if bids else []
    opportunity = None
    if r.opportunity_id:
        from ..models.sales import Opportunity
        o = await db.get(Opportunity, r.opportunity_id)
        if o is not None:
            opportunity = {"id": o.id, "pipeline": o.pipeline, "stage": o.stage, "converted_kind": o.converted_kind,
                           "converted_id": o.converted_id, "deposit_confirmed_at": o.deposit_confirmed_at.isoformat() if o.deposit_confirmed_at else None}
    d = _request_view(actor, r)
    d["requirement_tiers"] = reqs.tiers(r.requirements or [])
    d["gates"] = d["active_search_gate"]  # same structured decision, already money-sanitized for this actor
    return {
        "request": d, "contact": _contact_brief(contact), "purchased_vehicle": _vehicle_brief(vehicle), "opportunity": opportunity,
        "candidates": candidates, "candidate_ranking": reqs.rank([c for c in candidates if not c["mandatory_fail"]]),
        "translations": [serialize_translation(t) for t in translations], "bids": [_bid_view(actor, b) for b in bids],
        "bid_approvals": [{"id": a.id, "status": a.status, "title": a.title, "version": a.approval_version, "entity_id": a.entity_id,
                           "invalidated_reason": a.invalidated_reason, "review_path": a.review_path} for a in approvals],
        "tasks": [{"id": t.id, "title": t.title, "status": t.status, "due_at": t.due_at.isoformat() if t.due_at else None,
                   "owner_user_id": t.owner_user_id} for t in tasks],
        "activity": [{"id": a.id, "at": a.at.isoformat(), "what": a.what, "kind": a.kind, "state": a.state, "entity_kind": a.entity_kind,
                      "entity_id": a.entity_id, "exception": a.exception, "actor": a.actor, "sources": a.sources} for a in acts],
        "inbox": {"conversations": [], "note": "Linked inbox conversations appear when the inbox domain is connected"},
    }


@router.get("/{request_id}/candidates")
async def request_candidates(request_id: str, actor: Actor = Depends(require("requests.read")), db: AsyncSession = Depends(get_db)):
    r = await _load(db, actor, request_id)
    rows = await _candidate_rows(db, r)
    return {"items": rows, "total": len(rows), "ranking": reqs.rank([c for c in rows if not c["mandatory_fail"]]),
            "exclusions": list(r.exclusions or [])}


@router.post("")
async def create_request(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    res = await dispatch(ctx, "import_requests.create", _normalize(payload))
    return res.to_dict()


@router.post("/{request_id}/match")
async def match_request(request_id: str, payload: dict | None = Body(None), ctx: CommandContext = Depends(command_context)):
    res = await dispatch(ctx, "candidates.match", {**(payload or {}), "request_id": request_id})
    return res.to_dict()


@router.post("/{request_id}/{action}")
async def request_action(request_id: str, action: str, payload: dict | None = Body(None),
                         ctx: CommandContext = Depends(command_context)):
    name = COMMANDS.get(action)
    if name is None:
        raise HTTPException(404, f"unknown action {action}; one of {sorted(COMMANDS)}")
    res = await dispatch(ctx, name, {**_normalize(payload or {}), "request_id": request_id})
    return res.to_dict()
