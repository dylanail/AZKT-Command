"""Knowledge, corpus, retrieval, procedures/Teach, promotion proposals and evaluation (spec §9, §2.3 Teach / S17 / S18).

Reads apply the same ACL as retrieval (owner: all; others: role-safe, never owner/finance visibility, mechanics only
their assigned vehicles). Every write dispatches a command; GETs have no side effects.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import command_context, require
from ..core.errors import NotFound
from ..db import get_db
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..models.knowledge import KnowledgeItem, ProcedureVersion
from ..services import evaluation as eval_svc
from ..services import knowledge as ksvc
from ..services import procedures as psvc
from ..services import retrieval

router = APIRouter(tags=["knowledge"])


def _ok(res) -> dict:
    return res.to_dict()


# ── /api/knowledge : items ──────────────────────────────────────────────────
@router.get("/api/knowledge")
async def list_items(status: str | None = None, kind: str | None = None, contact_id: str | None = None, vehicle_id: str | None = None,
                     workflow: str | None = None, q: str | None = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                     actor: Actor = Depends(require("agents.chat")), db: AsyncSession = Depends(get_db)):
    if status and status not in ksvc.STATUSES:
        raise HTTPException(422, f"status must be one of {ksvc.STATUSES}")
    if kind and kind not in ksvc.KINDS:
        raise HTTPException(422, f"kind must be one of {ksvc.KINDS}")
    clauses = list(ksvc.visible_item_clauses(actor))
    if status:
        clauses.append(KnowledgeItem.status == status)
    if kind:
        clauses.append(KnowledgeItem.kind == kind)
    if contact_id:
        clauses.append(KnowledgeItem.scope["contact_id"].as_string() == contact_id)
    if vehicle_id:
        clauses.append(KnowledgeItem.scope["vehicle_id"].as_string() == vehicle_id)
    if workflow:
        clauses.append(KnowledgeItem.scope["workflow"].as_string() == workflow)
    if q:
        pat = f"%{q.strip()}%"
        clauses.append(or_(KnowledgeItem.title.ilike(pat), KnowledgeItem.content.ilike(pat)))
    total = (await db.execute(select(func.count()).select_from(KnowledgeItem).where(*clauses))).scalar_one()
    rows = (await db.execute(select(KnowledgeItem).where(*clauses).order_by(KnowledgeItem.updated_at.desc()).limit(limit).offset(offset))).scalars().all()
    return {"items": [ksvc.serialize_item(k) for k in rows], "total": int(total), "kinds": list(ksvc.KINDS), "statuses": list(ksvc.STATUSES)}


@router.get("/api/knowledge/search")
async def search(q: str = Query(..., min_length=1), contact_id: str | None = None, vehicle_id: str | None = None,
                 kinds: str | None = None, limit: int = Query(8, ge=1, le=50),
                 actor: Actor = Depends(require("agents.chat")), db: AsyncSession = Depends(get_db)):
    """ACL-safe retrieval envelope: current facts, approved knowledge, historical examples (labelled), sources."""
    res = await retrieval.retrieve(db, actor, q, contact_id=contact_id, vehicle_id=vehicle_id,
                                   kinds=[k for k in (kinds or "").split(",") if k] or None, limit=limit)
    return res.to_dict()


@router.get("/api/knowledge/sources")
async def sources(q: str = Query(..., min_length=1), source_kinds: str | None = None, limit: int = Query(20, ge=1, le=100),
                  actor: Actor = Depends(require("agents.chat")), db: AsyncSession = Depends(get_db)):
    """The UI 'sources.search' tool with the same ACL as retrieval."""
    return await retrieval.search_sources(db, actor, q, limit=limit, source_kinds=[k for k in (source_kinds or "").split(",") if k] or None)


# ── /api/knowledge/corpus ───────────────────────────────────────────────────
@router.get("/api/knowledge/corpus")
async def corpus_coverage(actor: Actor = Depends(require("knowledge.write")), db: AsyncSession = Depends(get_db)):
    return await ksvc.coverage(db)


@router.get("/api/knowledge/corpus/manifests")
async def corpus_manifests(actor: Actor = Depends(require("knowledge.write")), db: AsyncSession = Depends(get_db)):
    items = await ksvc.list_manifests(db)
    return {"items": items, "total": len(items)}


@router.post("/api/knowledge/corpus/manifests")
async def corpus_create_manifest(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    return _ok(await dispatch(ctx, "corpus.create_manifest", payload))


@router.post("/api/knowledge/corpus/index")
async def corpus_index(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    return _ok(await dispatch(ctx, "corpus.index_text", payload))


@router.post("/api/knowledge/corpus/reindex")
async def corpus_reindex(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    return _ok(await dispatch(ctx, "corpus.reindex_source", payload))


@router.post("/api/knowledge/corpus/tombstone")
async def corpus_tombstone(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    return _ok(await dispatch(ctx, "corpus.tombstone", payload))


# ── /api/knowledge/evaluation ───────────────────────────────────────────────
@router.get("/api/knowledge/evaluation")
async def evaluation_sets(set_name: str | None = None, actor: Actor = Depends(require("knowledge.write")), db: AsyncSession = Depends(get_db)):
    return await eval_svc.eval_summary(db, set_name)


@router.post("/api/knowledge/evaluation/cases")
async def evaluation_add_cases(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    return _ok(await dispatch(ctx, "eval.add_cases", payload))


@router.post("/api/knowledge/evaluation/results")
async def evaluation_record_results(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    return _ok(await dispatch(ctx, "eval.record_results", payload))


@router.get("/api/knowledge/workflows/{workflow_key}")
async def workflow_status(workflow_key: str, actor: Actor = Depends(require("knowledge.write")), db: AsyncSession = Depends(get_db)):
    return await eval_svc.workflow_status(db, workflow_key)


@router.post("/api/knowledge/learn")
async def learn(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    """Classify a draft edit into scoped lessons and record the outcome (G11)."""
    return _ok(await dispatch(ctx, "learning.record_edit", payload))


@router.post("/api/knowledge/outcomes")
async def record_outcome(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    return _ok(await dispatch(ctx, "learning.record_outcome", payload))


# item detail + lifecycle (declared after the fixed sub-paths)
@router.get("/api/knowledge/{item_id}")
async def get_item(item_id: str, actor: Actor = Depends(require("agents.chat")), db: AsyncSession = Depends(get_db)):
    k = (await db.execute(select(KnowledgeItem).where(KnowledgeItem.id == item_id, *ksvc.visible_item_clauses(actor)))).scalar_one_or_none()
    if k is None:
        raise NotFound("knowledge item not found")
    return ksvc.serialize_item(k)


@router.post("/api/knowledge")
async def propose_item(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    return _ok(await dispatch(ctx, "knowledge.propose", payload))


@router.post("/api/knowledge/{item_id}/approve")
async def approve_item(item_id: str, payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context)):
    return _ok(await dispatch(ctx, "knowledge.approve", {**payload, "item_id": item_id}))


@router.post("/api/knowledge/{item_id}/retire")
async def retire_item(item_id: str, payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context)):
    return _ok(await dispatch(ctx, "knowledge.retire", {**payload, "item_id": item_id}))


@router.post("/api/knowledge/{item_id}/reject")
async def reject_item(item_id: str, payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context)):
    return _ok(await dispatch(ctx, "knowledge.reject", {**payload, "item_id": item_id}))


# ── /api/procedures ─────────────────────────────────────────────────────────
@router.get("/api/procedures")
async def list_procedures(status: str | None = None, workflow_key: str | None = None, limit: int = Query(100, ge=1, le=500),
                          offset: int = Query(0, ge=0), actor: Actor = Depends(require("agents.chat")), db: AsyncSession = Depends(get_db)):
    out = await psvc.list_procedures(db, status=status, workflow_key=workflow_key, limit=limit, offset=offset)
    return {**out, "items": [psvc.redact_for(d, actor) for d in out["items"]]}


@router.post("/api/procedures")
async def propose_procedure(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    return _ok(await dispatch(ctx, "procedures.propose", payload))


@router.get("/api/procedures/{procedure_id}")
async def get_procedure(procedure_id: str, actor: Actor = Depends(require("agents.chat")), db: AsyncSession = Depends(get_db)):
    return psvc.redact_for(await psvc.get_procedure(db, procedure_id), actor)


@router.get("/api/procedures/{procedure_id}/versions")
async def procedure_versions(procedure_id: str, actor: Actor = Depends(require("agents.chat")), db: AsyncSession = Depends(get_db)):
    d = psvc.redact_for(await psvc.get_procedure(db, procedure_id), actor)
    return {"items": d["versions"], "total": len(d["versions"]), "current_version_id": d["current_version_id"]}


@router.get("/api/procedures/{procedure_id}/versions/{version_id}")
async def procedure_version(procedure_id: str, version_id: str, actor: Actor = Depends(require("agents.chat")),
                            db: AsyncSession = Depends(get_db)):
    v = await db.get(ProcedureVersion, version_id)
    if v is None or v.procedure_id != procedure_id:
        raise NotFound("procedure version not found")
    return psvc.redact_for(psvc.serialize_version(v), actor)


@router.post("/api/procedures/{procedure_id}/versions/{version_id}/tests")
async def procedure_run_tests(procedure_id: str, version_id: str, payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context)):
    return _ok(await dispatch(ctx, "procedures.run_tests", {**payload, "version_id": version_id}))


@router.post("/api/procedures/{procedure_id}/versions/{version_id}/promote")
async def procedure_promote(procedure_id: str, version_id: str, payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context)):
    return _ok(await dispatch(ctx, "procedures.promote", {**payload, "version_id": version_id}))


@router.post("/api/procedures/{procedure_id}/versions/{version_id}/rollback")
async def procedure_rollback(procedure_id: str, version_id: str, payload: dict = Body(default={}), ctx: CommandContext = Depends(command_context)):
    return _ok(await dispatch(ctx, "procedures.rollback", {**payload, "version_id": version_id}))


# ── /api/proposals ──────────────────────────────────────────────────────────
@router.get("/api/proposals")
async def list_proposals(status: str | None = None, workflow_key: str | None = None, limit: int = Query(100, ge=1, le=500),
                         offset: int = Query(0, ge=0), actor: Actor = Depends(require("permissions")), db: AsyncSession = Depends(get_db)):
    return await eval_svc.list_proposals(db, status=status, workflow_key=workflow_key, limit=limit, offset=offset)


@router.post("/api/proposals/generate")
async def generate_proposal(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    return _ok(await dispatch(ctx, "promotion_proposals.generate", payload))


@router.post("/api/proposals/regression")
async def record_regression(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    return _ok(await dispatch(ctx, "promotion_proposals.record_regression", payload))


@router.get("/api/proposals/{proposal_id}")
async def get_proposal(proposal_id: str, actor: Actor = Depends(require("permissions")), db: AsyncSession = Depends(get_db)):
    return await eval_svc.get_proposal(db, proposal_id)


@router.post("/api/proposals/{proposal_id}/decide")
async def decide_proposal(proposal_id: str, payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    return _ok(await dispatch(ctx, "promotion_proposals.decide", {**payload, "proposal_id": proposal_id}))


# ── /api/teach ──────────────────────────────────────────────────────────────
TEACH_KNOWLEDGE_KINDS = ("policy", "style", "template", "example", "lesson", "exception")


@router.post("/api/teach")
async def teach(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)):
    """Teach entry point: text/correction/demonstration -> procedure proposal; policy/style/exception -> knowledge item.
    Everything lands as a proposal for owner review; no permission changes."""
    kind = (payload.get("kind") or "procedure").lower()
    if kind in TEACH_KNOWLEDGE_KINDS:
        body = {k: v for k, v in payload.items() if k != "kind"}
        body.setdefault("source_kind", "teach")
        body["kind"] = kind
        body.setdefault("title", (payload.get("text") or payload.get("content") or "")[:80] or "Untitled")
        body.setdefault("content", payload.get("text") or payload.get("content") or "")
        body.pop("text", None)
        return _ok(await dispatch(ctx, "knowledge.propose", body))
    source_kind = {"procedure": "teach_text", "text": "teach_text", "teach_text": "teach_text", "correction": "correction",
                   "demonstration": "demonstration", "email_example": "email_example"}.get(kind)
    if source_kind is None:
        raise HTTPException(422, "kind must be one of procedure|text|correction|demonstration|email_example|" + "|".join(TEACH_KNOWLEDGE_KINDS))
    body = {k: v for k, v in payload.items() if k != "kind"}
    body["source_kind"] = source_kind
    body.setdefault("title", (payload.get("text") or "")[:80] or "Untitled procedure")
    return _ok(await dispatch(ctx, "procedures.propose", body))
