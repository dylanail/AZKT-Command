"""Allowed / Needs review / Blocked decisions (spec §11). Confidence is never the gate."""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from ..models.runtime import Permission, WorkflowControl
from .actors import PERM_KEYS, PERM_TO_CLIENT_SCOPE, Actor

POLICY_VERSION = "v1"

# Role presets (spec §11.1 and the prototype's ROLE_DEFAULTS). Owner = everything.
ROLE_DEFAULTS: dict[str, dict[str, bool]] = {
    "owner": {k: True for k in PERM_KEYS},
    "manager": {
        "vehicles.read": True, "vehicles.write": True, "vehicles.all": True,
        "tasks.read": True, "tasks.write": True, "tasks.assign": True, "tasks.verify": False,
        "contacts.read": True, "contacts.write": True,
        "sales.read": True, "sales.write": True,
        "requests.read": True, "requests.write": True,
        "shipping.read": True, "shipping.write": True,
        "inbox.read": True, "inbox.draft": True, "inbox.send": False,
        "listings.read": True, "listings.draft": True, "listings.publish": False,
        "parts.request": True, "parts.order": False,
        "documents.read": True, "documents.write": True,
        "costs.read": False, "finance.status": True, "finance.write": False,
        "activity.read": True, "agents.chat": True, "intake": True,
        "approve": False, "team": False, "settings": False, "connections": False,
        "knowledge.write": False, "permissions": False,
    },
    "mechanic": {
        "vehicles.read": True, "vehicles.write": False, "vehicles.all": False,
        "tasks.read": True, "tasks.write": True, "tasks.assign": False, "tasks.verify": False,
        "contacts.read": False, "contacts.write": False,
        "sales.read": False, "sales.write": False,
        "requests.read": False, "requests.write": False,
        "shipping.read": False, "shipping.write": False,
        "inbox.read": False, "inbox.draft": False, "inbox.send": False,
        "listings.read": False, "listings.draft": False, "listings.publish": False,
        "parts.request": True, "parts.order": False,
        "documents.read": False, "documents.write": False,
        "costs.read": False, "finance.status": False, "finance.write": False,
        "activity.read": False, "agents.chat": True, "intake": True,
        "approve": False, "team": False, "settings": False, "connections": False,
        "knowledge.write": False, "permissions": False,
    },
}
ROLE_DEFAULTS["sales"] = {**ROLE_DEFAULTS["manager"], "tasks.assign": False, "shipping.write": False,
                          "requests.write": True, "listings.draft": True}
ROLE_DEFAULTS["logistics"] = {**ROLE_DEFAULTS["mechanic"], "vehicles.all": True, "shipping.read": True,
                              "shipping.write": True, "contacts.read": True, "documents.read": True,
                              "documents.write": True, "activity.read": True}
ROLE_DEFAULTS["books"] = {**ROLE_DEFAULTS["mechanic"], "vehicles.all": True, "costs.read": True,
                          "finance.status": True, "finance.write": True, "documents.read": True,
                          "activity.read": True, "tasks.write": False, "parts.request": False}

# Owner-locked keys: cannot be granted to non-owners by override.
OWNER_LOCKED = {"tasks.verify", "team", "approve", "permissions", "settings", "connections",
                "inbox.send", "listings.publish", "parts.order", "finance.write"}


def effective_perms(role: str, overrides: dict | None) -> dict[str, bool]:
    base = dict(ROLE_DEFAULTS.get(role, ROLE_DEFAULTS["mechanic"]))
    for k, v in (overrides or {}).items():
        if k in OWNER_LOCKED and role != "owner":
            continue
        if k in base:
            base[k] = bool(v)
    return base


@dataclass
class Decision:
    outcome: str  # allowed | needs_review | blocked
    reasons: list[str] = field(default_factory=list)
    approval_kind: str | None = None
    permission_id: str | None = None
    policy_version: str = POLICY_VERSION

    @property
    def allowed(self) -> bool:
        return self.outcome == "allowed"

    def to_dict(self) -> dict:
        return {"outcome": self.outcome, "reasons": self.reasons, "policy_version": self.policy_version,
                "permission_id": self.permission_id}


def has_perm(actor: Actor, perm: str | None) -> bool:
    if perm is None:
        return True
    if actor.kind == "system":
        return True
    if actor.kind == "external":
        # intersection: the owner grant AND the client scope
        if not actor.perms.get(perm, False):
            return False
        needed = PERM_TO_CLIENT_SCOPE.get(perm)
        if needed is None:
            return False  # perms without a client-scope mapping are never available externally
        return needed in (actor.client_scopes or [])
    return bool(actor.perms.get(perm, False))


def _matches(pattern: str, name: str) -> bool:
    return fnmatch.fnmatchcase(name, pattern)


async def _is_paused(db, keys: list[str]) -> str | None:
    rows = (await db.execute(select(WorkflowControl).where(WorkflowControl.key.in_(keys)))).scalars().all()
    for r in rows:
        if r.paused:
            return r.key
    return None


async def _find_standing_permission(db, actor: Actor, spec, payload) -> tuple[Permission | None, list[str]]:
    """Return a matching active standing permission (spec §11.3) or None with reasons."""
    now = datetime.now(timezone.utc)
    rows = (await db.execute(select(Permission).where(Permission.status == "active"))).scalars().all()
    reasons: list[str] = []
    for p in rows:
        if not _matches(p.action_pattern, spec.name):
            continue
        if p.effective_from and p.effective_from > now:
            continue
        if p.expires_at and p.expires_at <= now:
            reasons.append(f"permission {p.id[:8]} expired")
            continue
        if p.subject_kind == "user" and p.subject_id != actor.user_id:
            continue
        if p.subject_kind == "client" and p.subject_id != actor.client_id:
            continue
        if p.workflow_key and getattr(spec, "workflow_key", None) not in (None, p.workflow_key):
            continue
        # recipient / domain / record limits
        limits = getattr(spec, "limits", None)
        info = limits(payload) if limits else {}
        rec = info.get("recipients") or []
        if p.recipients and any(r.lower() not in [x.lower() for x in p.recipients] for r in rec):
            reasons.append("recipient outside standing permission")
            continue
        if p.domains and any(r.split("@")[-1].lower() not in [d.lower() for d in p.domains] for r in rec):
            reasons.append("recipient domain outside standing permission")
            continue
        amt = info.get("amount")
        if amt is not None:
            amt = Decimal(str(amt))
            if info.get("currency") and p.currency and info["currency"] != p.currency:
                reasons.append("currency mismatch for standing permission")
                continue
            if p.per_action_limit is not None and amt > p.per_action_limit:
                reasons.append("amount above per-action limit")
                continue
            if p.cumulative_limit is not None and (p.used_amount + p.reserved_amount + amt) > p.cumulative_limit:
                reasons.append("cumulative limit would be exceeded")
                continue
        rl = p.rate_limit or {}
        if rl.get("per_day") and p.used_count >= int(rl["per_day"]):
            reasons.append("rate limit reached")
            continue
        recs = info.get("records") or []
        allowed = (p.allowed_records or {}).get("ids")
        if allowed and any(r not in allowed for r in recs):
            reasons.append("record outside standing permission")
            continue
        shared = info.get("fields") or []
        if p.fields and any(f not in p.fields for f in shared):
            reasons.append("data shared outside standing permission fields")
            continue
        return p, reasons
    return None, reasons


def _can_see_personal(actor: Actor) -> bool:
    """Same rule as services/inbox._can_see_personal on the read side."""
    return actor.kind == "system" or (actor.kind in ("user", "agent") and actor.role == "owner")


def check_record_scope(actor: Actor, records: list[tuple[str, str]], assigned_vehicle_ids: set[str] | None,
                       assigned_task_ids: set[str] | None, threads: dict[str, dict] | None = None) -> list[str]:
    """Record-level access for scope=assigned users and record-limited external clients.

    `threads` carries the resolved shape of `conversation` / `draft` records
    ({record_id: {vehicle_ids, personal}}); a thread is writable on exactly the terms it is readable
    on (services/inbox.list_threads): linked to a vehicle in the actor's visible set, or unlinked."""
    reasons: list[str] = []
    threads = threads or {}
    if actor.kind == "external":
        limit = (actor.client_record_scope or {}).get("vehicle_ids")
        if limit:
            for kind, rid in records:
                if kind == "vehicle" and rid not in limit:
                    reasons.append(f"vehicle {rid} outside client record scope")
                if kind in ("conversation", "draft") and rid in threads:
                    vids = threads[rid]["vehicle_ids"]
                    if vids and not set(vids) & set(limit):
                        reasons.append(f"{kind} {rid} is linked to a vehicle outside client record scope")
    if actor.kind in ("user", "agent") and (actor.scope == "assigned" or not actor.perms.get("vehicles.all", False)):
        for kind, rid in records:
            if kind == "vehicle" and rid not in (assigned_vehicle_ids or set()):
                reasons.append(f"vehicle {rid} not assigned to you")
            if kind == "task" and rid not in (assigned_task_ids or set()):
                reasons.append(f"task {rid} not assigned to you")
            if kind in ("conversation", "draft") and rid in threads:
                vids = threads[rid]["vehicle_ids"]
                if vids and not set(vids) & (assigned_vehicle_ids or set()):
                    reasons.append(f"{kind} {rid} is linked to a vehicle not assigned to you")
    if not _can_see_personal(actor):
        for kind, rid in records:
            if kind in ("conversation", "draft") and (threads.get(rid) or {}).get("personal"):
                reasons.append(f"{kind} {rid} is on the personal account")
    return reasons


async def evaluate(db, actor: Actor, spec, payload, *, approval=None, record_reasons: list[str] | None = None) -> Decision:
    """Decide whether `actor` may run `spec` with `payload` right now."""
    reasons: list[str] = []
    action_class = spec.action_class

    # Deterministic pause switches (global / workflow) apply to automated actors only.
    if actor.kind in ("agent", "external", "system") and action_class != "read":
        paused = await _is_paused(db, ["global", f"workflow:{getattr(spec, 'workflow_key', None) or spec.name}"])
        if paused:
            return Decision("blocked", [f"automation paused ({paused})"])

    if action_class == "forbidden_for_agents" and actor.kind != "user":
        return Decision("blocked", ["unavailable to autonomous agents and connectors"])

    if record_reasons:
        return Decision("blocked", list(record_reasons))

    if not has_perm(actor, spec.perm):
        return Decision("blocked", [f"missing permission {spec.perm}"])

    if action_class in ("read", "internal"):
        return Decision("allowed", ["internal authorized work"])

    if action_class == "owner_only":
        if actor.kind == "user" and actor.role == "owner":
            return Decision("allowed", ["owner action"])
        if actor.kind == "agent" and actor.role == "owner":
            return Decision("needs_review", ["owner decision required; prepared for exact approval"], approval_kind=spec.approval_kind)
        return Decision("blocked", ["owner-only action"])

    # consequential: exact approval binding wins, then a standing permission, else needs review
    if approval is not None:
        if approval.status not in ("approved", "queued", "executing"):
            return Decision("blocked", [f"approval is {approval.status}"])
        if approval.command_name != spec.name:
            return Decision("blocked", ["approval bound to a different action"])
        return Decision("allowed", [f"exact approval {approval.id[:8]} v{approval.approval_version}"])

    perm, why = await _find_standing_permission(db, actor, spec, payload)
    if perm is not None:
        return Decision("allowed", [f"standing permission {perm.id[:8]}"], permission_id=perm.id)
    reasons.extend(why or ["no standing permission covers this action"])
    if actor.kind == "external" and not has_perm(actor, spec.perm):
        return Decision("blocked", reasons)
    return Decision("needs_review", reasons, approval_kind=spec.approval_kind)
