"""Knowledge items and the retrieval corpus (spec §9.1–9.2, §14.1 manifest shape).

Knowledge items are the *approved knowledge* layer: policies, style preferences, templates, examples,
general lessons and customer-specific exceptions. Everything starts `proposed`; new general business policy and
customer exceptions require explicit owner acceptance (`knowledge.approve` is owner_only). Approving knowledge
never changes a permission (spec §9.4: the learning service cannot edit permissions).

Corpus chunks are the retrieval index: ~800-character chunks with overlap, an ACL label
`{visibility: all|owner|finance, personal_allowlisted: bool}` and a trust label
(`untrusted_external` for messages, `internal` for tasks/notes/facts, `approved` for knowledge/procedures).
Full-text search uses a `tsvector` computed in SQL; an embedding is stored only when an owner-configured
`EMBEDDINGS_URL` answers (never fabricated). Tombstones exclude a source from every retrieval immediately and
quarantine its text (A06).
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update

from ..core.errors import Blocked, Conflict, NotFound, ValidationFailed
from ..core.ids import sha256_hex, stable_hash
from ..domain.commands import CommandContext, command
from ..models.knowledge import CorpusChunk, CorpusManifest, EvalCase, KnowledgeItem

log = logging.getLogger("azkt.knowledge")

KINDS = ("policy", "style", "template", "example", "lesson", "exception", "procedure_note")
STATUSES = ("proposed", "approved", "retired", "rejected")
CLASSIFICATIONS = ("style", "factual", "concession", "policy", "manual")
# Kinds whose activation always needs the owner (spec §9.4: new general business policy; buyer exceptions stay scoped
# and owner-approved). Style/example lessons are usable only as examples and never as policy.
OWNER_KINDS = {"policy", "exception"}
USABLE_AS = {"policy": ["policy"], "style": ["example"], "template": ["template"], "example": ["example"],
             "lesson": ["fact"], "exception": ["exception"], "procedure_note": ["note"]}

CHUNKER_VERSION = "v1"
CHUNK_SIZE = 800
CHUNK_OVERLAP = 120
SOURCE_KINDS = ("message", "document", "knowledge", "procedure", "vehicle_fact", "task_note", "note", "example",
                "transcript", "web_page", "attachment")
TRUST_BY_SOURCE = {"message": "untrusted_external", "document": "untrusted_external", "attachment": "untrusted_external",
                   "web_page": "untrusted_external", "transcript": "untrusted_external", "example": "untrusted_external",
                   "task_note": "internal", "note": "internal", "vehicle_fact": "internal",
                   "knowledge": "approved", "procedure": "approved"}
HISTORICAL_BY_SOURCE = {"message": True, "document": True, "attachment": True, "web_page": True, "transcript": True,
                        "example": True, "task_note": False, "note": False, "vehicle_fact": False,
                        "knowledge": False, "procedure": False}
VISIBILITIES = ("all", "owner", "finance")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ── serializers ──────────────────────────────────────────────────────────────
def serialize_item(k: KnowledgeItem) -> dict:
    return {
        "id": k.id, "version": k.version, "kind": k.kind, "title": k.title, "content": k.content, "scope": dict(k.scope or {}),
        "status": k.status, "classification": k.classification, "usable_as": list(k.usable_as or []),
        "requires_owner": bool(k.requires_owner), "evidence": list(k.evidence or []), "visibility": k.visibility,
        "effective_from": _iso(k.effective_from), "expires_at": _iso(k.expires_at), "source_kind": k.source_kind,
        "source_ref": k.source_ref, "proposed_by": k.proposed_by, "approved_by": k.approved_by, "approved_at": _iso(k.approved_at),
        "retired_at": _iso(k.retired_at), "retired_reason": k.retired_reason, "rejected_at": _iso(k.rejected_at),
        "rejected_reason": k.rejected_reason, "diff_summary": dict(k.diff_summary or {}), "tests": list(k.tests or []),
        "dedupe_key": k.dedupe_key, "created_at": _iso(k.created_at), "updated_at": _iso(k.updated_at),
        "is_general": not any((k.scope or {}).get(x) for x in ("contact_id", "vehicle_id")),
    }


def serialize_manifest(m: CorpusManifest) -> dict:
    """Corpus export manifest shape (spec §9.2 step 1/6, §14.1): sources, coverage, versions, counts, exclusions, failures."""
    return {
        "id": m.id, "version": m.version, "label": m.label, "status": m.status, "sources": list(m.sources or []),
        "coverage": {"from": _iso(m.coverage_from), "to": _iso(m.coverage_to)},
        "counts": dict(m.counts or {}), "excluded": dict(m.excluded or {}),
        "embedding": {"model": m.embedding_model, "dims": m.embedding_dims},
        "chunker_version": m.chunker_version, "parser_version": m.parser_version,
        "retrieval_settings": dict(m.retrieval_settings or {}), "failures": list(m.failures or []),
        "activated_at": _iso(m.activated_at), "superseded_by_id": m.superseded_by_id,
        "last_indexed_at": _iso(m.last_indexed_at), "created_at": _iso(m.created_at),
    }


def serialize_chunk(c: CorpusChunk, *, include_text: bool = True) -> dict:
    return {
        "id": c.id, "manifest_id": c.manifest_id, "source_kind": c.source_kind, "source_id": c.source_id,
        "chunk_index": c.chunk_index, "text": c.text if include_text else None, "trust": c.trust, "acl": dict(c.acl or {}),
        "kind": c.kind, "lang": c.lang, "contact_id": c.contact_id, "vehicle_id": c.vehicle_id,
        "happened_at": _iso(c.happened_at), "speaker": c.speaker, "source_locator": dict(c.source_locator or {}),
        "is_historical": bool(c.is_historical), "tombstoned_at": _iso(c.tombstoned_at), "tombstone_reason": c.tombstone_reason,
        "embedding_model": c.embedding_model, "embedding_dims": c.embedding_dims, "has_embedding": c.embedding is not None,
        "content_hash": c.content_hash, "indexed_at": _iso(c.indexed_at), "chunker_version": c.chunker_version,
    }


# ── knowledge items ──────────────────────────────────────────────────────────
class KnowledgeProposeIn(BaseModel):
    kind: str
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    scope: dict = Field(default_factory=dict)  # {contact_id?, vehicle_id?, workflow?}
    source_kind: str | None = None  # correction|edit_diff|teach|import|manual
    source_ref: str | None = None
    classification: str | None = None
    evidence: list = Field(default_factory=list)
    visibility: str = "all"
    effective_from: datetime | None = None
    expires_at: datetime | None = None
    tests: list = Field(default_factory=list)
    diff_summary: dict = Field(default_factory=dict)
    dedupe_key: str | None = None


def _validate_scope(scope: dict) -> dict:
    out = {}
    for k in ("contact_id", "vehicle_id", "workflow"):
        v = scope.get(k)
        if v:
            out[k] = str(v)
    unknown = set(scope) - {"contact_id", "vehicle_id", "workflow"}
    if unknown:
        raise ValidationFailed(f"scope keys must be contact_id/vehicle_id/workflow (got {sorted(unknown)})")
    return out


async def propose_item(ctx: CommandContext, inp: KnowledgeProposeIn) -> tuple[KnowledgeItem, bool]:
    """Shared by knowledge.propose and learning: creates a proposed item (retry-safe via dedupe_key)."""
    if inp.kind not in KINDS:
        raise ValidationFailed(f"kind must be one of {KINDS}")
    if inp.classification is not None and inp.classification not in CLASSIFICATIONS:
        raise ValidationFailed(f"classification must be one of {CLASSIFICATIONS}")
    if inp.visibility not in VISIBILITIES:
        raise ValidationFailed(f"visibility must be one of {VISIBILITIES}")
    scope = _validate_scope(inp.scope or {})
    if inp.kind == "exception" and not scope.get("contact_id"):
        raise ValidationFailed("a customer exception must be scoped to a contact_id")
    key = inp.dedupe_key or stable_hash({"kind": inp.kind, "title": inp.title.strip().lower(), "content": inp.content.strip(),
                                         "scope": scope})[:32]
    existing = (await ctx.db.execute(select(KnowledgeItem).where(KnowledgeItem.dedupe_key == key))).scalars().first()
    if existing is not None:
        return existing, False
    classification = inp.classification or ("policy" if inp.kind == "policy" else "concession" if inp.kind == "exception"
                                            else "style" if inp.kind in ("style", "example", "template") else "manual")
    k = KnowledgeItem(kind=inp.kind, title=inp.title.strip(), content=inp.content.strip(), scope=scope, status="proposed",
                      classification=classification, usable_as=list(USABLE_AS.get(inp.kind, [])),
                      requires_owner=True, evidence=list(inp.evidence or []), visibility=inp.visibility,
                      effective_from=inp.effective_from, expires_at=inp.expires_at, source_kind=inp.source_kind,
                      source_ref=inp.source_ref, tests=list(inp.tests or []), diff_summary=dict(inp.diff_summary or {}),
                      dedupe_key=key, proposed_by=ctx.actor.user_id, created_by=ctx.actor.user_id)
    ctx.db.add(k)
    await ctx.db.flush()
    ctx.changed.append({"kind": "knowledge_item", "id": k.id, "version": k.version})
    ctx.record(f"Proposed {k.kind}: {k.title}", entity_kind="knowledge_item", entity_id=k.id, kind="system", state="proposed",
               details={"kind": k.kind, "scope": scope, "classification": classification, "requires_owner": True})
    ctx.emit("knowledge.changed", aggregate_type="knowledge_item", aggregate_id=k.id, aggregate_version=k.version,
             payload={"status": "proposed", "kind": k.kind, "scope": scope})
    return k, True


@command("knowledge.propose", input=KnowledgeProposeIn, perm="knowledge.write", action_class="internal",
         description="Propose a policy/style/template/example/lesson/exception. Everything starts proposed; "
                     "policies and customer exceptions need the owner's explicit acceptance.")
async def knowledge_propose(ctx: CommandContext, inp: KnowledgeProposeIn) -> dict:
    k, created = await propose_item(ctx, inp)
    return {"item": serialize_item(k), "created": created, "decision": "Needs review",
            "owner_required": True,  # every proposal waits for the owner; policies/exceptions always (OWNER_KINDS)
            "note": "Approval activates knowledge only; it never changes a permission."}


class KnowledgeRef(BaseModel):
    item_id: str
    expected_version: int | None = None
    reason: str | None = None
    effective_from: datetime | None = None
    expires_at: datetime | None = None


async def _get_item(ctx: CommandContext, item_id: str, expected_version: int | None) -> KnowledgeItem:
    k = (await ctx.db.execute(select(KnowledgeItem).where(KnowledgeItem.id == item_id).with_for_update())).scalar_one_or_none()
    if k is None:
        raise NotFound("knowledge item not found")
    if expected_version is not None and k.version != expected_version:
        raise Conflict("knowledge item changed since you loaded it", current_version=k.version)
    return k


@command("knowledge.approve", input=KnowledgeRef, perm="knowledge.write", action_class="owner_only", approval_kind="other",
         summary=lambda p: f"Approve knowledge {p.item_id[:8]}",
         description="Owner activates a proposed item. Factual lessons need evidence; approval grants no permission.")
async def knowledge_approve(ctx: CommandContext, inp: KnowledgeRef) -> dict:
    k = await _get_item(ctx, inp.item_id, inp.expected_version)
    if k.status != "proposed":
        raise Blocked(f"knowledge item is {k.status}", status=k.status)
    if k.classification == "factual" and not (k.evidence or []):
        raise Blocked("a factual lesson needs evidence before it can be approved", missing="evidence")
    k.status = "approved"
    k.approved_by = ctx.actor.user_id
    k.approved_at = ctx.now
    if inp.effective_from is not None:
        k.effective_from = inp.effective_from
    if k.effective_from is None:
        k.effective_from = ctx.now
    if inp.expires_at is not None:
        k.expires_at = inp.expires_at
    ctx.touch(k, "knowledge_item")
    ctx.record(f"Approved {k.kind}: {k.title}", entity_kind="knowledge_item", entity_id=k.id, kind="system", state="approved",
               visibility="owner" if k.visibility == "owner" else "all",
               details={"kind": k.kind, "scope": dict(k.scope or {}), "permission_change": False})
    ctx.emit("knowledge.changed", aggregate_type="knowledge_item", aggregate_id=k.id, aggregate_version=k.version,
             payload={"status": "approved", "kind": k.kind, "scope": dict(k.scope or {})})
    # approved knowledge becomes retrievable through its own (approved-trust) chunks
    await _index_item(ctx, k)
    return {"item": serialize_item(k), "permission_change": False}


@command("knowledge.retire", input=KnowledgeRef, perm="knowledge.write", action_class="owner_only", approval_kind="other",
         summary=lambda p: f"Retire knowledge {p.item_id[:8]}",
         description="Retire approved knowledge; it stays auditable but leaves current-policy retrieval.")
async def knowledge_retire(ctx: CommandContext, inp: KnowledgeRef) -> dict:
    k = await _get_item(ctx, inp.item_id, inp.expected_version)
    if k.status not in ("approved", "proposed"):
        raise Blocked(f"knowledge item is {k.status}", status=k.status)
    k.status = "retired"
    k.retired_at = ctx.now
    k.retired_by = ctx.actor.user_id
    k.retired_reason = inp.reason
    ctx.touch(k, "knowledge_item")
    ctx.record(f"Retired {k.kind}: {k.title}" + (f" — {inp.reason}" if inp.reason else ""), entity_kind="knowledge_item",
               entity_id=k.id, kind="system", state="retired")
    ctx.emit("knowledge.changed", aggregate_type="knowledge_item", aggregate_id=k.id, aggregate_version=k.version,
             payload={"status": "retired", "kind": k.kind})
    await tombstone_source(ctx, "knowledge", k.id, reason="retired")
    return {"item": serialize_item(k)}


@command("knowledge.reject", input=KnowledgeRef, perm="knowledge.write", action_class="owner_only", approval_kind="other",
         summary=lambda p: f"Reject knowledge proposal {p.item_id[:8]}",
         description="Decline a proposed item with a reason; the proposal stays on record.")
async def knowledge_reject(ctx: CommandContext, inp: KnowledgeRef) -> dict:
    k = await _get_item(ctx, inp.item_id, inp.expected_version)
    if k.status != "proposed":
        raise Blocked(f"knowledge item is {k.status}", status=k.status)
    k.status = "rejected"
    k.rejected_at = ctx.now
    k.rejected_by = ctx.actor.user_id
    k.rejected_reason = inp.reason
    ctx.touch(k, "knowledge_item")
    ctx.record(f"Rejected {k.kind} proposal: {k.title}" + (f" — {inp.reason}" if inp.reason else ""),
               entity_kind="knowledge_item", entity_id=k.id, kind="system", state="rejected")
    ctx.emit("knowledge.changed", aggregate_type="knowledge_item", aggregate_id=k.id, aggregate_version=k.version,
             payload={"status": "rejected", "kind": k.kind})
    return {"item": serialize_item(k)}


async def _index_item(ctx: CommandContext, k: KnowledgeItem) -> None:
    scope = dict(k.scope or {})
    await index_text(ctx, source_kind="knowledge", source_id=k.id, text=f"{k.title}\n{k.content}",
                     kind=k.kind, contact_id=scope.get("contact_id"), vehicle_id=scope.get("vehicle_id"),
                     visibility=k.visibility, happened_at=k.approved_at or ctx.now, speaker="AZKT policy",
                     source_locator={"kind": "knowledge_item", "id": k.id, "version": k.version},
                     source_version=k.version, replace=True)


# ── corpus: chunking ─────────────────────────────────────────────────────────
_WS = re.compile(r"[ \t]+")


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """~`size` character chunks with `overlap`, cut on paragraph/sentence boundaries when possible."""
    t = (text or "").replace("\r\n", "\n").strip()
    if not t:
        return []
    if len(t) <= size:
        return [t]
    out: list[str] = []
    start = 0
    n = len(t)
    while start < n:
        end = min(start + size, n)
        if end < n:
            window = t[start:end]
            cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(". "), window.rfind("。"))
            if cut > size * 0.5:
                end = start + cut + 1
        piece = t[start:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return out


def detect_lang(text: str) -> str:
    return "ja" if re.search(r"[぀-ヿ一-鿿]", text or "") else "en"


# ── corpus: embeddings (optional, owner-configured) ──────────────────────────
async def embed_texts(texts: list[str]) -> tuple[list[list[float] | None], dict | None, str | None]:
    """Returns (vectors, {"model", "dims"} | None, error | None). Without EMBEDDINGS_URL nothing is embedded
    and the chunk keeps embedding=None (full-text search still works)."""
    url = os.environ.get("EMBEDDINGS_URL", "")
    if not url or not texts:
        return [None] * len(texts), None, None
    try:
        import httpx
        payload: dict[str, Any] = {"input": texts}
        model = os.environ.get("EMBEDDINGS_MODEL")
        if model:
            payload["model"] = model
        headers = {}
        if os.environ.get("EMBEDDINGS_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['EMBEDDINGS_TOKEN']}"
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(url, json=payload, headers=headers)
        if r.status_code != 200:
            return [None] * len(texts), None, f"embeddings service returned {r.status_code}"
        body = r.json()
        vecs = [d.get("embedding") for d in body["data"]] if isinstance(body.get("data"), list) else body.get("embeddings")
        if not isinstance(vecs, list) or len(vecs) != len(texts):
            return [None] * len(texts), None, "embeddings service returned an unexpected shape"
        dims = len(vecs[0]) if vecs and vecs[0] else None
        return vecs, {"model": body.get("model") or model or "unknown", "dims": dims}, None
    except Exception as e:  # noqa: BLE001
        return [None] * len(texts), None, f"{type(e).__name__}: {e}"


# ── corpus: manifests ────────────────────────────────────────────────────────
class ManifestCreateIn(BaseModel):
    label: str = ""
    sources: list = Field(default_factory=list)  # [{kind, connection_id?, account?, scope, allowlist?}]
    coverage_from: datetime | None = None
    coverage_to: datetime | None = None
    embedding_model: str | None = None
    embedding_dims: int | None = None
    chunker_version: str = CHUNKER_VERSION
    parser_version: str = "v1"
    retrieval_settings: dict = Field(default_factory=dict)
    excluded: dict = Field(default_factory=dict)
    activate: bool = True
    dedupe_key: str | None = None


@command("corpus.create_manifest", input=ManifestCreateIn, perm="knowledge.write", action_class="internal",
         description="Record which sources/coverage/versions the corpus is built from. No 'knowledge complete' claim without one.")
async def corpus_create_manifest(ctx: CommandContext, inp: ManifestCreateIn) -> dict:
    if inp.embedding_model and inp.embedding_dims:
        other = (await ctx.db.execute(select(CorpusManifest).where(
            CorpusManifest.status == "active", CorpusManifest.embedding_model.is_not(None),
            CorpusManifest.embedding_model != inp.embedding_model))).scalars().first()
        if other is not None and other.embedding_dims and other.embedding_dims != inp.embedding_dims:
            # spec §9.2 step 6: never mix incompatible dimensions in one active index
            raise Blocked("active manifest uses a different embedding dimension; build a staged index and swap after validation",
                          active_manifest_id=other.id)
    key = inp.dedupe_key or stable_hash({"label": inp.label, "sources": inp.sources, "from": str(inp.coverage_from),
                                         "to": str(inp.coverage_to), "chunker": inp.chunker_version})[:32]
    existing = (await ctx.db.execute(select(CorpusManifest).where(CorpusManifest.label == inp.label,
                                                                  CorpusManifest.excluded["dedupe_key"].as_string() == key))).scalars().first()
    if existing is not None:
        return {"manifest": serialize_manifest(existing), "created": False}
    m = CorpusManifest(label=inp.label, sources=list(inp.sources), coverage_from=inp.coverage_from, coverage_to=inp.coverage_to,
                       embedding_model=inp.embedding_model, embedding_dims=inp.embedding_dims, chunker_version=inp.chunker_version,
                       parser_version=inp.parser_version, retrieval_settings=inp.retrieval_settings,
                       excluded={**inp.excluded, "dedupe_key": key}, counts={}, failures=[],
                       status="active" if inp.activate else "building", activated_at=ctx.now if inp.activate else None,
                       created_by=ctx.actor.user_id)
    ctx.db.add(m)
    await ctx.db.flush()
    if inp.activate:
        await ctx.db.execute(update(CorpusManifest).where(CorpusManifest.status == "active", CorpusManifest.id != m.id)
                             .values(status="superseded", superseded_by_id=m.id).execution_options(synchronize_session=False))
    ctx.changed.append({"kind": "corpus_manifest", "id": m.id, "version": m.version})
    ctx.record(f"Corpus manifest recorded: {m.label or m.id[:8]}", entity_kind="corpus_manifest", entity_id=m.id, kind="system",
               state=m.status, details={"sources": inp.sources, "chunker_version": inp.chunker_version})
    ctx.emit("corpus.manifest_changed", aggregate_type="corpus_manifest", aggregate_id=m.id, payload={"status": m.status})
    return {"manifest": serialize_manifest(m), "created": True}


async def active_manifest(db) -> CorpusManifest | None:
    return (await db.execute(select(CorpusManifest).where(CorpusManifest.status == "active")
                             .order_by(CorpusManifest.activated_at.desc().nulls_last()))).scalars().first()


# ── corpus: indexing ─────────────────────────────────────────────────────────
class IndexTextIn(BaseModel):
    source_kind: str
    source_id: str
    text: str
    kind: str | None = None  # example|policy|note|fact|...
    contact_id: str | None = None
    vehicle_id: str | None = None
    visibility: str = "all"  # all|owner|finance
    personal_allowlisted: bool = False
    trust: str | None = None
    is_historical: bool | None = None
    happened_at: datetime | None = None
    speaker: str | None = None
    lang: str | None = None
    source_locator: dict = Field(default_factory=dict)
    source_version: int | None = None
    manifest_id: str | None = None
    replace: bool = False  # drop existing (non-tombstoned) chunks of the source first


async def index_text(ctx: CommandContext, *, source_kind: str, source_id: str, text: str, kind: str | None = None,
                     contact_id: str | None = None, vehicle_id: str | None = None, visibility: str = "all",
                     personal_allowlisted: bool = False, trust: str | None = None, is_historical: bool | None = None,
                     happened_at: datetime | None = None, speaker: str | None = None, lang: str | None = None,
                     source_locator: dict | None = None, source_version: int | None = None, manifest_id: str | None = None,
                     replace: bool = False) -> dict:
    """Chunk + label + index one source. Idempotent per (source_kind, source_id, content hash)."""
    if source_kind not in SOURCE_KINDS:
        raise ValidationFailed(f"source_kind must be one of {SOURCE_KINDS}")
    if visibility not in VISIBILITIES:
        raise ValidationFailed(f"visibility must be one of {VISIBILITIES}")
    trust = trust or TRUST_BY_SOURCE.get(source_kind, "untrusted_external")
    if trust not in ("untrusted_external", "internal", "approved"):
        raise ValidationFailed("trust must be untrusted_external|internal|approved")
    if trust == "approved" and source_kind not in ("knowledge", "procedure"):
        raise ValidationFailed("only knowledge/procedure sources may carry approved trust")
    historical = HISTORICAL_BY_SOURCE.get(source_kind, True) if is_historical is None else bool(is_historical)
    tomb = (await ctx.db.execute(select(CorpusChunk).where(CorpusChunk.source_kind == source_kind, CorpusChunk.source_id == source_id,
                                                           CorpusChunk.tombstoned_at.is_not(None)))).scalars().first()
    if tomb is not None and tomb.tombstone_reason not in ("reindexed", "retired"):
        raise Blocked("source is tombstoned; re-admission needs an explicit readmit", tombstone_reason=tomb.tombstone_reason)
    manifest = None
    if manifest_id:
        manifest = await ctx.db.get(CorpusManifest, manifest_id)
    if manifest is None:
        manifest = await active_manifest(ctx.db)
    existing = (await ctx.db.execute(select(CorpusChunk).where(CorpusChunk.source_kind == source_kind, CorpusChunk.source_id == source_id,
                                                               CorpusChunk.tombstoned_at.is_(None))
                                     .order_by(CorpusChunk.chunk_index))).scalars().all()
    pieces = chunk_text(text)
    hashes = [sha256_hex(p)[:40] for p in pieces]
    if existing and not replace:
        if [c.content_hash for c in existing] == hashes:
            return {"chunks": [serialize_chunk(c, include_text=False) for c in existing], "created": 0, "unchanged": True}
        replace = True
    if existing and replace:
        await ctx.db.execute(delete(CorpusChunk).where(CorpusChunk.id.in_([c.id for c in existing])))
    embeddings, emb_info, emb_error = await embed_texts(pieces)
    if emb_info and manifest is not None and manifest.embedding_dims and emb_info.get("dims") not in (None, manifest.embedding_dims):
        # never mix incompatible dimensions in one index: keep FTS, drop the vector
        emb_error = f"embedding dims {emb_info.get('dims')} differ from manifest {manifest.embedding_dims}; vector discarded"
        embeddings = [None] * len(pieces)
        emb_info = None
    now = ctx.now
    rows: list[CorpusChunk] = []
    for i, piece in enumerate(pieces):
        c = CorpusChunk(manifest_id=manifest.id if manifest else None, source_kind=source_kind, source_id=source_id, chunk_index=i,
                        text=piece, embedding=embeddings[i], embedding_model=emb_info["model"] if emb_info and embeddings[i] else None,
                        embedding_dims=emb_info["dims"] if emb_info and embeddings[i] else None, trust=trust,
                        acl={"visibility": visibility, "personal_allowlisted": bool(personal_allowlisted)},
                        lang=lang or detect_lang(piece), contact_id=contact_id, vehicle_id=vehicle_id, happened_at=happened_at,
                        speaker=speaker, source_locator=dict(source_locator or {}), is_historical=historical, kind=kind,
                        content_hash=hashes[i], source_version=source_version, indexed_at=now, chunker_version=CHUNKER_VERSION,
                        created_by=ctx.actor.user_id)
        ctx.db.add(c)
        rows.append(c)
    await ctx.db.flush()
    if rows:
        await ctx.db.execute(update(CorpusChunk).where(CorpusChunk.id.in_([c.id for c in rows]))
                             .values(tsv=func.to_tsvector("english", CorpusChunk.text)).execution_options(synchronize_session=False))
    if manifest is not None:
        manifest.last_indexed_at = now
        counts = dict(manifest.counts or {})
        counts[source_kind] = int(counts.get(source_kind, 0)) + len(rows)
        counts["chunks"] = int(counts.get("chunks", 0)) + len(rows) - len(existing)
        manifest.counts = counts
        ctx.touch(manifest, "corpus_manifest")  # the manifest is a business row: mutating it bumps its version
        if emb_error:
            manifest.failures = list(manifest.failures or [])[-199:] + [{"at": now.isoformat(), "source": f"{source_kind}:{source_id}",
                                                                          "stage": "embedding", "error": emb_error}]
    ctx.record(f"Indexed {source_kind} {source_id[:8]} ({len(rows)} chunks, {trust})", entity_kind=source_kind, entity_id=source_id,
               kind="system", state="indexed", visibility="owner" if visibility != "all" or personal_allowlisted else "all",
               details={"chunks": len(rows), "trust": trust, "visibility": visibility, "embedding": bool(emb_info), "embedding_error": emb_error})
    ctx.emit("corpus.indexed", aggregate_type=source_kind, aggregate_id=source_id,
             payload={"chunks": len(rows), "trust": trust, "replaced": bool(existing)})
    return {"chunks": [serialize_chunk(c, include_text=False) for c in rows], "created": len(rows), "unchanged": False,
            "embedding": emb_info, "embedding_error": emb_error}


@command("corpus.index_text", input=IndexTextIn, perm="knowledge.write", action_class="internal",
         description="Chunk and index one source with ACL + trust labels. Messages are untrusted evidence; never authority.")
async def corpus_index_text(ctx: CommandContext, inp: IndexTextIn) -> dict:
    return await index_text(ctx, **inp.model_dump())


class TombstoneIn(BaseModel):
    source_kind: str
    source_id: str
    reason: str = "access_revoked"


async def tombstone_source(ctx: CommandContext, source_kind: str, source_id: str, *, reason: str) -> int:
    """Exclude a source from every retrieval immediately and quarantine its text/embedding (A06, invariant 12).
    Only minimal audit metadata (locator, timestamps, reason) is retained."""
    rows = (await ctx.db.execute(select(CorpusChunk).where(CorpusChunk.source_kind == source_kind, CorpusChunk.source_id == source_id,
                                                           CorpusChunk.tombstoned_at.is_(None)))).scalars().all()
    for c in rows:
        c.tombstoned_at = ctx.now
        c.tombstone_reason = reason
        c.text = ""
        c.tsv = None
        c.embedding = None
        c.embedding_model = None
        c.embedding_dims = None
        c.bump(ctx.actor.user_id or ctx.actor.client_id)  # quarantining a chunk is a mutation: bump its version
    if rows:
        ctx.changed.append({"kind": "corpus_source", "id": f"{source_kind}:{source_id}", "version": len(rows)})
        await ctx.db.flush()
        await ctx.db.execute(update(CorpusChunk).where(CorpusChunk.id.in_([c.id for c in rows]))
                             .values(tsv=None).execution_options(synchronize_session=False))
        ctx.record(f"Tombstoned {source_kind} {source_id[:8]} ({len(rows)} chunks): {reason}", entity_kind=source_kind,
                   entity_id=source_id, kind="access", state="tombstoned", details={"chunks": len(rows), "reason": reason})
        ctx.emit("corpus.tombstoned", aggregate_type=source_kind, aggregate_id=source_id, payload={"chunks": len(rows), "reason": reason})
    return len(rows)


@command("corpus.tombstone", input=TombstoneIn, perm="knowledge.write", action_class="internal",
         description="Remove a source from retrieval and embeddings now (allowlist narrowed, deletion, access revoked).")
async def corpus_tombstone(ctx: CommandContext, inp: TombstoneIn) -> dict:
    n = await tombstone_source(ctx, inp.source_kind, inp.source_id, reason=inp.reason)
    return {"tombstoned": n, "source_kind": inp.source_kind, "source_id": inp.source_id}


class ReindexIn(IndexTextIn):
    readmit: bool = False  # explicit re-admission of a tombstoned source (owner decision, e.g. allowlist widened again)


@command("corpus.reindex_source", input=ReindexIn, perm="knowledge.write", action_class="internal",
         description="Re-chunk a source (parser/chunker upgrade or corrected text). Tombstoned sources need readmit=true.")
async def corpus_reindex_source(ctx: CommandContext, inp: ReindexIn) -> dict:
    data = inp.model_dump()
    readmit = data.pop("readmit")
    data["replace"] = True
    tombs = (await ctx.db.execute(select(CorpusChunk).where(CorpusChunk.source_kind == inp.source_kind, CorpusChunk.source_id == inp.source_id,
                                                            CorpusChunk.tombstoned_at.is_not(None)))).scalars().all()
    blocking = [t for t in tombs if t.tombstone_reason not in ("reindexed", "retired")]
    if blocking and not readmit:
        raise Blocked("source is tombstoned; pass readmit=true to re-admit it", tombstone_reason=blocking[0].tombstone_reason)
    if tombs:
        await ctx.db.execute(delete(CorpusChunk).where(CorpusChunk.id.in_([t.id for t in tombs])))
        if blocking:
            ctx.record(f"Re-admitted {inp.source_kind} {inp.source_id[:8]} to the corpus", entity_kind=inp.source_kind,
                       entity_id=inp.source_id, kind="access", state="readmitted", visibility="owner")
    return await index_text(ctx, **data)


# ── corpus: coverage / stats ─────────────────────────────────────────────────
async def coverage(db) -> dict:
    """Manifest stats + failures + excluded scope (spec §9.2: show coverage, failures, excluded scope, most recent sync)."""
    m = await active_manifest(db)
    by_kind = {k: int(n) for k, n in (await db.execute(select(CorpusChunk.source_kind, func.count())
                                                       .where(CorpusChunk.tombstoned_at.is_(None)).group_by(CorpusChunk.source_kind))).all()}
    by_trust = {k: int(n) for k, n in (await db.execute(select(CorpusChunk.trust, func.count())
                                                        .where(CorpusChunk.tombstoned_at.is_(None)).group_by(CorpusChunk.trust))).all()}
    tombstoned = int((await db.execute(select(func.count()).select_from(CorpusChunk).where(CorpusChunk.tombstoned_at.is_not(None)))).scalar_one())
    embedded = int((await db.execute(select(func.count()).select_from(CorpusChunk)
                                     .where(CorpusChunk.tombstoned_at.is_(None), CorpusChunk.embedding.is_not(None)))).scalar_one())
    eval_targets = int((await db.execute(select(func.count()).select_from(EvalCase).where(EvalCase.excluded_from_retrieval.is_(True)))).scalar_one())
    last = (await db.execute(select(func.max(CorpusChunk.indexed_at)))).scalar_one()
    span = (await db.execute(select(func.min(CorpusChunk.happened_at), func.max(CorpusChunk.happened_at))
                             .where(CorpusChunk.tombstoned_at.is_(None)))).one()
    knowledge_counts = {k: int(n) for k, n in (await db.execute(select(KnowledgeItem.status, func.count()).group_by(KnowledgeItem.status))).all()}
    total = sum(by_kind.values())
    return {
        "manifest": serialize_manifest(m) if m else None,
        "defined": m is not None,
        "claim": "No manifest recorded — coverage is undefined" if m is None else "Coverage per manifest",
        "counts": {"chunks": total, "by_source_kind": by_kind, "by_trust": by_trust, "embedded": embedded,
                   "knowledge_items": knowledge_counts},
        "excluded": {"tombstoned_chunks": tombstoned, "eval_targets": eval_targets,
                     **({k: v for k, v in (m.excluded or {}).items() if k != "dedupe_key"} if m else {})},
        "failures": list(m.failures or []) if m else [],
        "coverage": {"from": _iso(m.coverage_from) if m else None, "to": _iso(m.coverage_to) if m else None,
                     "observed_from": _iso(span[0]), "observed_to": _iso(span[1])},
        "last_indexed_at": _iso(last),
        "embedding": {"configured": bool(os.environ.get("EMBEDDINGS_URL")), "model": m.embedding_model if m else None,
                      "dims": m.embedding_dims if m else None},
    }


async def list_manifests(db) -> list[dict]:
    rows = (await db.execute(select(CorpusManifest).order_by(CorpusManifest.created_at.desc()))).scalars().all()
    return [serialize_manifest(m) for m in rows]


# ── read helpers (routers) ───────────────────────────────────────────────────
def visible_item_clauses(actor) -> list:
    """Role-safe filter for knowledge item reads. Owner: all. Others: never owner-visibility items; customer
    exceptions only with contacts.read; mechanics (assigned scope) only general style/policy/template items."""
    from ..domain.policy import has_perm
    if actor.kind == "system" or (actor.kind in ("user", "agent") and actor.role == "owner"):
        return []
    clauses = [KnowledgeItem.visibility == "all"]
    if actor.kind == "external" or not has_perm(actor, "contacts.read"):
        clauses.append(KnowledgeItem.kind != "exception")
    if actor.kind == "external" or actor.scope == "assigned":
        clauses.append(KnowledgeItem.kind.in_(("style", "policy", "template", "procedure_note")))
        clauses.append(KnowledgeItem.scope["contact_id"].as_string().is_(None))
    return clauses
