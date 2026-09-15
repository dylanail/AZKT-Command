"""Manager chat: one entry point for web, Telegram and the external-agent connector (spec §7.4, §10.9).

`handle_message()` is the whole surface. It:

1. persists the turn on a thread shared by every channel (`{user_id}:{role}`),
2. answers simple lookups **deterministically**, with read tools and no model and no mission — so "what is
   holding up STK-0007", "today" and "overdue" keep working when the model is unavailable or capped (C10),
3. applies the explicit-context rule before any write: with a pinned vehicle "this one needs tires" becomes a
   reported issue and its task on *that* truck; with no pinned vehicle and two similar trucks it asks which
   one, and writes nothing (H13),
4. runs photo/voice intake through the intake commands and returns the compact saved result (§7.4 step 8),
5. otherwise opens a mission and runs it inline within its budget, returning the reply and the mission id.

Specialists (customer_sales, sourcing, logistics, shop, listings, finance) are the same runtime with a
different role prompt over the same records and the same policy — never a separate memory.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator, Callable

from sqlalchemy import select

from ..core.errors import Denied, ValidationFailed
from ..domain.access import can_see_costs
from ..domain.actors import Actor
from ..domain.commands import CommandContext
from ..domain.policy import has_perm
from ..models.runtime import ChatTurn, Mission
from . import runtime, tools
from .prompts import ROLES

log = logging.getLogger("azkt.agent.manager")

STOCK_RE = re.compile(r"\bstk[-\s]?0*(\d{1,8})\b", re.I)
FRAME_RE = re.compile(r"\b([A-Z]{2,4}[0-9]{2,3}[-\s]?[0-9]{5,8})\b", re.I)
HOLDING_RE = re.compile(r"\b(what'?s|what is|whats)\s+(holding\s+up|blocking|stopping)\b", re.I)
STATUS_RE = re.compile(r"\b(status|state|where is|how is|update on)\b", re.I)
TODAY_RE = re.compile(r"^\s*(what'?s\s+)?(on\s+)?today\??\s*$|^\s*today'?s?\s+(tasks|work|list)\b", re.I)
OVERDUE_RE = re.compile(r"\boverdue\b", re.I)
NEEDS_RE = re.compile(r"\bneeds?\s+(?P<what>[a-z][\w /'-]{2,60})", re.I)
THIS_ONE_RE = re.compile(r"\b(this one|this truck|this vehicle|this car|it)\b", re.I)

# "needs tires" -> "Replace tires"; keep the owner's words when no verb is obvious.
VERBS = {"tire": "Replace", "tires": "Replace", "detail": "Detail", "detailing": "Detail", "wash": "Wash",
         "oil": "Change", "brakes": "Replace", "battery": "Replace", "paint": "Repaint", "inspection": "Complete",
         "photos": "Take", "cleaning": "Clean", "a/c": "Diagnose", "ac": "Diagnose", "clutch": "Inspect"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def thread_key_for(actor: Actor, role: str) -> str:
    if actor.kind == "external":
        return f"client:{actor.client_id}:{role}"
    return f"{actor.user_id}:{role}"


async def store_turn(db, thread_key: str, role: str, content: str, *, channel: str = "web",
                     actor_user_id: str | None = None, mission_id: str | None = None, run_id: str | None = None,
                     context: dict | None = None, blocks: list | None = None, agent_role: str = "manager",
                     state: str = "final", telegram_message_id: int | None = None) -> ChatTurn:
    t = ChatTurn(thread_key=thread_key, role=role, content=(content or "")[:8000], blocks=list(blocks or []),
                 channel=channel, mission_id=mission_id, run_id=run_id, context=dict(context or {}),
                 actor_user_id=actor_user_id, agent_role=agent_role, state=state,
                 telegram_message_id=telegram_message_id, created_by=actor_user_id)
    db.add(t)
    await db.flush()
    return t


async def thread(db, actor: Actor, role: str = "manager", *, limit: int = 50) -> dict:
    # coerce exactly as handle_message does, or a chat sent with an unknown role would be stored on the
    # manager thread while this endpoint reported an empty one
    role = role if role in ROLES else "manager"
    key = thread_key_for(actor, role)
    rows = (await db.execute(select(ChatTurn).where(ChatTurn.thread_key == key)
                             .order_by(ChatTurn.created_at.desc()).limit(limit))).scalars().all()
    turns = [{"id": t.id, "role": t.role, "content": t.content, "blocks": list(t.blocks or []),
              "channel": t.channel, "mission_id": t.mission_id, "run_id": t.run_id, "context": dict(t.context or {}),
              "state": t.state, "at": t.created_at.isoformat() if t.created_at else None}
             for t in reversed(rows)]
    return {"thread_key": key, "role": role, "turns": turns, "count": len(turns)}


# ── the entry point ──────────────────────────────────────────────────────────
def chat_correlation(actor: Actor, request_id: str) -> str:
    """Idempotency key for one chat message. A dropped connection and a retried POST are the same
    logical request, so they must map to the same mission — never a second one (spec §12.1 step 3)."""
    return f"chat:{actor.key}:{request_id}"[:200]


async def _replay(db, mission: Mission, *, key: str, role: str, context: dict) -> dict:
    """The stored answer for a chat request that already ran. Nothing is re-run and nothing is re-written."""
    from ..models.runtime import Run
    run = (await db.execute(select(Run).where(Run.mission_id == mission.id)
                            .order_by(Run.created_at.desc()).limit(1))).scalars().first()
    r = dict(mission.result or {})
    approvals = r.get("approvals") or []
    return {"text": _with_review_links(r.get("summary") or "", approvals, mission.status),
            "status": mission.status, "mission_id": mission.id, "run_id": run.id if run is not None else None,
            "cursor": int(mission.cursor or 0), "changed": r.get("changed") or [], "approvals": approvals,
            "needed_input": r.get("needed_input"), "run_status": r.get("run_status"), "error": r.get("error"),
            "wrote": bool(r.get("changed")), "replayed": True, "thread_key": key, "role": role, "context": context,
            "blocks": [{"type": "mission", "id": mission.id, "status": mission.status}]}


def _with_review_links(summary: str, approvals: list, mission_status: str) -> str:
    if mission_status == "waiting_approval" and approvals:
        links = "; ".join(f"{a.get('title') or a.get('kind')} → {a.get('review_path')}" for a in approvals)
        return (summary + f"\n\nNothing was sent or ordered. Waiting for your signed-in review: {links}").strip()
    return summary


async def handle_message(db, actor: Actor, text: str, *, channel: str = "web", thread_key: str | None = None,
                         context: dict | None = None, attachments: list | None = None, role: str = "manager",
                         request_id: str | None = None, mission_kwargs: dict | None = None,
                         on_event_cb: Callable[..., Any] | None = None, store: bool = True) -> dict:
    """Answer one message. Returns {text, status, mission_id, run_id, cursor, changed, approvals, ...}."""
    role = role if role in ROLES else "manager"
    if not has_perm(actor, "agents.chat"):
        raise Denied("this person cannot use the AI Manager", perm="agents.chat")
    text = (text or "").strip()
    attachments = [a for a in (attachments or []) if a]
    if not text and not attachments:
        raise ValidationFailed("message or attachments required")
    key = thread_key or thread_key_for(actor, role)
    context = dict(context or {})

    if request_id and not (mission_kwargs or {}).get("correlation_id"):
        prior = (await db.execute(select(Mission).where(
            Mission.correlation_id == chat_correlation(actor, request_id)))).scalars().first()
        if prior is not None:
            if (prior.outcome or "")[:200] != (text or "Help with the attached evidence")[:200]:
                from ..core.errors import DomainError
                raise DomainError("request_id reused with a different message", code="idempotency_conflict")
            return await _replay(db, prior, key=key, role=role, context=context)

    if store:
        await store_turn(db, key, "user", text, channel=channel, actor_user_id=actor.user_id,
                         context=context, agent_role=role, blocks=[{"type": "attachment", "asset_id": a}
                                                                   for a in attachments])
        await db.commit()

    out = await _route(db, actor, text, channel=channel, thread_key=key, context=context,
                       attachments=attachments, role=role, request_id=request_id,
                       mission_kwargs=mission_kwargs, on_event_cb=on_event_cb)
    out.setdefault("thread_key", key)
    out.setdefault("role", role)
    out["context"] = context                      # pinned context is echoed back; navigation never changes it
    if store:
        await store_turn(db, key, "assistant", out.get("text") or "", channel=channel,
                         actor_user_id=actor.user_id, mission_id=out.get("mission_id"), run_id=out.get("run_id"),
                         context=context, agent_role=role,
                         blocks=[b for b in (out.get("blocks") or [])],
                         state="error" if out.get("status") == "failed" else "final")
        await db.commit()
    return out


async def _route(db, actor: Actor, text: str, *, channel: str, thread_key: str, context: dict,
                 attachments: list, role: str, request_id: str | None, mission_kwargs: dict | None,
                 on_event_cb) -> dict:
    ctx = CommandContext(db=db, actor=actor, channel=channel, request_id=request_id,
                         correlation_id=(mission_kwargs or {}).get("correlation_id"))

    if attachments or (context.get("intake_id")):
        return await _intake(db, ctx, actor, text, context=context, attachments=attachments,
                             channel=channel, request_id=request_id)

    issue = await _issue_report(db, ctx, actor, text, context=context)
    if issue is not None:
        return issue

    quick = await _quick_lookup(db, ctx, actor, text, context=context)
    if quick is not None:
        return quick

    return await _mission(db, actor, text, channel=channel, thread_key=thread_key, context=context,
                          role=role, mission_kwargs=mission_kwargs, on_event_cb=on_event_cb,
                          request_id=request_id)


# ── deterministic fast paths ─────────────────────────────────────────────────
async def _vehicle_from_text(db, actor: Actor, text: str, context: dict) -> tuple[Any, list]:
    """Return (vehicle_row_or_None, candidates). Never picks between two similar trucks."""
    from ..models.vehicles import Vehicle
    from ..services import matching
    from ..domain.access import visible_vehicle_ids
    limit = await visible_vehicle_ids(db, actor)

    async def visible(v) -> bool:
        return v is not None and (limit is None or v.id in limit)

    if context.get("vehicle_id"):
        v = await db.get(Vehicle, context["vehicle_id"])
        if await visible(v):
            return v, []
    res = await matching.resolve_vehicle(db, text=text)
    if res.vehicle_id:
        v = await db.get(Vehicle, res.vehicle_id)
        if await visible(v):
            return v, []
    m = STOCK_RE.search(text or "")
    if m:
        stock = f"STK-{int(m.group(1)):04d}"
        v = (await db.execute(select(Vehicle).where(Vehicle.stock_no == stock))).scalars().first()
        if await visible(v):
            return v, []
    # free-text make/model: gather candidates but never choose
    words = [w for w in re.findall(r"[A-Za-z]{3,}", text or "") if w.lower() not in
             ("this", "that", "the", "needs", "need", "truck", "vehicle", "one", "please", "and", "with", "for")]
    cands: list = []
    for w in words[:4]:
        like = f"%{w}%"
        rows = (await db.execute(select(Vehicle).where(Vehicle.archived_at.is_(None))
                                 .where((Vehicle.make.ilike(like)) | (Vehicle.model.ilike(like))
                                        | (Vehicle.title.ilike(like)) | (Vehicle.color.ilike(like)))
                                 .order_by(Vehicle.created_at.desc()).limit(25))).scalars().all()
        for v in rows:
            if (limit is None or v.id in limit) and all(c.id != v.id for c in cands):
                cands.append(v)
    if len(cands) == 1:
        return cands[0], []
    return None, cands


def _verb_title(what: str) -> str:
    what = " ".join(what.split()).strip(" .!,")
    head = what.split()[0].lower() if what.split() else what.lower()
    verb = VERBS.get(head) or VERBS.get(what.lower())
    if verb:
        return f"{verb} {what}"[:200]
    return f"Handle: {what}"[:200]


async def _issue_report(db, ctx: CommandContext, actor: Actor, text: str, *, context: dict) -> dict | None:
    """H13: a scoped “this one needs tires” writes; an unscoped one asks first."""
    m = NEEDS_RE.search(text or "")
    if not m:
        return None
    what = m.group("what")
    pinned = bool(context.get("vehicle_id"))
    if not pinned and not THIS_ONE_RE.search(text or "") and not STOCK_RE.search(text or ""):
        # not obviously a report about one truck; let the mission handle it
        if not re.match(r"^\s*needs\b", text or "", re.I):
            return None
    v, cands = await _vehicle_from_text(db, actor, text, context)
    if v is None:
        listed = ", ".join(f"{c.stock_no or c.id[:8]} ({(c.make or '').strip()} {(c.model or '').strip()}".strip() + ")"
                           for c in cands[:5])
        question = ("Which truck do you mean? " + (f"I can see {listed}." if cands else
                    "I could not match a vehicle from that message.")
                    + " Tell me the stock number (or open the vehicle and say it again) and I will record it.")
        return {"text": question, "status": "needs_information", "mission_id": None, "run_id": None,
                "cursor": 0, "changed": [], "approvals": [],
                "needed_input": {"question": "which vehicle?",
                                 "candidates": [{"id": c.id, "stock_no": c.stock_no,
                                                 "title": f"{c.make or ''} {c.model or ''}".strip()} for c in cands[:5]]},
                "fast_path": "ask_which_vehicle", "wrote": False}
    title = _verb_title(what)
    res = await tools.execute(ctx, "shop_create_issue", {
        "vehicle_id": v.id, "title": title, "detail": text[:2000], "source_kind": "owner_reported",
        "create_task": True, "task_title": title, "priority": "normal"})
    if res.status != "ok":
        return {"text": f"I could not record that on {v.stock_no or v.id[:8]}: {res.error or res.status}.",
                "status": "blocked", "mission_id": None, "run_id": None, "cursor": 0, "changed": [],
                "approvals": [], "fast_path": "issue_report", "wrote": False,
                "reasons": res.decision.get("reasons", [])}
    await db.commit()
    data = res.data or {}
    issue = data.get("issue") or {}
    task = data.get("task") or {}
    label = f"{(v.make or '').strip()} {(v.model or '').strip()}".strip() or "vehicle"
    return {"text": f"Recorded on {label} · {v.stock_no or v.id[:8]}: “{title}” as a reported issue"
                    + (f" with the task “{task.get('title', title)}”." if task else ".")
                    + " It is owner-reported, not inspected.",
            "status": "answered", "mission_id": None, "run_id": None, "cursor": 0,
            "changed": res.changed, "approvals": [], "fast_path": "issue_report", "wrote": True,
            "blocks": [{"type": "record", "kind": "vehicle", "id": v.id, "label": v.stock_no},
                       {"type": "record", "kind": "recon_issue", "id": issue.get("id")}]}


async def _quick_lookup(db, ctx: CommandContext, actor: Actor, text: str, *, context: dict) -> dict | None:
    t = (text or "").strip()
    if TODAY_RE.search(t):
        return await _task_answer(db, ctx, actor, bucket="upcoming", label="today")
    if OVERDUE_RE.search(t) and len(t) < 120:
        return await _task_answer(db, ctx, actor, bucket="overdue", label="overdue")
    wants_status = bool(HOLDING_RE.search(t) or STATUS_RE.search(t) or STOCK_RE.search(t))
    if not wants_status:
        return None
    v, cands = await _vehicle_from_text(db, actor, t, context)
    if v is None:
        if not cands:
            return None
        listed = ", ".join(f"{c.stock_no or c.id[:8]}" for c in cands[:5])
        return {"text": f"Several trucks match that: {listed}. Which one?", "status": "needs_information",
                "mission_id": None, "run_id": None, "cursor": 0, "changed": [], "approvals": [],
                "fast_path": "ask_which_vehicle", "wrote": False,
                "needed_input": {"question": "which vehicle?",
                                 "candidates": [{"id": c.id, "stock_no": c.stock_no} for c in cands[:5]]}}
    res = await tools.execute(ctx, "vehicles_get_context", {"vehicle_id": v.id})
    if res.status != "ok":
        return {"text": f"I cannot open that vehicle: {res.error or 'not accessible'}.", "status": "blocked",
                "mission_id": None, "run_id": None, "cursor": 0, "changed": [], "approvals": [],
                "fast_path": "vehicle_status", "wrote": False}
    return {"text": _status_text(res.data, holding=bool(HOLDING_RE.search(t)), money=can_see_costs(actor)),
            "status": "answered", "mission_id": None, "run_id": None, "cursor": 0, "changed": [],
            "approvals": [], "fast_path": "vehicle_status", "wrote": False,
            "blocks": [{"type": "record", "kind": "vehicle", "id": v.id, "label": v.stock_no}]}


def _status_text(detail: dict, *, holding: bool, money: bool) -> str:
    v = detail.get("vehicle") or {}
    tabs = detail.get("tabs") or {}
    ov = tabs.get("overview") or {}
    work = tabs.get("work") or {}
    health = ov.get("health") or {}
    ident = ov.get("identity") or {}
    states = ov.get("states") or {}
    open_tasks = [t for t in (work.get("tasks") or [])
                  if t.get("status") in ("open", "in_progress", "blocked", "waiting", "awaiting_verification")]
    open_issues = [i for i in (work.get("recon_issues") or []) if i.get("status") != "resolved"]
    lines = [f"{ident.get('title') or v.get('title') or 'Vehicle'} · {ident.get('stock_no') or v.get('stock_no') or ''}".strip()]
    if holding:
        lines.append(f"Holding it up: {health.get('reason') or 'nothing recorded as a blocker'}.")
    lines.append(f"Health: {health.get('health') or 'unknown'}"
                 + (f" — {health.get('reason')}" if health.get("reason") and not holding else ""))
    if health.get("next_action"):
        lines.append(f"Next: {health['next_action']}" + (f" (due {health.get('due_at')})" if health.get("due_at") else ""))
    lines.append("States: " + " · ".join(f"{k}={val}" for k, val in states.items()))
    lines.append(f"Open work: {len(open_tasks)} task(s), {len(open_issues)} recon issue(s).")
    if open_tasks[:3]:
        lines.append("Top: " + "; ".join(t.get("title", "") for t in open_tasks[:3]))
    if ident.get("missing_identity_fields"):
        lines.append("Intake incomplete — missing: " + ", ".join(ident["missing_identity_fields"]))
    if money:
        mv = tabs.get("money") or {}
        if not mv.get("money_hidden"):
            lines.append("Recorded costs: " + (", ".join(f"{k} {val}" for k, val in (mv.get("cost_total") or {}).items())
                                               or "none recorded") + f" (coverage: {mv.get('coverage')}).")
    lines.append("Source: the AZKT vehicle card.")
    return "\n".join(x for x in lines if x)


async def _task_answer(db, ctx: CommandContext, actor: Actor, *, bucket: str, label: str) -> dict:
    res = await tools.execute(ctx, "tasks_list", {"view": "all", "bucket": bucket, "limit": 25})
    if res.status != "ok":
        return {"text": f"I cannot read the task list: {res.error}.", "status": "blocked", "mission_id": None,
                "run_id": None, "cursor": 0, "changed": [], "approvals": [], "fast_path": "tasks", "wrote": False}
    items = (res.data or {}).get("items", [])
    now = _now()
    if bucket == "upcoming":
        items = [t for t in items if not t.get("due_at") or
                 (t.get("due_at") and t["due_at"][:10] <= (now + timedelta(days=1)).isoformat()[:10])]
    if not items:
        body = f"Nothing {label}." if bucket != "upcoming" else "Nothing due today."
    else:
        body = f"{len(items)} {label}:\n" + "\n".join(
            f"· {t.get('title')}" + (f" — due {t.get('local_due')}" if t.get("local_due") else "")
            + (f" ({t.get('status')})" if t.get("status") not in ("open",) else "") for t in items[:12])
    return {"text": body + "\nSource: the shared Tasks list.", "status": "answered", "mission_id": None,
            "run_id": None, "cursor": 0, "changed": [], "approvals": [], "fast_path": f"tasks:{bucket}",
            "wrote": False}


# ── intake (spec §7.4) ───────────────────────────────────────────────────────
async def _intake(db, ctx: CommandContext, actor: Actor, text: str, *, context: dict, attachments: list,
                  channel: str, request_id: str | None) -> dict:
    intake_id = context.get("intake_id")
    if not intake_id:
        mode = "existing" if context.get("vehicle_id") else ("new" if re.search(r"\bnew\b", text or "", re.I) else "find")
        start = await tools.execute(ctx, "intake_start", {
            "target_mode": mode, "vehicle_id": context.get("vehicle_id"),
            "channel": "telegram" if channel == "telegram" else ("mcp" if channel == "mcp" else "web"),
            "request_key": request_id, "text": text or None, "asset_ids": list(attachments)})
        if start.status != "ok":
            return _blocked("I could not start the intake", start)
        intake_id = ((start.data or {}).get("intake") or {}).get("id")
        await db.commit()
    elif attachments:
        add = await tools.execute(ctx, "intake_add_assets", {"intake_id": intake_id, "asset_ids": list(attachments)})
        if add.status != "ok":
            return _blocked("I could not attach those files", add)
        await db.commit()
    if text:
        note = await tools.execute(ctx, "intake_add_note", {"intake_id": intake_id, "text": text[:8000]})
        if note.status != "ok":
            return _blocked("I could not save that note", note)
        await db.commit()
    await tools.execute(ctx, "intake_analyze", {"intake_id": intake_id})
    await db.commit()
    applied = await tools.execute(ctx, "intake_apply", {"intake_id": intake_id})
    await db.commit()
    if applied.status != "ok":
        return _blocked("I saved your photos and notes, but could not apply them yet", applied,
                        extra={"intake_id": intake_id})
    d = applied.data or {}
    if d.get("decision") == "Blocked":
        # material ambiguity: the evidence is saved, but nothing is written to a truck yet (§7.4 step 4, I03)
        choice = d.get("choice") or {}
        options = choice.get("candidates") or choice.get("options") or []
        listed = ", ".join(str(c.get("stock_no") or c.get("vehicle_id") or c.get("id")) for c in options[:5])
        return {"text": ("Your photos and notes are saved. Which truck is this? "
                         + (f"Closest matches: {listed}." if listed else
                            (choice.get("reason") or "I could not identify the vehicle from the evidence."))
                         + " Nothing was written to a vehicle yet."),
                "status": "needs_information", "mission_id": None, "run_id": None, "cursor": 0,
                "changed": applied.changed, "approvals": [], "fast_path": "intake", "wrote": False,
                "intake_id": intake_id,
                "needed_input": {"question": "which vehicle?", "candidates": options,
                                 "reasons": d.get("reasons") or []},
                "blocks": [{"type": "intake", "id": intake_id}]}
    r = d.get("result") or {}
    vehicle = d.get("vehicle") or {}
    bullets = r.get("condition_bullets") or []
    created = r.get("tasks_created") or []
    updated = r.get("tasks_updated") or []
    failed = r.get("photos_failed") or []
    missing = r.get("missing") or []
    lines = [("Created " if r.get("created") else "Updated ")
             + f"{r.get('title') or vehicle.get('title') or 'the vehicle card'} · {r.get('stock_no') or ''}".strip()]
    lines.append(f"Photos saved: {r.get('photos_saved', 0)}"
                 + (f" · {len(failed)} failed (still retryable)" if failed else ""))
    if bullets:
        lines.append("Condition at intake:\n" + "\n".join(
            f"· {b.get('text') if isinstance(b, dict) else b}"
            + (f" ({b.get('source')})" if isinstance(b, dict) and b.get("source") else "") for b in bullets[:8]))
    if created or updated:
        lines.append("Tasks: " + "; ".join((t.get("title") if isinstance(t, dict) else str(t))
                                           for t in [*created, *updated][:8]))
    if missing:
        lines.append("Still missing: " + ", ".join(missing) + " — intake incomplete; nothing was invented.")
    if r.get("card_path"):
        lines.append(f"Card: {r['card_path']}")
    return {"text": "\n".join(lines), "status": "answered", "mission_id": None, "run_id": None, "cursor": 0,
            "changed": applied.changed, "approvals": [], "fast_path": "intake",
            "wrote": True, "intake_id": intake_id, "intake_result": r,
            "blocks": [{"type": "record", "kind": "vehicle", "id": vehicle.get("id"), "label": r.get("stock_no")},
                       {"type": "intake", "id": intake_id}]}


def _blocked(prefix: str, res: tools.ToolResult, *, extra: dict | None = None) -> dict:
    out = {"text": f"{prefix}: {res.error or res.status}.", "status": "blocked", "mission_id": None, "run_id": None,
           "cursor": 0, "changed": [], "approvals": [], "wrote": False,
           "reasons": res.decision.get("reasons", [])}
    out.update(extra or {})
    return out


# ── mission path ─────────────────────────────────────────────────────────────
async def _mission(db, actor: Actor, text: str, *, channel: str, thread_key: str, context: dict, role: str,
                   mission_kwargs: dict | None, on_event_cb, request_id: str | None = None) -> dict:
    kw = dict(mission_kwargs or {})
    if request_id and not kw.get("correlation_id"):
        kw["correlation_id"] = chat_correlation(actor, request_id)
    entity_refs = list(kw.pop("entity_refs", []) or [])
    if context.get("vehicle_id") and not any(r.get("id") == context["vehicle_id"] for r in entity_refs):
        entity_refs.append({"kind": "vehicle", "id": context["vehicle_id"], "label": context.get("label")})
    if context.get("case_id"):
        entity_refs.append({"kind": "case", "id": context["case_id"]})
    mission = await runtime.create_mission(
        db, actor, outcome=text or "Help with the attached evidence",
        trigger=kw.pop("trigger", "telegram" if channel == "telegram" else "chat"), channel=channel, role=role,
        entity_refs=entity_refs, thread_key=thread_key, context=context, **kw)
    await db.commit()
    run, out = await runtime.run_inline(db, mission, on_event_cb=on_event_cb)
    mission = await db.get(Mission, mission.id)
    reply = _with_review_links(out.summary, out.approvals or [], out.mission_status)
    return {"text": reply, "status": mission.status, "mission_id": mission.id, "run_id": run.id,
            "cursor": int(mission.cursor or 0), "changed": out.changed or [], "approvals": out.approvals or [],
            "needed_input": out.needed_input, "run_status": out.run_status, "used_model": out.used_model,
            "error": out.error, "wrote": bool(out.changed),
            "blocks": [{"type": "mission", "id": mission.id, "status": mission.status}]}


# ── streaming (web) ──────────────────────────────────────────────────────────
async def stream_message(db, actor: Actor, text: str, **kw) -> AsyncGenerator[dict, None]:
    """SSE events: text, tool_started, tool_finished, needs_review, done. Progress describes real work."""
    queue: asyncio.Queue = asyncio.Queue()

    async def cb(kind: str, payload: dict) -> None:
        await queue.put({"event": kind, "data": payload})

    task = asyncio.create_task(handle_message(db, actor, text, on_event_cb=cb, **kw))
    yield {"event": "start", "data": {"role": kw.get("role", "manager")}}
    try:
        while not task.done() or not queue.empty():
            try:
                yield await asyncio.wait_for(queue.get(), timeout=0.15)
            except asyncio.TimeoutError:
                continue
        result = task.result()
        yield {"event": "done", "data": result}
    except Exception as e:  # noqa: BLE001
        log.exception("manager stream failed")
        yield {"event": "error", "data": {"error": f"{type(e).__name__}: {str(e)[:300]}"}}
    finally:
        # A browser that closes the stream must not leave handle_message running on a session the caller
        # is about to close. The mission itself is durable and keeps its completed effects.
        if not task.done():
            task.cancel()
        with contextlib.suppress(BaseException):
            await task


# ── status (per role) ────────────────────────────────────────────────────────
async def status(db, actor: Actor) -> dict:
    """Per-role health and what is actually running. No fabricated 'live thinking'."""
    from ..adapters.model import available as model_available, budget_state
    st = await budget_state(db)
    roles = []
    for r in ROLES:
        rows = (await db.execute(select(Mission).where(Mission.role == r,
                                                       Mission.status.in_(("open", "running"))))).scalars().all()
        waiting = (await db.execute(select(Mission).where(
            Mission.role == r, Mission.status.in_(("waiting_approval", "waiting_external", "waiting_until",
                                                   "needs_information", "paused"))))).scalars().all()
        if actor.kind in ("user", "agent") and actor.role != "owner":
            rows = [m for m in rows if m.responsible_user_id == actor.user_id]
            waiting = [m for m in waiting if m.responsible_user_id == actor.user_id]
        roles.append({
            "role": r,
            "health": "ok" if st["api_key_present"] and st["configured"] and not st["over_cap"]
                      else ("unavailable" if not st["api_key_present"] else
                            ("setup_blocked" if not st["configured"] else "over_budget")),
            "doing": [{"mission_id": m.id, "outcome": m.outcome[:160], "status": m.status,
                       "started_at": m.created_at.isoformat() if m.created_at else None} for m in rows[:10]],
            "waiting": [{"mission_id": m.id, "status": m.status, "waiting_on": m.waiting_on,
                         "next_check_at": m.next_check_at.isoformat() if m.next_check_at else None}
                        for m in waiting[:10]],
            "counts": {"running": len(rows), "waiting": len(waiting)},
        })
    return {"model": {"available": model_available(), "budget_configured": st["configured"],
                      "over_cap": st["over_cap"], "day_usd": st["day_usd"], "daily_cap_usd": st["daily_cap_usd"]},
            "deterministic_paths": ["vehicle status", "today", "overdue", "photo/voice intake", "scoped issue report"],
            "roles": roles, "as_of": _now().isoformat()}
