"""Import requests, auction candidates, translations and bid packets (spec §8.1–8.2, §5.2 IRQ side).

Lifecycle: inquiry → qualification → deposit_pending → active_search → purchased → delivered → closed,
with `paused` as an overlay. Active Search opens only when the agreement is signed (with evidence), the
configured deposit is confirmed from payment evidence and the versioned requirements are usable; the
gate list is structured and never invented.

Candidates are identified by provider + auction house + lot + auction date; matching is many-to-many
and private per request (invariant 8). Consequential external requests (translation request, bid
submission) persist ExternalAction intents under exact approval or fall back to a manual task with
the exact content — never an inline send, never a claimed receipt.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import select

from ..core.errors import Blocked, Conflict, Denied, NotFound, ValidationFailed
from ..core.ids import new_id, stable_hash
from ..core.money import convert, parse_amount, quantize
from ..core.time import PHOENIX, TOKYO, ensure_aware, fmt_local, to_zone
from ..domain.commands import REGISTRY, CommandContext, command, dispatch
from ..models.contacts import Contact
from ..models.legacy import Setting
from ..models.runtime import ExternalAction
from ..models.sourcing import IR_LIFECYCLE, Bid, Candidate, CandidateMatch, ImportRequest, Translation
from ..models.vehicles import Vehicle
from . import approvals as approvals_svc
from . import requirements as reqs
from ..adapters import auction_source, translation_docs

log = logging.getLogger("azkt.sourcing")

PRE_SEARCH = ("inquiry", "qualification", "deposit_pending")
OPEN_STATUSES = ("inquiry", "qualification", "deposit_pending", "active_search")
MATCHABLE = ("active_search",)
AGREEMENT_STATUSES = ("none", "sent", "signed")
ACTIVE_BID_STATUSES = ("pending_approval", "approved", "submitted")
FINAL_MATCH_STATES = ("won", "lost", "passed")
TEAMS_SENDER = None   # adapter hook: async fn(route: dict, content: str, action) -> receipt. None = no Teams adapter.


# ── time / money helpers ─────────────────────────────────────────────────────
def dual_time(dt: datetime | None, source: str | None = None) -> dict:
    """Every auction/deadline instant is shown in UTC, Tokyo and Phoenix (spec §8.2 step 1)."""
    if dt is None:
        return {"utc": None, "tokyo": "Not recorded", "phoenix": "Not recorded", "tokyo_iso": None, "phoenix_iso": None,
                "source": source}
    dt = ensure_aware(dt)
    return {"utc": dt.isoformat(), "tokyo": fmt_local(dt, TOKYO), "phoenix": fmt_local(dt, PHOENIX),
            "tokyo_iso": to_zone(dt, TOKYO).isoformat(), "phoenix_iso": to_zone(dt, PHOENIX).isoformat(), "source": source}


def _iso(dt: datetime | None) -> str | None:
    return ensure_aware(dt).isoformat() if dt else None


def _dec(d: Decimal | None) -> str | None:
    """Plain decimal text (never exponent notation from the driver)."""
    return None if d is None else format(d, "f")


def _money(amount: Decimal | None, currency: str | None) -> dict | None:
    if amount is None:
        return None
    return {"amount": _dec(amount), "currency": currency}


# ── serializers ──────────────────────────────────────────────────────────────
def gates(r: ImportRequest) -> list[dict]:
    """Structured Active Search gate list (spec §8.1). Unset rules block with an explicit reason."""
    out = []
    ag_ok = r.agreement_status == "signed" and bool((r.agreement_evidence or {}).get("source_ref"))
    out.append({"key": "agreement", "ok": ag_ok, "status": r.agreement_status,
                "reason": None if ag_ok else ("Agreement signed but no evidence recorded" if r.agreement_status == "signed"
                                              else f"Agreement {r.agreement_status if r.agreement_status != 'none' else 'not recorded'}")})
    if not r.deposit_rule:
        out.append({"key": "deposit", "ok": False, "status": r.deposit_status,
                    "reason": "Deposit rule not configured — the owner must set the required amount and currency"})
    else:
        dep_ok = r.deposit_status == "confirmed"
        out.append({"key": "deposit", "ok": dep_ok, "status": r.deposit_status,
                    "reason": None if dep_ok else f"Deposit {r.deposit_status} (required {r.deposit_rule.get('amount')} {r.deposit_rule.get('currency')})"})
    ok, why = reqs.usable(r.requirements or [])
    out.append({"key": "requirements", "ok": ok, "status": f"v{r.requirements_version}", "reason": why})
    return out


def gate_decision(r: ImportRequest) -> dict:
    g = gates(r)
    blocked = [x for x in g if not x["ok"]]
    return {"decision": "Blocked" if blocked else "Allowed", "gates": g, "reasons": [x["reason"] for x in blocked]}


def serialize_request(r: ImportRequest) -> dict:
    return {
        "id": r.id, "version": r.version, "contact_id": r.contact_id, "opportunity_id": r.opportunity_id, "title": r.title,
        "status": r.status, "lifecycle": list(IR_LIFECYCLE), "paused": bool(r.paused), "paused_reason": r.paused_reason,
        "paused_at": _iso(r.paused_at), "requirements": list(r.requirements or []), "requirement_tiers": reqs.tiers(r.requirements or []),
        "requirements_version": r.requirements_version, "requirements_history": list(r.requirements_history or []),
        "budget_amount": _dec(r.budget_amount), "budget_currency": r.budget_currency,
        "agreement_id": r.agreement_id, "agreement_status": r.agreement_status, "agreement_evidence": dict(r.agreement_evidence or {}),
        "deposit_rule": dict(r.deposit_rule or {}), "deposit_status": r.deposit_status, "deposit_evidence": dict(r.deposit_evidence or {}),
        "deposit_confirmed_at": _iso(r.deposit_confirmed_at), "deposit_invoice_id": r.deposit_invoice_id,
        "active_search_gate": gate_decision(r), "purchased_vehicle_id": r.purchased_vehicle_id,
        "purchase_evidence": dict(r.purchase_evidence or {}), "purchased_at": _iso(r.purchased_at),
        "delivered_at": _iso(r.delivered_at), "closed_at": _iso(r.closed_at), "close_reason": r.close_reason,
        "exclusions": list(r.exclusions or []), "notes": r.notes, "next_check_at": _iso(r.next_check_at),
        "source_ref": r.source_ref, "legacy_irq_id": r.legacy_irq_id, "lifecycle_history": list(r.lifecycle_history or []),
        "extra": dict(r.extra or {}), "created_at": _iso(r.created_at), "updated_at": _iso(r.updated_at),
    }


def serialize_candidate(c: Candidate) -> dict:
    return {
        "id": c.id, "version": c.version, "provider": c.provider, "auction_house": c.auction_house, "lot_no": c.lot_no,
        "identity": f"{c.provider}:{c.auction_house}:{c.lot_no}:{_iso(c.auction_at)}",
        "auction_at": dual_time(c.auction_at, c.auction_at_source), "deadline_at": dual_time(c.deadline_at, c.deadline_source),
        "deadline_passed": bool(c.deadline_at and ensure_aware(c.deadline_at) < datetime.now(timezone.utc)),
        "source_url": c.source_url, "title": c.title, "frame_raw": c.frame_raw, "specs": dict(c.specs or {}),
        "spec_sources": dict((c.extra or {}).get("spec_sources") or {}), "snapshot": dict(c.snapshot or {}),
        "snapshot_version": c.snapshot_version, "snapshot_hash": c.snapshot_hash, "images": list(c.images or []),
        "status": c.status, "discovered_at": _iso(c.discovered_at), "last_seen_at": _iso(c.last_seen_at),
        "ingest_count": c.ingest_count, "vehicle_id": c.vehicle_id, "result": dict(c.result or {}),
        "created_at": _iso(c.created_at), "updated_at": _iso(c.updated_at),
    }


def serialize_match(m: CandidateMatch, *, include_draft: bool = True) -> dict:
    ex = dict(m.extra or {})
    d = {
        "id": m.id, "version": m.version, "candidate_id": m.candidate_id, "import_request_id": m.import_request_id,
        "requirements_version": m.requirements_version, "outcomes": list(m.outcomes or []), "score": m.score,
        "preference_score": ex.get("preference_score"), "mandatory_fail": bool(m.mandatory_fail),
        "mandatory_unknown": bool(m.mandatory_unknown), "bid_ready": bool(m.bid_ready), "status": m.status,
        "rejected_reason": m.rejected_reason, "needs_confirmation": ex.get("needs_confirmation") or [],
        "stale": bool(m.stale), "stale_reason": m.stale_reason, "evaluated_at": _iso(m.evaluated_at),
        "translation_id": m.translation_id, "translation_revision_no": m.translation_revision_no, "bid_id": m.bid_id,
        "buyer_draft_id": m.buyer_draft_id, "buyer_message_sent_at": _iso(m.buyer_message_sent_at),
        "interest": ex.get("interest"), "correction_case_id": ex.get("correction_case_id"),
    }
    if include_draft:
        d["buyer_draft"] = ex.get("buyer_draft")
    return d


def serialize_translation(t: Translation, action: ExternalAction | None = None) -> dict:
    return {
        "id": t.id, "version": t.version, "candidate_id": t.candidate_id, "status": t.status,
        "request_channel": t.request_channel, "request_action_id": t.request_action_id,
        "request_action_state": (action.state if action else None), "request_receipt": (dict(action.receipt or {}) if action else {}),
        "manual_task_id": t.manual_task_id, "requested_at": _iso(t.requested_at), "request_content": t.request_content,
        "approval_id": t.approval_id, "doc_provider": t.doc_provider, "doc_ref": t.doc_ref, "doc_revision": t.doc_revision,
        "doc_modified_at": _iso(t.doc_modified_at), "revision_no": t.revision_no, "required_sections": list(t.required_sections or []),
        "completeness": dict(t.completeness or {}), "identity_check": dict(t.identity_check or {}),
        "excerpts": list(t.excerpts or []), "findings": dict(t.findings or {}), "completed_at": _iso(t.completed_at),
        "last_checked_at": _iso(t.last_checked_at), "revision_history": list(t.revision_history or []),
        "created_at": _iso(t.created_at), "updated_at": _iso(t.updated_at),
    }


def serialize_bid(b: Bid) -> dict:
    return {
        "id": b.id, "version": b.version, "candidate_id": b.candidate_id, "import_request_id": b.import_request_id,
        "max_amount": _dec(b.max_amount), "currency": b.currency, "fee_basis": b.fee_basis,
        "fx_estimate": dict(b.fx_estimate or {}), "deadline_at": dual_time(b.deadline_at, (b.packet or {}).get("deadline", {}).get("source")),
        "auction_house": b.auction_house, "lot_no": b.lot_no, "auction_at": dual_time(b.auction_at),
        "packet": dict(b.packet or {}), "packet_hash": b.packet_hash, "approval_id": b.approval_id, "status": b.status,
        "submission_channel": b.submission_channel, "submission_task_id": b.submission_task_id,
        "submission_action_id": b.submission_action_id, "submission_evidence": dict(b.submission_evidence or {}),
        "submitted_at": _iso(b.submitted_at), "result": dict(b.result or {}), "result_evidence": dict(b.result_evidence or {}),
        "result_at": _iso(b.result_at), "invalidated_reason": b.invalidated_reason,
        "translation_revision_no": b.translation_revision_no, "requirements_version": b.requirements_version,
        "extra": dict(b.extra or {}), "created_at": _iso(b.created_at), "updated_at": _iso(b.updated_at),
    }


# ── loaders ──────────────────────────────────────────────────────────────────
async def _req(ctx: CommandContext, request_id: str, expected_version: int | None = None, *, lock: bool = True) -> ImportRequest:
    q = select(ImportRequest).where(ImportRequest.id == request_id)
    r = (await ctx.db.execute(q.with_for_update() if lock else q)).scalar_one_or_none()
    if r is None:
        raise NotFound("import request not found")
    if expected_version is not None and r.version != expected_version:
        raise Conflict("import request changed since you loaded it", current_version=r.version)
    return r


async def _cand(ctx: CommandContext, candidate_id: str, *, lock: bool = False) -> Candidate:
    q = select(Candidate).where(Candidate.id == candidate_id)
    c = (await ctx.db.execute(q.with_for_update() if lock else q)).scalar_one_or_none()
    if c is None:
        raise NotFound("candidate not found")
    return c


async def _match(ctx: CommandContext, match_id: str, expected_version: int | None = None) -> CandidateMatch:
    m = (await ctx.db.execute(select(CandidateMatch).where(CandidateMatch.id == match_id).with_for_update())).scalar_one_or_none()
    if m is None:
        raise NotFound("candidate match not found")
    if expected_version is not None and m.version != expected_version:
        raise Conflict("match changed since you loaded it", current_version=m.version)
    return m


async def _translation(ctx: CommandContext, translation_id: str, expected_version: int | None = None) -> Translation:
    t = (await ctx.db.execute(select(Translation).where(Translation.id == translation_id).with_for_update())).scalar_one_or_none()
    if t is None:
        raise NotFound("translation not found")
    if expected_version is not None and t.version != expected_version:
        raise Conflict("translation changed since you loaded it", current_version=t.version)
    return t


async def _bid(ctx: CommandContext, bid_id: str, expected_version: int | None = None) -> Bid:
    b = (await ctx.db.execute(select(Bid).where(Bid.id == bid_id).with_for_update())).scalar_one_or_none()
    if b is None:
        raise NotFound("bid not found")
    if expected_version is not None and b.version != expected_version:
        raise Conflict("bid changed since you loaded it", current_version=b.version)
    return b


async def current_translation(db, candidate_id: str) -> Translation | None:
    rows = (await db.execute(select(Translation).where(Translation.candidate_id == candidate_id,
                                                        Translation.status != "invalidated")
                             .order_by(Translation.created_at.desc()))).scalars().all()
    return rows[0] if rows else None


async def teams_route(db) -> dict | None:
    """Exporter Teams route, if one is configured (connection `teams` or setting `sourcing.teams_route`)."""
    try:
        from ..models.comms import Connection
        row = (await db.execute(select(Connection).where(Connection.provider == "teams",
                                                          Connection.status == "connected"))).scalars().first()
        if row is not None and (row.config or {}).get("chat_id"):
            return {"connection_id": row.id, "chat_id": row.config["chat_id"], "account": row.config.get("account"),
                    "account_identity": row.account_identity}
    except Exception:  # noqa: BLE001 - comms model owned elsewhere; absence is a plain "not configured"
        log.debug("teams connection lookup unavailable", exc_info=True)
    s = await db.get(Setting, "sourcing.teams_route")
    if s is not None and isinstance(s.value, dict):
        v = s.value.get("data") if "data" in s.value else s.value
        if isinstance(v, dict) and v.get("chat_id"):
            return {"connection_id": None, "chat_id": v["chat_id"], "account": v.get("account"), "account_identity": v.get("account_identity")}
    return None


# ── lifecycle ────────────────────────────────────────────────────────────────
def _set_status(ctx: CommandContext, r: ImportRequest, to: str, reason: str | None = None) -> bool:
    if to not in IR_LIFECYCLE:
        raise ValidationFailed(f"status must be one of {IR_LIFECYCLE}")
    if r.status == to:
        return False
    frm = r.status
    r.status = to
    r.lifecycle_history = list(r.lifecycle_history or []) + [{"from": frm, "to": to, "at": ctx.now.isoformat(),
                                                              "by": ctx.actor.user_id, "reason": reason}]
    ctx.record(f"Import request {to.replace('_', ' ')}: {r.title or r.id[:8]}" + (f" — {reason}" if reason else ""),
               entity_kind="import_request", entity_id=r.id, kind="task", state=to, details={"from": frm, "to": to})
    ctx.emit("import_request.changed", aggregate_type="import_request", aggregate_id=r.id, aggregate_version=r.version,
             payload={"import_request_id": r.id, "change": "status", "from": frm, "to": to, "contact_id": r.contact_id})
    return True


def _advance(ctx: CommandContext, r: ImportRequest) -> dict:
    """Move forward through the pre-search stages only when the evidence-backed gates allow it."""
    decision = gate_decision(r)
    if r.status in PRE_SEARCH:
        if decision["decision"] == "Allowed":
            _set_status(ctx, r, "active_search", "agreement signed, deposit confirmed, requirements usable")
        elif r.agreement_status == "signed" and r.status in ("inquiry", "qualification"):
            _set_status(ctx, r, "deposit_pending", "agreement signed; waiting for deposit")
        elif r.status == "inquiry" and (r.agreement_status != "none" or reqs.usable(r.requirements or [])[0]):
            _set_status(ctx, r, "qualification", "qualification started")
    return gate_decision(r)


def _emit_request(ctx: CommandContext, r: ImportRequest, change: str, **extra) -> None:
    ctx.emit("import_request.changed", aggregate_type="import_request", aggregate_id=r.id, aggregate_version=r.version,
             payload={"import_request_id": r.id, "change": change, "status": r.status, "contact_id": r.contact_id, **extra})


# ── import request commands ─────────────────────────────────────────────────
class RequestCreateIn(BaseModel):
    contact_id: str
    opportunity_id: str | None = None
    title: str = ""
    requirements: list[dict] = Field(default_factory=list)
    budget_amount: Decimal | None = None
    budget_currency: str | None = None
    notes: str = ""
    source_ref: str | None = None     # creation dedupe key (message id, legacy IRQ id ...)
    legacy_irq_id: str | None = None
    next_check_at: datetime | None = None


@command("import_requests.create", input=RequestCreateIn, perm="requests.write", action_class="internal",
         description="Create an import request (lifecycle starts at Inquiry). Same source_ref/opportunity returns the existing one.")
async def request_create(ctx: CommandContext, inp: RequestCreateIn) -> dict:
    if inp.source_ref:
        ex = (await ctx.db.execute(select(ImportRequest).where(ImportRequest.source_ref == inp.source_ref))).scalar_one_or_none()
        if ex is not None:
            return {"request": serialize_request(ex), "created": False, "matched_by": "source_ref"}
    if inp.opportunity_id:
        ex = (await ctx.db.execute(select(ImportRequest).where(ImportRequest.opportunity_id == inp.opportunity_id))).scalar_one_or_none()
        if ex is not None:
            return {"request": serialize_request(ex), "created": False, "matched_by": "opportunity_id"}
    if await ctx.db.get(Contact, inp.contact_id) is None:
        raise NotFound("contact not found")
    requirements = reqs.normalize_requirements(inp.requirements)
    if inp.budget_amount is not None and not inp.budget_currency:
        raise ValidationFailed("budget_currency required with budget_amount")
    r = ImportRequest(contact_id=inp.contact_id, opportunity_id=inp.opportunity_id, title=inp.title.strip(),
                      status="inquiry", requirements=requirements, requirements_version=1,
                      requirements_history=[{"version": 1, "requirements": requirements, "evidence": None,
                                             "at": ctx.now.isoformat(), "by": ctx.actor.user_id, "reason": "created"}],
                      budget_amount=(quantize(inp.budget_amount, inp.budget_currency) if inp.budget_amount is not None else None),
                      budget_currency=(inp.budget_currency.upper() if inp.budget_currency else None), notes=inp.notes,
                      source_ref=inp.source_ref, legacy_irq_id=inp.legacy_irq_id, next_check_at=inp.next_check_at,
                      lifecycle_history=[{"from": None, "to": "inquiry", "at": ctx.now.isoformat(), "by": ctx.actor.user_id, "reason": "created"}],
                      created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
    ctx.db.add(r)
    await ctx.db.flush()
    ctx.changed.append({"kind": "import_request", "id": r.id, "version": r.version})
    ctx.record(f"Created import request: {r.title or r.id[:8]}", entity_kind="import_request", entity_id=r.id, kind="task",
               state="inquiry", details={"contact_id": r.contact_id, "opportunity_id": r.opportunity_id})
    _emit_request(ctx, r, "created")
    if inp.opportunity_id:
        await _link_opportunity_side(ctx, r)
    return {"request": serialize_request(r), "created": True}


async def _link_opportunity_side(ctx: CommandContext, r: ImportRequest) -> dict:
    """Mirror the link on the opportunity through the sales domain command when available."""
    if "sales.link_request" not in REGISTRY or not r.opportunity_id:
        return {"linked": False, "reason": "sales.link_request not registered"}
    try:
        await dispatch(ctx.child(), "sales.link_request", {"opportunity_id": r.opportunity_id, "import_request_id": r.id}, commit=False)
        return {"linked": True}
    except (Denied, Conflict, NotFound) as e:
        ctx.record(f"Opportunity link not mirrored: {e.message}", entity_kind="import_request", entity_id=r.id, kind="task",
                   state=r.status, exception=True, details=e.detail)
        return {"linked": False, "reason": e.message}


class RequestUpdateIn(BaseModel):
    request_id: str
    expected_version: int | None = None
    title: str | None = None
    notes: str | None = None
    budget_amount: Decimal | None = None
    budget_currency: str | None = None
    next_check_at: datetime | None = None
    extra: dict | None = None


@command("import_requests.update", input=RequestUpdateIn, perm="requests.write", action_class="internal",
         description="Edit title/notes/budget/next check. Requirements change only through revise_requirements.")
async def request_update(ctx: CommandContext, inp: RequestUpdateIn) -> dict:
    r = await _req(ctx, inp.request_id, inp.expected_version)
    if inp.title is not None:
        r.title = inp.title.strip()
    if inp.notes is not None:
        r.notes = inp.notes
    if inp.budget_amount is not None:
        cur = (inp.budget_currency or r.budget_currency)
        if not cur:
            raise ValidationFailed("budget_currency required with budget_amount")
        r.budget_amount = quantize(inp.budget_amount, cur)
        r.budget_currency = cur.upper()
    elif inp.budget_currency:
        r.budget_currency = inp.budget_currency.upper()
    if inp.next_check_at is not None:
        r.next_check_at = inp.next_check_at
    if inp.extra is not None:
        r.extra = {**(r.extra or {}), **inp.extra}
    ctx.touch(r, "import_request")
    ctx.record(f"Updated import request: {r.title or r.id[:8]}", entity_kind="import_request", entity_id=r.id, kind="task", state=r.status)
    _emit_request(ctx, r, "updated")
    return {"request": serialize_request(r)}


class ReviseRequirementsIn(BaseModel):
    request_id: str
    requirements: list[dict]
    source_ref: str | None = None      # buyer evidence (message id / call note) — required when a must-have changes
    note: str | None = None
    expected_version: int | None = None


@command("import_requests.revise_requirements", input=ReviseRequirementsIn, perm="requests.write", action_class="internal",
         description="Authorized requirements revision. Changing a must-have needs buyer evidence; bumps the version and "
                     "invalidates bid readiness of existing matches until they are re-evaluated.")
async def revise_requirements(ctx: CommandContext, inp: ReviseRequirementsIn) -> dict:
    r = await _req(ctx, inp.request_id, inp.expected_version)
    if r.status in ("purchased", "delivered", "closed"):
        raise Blocked(f"import request is {r.status}")
    new = reqs.normalize_requirements(inp.requirements)
    old = list(r.requirements or [])
    changed_must = reqs.must_changed(old, new)
    if changed_must and not inp.source_ref:
        raise Blocked("changing a must-have requirement needs buyer evidence (source_ref); a stage override cannot relax it",
                      must_changed=True)
    if new == old:
        return {"request": serialize_request(r), "revised": False}
    r.requirements = new
    r.requirements_version = (r.requirements_version or 1) + 1
    r.requirements_history = list(r.requirements_history or []) + [{
        "version": r.requirements_version, "requirements": new, "evidence": inp.source_ref, "note": inp.note,
        "must_changed": changed_must, "at": ctx.now.isoformat(), "by": ctx.actor.user_id, "reason": "revised"}]
    ctx.touch(r, "import_request")
    # existing matches must be re-evaluated; pending bid approvals bound to the old version are invalid
    matches = (await ctx.db.execute(select(CandidateMatch).where(CandidateMatch.import_request_id == r.id))).scalars().all()
    stale_n, invalidated = 0, 0
    for m in matches:
        if m.status in FINAL_MATCH_STATES:
            continue
        m.stale = True
        m.stale_reason = f"requirements revised to v{r.requirements_version}"
        m.bid_ready = False
        ctx.touch(m, "candidate_match")
        stale_n += 1
    bids = (await ctx.db.execute(select(Bid).where(Bid.import_request_id == r.id, Bid.status.in_(("pending_approval", "approved"))))).scalars().all()
    for b in bids:
        await _invalidate_bid(ctx, b, f"buyer requirements revised to v{r.requirements_version}")
        invalidated += 1
    ctx.record(f"Requirements revised to v{r.requirements_version}: {r.title or r.id[:8]}", entity_kind="import_request", entity_id=r.id,
               kind="fact", state=r.status, sources=[inp.source_ref] if inp.source_ref else None,
               details={"must_changed": changed_must, "stale_matches": stale_n, "invalidated_bids": invalidated})
    _emit_request(ctx, r, "requirements_revised", requirements_version=r.requirements_version, must_changed=changed_must)
    _advance(ctx, r)
    return {"request": serialize_request(r), "revised": True, "must_changed": changed_must, "stale_matches": stale_n,
            "invalidated_bids": invalidated}


class AttachOpportunityIn(BaseModel):
    request_id: str
    opportunity_id: str
    expected_version: int | None = None


@command("import_requests.attach_opportunity", input=AttachOpportunityIn, perm="requests.write", action_class="internal",
         description="Link the IRQ lead (opportunity) this request fulfils; exactly one per request and per opportunity.")
async def attach_opportunity(ctx: CommandContext, inp: AttachOpportunityIn) -> dict:
    r = await _req(ctx, inp.request_id, inp.expected_version)
    if r.opportunity_id == inp.opportunity_id:
        return {"request": serialize_request(r), "attached": False, "idempotent": True}
    if r.opportunity_id:
        raise Conflict("import request is already linked to another opportunity", opportunity_id=r.opportunity_id)
    other = (await ctx.db.execute(select(ImportRequest).where(ImportRequest.opportunity_id == inp.opportunity_id))).scalar_one_or_none()
    if other is not None:
        raise Conflict("that opportunity is already linked to another import request", import_request_id=other.id)
    r.opportunity_id = inp.opportunity_id
    ctx.touch(r, "import_request")
    ctx.record("Linked opportunity to import request", entity_kind="import_request", entity_id=r.id, kind="task", state=r.status,
               details={"opportunity_id": inp.opportunity_id})
    _emit_request(ctx, r, "opportunity_attached", opportunity_id=inp.opportunity_id)
    mirrored = await _link_opportunity_side(ctx, r)
    return {"request": serialize_request(r), "attached": True, "opportunity_link": mirrored}


class SetAgreementIn(BaseModel):
    request_id: str
    agreement_id: str | None = None
    status: str = "sent"                # none|sent|signed
    source_ref: str | None = None       # signed copy / message id — required for signed
    signed_at: datetime | None = None
    expected_version: int | None = None


@command("import_requests.set_agreement", input=SetAgreementIn, perm="requests.write", action_class="internal",
         description="Record the agreement link/status. `signed` needs evidence (source_ref).")
async def set_agreement(ctx: CommandContext, inp: SetAgreementIn) -> dict:
    if inp.status not in AGREEMENT_STATUSES:
        raise ValidationFailed(f"status must be one of {AGREEMENT_STATUSES}")
    r = await _req(ctx, inp.request_id, inp.expected_version)
    if inp.status == "signed" and not inp.source_ref:
        raise Blocked("a signed agreement needs evidence (source_ref)")
    r.agreement_id = inp.agreement_id or r.agreement_id
    r.agreement_status = inp.status
    if inp.status == "signed":
        # signed_at is the buyer's signing time from the evidence; unknown stays unknown (never "now")
        r.agreement_evidence = {"source_ref": inp.source_ref, "signed_at": _iso(inp.signed_at), "recorded_at": ctx.now.isoformat(),
                                "recorded_by": ctx.actor.user_id, "agreement_id": r.agreement_id}
    ctx.touch(r, "import_request")
    ctx.record(f"Agreement {inp.status}: {r.title or r.id[:8]}", entity_kind="import_request", entity_id=r.id, kind="fact",
               state=r.status, sources=[inp.source_ref] if inp.source_ref else None, details={"agreement_id": r.agreement_id})
    _emit_request(ctx, r, "agreement", agreement_status=r.agreement_status)
    decision = _advance(ctx, r)
    return {"request": serialize_request(r), "gate": decision}


class SetDepositRuleIn(BaseModel):
    request_id: str
    amount: Decimal
    currency: str
    source_ref: str | None = None       # agreement clause / terms reference
    expected_version: int | None = None


@command("import_requests.set_deposit_rule", input=SetDepositRuleIn, perm="requests.write", action_class="owner_only",
         approval_kind="terms", summary=lambda p: f"Set deposit rule {p.amount} {p.currency}",
         description="Owner sets the exact required deposit (amount + currency). Unset rules keep the deposit gate blocked.")
async def set_deposit_rule(ctx: CommandContext, inp: SetDepositRuleIn) -> dict:
    if inp.amount <= 0 or len(inp.currency) != 3:
        raise ValidationFailed("deposit rule needs a positive amount and an ISO currency")
    r = await _req(ctx, inp.request_id, inp.expected_version)
    cur = inp.currency.upper()
    r.deposit_rule = {"amount": str(quantize(inp.amount, cur)), "currency": cur, "source_ref": inp.source_ref,
                      "set_by": ctx.actor.user_id, "set_at": ctx.now.isoformat()}
    if r.deposit_status == "unset":
        r.deposit_status = "pending"
    elif r.deposit_status in ("confirmed", "partial"):
        # recorded payment evidence is re-checked against the new rule; it is never re-confirmed by assumption
        ev = dict(r.deposit_evidence or {})
        received = parse_amount(ev.get("amount") or "0") if ev.get("currency") == cur else None
        if received is None:
            r.deposit_status = "pending"
            r.deposit_evidence = {**ev, "remaining": None, "note": f"recorded {ev.get('amount')} {ev.get('currency')} needs explicit conversion to {cur}"}
            ctx.record(f"Deposit rule currency changed to {cur}; recorded {ev.get('currency')} evidence needs explicit conversion",
                       entity_kind="import_request", entity_id=r.id, kind="payment", state=r.status, visibility="owner", exception=True)
        elif received < quantize(inp.amount, cur):
            r.deposit_status = "partial"
            r.deposit_evidence = {**ev, "remaining": str(quantize(inp.amount, cur) - received), "required": r.deposit_rule["amount"]}
        else:
            newly = r.deposit_status != "confirmed"
            r.deposit_status = "confirmed"
            r.deposit_evidence = {**ev, "remaining": None, "required": r.deposit_rule["amount"],
                                  "overpaid": str(received - quantize(inp.amount, cur)) if received > quantize(inp.amount, cur) else None}
            if newly:
                r.deposit_confirmed_at = ctx.now
                r.deposit_evidence["confirmed_at"] = ctx.now.isoformat()
                ctx.emit("deposit.confirmed", aggregate_type="import_request", aggregate_id=r.id, aggregate_version=r.version,
                         payload={"import_request_id": r.id, "payment_id": ev.get("payment_id"), "amount": str(received), "currency": cur,
                                  "opportunity_id": r.opportunity_id, "source_ref": ev.get("source_ref"), "reason": "deposit rule revised"})
    ctx.touch(r, "import_request")
    ctx.record(f"Deposit rule set: {r.deposit_rule['amount']} {cur}", entity_kind="import_request", entity_id=r.id, kind="payment",
               state=r.status, visibility="owner", sources=[inp.source_ref] if inp.source_ref else None)
    _emit_request(ctx, r, "deposit_rule")
    decision = _advance(ctx, r)
    return {"request": serialize_request(r), "gate": decision}


class ConfirmDepositIn(BaseModel):
    request_id: str
    payment_id: str
    amount: Decimal
    currency: str
    source_ref: str                    # payment evidence (provider payment / event id)
    confirmed_at: datetime | None = None
    expected_version: int | None = None


@command("import_requests.confirm_deposit", input=ConfirmDepositIn, perm="finance.write", action_class="internal",
         description="Finance handoff (spec §5.2): confirm the deposit from payment evidence against the configured rule. "
                     "Idempotent per payment; partial/currency mismatches stay explicit and block the gate.")
async def confirm_deposit(ctx: CommandContext, inp: ConfirmDepositIn) -> dict:
    r = await _req(ctx, inp.request_id, inp.expected_version)
    ev = dict(r.deposit_evidence or {})
    payments = list(ev.get("payments") or [])
    # a payment already recorded against this request is never counted twice (invariant 6)
    if ev.get("payment_id") == inp.payment_id or any(p.get("payment_id") == inp.payment_id for p in payments):
        return {"request": serialize_request(r), "confirmed": r.deposit_status == "confirmed", "changed": False, "idempotent": True,
                "gate": gate_decision(r)}
    if r.deposit_status == "confirmed":
        raise Conflict("deposit already confirmed from a different payment", payment_id=ev.get("payment_id"))
    if not r.deposit_rule:
        raise Blocked("deposit rule not configured — the owner must set the required amount and currency before the gate can open",
                      gate=gate_decision(r))
    rule_amt, rule_cur = parse_amount(r.deposit_rule["amount"]), r.deposit_rule["currency"]
    cur = inp.currency.upper()
    if cur != rule_cur:
        raise Blocked(f"deposit currency {cur} does not match the required {rule_cur}; explicit conversion needed",
                      required=r.deposit_rule, received=str(inp.amount))
    amt = quantize(inp.amount, cur)
    if amt <= 0:
        raise ValidationFailed("deposit amount must be positive")
    payments.append({"payment_id": inp.payment_id, "amount": str(amt), "currency": cur, "source_ref": inp.source_ref,
                     "received_at": _iso(inp.confirmed_at), "recorded_at": ctx.now.isoformat(), "recorded_by": ctx.actor.user_id})
    total = quantize(sum((parse_amount(p["amount"]) for p in payments), Decimal("0")), cur)
    if total < rule_amt:
        # partial payment: recorded and visible with the remaining balance (spec §5.2); the gate stays blocked.
        # This is a persisted fact, so it returns a Blocked decision instead of raising (a raise would roll it back).
        remaining = rule_amt - total
        r.deposit_status = "partial"
        r.deposit_evidence = {**ev, "payment_id": inp.payment_id, "amount": str(total), "currency": cur, "source_ref": inp.source_ref,
                              "payments": payments, "remaining": str(remaining), "required": str(rule_amt)}
        ctx.touch(r, "import_request")
        ctx.record(f"Partial deposit {amt} {cur} (received {total}, remaining {remaining})", entity_kind="import_request", entity_id=r.id,
                   kind="payment", state=r.status, sources=[inp.source_ref], exception=True, details={"payment_id": inp.payment_id})
        _emit_request(ctx, r, "deposit_partial", remaining=str(remaining), payment_id=inp.payment_id)
        return {"request": serialize_request(r), "confirmed": False, "changed": True, "partial": True, "remaining": str(remaining),
                "currency": cur, "decision": "Blocked", "reasons": [f"partial deposit; {remaining} {cur} outstanding"],
                "gate": gate_decision(r)}
    r.deposit_status = "confirmed"
    r.deposit_confirmed_at = ensure_aware(inp.confirmed_at) or ctx.now
    r.deposit_evidence = {"payment_id": inp.payment_id, "amount": str(total), "currency": cur, "source_ref": inp.source_ref,
                          "payments": payments, "confirmed_at": r.deposit_confirmed_at.isoformat(),
                          "overpaid": str(total - rule_amt) if total > rule_amt else None, "confirmed_by": ctx.actor.user_id}
    ctx.touch(r, "import_request")
    ctx.record(f"Deposit confirmed {total} {cur}: {r.title or r.id[:8]}", entity_kind="import_request", entity_id=r.id, kind="payment",
               state=r.status, sources=[inp.source_ref], details={"payment_id": inp.payment_id, "payments": len(payments)})
    ctx.emit("deposit.confirmed", aggregate_type="import_request", aggregate_id=r.id, aggregate_version=r.version,
             payload={"import_request_id": r.id, "payment_id": inp.payment_id, "amount": str(total), "currency": cur,
                      "opportunity_id": r.opportunity_id, "source_ref": inp.source_ref})
    decision = _advance(ctx, r)
    return {"request": serialize_request(r), "confirmed": True, "changed": True, "gate": decision}


class PauseIn(BaseModel):
    request_id: str
    reason: str | None = None
    expected_version: int | None = None


@command("import_requests.pause", input=PauseIn, perm="requests.write", action_class="internal",
         description="Pause as an overlay (matching/bidding stop; nothing is deleted).")
async def request_pause(ctx: CommandContext, inp: PauseIn) -> dict:
    r = await _req(ctx, inp.request_id, inp.expected_version)
    if r.paused:
        return {"request": serialize_request(r), "changed": False}
    r.paused, r.paused_reason, r.paused_at = True, inp.reason, ctx.now
    ctx.touch(r, "import_request")
    ctx.record(f"Paused import request: {r.title or r.id[:8]}" + (f" — {inp.reason}" if inp.reason else ""), entity_kind="import_request",
               entity_id=r.id, kind="task", state=r.status, exception=True)
    _emit_request(ctx, r, "paused", reason=inp.reason)
    return {"request": serialize_request(r), "changed": True}


@command("import_requests.resume", input=PauseIn, perm="requests.write", action_class="internal",
         description="Clear the paused overlay; pending work is revalidated by its own commands.")
async def request_resume(ctx: CommandContext, inp: PauseIn) -> dict:
    r = await _req(ctx, inp.request_id, inp.expected_version)
    if not r.paused:
        return {"request": serialize_request(r), "changed": False}
    r.paused, r.paused_reason, r.paused_at = False, None, None
    ctx.touch(r, "import_request")
    ctx.record(f"Resumed import request: {r.title or r.id[:8]}", entity_kind="import_request", entity_id=r.id, kind="task", state=r.status)
    _emit_request(ctx, r, "resumed")
    return {"request": serialize_request(r), "changed": True}


class RecordPurchaseIn(BaseModel):
    request_id: str
    vehicle_id: str | None = None
    vehicle: dict | None = None          # {title, make, model, model_year, frame_no_raw, color} to create one
    evidence: dict                        # {source_ref (required), amount, currency, purchased_at, note}
    bid_id: str | None = None             # only when the purchase came from a recorded AZKT bid result
    expected_version: int | None = None


@command("import_requests.record_purchase", input=RecordPurchaseIn, perm="requests.write", action_class="owner_only",
         approval_kind="other", summary=lambda p: "Record purchased vehicle for import request",
         description="Owner links exactly one purchased vehicle with purchase evidence. Same vehicle again is idempotent; "
                     "a different vehicle conflicts. A manual purchase never invents a bid AZKT placed.")
async def record_purchase(ctx: CommandContext, inp: RecordPurchaseIn) -> dict:
    r = await _req(ctx, inp.request_id, inp.expected_version)
    if not (inp.evidence or {}).get("source_ref"):
        raise Blocked("purchase evidence needs a source_ref (invoice / message / auction result)")
    if not inp.vehicle_id and not inp.vehicle:
        raise ValidationFailed("vehicle_id or vehicle details required")
    bid: Bid | None = None
    if inp.bid_id:
        bid = await _bid(ctx, inp.bid_id)
        if bid.import_request_id != r.id:
            raise Conflict("bid belongs to a different import request", bid_id=bid.id)
        if bid.status != "won":
            raise Blocked(f"bid is {bid.status}; a purchase can only cite a bid whose result is won", bid_status=bid.status)
    vehicle: Vehicle | None = None
    if inp.vehicle_id:
        vehicle = await ctx.db.get(Vehicle, inp.vehicle_id)
        if vehicle is None:
            raise NotFound("vehicle not found")
    if r.purchased_vehicle_id:
        if vehicle is not None and vehicle.id == r.purchased_vehicle_id:
            return {"request": serialize_request(r), "vehicle_id": vehicle.id, "recorded": False, "idempotent": True}
        if vehicle is None and _same_purchase(r, await ctx.db.get(Vehicle, r.purchased_vehicle_id), inp.vehicle or {}, inp.evidence):
            # a retry of the same purchase (same frame / same evidence) never creates a second vehicle
            return {"request": serialize_request(r), "vehicle_id": r.purchased_vehicle_id, "recorded": False, "idempotent": True}
        raise Conflict("import request already has a purchased vehicle", purchased_vehicle_id=r.purchased_vehicle_id)
    if vehicle is None:
        vehicle = await _create_vehicle(ctx, inp.vehicle or {}, r, candidate=None, evidence=inp.evidence,
                                        create_key=f"import_request_purchase:{r.id}")
    other = (await ctx.db.execute(select(ImportRequest).where(ImportRequest.purchased_vehicle_id == vehicle.id,
                                                              ImportRequest.id != r.id))).scalar_one_or_none()
    if other is not None:
        raise Conflict("that vehicle is already the purchase of another import request", import_request_id=other.id)
    evidence = {**inp.evidence, "bid_id": bid.id if bid else None, "manual": bid is None, "recorded_by": ctx.actor.user_id,
                "recorded_at": ctx.now.isoformat()}
    r.purchased_vehicle_id = vehicle.id
    r.purchase_evidence = evidence
    r.purchased_at = ensure_aware(_dt(inp.evidence.get("purchased_at") or inp.evidence.get("at")))  # unknown stays unknown
    if not vehicle.purchase_evidence:
        vehicle.purchase_evidence = {k: v for k, v in evidence.items() if k != "manual"} | {"import_request_id": r.id}
        vehicle.buyer_contact_id = vehicle.buyer_contact_id or r.contact_id
        ctx.touch(vehicle, "vehicle")
    ctx.touch(r, "import_request")
    ctx.record(f"Purchase recorded{' (manual, no AZKT bid)' if bid is None else ''}: {r.title or r.id[:8]}", entity_kind="import_request",
               entity_id=r.id, kind="fact", state="purchased", sources=[inp.evidence["source_ref"]],
               details={"vehicle_id": vehicle.id, "bid_id": bid.id if bid else None})
    _set_status(ctx, r, "purchased", "purchase evidence recorded")
    _emit_request(ctx, r, "purchased", vehicle_id=vehicle.id, bid_id=bid.id if bid else None)
    ctx.emit("vehicle.state_changed", aggregate_type="vehicle", aggregate_id=vehicle.id, aggregate_version=vehicle.version,
             payload={"vehicle_id": vehicle.id, "change": "purchased", "import_request_id": r.id, "source_ref": inp.evidence["source_ref"]})
    await _bridge_purchase_milestone(ctx, vehicle, inp.evidence)
    return {"request": serialize_request(r), "vehicle_id": vehicle.id, "recorded": True, "bid_id": bid.id if bid else None}


def _dt(v) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    from ..core.time import parse_iso
    try:
        return parse_iso(str(v))
    except ValueError:
        raise ValidationFailed(f"invalid datetime {v!r}")


def _same_purchase(r: ImportRequest, purchased: Vehicle | None, fields: dict, evidence: dict) -> bool:
    """A retried manual purchase names the same vehicle when the frame matches or the evidence reference matches."""
    if purchased is None:
        return False
    from .matching import normalize_frame
    frame = fields.get("frame_no_raw")
    if frame and purchased.frame_no_raw and normalize_frame(frame) == (purchased.frame_no_norm or normalize_frame(purchased.frame_no_raw)):
        return True
    ref = (evidence or {}).get("source_ref")
    return bool(ref) and (r.purchase_evidence or {}).get("source_ref") == ref


async def _create_vehicle(ctx: CommandContext, fields: dict, r: ImportRequest, *, candidate: Candidate | None, evidence: dict,
                          create_key: str) -> Vehicle:
    """Create the purchased vehicle through the vehicles domain (`vehicles.create`, retry-safe via create_key) when that
    command is registered; otherwise fall back to a direct row with the same facts. Buyer link, reservation and the
    purchase evidence are sourcing-owned facts set here in the same transaction."""
    try:
        from . import vehicles as _vehicles  # noqa: F401 - lazy: registers vehicles.create when the module exists
    except Exception:  # noqa: BLE001
        pass
    draft = _new_vehicle(ctx, fields, r, candidate=candidate, evidence=evidence)
    if "vehicles.create" in REGISTRY:
        res = await dispatch(ctx.child(), "vehicles.create", {
            "title": draft.title, "make": draft.make, "model": draft.model, "model_year": draft.model_year, "color": draft.color,
            "frame_no_raw": draft.frame_no_raw, "logistics_state": "purchased", "allocation": "reserved",
            "purchase_amount": str(draft.purchase_amount) if draft.purchase_amount is not None else None,
            "purchase_currency": draft.purchase_currency, "acquired_at": _iso(draft.acquired_at),
            "source_kind": evidence.get("source_kind") or "document", "source_ref": evidence.get("source_ref"),
            "origin_candidate_id": candidate.id if candidate else None, "create_key": create_key,
            "extra": {"import_request_id": r.id, "auction_url": candidate.source_url if candidate else None}}, commit=False)
        vehicle = await ctx.db.get(Vehicle, res.data["vehicle"]["id"])
        vehicle.buyer_contact_id = vehicle.buyer_contact_id or r.contact_id
        vehicle.commercial_state = "reserved" if vehicle.commercial_state == "not_listed" else vehicle.commercial_state
        vehicle.situation = vehicle.situation or "Purchased for import request"
        if candidate is not None and not vehicle.auction_url:
            vehicle.auction_url = candidate.source_url
        return vehicle
    ctx.db.add(draft)
    await ctx.db.flush()
    ctx.changed.append({"kind": "vehicle", "id": draft.id, "version": draft.version})
    return draft


def _new_vehicle(ctx: CommandContext, fields: dict, r: ImportRequest, *, candidate: Candidate | None, evidence: dict) -> Vehicle:
    from .matching import normalize_frame
    specs = dict(candidate.specs or {}) if candidate else {}
    make = fields.get("make") or specs.get("make")
    model = fields.get("model") or specs.get("model")
    year = fields.get("model_year") or specs.get("year")
    try:
        year = int(year) if year is not None else None
    except (TypeError, ValueError):
        year = None
    frame = fields.get("frame_no_raw") or (candidate.frame_raw if candidate else None)
    title = fields.get("title") or " ".join(str(p) for p in (year, make, model) if p) or (candidate.title if candidate else "") or "Vehicle"
    amount, cur = evidence.get("amount"), evidence.get("currency")
    purchase_amount = quantize(parse_amount(amount), cur) if amount is not None and cur else None
    return Vehicle(title=title, make=make, model=model, model_year=year, color=fields.get("color") or specs.get("color"),
                   frame_no_raw=frame, frame_no_norm=normalize_frame(frame) if frame else None,
                   allocation="reserved", buyer_contact_id=r.contact_id, logistics_state="purchased",
                   recon_state="needs_inspection", commercial_state="reserved", documents_state="pending",
                   acquired_at=ensure_aware(_dt(evidence.get("purchased_at") or evidence.get("at"))),
                   purchase_amount=purchase_amount, purchase_currency=(cur.upper() if cur and purchase_amount is not None else None),
                   origin_candidate_id=candidate.id if candidate else None, auction_url=candidate.source_url if candidate else None,
                   purchase_evidence={}, intake_status="incomplete" if not frame else "complete",
                   missing_identity_fields=[] if frame else ["frame_no"], situation="Purchased for import request",
                   created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)


async def _bridge_purchase_milestone(ctx: CommandContext, vehicle: Vehicle, evidence: dict) -> dict:
    from .shipping import bridge_vehicle_milestone
    return await bridge_vehicle_milestone(ctx, vehicle_id=vehicle.id, kind="purchased", status="completed",
                                          at=ensure_aware(_dt(evidence.get("purchased_at") or evidence.get("at"))),
                                          source_kind=evidence.get("source_kind") or "document", source_ref=evidence.get("source_ref"),
                                          note="purchase recorded from import request")


class RequestCloseIn(BaseModel):
    request_id: str
    reason: str = Field(min_length=1)
    outcome: str = "closed"             # closed | delivered
    expected_version: int | None = None


@command("import_requests.close", input=RequestCloseIn, perm="requests.write", action_class="internal",
         description="Close (or mark delivered) an import request with a reason; history and exclusions are kept.")
async def request_close(ctx: CommandContext, inp: RequestCloseIn) -> dict:
    r = await _req(ctx, inp.request_id, inp.expected_version)
    if inp.outcome not in ("closed", "delivered"):
        raise ValidationFailed("outcome must be closed or delivered")
    if inp.outcome == "delivered":
        if r.status != "purchased":
            raise Blocked("only a purchased request can be marked delivered", status=r.status)
        r.delivered_at = ctx.now
        _set_status(ctx, r, "delivered", inp.reason)
    else:
        if r.status == "closed":
            return {"request": serialize_request(r), "changed": False}
        r.closed_at, r.close_reason = ctx.now, inp.reason
        r.paused, r.paused_reason = False, None
        _set_status(ctx, r, "closed", inp.reason)
        # open bids cannot survive a closed request
        for b in (await ctx.db.execute(select(Bid).where(Bid.import_request_id == r.id, Bid.status.in_(("draft", "pending_approval", "approved"))))).scalars().all():
            await _invalidate_bid(ctx, b, "import request closed")
    ctx.touch(r, "import_request")
    return {"request": serialize_request(r), "changed": True}


# ── candidates ──────────────────────────────────────────────────────────────
class IngestIn(BaseModel):
    candidates: list[dict] = Field(default_factory=list)   # already-normalized (adapters/auction_source.normalize)
    source: dict | None = None                              # {"kind": "fixture", "path"|"items"} | {"kind": "http"}
    since: datetime | None = None
    match: bool = True


@command("candidates.ingest", input=IngestIn, perm="requests.write", action_class="internal",
         description="Ingest normalized auction candidates. Identity = provider+house+lot+auction date: re-ingest updates the "
                     "snapshot and never duplicates. An unconfigured live source returns unsupported.")
async def candidates_ingest(ctx: CommandContext, inp: IngestIn) -> dict:
    items = list(inp.candidates)
    fetch = None
    if inp.source is not None:
        src = auction_source.source_for(inp.source)
        fetch = await src.fetch(inp.since)
        if fetch.status != "ok":
            ctx.record(f"Auction source {src.name}: {fetch.status} ({fetch.reason.get('kind')})", entity_kind="candidate", entity_id=None,
                       kind="connection", state=fetch.status, exception=True, details=fetch.reason)
            return {"status": fetch.status, "reason": fetch.reason, "source": src.name, "created": 0, "updated": 0, "unchanged": 0,
                    "candidate_ids": []}
        items.extend(fetch.candidates)
    created, updated, unchanged, ids = 0, 0, 0, []
    for raw in items:
        if not isinstance(raw, dict):
            raise ValidationFailed("each candidate must be an object")
        # always normalize: identity fields are validated and a pre-normalized record round-trips unchanged
        n = auction_source.normalize(raw, provider=raw.get("provider") or "manual")
        auction_at = _dt(n["auction_at"])
        deadline_at = _dt(n.get("deadline_at"))
        c = (await ctx.db.execute(select(Candidate).where(
            Candidate.provider == n["provider"], Candidate.auction_house == n["auction_house"],
            Candidate.lot_no == n["lot_no"], Candidate.auction_at == auction_at).with_for_update())).scalar_one_or_none()
        snap_hash = stable_hash({"specs": n["specs"], "snapshot": n["snapshot"], "deadline_at": _iso(deadline_at),
                                 "title": n["title"], "images": n["images"], "source_url": n.get("source_url")})
        if c is None:
            c = Candidate(provider=n["provider"], auction_house=n["auction_house"], lot_no=n["lot_no"], auction_at=auction_at,
                          auction_at_source=n.get("auction_at_source"), deadline_at=deadline_at, deadline_source=n.get("deadline_source"),
                          source_url=n.get("source_url"), title=n["title"], frame_raw=n.get("frame_raw"), specs=n["specs"],
                          snapshot=n["snapshot"], images=n["images"], status="active", discovered_at=ctx.now, last_seen_at=ctx.now,
                          snapshot_hash=snap_hash, snapshot_version=1, ingest_count=1,
                          extra={"spec_sources": {k: "snapshot" for k in n["specs"]}}, created_by=ctx.actor.user_id)
            ctx.db.add(c)
            await ctx.db.flush()
            ctx.changed.append({"kind": "candidate", "id": c.id, "version": c.version})
            ctx.record(f"Candidate discovered: {c.title} ({c.auction_house} lot {c.lot_no})", entity_kind="candidate", entity_id=c.id,
                       kind="fact", state="discovered", sources=[c.source_url] if c.source_url else None)
            ctx.emit("candidate.changed", aggregate_type="candidate", aggregate_id=c.id, aggregate_version=c.version,
                     payload={"candidate_id": c.id, "change": "discovered", "lot_no": c.lot_no, "auction_house": c.auction_house})
            created += 1
        else:
            c.last_seen_at = ctx.now
            c.ingest_count = (c.ingest_count or 1) + 1
            if c.snapshot_hash == snap_hash:
                ctx.touch(c, "candidate")   # seen again: bookkeeping changed, snapshot did not
                unchanged += 1
                ids.append(c.id)
                continue
            deadline_changed = (_iso(c.deadline_at) != _iso(deadline_at))
            # translation-sourced facts survive a snapshot refresh; snapshot fields are refreshed
            sources = dict((c.extra or {}).get("spec_sources") or {})
            merged = {k: v for k, v in (c.specs or {}).items() if str(sources.get(k, "")).startswith("translation:")}
            for k, v in n["specs"].items():
                if k not in merged:
                    merged[k] = v
                    sources[k] = "snapshot"
            specs_changed = merged != (c.specs or {})
            c.specs, c.snapshot, c.images, c.title = merged, n["snapshot"], n["images"], n["title"]
            c.source_url = n.get("source_url") or c.source_url
            c.frame_raw = n.get("frame_raw") or c.frame_raw
            c.deadline_at, c.deadline_source = deadline_at, n.get("deadline_source") or c.deadline_source
            c.snapshot_hash = snap_hash
            c.snapshot_version = (c.snapshot_version or 1) + 1
            c.extra = {**(c.extra or {}), "spec_sources": sources}
            ctx.touch(c, "candidate")
            ctx.record(f"Candidate snapshot updated (v{c.snapshot_version}): {c.title}", entity_kind="candidate", entity_id=c.id, kind="fact",
                       state=c.status, details={"deadline_changed": deadline_changed, "specs_changed": specs_changed})
            ctx.emit("candidate.changed", aggregate_type="candidate", aggregate_id=c.id, aggregate_version=c.version,
                     payload={"candidate_id": c.id, "change": "snapshot", "deadline_changed": deadline_changed})
            if specs_changed or deadline_changed:
                for m in (await ctx.db.execute(select(CandidateMatch).where(CandidateMatch.candidate_id == c.id))).scalars().all():
                    if m.status in FINAL_MATCH_STATES:
                        continue
                    m.stale, m.stale_reason, m.bid_ready = True, "candidate snapshot changed", False
                    ctx.touch(m, "candidate_match")
            if deadline_changed:
                for b in (await ctx.db.execute(select(Bid).where(Bid.candidate_id == c.id, Bid.status.in_(("pending_approval", "approved"))))).scalars().all():
                    await _invalidate_bid(ctx, b, "auction deadline changed in the source snapshot")
            updated += 1
        ids.append(c.id)
    matched = None
    if inp.match and ids:
        matched = await _match_all(ctx, candidate_ids=ids, request_id=None, force=False)
    return {"status": "ok", "created": created, "updated": updated, "unchanged": unchanged, "candidate_ids": ids,
            "source": fetch.to_dict() if fetch else None, "matched": matched}


class MatchIn(BaseModel):
    candidate_id: str | None = None
    request_id: str | None = None
    force: bool = False


@command("candidates.match", input=MatchIn, perm="requests.write", action_class="internal",
         description="Evaluate candidates against every Active Search request (or one). One match per pair; mandatory fails are "
                     "rejected and remembered in the request's exclusions so they are not re-presented.")
async def candidates_match(ctx: CommandContext, inp: MatchIn) -> dict:
    return await _match_all(ctx, candidate_ids=[inp.candidate_id] if inp.candidate_id else None, request_id=inp.request_id, force=inp.force)


async def _match_all(ctx: CommandContext, *, candidate_ids: list[str] | None, request_id: str | None, force: bool) -> dict:
    cq = select(Candidate).where(Candidate.status.in_(("discovered", "active")))
    if candidate_ids:
        cq = select(Candidate).where(Candidate.id.in_(candidate_ids))
    candidates = (await ctx.db.execute(cq.order_by(Candidate.created_at))).scalars().all()
    if request_id:
        requests = [await _req(ctx, request_id)]
        if requests[0].status in ("purchased", "delivered", "closed"):
            raise Blocked(f"import request is {requests[0].status}")
    else:
        requests = (await ctx.db.execute(select(ImportRequest).where(ImportRequest.status.in_(MATCHABLE),
                                                                     ImportRequest.paused.is_(False)))).scalars().all()
    evaluated, skipped, out = 0, 0, []
    for r in requests:
        if r.paused and not request_id:
            continue
        for c in candidates:
            if c.status not in ("discovered", "active"):
                skipped += 1
                continue
            res = await _evaluate_pair(ctx, c, r, force=force)
            if res is None:
                skipped += 1
            else:
                evaluated += 1
                out.append(res)
    return {"evaluated": evaluated, "skipped": skipped, "matches": out}


def _exclusion_add(r: ImportRequest, candidate_id: str, kind: str, reason: str, now: datetime, version: int | None) -> None:
    ex = [e for e in (r.exclusions or []) if not (e.get("candidate_id") == candidate_id and e.get("kind") == kind)]
    ex.append({"candidate_id": candidate_id, "kind": kind, "reason": reason, "requirements_version": version, "at": now.isoformat()})
    r.exclusions = ex


def _exclusion_remove(r: ImportRequest, candidate_id: str, kind: str) -> bool:
    before = list(r.exclusions or [])
    after = [e for e in before if not (e.get("candidate_id") == candidate_id and e.get("kind") == kind)]
    if len(after) != len(before):
        r.exclusions = after
        r.extra = {**(r.extra or {}), "exclusion_history": list((r.extra or {}).get("exclusion_history") or []) +
                   [e for e in before if e not in after]}
        return True
    return False


async def _evaluate_pair(ctx: CommandContext, c: Candidate, r: ImportRequest, *, force: bool) -> dict | None:
    m = (await ctx.db.execute(select(CandidateMatch).where(CandidateMatch.candidate_id == c.id,
                                                            CandidateMatch.import_request_id == r.id).with_for_update())).scalar_one_or_none()
    if m is not None and m.status in FINAL_MATCH_STATES:
        return None   # buyer passed / lot decided: never re-presented
    if any(e.get("candidate_id") == c.id and e.get("kind") in ("passed", "lost", "unavailable") for e in (r.exclusions or [])):
        return None
    t = await current_translation(ctx.db, c.id)
    rev = t.revision_no if t and t.status == "complete" else None
    if (m is not None and not force and not m.stale and m.requirements_version == r.requirements_version
            and m.translation_revision_no == rev):
        return None
    outcomes = reqs.evaluate(c.specs, r.requirements or [], spec_sources=(c.extra or {}).get("spec_sources"))
    s = reqs.summarize(outcomes)
    created = m is None
    if created:
        m = CandidateMatch(candidate_id=c.id, import_request_id=r.id, status="discovered", created_by=ctx.actor.user_id)
        ctx.db.add(m)
    history = list((m.extra or {}).get("history") or [])
    if not created:
        history.append({"at": _iso(m.evaluated_at), "requirements_version": m.requirements_version, "status": m.status,
                        "mandatory_fail": m.mandatory_fail, "mandatory_unknown": m.mandatory_unknown, "score": m.score})
    m.outcomes, m.score = outcomes, s["score"]
    m.mandatory_fail, m.mandatory_unknown, m.bid_ready = s["mandatory_fail"], s["mandatory_unknown"], s["bid_ready"]
    m.requirements_version, m.translation_revision_no = r.requirements_version, rev
    m.translation_id = t.id if t else m.translation_id
    m.evaluated_at, m.stale, m.stale_reason = ctx.now, False, None
    m.extra = {**(m.extra or {}), "preference_score": s["preference_score"], "needs_confirmation": s["needs_confirmation"],
               "failed_must": s["failed_must"], "history": history[-20:]}
    exclusions_changed = False
    if s["mandatory_fail"]:
        m.status = "rejected"
        m.rejected_reason = "Fails must-have: " + ", ".join(s["failed_must"])
        already = any(e.get("candidate_id") == c.id and e.get("kind") == "mandatory_fail" and e.get("reason") == m.rejected_reason
                      for e in (r.exclusions or []))
        if not already:
            _exclusion_add(r, c.id, "mandatory_fail", m.rejected_reason, ctx.now, r.requirements_version)
            exclusions_changed = True
    else:
        m.rejected_reason = None
        if _exclusion_remove(r, c.id, "mandatory_fail"):
            exclusions_changed = True
            ctx.record("Candidate no longer excluded after requirements revision", entity_kind="import_request", entity_id=r.id,
                       kind="fact", state=r.status, details={"candidate_id": c.id})
        if t is not None and t.status == "complete":
            m.status = "reevaluated" if not created else "translation_complete"
        elif t is not None and t.status in ("incomplete", "detected"):
            m.status = "translation_incomplete" if t.status == "incomplete" else "translation_requested"
        elif t is not None and t.status in ("requested", "manual_task", "pending_approval"):
            m.status = "translation_requested"
        elif m.status in ("discovered", "rejected", "evaluated") or created:
            m.status = "evaluated"
    if created:
        await ctx.db.flush()
        ctx.changed.append({"kind": "candidate_match", "id": m.id, "version": m.version})
    else:
        ctx.touch(m, "candidate_match")
    ctx.record(f"Candidate {'rejected' if s['mandatory_fail'] else 'evaluated'} for request {r.title or r.id[:8]}: {c.title}",
               entity_kind="candidate_match", entity_id=m.id, kind="fact", state=m.status,
               details={"candidate_id": c.id, "import_request_id": r.id, "mandatory_fail": s["mandatory_fail"],
                        "mandatory_unknown": s["mandatory_unknown"], "score": s["score"]})
    ctx.emit("candidate.changed", aggregate_type="candidate_match", aggregate_id=m.id, aggregate_version=m.version,
             payload={"candidate_id": c.id, "import_request_id": r.id, "change": "matched", "status": m.status,
                      "bid_ready": m.bid_ready, "mandatory_fail": m.mandatory_fail, "mandatory_unknown": m.mandatory_unknown})
    if exclusions_changed:
        ctx.touch(r, "import_request")
    return serialize_match(m, include_draft=False)


class MatchRefIn(BaseModel):
    match_id: str
    reason: str | None = None
    note: str | None = None
    expected_version: int | None = None


@command("candidates.pass", input=MatchRefIn, perm="requests.write", action_class="internal",
         description="Buyer/owner passes on a candidate for this request; remembered so it is never re-presented.")
async def candidates_pass(ctx: CommandContext, inp: MatchRefIn) -> dict:
    m = await _match(ctx, inp.match_id, inp.expected_version)
    if m.status in ("won", "lost"):
        raise Blocked(f"match is {m.status}")
    r = await _req(ctx, m.import_request_id)
    m.status, m.bid_ready = "passed", False
    m.rejected_reason = inp.reason or "passed"
    ctx.touch(m, "candidate_match")
    _exclusion_add(r, m.candidate_id, "passed", inp.reason or "buyer passed", ctx.now, r.requirements_version)
    ctx.touch(r, "import_request")
    for b in (await ctx.db.execute(select(Bid).where(Bid.candidate_id == m.candidate_id, Bid.import_request_id == r.id,
                                                       Bid.status.in_(("draft", "pending_approval", "approved"))))).scalars().all():
        await _invalidate_bid(ctx, b, "buyer passed on the candidate")
    ctx.record(f"Passed on candidate: {inp.reason or ''}".strip(), entity_kind="candidate_match", entity_id=m.id, kind="task", state="passed")
    return {"match": serialize_match(m)}


@command("candidates.record_interest", input=MatchRefIn, perm="requests.write", action_class="internal",
         description="Record buyer interest in a candidate. Interest is not bid authorization (spec §8.2 step 10).")
async def candidates_record_interest(ctx: CommandContext, inp: MatchRefIn) -> dict:
    m = await _match(ctx, inp.match_id, inp.expected_version)
    m.extra = {**(m.extra or {}), "interest": {"at": ctx.now.isoformat(), "by": ctx.actor.user_id, "note": inp.note,
                                               "bid_authorized": False}}
    if m.status in ("buyer_review", "reevaluated", "evaluated", "translation_complete"):
        m.status = "bid_decision"
    ctx.touch(m, "candidate_match")
    ctx.record("Buyer interest recorded (not a bid authorization)", entity_kind="candidate_match", entity_id=m.id, kind="message", state=m.status)
    return {"match": serialize_match(m)}


# ── buyer-facing candidate messages (per-request scoped drafts) ─────────────
def buyer_message_body(c: Candidate, r: ImportRequest, m: CandidateMatch, t: Translation | None) -> str:
    """Only this request's facts. No other buyer, budget or term appears (spec §8.2 step 8)."""
    lines = [f"Candidate for your import request{(': ' + r.title) if r.title else ''}",
             f"{c.title} — {c.auction_house} lot {c.lot_no}",
             f"Auction: {fmt_local(c.auction_at, TOKYO)} / {fmt_local(c.auction_at, PHOENIX)}",
             f"Bid deadline: {fmt_local(c.deadline_at, TOKYO)} / {fmt_local(c.deadline_at, PHOENIX)}" if c.deadline_at else "Bid deadline: Not recorded",
             "", "Your requirements:"]
    for o in m.outcomes or []:
        ev = o.get("evidence") or {}
        observed = ev.get("observed")
        detail = f" (observed {observed})" if observed is not None else (f" ({ev.get('reason')})" if ev.get("reason") else "")
        lines.append(f"- {o.get('text') or o['key']} [{o['tier']}]: {o['result']}{detail}")
    if t is not None and t.status == "complete":
        lines.append("")
        lines.append(f"Translation: complete (revision {t.revision_no}).")
        for e in (t.excerpts or [])[:6]:
            if e.get("en"):
                lines.append(f"- {e.get('section', 'note')}: {e['en']}")
    else:
        lines.append("")
        lines.append(f"Translation: {t.status.replace('_', ' ') if t else 'not requested yet'}.")
    if m.mandatory_unknown:
        lines.append("Some must-have items are still unknown; we will confirm them before any bid.")
    lines.append("")
    lines.append("Replying with interest does not place a bid; every bid is prepared and approved separately.")
    return "\n".join(lines)


@command("candidates.prepare_buyer_message", input=MatchRefIn, perm="requests.write", action_class="internal",
         description="Prepare one scoped buyer draft for this request/candidate. Stored inline on the match until the inbox "
                     "domain exists (contract: inbox drafts.create(conversation_id, body, sources)).")
async def prepare_buyer_message(ctx: CommandContext, inp: MatchRefIn) -> dict:
    m = await _match(ctx, inp.match_id, inp.expected_version)
    if m.mandatory_fail or m.status in ("rejected", "passed", "lost"):
        raise Blocked("candidate is not suitable for this request", status=m.status, reason=m.rejected_reason)
    if m.stale:
        raise Blocked("match needs re-evaluation before a buyer message", reason=m.stale_reason)
    c = await _cand(ctx, m.candidate_id)
    r = await _req(ctx, m.import_request_id, lock=False)
    t = await current_translation(ctx.db, c.id)
    prior = (m.extra or {}).get("buyer_draft") or {}
    draft = {"id": new_id(), "version": int(prior.get("version") or 0) + 1, "body": buyer_message_body(c, r, m, t),
             "created_at": ctx.now.isoformat(), "created_by": ctx.actor.user_id, "sent_at": None, "invalidated": False,
             "invalidated_reason": None, "supersedes_id": prior.get("id"),
             "sources": [f"candidate:{c.id}:snapshot_v{c.snapshot_version}"] + ([f"translation:{t.id}:r{t.revision_no}"] if t else []),
             "scope": {"import_request_id": r.id, "candidate_id": c.id, "contact_id": r.contact_id},
             "translation_revision_no": t.revision_no if t else None, "requirements_version": r.requirements_version}
    history = list((m.extra or {}).get("draft_history") or [])
    if prior:
        history.append(prior)
    m.extra = {**(m.extra or {}), "buyer_draft": draft, "draft_history": history[-10:]}
    m.buyer_draft_id = draft["id"]
    if m.status in ("evaluated", "reevaluated", "translation_complete"):
        m.status = "buyer_review"
    ctx.touch(m, "candidate_match")
    ctx.record(f"Buyer draft prepared (v{draft['version']}) for request {r.title or r.id[:8]}", entity_kind="candidate_match", entity_id=m.id,
               kind="message", state="draft", sources=draft["sources"])
    return {"match": serialize_match(m), "draft": draft}


class BuyerSentIn(BaseModel):
    match_id: str
    message_ref: str
    sent_at: datetime | None = None
    expected_version: int | None = None


@command("candidates.mark_buyer_message_sent", input=BuyerSentIn, perm="requests.write", action_class="internal",
         description="Record that the buyer draft was sent through an authorized channel (evidence: message_ref).")
async def mark_buyer_message_sent(ctx: CommandContext, inp: BuyerSentIn) -> dict:
    m = await _match(ctx, inp.match_id, inp.expected_version)
    draft = dict((m.extra or {}).get("buyer_draft") or {})
    if not draft:
        raise Blocked("no buyer draft to mark as sent")
    if draft.get("invalidated"):
        raise Blocked("draft was invalidated; prepare a new one", reason=draft.get("invalidated_reason"))
    draft["sent_at"] = _iso(inp.sent_at) or ctx.now.isoformat()
    draft["message_ref"] = inp.message_ref
    m.extra = {**(m.extra or {}), "buyer_draft": draft}
    m.buyer_message_sent_at = ensure_aware(inp.sent_at) or ctx.now
    ctx.touch(m, "candidate_match")
    ctx.record("Buyer message sent", entity_kind="candidate_match", entity_id=m.id, kind="message", state="sent",
               receipt={"message_ref": inp.message_ref}, sources=[inp.message_ref])
    return {"match": serialize_match(m)}


# ── translations ────────────────────────────────────────────────────────────
def translation_request_content(c: Candidate) -> str:
    return "\n".join([
        f"Translation request — {c.auction_house} lot {c.lot_no}",
        f"Auction: {fmt_local(c.auction_at, TOKYO)} ({fmt_local(c.auction_at, PHOENIX)})",
        f"Vehicle: {c.title}" + (f" · frame {c.frame_raw}" if c.frame_raw else ""),
        f"Listing: {c.source_url}" if c.source_url else "Listing: not recorded",
        "Please translate the auction sheet into the shared document with these sections: "
        + ", ".join(translation_docs.DEFAULT_REQUIRED_SECTIONS) + ".",
        "Mark the document with 'Translation complete' when finished.",
    ])


class TranslationRequestIn(BaseModel):
    candidate_id: str
    exporter_contact_id: str | None = None
    note: str | None = None


def _translation_summary(p: TranslationRequestIn) -> str:
    return f"Request auction-sheet translation for candidate {p.candidate_id[:8]}"


@command("translations.request", input=TranslationRequestIn, perm="requests.write", action_class="consequential",
         approval_kind="translation_request", records=lambda p: [("candidate", p.candidate_id)],
         summary=_translation_summary, limits=lambda p: {"records": [p.candidate_id]},
         consequence=lambda p: {"scope": "exporter translation request", "targets": {"channel": "teams_or_manual"}, "moves_money": False},
         description="Ask the exporter to translate the auction sheet. Exact approval initially; one request per candidate per "
                     "revision even when several requests match. Persists a Teams intent when a route is configured, otherwise a "
                     "manual task with the exact content (never claims sent).")
async def translations_request(ctx: CommandContext, inp: TranslationRequestIn) -> dict:
    c = await _cand(ctx, inp.candidate_id, lock=True)
    existing = await current_translation(ctx.db, c.id)
    if existing is not None and existing.status in ("pending_approval", "requested", "manual_task", "detected", "incomplete", "complete"):
        await _link_matches_to_translation(ctx, c.id, existing)
        return {"translation": serialize_translation(existing), "created": False, "deduplicated": True}
    rev = (existing.revision_no + 1) if existing is not None else 0
    content = translation_request_content(c) + (f"\nNote: {inp.note}" if inp.note else "")
    t = Translation(candidate_id=c.id, status="requested", requested_at=ctx.now, request_content=content,
                    required_sections=list(translation_docs.DEFAULT_REQUIRED_SECTIONS), revision_no=rev,
                    approval_id=ctx.approval.id if ctx.approval else None, doc_provider="google_docs",
                    extra={"exporter_contact_id": inp.exporter_contact_id}, created_by=ctx.actor.user_id)
    ctx.db.add(t)
    await ctx.db.flush()
    ctx.changed.append({"kind": "translation", "id": t.id, "version": t.version})
    route = await teams_route(ctx.db)
    if route:
        act = await approvals_svc.intend_external_action(
            ctx, command_name="translations.request", provider="teams", entity_kind="translation", entity_id=t.id,
            dedupe_key=f"translation_request:{c.id}:r{rev}",
            payload={"translation_id": t.id, "candidate_id": c.id, "route": route, "content": content})
        t.request_channel, t.request_action_id = "teams", act.id
    else:
        task_id = await _translation_manual_task(ctx, t, c, content, reason="No Teams send route is configured.")
        t.request_channel, t.manual_task_id, t.status = "manual", task_id, "manual_task"
    await _link_matches_to_translation(ctx, c.id, t)
    ctx.record(f"Translation requested ({t.request_channel}): {c.auction_house} lot {c.lot_no}", entity_kind="translation", entity_id=t.id,
               kind="automation" if route else "task", state=t.status)
    ctx.emit("translation.requested", aggregate_type="translation", aggregate_id=t.id, aggregate_version=t.version,
             payload={"translation_id": t.id, "candidate_id": c.id, "channel": t.request_channel, "revision_no": rev})
    return {"translation": serialize_translation(t), "created": True, "deduplicated": False}


async def _translation_manual_task(ctx: CommandContext, t: Translation, c: Candidate, content: str, *, reason: str) -> str:
    rev = f" (revision {t.revision_no})" if t.revision_no else ""
    res = await dispatch(ctx.child(), "tasks.create", {
        "title": f"Send translation request: {c.auction_house} lot {c.lot_no}{rev}", "type": "operational", "priority": "high",
        "instructions": content, "notes": f"{reason} Send this exact content to the exporter and attach a note with the confirmation. "
                                          "Nothing has been sent.",
        "evidence_required": [{"kind": "note", "label": "Confirmation the request was sent", "min": 1}],
        "source_kind": "sourcing", "source_id": t.id, "dedupe": True}, commit=False)
    return res.data["task"]["id"]


@approvals_svc.executor("translations.request")
async def _exec_translation_request(db, act: ExternalAction) -> dict:
    """Without a Teams send adapter the exact content becomes a manual sending task on the translation; the receipt
    says sent=false and never claims delivery. A real adapter returns its own receipt (sent, not completed)."""
    if TEAMS_SENDER is not None:
        return await TEAMS_SENDER(act.payload.get("route") or {}, act.payload.get("content") or "", act)
    from ..domain.actors import SYSTEM_ACTOR
    p = dict(act.payload or {})
    t = await db.get(Translation, p.get("translation_id")) if p.get("translation_id") else None
    if t is None:
        raise NotFound("translation no longer exists")
    c = await db.get(Candidate, t.candidate_id)
    ctx = CommandContext(db=db, actor=SYSTEM_ACTOR, channel="worker", correlation_id=act.correlation_id)
    task_id = await _translation_manual_task(ctx, t, c, p.get("content") or t.request_content, reason="The Teams send adapter is not connected.")
    t.request_channel, t.manual_task_id = "manual", task_id
    if t.status == "requested":
        t.status = "manual_task"
    t.bump(None)
    return {"provider": "teams", "sent": False, "state": "manual_send_required", "task_id": task_id,
            "note": "Teams send adapter not connected; a manual sending task holds the exact content. This receipt does not claim delivery."}


async def _link_matches_to_translation(ctx: CommandContext, candidate_id: str, t: Translation) -> int:
    n = 0
    for m in (await ctx.db.execute(select(CandidateMatch).where(CandidateMatch.candidate_id == candidate_id))).scalars().all():
        if m.status in FINAL_MATCH_STATES or m.status == "rejected":
            continue
        changed = False
        if m.translation_id != t.id:
            m.translation_id, changed = t.id, True
        if t.status in ("requested", "manual_task", "pending_approval") and m.status in ("discovered", "evaluated"):
            m.status, changed = "translation_requested", True
        if changed:
            ctx.touch(m, "candidate_match")
            n += 1
    return n


class TranslationDetectedIn(BaseModel):
    translation_id: str
    doc_ref: str
    doc_revision: str
    text: str | None = None
    doc: dict | None = None                # adapter spec: {"kind": "fixture", "docs": {...}} | {"kind": "google_docs"}
    modified_at: datetime | None = None
    required_sections: list[str] | None = None
    expected_version: int | None = None


@command("translations.record_detected", input=TranslationDetectedIn, perm="requests.write", action_class="internal",
         description="A document revision was seen. Completeness comes from required sections in the content; a stable "
                     "timestamp alone is never completion. Identity (lot) must match the candidate.")
async def translations_record_detected(ctx: CommandContext, inp: TranslationDetectedIn) -> dict:
    t = await _translation(ctx, inp.translation_id, inp.expected_version)
    if t.status in ("invalidated",):
        raise Blocked("translation is invalidated")
    c = await _cand(ctx, t.candidate_id)
    text = inp.text
    if text is None:
        if inp.doc is None:
            raise ValidationFailed("text or doc source required")
        doc = await translation_docs.docs_for(inp.doc).get_document(inp.doc_ref)
        if doc.status != "ok":
            t.last_checked_at = ctx.now
            ctx.touch(t, "translation")
            ctx.record(f"Translation doc check {doc.status}: {doc.reason.get('detail')}", entity_kind="translation", entity_id=t.id,
                       kind="connection", state=doc.status, exception=True, details=doc.reason)
            return {"translation": serialize_translation(t), "status": doc.status, "reason": doc.reason}
        text = doc.text
        inp.doc_revision = doc.revision or inp.doc_revision
    t.last_checked_at = ctx.now
    if t.doc_ref == inp.doc_ref and t.doc_revision == inp.doc_revision and t.status in ("detected", "incomplete", "complete"):
        # same revision seen again (or only the timestamp moved): nothing new, definitely not completion
        t.doc_modified_at = inp.modified_at or t.doc_modified_at
        ctx.touch(t, "translation")
        return {"translation": serialize_translation(t), "changed": False, "reason": "same document revision"}
    if t.status == "complete":
        raise Blocked("translation is complete; use translations.revise for a corrected document", revision_no=t.revision_no)
    sections = list(inp.required_sections or t.required_sections or translation_docs.DEFAULT_REQUIRED_SECTIONS)
    identity = translation_docs.identity_matches(text, c.lot_no, c.auction_house)
    completeness = translation_docs.check_completeness(text, sections)
    t.doc_ref, t.doc_revision, t.doc_modified_at = inp.doc_ref, inp.doc_revision, inp.modified_at
    t.required_sections, t.identity_check = sections, identity
    t.completeness = {**completeness, "identity_ok": identity["ok"], "doc_revision": inp.doc_revision}
    t.extra = {**(t.extra or {}), "doc_text": text, "doc_text_hash": stable_hash(text)}
    if not identity["ok"]:
        t.status = "incomplete"
        reason = f"document does not name lot {c.lot_no}" + ("" if identity["house_found"] else f" / {c.auction_house}")
    elif completeness["ok"]:
        t.status = "detected"
        reason = "all required sections present; ready to mark complete"
    else:
        t.status = "incomplete"
        reason = "missing: " + ", ".join(completeness["missing"] + [f"{s} (empty)" for s in completeness["empty"]])
    ctx.touch(t, "translation")
    for m in (await ctx.db.execute(select(CandidateMatch).where(CandidateMatch.translation_id == t.id))).scalars().all():
        if m.status in FINAL_MATCH_STATES or m.status == "rejected":
            continue
        m.status = "translation_incomplete" if t.status == "incomplete" else "translation_requested"
        ctx.touch(m, "candidate_match")
    ctx.record(f"Translation doc revision {inp.doc_revision} {t.status}: {reason}", entity_kind="translation", entity_id=t.id, kind="fact",
               state=t.status, sources=[f"{t.doc_provider}:{inp.doc_ref}#{inp.doc_revision}"], exception=(t.status == "incomplete"),
               details={"completeness": completeness, "identity": identity})
    ctx.emit("translation.detected", aggregate_type="translation", aggregate_id=t.id, aggregate_version=t.version,
             payload={"translation_id": t.id, "candidate_id": c.id, "status": t.status, "doc_revision": inp.doc_revision})
    return {"translation": serialize_translation(t), "changed": True, "reason": reason}


class TranslationCompleteIn(BaseModel):
    translation_id: str
    excerpts: list[dict] = Field(default_factory=list)   # [{section, ja, en}]
    findings: dict = Field(default_factory=dict)         # structured facts extracted (e.g. {"ac": true, "grade": "4"})
    expected_version: int | None = None


@command("translations.mark_complete", input=TranslationCompleteIn, perm="requests.write", action_class="internal",
         description="Mark a detected, complete, identity-matched translation complete; stores ja/en excerpts with the "
                     "document revision, merges structured findings into the candidate and re-evaluates matches.")
async def translations_mark_complete(ctx: CommandContext, inp: TranslationCompleteIn) -> dict:
    t = await _translation(ctx, inp.translation_id, inp.expected_version)
    if t.status == "complete":
        return {"translation": serialize_translation(t), "changed": False}
    comp = dict(t.completeness or {})
    if t.status != "detected" or not comp.get("ok") or not (t.identity_check or {}).get("ok"):
        raise Blocked("translation is not complete", status=t.status, missing=comp.get("missing"), empty=comp.get("empty"),
                      identity=t.identity_check)
    for e in inp.excerpts:
        if not isinstance(e, dict) or not (e.get("ja") or e.get("en")):
            raise ValidationFailed("each excerpt needs ja and/or en text")
    c = await _cand(ctx, t.candidate_id, lock=True)
    t.excerpts = [{"section": e.get("section"), "ja": e.get("ja"), "en": e.get("en"), "doc_revision": t.doc_revision,
                   "revision_no": t.revision_no} for e in inp.excerpts]
    t.findings = dict(inp.findings)
    t.status, t.completed_at = "complete", ctx.now
    ctx.touch(t, "translation")
    if inp.findings:
        sources = dict((c.extra or {}).get("spec_sources") or {})
        specs = dict(c.specs or {})
        for k, v in inp.findings.items():
            specs[k] = v
            sources[k] = f"translation:{t.id}:r{t.revision_no}"
        c.specs = specs
        c.extra = {**(c.extra or {}), "spec_sources": sources}
        ctx.touch(c, "candidate")
    for m in (await ctx.db.execute(select(CandidateMatch).where(CandidateMatch.translation_id == t.id))).scalars().all():
        if m.status in FINAL_MATCH_STATES or m.status == "rejected":
            continue
        m.status, m.stale, m.stale_reason = "translation_complete", True, "translation completed; re-evaluate"
        ctx.touch(m, "candidate_match")
    ctx.record(f"Translation complete (revision {t.revision_no}, doc {t.doc_revision})", entity_kind="translation", entity_id=t.id,
               kind="fact", state="complete", sources=[f"{t.doc_provider}:{t.doc_ref}#{t.doc_revision}"])
    ctx.emit("translation.completed", aggregate_type="translation", aggregate_id=t.id, aggregate_version=t.version,
             payload={"translation_id": t.id, "candidate_id": c.id, "revision_no": t.revision_no})
    matched = await _match_all(ctx, candidate_ids=[c.id], request_id=None, force=False)
    return {"translation": serialize_translation(t), "changed": True, "reevaluated": matched}


class TranslationReviseIn(BaseModel):
    translation_id: str
    doc_revision: str
    text: str
    reason: str = Field(min_length=1)
    expected_version: int | None = None


@command("translations.revise", input=TranslationReviseIn, perm="requests.write", action_class="internal",
         description="A corrected document arrived: keep the prior excerpts/revision, start a new revision, invalidate unsent buyer "
                     "drafts and pending bid approvals, and open a correction case for messages already sent.")
async def translations_revise(ctx: CommandContext, inp: TranslationReviseIn) -> dict:
    t = await _translation(ctx, inp.translation_id, inp.expected_version)
    if t.status == "invalidated":
        raise Blocked("translation is invalidated")
    c = await _cand(ctx, t.candidate_id, lock=True)
    t.revision_history = list(t.revision_history or []) + [{
        "revision_no": t.revision_no, "doc_revision": t.doc_revision, "status": t.status, "excerpts": list(t.excerpts or []),
        "findings": dict(t.findings or {}), "completeness": dict(t.completeness or {}), "completed_at": _iso(t.completed_at),
        "superseded_at": ctx.now.isoformat(), "reason": inp.reason}]
    prev_rev = t.revision_no
    t.revision_no = (t.revision_no or 0) + 1
    t.excerpts, t.findings, t.completed_at = [], {}, None
    # facts sourced from the superseded translation are no longer trusted until re-confirmed
    sources = dict((c.extra or {}).get("spec_sources") or {})
    specs = dict(c.specs or {})
    dropped = [k for k, s in sources.items() if str(s).startswith(f"translation:{t.id}:")]
    for k in dropped:
        specs.pop(k, None)
        sources.pop(k, None)
    if dropped:
        c.specs, c.extra = specs, {**(c.extra or {}), "spec_sources": sources}
        ctx.touch(c, "candidate")
    identity = translation_docs.identity_matches(inp.text, c.lot_no, c.auction_house)
    completeness = translation_docs.check_completeness(inp.text, t.required_sections or translation_docs.DEFAULT_REQUIRED_SECTIONS)
    t.doc_revision, t.identity_check = inp.doc_revision, identity
    t.completeness = {**completeness, "identity_ok": identity["ok"], "doc_revision": inp.doc_revision}
    t.extra = {**(t.extra or {}), "doc_text": inp.text, "doc_text_hash": stable_hash(inp.text)}
    t.status = "detected" if (identity["ok"] and completeness["ok"]) else "incomplete"
    t.last_checked_at = ctx.now
    ctx.touch(t, "translation")
    # dependent work
    drafts_invalidated, correction_cases, bids_invalidated, bids_flagged = 0, [], 0, 0
    for m in (await ctx.db.execute(select(CandidateMatch).where(CandidateMatch.candidate_id == c.id))).scalars().all():
        if m.status in FINAL_MATCH_STATES:
            continue
        ex = dict(m.extra or {})
        draft = dict(ex.get("buyer_draft") or {})
        changed = False
        if draft and not draft.get("sent_at") and not draft.get("invalidated"):
            draft["invalidated"], draft["invalidated_reason"] = True, f"translation revised (r{t.revision_no}): {inp.reason}"
            ex["buyer_draft"] = draft
            drafts_invalidated += 1
            changed = True
        if m.buyer_message_sent_at and draft.get("translation_revision_no") == prev_rev and not ex.get("correction_case_id"):
            case = await dispatch(ctx.child(), "cases.open", {
                "title": f"Translation corrected after buyer message: {c.auction_house} lot {c.lot_no}", "kind": "reply",
                "import_request_id": m.import_request_id, "summary": f"Revision {t.revision_no} changed facts the sent message relied on: {inp.reason}",
                "next_action": "Prepare and review a correction draft for the buyer", "next_check_at": ctx.now}, commit=False)
            ex["correction_case_id"] = case.data["case"]["id"]
            correction_cases.append(case.data["case"]["id"])
            changed = True
        if m.status != "rejected":
            m.status, m.stale, m.stale_reason, m.bid_ready = "translation_incomplete" if t.status == "incomplete" else "translation_requested", True, f"translation revised to r{t.revision_no}", False
            changed = True
        if changed:
            m.extra = ex
            ctx.touch(m, "candidate_match")
    for b in (await ctx.db.execute(select(Bid).where(Bid.candidate_id == c.id, Bid.status.in_(ACTIVE_BID_STATUSES)))).scalars().all():
        if b.status == "submitted":
            b.extra = {**(b.extra or {}), "revalidate_required": True, "reason": f"translation revised to r{t.revision_no}"}
            ctx.touch(b, "bid")
            await dispatch(ctx.child(), "tasks.create", {
                "title": f"Stop and revalidate submitted bid: {c.auction_house} lot {c.lot_no}", "type": "operational", "priority": "urgent",
                "import_request_id": b.import_request_id, "instructions": f"Translation revision {t.revision_no}: {inp.reason}",
                "source_kind": "sourcing", "source_id": b.id, "dedupe": True}, commit=False)
            bids_flagged += 1
        else:
            await _invalidate_bid(ctx, b, f"translation revised to r{t.revision_no}: {inp.reason}")
            bids_invalidated += 1
    ctx.record(f"Translation revised to r{t.revision_no} ({t.status}): {inp.reason}", entity_kind="translation", entity_id=t.id, kind="fact",
               state=t.status, exception=True, sources=[f"{t.doc_provider}:{t.doc_ref}#{inp.doc_revision}"],
               details={"drafts_invalidated": drafts_invalidated, "correction_cases": correction_cases, "bids_invalidated": bids_invalidated,
                        "bids_flagged": bids_flagged, "dropped_facts": dropped})
    ctx.emit("translation.revised", aggregate_type="translation", aggregate_id=t.id, aggregate_version=t.version,
             payload={"translation_id": t.id, "candidate_id": c.id, "revision_no": t.revision_no, "previous_revision_no": prev_rev,
                      "status": t.status, "reason": inp.reason})
    return {"translation": serialize_translation(t), "drafts_invalidated": drafts_invalidated, "correction_cases": correction_cases,
            "bids_invalidated": bids_invalidated, "bids_flagged": bids_flagged}


# ── bids ─────────────────────────────────────────────────────────────────────
async def _invalidate_bid(ctx: CommandContext, b: Bid, reason: str) -> None:
    await approvals_svc.invalidate_for_entity(ctx, "bid", b.id, reason)
    b.status, b.invalidated_reason = "invalidated", reason
    ctx.touch(b, "bid")
    ctx.record(f"Bid invalidated: {reason}", entity_kind="bid", entity_id=b.id, kind="approval", state="invalidated", exception=True)
    ctx.emit("bid.changed", aggregate_type="bid", aggregate_id=b.id, aggregate_version=b.version,
             payload={"bid_id": b.id, "change": "invalidated", "reason": reason, "candidate_id": b.candidate_id})


def build_packet(c: Candidate, r: ImportRequest, m: CandidateMatch | None, t: Translation | None, *, max_amount: Decimal, currency: str,
                 fee_basis: str, fx_estimate: dict | None, disclosures: list[str], note: str | None) -> dict:
    fx: dict
    if fx_estimate and fx_estimate.get("rate") and fx_estimate.get("source") and fx_estimate.get("date"):
        rate = parse_amount(fx_estimate["rate"])
        fx = {"rate": str(rate), "source": fx_estimate["source"], "date": str(fx_estimate["date"]),
              "usd": str(convert(max_amount, rate, "USD")), "status": "estimated"}
    else:
        fx = {"status": "not_provided", "note": "no exchange estimate recorded (rate, source and date required)"}
    return {
        "auction": {"provider": c.provider, "auction_house": c.auction_house, "lot_no": c.lot_no,
                    "auction_at": dual_time(c.auction_at, c.auction_at_source), "source_url": c.source_url, "title": c.title,
                    "frame_raw": c.frame_raw, "snapshot_version": c.snapshot_version},
        "deadline": dual_time(c.deadline_at, c.deadline_source),
        "max": {"amount": str(max_amount), "currency": currency},
        "fee_basis": fee_basis, "fx_estimate": fx,
        "buyer_requirements": {"import_request_id": r.id, "version": r.requirements_version, "items": list(r.requirements or []),
                               "outcomes": list(m.outcomes or []) if m else [], "mandatory_unknown": bool(m.mandatory_unknown) if m else None,
                               "mandatory_fail": bool(m.mandatory_fail) if m else None},
        "translation": {"id": t.id if t else None, "status": t.status if t else "not requested", "revision_no": t.revision_no if t else None,
                        "doc_revision": t.doc_revision if t else None, "excerpts": list(t.excerpts or []) if t else []},
        "agreement": {"id": r.agreement_id, "status": r.agreement_status, "evidence": dict(r.agreement_evidence or {})},
        "deposit": {"status": r.deposit_status},
        "disclosures": list(disclosures), "note": note,
    }


class BidPrepareIn(BaseModel):
    candidate_id: str
    import_request_id: str
    max_amount: Decimal
    currency: str = "JPY"
    fee_basis: str = ""
    fx_estimate: dict | None = None      # {rate, source, date}
    disclosures: list[str] = Field(default_factory=list)
    note: str | None = None
    bid_id: str | None = None
    expected_version: int | None = None


@command("bids.prepare", input=BidPrepareIn, perm="requests.write", action_class="internal",
         description="Prepare the exact reviewable bid packet (auction identity, max JPY, fee basis, FX estimate with source/date, "
                     "deadline in JST and Phoenix, buyer requirements, disclosures). Re-preparing a pending bid invalidates its approval.")
async def bids_prepare(ctx: CommandContext, inp: BidPrepareIn) -> dict:
    if inp.max_amount <= 0:
        raise ValidationFailed("max_amount must be positive")
    cur = inp.currency.upper()
    max_amount = quantize(inp.max_amount, cur)
    c = await _cand(ctx, inp.candidate_id)
    r = await _req(ctx, inp.import_request_id, lock=False)
    m = (await ctx.db.execute(select(CandidateMatch).where(CandidateMatch.candidate_id == c.id,
                                                            CandidateMatch.import_request_id == r.id))).scalar_one_or_none()
    t = await current_translation(ctx.db, c.id)
    if c.deadline_at is None:
        raise Blocked("auction deadline not recorded for this candidate; a bid packet needs a sourced deadline")
    b: Bid | None = None
    if inp.bid_id:
        b = await _bid(ctx, inp.bid_id, inp.expected_version)
    else:
        b = (await ctx.db.execute(select(Bid).where(Bid.candidate_id == c.id, Bid.import_request_id == r.id,
                                                    Bid.status.in_(("draft", "pending_approval"))).with_for_update())).scalars().first()
    if b is not None and b.status not in ("draft", "pending_approval"):
        raise Blocked(f"bid is {b.status}; record its result or cancel it before preparing another", bid_id=b.id)
    packet = build_packet(c, r, m, t, max_amount=max_amount, currency=cur, fee_basis=inp.fee_basis, fx_estimate=inp.fx_estimate,
                          disclosures=inp.disclosures, note=inp.note)
    packet_hash = stable_hash(packet)
    created = b is None
    if created:
        b = Bid(candidate_id=c.id, import_request_id=r.id, status="draft", created_by=ctx.actor.user_id)
        ctx.db.add(b)
    elif b.status == "pending_approval" and b.packet_hash != packet_hash:
        await approvals_svc.invalidate_for_entity(ctx, "bid", b.id, "bid packet changed — review again")
        b.status, b.approval_id = "draft", None
    b.max_amount, b.currency, b.fee_basis = max_amount, cur, inp.fee_basis
    b.fx_estimate, b.packet, b.packet_hash = packet["fx_estimate"], packet, packet_hash
    b.deadline_at, b.auction_house, b.lot_no, b.auction_at = c.deadline_at, c.auction_house, c.lot_no, c.auction_at
    b.translation_revision_no = t.revision_no if t else None
    b.requirements_version = r.requirements_version
    if created:
        await ctx.db.flush()
        ctx.changed.append({"kind": "bid", "id": b.id, "version": b.version})
    else:
        ctx.touch(b, "bid")
    if m is not None and m.bid_id != b.id:
        m.bid_id = b.id
        ctx.touch(m, "candidate_match")
    ctx.record(f"Bid packet prepared: {c.auction_house} lot {c.lot_no}, max {max_amount} {cur}", entity_kind="bid", entity_id=b.id,
               kind="task", state=b.status, visibility="owner", details={"packet_hash": packet_hash})
    ctx.emit("bid.changed", aggregate_type="bid", aggregate_id=b.id, aggregate_version=b.version,
             payload={"bid_id": b.id, "change": "prepared", "candidate_id": c.id, "import_request_id": r.id})
    gate = await bid_gate(ctx, b)
    return {"bid": serialize_bid(b), "created": created, "gate": {"decision": "Allowed" if not gate else "Blocked", "reasons": gate}}


async def bid_gate(ctx: CommandContext, b: Bid) -> list[str]:
    """Reasons a bid cannot go for approval / be executed right now (spec §8.2 steps 2, 9, 10; invariant 8)."""
    reasons: list[str] = []
    c = await ctx.db.get(Candidate, b.candidate_id)
    r = await ctx.db.get(ImportRequest, b.import_request_id) if b.import_request_id else None
    if r is None:
        reasons.append("bid is not linked to an import request")
    else:
        gate = gate_decision(r)
        if r.status != "active_search":
            reasons.append(f"import request is {r.status}: " + "; ".join(gate["reasons"] or ["not in Active Search"]))
        elif gate["decision"] != "Allowed":
            # evidence behind Active Search changed after entry (deposit rule revised, must-haves removed ...)
            reasons.append("Active Search evidence no longer holds: " + "; ".join(gate["reasons"]))
        if r.paused:
            reasons.append("import request is paused")
        m = (await ctx.db.execute(select(CandidateMatch).where(CandidateMatch.candidate_id == b.candidate_id,
                                                                CandidateMatch.import_request_id == r.id))).scalar_one_or_none()
        if m is None:
            reasons.append("candidate is not matched to this request")
        else:
            if m.stale:
                reasons.append(f"match needs re-evaluation ({m.stale_reason})")
            if m.mandatory_fail:
                reasons.append("fails must-have: " + ", ".join((m.extra or {}).get("failed_must") or []))
            if m.mandatory_unknown:
                reasons.append("unknown must-have needs confirmation: " + ", ".join((m.extra or {}).get("needs_confirmation") or []))
            if m.status in ("passed", "lost"):
                reasons.append(f"candidate {m.status} for this request")
            if m.requirements_version != r.requirements_version:
                reasons.append("match evaluated against an older requirements version")
    t = await current_translation(ctx.db, b.candidate_id)
    if t is None:
        reasons.append("translation not requested")
    elif t.status != "complete":
        reasons.append(f"translation {t.status.replace('_', ' ')}")
    elif b.translation_revision_no != t.revision_no:
        reasons.append(f"packet built on translation r{b.translation_revision_no}, current is r{t.revision_no}")
    if b.deadline_at is None:
        reasons.append("deadline not recorded")
    elif ensure_aware(b.deadline_at) <= ctx.now:
        reasons.append(f"auction deadline passed ({fmt_local(b.deadline_at, TOKYO)} / {fmt_local(b.deadline_at, PHOENIX)})")
    if c is not None:
        if (c.lot_no, c.auction_house, _iso(c.auction_at)) != (b.lot_no, b.auction_house, _iso(b.auction_at)):
            reasons.append("auction identity changed since the packet was prepared")
        if _iso(c.deadline_at) != _iso(b.deadline_at):
            reasons.append("auction deadline changed since the packet was prepared")
        if c.status not in ("discovered", "active"):
            reasons.append(f"candidate is {c.status}")
    if not b.packet_hash:
        reasons.append("packet not prepared")
    competing = (await ctx.db.execute(select(Bid).where(
        Bid.id != b.id, Bid.auction_house == b.auction_house, Bid.lot_no == b.lot_no, Bid.auction_at == b.auction_at,
        Bid.status.in_(ACTIVE_BID_STATUSES), Bid.import_request_id != b.import_request_id))).scalars().all()
    if competing:
        reasons.append("another linked request already has an active bid on this lot (no duplicate competing bids, invariant 8)")
    return reasons


class BidRefIn(BaseModel):
    bid_id: str
    expected_version: int | None = None
    note: str | None = None


@command("bids.submit_for_approval", input=BidRefIn, perm="requests.write", action_class="internal",
         records=lambda p: [("bid", p.bid_id)],
         description="Check every bid gate, then raise the exact packet for the owner's approval (bids.submit). Blocked when a must-have "
                     "is unknown/failed, the translation is incomplete, the deadline passed or another linked request holds an active bid.")
async def bids_submit_for_approval(ctx: CommandContext, inp: BidRefIn) -> dict:
    b = await _bid(ctx, inp.bid_id, inp.expected_version)
    if b.status == "pending_approval" and b.approval_id:
        return {"bid": serialize_bid(b), "status": "needs_review", "approval_id": b.approval_id, "idempotent": True}
    if b.status != "draft":
        raise Blocked(f"bid is {b.status}", bid_status=b.status)
    reasons = await bid_gate(ctx, b)
    if reasons:
        ctx.record("Bid blocked: " + "; ".join(reasons), entity_kind="bid", entity_id=b.id, kind="approval", state="blocked", exception=True)
        raise Blocked("bid cannot be submitted for approval", reasons=reasons, decision="Blocked")
    res = await dispatch(ctx.child(), "bids.submit", {"bid_id": b.id, "packet_hash": b.packet_hash}, commit=False)
    if res.status == "needs_review":
        b.status, b.approval_id = "pending_approval", res.approval_id
        ctx.touch(b, "bid")
        ctx.record("Bid packet raised for owner approval", entity_kind="bid", entity_id=b.id, kind="approval", state="pending",
                   details={"approval_id": res.approval_id})
        ctx.emit("bid.changed", aggregate_type="bid", aggregate_id=b.id, aggregate_version=b.version,
                 payload={"bid_id": b.id, "change": "pending_approval", "approval_id": res.approval_id})
    return {"bid": serialize_bid(b), "status": res.status, "approval_id": res.approval_id, "decision": res.decision}


class BidSubmitIn(BaseModel):
    bid_id: str
    packet_hash: str


def _bid_summary(p: BidSubmitIn) -> str:
    return f"Bid {p.bid_id[:8]} (packet {p.packet_hash[:10]})"


async def bid_revalidate(ctx: CommandContext, inp: BidSubmitIn, approval) -> list[str]:
    b = (await ctx.db.execute(select(Bid).where(Bid.id == inp.bid_id))).scalar_one_or_none()
    if b is None:
        return ["bid no longer exists"]
    reasons = []
    if b.status != "pending_approval":
        reasons.append(f"bid is {b.status}")
    if b.packet_hash != inp.packet_hash:
        reasons.append("bid packet changed (max amount / lot / deadline / translation) — review again")
    reasons.extend(await bid_gate(ctx, b))
    return list(dict.fromkeys(reasons))


@command("bids.submit", input=BidSubmitIn, perm="requests.write", action_class="consequential", approval_kind="bid",
         records=lambda p: [("bid", p.bid_id)], summary=_bid_summary, expires_hours=24, revalidate=bid_revalidate,
         consequence=lambda p: {"scope": "auction bid", "moves_money": True, "targets": {"channel": "exporter"}},
         description="Execute an owner-approved bid packet: a Teams intent when the exporter route is configured, otherwise a manual "
                     "placement task with the exact packet. Approved never means submitted; bids.record_submitted records placement.")
async def bids_submit(ctx: CommandContext, inp: BidSubmitIn) -> dict:
    if ctx.approval is None or ctx.approval.command_name != "bids.submit":
        # spec §11.2: a bid is the owner's individual exact approval — a standing permission or wildcard grant never authorizes it
        raise Denied("a bid executes only under the owner's exact approval of this packet", command="bids.submit",
                     decision={"outcome": "blocked", "reasons": ["bids are not eligible for standing permissions"]})
    b = await _bid(ctx, inp.bid_id)
    if b.packet_hash != inp.packet_hash:
        raise Blocked("bid packet changed — review again", current_hash=b.packet_hash)
    reasons = await bid_gate(ctx, b)
    if reasons:
        raise Blocked("bid gates failed at execution time", reasons=reasons)
    c = await _cand(ctx, b.candidate_id)
    b.status = "approved"
    b.approval_id = ctx.approval.id
    route = await teams_route(ctx.db)
    packet_text = _packet_text(b)
    if route:
        act = await approvals_svc.intend_external_action(
            ctx, command_name="bids.submit", provider="teams", entity_kind="bid", entity_id=b.id,
            dedupe_key=f"bid_submit:{b.id}:{b.packet_hash}",
            payload={"bid_id": b.id, "route": route, "content": packet_text, "packet_hash": b.packet_hash,
                     "title": f"Place approved bid: {c.auction_house} lot {c.lot_no} (max {_dec(b.max_amount)} {b.currency})",
                     "import_request_id": b.import_request_id, "deadline_at": _iso(b.deadline_at)})
        b.submission_channel, b.submission_action_id = "teams", act.id
    else:
        task_id = await _bid_manual_task(ctx, b, c, packet_text, reason="No exporter send route is configured.")
        b.submission_channel, b.submission_task_id = "manual_task", task_id
    ctx.touch(b, "bid")
    ctx.record(f"Bid approved ({b.submission_channel}): {c.auction_house} lot {c.lot_no}, max {_dec(b.max_amount)} {b.currency}", entity_kind="bid",
               entity_id=b.id, kind="approval", state="approved", visibility="owner")
    ctx.emit("bid.changed", aggregate_type="bid", aggregate_id=b.id, aggregate_version=b.version,
             payload={"bid_id": b.id, "change": "approved", "channel": b.submission_channel, "candidate_id": c.id})
    return {"bid": serialize_bid(b), "submitted": False, "channel": b.submission_channel}


def _packet_text(b: Bid) -> str:
    p = b.packet or {}
    a, d = p.get("auction", {}), p.get("deadline", {})
    return "\n".join([
        f"BID PACKET — {a.get('auction_house')} lot {a.get('lot_no')} ({a.get('title')})",
        f"Auction: {a.get('auction_at', {}).get('tokyo')} / {a.get('auction_at', {}).get('phoenix')}",
        f"Deadline: {d.get('tokyo')} / {d.get('phoenix')} (source: {d.get('source') or 'not recorded'})",
        f"Maximum: {p.get('max', {}).get('amount')} {p.get('max', {}).get('currency')}",
        f"Fee basis: {p.get('fee_basis') or 'not recorded'}",
        f"FX estimate: {p.get('fx_estimate')}",
        f"Translation: {p.get('translation', {}).get('status')} r{p.get('translation', {}).get('revision_no')}",
        "Disclosures: " + ("; ".join(p.get("disclosures") or []) or "none recorded"),
        f"Packet hash: {b.packet_hash}",
    ])


async def _bid_manual_task(ctx: CommandContext, b: Bid, c: Candidate, packet_text: str, *, reason: str) -> str:
    """The exact packet becomes a manual placement task; approved never means placed (bids.record_submitted records it)."""
    res = await dispatch(ctx.child(), "tasks.create", {
        "title": f"Place approved bid: {c.auction_house} lot {c.lot_no} (max {_dec(b.max_amount)} {b.currency})", "type": "operational",
        "priority": "urgent", "import_request_id": b.import_request_id, "instructions": packet_text,
        "due_at": _iso(b.deadline_at), "timezone": TOKYO,
        "notes": f"{reason} Place this exact bid with the exporter and record placement with bids.record_submitted (evidence required). "
                 "Nothing has been sent.",
        "evidence_required": [{"kind": "note", "label": "Exporter confirmation of the placed bid", "min": 1}],
        "source_kind": "sourcing", "source_id": b.id, "dedupe": True}, commit=False)
    return res.data["task"]["id"]


@approvals_svc.executor("bids.submit")
async def _exec_bid_submit(db, act: ExternalAction) -> dict:
    """Without a Teams send adapter the approved packet becomes a manual placement task on the bid; the receipt
    says sent=false and never claims delivery. A real adapter returns its own receipt."""
    if TEAMS_SENDER is not None:
        return await TEAMS_SENDER(act.payload.get("route") or {}, act.payload.get("content") or "", act)
    from ..domain.actors import SYSTEM_ACTOR
    p = dict(act.payload or {})
    b = await db.get(Bid, p.get("bid_id")) if p.get("bid_id") else None
    if b is None:
        raise NotFound("bid no longer exists")
    c = await db.get(Candidate, b.candidate_id)
    ctx = CommandContext(db=db, actor=SYSTEM_ACTOR, channel="worker", correlation_id=act.correlation_id)
    task_id = await _bid_manual_task(ctx, b, c, p.get("content") or _packet_text(b), reason="The Teams send adapter is not connected.")
    b.submission_channel, b.submission_task_id = "manual_task", task_id
    b.bump(None)
    return {"provider": "teams", "sent": False, "state": "manual_send_required", "task_id": task_id,
            "note": "Teams send adapter not connected; a manual placement task holds the exact packet. This receipt does not claim the bid was placed."}


class BidSubmittedIn(BaseModel):
    bid_id: str
    evidence: dict                      # {source_ref (required), submitted_at, note}
    expected_version: int | None = None


@command("bids.record_submitted", input=BidSubmittedIn, perm="requests.write", action_class="owner_only", approval_kind="bid",
         summary=lambda p: f"Record bid {p.bid_id[:8]} as placed",
         description="Owner records that an approved bid was actually placed with the exporter (evidence required).")
async def bids_record_submitted(ctx: CommandContext, inp: BidSubmittedIn) -> dict:
    b = await _bid(ctx, inp.bid_id, inp.expected_version)
    if not inp.evidence.get("source_ref"):
        raise Blocked("placement evidence needs a source_ref")
    if b.status == "submitted":
        return {"bid": serialize_bid(b), "changed": False}
    if b.status != "approved":
        raise Blocked(f"bid is {b.status}; only an approved bid can be recorded as placed", bid_status=b.status)
    b.status = "submitted"
    b.submitted_at = ensure_aware(_dt(inp.evidence.get("submitted_at"))) or ctx.now
    b.submission_evidence = {**inp.evidence, "recorded_by": ctx.actor.user_id, "recorded_at": ctx.now.isoformat()}
    ctx.touch(b, "bid")
    ctx.record("Bid placed with exporter (evidence recorded)", entity_kind="bid", entity_id=b.id, kind="fact", state="submitted",
               sources=[inp.evidence["source_ref"]], visibility="owner")
    ctx.emit("bid.changed", aggregate_type="bid", aggregate_id=b.id, aggregate_version=b.version, payload={"bid_id": b.id, "change": "submitted"})
    return {"bid": serialize_bid(b), "changed": True}


@command("bids.cancel", input=BidRefIn, perm="requests.write", action_class="internal", records=lambda p: [("bid", p.bid_id)],
         description="Cancel a draft/pending/approved (not yet placed) bid; its approval is invalidated.")
async def bids_cancel(ctx: CommandContext, inp: BidRefIn) -> dict:
    b = await _bid(ctx, inp.bid_id, inp.expected_version)
    if b.status not in ("draft", "pending_approval", "approved"):
        raise Blocked(f"bid is {b.status}", bid_status=b.status)
    await approvals_svc.invalidate_for_entity(ctx, "bid", b.id, inp.note or "bid cancelled")
    b.status, b.invalidated_reason = "cancelled", inp.note or "cancelled"
    ctx.touch(b, "bid")
    ctx.record(f"Bid cancelled: {inp.note or ''}".strip(), entity_kind="bid", entity_id=b.id, kind="task", state="cancelled")
    ctx.emit("bid.changed", aggregate_type="bid", aggregate_id=b.id, aggregate_version=b.version, payload={"bid_id": b.id, "change": "cancelled"})
    return {"bid": serialize_bid(b)}


class BidResultIn(BaseModel):
    bid_id: str
    result: str                         # won | lost
    evidence: dict                      # {source_ref (required), amount, currency, at, note}
    vehicle: dict | None = None         # optional identity details for the created vehicle on a win
    expected_version: int | None = None


@command("bids.record_result", input=BidResultIn, perm="requests.write", action_class="owner_only", approval_kind="bid",
         summary=lambda p: f"Record bid {p.bid_id[:8]} result: {p.result}",
         description="Owner records the external win/loss with evidence. Won links/creates exactly one purchased vehicle "
                     "(origin_candidate_id); lost keeps the request's exclusions. Replaying the same result is idempotent.")
async def bids_record_result(ctx: CommandContext, inp: BidResultIn) -> dict:
    if inp.result not in ("won", "lost"):
        raise ValidationFailed("result must be won or lost")
    if not (inp.evidence or {}).get("source_ref"):
        raise Blocked("result evidence needs a source_ref (exporter message / auction result)")
    b = await _bid(ctx, inp.bid_id, inp.expected_version)
    if b.status in ("won", "lost"):
        if b.status == inp.result and (b.result_evidence or {}).get("source_ref") == inp.evidence["source_ref"]:
            return {"bid": serialize_bid(b), "changed": False, "idempotent": True, "vehicle_id": (await _cand(ctx, b.candidate_id)).vehicle_id}
        raise Conflict(f"bid result already recorded as {b.status}", recorded=b.result_evidence)
    if b.status not in ("approved", "submitted"):
        raise Blocked(f"bid is {b.status}; a result can only be recorded for an approved or placed bid", bid_status=b.status)
    c = await _cand(ctx, b.candidate_id, lock=True)
    r = await _req(ctx, b.import_request_id)
    m = (await ctx.db.execute(select(CandidateMatch).where(CandidateMatch.candidate_id == c.id,
                                                            CandidateMatch.import_request_id == r.id).with_for_update())).scalar_one_or_none()
    # result_at is when the auction result happened per the evidence; unknown stays unknown (recorded_at is in the evidence)
    b.status, b.result_at = inp.result, ensure_aware(_dt(inp.evidence.get("at")))
    b.result = {"result": inp.result, "amount": str(inp.evidence["amount"]) if inp.evidence.get("amount") is not None else None,
                "currency": inp.evidence.get("currency")}
    b.result_evidence = {**inp.evidence, "recorded_by": ctx.actor.user_id, "recorded_at": ctx.now.isoformat()}
    ctx.touch(b, "bid")
    vehicle_id = None
    if inp.result == "won":
        vehicle = await ctx.db.get(Vehicle, c.vehicle_id) if c.vehicle_id else None
        if vehicle is None and r.purchased_vehicle_id:
            raise Conflict("import request already has a different purchased vehicle", purchased_vehicle_id=r.purchased_vehicle_id)
        if vehicle is None:
            vehicle = await _create_vehicle(ctx, inp.vehicle or {}, r, candidate=c, evidence=inp.evidence,
                                            create_key=f"bid_won:{b.id}")
            c.vehicle_id = vehicle.id
        vehicle_id = vehicle.id
        if r.purchased_vehicle_id and r.purchased_vehicle_id != vehicle.id:
            raise Conflict("import request already has a different purchased vehicle", purchased_vehicle_id=r.purchased_vehicle_id)
        evidence = {**inp.evidence, "bid_id": b.id, "manual": False, "recorded_by": ctx.actor.user_id, "recorded_at": ctx.now.isoformat()}
        r.purchased_vehicle_id, r.purchase_evidence = vehicle.id, evidence
        r.purchased_at = ensure_aware(_dt(inp.evidence.get("at") or inp.evidence.get("purchased_at")))  # unknown stays unknown
        vehicle.purchase_evidence = {**evidence, "import_request_id": r.id, "candidate_id": c.id}
        ctx.touch(vehicle, "vehicle")
        c.status, c.result = "won", {"result": "won", "bid_id": b.id, "import_request_id": r.id, "at": ctx.now.isoformat()}
        ctx.touch(c, "candidate")
        if m is not None:
            m.status = "won"
            ctx.touch(m, "candidate_match")
        # the lot is gone for every other linked request
        for om in (await ctx.db.execute(select(CandidateMatch).where(CandidateMatch.candidate_id == c.id,
                                                                     CandidateMatch.import_request_id != r.id))).scalars().all():
            if om.status in FINAL_MATCH_STATES:
                continue
            om.status, om.bid_ready, om.rejected_reason = "lost", False, "lot won for another request"
            ctx.touch(om, "candidate_match")
            orr = await _req(ctx, om.import_request_id)
            _exclusion_add(orr, c.id, "unavailable", "lot won for another request", ctx.now, orr.requirements_version)
            ctx.touch(orr, "import_request")
        _set_status(ctx, r, "purchased", "auction won")
        ctx.touch(r, "import_request")
        ctx.record(f"Auction won: {c.auction_house} lot {c.lot_no} → vehicle {vehicle.title}", entity_kind="bid", entity_id=b.id, kind="fact",
                   state="won", sources=[inp.evidence["source_ref"]], details={"vehicle_id": vehicle.id, "import_request_id": r.id})
        _emit_request(ctx, r, "purchased", vehicle_id=vehicle.id, bid_id=b.id)
        ctx.emit("vehicle.state_changed", aggregate_type="vehicle", aggregate_id=vehicle.id, aggregate_version=vehicle.version,
                 payload={"vehicle_id": vehicle.id, "change": "purchased", "origin_candidate_id": c.id, "bid_id": b.id,
                          "import_request_id": r.id, "source_ref": inp.evidence["source_ref"]})
        await _bridge_purchase_milestone(ctx, vehicle, inp.evidence)
    else:
        if m is not None:
            m.status, m.bid_ready, m.rejected_reason = "lost", False, "lost at auction"
            ctx.touch(m, "candidate_match")
        _exclusion_add(r, c.id, "lost", "lost at auction", ctx.now, r.requirements_version)
        ctx.touch(r, "import_request")
        others = (await ctx.db.execute(select(Bid).where(Bid.candidate_id == c.id, Bid.id != b.id, Bid.status.in_(ACTIVE_BID_STATUSES)))).scalars().all()
        if not others:
            c.status, c.result = "lost", {"result": "lost", "bid_id": b.id, "at": ctx.now.isoformat()}
            ctx.touch(c, "candidate")
        ctx.record(f"Auction lost: {c.auction_house} lot {c.lot_no}; sourcing continues with exclusions kept", entity_kind="bid", entity_id=b.id,
                   kind="fact", state="lost", sources=[inp.evidence["source_ref"]])
        _emit_request(ctx, r, "bid_lost", bid_id=b.id, candidate_id=c.id)
    ctx.emit("bid.changed", aggregate_type="bid", aggregate_id=b.id, aggregate_version=b.version,
             payload={"bid_id": b.id, "change": inp.result, "candidate_id": c.id, "vehicle_id": vehicle_id})
    return {"bid": serialize_bid(b), "changed": True, "vehicle_id": vehicle_id, "request_status": r.status}
