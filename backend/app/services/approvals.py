"""Exact approval protocol and external action execution (spec §11.4–11.5).

approve  -> revalidate bindings, mark approved, re-dispatch the bound command with ctx.approval set.
           Handlers of consequential commands must NOT perform the external effect inline; they
           persist an ExternalAction intent via `intend_external_action()` and enqueue
           `external_action.execute`, which the sole executor claims with a lease and fencing token.
decline / cancel / expire / invalidate keep the exact history.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select, update

from ..core.errors import Blocked, Conflict, NotFound
from ..core.ids import new_id
from ..models.runtime import Approval, ExternalAction
from ..domain import jobs
from ..domain.commands import CommandContext, command, dispatch, spec_for

EXECUTORS: dict[str, Any] = {}  # command_name -> async fn(db, action) -> receipt dict


def executor(command_name: str):
    """Register the adapter call that performs a persisted external action."""
    def deco(fn):
        EXECUTORS[command_name] = fn
        return fn
    return deco


class ApprovalDecision(BaseModel):
    approval_id: str
    expected_version: int | None = None
    note: str | None = None


class ApprovalEdit(BaseModel):
    approval_id: str
    payload: dict
    note: str | None = None


def _brief(a: Approval) -> dict:
    return {"id": a.id, "kind": a.kind, "status": a.status, "version": a.approval_version, "title": a.title,
            "command_name": a.command_name, "payload": a.payload, "payload_hash": a.payload_hash,
            "targets": a.targets, "consequence": a.consequence, "checks": a.checks, "sources": a.sources,
            "expires_at": a.expires_at.isoformat() if a.expires_at else None, "receipt": a.receipt,
            "result": a.result, "invalidated_reason": a.invalidated_reason, "supersedes_id": a.supersedes_id,
            "superseded_by_id": a.superseded_by_id, "entity_kind": a.entity_kind, "entity_id": a.entity_id,
            "requested_by": a.requested_by, "authorized_by": a.authorized_by,
            "decided_at": a.decided_at.isoformat() if a.decided_at else None, "review_path": a.review_path,
            "external_action_id": a.external_action_id, "policy_version": a.policy_version,
            "created_at": a.created_at.isoformat() if a.created_at else None}


async def _load(ctx: CommandContext, approval_id: str, expected_version: int | None) -> Approval:
    a = (await ctx.db.execute(select(Approval).where(Approval.id == approval_id).with_for_update())).scalar_one_or_none()
    if a is None:
        raise NotFound("approval not found")
    if expected_version is not None and a.approval_version != expected_version:
        raise Conflict("approval version changed — review again", current_version=a.approval_version)
    return a


@command("approvals.approve", input=ApprovalDecision, perm="approve", action_class="owner_only",
         description="Approve an exact action version and execute it through the bound command.")
async def approve(ctx: CommandContext, inp: ApprovalDecision) -> dict:
    a = await _load(ctx, inp.approval_id, inp.expected_version)
    now = ctx.now
    if a.status != "pending":
        raise Blocked(f"approval is {a.status}", status=a.status)
    if a.expires_at and a.expires_at < now:
        a.status = "expired"
        ctx.record(f"Approval expired: {a.title}", entity_kind="approval", entity_id=a.id, kind="approval", state="expired")
        raise Blocked("approval expired", status="expired")
    spec = spec_for(a.command_name)
    inp_model = spec.input_model.model_validate(a.payload)
    # revalidate bindings immediately before execution (spec §11.4)
    failing: list[str] = []
    if spec.revalidate:
        failing = await spec.revalidate(ctx, inp_model, a)
    if failing:
        a.status = "invalidated"
        a.invalidated_reason = "; ".join(failing)
        a.checks = list(a.checks or []) + [{"key": "revalidate", "ok": False, "label": f} for f in failing]
        ctx.record(f"Approval invalidated before execution: {a.title}", entity_kind="approval", entity_id=a.id,
                   kind="approval", state="invalidated", details={"reasons": failing}, exception=True)
        ctx.emit("approval.changed", aggregate_type="approval", aggregate_id=a.id, payload={"status": "invalidated"})
        return {"approval": _brief(a), "executed": False}
    a.status = "approved"
    a.authorized_by = ctx.actor.user_id
    a.decided_at = now
    a.decision_note = inp.note
    ctx.record(f"Approved: {a.title}", entity_kind="approval", entity_id=a.id, kind="approval", state="approved")
    # execute the bound command under this approval (same transaction; external effects become intents)
    sub = ctx.child(approval=a)
    res = await dispatch(sub, a.command_name, inp_model, commit=False)
    a.result = res.data if isinstance(res.data, dict) else {"data": res.data}
    if a.external_action_id:
        a.status = "queued"
    else:
        a.status = "confirmed"
        a.receipt = {"kind": "internal", "changed": res.changed, "at": now.isoformat()}
    ctx.emit("approval.changed", aggregate_type="approval", aggregate_id=a.id, payload={"status": a.status})
    return {"approval": _brief(a), "executed": True, "result": res.to_dict()}


@command("approvals.decline", input=ApprovalDecision, perm="approve", action_class="owner_only",
         description="Decline a pending approval.")
async def decline(ctx: CommandContext, inp: ApprovalDecision) -> dict:
    a = await _load(ctx, inp.approval_id, inp.expected_version)
    if a.status != "pending":
        raise Blocked(f"approval is {a.status}", status=a.status)
    a.status = "declined"
    a.authorized_by = ctx.actor.user_id
    a.decided_at = ctx.now
    a.decision_note = inp.note
    ctx.record(f"Declined: {a.title}", entity_kind="approval", entity_id=a.id, kind="approval", state="declined")
    ctx.emit("approval.changed", aggregate_type="approval", aggregate_id=a.id, payload={"status": "declined"})
    return {"approval": _brief(a)}


@command("approvals.edit", input=ApprovalEdit, perm="approve", action_class="owner_only",
         description="Edit creates a new version and invalidates the prior one (spec §11.4).")
async def edit(ctx: CommandContext, inp: ApprovalEdit) -> dict:
    a = await _load(ctx, inp.approval_id, None)
    if a.status != "pending":
        raise Blocked(f"approval is {a.status}", status=a.status)
    spec = spec_for(a.command_name)
    from ..core.ids import stable_hash
    new_inp = spec.input_model.model_validate({**a.payload, **inp.payload})
    payload = new_inp.model_dump(mode="json")
    b = Approval(kind=a.kind, command_name=a.command_name, payload=payload, payload_hash=stable_hash(payload),
                 approval_version=a.approval_version + 1, status="pending",
                 title=spec.summary(new_inp) if spec.summary else a.title, summary=a.summary,
                 recommendation=a.recommendation, targets=(spec.consequence(new_inp) if spec.consequence else {}).get("targets", a.targets),
                 consequence=spec.consequence(new_inp) if spec.consequence else a.consequence,
                 checks=[{"key": "edited", "ok": True, "label": f"Edited from v{a.approval_version}"}],
                 sources=a.sources, conditions=a.conditions, record_versions=a.record_versions,
                 policy_version=a.policy_version, requested_by=a.requested_by, entity_kind=a.entity_kind,
                 entity_id=a.entity_id, mission_id=a.mission_id, run_id=a.run_id,
                 expires_at=ctx.now + timedelta(hours=spec.expires_hours), supersedes_id=a.id)
    ctx.db.add(b)
    await ctx.db.flush()
    b.review_path = f"/approvals/{b.id}"
    a.status = "invalidated"
    a.invalidated_reason = f"Details changed — review again (superseded by v{b.approval_version})"
    a.superseded_by_id = b.id
    ctx.record(f"Approval edited: {a.title} → version {b.approval_version}", entity_kind="approval", entity_id=b.id,
               kind="approval", state="pending", details={"supersedes": a.id})
    ctx.emit("approval.changed", aggregate_type="approval", aggregate_id=b.id, payload={"status": "pending", "supersedes": a.id})
    return {"approval": _brief(b), "invalidated": _brief(a)}


class ApprovalRef(BaseModel):
    approval_id: str
    reason: str = ""


@command("approvals.invalidate", input=ApprovalRef, perm=None, action_class="internal",
         description="System/agent invalidation when a bound fact, target or policy changed.")
async def invalidate(ctx: CommandContext, inp: ApprovalRef) -> dict:
    a = await _load(ctx, inp.approval_id, None)
    if a.status in ("pending", "approved", "queued"):
        a.status = "invalidated"
        a.invalidated_reason = inp.reason or "Details changed — review again"
        ctx.record(f"Approval invalidated: {a.title}", entity_kind="approval", entity_id=a.id, kind="approval",
                   state="invalidated", details={"reason": a.invalidated_reason}, exception=True)
        ctx.emit("approval.changed", aggregate_type="approval", aggregate_id=a.id, payload={"status": "invalidated"})
        if a.external_action_id:
            await ctx.db.execute(update(ExternalAction).where(ExternalAction.id == a.external_action_id,
                                                              ExternalAction.state == "intent").values(state="cancelled"))
    return {"approval": _brief(a)}


async def invalidate_for_entity(ctx: CommandContext, entity_kind: str, entity_id: str, reason: str) -> int:
    rows = (await ctx.db.execute(select(Approval).where(Approval.entity_kind == entity_kind, Approval.entity_id == entity_id,
                                                        Approval.status.in_(("pending", "approved", "queued"))))).scalars().all()
    for a in rows:
        await invalidate(ctx.child(), ApprovalRef(approval_id=a.id, reason=reason))
    return len(rows)


async def expire_stale(session_factory) -> int:
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        res = await db.execute(update(Approval).where(Approval.status == "pending", Approval.expires_at < now)
                               .values(status="expired"))
        await db.commit()
        return res.rowcount or 0


# ── External action intents (spec §11.5) ────────────────────────────────────
async def intend_external_action(ctx: CommandContext, *, command_name: str, payload: dict, dedupe_key: str,
                                 provider: str, entity_kind: str | None = None, entity_id: str | None = None,
                                 run_at: datetime | None = None) -> ExternalAction:
    existing = (await ctx.db.execute(select(ExternalAction).where(ExternalAction.dedupe_key == dedupe_key))).scalar_one_or_none()
    if existing is not None:
        return existing  # same logical action: never a second send
    act = ExternalAction(dedupe_key=dedupe_key, command_name=command_name, payload=payload,
                         approval_id=ctx.approval.id if ctx.approval else None, provider=provider,
                         correlation_id=ctx.correlation_id, entity_kind=entity_kind, entity_id=entity_id,
                         actor=ctx.actor.snapshot(), mission_id=ctx.mission_id, state="intent")
    ctx.db.add(act)
    await ctx.db.flush()
    if ctx.approval is not None:
        ctx.approval.external_action_id = act.id
        ctx.approval.executor = "worker"
    ctx.record(f"Queued external action: {command_name}", entity_kind=entity_kind, entity_id=entity_id,
               kind="automation", state="queued", details={"external_action_id": act.id, "provider": provider})
    await jobs.enqueue(ctx.db, "external_action.execute", {"action_id": act.id}, dedupe_key=f"xa:{act.id}",
                       run_at=run_at, correlation_id=ctx.correlation_id)
    return act


@jobs.job("external_action.execute")
async def _execute_external_action(jctx: jobs.JobContext, payload: dict) -> dict:
    db = jctx.db
    act = (await db.execute(select(ExternalAction).where(ExternalAction.id == payload["action_id"]).with_for_update())).scalar_one_or_none()
    if act is None:
        return {"skipped": "missing"}
    if act.state not in ("intent", "unknown", "claimed"):
        return {"skipped": act.state}
    now = datetime.now(timezone.utc)
    # revalidate approval binding right before execution
    if act.approval_id:
        a = await db.get(Approval, act.approval_id)
        if a is None or a.status not in ("approved", "queued", "executing"):
            act.state = "cancelled"
            act.error = f"approval {a.status if a else 'missing'}"
            await db.commit()
            return {"cancelled": act.error}
        a.status = "executing"
    act.state = "executing"
    act.fencing_token = (act.fencing_token or 0) + 1
    act.lease_token = jctx.lease_token
    act.lease_until = now + timedelta(minutes=5)
    act.attempts = (act.attempts or 0) + 1
    token = act.fencing_token
    await db.commit()

    fn = EXECUTORS.get(act.command_name)
    if fn is None:
        act.state = "failed"
        act.error = "no executor registered"
        if act.approval_id:
            a = await db.get(Approval, act.approval_id)
            a.status = "failed"
            a.result = {"error": act.error}
        await db.commit()
        return {"failed": act.error}
    try:
        receipt = await fn(db, act)
        # An executor that could only hand the work to a person (no adapter) reports sent=False /
        # handed_off=True; keep that visibly distinct from a provider-confirmed effect.
        outcome = "handed_off" if (receipt.get("handed_off") or receipt.get("sent") is False) else "confirmed"
    except UnknownResult as e:
        receipt = {"error": str(e), "provider_ref": e.provider_ref}
        outcome = "unknown"
    except Exception as e:  # noqa: BLE001
        receipt = {"error": f"{type(e).__name__}: {e}"}
        outcome = "failed"
    # fenced commit: a stale executor cannot record a result
    res = await db.execute(update(ExternalAction).where(ExternalAction.id == act.id, ExternalAction.fencing_token == token)
                           .values(state=outcome, receipt=receipt, executed_at=now, error=receipt.get("error"),
                                   provider_ref=receipt.get("provider_ref"), lease_token=None))
    if res.rowcount != 1:
        await db.rollback()
        return {"stale": True}
    if act.approval_id:
        a = await db.get(Approval, act.approval_id)
        # "handed_off" stays its own status: a person still has to finish the work, so it is never
        # relabelled as a provider-confirmed effect.
        a.status = {"confirmed": "confirmed", "handed_off": "handed_off", "failed": "failed",
                    "unknown": "result_unknown"}[outcome]
        a.receipt = receipt
    from ..models.runtime import ActivityEntry, Event
    db.add(ActivityEntry(at=now, actor=act.actor or {"kind": "system"}, what=f"External action {outcome}: {act.command_name}",
                         entity_kind=act.entity_kind, entity_id=act.entity_id, kind="automation", state=outcome,
                         receipt=receipt, correlation_id=act.correlation_id, mission_id=act.mission_id,
                         exception=(outcome not in ("confirmed", "handed_off")), details={"external_action_id": act.id}))
    db.add(Event(type=f"external_action.{outcome}", aggregate_type=act.entity_kind, aggregate_id=act.entity_id,
                 payload={"external_action_id": act.id, "command": act.command_name, "receipt": receipt},
                 correlation_id=act.correlation_id, happened_at=now, actor=act.actor or {}))
    await db.commit()
    return {outcome: act.id}


class UnknownResult(Exception):
    """Raised by executors when the provider may have accepted the action but the result was lost."""
    def __init__(self, message: str, provider_ref: str | None = None):
        super().__init__(message)
        self.provider_ref = provider_ref


async def reconcile_unknown(session_factory) -> int:
    """Startup / periodic: unknown actions stay unknown until an adapter reconciler resolves them."""
    return 0
