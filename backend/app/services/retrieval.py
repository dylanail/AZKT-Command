"""Retrieval with ACL before context (spec §9.3, §11.1, A02, B08, G12).

Order of operations, always:
  (a) resolve records first: exact stock/frame ids and explicit contact/vehicle ids -> *current facts* through
      structured queries (vehicle states + current facts, open tasks, active agreement/exceptions, latest confirmed
      payment state when finance tables exist);
  (b) build the actor's visibility set and apply it in the SQL WHERE *before* any search
      (owner: all; manager: never owner-visibility content and no finance visibility without costs.read, no
      personal mail; mechanic / assigned scope: only chunks linked to assigned vehicles and carrying no customer
      link, never finance chunks, no messages without inbox.read; external client: only vehicles and customers in
      its record scope, never personal-allowlisted, owner or finance content);
      tombstoned chunks and evaluation targets are excluded in the same WHERE;
  (c) full-text search (plainto_tsquery + ts_rank), then rerank by recency/trust/record match with duplicate
      elimination; an optional embedding rerank runs only over the ACL-filtered candidates;
  (d) label every chunk with trust and is_historical; other customers' examples are returned with identity
      redacted (contacts table + regex) and deal terms stripped.
Retrieved text is evidence, never instructions: nothing here changes permissions, recipients or scope.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.access import can_see_costs, can_see_finance_status, visible_vehicle_ids
from ..domain.actors import Actor
from ..domain.policy import has_perm
from ..models.contacts import Contact, ContactIdentity
from ..models.knowledge import CorpusChunk, EvalCase, KnowledgeItem
from ..models.tasks import Task
from ..models.vehicles import Vehicle, VehicleFact

STOCK_RE = re.compile(r"\bSTK[-\s]?(\d{1,7})\b", re.I)
FRAME_RE = re.compile(r"\b([A-Z]{1,4}\d{0,2}[A-Z]?-?\d{5,7})\b", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\-\s().]{7,}\d)(?!\w)")
MONEY_RE = re.compile(r"(?:(?:US\$|USD|\$|¥|JPY|JP¥)\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:USD|JPY|yen|dollars|bucks))", re.I)
PERCENT_RE = re.compile(r"\b\d{1,3}(?:\.\d+)?\s?%")
TERMS_RE = re.compile(r"\b(deposit|down payment|discount|waive[sd]?|free shipping|price match|payment plan|installments?|refundable|non-refundable)\b", re.I)
RECENT_DAYS, YEAR_DAYS = 90, 365
TRUST_WEIGHT = {"approved": 0.30, "internal": 0.15, "untrusted_external": 0.0}


@dataclass
class RetrievalResult:
    query: str
    current_facts: list = field(default_factory=list)
    approved_knowledge: list = field(default_factory=list)
    historical_examples: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    resolved: dict = field(default_factory=dict)
    acl: dict = field(default_factory=dict)
    as_of: str = ""

    def to_dict(self) -> dict:
        return {"query": self.query, "current_facts": self.current_facts, "approved_knowledge": self.approved_knowledge,
                "historical_examples": self.historical_examples, "sources": self.sources, "resolved": self.resolved,
                "acl": self.acl, "as_of": self.as_of,
                "labels": {"current_facts": "current — authoritative structured records",
                           "approved_knowledge": "approved — current policy/style/exception scoped to this request",
                           "historical_examples": "historical — tone/reasoning examples only; facts are not current"}}


# ── ACL ──────────────────────────────────────────────────────────────────────
async def _external_contact_ids(db: AsyncSession, actor: Actor, vehicle_limit: set[str] | None) -> list[str] | None:
    """Contacts an external client may resolve: its granted contact_ids, else the buyers of the vehicles in its
    record scope. None means unrestricted (a record-unlimited client). Spec invariant 14: the client grant follows
    every read, so a connector cannot name an arbitrary contact_id and read that customer's facts."""
    scope = actor.client_record_scope or {}
    ids = scope.get("contact_ids")
    if ids:
        return sorted({str(i) for i in ids})
    if vehicle_limit is None:
        return None
    if not vehicle_limit:
        return []
    rows = (await db.execute(select(Vehicle.buyer_contact_id).where(Vehicle.id.in_(sorted(vehicle_limit)),
                                                                    Vehicle.buyer_contact_id.is_not(None)))).all()
    return sorted({r[0] for r in rows})


async def acl_for(db: AsyncSession, actor: Actor) -> dict:
    """The actor's visibility set, resolved before any search."""
    is_owner = actor.kind == "system" or (actor.kind in ("user", "agent") and actor.role == "owner")
    vehicle_limit = None if actor.kind == "system" else await visible_vehicle_ids(db, actor)
    contact_limit = await _external_contact_ids(db, actor, vehicle_limit) if actor.kind == "external" else None
    return {
        "is_owner": is_owner,
        "vehicle_ids": None if vehicle_limit is None else sorted(vehicle_limit),
        "contact_ids": contact_limit,
        "costs": actor.kind == "system" or can_see_costs(actor),
        "finance_status": actor.kind == "system" or can_see_finance_status(actor),
        "contacts": actor.kind == "system" or has_perm(actor, "contacts.read"),
        "messages": actor.kind == "system" or has_perm(actor, "inbox.read"),
        "tasks": actor.kind == "system" or has_perm(actor, "tasks.read"),
        "sales": actor.kind == "system" or has_perm(actor, "sales.read"),
        "personal": is_owner and actor.kind != "external",
        "assigned_only": actor.kind == "external" or (actor.kind in ("user", "agent") and not is_owner
                                                      and (actor.scope == "assigned" or not actor.perms.get("vehicles.all", False))),
        "external": actor.kind == "external",
    }


def chunk_clauses(acl: dict) -> list:
    """SQL WHERE fragments implementing the ACL. Applied before search, rerank and any model context."""
    vis = CorpusChunk.acl["visibility"].as_string()
    personal = CorpusChunk.acl["personal_allowlisted"].as_boolean()
    clauses = [CorpusChunk.tombstoned_at.is_(None), CorpusChunk.text != ""]
    # evaluation targets never reach retrieval (spec §9.5, G12)
    targets = select(EvalCase.target_ref).where(EvalCase.excluded_from_retrieval.is_(True), EvalCase.target_ref.is_not(None))
    clauses.append(not_((CorpusChunk.source_kind + ":" + CorpusChunk.source_id).in_(targets)))
    if acl["is_owner"] and not acl["external"]:
        return clauses
    if not acl["personal"]:
        clauses.append(or_(personal.is_(None), personal.is_(False)))
    # owner-visibility content stays with the owner even for an actor who holds costs.read (spec §11.1)
    clauses.append(or_(vis.is_(None), vis != "owner"))
    if not acl["costs"]:
        clauses.append(or_(vis.is_(None), vis == "all"))
    if not acl["messages"]:
        clauses.append(CorpusChunk.source_kind.notin_(("message", "transcript", "attachment")))
    if acl["assigned_only"]:
        if acl["vehicle_ids"] is None:
            clauses.append(CorpusChunk.vehicle_id.is_not(None))  # record-unlimited external client: vehicle-linked chunks only
        else:
            ids = acl["vehicle_ids"]
            clauses.append(CorpusChunk.vehicle_id.in_(ids) if ids else CorpusChunk.id.is_(None))
    if not acl["contacts"]:
        # never contact-level content: only chunks tied to a visible vehicle and carrying no customer link,
        # and never a customer exception (A02: a mechanic sees vehicles and work, not unrelated customers)
        clauses.append(CorpusChunk.vehicle_id.is_not(None))
        clauses.append(CorpusChunk.contact_id.is_(None))
        clauses.append(or_(CorpusChunk.kind.is_(None), CorpusChunk.kind != "exception"))
    elif acl["contact_ids"] is not None:
        # an external client with read:contacts still only reaches the customers inside its record scope
        ids = acl["contact_ids"]
        clauses.append(or_(CorpusChunk.contact_id.is_(None), CorpusChunk.contact_id.in_(ids)) if ids
                       else CorpusChunk.contact_id.is_(None))
    if acl["external"]:
        clauses.append(or_(vis.is_(None), vis == "all"))
        clauses.append(or_(CorpusChunk.kind.is_(None), CorpusChunk.kind != "exception"))
    return clauses


# ── (a) record resolution + current facts ────────────────────────────────────
def extract_ids(query: str) -> dict:
    stocks = sorted({f"STK-{int(m.group(1)):04d}" for m in STOCK_RE.finditer(query or "")})
    frames = sorted({re.sub(r"[^A-Z0-9]", "", m.group(1).upper()) for m in FRAME_RE.finditer(query or "")
                     if not m.group(1).upper().startswith("STK")})
    emails = sorted({e.lower() for e in EMAIL_RE.findall(query or "")})
    return {"stock_nos": stocks, "frame_nos": frames, "emails": emails}


def _fact_view(f: VehicleFact) -> dict:
    return {"key": f.key, "value": f.value, "unit": f.unit, "currency": f.currency, "status": f.status,
            "observed_at": f.observed_at.isoformat() if f.observed_at else None, "source_kind": f.source_kind,
            "source_ref": f.source_ref, "visibility": f.visibility}


async def _vehicle_facts(db: AsyncSession, acl: dict, v: Vehicle) -> dict:
    facts_q = select(VehicleFact).where(VehicleFact.vehicle_id == v.id, VehicleFact.is_current.is_(True))
    if not acl["costs"]:
        facts_q = facts_q.where(VehicleFact.visibility == "all", VehicleFact.key.notin_(("purchase_amount", "asking_price")))
    facts = [_fact_view(f) for f in (await db.execute(facts_q.order_by(VehicleFact.key))).scalars().all()]
    d = {
        "kind": "vehicle", "id": v.id, "version": v.version, "stock_no": v.stock_no, "frame_no": v.frame_no_raw,
        "title": " ".join(p for p in (str(v.model_year) if v.model_year else None, v.make, v.model, v.color) if p) or v.title,
        "states": {"logistics": v.logistics_state, "recon": v.recon_state, "commercial": v.commercial_state,
                   "documents": v.documents_state}, "allocation": v.allocation, "health": v.health,
        "location": v.location or "Not recorded", "next_action": v.next_action,
        "buyer_contact_id": v.buyer_contact_id if acl["contacts"] else None,
        "facts": facts, "label": "current", "status": "confirmed",
        "availability": {"inventory": "available", "reserved": "reserved", "sold": "sold", "candidate": "not yet purchased"}.get(v.allocation, v.allocation),
    }
    if acl["costs"]:
        d["money"] = {"asking_price": str(v.asking_price) if v.asking_price is not None else None, "asking_currency": v.asking_currency,
                      "purchase_amount": str(v.purchase_amount) if v.purchase_amount is not None else None,
                      "purchase_currency": v.purchase_currency}
    else:
        d["money_hidden"] = True
    return d


async def _open_tasks(db: AsyncSession, actor: Actor, acl: dict, vehicle_id: str | None, contact_id: str | None) -> list[dict]:
    if not acl["tasks"] or not (vehicle_id or contact_id):
        return []
    q = select(Task).where(Task.status.in_(("open", "in_progress", "blocked", "waiting", "awaiting_verification")))
    q = q.where(Task.vehicle_id == vehicle_id) if vehicle_id else q.where(Task.contact_id == contact_id)
    if acl["assigned_only"] and actor.kind != "external":
        q = q.where(Task.owner_user_id == actor.user_id)
    rows = (await db.execute(q.order_by(Task.due_at.asc().nulls_last()).limit(10))).scalars().all()
    return [{"kind": "task", "id": t.id, "title": t.title, "status": t.status, "due_at": t.due_at.isoformat() if t.due_at else None,
             "owner_user_id": t.owner_user_id, "vehicle_id": t.vehicle_id, "contact_id": t.contact_id, "label": "current"} for t in rows]


async def _contact_facts(db: AsyncSession, acl: dict, c: Contact) -> list[dict]:
    out: list[dict] = [{"kind": "contact", "id": c.id, "version": c.version, "name": c.name, "company": c.company,
                        "roles": list(c.roles or []), "status": c.status, "verified": bool(c.verified), "label": "current"}]
    try:
        from ..models.finance import Agreement
        ags = (await db.execute(select(Agreement).where(Agreement.contact_id == c.id, Agreement.status.in_(("sent", "signed")))
                                .order_by(Agreement.agreement_version.desc()))).scalars().all()
        for a in ags:
            d = {"kind": "agreement", "id": a.id, "agreement_kind": a.kind, "status": a.status, "version": a.agreement_version,
                 "exceptions": list(a.exceptions or []), "label": "current", "contact_id": c.id,
                 "signed_at": a.signed_at.isoformat() if a.signed_at else None}
            if acl["finance_status"]:
                d["deposit"] = {"amount": str(a.deposit_amount) if a.deposit_amount is not None else None, "currency": a.deposit_currency}
                d["terms"] = dict(a.terms or {})
            else:
                d["money_hidden"] = True
            out.append(d)
    except ImportError:  # finance tables not present in this build
        pass
    if acl["sales"]:
        try:
            from ..models.sales import Opportunity
            opps = (await db.execute(select(Opportunity).where(Opportunity.contact_id == c.id, Opportunity.stage != "lost")
                                     .order_by(Opportunity.created_at.desc()).limit(5))).scalars().all()
            for o in opps:
                out.append({"kind": "opportunity", "id": o.id, "pipeline": o.pipeline, "stage": o.stage, "vehicle_id": o.vehicle_id,
                            "label": "current", "contact_id": c.id})
        except ImportError:
            pass
    if acl["finance_status"]:
        try:
            from ..models.finance import Invoice, Payment
            p = (await db.execute(select(Payment).where(Payment.contact_id == c.id, Payment.status.in_(("completed", "partially_refunded")))
                                  .order_by(Payment.occurred_at.desc().nulls_last()).limit(1))).scalars().first()
            if p is not None:
                out.append({"kind": "payment", "id": p.id, "status": p.status, "amount": str(p.amount), "currency": p.currency,
                            "occurred_at": p.occurred_at.isoformat() if p.occurred_at else None, "provider": p.provider,
                            "label": "current", "contact_id": c.id, "fact_status": "confirmed" if p.confirmed_at or p.source_kind in ("webhook", "api") else "reported"})
            invs = (await db.execute(select(Invoice).where(Invoice.contact_id == c.id, Invoice.status.in_(("open", "partially_paid")))
                                     .order_by(Invoice.due_at.asc().nulls_last()).limit(5))).scalars().all()
            for i in invs:
                out.append({"kind": "invoice", "id": i.id, "invoice_kind": i.kind, "status": i.status, "amount_due": str(i.amount_due),
                            "amount_allocated": str(i.amount_allocated), "currency": i.currency, "label": "current", "contact_id": c.id})
        except ImportError:
            pass
    return out


async def resolve_records(db: AsyncSession, actor: Actor, acl: dict, query: str, *, contact_id: str | None,
                          vehicle_id: str | None) -> tuple[list[Vehicle], Contact | None, dict]:
    ids = extract_ids(query)
    vq = select(Vehicle).where(Vehicle.archived_at.is_(None))
    ors = []
    if vehicle_id:
        ors.append(Vehicle.id == vehicle_id)
    for s in ids["stock_nos"]:
        ors.append(Vehicle.stock_no.ilike(s))
    for fr in ids["frame_nos"]:
        ors.append(Vehicle.frame_no_norm.ilike(f"%{fr}%"))
    vehicles: list[Vehicle] = []
    if ors:
        vq = vq.where(or_(*ors))
        if acl["vehicle_ids"] is not None:
            vq = vq.where(Vehicle.id.in_(acl["vehicle_ids"]) if acl["vehicle_ids"] else Vehicle.id.is_(None))
        vehicles = list((await db.execute(vq.limit(5))).scalars().all())
    contact: Contact | None = None
    allowed_contacts = acl.get("contact_ids")  # external client: only the customers inside its record scope
    if acl["contacts"] and allowed_contacts != []:
        if contact_id:
            c = await db.get(Contact, contact_id)
            if c is not None and c.status != "merged" and (allowed_contacts is None or c.id in allowed_contacts):
                contact = c
        elif ids["emails"]:
            pat = f"%{ids['emails'][0]}%"
            cq = select(Contact).where(Contact.status != "merged", Contact.search_text.ilike(pat))
            if allowed_contacts is not None:
                cq = cq.where(Contact.id.in_(allowed_contacts))
            contact = (await db.execute(cq.limit(1))).scalars().first()
    return vehicles, contact, ids


# ── (c) search + rerank ──────────────────────────────────────────────────────
def _terms(query: str) -> list[str]:
    return [t for t in re.findall(r"[\w'-]+", (query or "").lower()) if len(t) >= 3][:12]


def build_tsquery(q: str):
    """OR of the query terms (each through plainto_tsquery, so nothing is parsed as operators); ts_rank then
    orders chunks by how many terms they hit. plainto_tsquery alone is AND-only and drops any chunk missing one word."""
    terms = _terms(q)
    if not terms:
        return func.plainto_tsquery("english", q)
    parts = [func.plainto_tsquery("english", t) for t in terms]
    tsq = parts[0]
    for part in parts[1:]:
        tsq = tsq.op("||")(part)
    return tsq


async def search_chunks(db: AsyncSession, acl: dict, query: str, *, limit: int, kinds: list[str] | None = None,
                        source_kinds: list[str] | None = None) -> list[tuple[CorpusChunk, float]]:
    clauses = chunk_clauses(acl)
    if kinds:
        clauses.append(CorpusChunk.kind.in_(kinds))
    if source_kinds:
        clauses.append(CorpusChunk.source_kind.in_(source_kinds))
    q = (query or "").strip()
    if not q:
        return []
    tsq = build_tsquery(q)
    rank = func.ts_rank(CorpusChunk.tsv, tsq)
    ids = extract_ids(q)
    id_ors = [CorpusChunk.text.ilike(f"%{s}%") for s in ids["stock_nos"]] + [CorpusChunk.text.ilike(f"%{f}%") for f in ids["frame_nos"]]
    match = CorpusChunk.tsv.op("@@")(tsq)
    where = or_(match, *id_ors) if id_ors else match
    rows = (await db.execute(select(CorpusChunk, rank).where(*clauses, where)
                             .order_by(rank.desc(), CorpusChunk.happened_at.desc().nulls_last()).limit(limit * 4))).all()
    if not rows:
        # lexical fallback for terms tsvector normalizes away (ids, Japanese, very short text)
        terms = _terms(q)
        if terms:
            like = or_(*[CorpusChunk.text.ilike(f"%{t}%") for t in terms[:6]])
            rows = [(c, 0.01) for c in (await db.execute(select(CorpusChunk).where(*clauses, like)
                                                        .order_by(CorpusChunk.happened_at.desc().nulls_last()).limit(limit * 2))).scalars().all()]
    return [(c, float(r or 0.0)) for c, r in rows]


def _cosine(a: list[float] | None, b: list[float] | None) -> float | None:
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else None


def rerank(rows: list[tuple[CorpusChunk, float]], *, now: datetime, vehicle_ids: set[str], contact_id: str | None,
           query_embedding: list[float] | None = None) -> list[tuple[CorpusChunk, float]]:
    """Relevance + recency + trust + record match, duplicates eliminated (same content hash / same source+text)."""
    scored: list[tuple[CorpusChunk, float]] = []
    seen_hash: set[str] = set()
    for c, rank in rows:
        h = c.content_hash or (c.source_kind + ":" + c.source_id + ":" + str(c.chunk_index))
        if h in seen_hash:
            continue
        seen_hash.add(h)
        score = rank + TRUST_WEIGHT.get(c.trust, 0.0)
        if c.happened_at:
            age = (now - c.happened_at.replace(tzinfo=c.happened_at.tzinfo or timezone.utc)).days
            score += 0.2 if age <= RECENT_DAYS else 0.1 if age <= YEAR_DAYS else 0.0
        if c.vehicle_id and c.vehicle_id in vehicle_ids:
            score += 0.25
        if contact_id and c.contact_id == contact_id:
            score += 0.25
        sim = _cosine(query_embedding, c.embedding) if query_embedding else None
        if sim is not None:
            score += 0.5 * sim
        scored.append((c, score))
    scored.sort(key=lambda x: (-x[1], -(x[0].happened_at.timestamp() if x[0].happened_at else 0)))
    return scored


# ── (d) labelling + redaction ────────────────────────────────────────────────
NAME_RE = re.compile(r"\b[A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{1,20}){0,2}\b")
MAX_NAME_CANDIDATES = 240


async def _contacts_named_in(db: AsyncSession, texts: list[str]) -> list[str]:
    """Identity terms for customers a chunk is *not* linked to: candidate names in the text are matched against the
    contacts table, so an unlinked historical example still loses a real customer's name (spec §9.3, G12/B08)."""
    cands: set[str] = set()
    for t in texts:
        for m in NAME_RE.finditer(t or ""):
            words = m.group(0).split()
            for i in range(len(words)):
                for j in range(i + 1, len(words) + 1):
                    s = " ".join(words[i:j])
                    if len(s) >= 3:
                        cands.add(s.lower())
            if len(cands) > MAX_NAME_CANDIDATES:
                break
    if not cands:
        return []
    values = sorted(cands)[:MAX_NAME_CANDIDATES]
    rows = (await db.execute(select(Contact.name, Contact.company).where(
        or_(func.lower(Contact.name).in_(values), func.lower(Contact.company).in_(values))))).all()
    out: list[str] = []
    for name, company in rows:
        for v in (name, company):
            if v and v.strip().lower() in cands:
                out.append(v.strip())
                out += [w for w in v.split() if len(w) >= 3]
    return out


async def _identity_terms(db: AsyncSession, contact_ids: set[str]) -> dict[str, list[str]]:
    if not contact_ids:
        return {}
    out: dict[str, list[str]] = {cid: [] for cid in contact_ids}
    for c in (await db.execute(select(Contact).where(Contact.id.in_(list(contact_ids))))).scalars().all():
        terms = [c.name, c.company, c.primary_email, c.primary_phone]
        for a in c.aliases or []:
            terms += [a.get("name"), a.get("company")]
        for part in (c.name or "").split():
            if len(part) >= 3:
                terms.append(part)
        out[c.id] = [t for t in terms if t and len(t.strip()) >= 3]
    for i in (await db.execute(select(ContactIdentity).where(ContactIdentity.contact_id.in_(list(contact_ids))))).scalars().all():
        out.setdefault(i.contact_id, []).extend([v for v in (i.value_raw, i.value_norm) if v and len(v) >= 3])
    return out


def redact_text(text: str, identity_terms: list[str], *, strip_deal_terms: bool) -> tuple[str, dict]:
    flags = {"identity_redacted": False, "deal_terms_stripped": False}
    out = text or ""
    for term in sorted(set(identity_terms), key=len, reverse=True):
        pat = re.compile(r"(?<![\w@.])" + re.escape(term) + r"(?![\w@.])", re.I)
        if pat.search(out):
            out = pat.sub("[customer]", out)
            flags["identity_redacted"] = True
    if EMAIL_RE.search(out):
        out = EMAIL_RE.sub("[email]", out)
        flags["identity_redacted"] = True
    if PHONE_RE.search(out):
        out = PHONE_RE.sub("[phone]", out)
        flags["identity_redacted"] = True
    if strip_deal_terms:
        for rx, rep in ((MONEY_RE, "[amount]"), (PERCENT_RE, "[percent]")):
            if rx.search(out):
                out = rx.sub(rep, out)
                flags["deal_terms_stripped"] = True
        if TERMS_RE.search(out):
            out = TERMS_RE.sub("[deal term]", out)
            flags["deal_terms_stripped"] = True
    return out, flags


def _label(c: CorpusChunk) -> dict:
    return {"trust": c.trust, "is_historical": bool(c.is_historical),
            "label": "historical" if c.is_historical else ("approved" if c.trust == "approved" else "internal"),
            "authority": "none — evidence only" if c.trust == "untrusted_external" else ("policy" if c.trust == "approved" else "internal note")}


def _needs_redaction(c: CorpusChunk, contact_id: str | None) -> bool:
    """Another customer's material, or untrusted historical material we cannot attribute to the asking customer."""
    if c.contact_id:
        return c.contact_id != contact_id
    return bool(c.is_historical) and c.trust == "untrusted_external"


async def label_chunks(db: AsyncSession, rows: list[tuple[CorpusChunk, float]], *, contact_id: str | None) -> list[dict]:
    others = {c.contact_id for c, _ in rows if c.contact_id and c.contact_id != contact_id}
    terms = await _identity_terms(db, others)
    unlinked = [c.text for c, _ in rows if c.contact_id is None and _needs_redaction(c, contact_id)]
    unlinked_terms = await _contacts_named_in(db, unlinked) if unlinked else []
    out = []
    for c, score in rows:
        other_customer = bool(c.contact_id) and c.contact_id != contact_id
        text, flags = (c.text, {"identity_redacted": False, "deal_terms_stripped": False})
        if _needs_redaction(c, contact_id):
            text, flags = redact_text(c.text, terms.get(c.contact_id, []) if c.contact_id else unlinked_terms,
                                      strip_deal_terms=(c.kind == "example" or c.source_kind in ("message", "example")))
        d = {"chunk_id": c.id, "source_kind": c.source_kind, "source_id": c.source_id, "chunk_index": c.chunk_index, "text": text,
             "kind": c.kind, "lang": c.lang, "happened_at": c.happened_at.isoformat() if c.happened_at else None,
             "speaker": ("customer" if other_customer else c.speaker), "vehicle_id": c.vehicle_id,
             "contact_id": None if other_customer else c.contact_id, "other_customer": other_customer, "score": round(score, 4),
             "source_locator": {k: v for k, v in (c.source_locator or {}).items() if k not in ("from", "to", "participants")} if other_customer else dict(c.source_locator or {}),
             **flags, **_label(c)}
        out.append(d)
    return out


# ── approved knowledge (structured, scoped) ──────────────────────────────────
async def approved_knowledge(db: AsyncSession, actor: Actor, acl: dict, query: str, *, contact_id: str | None,
                             vehicle_ids: set[str], kinds: list[str] | None, limit: int, now: datetime) -> list[dict]:
    from .knowledge import serialize_item, visible_item_clauses
    clauses = [KnowledgeItem.status == "approved",
               or_(KnowledgeItem.effective_from.is_(None), KnowledgeItem.effective_from <= now),
               or_(KnowledgeItem.expires_at.is_(None), KnowledgeItem.expires_at > now)]
    clauses += visible_item_clauses(actor)
    # scope: general items, this contact's exceptions, these vehicles' lessons — never another customer's exception (G12)
    scope_c = KnowledgeItem.scope["contact_id"].as_string()
    scope_v = KnowledgeItem.scope["vehicle_id"].as_string()
    scope_ors = [and_(scope_c.is_(None), scope_v.is_(None))]
    if contact_id and acl["contacts"]:
        scope_ors.append(scope_c == contact_id)
    if vehicle_ids:
        scope_ors.append(scope_v.in_(list(vehicle_ids)))
    clauses.append(or_(*scope_ors))
    if kinds:
        clauses.append(KnowledgeItem.kind.in_(kinds))
    rows = (await db.execute(select(KnowledgeItem).where(*clauses).order_by(KnowledgeItem.approved_at.desc().nulls_last()).limit(200))).scalars().all()
    terms = _terms(query)
    scored = []
    for k in rows:
        hay = f"{k.title} {k.content}".lower()
        hits = sum(1 for t in terms if t in hay)
        scoped = bool((k.scope or {}).get("contact_id") or (k.scope or {}).get("vehicle_id"))
        score = hits + (2.0 if scoped else 0.0) + (1.0 if k.kind in ("policy", "exception") else 0.0)
        if hits == 0 and not scoped and k.kind not in ("policy",):
            continue
        scored.append((score, k))
    scored.sort(key=lambda x: -x[0])
    out = []
    for score, k in scored[:limit]:
        d = serialize_item(k)
        d.update({"trust": "approved", "is_historical": False, "label": "approved", "score": score,
                  "usable_as": list(k.usable_as or []), "authority": "policy" if k.kind in ("policy", "exception") else "example/style only"})
        out.append(d)
    return out


# ── entry points ─────────────────────────────────────────────────────────────
async def retrieve(db: AsyncSession, actor: Actor, query: str, *, contact_id: str | None = None, vehicle_id: str | None = None,
                   kinds: list[str] | None = None, limit: int = 8, query_embedding: list[float] | None = None) -> RetrievalResult:
    now = datetime.now(timezone.utc)
    acl = await acl_for(db, actor)
    res = RetrievalResult(query=query, as_of=now.isoformat())
    res.acl = {k: acl[k] for k in ("is_owner", "costs", "finance_status", "contacts", "messages", "assigned_only", "external")}
    res.acl["record_scoped_contacts"] = acl["contact_ids"] is not None
    # (a) records first
    if vehicle_id and acl["vehicle_ids"] is not None and vehicle_id not in acl["vehicle_ids"]:
        vehicle_id = None  # outside the actor's record scope: not resolved, not mentioned
    if contact_id and not acl["contacts"]:
        contact_id = None
    if contact_id and acl.get("contact_ids") is not None and contact_id not in acl["contact_ids"]:
        contact_id = None  # outside the client's record scope: not resolved, not mentioned
    vehicles, contact, ids = await resolve_records(db, actor, acl, query, contact_id=contact_id, vehicle_id=vehicle_id)
    vehicle_ids = {v.id for v in vehicles}
    for v in vehicles:
        res.current_facts.append(await _vehicle_facts(db, acl, v))
        res.current_facts.extend(await _open_tasks(db, actor, acl, v.id, None))
    if contact is not None:
        res.current_facts.extend(await _contact_facts(db, acl, contact))
        if not vehicles:
            res.current_facts.extend(await _open_tasks(db, actor, acl, None, contact.id))
    res.resolved = {"vehicle_ids": sorted(vehicle_ids), "contact_id": contact.id if contact else None, "extracted": ids}
    # (b)+(c) ACL-filtered search, rerank
    rows = await search_chunks(db, acl, query, limit=limit, kinds=kinds)
    ranked = rerank(rows, now=now, vehicle_ids=vehicle_ids, contact_id=contact.id if contact else None, query_embedding=query_embedding)
    # (d) label. Approved knowledge comes only from the scoped structured query (never from a chunk hit, so another
    # customer's exception or a retired policy can never surface as an "example" — G12); everything else is historical.
    labelled = await label_chunks(db, ranked, contact_id=contact.id if contact else None)
    res.approved_knowledge = await approved_knowledge(db, actor, acl, query, contact_id=contact.id if contact else None,
                                                      vehicle_ids=vehicle_ids, kinds=kinds, limit=limit, now=now)
    historical = [d for d in labelled if d["source_kind"] not in ("knowledge", "procedure")]
    per_source: dict[str, int] = {}
    kept = []
    for d in historical:
        key = f"{d['source_kind']}:{d['source_id']}"
        if per_source.get(key, 0) >= 2:
            continue
        per_source[key] = per_source.get(key, 0) + 1
        kept.append(d)
        if len(kept) >= limit:
            break
    res.historical_examples = kept
    res.sources = ([{"kind": f["kind"], "id": f["id"], "label": "current", "trust": "structured"} for f in res.current_facts if f.get("id")]
                   + [{"kind": "knowledge_item", "id": k["id"], "label": "approved", "trust": "approved"} for k in res.approved_knowledge]
                   + [{"kind": h["source_kind"], "id": h["source_id"], "chunk_id": h["chunk_id"], "label": h["label"], "trust": h["trust"],
                       "locator": h["source_locator"]} for h in res.historical_examples])
    return res


async def search_sources(db: AsyncSession, actor: Actor, q: str, *, limit: int = 20, source_kinds: list[str] | None = None) -> dict:
    """The UI 'sources.search' tool: same ACL as retrieve(); previews are redacted the same way."""
    now = datetime.now(timezone.utc)
    acl = await acl_for(db, actor)
    rows = await search_chunks(db, acl, q, limit=limit, source_kinds=source_kinds)
    ranked = rerank(rows, now=now, vehicle_ids=set(), contact_id=None)[:limit]
    labelled = await label_chunks(db, ranked, contact_id=None)
    items = []
    for d in labelled:
        d["snippet"] = (d["text"][:240] + "…") if len(d["text"]) > 240 else d["text"]
        d.pop("text", None)
        items.append(d)
    return {"items": items, "total": len(items), "query": q,
            "acl": {k: acl[k] for k in ("is_owner", "costs", "contacts", "messages", "assigned_only", "external")}}
