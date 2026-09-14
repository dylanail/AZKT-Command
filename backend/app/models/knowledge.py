from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, BusinessRow


class KnowledgeItem(Base, BusinessRow):
    """Approved knowledge: policies, style, templates, examples, lessons, customer exceptions (spec §9.1)."""
    __tablename__ = "knowledge_items"
    kind: Mapped[str] = mapped_column(String, index=True)  # policy|style|template|example|lesson|exception|procedure_note
    title: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text, default="")
    scope: Mapped[dict] = mapped_column(JSON, default=dict)  # {contact_id?, vehicle_id?, workflow?}
    status: Mapped[str] = mapped_column(String, default="proposed", index=True)  # proposed|approved|retired|rejected
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_kind: Mapped[str | None] = mapped_column(String, nullable=True)  # correction|edit_diff|teach|import|manual
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    visibility: Mapped[str] = mapped_column(String, default="all")
    approved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tests: Mapped[list] = mapped_column(JSON, default=list)


class CorpusManifest(Base, BusinessRow):
    __tablename__ = "corpus_manifests"
    label: Mapped[str] = mapped_column(String, default="")
    sources: Mapped[list] = mapped_column(JSON, default=list)
    coverage_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    coverage_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    excluded: Mapped[dict] = mapped_column(JSON, default=dict)
    embedding_model: Mapped[str | None] = mapped_column(String, nullable=True)
    embedding_dims: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunker_version: Mapped[str] = mapped_column(String, default="v1")
    status: Mapped[str] = mapped_column(String, default="building")  # building|active|superseded|failed
    failures: Mapped[list] = mapped_column(JSON, default=list)


class CorpusChunk(Base, BusinessRow):
    """Retrieval chunk with ACL and trust label; FTS via tsvector, optional embedding (spec §9.2–9.3)."""
    __tablename__ = "corpus_chunks"
    manifest_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    source_kind: Mapped[str] = mapped_column(String, index=True)  # message|document|knowledge|procedure|vehicle_fact|task_note
    source_id: Mapped[str] = mapped_column(String, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    tsv: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String, nullable=True)
    trust: Mapped[str] = mapped_column(String, default="untrusted_external")  # untrusted_external|internal|approved
    acl: Mapped[dict] = mapped_column(JSON, default=dict)  # {visibility: all|owner|finance, personal_allowlist: bool}
    lang: Mapped[str | None] = mapped_column(String, nullable=True)
    contact_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    vehicle_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    happened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    speaker: Mapped[str | None] = mapped_column(String, nullable=True)
    source_locator: Mapped[dict] = mapped_column(JSON, default=dict)
    is_historical: Mapped[bool] = mapped_column(Boolean, default=True)
    tombstoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class Procedure(Base, BusinessRow):
    __tablename__ = "procedures"
    key: Mapped[str] = mapped_column(String, unique=True)
    title: Mapped[str] = mapped_column(String)
    goal: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="proposed", index=True)  # proposed|offline_tested|shadow|supervised|bounded_automatic|withdrawn
    current_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    workflow_key: Mapped[str | None] = mapped_column(String, nullable=True)


class ProcedureVersion(Base, BusinessRow):
    __tablename__ = "procedure_versions"
    procedure_id: Mapped[str] = mapped_column(ForeignKey("procedures.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    spec: Mapped[dict] = mapped_column(JSON, default=dict)  # inputs, hard_constraints, normal_path, alternatives, evidence_of_completion, escalation, tests
    source_kind: Mapped[str | None] = mapped_column(String, nullable=True)  # teach_text|demonstration|correction|email_example
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    test_results: Mapped[dict] = mapped_column(JSON, default=dict)
    stage: Mapped[str] = mapped_column(String, default="proposed")
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    withdrawn_reason: Mapped[str | None] = mapped_column(String, nullable=True)


class PromotionProposal(Base, BusinessRow):
    """Evidence-backed proposal to enable a bounded automatic workflow (spec §9.5)."""
    __tablename__ = "promotion_proposals"
    workflow_key: Mapped[str] = mapped_column(String, index=True)
    action_class: Mapped[str] = mapped_column(String)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)  # {cases, days, accepted_pct, critical_errors, samples}
    thresholds: Mapped[dict] = mapped_column(JSON, default=dict)
    proposed_permission: Mapped[dict] = mapped_column(JSON, default=dict)
    exclusions: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String, default="proposed", index=True)  # proposed|approved|declined|paused|withdrawn
    decided_by: Mapped[str | None] = mapped_column(String, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    permission_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_asked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    regression: Mapped[dict] = mapped_column(JSON, default=dict)


class EvalCase(Base, BusinessRow):
    __tablename__ = "eval_cases"
    set_name: Mapped[str] = mapped_column(String, index=True)
    split: Mapped[str] = mapped_column(String, default="test")  # train|val|test
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    expected: Mapped[dict] = mapped_column(JSON, default=dict)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    contact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    excluded_from_retrieval: Mapped[bool] = mapped_column(Boolean, default=True)
    last_result: Mapped[dict] = mapped_column(JSON, default=dict)


class WorkflowOutcome(Base, BusinessRow):
    """Per-case record used to measure reliability (spec §1.3, §9.5)."""
    __tablename__ = "workflow_outcomes"
    workflow_key: Mapped[str] = mapped_column(String, index=True)
    entity_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    outcome: Mapped[str] = mapped_column(String)  # accepted|style_edit|factual_edit|declined|critical_error|unanswered
    review_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
