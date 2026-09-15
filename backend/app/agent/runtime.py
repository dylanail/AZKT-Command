"""The AZKT agent runtime: missions, runs and the bounded tool loop (spec §10.3–10.7).

    event / chat / connector request
        -> create_mission(...)                 persistent assignment contract
        -> start_run(...)                      durable job "mission.run"
        -> claim_run(...)                      lease + monotonic fencing token
        -> execute_run(...)                    assemble context -> model -> tools -> checkpoint
        -> succeeded | waiting_* | needs_information | failed | cancelled

Deterministic code owns the boundaries: leases and fencing (H01), budgets and loop-breaking (H06),
cancellation that keeps completed effects (H05), and the distinction between "this run finished" and
"the case finished" (H12). The model chooses steps inside the contract; it never changes the outcome,
the authority or the spend.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from ..adapters.model import ModelClient, ModelRefused, ModelUnavailable
from ..core.config import settings
from ..core.ids import new_id, stable_hash
from ..domain import jobs
from ..domain.actors import SYSTEM_ACTOR, Actor
from ..domain.commands import CommandContext, dispatch
from ..domain.events import on_event
from ..domain.jobs import sweep
from ..domain.policy import effective_perms
from ..models.runtime import Mission, Run, RunStep
from . import prompts, tools

log = logging.getLogger("azkt.agent.runtime")

# Test/alternate-provider hook: fn(db, *, workflow, run_id, mission_id, essential) -> object with .complete()
MODEL_FACTORY: Callable[..., Any] | None = None

TERMINAL_MISSION = ("succeeded", "failed", "cancelled")
WAITING_MISSION = ("waiting_approval", "waiting_external", "waiting_until", "needs_information", "paused")
LEASE_SECONDS = 120
MAX_IDENTICAL_FAILURES = 3      # after this many identical tool failures: exactly one alternative, then escalate


class StaleLease(Exception):
    """A worker whose lease was superseded tried to persist a step (invariant 2 / H01)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ── control tools (runtime-owned; not business commands) ─────────────────────
class NeedsInformationIn(BaseModel):
    question: str = Field(description="The single material question for the person, in plain English")
    already_checked: list[str] = Field(default_factory=list, description="What you already looked at")
    missing: list[str] = Field(default_factory=list, description="The smallest missing facts")


class WaitUntilIn(BaseModel):
    next_check_at: datetime = Field(description="UTC instant of the next check")
    reason: str = Field(default="", description="What you are waiting for")


class WaitExternalIn(BaseModel):
    waiting_on: str = Field(description="Who or what the case is waiting on, e.g. 'vendor reply'")
    next_check_at: datetime | None = None
    reason: str = ""


CONTROL_TOOLS: dict[str, tuple[type[BaseModel], str]] = {
    "runtime_needs_information": (NeedsInformationIn,
        "Stop and ask the person exactly one material question. Use this instead of guessing or writing. "
        "The mission stays open with status needs_information until they answer."),
    "runtime_wait_until": (WaitUntilIn,
        "Stop this run and persist a next check at a time. The mission resumes automatically; no model stays "
        "alive while waiting."),
    "runtime_wait_external": (WaitExternalIn,
        "Stop this run because the case is waiting on someone outside AZKT (a vendor, a customer, a provider). "
        "Persists the waiting condition and the next check."),
}


def control_definitions() -> list[dict]:
    out = []
    for name, (model, desc) in CONTROL_TOOLS.items():
        schema = tools.strict_schema(model.model_json_schema())
        d = {"name": name, "description": desc, "input_schema": schema}
        if tools.strict_compatible(schema):
            d["strict"] = True
        out.append(d)
    return out


CHECKPOINT_MESSAGES = 40


def _has_tool_result(msg: dict) -> bool:
    content = msg.get("content")
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def trim_messages(messages: list[dict], keep: int = CHECKPOINT_MESSAGES) -> list[dict]:
    """Checkpoint a conversation that is still *valid* to resume from.

    A plain `messages[-keep:]` slice would start the resumed conversation on an assistant turn, or on a
    user message holding `tool_result` blocks whose `tool_use` has been dropped — both are rejected by the
    API, so a crashed run could never be picked up (invariant 2, H01/H02). Instead the opening context is
    always kept and whole assistant/tool-result turns are dropped from the front.
    """
    msgs = list(messages)
    if len(msgs) <= keep:
        return msgs
    head: list[dict] = []
    i = 0
    while i < len(msgs) and msgs[i].get("role") == "user" and not _has_tool_result(msgs[i]):
        head.append(msgs[i])
        i += 1
    tail = msgs[i:]
    while len(head) + len(tail) > keep and len(tail) > 2:
        tail = tail[1:]                                   # drop the assistant turn ...
        while tail and _has_tool_result(tail[0]):
            tail = tail[1:]                               # ... together with its tool results
    return head + tail


# ── actors and scope ─────────────────────────────────────────────────────────
def scope_of(actor: Actor) -> dict:
    """The permitted action scope frozen onto the mission (spec §10.3). Later resumes intersect with it."""
    return {"kind": actor.kind, "user_id": actor.user_id, "role": actor.role, "scope": actor.scope,
            "perms": sorted(k for k, v in (actor.perms or {}).items() if v),
            "client_id": actor.client_id, "client_scopes": list(actor.client_scopes or []),
            "client_record_scope": dict(actor.client_record_scope or {}),
            "delegation_depth": actor.delegation_depth}


async def actor_for_mission(db, mission: Mission) -> Actor:
    """Rebuild the run's actor from *current* grants, intersected with the mission's permitted scope.

    A revoked person or connector loses access on the next step, and a mission can never gain authority
    it did not have when it was created (invariant 14, A03, J03).
    """
    scope = dict(mission.permitted_scope or {})
    allowed = set(scope.get("perms") or [])
    if scope.get("kind") == "external" or mission.client_id:
        from ..models.external import ExternalClient
        from ..services import external_clients as ec
        client = await db.get(ExternalClient, mission.client_id or scope.get("client_id"))
        if client is None or not ec.is_active(client):
            raise PermissionError("external client is not active")
        actor = await ec.actor_for(db, client, delegation_depth=int(scope.get("delegation_depth") or 0))
        actor.perms = {k: (v and k in allowed) for k, v in actor.perms.items()}
        actor.client_scopes = [s for s in (actor.client_scopes or []) if s in set(scope.get("client_scopes") or [])]
        actor.mission_id = mission.id
        return actor
    from ..models.auth import User
    user = await db.get(User, scope.get("user_id") or mission.responsible_user_id)
    if user is None:
        return Actor(kind="system", role="system", scope="all", perms={}, display_name="AZKT")
    if getattr(user, "status", "active") != "active":
        raise PermissionError("the person this mission acts for is no longer active")
    perms = effective_perms(user.role, user.perms)
    perms = {k: (v and k in allowed) for k, v in perms.items()}
    return Actor(kind="agent", user_id=user.id, role=user.role, scope=user.scope, perms=perms,
                 display_name=user.display_name or user.handle, agent_role=mission.role, mission_id=mission.id)


# ── mission lifecycle ────────────────────────────────────────────────────────
def default_budget() -> dict:
    return {"steps": int(settings.MODEL_MAX_TOOL_STEPS), "seconds": int(settings.MODEL_RUN_TIMEOUT_SECONDS)}


async def create_mission(db, actor: Actor, *, outcome: str, trigger: str = "chat", channel: str = "web",
                         role: str = "manager", entity_refs: list | None = None,
                         hard_requirements: list | None = None, preferences: list | None = None,
                         thread_key: str | None = None, context: dict | None = None,
                         budget: dict | None = None, parent_mission_id: str | None = None,
                         depth: int = 0, client_id: str | None = None, delegated_request_id: str | None = None,
                         correlation_id: str | None = None, case_id: str | None = None,
                         stop_conditions: list | None = None, source_requirements: list | None = None) -> Mission:
    m = Mission(outcome=outcome[:4000], trigger=trigger, channel=channel, role=role,
                initiating_actor=actor.snapshot(), responsible_user_id=actor.user_id,
                entity_refs=list(entity_refs or []), hard_requirements=list(hard_requirements or []),
                preferences=list(preferences or []), permitted_scope=scope_of(actor),
                source_requirements=list(source_requirements or []), completion_evidence=[],
                stop_conditions=list(stop_conditions or ["budget exhausted", "needs information", "waiting approval"]),
                budget=dict(budget or default_budget()), status="open", thread_key=thread_key,
                case_id=case_id, client_id=client_id, delegated_request_id=delegated_request_id,
                parent_mission_id=parent_mission_id, depth=int(depth),
                correlation_id=correlation_id or new_id(), result={"context": dict(context or {})},
                cursor=0, updates=[], created_by=actor.user_id, updated_by=actor.user_id)
    db.add(m)
    await db.flush()
    add_update(m, "open", f"Mission opened: {outcome[:200]}")
    try:
        from ..services import procedures
        pv = await procedures.current_version_for(db, role)
        if pv is not None:
            m.procedure_version = pv.id
    except Exception:  # noqa: BLE001
        pass
    return m


def add_update(mission: Mission, state: str, text: str, **extra) -> dict:
    """Append one progress update under a monotonically increasing cursor (pollers use it, spec §10.8)."""
    mission.cursor = int(mission.cursor or 0) + 1
    row = {"seq": mission.cursor, "at": _iso(_now()), "state": state, "text": text[:1000]}
    row.update({k: v for k, v in extra.items() if v is not None})
    mission.updates = (list(mission.updates or []) + [row])[-300:]
    return row


async def start_run(db, mission: Mission, *, trigger: str = "job", enqueue: bool = True) -> Run:
    run = Run(mission_id=mission.id, status="queued", budget_steps=int((mission.budget or {}).get("steps") or 20),
              trigger=trigger, checkpoint={}, result={}, created_by=mission.responsible_user_id)
    db.add(run)
    await db.flush()
    if mission.status in ("open", *WAITING_MISSION):
        mission.status = "running" if enqueue else mission.status
    add_update(mission, "queued", "Run queued", run_id=run.id)
    if enqueue:
        await jobs.enqueue(db, "mission.run", {"mission_id": mission.id, "run_id": run.id},
                           dedupe_key=f"mission.run:{run.id}", correlation_id=mission.correlation_id)
    return run


async def claim_run(db, run_id: str, worker_id: str, *, lease_seconds: int = LEASE_SECONDS) -> tuple[Run, str] | None:
    """Take the run's lease and bump its fencing token. Returns None when another worker holds a live lease."""
    now = _now()
    token = new_id()
    res = await db.execute(
        update(Run).where(Run.id == run_id, Run.status.in_(("queued", "running", "waiting_approval",
                                                            "waiting_external", "waiting_until")),
                          (Run.lease_until.is_(None)) | (Run.lease_until < now))
        .values(lease_token=token, lease_until=now + timedelta(seconds=lease_seconds),
                fencing_token=Run.fencing_token + 1, status="running", worker_id=worker_id,
                started_at=func.coalesce(Run.started_at, now)))
    await db.commit()
    if res.rowcount != 1:
        return None
    run = await db.get(Run, run_id)
    await db.refresh(run)
    return run, token


async def fenced(db, run_id: str, token: str, /, **values) -> None:
    """Persist a change to the run only if this worker still holds the lease (H01).

    On a superseded lease nothing is written and `StaleLease` is raised. The rollback that discards this
    worker's pending writes also expires every loaded object, so callers keep plain ids (not ORM handles)
    for anything they still need afterwards.
    """
    res = await db.execute(update(Run).where(Run.id == run_id, Run.lease_token == token).values(**values))
    if res.rowcount != 1:
        await db.rollback()
        raise StaleLease(f"run {run_id}: lease superseded; this worker may not commit")
    await db.commit()


async def heartbeat(db, run_id: str, lease_token: str, *, lease_seconds: int = LEASE_SECONDS) -> None:
    await fenced(db, run_id, lease_token, lease_until=_now() + timedelta(seconds=lease_seconds))


async def cancel_mission(db, actor: Actor, mission_id: str, *, reason: str = "cancelled by the owner") -> dict:
    """Stop remaining work; already completed effects are kept (spec §11.5 step 6, H05)."""
    m = await db.get(Mission, mission_id)
    if m is None:
        from ..core.errors import NotFound
        raise NotFound("mission not found")
    if m.status in TERMINAL_MISSION:
        return {"mission": brief(m), "already": m.status}
    m.status = "cancelled"
    m.finished_at = _now()
    m.waiting_on = None
    m.next_check_at = None
    m.paused_reason = reason
    add_update(m, "cancelled", reason)
    await db.execute(update(Run).where(Run.mission_id == m.id,
                                       Run.status.in_(("queued", "running", "waiting_approval", "waiting_external",
                                                       "waiting_until")))
                     .values(status="cancelled", finished_at=_now(), lease_token=None, error=reason))
    from ..models.runtime import Job
    pending = (await db.execute(select(Job).where(Job.kind == "mission.run", Job.state == "queued"))).scalars().all()
    for j in pending:
        if (j.payload or {}).get("mission_id") == m.id:
            j.state, j.finished_at, j.last_error = "cancelled", _now(), reason
    await db.commit()
    return {"mission": brief(m), "cancelled": True}


def brief(m: Mission) -> dict:
    return {"id": m.id, "outcome": m.outcome, "status": m.status, "role": m.role, "trigger": m.trigger,
            "channel": m.channel, "entity_refs": list(m.entity_refs or []), "cursor": int(m.cursor or 0),
            "waiting_on": m.waiting_on, "next_check_at": _iso(m.next_check_at), "case_id": m.case_id,
            "result": dict(m.result or {}), "depth": int(m.depth or 0), "client_id": m.client_id,
            "delegated_request_id": m.delegated_request_id, "correlation_id": m.correlation_id,
            "created_at": _iso(m.created_at), "finished_at": _iso(m.finished_at),
            "paused_reason": m.paused_reason, "version": m.version}


def run_brief(r: Run) -> dict:
    return {"id": r.id, "mission_id": r.mission_id, "status": r.status, "steps_used": int(r.steps_used or 0),
            "budget_steps": int(r.budget_steps or 0), "started_at": _iso(r.started_at),
            "finished_at": _iso(r.finished_at), "error": r.error, "used_model": bool(r.used_model),
            "model": r.model, "fencing_token": int(r.fencing_token or 0), "result": dict(r.result or {})}


def updates_since(m: Mission, cursor: int = 0) -> list[dict]:
    return [u for u in (m.updates or []) if int(u.get("seq") or 0) > int(cursor or 0)]


# ── context assembly ─────────────────────────────────────────────────────────
async def assemble_context(db, mission: Mission, actor: Actor) -> tuple[str, list[dict]]:
    """System prompt (cacheable) + the mission's variable context as the first user message."""
    system = prompts.system_prompt(mission.role)
    if actor.kind == "external":
        system = system + "\n\n" + prompts.external_client_note(actor.client_name or actor.client_id or "connector")

    lines: list[str] = ["<mission>",
                        f"outcome: {mission.outcome}",
                        f"role: {mission.role}   trigger: {mission.trigger}   channel: {mission.channel}",
                        f"budget: {json.dumps(mission.budget or {})}"]
    if mission.hard_requirements:
        lines.append("hard requirements (never adapt these): " + "; ".join(str(x) for x in mission.hard_requirements))
    if mission.preferences:
        lines.append("preferences: " + "; ".join(str(x) for x in mission.preferences))
    scope = mission.permitted_scope or {}
    lines.append(f"acting for: {scope.get('role')} ({scope.get('kind')}), record scope: {scope.get('scope')}")
    if scope.get("client_scopes"):
        lines.append("external client grant: " + ", ".join(scope["client_scopes"]))
    lines.append("</mission>")

    ctxinfo = (mission.result or {}).get("context") or {}
    if ctxinfo:
        lines.append("<pinned_context>" + json.dumps(ctxinfo, default=str) + "</pinned_context>")

    facts = await _current_facts(db, mission, actor)
    if facts:
        lines.append("<current_facts>" + json.dumps(facts, default=str)[:20000] + "</current_facts>")

    proc = await _procedure_text(db, mission, actor)
    if proc:
        lines.append("<procedure>" + proc[:4000] + "</procedure>")

    progress = await _progress(db, mission)
    if progress:
        lines.append("<work_already_done>" + json.dumps(progress, default=str)[:6000] + "</work_already_done>")
        lines.append(prompts.RESUME_INSTRUCTION)

    messages: list[dict] = [{"role": "user", "content": "\n".join(lines)}]
    for turn in await _conversation(db, mission):
        messages.append(turn)
    messages.append({"role": "user", "content": f"<request>{mission.outcome}</request>\n"
                                                f"{prompts.NEEDED_INPUT_INSTRUCTION}"})
    return system, messages


async def _current_facts(db, mission: Mission, actor: Actor) -> dict:
    """Read the pinned entities through the read tools so the ACL is applied once, here."""
    out: dict = {}
    ctx = CommandContext(db=db, actor=actor, channel=mission.channel, correlation_id=mission.correlation_id,
                         mission_id=mission.id)
    for ref in (mission.entity_refs or [])[:5]:
        kind, rid = ref.get("kind"), ref.get("id")
        if not rid:
            continue
        try:
            if kind == "vehicle":
                res = await tools.execute(ctx, "vehicles_get_context", {"vehicle_id": rid})
            elif kind == "shipment":
                res = await tools.execute(ctx, "shipments_get_case", {"shipment_id": rid})
            elif kind == "import_request":
                res = await tools.execute(ctx, "requests_get", {"request_id": rid})
            else:
                continue
            out[f"{kind}:{rid}"] = res.to_dict()
        except Exception as e:  # noqa: BLE001
            out[f"{kind}:{rid}"] = {"status": "error", "error": str(e)[:200]}
    return out


async def _progress(db, mission: Mission) -> dict | None:
    """What earlier runs of this mission already did, for a resumed run.

    A resume (approval decided, next check due, client replied) starts a *new* run with an empty
    checkpoint, so without this the model would re-plan from the bare outcome and ask for the same
    consequential action again — producing a second approval for work the owner has already decided
    (spec §10.4 step 8, §11.4). States and receipts here are read from the records, never invented.
    """
    prior = dict(mission.result or {})
    updates = [u for u in (mission.updates or [])
               if u.get("state") not in ("open", "queued", "running", "resuming")][-12:]
    from ..models.runtime import Approval
    rows = (await db.execute(select(Approval).where(Approval.mission_id == mission.id)
                             .order_by(Approval.created_at))).scalars().all()
    if not (updates or rows or prior.get("summary") or prior.get("changed")):
        return None                      # a first run has nothing to replay
    out: dict = {"previous_result": {k: prior.get(k) for k in ("summary", "run_status", "error", "needed_input")
                                     if prior.get(k)},
                 "changed_so_far": (prior.get("changed") or [])[-20:],
                 "updates": updates}
    if rows:
        out["approvals"] = [{"id": a.id, "title": a.title, "command": a.command_name, "status": a.status,
                             "decided_at": _iso(a.decided_at), "decision_note": a.decision_note,
                             "receipt": dict(a.receipt or {}) or None,
                             "invalidated_reason": a.invalidated_reason} for a in rows[-10:]]
    return out


async def _procedure_text(db, mission: Mission, actor: Actor) -> str | None:
    if not mission.procedure_version:
        return None
    try:
        from ..models.knowledge import ProcedureVersion
        from ..services import procedures
        v = await db.get(ProcedureVersion, mission.procedure_version)
        if v is None:
            return None
        return json.dumps(procedures.redact_for(procedures.serialize_version(v), actor), default=str)
    except Exception:  # noqa: BLE001
        return None


async def _conversation(db, mission: Mission, limit: int = 12) -> list[dict]:
    if not mission.thread_key:
        return []
    from ..models.runtime import ChatTurn
    rows = (await db.execute(select(ChatTurn).where(ChatTurn.thread_key == mission.thread_key,
                                                    ChatTurn.role.in_(("user", "assistant")),
                                                    ChatTurn.state == "final")
                             .order_by(ChatTurn.created_at.desc()).limit(limit))).scalars().all()
    out = []
    for t in reversed(rows):
        text = (t.content or "").strip()
        if not text:
            continue
        out.append({"role": t.role, "content": (f"<prior_message channel=\"{t.channel}\">{text}</prior_message>"
                                                if t.role == "user" else text)[:6000]})
    return out


# ── the loop ─────────────────────────────────────────────────────────────────
@dataclass
class RunOutcome:
    run_status: str
    mission_status: str
    summary: str
    changed: list
    needed_input: dict | None = None
    approvals: list = None          # type: ignore[assignment]
    error: str | None = None
    steps: int = 0
    used_model: bool = False

    def to_dict(self) -> dict:
        return {"run_status": self.run_status, "mission_status": self.mission_status, "summary": self.summary,
                "changed": self.changed or [], "needed_input": self.needed_input,
                "approvals": self.approvals or [], "error": self.error, "steps": self.steps,
                "used_model": self.used_model}


def _model_client(db, **kw):
    if MODEL_FACTORY is not None:
        return MODEL_FACTORY(db, **kw)
    return ModelClient(db, **kw)


def _block_to_dict(b) -> dict | None:
    if isinstance(b, dict):
        return b
    dump = getattr(b, "model_dump", None)
    if callable(dump):
        try:
            return {k: v for k, v in dump(exclude_none=True).items() if k not in ("cache_control",)}
        except Exception:  # noqa: BLE001
            pass
    t = getattr(b, "type", None)
    if t == "text":
        return {"type": "text", "text": getattr(b, "text", "")}
    if t == "tool_use":
        return {"type": "tool_use", "id": getattr(b, "id", ""), "name": getattr(b, "name", ""),
                "input": getattr(b, "input", {}) or {}}
    return None


def _blocks(res) -> list:
    raw = getattr(res, "raw", None)
    content = getattr(raw, "content", None)
    if content is None and isinstance(raw, dict):
        content = raw.get("content")
    return list(content or [])


async def execute_run(db, mission: Mission, run: Run, lease_token: str, *, on_event_cb=None) -> RunOutcome:
    """One bounded model/tool loop. Persists a checkpoint after every completed step."""
    mission_id, run_id = mission.id, run.id
    try:
        actor = await actor_for_mission(db, mission)
    except PermissionError as e:
        return await _finish(db, mission, run, lease_token,
                             RunOutcome("failed", "paused", f"Access changed: {e}", [], error=str(e)))
    ctx = CommandContext(db=db, actor=actor, channel=mission.channel, correlation_id=mission.correlation_id,
                         causation_id=mission.delegated_request_id, mission_id=mission_id, run_id=run_id)
    budget = mission.budget or default_budget()
    deadline = time.monotonic() + float(budget.get("seconds") or settings.MODEL_RUN_TIMEOUT_SECONDS)
    max_steps = int(budget.get("steps") or settings.MODEL_MAX_TOOL_STEPS)

    checkpoint = dict(run.checkpoint or {})
    messages: list[dict] = checkpoint.get("messages") or []
    failures: dict[str, int] = dict(checkpoint.get("failures") or {})
    alternatives: dict[str, bool] = dict(checkpoint.get("alternatives") or {})
    approvals: list[dict] = list(checkpoint.get("approvals") or [])
    changed: list = list(checkpoint.get("changed") or [])
    if not messages:
        system, messages = await assemble_context(db, mission, actor)
        checkpoint["system"] = system
    system = checkpoint.get("system") or prompts.system_prompt(mission.role)

    tool_defs = tools.definitions(actor) + control_definitions()
    client = _model_client(db, workflow=f"agent:{mission.role}", run_id=run.id, mission_id=mission.id, essential=False)
    steps = int(run.steps_used or 0)
    final_text = ""
    used_model = bool(run.used_model)

    async def emit(kind: str, payload: dict) -> None:
        if on_event_cb is not None:
            await on_event_cb(kind, payload)

    while True:
        fresh = await _refresh_status(db, mission_id)
        if fresh == "cancelled":
            return await _finish(db, mission, run, lease_token,
                                 RunOutcome("cancelled", "cancelled", "Run cancelled; completed changes were kept.",
                                            changed, steps=steps, used_model=used_model, approvals=approvals))
        paused = await _paused_reason(db, mission)
        if paused:
            return await _finish(db, mission, run, lease_token,
                                 RunOutcome("failed", "paused", f"Automation is paused ({paused}).", changed,
                                            steps=steps, used_model=used_model, approvals=approvals,
                                            error=f"paused:{paused}"))
        if steps >= max_steps or time.monotonic() > deadline:
            reason = "step budget exhausted" if steps >= max_steps else "time budget exhausted"
            task = await _recovery_task(db, mission, run, reason,
                                        f"The agent run for “{mission.outcome[:120]}” stopped: {reason}.")
            return await _finish(db, mission, run, lease_token,
                                 RunOutcome("failed", "needs_information",
                                            f"I stopped before finishing ({reason}). I left a recovery task so this "
                                            f"does not get lost.", changed, steps=steps, used_model=used_model,
                                            approvals=approvals, error=reason,
                                            needed_input={"question": "How should I continue?", "task": task}))

        try:
            res = await client.complete(system=system, messages=messages, tools=tool_defs,
                                        max_tokens=8000, effort=settings.AZKT_MODEL_EFFORT)
        except ModelUnavailable as e:
            status = "paused" if "budget" in str(e).lower() else "needs_information"
            summary = prompts.DETERMINISTIC_UNAVAILABLE.format(reason=str(e)[:200])
            return await _finish(db, mission, run, lease_token,
                                 RunOutcome("needs_information", status, summary, changed, steps=steps,
                                            used_model=used_model, approvals=approvals, error=str(e)[:500]))
        except ModelRefused as e:
            summary = ("The model declined to continue this request"
                       + (f" ({e.category})" if e.category else "") + ". Nothing was changed by that step.")
            return await _finish(db, mission, run, lease_token,
                                 RunOutcome("needs_information", "needs_information", summary, changed, steps=steps,
                                            used_model=used_model, approvals=approvals, error=str(e)[:500]))
        used_model = True
        steps += 1
        run.model = getattr(res, "model", None) or run.model
        blocks = _blocks(res)
        assistant_blocks = [d for d in (_block_to_dict(b) for b in blocks) if d]
        text = (getattr(res, "text", "") or "").strip()
        if text:
            final_text = text
            await emit("text", {"text": text})
        if assistant_blocks:
            messages.append({"role": "assistant", "content": assistant_blocks})
        elif text:
            messages.append({"role": "assistant", "content": text})

        tool_uses = [d for d in assistant_blocks if d.get("type") == "tool_use"]
        if not tool_uses:
            break

        results: list[dict] = []
        stop: RunOutcome | None = None
        for tu in tool_uses:
            name, tid, payload = tu.get("name", ""), tu.get("id", ""), tu.get("input") or {}
            await emit("tool_started", {"tool": name, "id": tid})
            if name in CONTROL_TOOLS:
                stop_outcome, content = await _control(db, mission, run, name, payload, changed, approvals,
                                                       steps, used_model)
                results.append({"type": "tool_result", "tool_use_id": tid, "content": content})
                await emit("tool_finished", {"tool": name, "status": "ok"})
                if stop_outcome is not None:
                    stop = stop_outcome
                continue
            key = f"{name}:{stable_hash(payload)}"
            tr = await tools.execute(ctx, name, payload, run_id=run.id, seq=steps,
                                     request_id=f"run:{run.id}:{tid}")
            content = tr.for_model()
            if tr.status == "needs_review" and tr.approval:
                approvals.append(tr.approval)
                await emit("needs_review", {"tool": name, "approval": tr.approval})
            if tr.changed:
                changed.extend(tr.changed)
            if tr.is_error:
                failures[key] = failures.get(key, 0) + 1
                n = failures[key]
                if n == MAX_IDENTICAL_FAILURES and not alternatives.get(key):
                    alternatives[key] = True
                    db.add(RunStep(run_id=run.id, seq=steps, kind="decision", tool_name=name,
                                   input={"failures": n}, output={"decision": "try one permitted alternative"},
                                   ok=True, started_at=_now(), finished_at=_now(), decision="alternative_requested"))
                    content += ("\n\nRUNTIME: this exact call has now failed the same way 3 times. Try exactly ONE "
                                "permitted alternative route. If that also fails, stop — do not repeat this call.")
                elif n > MAX_IDENTICAL_FAILURES:
                    task = await _recovery_task(
                        db, mission, run, f"{name} failed {n} times",
                        f"The agent could not complete “{mission.outcome[:120]}”: {name} failed {n} times "
                        f"({tr.error or 'tool error'}). A person needs to take this over.")
                    db.add(RunStep(run_id=run.id, seq=steps, kind="escalate", tool_name=name,
                                   input={"failures": n}, output={"task": task}, ok=False,
                                   started_at=_now(), finished_at=_now(), decision="escalated"))
                    stop = RunOutcome("failed", "needs_information",
                                      f"I could not get {name} to work after {n} identical failures and one "
                                      f"alternative. I created a recovery task instead of retrying.",
                                      changed, steps=steps, used_model=used_model, approvals=approvals,
                                      error=f"repeated tool failure: {name}",
                                      needed_input={"question": "Please take this over or tell me another route.",
                                                    "task": task})
            else:
                failures.pop(key, None)
            results.append({"type": "tool_result", "tool_use_id": tid, "content": content,
                            **({"is_error": True} if tr.is_error else {})})
            await emit("tool_finished", {"tool": name, "status": tr.status})
        # every tool_result for this assistant turn travels in ONE user message
        messages.append({"role": "user", "content": results})
        run.checkpoint = {"system": system, "messages": trim_messages(messages), "failures": failures,
                          "alternatives": alternatives, "approvals": approvals, "changed": changed[-100:]}
        run.steps_used, run.used_model = steps, used_model
        try:
            await fenced(db, run.id, lease_token, checkpoint=run.checkpoint, steps_used=steps,
                         used_model=used_model, lease_until=_now() + timedelta(seconds=LEASE_SECONDS),
                         model=run.model)
        except StaleLease:
            # cancellation (or a newer worker) took the lease between steps. Completed effects stay
            # committed; this worker simply stops here (H05, spec §11.5 step 6).
            if await _refresh_status(db, mission_id) == "cancelled":
                return RunOutcome("cancelled", "cancelled", "Run cancelled; completed changes were kept.",
                                  changed, steps=steps, used_model=used_model, approvals=approvals)
            raise
        mission = await db.get(Mission, mission_id)
        add_update(mission, "running", f"Step {steps}: " + ", ".join(t.get("name", "?") for t in tool_uses))
        await db.commit()
        if stop is not None:
            return await _finish(db, mission, run, lease_token, stop)

    # clean end_turn: the run succeeded. The *case* may still be waiting (H12).
    mission_status = "waiting_approval" if approvals else "succeeded"
    summary = final_text or "Done."
    run.checkpoint = {"system": system, "messages": trim_messages(messages), "failures": failures,
                      "alternatives": alternatives, "approvals": approvals, "changed": changed[-100:]}
    return await _finish(db, mission, run, lease_token,
                         RunOutcome("succeeded", mission_status, summary, changed, steps=steps,
                                    used_model=used_model, approvals=approvals))


async def _control(db, mission: Mission, run: Run, name: str, payload: dict, changed: list,
                   approvals: list, steps: int, used_model: bool) -> tuple[RunOutcome | None, str]:
    model_cls, _ = CONTROL_TOOLS[name]
    try:
        inp = model_cls.model_validate(payload or {})
    except Exception as e:  # noqa: BLE001
        return None, json.dumps({"status": "error", "error": f"invalid input: {str(e)[:200]}"})
    db.add(RunStep(run_id=run.id, seq=steps, kind="wait" if name != "runtime_needs_information" else "decision",
                   tool_name=name, input=json.loads(json.dumps(payload, default=str)), output={}, ok=True,
                   started_at=_now(), finished_at=_now(), decision="allowed"))
    if name == "runtime_needs_information":
        needed = {"question": inp.question, "already_checked": inp.already_checked, "missing": inp.missing}
        return (RunOutcome("needs_information", "needs_information", inp.question, changed, needed_input=needed,
                           steps=steps, used_model=used_model, approvals=approvals),
                json.dumps({"status": "ok", "stopped": True, "asked": inp.question}))
    if name == "runtime_wait_until":
        mission.next_check_at = inp.next_check_at
        mission.waiting_on = inp.reason or "time"
        return (RunOutcome("waiting_until", "waiting_until", inp.reason or "Waiting until the next check.", changed,
                           steps=steps, used_model=used_model, approvals=approvals),
                json.dumps({"status": "ok", "stopped": True, "next_check_at": _iso(inp.next_check_at)}))
    mission.waiting_on = inp.waiting_on
    mission.next_check_at = inp.next_check_at or (_now() + timedelta(hours=24))
    return (RunOutcome("waiting_external", "waiting_external", inp.reason or f"Waiting on {inp.waiting_on}.", changed,
                       steps=steps, used_model=used_model, approvals=approvals),
            json.dumps({"status": "ok", "stopped": True, "waiting_on": inp.waiting_on}))


async def _finish(db, mission: Mission, run: Run, lease_token: str, out: RunOutcome) -> RunOutcome:
    now = _now()
    run_id, mission_id = run.id, mission.id
    values = dict(status=out.run_status, finished_at=now, steps_used=out.steps or run.steps_used,
                  used_model=out.used_model or run.used_model, error=out.error, lease_token=None,
                  result=json.loads(json.dumps(out.to_dict(), default=str)), checkpoint=run.checkpoint or {})
    try:
        await fenced(db, run_id, lease_token, **values)   # a stale worker cannot complete the run (H01)
    except StaleLease:
        current = await db.get(Run, run_id)
        if current is None or current.status not in ("cancelled", "succeeded", "failed"):
            raise                                   # a newer worker owns a still-running run: stay out of it
        out.run_status = current.status
        run = current
    m = await db.get(Mission, mission_id)
    if m is not None and m.status != "cancelled":
        m.status = out.mission_status
        m.result = {**(m.result or {}), "summary": out.summary, "changed": out.changed or [],
                    "approvals": out.approvals or [], "needed_input": out.needed_input,
                    "run_status": out.run_status, "error": out.error}
        if out.mission_status in TERMINAL_MISSION:
            m.finished_at = now
        if out.mission_status == "waiting_approval" and out.approvals:
            m.waiting_on = "owner approval"
        add_update(m, out.mission_status, out.summary[:500], run_id=run_id)
        m.bump(m.responsible_user_id)
        _emit_mission_event(db, m, run_id, out)
    await db.commit()
    return out


def _emit_mission_event(db, m: Mission, run_id: str, out: RunOutcome) -> None:
    from ..models.runtime import Event
    db.add(Event(type="mission.updated", aggregate_type="mission", aggregate_id=m.id, aggregate_version=m.version,
                 payload={"mission_id": m.id, "run_id": run_id, "status": m.status, "run_status": out.run_status,
                          "cursor": int(m.cursor or 0), "delegated_request_id": m.delegated_request_id},
                 correlation_id=m.correlation_id, causation_id=m.delegated_request_id, happened_at=_now(),
                 actor=dict(m.initiating_actor or {})))


async def _refresh_status(db, mission_id: str) -> str | None:
    return await db.scalar(select(Mission.status).where(Mission.id == mission_id))


async def _paused_reason(db, mission: Mission) -> str | None:
    from ..models.runtime import WorkflowControl
    keys = ["global", f"workflow:agent.{mission.role}"]
    if mission.thread_key:
        keys.append(f"thread:{mission.thread_key}")
    rows = (await db.execute(select(WorkflowControl).where(WorkflowControl.key.in_(keys)))).scalars().all()
    for r in rows:
        if r.paused:
            return r.key
    return None


async def _recovery_task(db, mission: Mission, run: Run, reason: str, detail: str) -> dict | None:
    """Escalate with a real task (spec §10.5 'create a clear recovery task'). Written as the system actor so
    escalation never fails because the mission's own grant is narrow."""
    vehicle_id = next((r.get("id") for r in (mission.entity_refs or []) if r.get("kind") == "vehicle"), None)
    ctx = CommandContext(db=db, actor=SYSTEM_ACTOR, channel="worker", correlation_id=mission.correlation_id,
                         mission_id=mission.id, run_id=run.id)
    try:
        res = await dispatch(ctx, "tasks.create", {
            "title": f"Take over: {mission.outcome[:120]}"[:200],
            "type": "operational", "priority": "high", "vehicle_id": vehicle_id,
            "owner_user_id": mission.responsible_user_id,
            "notes": f"{detail}\nReason: {reason}\nMission {mission.id} · run {run.id}",
        })
        return (res.data or {}).get("task") if isinstance(res.data, dict) else None
    except Exception as e:  # noqa: BLE001
        log.warning("recovery task could not be created: %s", e)
        return None


# ── jobs, sweeps and event resumption ────────────────────────────────────────
@jobs.job("mission.run")
async def _job_mission_run(jctx: jobs.JobContext, payload: dict) -> dict:
    db = jctx.db
    run_id, mission_id = payload.get("run_id"), payload.get("mission_id")
    run = await db.get(Run, run_id) if run_id else None
    mission = await db.get(Mission, mission_id) if mission_id else None
    if run is None or mission is None:
        return {"skipped": "missing"}
    if mission.status == "cancelled" or run.status in ("cancelled", "succeeded", "failed"):
        return {"skipped": run.status}
    claimed = await claim_run(db, run.id, jctx.worker_id)
    if claimed is None:
        return {"skipped": "lease held by another worker"}
    run, token = claimed
    mission = await db.get(Mission, mission_id)
    try:
        out = await execute_run(db, mission, run, token)
    except StaleLease as e:
        log.warning("%s", e)
        return {"stale": True}
    return {"run": run.id, "status": out.run_status, "mission_status": out.mission_status}


async def run_inline(db, mission: Mission, *, worker_id: str = "inline", on_event_cb=None) -> tuple[Run, RunOutcome]:
    """Run a mission now, in this request (chat and quick connector work). Same lease, fencing and budget."""
    run = await start_run(db, mission, trigger="inline", enqueue=False)
    await db.commit()
    claimed = await claim_run(db, run.id, worker_id)
    if claimed is None:
        return run, RunOutcome("failed", mission.status, "Another worker is already running this mission.", [],
                               error="lease held")
    run, token = claimed
    mission = await db.get(Mission, mission.id)
    out = await execute_run(db, mission, run, token, on_event_cb=on_event_cb)
    return run, out


async def resume(db, mission: Mission, *, reason: str, trigger: str = "resume") -> Run | None:
    """Queue a fresh run for a waiting mission (approval decided, next check due, client replied)."""
    if mission.status in TERMINAL_MISSION:
        return None
    add_update(mission, "resuming", reason)
    mission.next_check_at = None
    run = await start_run(db, mission, trigger=trigger)
    return run


@sweep("missions.resume_due", 60)
async def _resume_due(session_factory) -> int:
    """Deterministic follow-through: a waiting mission whose next check is due gets a new run (spec §12.4)."""
    n = 0
    async with session_factory() as db:
        rows = (await db.execute(select(Mission).where(
            Mission.status.in_(("waiting_until", "waiting_external")),
            Mission.next_check_at.is_not(None), Mission.next_check_at <= _now()).limit(50))).scalars().all()
        for m in rows:
            await resume(db, m, reason=f"next check due ({m.waiting_on or 'time'})", trigger="sweep")
            n += 1
        await db.commit()
    return n


@on_event("approval.changed")
async def _on_approval_changed(db, ev) -> None:
    """A mission waiting on an approval resumes when the owner decides it (spec §10.4 step 8)."""
    approval_id = ev.aggregate_id
    status = (ev.payload or {}).get("status")
    if not approval_id or status in (None, "pending"):
        return
    from ..models.runtime import Approval
    a = await db.get(Approval, approval_id)
    if a is None or not a.mission_id:
        return
    m = await db.get(Mission, a.mission_id)
    if m is None or m.status not in ("waiting_approval", "paused"):
        return
    await resume(db, m, reason=f"approval {approval_id[:8]} is {status}", trigger="approval")


@on_event("permission.revoked")
async def _on_permission_revoked(db, ev) -> None:
    """Revoking a grant pauses that person's open missions immediately (A03, H05)."""
    uid = (ev.payload or {}).get("user_id") or (ev.payload or {}).get("subject_id")
    if not uid:
        return
    rows = (await db.execute(select(Mission).where(Mission.responsible_user_id == uid,
                                                   Mission.status.in_(("open", "running", *WAITING_MISSION))))).scalars().all()
    for m in rows:
        m.status = "paused"
        m.paused_reason = "access changed; revalidate before resuming"
        add_update(m, "paused", "Access changed — this mission is paused until it is revalidated.")


# ── convenience for callers that only need a mission + result ────────────────
async def run_now(db, actor: Actor, *, outcome: str, **kw) -> tuple[Mission, Run, RunOutcome]:
    mission = await create_mission(db, actor, outcome=outcome, **kw)
    await db.commit()
    run, out = await run_inline(db, mission)
    mission = await db.get(Mission, mission.id)
    return mission, run, out
