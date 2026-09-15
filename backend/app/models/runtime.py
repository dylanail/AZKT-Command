from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, BusinessRow

APPROVAL_STATES = ("pending", "approved", "queued", "executing", "confirmed", "handed_off", "failed",
                   "result_unknown", "declined", "expired", "invalidated", "canceled")
RUN_STATES = ("queued", "running", "waiting_approval", "waiting_external", "waiting_until",
              "needs_information", "succeeded", "failed", "cancelled")
ROLES = ("manager", "customer_sales", "sourcing", "logistics", "shop", "listings", "finance")


class Mission(Base, BusinessRow):
    """Persistent assignment contract (spec §10.3)."""
    __tablename__ = "missions"
    outcome: Mapped[str] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(String, default="chat")  # chat|telegram|mcp|http|event|schedule|intake
    channel: Mapped[str] = mapped_column(String, default="web")
    initiating_actor: Mapped[dict] = mapped_column(JSON, default=dict)  # Actor snapshot
    role: Mapped[str] = mapped_column(String, default="manager", index=True)
    responsible_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    entity_refs: Mapped[list] = mapped_column(JSON, default=list)  # [{kind, id, label}]
    hard_requirements: Mapped[list] = mapped_column(JSON, default=list)
    preferences: Mapped[list] = mapped_column(JSON, default=list)
    permitted_scope: Mapped[dict] = mapped_column(JSON, default=dict)
    source_requirements: Mapped[list] = mapped_column(JSON, default=list)
    completion_evidence: Mapped[list] = mapped_column(JSON, default=list)
    stop_conditions: Mapped[list] = mapped_column(JSON, default=list)
    budget: Mapped[dict] = mapped_column(JSON, default=dict)  # {steps, seconds, usd}
    procedure_version: Mapped[str | None] = mapped_column(String, nullable=True)
    policy_version: Mapped[str] = mapped_column(String, default="v1")
    status: Mapped[str] = mapped_column(String, default="open", index=True)  # open|running|waiting_approval|waiting_external|waiting_until|needs_information|succeeded|failed|cancelled|paused
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    waiting_on: Mapped[str | None] = mapped_column(String, nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)  # {summary, changed:[{kind,id,version}], citations, receipts, needed_input}
    cursor: Mapped[int] = mapped_column(Integer, default=0)  # monotonically increasing progress sequence
    updates: Mapped[list] = mapped_column(JSON, default=list)  # [{seq, at, state, text}]
    thread_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)  # chat thread
    case_id: Mapped[str | None] = mapped_column(String, nullable=True)
    client_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    delegated_request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    parent_mission_id: Mapped[str | None] = mapped_column(String, nullable=True)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    correlation_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    paused_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Run(Base, BusinessRow):
    """One bounded execution of a mission (spec §10.4)."""
    __tablename__ = "runs"
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id"), index=True)
    status: Mapped[str] = mapped_column(String, default="queued", index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    effort: Mapped[str | None] = mapped_column(String, nullable=True)
    steps_used: Mapped[int] = mapped_column(Integer, default=0)
    budget_steps: Mapped[int] = mapped_column(Integer, default=20)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)  # {messages, pending_tool}
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Monotonic fencing token: only the holder of the newest lease may persist a step or finish the run
    # (spec §10.4 step 2, invariant 2 / H01). A stale worker's fenced UPDATE matches no row.
    fencing_token: Mapped[int] = mapped_column(Integer, default=0)
    worker_id: Mapped[str | None] = mapped_column(String, nullable=True)
    trigger: Mapped[str | None] = mapped_column(String, nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    used_model: Mapped[bool] = mapped_column(Boolean, default=False)


class RunStep(Base, BusinessRow):
    __tablename__ = "run_steps"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String, default="tool")  # model|tool|wait|escalate|decision
    tool_name: Mapped[str | None] = mapped_column(String, nullable=True)
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    decision: Mapped[str | None] = mapped_column(String, nullable=True)  # allowed|needs_review|blocked


class ChatTurn(Base, BusinessRow):
    """Manager / specialist conversation turn, shared by web and Telegram (spec §5.5, §10.9)."""
    __tablename__ = "chat_turns"
    thread_key: Mapped[str] = mapped_column(String, index=True)  # f"{user_id}:{role}" or client thread
    role: Mapped[str] = mapped_column(String)  # user|assistant|system|tool
    content: Mapped[str] = mapped_column(Text, default="")
    blocks: Mapped[list] = mapped_column(JSON, default=list)  # structured content (attachments, actions, citations)
    channel: Mapped[str] = mapped_column(String, default="web")
    mission_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context: Mapped[dict] = mapped_column(JSON, default=dict)  # pinned entity context at the time
    actor_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    agent_role: Mapped[str] = mapped_column(String, default="manager")
    state: Mapped[str] = mapped_column(String, default="final")  # final|streaming|error


class Approval(Base, BusinessRow):
    """Exact approval bound to a payload hash (spec §11.4)."""
    __tablename__ = "approvals"
    kind: Mapped[str] = mapped_column(String, index=True)  # send_message|publish|bid|parts_order|booking|payment|price_change|permission|quote_request|translation_request|fact_overwrite|other
    command_name: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    payload_hash: Mapped[str] = mapped_column(String, index=True)
    approval_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    title: Mapped[str] = mapped_column(String, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    recommendation: Mapped[str] = mapped_column(Text, default="")
    targets: Mapped[dict] = mapped_column(JSON, default=dict)  # recipients/account/channel/site
    consequence: Mapped[dict] = mapped_column(JSON, default=dict)  # {amount, currency, scope, moves_money: bool}
    checks: Mapped[list] = mapped_column(JSON, default=list)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    conditions: Mapped[dict] = mapped_column(JSON, default=dict)
    record_versions: Mapped[dict] = mapped_column(JSON, default=dict)  # {entity: version} bound at request time
    policy_version: Mapped[str] = mapped_column(String, default="v1")
    requested_by: Mapped[dict] = mapped_column(JSON, default=dict)  # Actor snapshot
    entity_kind: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    entity_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    mission_id: Mapped[str | None] = mapped_column(String, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    authorized_by: Mapped[str | None] = mapped_column(String, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    executor: Mapped[str | None] = mapped_column(String, nullable=True)
    external_action_id: Mapped[str | None] = mapped_column(String, nullable=True)
    receipt: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    invalidated_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    supersedes_id: Mapped[str | None] = mapped_column(String, nullable=True)
    superseded_by_id: Mapped[str | None] = mapped_column(String, nullable=True)
    review_path: Mapped[str | None] = mapped_column(String, nullable=True)  # /approvals/<id>


class Permission(Base, BusinessRow):
    """A bounded standing permission (spec §11.3)."""
    __tablename__ = "permissions"
    subject_kind: Mapped[str] = mapped_column(String, default="workflow")  # workflow|user|client|role
    subject_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    workflow_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    action_pattern: Mapped[str] = mapped_column(String)  # command name or glob e.g. "inbox.send"
    allowed_records: Mapped[dict] = mapped_column(JSON, default=dict)
    recipients: Mapped[list] = mapped_column(JSON, default=list)
    domains: Mapped[list] = mapped_column(JSON, default=list)
    fields: Mapped[list] = mapped_column(JSON, default=list)
    per_action_limit: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    cumulative_limit: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String, nullable=True)
    used_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    reserved_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    freshness_requirements: Mapped[dict] = mapped_column(JSON, default=dict)
    excluded_cases: Mapped[list] = mapped_column(JSON, default=list)
    rate_limit: Mapped[dict] = mapped_column(JSON, default=dict)  # {per_day: n}
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    authorized_by: Mapped[str | None] = mapped_column(String, nullable=True)
    permission_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="active", index=True)  # active|revoked|expired|paused
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    proposal_id: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")


class ExternalAction(Base, BusinessRow):
    """Persisted intent for an external side effect; single executor (spec §11.5)."""
    __tablename__ = "external_actions"
    dedupe_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    command_name: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    approval_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    permission_id: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str] = mapped_column(String, default="intent", index=True)  # intent|claimed|executing|confirmed|handed_off|failed|unknown|cancelled|reconciled
    lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    provider: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    receipt: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_state: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mission_id: Mapped[str | None] = mapped_column(String, nullable=True)
    entity_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    actor: Mapped[dict] = mapped_column(JSON, default=dict)


class Event(Base, BusinessRow):
    """Outbox / domain event (spec §12.1)."""
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("provider", "provider_event_id", name="uq_event_provider"),)
    type: Mapped[str] = mapped_column(String, index=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    aggregate_type: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    aggregate_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    aggregate_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    causation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    happened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[dict] = mapped_column(JSON, default=dict)


class Job(Base, BusinessRow):
    """Durable queue row with lease + fencing (spec §13.2)."""
    __tablename__ = "jobs"
    kind: Mapped[str] = mapped_column(String, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    state: Mapped[str] = mapped_column(String, default="queued", index=True)  # queued|running|done|failed|cancelled
    priority: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    dedupe_key: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    correlation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String, nullable=True)


class ActivityEntry(Base, BusinessRow):
    """Append-only business activity (spec §2.3 Activity)."""
    __tablename__ = "activity"
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    actor: Mapped[dict] = mapped_column(JSON, default=dict)  # {kind, id, name, client_id}
    what: Mapped[str] = mapped_column(Text)
    entity_kind: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    entity_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String, default="task", index=True)  # automation|approval|message|task|fact|intake|payment|publication|system|access|connection
    state: Mapped[str | None] = mapped_column(String, nullable=True)
    receipt: Mapped[dict] = mapped_column(JSON, default=dict)
    command_name: Mapped[str | None] = mapped_column(String, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    mission_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    policy_version: Mapped[str | None] = mapped_column(String, nullable=True)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    correlation_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    visibility: Mapped[str] = mapped_column(String, default="all")  # all|owner|finance
    exception: Mapped[bool] = mapped_column(Boolean, default=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class CommandLog(Base):
    """Idempotency store for commands (spec §12.1)."""
    __tablename__ = "command_log"
    __table_args__ = (UniqueConstraint("actor_key", "request_id", name="uq_command_request"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    actor_key: Mapped[str] = mapped_column(String, index=True)
    request_id: Mapped[str] = mapped_column(String)
    command_name: Mapped[str] = mapped_column(String)
    payload_hash: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ModelUsage(Base, BusinessRow):
    __tablename__ = "model_usage"
    run_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    mission_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    workflow: Mapped[str] = mapped_column(String, default="chat", index=True)
    model: Mapped[str] = mapped_column(String)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class WorkflowControl(Base):
    """Deterministic pause switches: global, per workflow, per thread (spec §11.5)."""
    __tablename__ = "workflow_controls"
    key: Mapped[str] = mapped_column(String, primary_key=True)  # global | workflow:<name> | thread:<conversation id>
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    changed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
