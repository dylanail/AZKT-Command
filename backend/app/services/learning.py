"""Learning from corrections (spec §4.4, §9.4, G11).

An owner/manager edit of a draft is classified into scoped lessons:
  style      shortened / reworded text with the same facts -> KnowledgeItem kind=style (usable only as an example,
             never as policy), scoped to the workflow;
  factual    a number / date / amount / availability word changed -> kind=lesson scoped to the entity
             (vehicle or contact) referencing evidence; without evidence it stays proposed and cannot be approved;
  concession one-time customer favour -> kind=exception scoped to that contact (owner-approved, spec §9.4);
  policy     new general rule ("from now on", "all customers", "never") -> kind=policy proposal requiring owner
             acceptance. No permission ever changes here.
Classification is deterministic (diff heuristics); a model classification is used only as a second opinion
when the model adapter is available and never required. Every edit records a WorkflowOutcome
(accepted | style_edit | factual_edit | declined | critical_error | unanswered) with review_seconds so promotion
proposals are evidence-backed (spec §1.3, §9.5).
"""
from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy import select

from ..core.errors import ValidationFailed
from ..core.ids import stable_hash
from ..domain.commands import CommandContext, command
from ..models.knowledge import WorkflowOutcome
from .knowledge import KnowledgeProposeIn, propose_item, serialize_item

log = logging.getLogger("azkt.learning")

OUTCOMES = ("accepted", "style_edit", "factual_edit", "declined", "critical_error", "unanswered")
SEVERITIES = ("normal", "major", "critical")

POLICY_RE = re.compile(r"\b(from now on|going forward|always|never|all customers|every customer|our policy|we require|"
                       r"as a rule|standard(?:ly)?|by default|in all cases|no longer|policy)\b", re.I)
CONCESSION_RE = re.compile(r"\b(waive[sd]?|as a one[- ]time|just for you|for you specifically|this once|as an exception|"
                           r"exception(?:ally)?|discount|free of charge|no charge|on the house|we'll cover|i'll cover|"
                           r"extend(?:ed)? (?:the|your) (?:hold|deadline|reservation)|honou?r the)\b", re.I)
AVAILABILITY_WORDS = ("available", "reserved", "sold", "unavailable", "on hold", "in stock", "pending", "delivered", "shipped",
                      "arrived", "ready", "not ready")
FACT_RE = re.compile(r"(?:\$\s?\d[\d,]*(?:\.\d+)?|¥\s?\d[\d,]*|\b\d[\d,]*(?:\.\d+)?\s?(?:usd|jpy|yen|dollars|km|miles|days?|weeks?|hours?)\b|"
                     r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b(?:mon|tues?|wed(?:nes)?|thurs?|fri|sat(?:ur)?|sun)day\b|"
                     r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2}\b|\bSTK-\d{3,7}\b|\b\d{4}\b)", re.I)


@dataclass
class Lesson:
    classification: str  # style|factual|concession|policy
    kind: str            # style|lesson|exception|policy
    title: str
    content: str
    scope: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)
    diff: dict = field(default_factory=dict)
    needs_evidence: bool = False
    usable_as: list = field(default_factory=list)


def _sentences(text: str) -> list[str]:
    t = (text or "").replace("\r\n", "\n")
    parts = re.split(r"(?<=[.!?。])\s+|\n{2,}|\n(?=[-•*]\s)|\n", t)
    return [p.strip() for p in parts if p and p.strip()]


def _facts(text: str) -> set[str]:
    out = {m.group(0).lower().replace(" ", "") for m in FACT_RE.finditer(text or "")}
    low = (text or "").lower()
    out |= {w for w in AVAILABILITY_WORDS if re.search(r"\b" + re.escape(w) + r"\b", low)}
    return out


def diff_edit(before: str, after: str) -> dict:
    """Sentence-level diff: removed, added, replaced pairs (deterministic)."""
    a, b = _sentences(before), _sentences(after)
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    removed: list[str] = []
    added: list[str] = []
    replaced: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "delete":
            removed += a[i1:i2]
        elif tag == "insert":
            added += b[j1:j2]
        elif tag == "replace":
            olds, news = a[i1:i2], b[j1:j2]
            for k in range(max(len(olds), len(news))):
                o = olds[k] if k < len(olds) else None
                n = news[k] if k < len(news) else None
                if o and n:
                    replaced.append((o, n))
                elif o:
                    removed.append(o)
                elif n:
                    added.append(n)
    return {"removed": removed, "added": added, "replaced": [{"before": o, "after": n} for o, n in replaced],
            "len_before": len(before or ""), "len_after": len(after or ""),
            "ratio": round(len(after or "") / len(before), 3) if before else None}


def classify_edit(before: str, after: str, context: dict) -> list[Lesson]:
    """Deterministic classification into scoped lessons. One edit can yield several lessons (G11)."""
    d = diff_edit(before, after)
    contact_id = context.get("contact_id")
    vehicle_id = context.get("vehicle_id")
    workflow = context.get("workflow_key") or "reply"
    declared = context.get("declared_kind")  # explicit hint from the editor: style|factual|concession|policy
    evidence = list(context.get("evidence") or [])
    entity_scope = {"vehicle_id": vehicle_id} if vehicle_id else ({"contact_id": contact_id} if contact_id else {})
    lessons: list[Lesson] = []
    style_bits: list[str] = []
    seen_policy: set[str] = set()

    def add_policy(sentence: str) -> None:
        key = sentence.lower()
        if key in seen_policy:
            return
        seen_policy.add(key)
        lessons.append(Lesson("policy", "policy", f"Proposed policy: {sentence[:80]}", sentence, scope={},
                              diff={"added": [sentence]}, usable_as=["policy"]))

    def add_concession(sentence: str) -> None:
        if not contact_id:
            # a concession without a contact cannot be scoped; keep it as a lesson needing review rather than a policy
            lessons.append(Lesson("factual", "lesson", f"Unscoped concession noted: {sentence[:80]}", sentence, scope=entity_scope,
                                  evidence=evidence, diff={"added": [sentence]}, needs_evidence=not evidence, usable_as=["fact"]))
            return
        lessons.append(Lesson("concession", "exception", f"Customer exception: {sentence[:80]}", sentence,
                              scope={"contact_id": contact_id}, diff={"added": [sentence]}, usable_as=["exception"]))

    def add_factual(old: str | None, new: str, changed: set[str]) -> None:
        lessons.append(Lesson("factual", "lesson", f"Fact corrected: {', '.join(sorted(changed))[:80]}", new, scope=entity_scope,
                              evidence=evidence, diff={"before": old, "after": new, "changed": sorted(changed)},
                              needs_evidence=not evidence, usable_as=["fact"]))

    for pair in d["replaced"]:
        old, new = pair["before"], pair["after"]
        f_old, f_new = _facts(old), _facts(new)
        changed = (f_old ^ f_new)
        if declared == "policy" or (POLICY_RE.search(new) and not POLICY_RE.search(old)):
            add_policy(new)
        elif declared == "concession" or (CONCESSION_RE.search(new) and not CONCESSION_RE.search(old)):
            add_concession(new)
        elif changed and (f_new - f_old or declared == "factual"):
            add_factual(old, new, changed)
        else:
            style_bits.append(new)
        if len(new) < 0.75 * len(old) and new not in style_bits:
            style_bits.append(new)  # the same sentence was also tightened: style evidence alongside the primary lesson
    for s in d["added"]:
        if declared == "policy" or POLICY_RE.search(s):
            add_policy(s)
        elif declared == "concession" or CONCESSION_RE.search(s):
            add_concession(s)
        elif declared == "factual" or (_facts(s) and (_facts(s) - _facts(before))):
            add_factual(None, s, _facts(s) - _facts(before))
        else:
            style_bits.append(s)
    shortened = d["ratio"] is not None and d["ratio"] < 0.85
    # style evidence comes from the style-only segments: removed sentences and reworded sentences with the same facts.
    # Facts dropped by a removal are recorded for the reviewer; a style example never carries policy or fact authority.
    dropped = set().union(*[_facts(r) for r in d["removed"]]) - _facts(after) if d["removed"] else set()
    if declared == "style" or d["removed"] or style_bits:
        lessons.append(Lesson("style", "style", "Style preference: " + ("shorter reply" if shortened else "rewording"),
                              (after or "").strip(), scope={"workflow": workflow},
                              diff={"removed": d["removed"][:5], "reworded": style_bits[:5], "ratio": d["ratio"],
                                    "dropped_facts": sorted(dropped)}, usable_as=["example"]))
    return lessons


async def model_second_opinion(db, before: str, after: str) -> dict | None:
    """Optional: ask the model adapter to classify the diff. Never required; a failure returns None."""
    try:
        from ..adapters.model import ModelClient, ModelUnavailable, available
    except Exception:  # noqa: BLE001
        return None
    if not available():
        return None

    class EditClassification(BaseModel):
        kinds: list[str]
        summary: str

    try:
        client = ModelClient(db, workflow="learning")
        res = await client.extract(EditClassification, system="Classify a draft edit into any of: style, factual, concession, policy. "
                                   "Return only the categories the diff supports.", user_content=f"BEFORE:\n{before}\n\nAFTER:\n{after}")
        return {"kinds": [k for k in res.kinds if k in ("style", "factual", "concession", "policy")], "summary": res.summary}
    except ModelUnavailable:
        return None
    except Exception as e:  # noqa: BLE001
        log.info("model classification skipped: %s", e)
        return None


def outcome_for(lessons: list[Lesson], before: str, after: str, override: str | None) -> str:
    if override:
        return override
    if (before or "").strip() == (after or "").strip():
        return "accepted"
    if any(lesson.classification in ("factual", "concession", "policy") for lesson in lessons):
        return "factual_edit"
    return "style_edit"


def serialize_outcome(o: WorkflowOutcome) -> dict:
    return {"id": o.id, "workflow_key": o.workflow_key, "entity_kind": o.entity_kind, "entity_id": o.entity_id, "outcome": o.outcome,
            "severity": o.severity, "review_seconds": o.review_seconds, "at": o.at.isoformat() if o.at else None,
            "classification": list(o.classification or []), "details": dict(o.details or {}), "actor_user_id": o.actor_user_id,
            "source_ref": o.source_ref}


async def record_outcome(ctx: CommandContext, *, workflow_key: str, outcome: str, entity_kind: str | None = None,
                         entity_id: str | None = None, review_seconds: int | None = None, severity: str = "normal",
                         classification: list[str] | None = None, details: dict | None = None, at: datetime | None = None,
                         source_ref: str | None = None, dedupe_key: str | None = None) -> tuple[WorkflowOutcome, bool]:
    if outcome not in OUTCOMES:
        raise ValidationFailed(f"outcome must be one of {OUTCOMES}")
    if severity not in SEVERITIES:
        raise ValidationFailed(f"severity must be one of {SEVERITIES}")
    key = dedupe_key or stable_hash({"w": workflow_key, "k": entity_kind, "i": entity_id, "o": outcome, "s": source_ref,
                                     "at": (at or ctx.now).isoformat()})[:32]
    existing = (await ctx.db.execute(select(WorkflowOutcome).where(WorkflowOutcome.dedupe_key == key))).scalars().first()
    if existing is not None:
        return existing, False
    row = WorkflowOutcome(workflow_key=workflow_key, entity_kind=entity_kind, entity_id=entity_id, outcome=outcome,
                          review_seconds=review_seconds, at=at or ctx.now, details=details or {}, dedupe_key=key, severity=severity,
                          actor_user_id=ctx.actor.user_id, classification=list(classification or []), source_ref=source_ref,
                          created_by=ctx.actor.user_id)
    ctx.db.add(row)
    await ctx.db.flush()
    ctx.emit("workflow.outcome_recorded", aggregate_type="workflow", aggregate_id=workflow_key,
             payload={"outcome": outcome, "severity": severity, "entity_kind": entity_kind, "entity_id": entity_id})
    return row, True


class LearnFromEditIn(BaseModel):
    draft_before: str = ""
    draft_after: str = ""
    workflow_key: str = "reply"
    entity_kind: str | None = None  # draft|message|conversation
    entity_id: str | None = None
    contact_id: str | None = None
    vehicle_id: str | None = None
    conversation_id: str | None = None
    draft_version: int | None = None
    review_seconds: int | None = Field(default=None, ge=0)
    evidence: list = Field(default_factory=list)  # [{kind, ref, label}] for factual corrections
    declared_kind: str | None = None  # style|factual|concession|policy (editor's explicit hint)
    outcome: str | None = None  # override: declined|critical_error|unanswered
    use_model: bool = False
    dedupe_key: str | None = None


@command("learning.record_edit", input=LearnFromEditIn, perm="inbox.draft", action_class="internal", workflow_key="learning",
         description="Classify a draft edit into scoped lessons (style/fact/exception/policy proposals) and record the "
                     "workflow outcome. Proposals only; nothing here changes a permission.")
async def learning_record_edit(ctx: CommandContext, inp: LearnFromEditIn) -> dict:
    if inp.declared_kind is not None and inp.declared_kind not in ("style", "factual", "concession", "policy"):
        raise ValidationFailed("declared_kind must be style|factual|concession|policy")
    if inp.outcome is not None and inp.outcome not in OUTCOMES:
        raise ValidationFailed(f"outcome must be one of {OUTCOMES}")
    context = {"contact_id": inp.contact_id, "vehicle_id": inp.vehicle_id, "workflow_key": inp.workflow_key,
               "declared_kind": inp.declared_kind, "evidence": inp.evidence}
    lessons = classify_edit(inp.draft_before, inp.draft_after, context)
    method = "heuristic"
    opinion = await model_second_opinion(ctx.db, inp.draft_before, inp.draft_after) if inp.use_model else None
    if opinion:
        method = "heuristic+model"
    base_key = inp.dedupe_key or stable_hash({"w": inp.workflow_key, "e": inp.entity_id, "v": inp.draft_version,
                                              "b": inp.draft_before, "a": inp.draft_after})[:24]
    items = []
    for i, lesson in enumerate(lessons):
        pin = KnowledgeProposeIn(kind=lesson.kind, title=lesson.title, content=lesson.content, scope=lesson.scope,
                                 source_kind="edit_diff", source_ref=inp.entity_id or inp.conversation_id, classification=lesson.classification,
                                 evidence=lesson.evidence, diff_summary={**lesson.diff, "needs_evidence": lesson.needs_evidence,
                                                                          "model_opinion": opinion},
                                 dedupe_key=f"{base_key}:{i}:{lesson.classification}")
        k, created = await propose_item(ctx, pin)
        d = serialize_item(k)
        d["created"] = created
        d["needs_evidence"] = lesson.needs_evidence
        d["requires_owner"] = True
        d["decision"] = "Needs review"
        items.append(d)
    outcome = outcome_for(lessons, inp.draft_before, inp.draft_after, inp.outcome)
    row, _ = await record_outcome(ctx, workflow_key=inp.workflow_key, outcome=outcome, entity_kind=inp.entity_kind, entity_id=inp.entity_id,
                                  review_seconds=inp.review_seconds, severity="critical" if outcome == "critical_error" else "normal",
                                  classification=[lesson.classification for lesson in lessons],
                                  details={"draft_version": inp.draft_version, "contact_id": inp.contact_id, "vehicle_id": inp.vehicle_id,
                                           "method": method, "lessons": [it["id"] for it in items]},
                                  source_ref=inp.conversation_id, dedupe_key=f"{base_key}:outcome")
    ctx.record(f"Learned from edit: {outcome} ({len(items)} lesson{'s' if len(items) != 1 else ''})", entity_kind=inp.entity_kind,
               entity_id=inp.entity_id, kind="system", state=outcome,
               details={"workflow_key": inp.workflow_key, "lessons": [(it["kind"], it["classification"]) for it in items],
                        "permission_change": False, "method": method})
    return {"lessons": items, "outcome": serialize_outcome(row), "method": method, "permission_change": False}


class OutcomeIn(BaseModel):
    workflow_key: str
    outcome: str
    entity_kind: str | None = None
    entity_id: str | None = None
    review_seconds: int | None = Field(default=None, ge=0)
    severity: str = "normal"
    details: dict = Field(default_factory=dict)
    at: datetime | None = None
    source_ref: str | None = None
    dedupe_key: str | None = None


@command("learning.record_outcome", input=OutcomeIn, perm="inbox.draft", action_class="internal", workflow_key="learning",
         description="Record one reviewed case outcome for a workflow (accepted/edited/declined/critical_error/unanswered).")
async def learning_record_outcome(ctx: CommandContext, inp: OutcomeIn) -> dict:
    row, created = await record_outcome(ctx, **inp.model_dump())
    return {"outcome": serialize_outcome(row), "created": created}


async def learn_from_edit(db, actor, draft_before: str, draft_after: str, context: dict, *, request_id: str | None = None,
                          commit: bool = True) -> dict:
    """Convenience wrapper for other domains (inbox): classify + persist through the command layer."""
    from ..domain.commands import dispatch
    payload = {"draft_before": draft_before, "draft_after": draft_after, **{k: v for k, v in context.items() if k in LearnFromEditIn.model_fields}}
    res = await dispatch(CommandContext(db=db, actor=actor, request_id=request_id, channel=context.get("channel", "web")),
                         "learning.record_edit", payload, commit=commit)
    return res.data
