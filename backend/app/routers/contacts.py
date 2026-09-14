"""Contacts API (spec §2.3 Contacts, §3.3). Reads apply record scope and hide money; writes dispatch
services/contacts.py commands. `/resolve` exposes the shared matching service to owner/manager."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import command_context, require
from ..core.errors import NotFound
from ..db import get_db
from ..domain.access import sanitize_money, visible_vehicle_ids
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..domain.policy import has_perm
from ..models.contacts import Contact, ContactMerge
from ..models.sales import Opportunity
from ..models.tasks import Commitment, Task
from ..models.vehicles import Vehicle
from ..services.contacts import TAB_ROLES, identities_of, serialize_contact, serialize_merge
from ..services.matching import resolve_contact
from ..services.sales import STAGE_LABELS, serialize_opportunity, vehicle_title

router = APIRouter(prefix="/api/contacts", tags=["contacts"])
MONEY_KEYS = ("budget_amount", "budget_currency")
COMMANDS = {
    "update": "contacts.update", "identities": "contacts.add_identity", "merge-propose": "contacts.merge_propose",
    "merge": "contacts.merge", "archive": "contacts.archive", "restore": "contacts.restore",
    "mark-provisional": "contacts.mark_provisional",
}


@router.get("")
async def list_contacts(tab: str = Query("all"), q: str | None = None, status: str | None = None,
                        include_archived: bool = False, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                        actor: Actor = Depends(require("contacts.read")), db: AsyncSession = Depends(get_db)):
    if tab not in ("all", *TAB_ROLES):
        raise HTTPException(422, f"tab must be one of {('all', *TAB_ROLES)}")
    clauses = [Contact.status != "merged"]
    if status:
        clauses.append(Contact.status == status)
    elif not include_archived:
        clauses.append(Contact.status != "archived")
    if tab != "all":
        # roles is JSON; match the serialized array text for any of the tab's roles
        clauses.append(or_(*[cast(Contact.roles, Text).ilike(f'%"{r}"%') for r in TAB_ROLES[tab]]))
    if q:
        pat = f"%{q.strip().lower()}%"
        clauses.append(or_(Contact.search_text.ilike(pat), Contact.name.ilike(pat)))
    total = (await db.execute(select(func.count()).select_from(Contact).where(*clauses))).scalar_one()
    rows = (await db.execute(select(Contact).where(*clauses).order_by(Contact.name.asc(), Contact.created_at)
                             .limit(limit).offset(offset))).scalars().all()
    return {"items": [serialize_contact(c) for c in rows], "total": int(total), "tab": tab}


@router.get("/resolve")
async def resolve(email: str | None = None, phone: str | None = None, name: str | None = None,
                  company: str | None = None, provider_ref: str | None = None, conversation_id: str | None = None,
                  actor: Actor = Depends(require("contacts.read")), db: AsyncSession = Depends(get_db)):
    if not (actor.kind in ("user", "agent") and actor.role in ("owner", "manager")):
        raise HTTPException(403, "owner or manager only")
    if not any((email, phone, name, provider_ref, conversation_id)):
        raise HTTPException(422, "give at least one of email, phone, name, provider_ref, conversation_id")
    res = await resolve_contact(db, email=email, phone=phone, name=name, company=company, provider_ref=provider_ref,
                                thread_mapping=conversation_id)
    d = res.to_dict()
    ids = [c["contact_id"] for c in d["candidates"]]
    names = {}
    if ids:
        for c in (await db.execute(select(Contact).where(Contact.id.in_(ids)))).scalars().all():
            names[c.id] = {"name": c.name, "company": c.company, "status": c.status}
    for c in d["candidates"]:
        c.update(names.get(c["contact_id"], {}))
    d["decision"] = {"matched": "Allowed", "proposed": "Needs review", "ambiguous": "Needs review", "unmatched": "Needs review"}[res.state]
    return d


@router.get("/{contact_id}")
async def get_contact(contact_id: str, actor: Actor = Depends(require("contacts.read")), db: AsyncSession = Depends(get_db)):
    from ..routers.tasks import task_view, visibility_clauses
    c = await db.get(Contact, contact_id)
    if c is None:
        raise NotFound("contact not found")
    now = datetime.now(timezone.utc)
    identities = await identities_of(db, c.id)
    limit = await visible_vehicle_ids(db, actor)
    opps = (await db.execute(select(Opportunity).where(Opportunity.contact_id == c.id).order_by(Opportunity.created_at.desc()))).scalars().all()
    opportunities = []
    for o in opps:
        if limit is not None and o.vehicle_id not in limit:
            continue   # record-limited actors see only leads on their visible vehicles (IRQ leads have none)
        d = sanitize_money(actor, serialize_opportunity(o), MONEY_KEYS)
        d["stage_label"] = STAGE_LABELS.get(o.stage, o.stage)
        opportunities.append(d)
    requests = []
    try:
        from ..models.sourcing import ImportRequest
        for r in (await db.execute(select(ImportRequest).where(ImportRequest.contact_id == c.id))).scalars().all():
            requests.append({"id": r.id, "title": r.title, "status": r.status, "deposit_status": r.deposit_status,
                             "opportunity_id": r.opportunity_id})
    except Exception:  # noqa: BLE001
        requests = []
    vq = select(Vehicle).where(Vehicle.buyer_contact_id == c.id)
    if limit is not None:
        vq = vq.where(Vehicle.id.in_(list(limit)))
    vehicles = [{"id": v.id, "title": vehicle_title(v), "stock_no": v.stock_no, "commercial_state": v.commercial_state,
                 "allocation": v.allocation} for v in (await db.execute(vq)).scalars().all()]
    # the same task visibility rule as /api/tasks (own / reports / unassigned / client record scope)
    tq = select(Task).where(Task.contact_id == c.id, *await visibility_clauses(db, actor, "all"))
    tasks = [task_view(t, now=now) for t in (await db.execute(tq.order_by(Task.due_at.asc().nulls_last()))).scalars().all()]
    promises = [{"id": p.id, "text": p.text, "status": p.status, "due_at": p.due_at.isoformat() if p.due_at else None,
                 "made_at": p.made_at.isoformat() if p.made_at else None, "made_by": p.made_by, "vehicle_id": p.vehicle_id,
                 "opportunity_id": p.opportunity_id}
                for p in (await db.execute(select(Commitment).where(Commitment.contact_id == c.id)
                                           .order_by(Commitment.made_at.desc().nulls_last()))).scalars().all()]
    conversations = []
    if has_perm(actor, "inbox.read"):
        try:
            from ..models.comms import Conversation
            for cv in (await db.execute(select(Conversation).where(Conversation.contact_id == c.id)
                                        .order_by(Conversation.last_inbound_at.desc().nulls_last()).limit(50))).scalars().all():
                conversations.append({"id": cv.id, "subject": cv.subject, "channel": cv.channel, "state": cv.state,
                                      "contact_match": cv.contact_match,
                                      "last_inbound_at": cv.last_inbound_at.isoformat() if cv.last_inbound_at else None})
        except Exception:  # noqa: BLE001
            conversations = []
    merges = [serialize_merge(m) for m in (await db.execute(select(ContactMerge).where(
        or_(ContactMerge.survivor_id == c.id, ContactMerge.merged_id == c.id)).order_by(ContactMerge.created_at.desc()))).scalars().all()]
    return {"contact": serialize_contact(c, identities), "opportunities": opportunities, "import_requests": requests,
            "vehicles": vehicles, "tasks": tasks, "promises": promises, "consent": dict(c.consent or {}),
            "conversations": conversations, "merge_history": merges,
            "merged_into": ({"id": c.merged_into_id} if c.merged_into_id else None)}


# ── writes ───────────────────────────────────────────────────────────────────
@router.post("")
async def create_contact(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    return (await dispatch(ctx, "contacts.create", payload)).to_dict()


@router.post("/merges/{merge_id}/unmerge")
async def unmerge(merge_id: str, payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context)):
    return (await dispatch(ctx, "contacts.unmerge", {**(payload or {}), "merge_id": merge_id})).to_dict()


@router.post("/{contact_id}/identities/{identity_id}/remove")
async def remove_identity(contact_id: str, identity_id: str, payload: dict = Body(default={}),
                          ctx: CommandContext = Depends(command_context)):
    return (await dispatch(ctx, "contacts.remove_identity",
                           {**(payload or {}), "contact_id": contact_id, "identity_id": identity_id})).to_dict()


@router.post("/{contact_id}/{action}")
async def contact_action(contact_id: str, action: str, payload: dict = Body(default={}),
                         ctx: CommandContext = Depends(command_context)):
    name = COMMANDS.get(action)
    if name is None:
        raise HTTPException(404, f"unknown contact action {action!r}")
    body = dict(payload or {})
    if action in ("merge", "merge-propose"):
        body.setdefault("survivor_id", contact_id)
    else:
        body["contact_id"] = contact_id
    return (await dispatch(ctx, name, body)).to_dict()
