"""The single write path (ARCHITECTURE.md, spec §12.1).

    @command("tasks.create", input=TaskCreateIn, perm="tasks.write", action_class="internal",
             records=lambda p: [("vehicle", p.vehicle_id)] if p.vehicle_id else [])
    async def tasks_create(ctx: CommandContext, inp: TaskCreateIn) -> dict: ...

    result = await dispatch(ctx, "tasks.create", {...})
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import Denied, DomainError, ValidationFailed
from ..core.ids import new_id, stable_hash
from ..models.runtime import ActivityEntry, Approval, CommandLog, Event, Permission
from . import policy
from .actors import Actor

ACTION_CLASSES = ("read", "internal", "consequential", "owner_only", "forbidden_for_agents")


@dataclass
class CommandSpec:
    name: str
    handler: Callable[..., Awaitable[Any]]
    input_model: type[BaseModel]
    perm: str | None
    action_class: str
    description: str = ""
    approval_kind: str | None = None
    workflow_key: str | None = None
    records: Callable[[BaseModel], list[tuple[str, str]]] | None = None
    limits: Callable[[BaseModel], dict] | None = None        # recipients/amount/currency/records for standing perms
    summary: Callable[[BaseModel], str] | None = None       # human title for approvals
    consequence: Callable[[BaseModel], dict] | None = None  # amount/currency/scope for approvals
    revalidate: Callable[..., Awaitable[list[str]]] | None = None  # (ctx, inp, approval) -> failing reasons
    expires_hours: int = 48


REGISTRY: dict[str, CommandSpec] = {}


def command(name: str, *, input: type[BaseModel], perm: str | None, action_class: str = "internal",
            description: str = "", approval_kind: str | None = None, workflow_key: str | None = None,
            records=None, limits=None, summary=None, consequence=None, revalidate=None, expires_hours: int = 48):
    if action_class not in ACTION_CLASSES:
        raise ValueError(f"bad action_class {action_class}")

    def deco(fn):
        if name in REGISTRY:
            raise ValueError(f"command {name} already registered")
        REGISTRY[name] = CommandSpec(name, fn, input, perm, action_class, description or (fn.__doc__ or "").strip(),
                                     approval_kind or name.split(".")[0], workflow_key, records, limits,
                                     summary, consequence, revalidate, expires_hours)
        return fn
    return deco


@dataclass
class CommandContext:
    db: AsyncSession
    actor: Actor
    request_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    channel: str = "web"
    source_refs: list = field(default_factory=list)
    approval: Approval | None = None
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    _activity: list = field(default_factory=list)
    _events: list = field(default_factory=list)
    changed: list = field(default_factory=list)   # [{kind, id, version}]
    mission_id: str | None = None
    run_id: str | None = None

    def record(self, what: str, *, entity_kind: str | None = None, entity_id: str | None = None,
               kind: str = "task", state: str | None = None, receipt: dict | None = None,
               sources: list | None = None, visibility: str = "all", exception: bool = False,
               details: dict | None = None) -> ActivityEntry:
        row = ActivityEntry(
            at=self.now, actor=self.actor.snapshot(), what=what, entity_kind=entity_kind, entity_id=entity_id,
            kind=kind, state=state, receipt=receipt or {}, sources=sources or list(self.source_refs),
            correlation_id=self.correlation_id, policy_version=policy.POLICY_VERSION, visibility=visibility,
            exception=exception, details=details or {}, run_id=self.run_id, mission_id=self.mission_id,
        )
        self.db.add(row)
        self._activity.append(row)
        return row

    def emit(self, type: str, *, aggregate_type: str | None = None, aggregate_id: str | None = None,
             aggregate_version: int | None = None, payload: dict | None = None, provider: str | None = None,
             provider_event_id: str | None = None) -> Event:
        ev = Event(type=type, aggregate_type=aggregate_type, aggregate_id=aggregate_id,
                   aggregate_version=aggregate_version, payload=payload or {}, provider=provider,
                   provider_event_id=provider_event_id, correlation_id=self.correlation_id,
                   causation_id=self.causation_id, happened_at=self.now, actor=self.actor.snapshot())
        self.db.add(ev)
        self._events.append(ev)
        return ev

    def touch(self, row, kind: str) -> None:
        """Bump version, mark as changed for the result envelope."""
        row.bump(self.actor.user_id or self.actor.client_id)
        self.changed.append({"kind": kind, "id": row.id, "version": row.version})

    def child(self, **overrides) -> "CommandContext":
        """A context for a nested command call inside a handler (same transaction)."""
        base = dict(db=self.db, actor=self.actor, request_id=None, correlation_id=self.correlation_id,
                    causation_id=self.causation_id, channel=self.channel, source_refs=list(self.source_refs),
                    approval=None, now=self.now, mission_id=self.mission_id, run_id=self.run_id)
        base.update(overrides)
        c = CommandContext(**base)
        c.changed = self.changed
        return c


@dataclass
class CommandResult:
    status: str                      # ok | needs_review | blocked
    data: Any = None
    changed: list = field(default_factory=list)
    approval_id: str | None = None
    decision: dict = field(default_factory=dict)
    request_id: str | None = None

    def to_dict(self) -> dict:
        return {"status": self.status, "data": self.data, "changed": self.changed,
                "approval_id": self.approval_id, "decision": self.decision, "request_id": self.request_id}


def spec_for(name: str) -> CommandSpec:
    try:
        return REGISTRY[name]
    except KeyError:
        raise DomainError(f"unknown command {name}", code="unknown_command")


async def _assigned_sets(db, actor: Actor) -> tuple[set[str], set[str]]:
    """Vehicles/tasks an assigned-scope person (or anyone without vehicles.all) may write to —
    the same rule as access.visible_vehicle_ids on the read side."""
    limited = actor.scope == "assigned" or not actor.perms.get("vehicles.all", False)
    if actor.kind not in ("user", "agent") or not limited or not actor.user_id:
        return set(), set()
    from ..models.tasks import Task
    rows = (await db.execute(select(Task.id, Task.vehicle_id).where(Task.owner_user_id == actor.user_id))).all()
    return {v for _, v in rows if v}, {t for t, _ in rows}


async def _expand_records(db, records: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Some commands name an indirect record — a listing package rather than the vehicle it describes.
    Record scope, the approval's entity binding and invalidate_for_entity all work on the business
    record that owns it, so resolve those here, once, before any of them run."""
    pkg_ids = [i for k, i in records if k == "listing_package"]
    if not pkg_ids:
        return records
    from ..models.listings import ListingPackage
    owner = {pid: vid for pid, vid in (await db.execute(
        select(ListingPackage.id, ListingPackage.vehicle_id).where(ListingPackage.id.in_(pkg_ids)))).all() if vid}
    out: list[tuple[str, str]] = []
    for kind, rid in records:
        if kind == "listing_package":
            if rid in owner:
                out.append(("vehicle", owner[rid]))
            continue        # an unknown package is the handler's NotFound to report, not a scope refusal
        out.append((kind, rid))
    return out


async def _thread_scope(db, records: list[tuple[str, str]]) -> dict[str, dict]:
    """Resolve `conversation` / `draft` records to what record scope needs: the vehicles the thread is
    linked to and whether it lives on the personal account. Mirrors services/inbox.list_threads.

    A record id that resolves to nothing is left out, so a missing row is still reported honestly by
    the handler (404) instead of becoming a scope refusal."""
    by_record = {i: i for k, i in records if k == "conversation"}
    draft_ids = {i for k, i in records if k == "draft"}
    if not by_record and not draft_ids:
        return {}
    from ..models.comms import Connection, Conversation, Draft
    if draft_ids:
        for did, cid in (await db.execute(select(Draft.id, Draft.conversation_id)
                                          .where(Draft.id.in_(draft_ids)))).all():
            if cid:
                by_record[did] = cid
    convs = {c.id: c for c in (await db.execute(select(Conversation).where(
        Conversation.id.in_(set(by_record.values()))))).scalars().all()}
    personal = set((await db.execute(select(Connection.id).where(
        Connection.provider == "gmail_personal"))).scalars().all())
    out: dict[str, dict] = {}
    for rid, cid in by_record.items():
        c = convs.get(cid)
        if c is None:
            continue
        out[rid] = {"vehicle_ids": [l["id"] for l in (c.links or []) if l.get("kind") == "vehicle" and l.get("id")],
                    "personal": c.connection_id in personal}
    return out


async def dispatch(ctx: CommandContext, name: str, payload: dict | BaseModel, *, commit: bool = True) -> CommandResult:
    spec = spec_for(name)
    try:
        inp = payload if isinstance(payload, spec.input_model) else spec.input_model.model_validate(payload)
    except ValidationError as e:
        raise ValidationFailed("invalid input", errors=e.errors(include_url=False))

    payload_hash = stable_hash(inp.model_dump(mode="json"))

    # idempotent replay
    if ctx.request_id:
        prior = (await ctx.db.execute(select(CommandLog).where(
            CommandLog.actor_key == ctx.actor.key, CommandLog.request_id == ctx.request_id))).scalar_one_or_none()
        if prior is not None:
            if prior.payload_hash != payload_hash or prior.command_name != name:
                raise DomainError("request_id reused with a different payload", code="idempotency_conflict")
            r = prior.result or {}
            return CommandResult(r.get("status", "ok"), r.get("data"), r.get("changed", []), r.get("approval_id"),
                                 r.get("decision", {}), ctx.request_id)

    # record scope
    records = spec.records(inp) if spec.records else []
    records = await _expand_records(ctx.db, [(k, i) for k, i in records if i])
    assigned_vehicles, assigned_tasks = await _assigned_sets(ctx.db, ctx.actor)
    threads = await _thread_scope(ctx.db, records)
    record_reasons = policy.check_record_scope(ctx.actor, records, assigned_vehicles, assigned_tasks, threads=threads)

    decision = await policy.evaluate(ctx.db, ctx.actor, spec, inp, approval=ctx.approval, record_reasons=record_reasons)

    if decision.outcome == "blocked":
        result = CommandResult("blocked", None, [], None, decision.to_dict(), ctx.request_id)
        await _log(ctx, name, payload_hash, result, commit)
        raise Denied("; ".join(decision.reasons) or "not allowed", decision=decision.to_dict(), command=name)

    if decision.outcome == "needs_review":
        approval = await _create_approval(ctx, spec, inp, payload_hash, decision, records)
        result = CommandResult("needs_review", {"approval": _approval_brief(approval)}, [], approval.id,
                               decision.to_dict(), ctx.request_id)
        await _log(ctx, name, payload_hash, result, commit)
        return result

    # allowed: reserve standing-permission usage atomically before the effect
    if decision.permission_id:
        await _reserve_permission(ctx, decision.permission_id, spec, inp)

    if inspect.iscoroutinefunction(spec.handler):
        data = await spec.handler(ctx, inp)
    else:
        data = spec.handler(ctx, inp)
    result = CommandResult("ok", data, list(ctx.changed), None, decision.to_dict(), ctx.request_id)
    await _log(ctx, name, payload_hash, result, commit)
    return result


async def _log(ctx: CommandContext, name: str, payload_hash: str, result: CommandResult, commit: bool) -> None:
    if ctx.request_id:
        ctx.db.add(CommandLog(id=new_id(), actor_key=ctx.actor.key, request_id=ctx.request_id, command_name=name,
                              payload_hash=payload_hash, status=result.status, result=_jsonable(result.to_dict()),
                              created_at=ctx.now))
    if commit:
        try:
            await ctx.db.commit()
        except IntegrityError:
            await ctx.db.rollback()
            # concurrent duplicate request_id: return the stored result
            prior = (await ctx.db.execute(select(CommandLog).where(
                CommandLog.actor_key == ctx.actor.key, CommandLog.request_id == ctx.request_id))).scalar_one_or_none()
            if prior is None:
                raise
            r = prior.result or {}
            result.status, result.data, result.changed, result.approval_id = (
                r.get("status", "ok"), r.get("data"), r.get("changed", []), r.get("approval_id"))


def _jsonable(obj):
    import json
    return json.loads(json.dumps(obj, default=str))


def _approval_brief(a: Approval) -> dict:
    return {"id": a.id, "kind": a.kind, "title": a.title, "status": a.status, "version": a.approval_version,
            "expires_at": a.expires_at.isoformat() if a.expires_at else None, "review_path": a.review_path}


async def _create_approval(ctx: CommandContext, spec: CommandSpec, inp: BaseModel, payload_hash: str,
                           decision: policy.Decision, records: list) -> Approval:
    from datetime import timedelta
    payload = _jsonable(inp.model_dump(mode="json"))
    # Supersede an identical pending approval instead of duplicating it.
    existing = (await ctx.db.execute(select(Approval).where(
        Approval.command_name == spec.name, Approval.payload_hash == payload_hash, Approval.status == "pending"))).scalar_one_or_none()
    if existing is not None:
        return existing
    title = spec.summary(inp) if spec.summary else spec.name
    consequence = spec.consequence(inp) if spec.consequence else {}
    entity_kind, entity_id = (records[0] if records else (None, None))
    a = Approval(kind=spec.approval_kind or "other", command_name=spec.name, payload=payload, payload_hash=payload_hash,
                 title=title, summary=spec.description, consequence=consequence, targets=consequence.get("targets", {}),
                 requested_by=ctx.actor.snapshot(), entity_kind=entity_kind, entity_id=entity_id,
                 expires_at=ctx.now + timedelta(hours=spec.expires_hours), policy_version=decision.policy_version,
                 mission_id=ctx.mission_id, run_id=ctx.run_id, sources=list(ctx.source_refs),
                 checks=[{"key": "policy", "ok": True, "label": "; ".join(decision.reasons)}])
    ctx.db.add(a)
    await ctx.db.flush()
    a.review_path = f"/approvals/{a.id}"
    ctx.record(f"Approval requested: {title}", entity_kind="approval", entity_id=a.id, kind="approval", state="pending")
    ctx.emit("approval.changed", aggregate_type="approval", aggregate_id=a.id, payload={"status": "pending"})
    return a


async def _reserve_permission(ctx: CommandContext, permission_id: str, spec: CommandSpec, inp: BaseModel) -> None:
    from decimal import Decimal
    row = (await ctx.db.execute(select(Permission).where(Permission.id == permission_id).with_for_update())).scalar_one()
    info = spec.limits(inp) if spec.limits else {}
    amt = info.get("amount")
    if amt is not None:
        amt = Decimal(str(amt))
        if row.cumulative_limit is not None and row.used_amount + row.reserved_amount + amt > row.cumulative_limit:
            raise Denied("cumulative permission limit reached", command=spec.name)
        row.used_amount = row.used_amount + amt
    row.used_count = (row.used_count or 0) + 1
