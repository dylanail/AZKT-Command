"""Owner-only key/value settings with a validated allowlist, shop gate rules and workflow pause
controls (spec §2.3 Settings, §5.4 reminder defaults, §8.4 gates, §11.5 deterministic controls).

Storage: the `settings` table (key -> JSON). Each value is an envelope
`{"data": {...}, "version": n}` so `settings.update` can take `expected_version`.
Other domains read effective values through `get_effective(db, key)` / `effective_gate_rules(db, to_state)`;
they never read the raw rows.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from ..core.errors import Blocked, Conflict, NotFound, ValidationFailed
from ..core.money import parse_amount
from ..core.time import PHOENIX
from ..domain.commands import CommandContext, command
from ..models.legacy import Setting
from ..models.runtime import Approval, ExternalAction, WorkflowControl
from ..models.vehicles import RECON_STATES, ShopGateRule

REMINDER_KINDS_CONFIGURABLE = ("task_reminder", "overdue", "digest", "deposit_confirmed")
CHANNEL_MODES = ("telegram_email", "telegram_fallback_email", "email_only")
GATE_REQUIREMENTS = ("photos_min", "recon_verified", "disclosures_written", "inspection_logged", "docs_complete")
# Factual gates can never be turned green by an override (spec §8.4, invariant 10).
FACTUAL_REQUIREMENTS = {"photos_min", "recon_verified", "inspection_logged", "docs_complete"}


# ── allowlisted keys and their validated shapes ─────────────────────────────
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskReminderCfg(_Strict):
    enabled: bool = True
    default_offset: str = "15m"  # at|15m|1h|1d|custom


class OverdueCfg(_Strict):
    enabled: bool = True
    delay_minutes: int = Field(default=60, ge=1, le=24 * 60)  # once, one hour after scheduled time


class DigestCfg(_Strict):
    enabled: bool = True
    local_time: str = "08:00"  # proposed 8:00 AM Phoenix; editable
    timezone: str = PHOENIX
    non_empty_only: bool = True


class DepositConfirmedCfg(_Strict):
    enabled: bool = True
    owner_only: bool = True


class RemindersSettings(_Strict):
    task_reminder: TaskReminderCfg = TaskReminderCfg()
    overdue: OverdueCfg = OverdueCfg()
    digest: DigestCfg = DigestCfg()
    deposit_confirmed: DepositConfirmedCfg = DepositConfirmedCfg()
    # default channel per reminder kind; per-person prefs (me.update_prefs) override these
    channels: dict[str, str] = Field(default_factory=lambda: {k: "email_only" for k in REMINDER_KINDS_CONFIGURABLE})
    employee_reminders_enabled: bool = False  # employee/customer sends are separate opt-in permissions
    late_grace_minutes: int = Field(default=10, ge=0, le=24 * 60)
    obsolete_after_hours: int = Field(default=12, ge=1, le=24 * 14)


class SpendCaps(_Strict):
    per_action: str | None = None   # decimal string; None = unset (no default spending cap, spec §11.2)
    daily: str | None = None
    monthly: str | None = None
    currency: str = "USD"


class AutomationSettings(_Strict):
    parts_cap: str | None = None  # unset by default: parts purchases stay exact-review
    spend_caps: SpendCaps = SpendCaps()
    model_daily_budget_usd: str | None = None
    model_monthly_budget_usd: str | None = None
    discretionary_ai_enabled: bool = False


class IntakeSettings(_Strict):
    photo_requirements: list[str] = Field(default_factory=lambda: ["front_34", "interior", "rear", "side", "bed", "engine"])
    require_condition_note: bool = True


class CalendarSettings(_Strict):
    """Calendar writes are a separate explicit capability (spec §11.2), so `writes_enabled` starts
    false and a connected Google account alone never makes AZKT create anything."""
    writes_enabled: bool = False
    calendar_id: str = "primary"            # "primary" = the connected account's own calendar
    conflict_check: bool = True             # look for an overlapping entry and report it on the task
    call_minutes: int = Field(default=30, ge=5, le=12 * 60)      # length when a task only says when it starts
    meeting_minutes: int = Field(default=60, ge=5, le=12 * 60)
    follow_up_minutes: int = Field(default=30, ge=5, le=12 * 60)


class ReportingSettings(_Strict):
    """Cost categories that must be recorded before Home labels gross profit "Recorded" (spec §2.4)."""
    required_cost_categories: list[str] = Field(default_factory=lambda: ["purchase", "import", "recon"])


SETTINGS_SPEC: dict[str, type[BaseModel]] = {
    "reminders": RemindersSettings,
    "automation": AutomationSettings,
    "intake": IntakeSettings,
    "reporting": ReportingSettings,
    "calendar": CalendarSettings,
}


def _validate(key: str, data: dict) -> dict:
    model = SETTINGS_SPEC.get(key)
    if model is None:
        raise ValidationFailed(f"unknown setting {key}; allowed: {sorted(SETTINGS_SPEC)}")
    try:
        obj = model.model_validate(data)
    except ValidationError as e:
        raise ValidationFailed(f"invalid value for {key}", errors=e.errors(include_url=False))
    out = obj.model_dump(mode="json")
    if key == "reminders":
        r: RemindersSettings = obj  # type: ignore[assignment]
        if r.task_reminder.default_offset not in ("at", "15m", "1h", "1d", "custom"):
            raise ValidationFailed("task_reminder.default_offset must be at|15m|1h|1d|custom")
        _hhmm(r.digest.local_time, "digest.local_time")
        try:
            ZoneInfo(r.digest.timezone)
        except Exception:  # noqa: BLE001
            raise ValidationFailed(f"unknown timezone {r.digest.timezone!r}")
        for k, v in r.channels.items():
            if k not in REMINDER_KINDS_CONFIGURABLE:
                raise ValidationFailed(f"unknown reminder kind {k}")
            if v not in CHANNEL_MODES:
                raise ValidationFailed(f"channel mode must be one of {CHANNEL_MODES}")
    if key == "reporting":
        from ..models.finance import COST_CATEGORIES
        rp: ReportingSettings = obj  # type: ignore[assignment]
        bad = [c for c in rp.required_cost_categories if c not in COST_CATEGORIES]
        if bad:
            raise ValidationFailed(f"unknown cost categories {bad}; allowed: {sorted(COST_CATEGORIES)}")
        if not rp.required_cost_categories:
            raise ValidationFailed("at least one required cost category is needed")
    if key == "calendar":
        c: CalendarSettings = obj  # type: ignore[assignment]
        cid = (c.calendar_id or "").strip()
        if not cid:
            raise ValidationFailed("calendar_id must be 'primary' or a calendar address")
        out["calendar_id"] = cid
    if key == "automation":
        a: AutomationSettings = obj  # type: ignore[assignment]
        for label, val in (("parts_cap", a.parts_cap), ("spend_caps.per_action", a.spend_caps.per_action),
                           ("spend_caps.daily", a.spend_caps.daily), ("spend_caps.monthly", a.spend_caps.monthly),
                           ("model_daily_budget_usd", a.model_daily_budget_usd), ("model_monthly_budget_usd", a.model_monthly_budget_usd)):
            if val is not None:
                try:
                    amt = parse_amount(val)
                except ValueError:
                    raise ValidationFailed(f"{label} must be a decimal amount")
                if amt < 0:
                    raise ValidationFailed(f"{label} cannot be negative")
        if len(a.spend_caps.currency) != 3:
            raise ValidationFailed("spend_caps.currency must be an ISO code")
        out["spend_caps"]["currency"] = a.spend_caps.currency.upper()
    return out


def _hhmm(value: str, label: str) -> str:
    try:
        h, m = value.split(":")
        if not (0 <= int(h) < 24 and 0 <= int(m) < 60):
            raise ValueError
    except Exception:  # noqa: BLE001
        raise ValidationFailed(f"{label} must be HH:MM")
    return f"{int(h):02d}:{int(m):02d}"


def defaults_for(key: str) -> dict:
    return SETTINGS_SPEC[key]().model_dump(mode="json")


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _unwrap(row: Setting | None) -> tuple[dict, int]:
    if row is None or not isinstance(row.value, dict):
        return {}, 0
    if "data" in row.value and "version" in row.value:
        return dict(row.value.get("data") or {}), int(row.value.get("version") or 0)
    return dict(row.value), 1  # legacy plain value


async def get_effective(db, key: str) -> dict:
    """Defaults merged with stored overrides, validated. Safe for other domains to call."""
    if key not in SETTINGS_SPEC:
        raise ValidationFailed(f"unknown setting {key}")
    row = await db.get(Setting, key)
    data, _ = _unwrap(row)
    try:
        return _validate(key, _deep_merge(defaults_for(key), data))
    except ValidationFailed:
        return defaults_for(key)  # a corrupt stored value never breaks readers


async def get_entry(db, key: str) -> dict:
    row = await db.get(Setting, key)
    data, version = _unwrap(row)
    return {"key": key, "value": await get_effective(db, key), "overrides": data, "version": version,
            "defaults": defaults_for(key), "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
            "updated_by": row.updated_by if row else None, "recorded": row is not None}


async def all_entries(db) -> dict:
    return {k: await get_entry(db, k) for k in SETTINGS_SPEC}


class SettingUpdateIn(BaseModel):
    key: str
    value: dict
    expected_version: int | None = None
    replace: bool = False  # True = replace stored overrides; False = deep-merge into them


@command("settings.update", input=SettingUpdateIn, perm="settings", action_class="owner_only", approval_kind="other",
         summary=lambda p: f"Change settings: {p.key}",
         description="Change an allowlisted business setting (reminders, automation caps, intake). Validated; versioned.")
async def settings_update(ctx: CommandContext, inp: SettingUpdateIn) -> dict:
    if inp.key not in SETTINGS_SPEC:
        raise ValidationFailed(f"unknown setting {inp.key}; allowed: {sorted(SETTINGS_SPEC)}")
    row = (await ctx.db.execute(select(Setting).where(Setting.key == inp.key).with_for_update())).scalar_one_or_none()
    data, version = _unwrap(row)
    if inp.expected_version is not None and version != inp.expected_version:
        raise Conflict("setting changed since you loaded it", current_version=version)
    new_overrides = dict(inp.value) if inp.replace else _deep_merge(data, inp.value)
    effective = _validate(inp.key, _deep_merge(defaults_for(inp.key), new_overrides))
    if row is None:
        row = Setting(key=inp.key, value={}, updated_at=ctx.now, updated_by=ctx.actor.user_id)
        ctx.db.add(row)
    row.value = {"data": new_overrides, "version": version + 1}
    row.updated_at = ctx.now
    row.updated_by = ctx.actor.user_id
    await ctx.db.flush()
    ctx.changed.append({"kind": "setting", "id": inp.key, "version": version + 1})
    ctx.record(f"Changed settings: {inp.key}", entity_kind="setting", entity_id=inp.key, kind="system", state="updated",
               visibility="owner", details={"keys": sorted(inp.value), "version": version + 1})
    ctx.emit("settings.changed", aggregate_type="setting", aggregate_id=inp.key, aggregate_version=version + 1,
             payload={"key": inp.key, "changed": sorted(inp.value)})
    return {"key": inp.key, "value": effective, "overrides": new_overrides, "version": version + 1}


# ── shop gate rules ─────────────────────────────────────────────────────────
def default_gate_rules() -> list[dict]:
    """Spec §8.4 defaults. Factual gates are not overridable."""
    return [
        {"to_state": "in_recon", "requirement": "inspection_logged", "label": "Inspection logged", "param": {}, "overridable": False},
        {"to_state": "ready_for_sale", "requirement": "photos_min", "label": "At least 6 photos", "param": {"min": 6}, "overridable": False},
        {"to_state": "ready_for_sale", "requirement": "recon_verified", "label": "Recon work verified by owner", "param": {}, "overridable": False},
        {"to_state": "ready_for_sale", "requirement": "disclosures_written", "label": "Disclosures written", "param": {}, "overridable": False},
    ]


def serialize_gate_rule(r: ShopGateRule) -> dict:
    return {"id": r.id, "version": r.version, "to_state": r.to_state, "requirement": r.requirement, "label": r.label,
            "param": r.param or {}, "overridable": bool(r.overridable), "active": bool(r.active),
            "factual": r.requirement in FACTUAL_REQUIREMENTS, "source": "persisted",
            "updated_at": r.updated_at.isoformat() if r.updated_at else None, "updated_by": r.updated_by}


async def effective_gate_rules(db, to_state: str | None = None) -> list[dict]:
    """Persisted active rules; states with no persisted rule at all fall back to the spec defaults
    (reported with source="default"). Consumed by the vehicles domain when moving recon stages."""
    rows = (await db.execute(select(ShopGateRule).order_by(ShopGateRule.to_state, ShopGateRule.requirement))).scalars().all()
    persisted_states = {r.to_state for r in rows}
    out = [serialize_gate_rule(r) for r in rows if r.active]
    for d in default_gate_rules():
        if d["to_state"] not in persisted_states:
            out.append({**d, "id": None, "version": 0, "active": True, "factual": d["requirement"] in FACTUAL_REQUIREMENTS,
                        "source": "default", "updated_at": None, "updated_by": None})
    if to_state:
        out = [r for r in out if r["to_state"] == to_state]
    return out


class GateRuleUpsertIn(BaseModel):
    rule_id: str | None = None
    expected_version: int | None = None
    to_state: str
    requirement: str
    label: str | None = None
    param: dict = Field(default_factory=dict)
    overridable: bool = False
    active: bool = True


def _validate_gate(inp: GateRuleUpsertIn) -> None:
    if inp.to_state not in RECON_STATES:
        raise ValidationFailed(f"to_state must be one of {RECON_STATES}")
    if inp.requirement not in GATE_REQUIREMENTS:
        raise ValidationFailed(f"requirement must be one of {GATE_REQUIREMENTS}")
    if inp.overridable and inp.requirement in FACTUAL_REQUIREMENTS:
        raise ValidationFailed(f"{inp.requirement} is a factual gate and cannot be overridable")
    if inp.requirement == "photos_min":
        try:
            n = int(inp.param.get("min", 0))
        except (TypeError, ValueError):
            raise ValidationFailed("photos_min needs param.min (integer)")
        if n < 1:
            raise ValidationFailed("photos_min needs param.min >= 1")


@command("settings.gate_rule_upsert", input=GateRuleUpsertIn, perm="settings", action_class="owner_only", approval_kind="other",
         summary=lambda p: f"Gate rule {p.to_state}: {p.requirement}",
         description="Create or update a shop gate rule. Factual gates cannot be made overridable.")
async def gate_rule_upsert(ctx: CommandContext, inp: GateRuleUpsertIn) -> dict:
    _validate_gate(inp)
    label = (inp.label or "").strip() or next((d["label"] for d in default_gate_rules() if d["requirement"] == inp.requirement),
                                              inp.requirement.replace("_", " "))
    r: ShopGateRule | None = None
    if inp.rule_id:
        r = (await ctx.db.execute(select(ShopGateRule).where(ShopGateRule.id == inp.rule_id).with_for_update())).scalar_one_or_none()
        if r is None:
            raise NotFound("gate rule not found")
        if inp.expected_version is not None and r.version != inp.expected_version:
            raise Conflict("gate rule changed", current_version=r.version)
        other = (await ctx.db.execute(select(ShopGateRule).where(
            ShopGateRule.to_state == inp.to_state, ShopGateRule.requirement == inp.requirement, ShopGateRule.id != r.id))).scalars().first()
        if other is not None:
            # (to_state, requirement) is the rule's identity: editing one rule onto another would leave two rows
            raise Conflict("a rule for that stage and requirement already exists; edit that rule instead", rule_id=other.id)
    else:
        r = (await ctx.db.execute(select(ShopGateRule).where(
            ShopGateRule.to_state == inp.to_state, ShopGateRule.requirement == inp.requirement).with_for_update())).scalars().first()
    created = r is None
    if created:
        # first persisted rule for a state materializes that state's defaults so nothing silently disappears
        await _materialize_defaults_for_state(ctx, inp.to_state, skip_requirement=inp.requirement)
        r = ShopGateRule(to_state=inp.to_state, requirement=inp.requirement, label=label, param=inp.param,
                         overridable=inp.overridable, active=inp.active, created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
        ctx.db.add(r)
        await ctx.db.flush()
        ctx.changed.append({"kind": "shop_gate_rule", "id": r.id, "version": r.version})
    else:
        r.to_state, r.requirement, r.label, r.param, r.overridable, r.active = (
            inp.to_state, inp.requirement, label, inp.param, inp.overridable, inp.active)
        ctx.touch(r, "shop_gate_rule")
    ctx.record(f"{'Added' if created else 'Updated'} gate rule: {r.to_state} requires {r.label}", entity_kind="shop_gate_rule",
               entity_id=r.id, kind="system", state="active" if r.active else "inactive", visibility="owner",
               details={"requirement": r.requirement, "param": r.param, "overridable": r.overridable})
    ctx.emit("settings.gate_rules_changed", aggregate_type="shop_gate_rule", aggregate_id=r.id, aggregate_version=r.version,
             payload={"to_state": r.to_state, "requirement": r.requirement, "active": r.active})
    return {"rule": serialize_gate_rule(r), "created": created}


async def _materialize_defaults_for_state(ctx: CommandContext, to_state: str, *, skip_requirement: str | None = None) -> int:
    n = await ctx.db.scalar(select(func.count()).select_from(ShopGateRule).where(ShopGateRule.to_state == to_state))
    if n:
        return 0
    made = 0
    for d in default_gate_rules():
        if d["to_state"] != to_state or d["requirement"] == skip_requirement:
            continue
        ctx.db.add(ShopGateRule(**d, active=True, created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id))
        made += 1
    await ctx.db.flush()
    return made


class GateRuleRef(BaseModel):
    rule_id: str
    expected_version: int | None = None


@command("settings.gate_rule_deactivate", input=GateRuleRef, perm="settings", action_class="owner_only", approval_kind="other",
         description="Deactivate a gate rule (history kept; a factual gate still cannot be overridden while active).")
async def gate_rule_deactivate(ctx: CommandContext, inp: GateRuleRef) -> dict:
    r = (await ctx.db.execute(select(ShopGateRule).where(ShopGateRule.id == inp.rule_id).with_for_update())).scalar_one_or_none()
    if r is None:
        raise NotFound("gate rule not found")
    if inp.expected_version is not None and r.version != inp.expected_version:
        raise Conflict("gate rule changed", current_version=r.version)
    if not r.active:
        raise Blocked("gate rule is already inactive")
    r.active = False
    ctx.touch(r, "shop_gate_rule")
    ctx.record(f"Deactivated gate rule: {r.to_state} requires {r.label}", entity_kind="shop_gate_rule", entity_id=r.id,
               kind="system", state="inactive", visibility="owner")
    ctx.emit("settings.gate_rules_changed", aggregate_type="shop_gate_rule", aggregate_id=r.id, aggregate_version=r.version,
             payload={"to_state": r.to_state, "requirement": r.requirement, "active": False})
    return {"rule": serialize_gate_rule(r)}


class GateRulesResetIn(BaseModel):
    to_state: str | None = None  # None = every state


@command("settings.gate_rules_reset", input=GateRulesResetIn, perm="settings", action_class="owner_only", approval_kind="other",
         description="Persist the spec default gate rules (idempotent), re-activating defaults that were deactivated.")
async def gate_rules_reset(ctx: CommandContext, inp: GateRulesResetIn) -> dict:
    if inp.to_state and inp.to_state not in RECON_STATES:
        raise ValidationFailed(f"to_state must be one of {RECON_STATES}")
    rows = (await ctx.db.execute(select(ShopGateRule))).scalars().all()
    by_key = {(r.to_state, r.requirement): r for r in rows}
    created, updated = 0, 0
    for d in default_gate_rules():
        if inp.to_state and d["to_state"] != inp.to_state:
            continue
        r = by_key.get((d["to_state"], d["requirement"]))
        if r is None:
            ctx.db.add(ShopGateRule(**d, active=True, created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id))
            created += 1
        elif not r.active or r.param != d["param"] or r.overridable != d["overridable"]:
            r.active, r.param, r.overridable, r.label = True, d["param"], d["overridable"], d["label"]
            ctx.touch(r, "shop_gate_rule")
            updated += 1
    await ctx.db.flush()
    ctx.record(f"Reset gate rules to defaults ({created} added, {updated} restored)", entity_kind="shop_gate_rule", entity_id=None,
               kind="system", state="active", visibility="owner", details={"to_state": inp.to_state})
    ctx.emit("settings.gate_rules_changed", aggregate_type="shop_gate_rule", payload={"reset": True, "to_state": inp.to_state})
    return {"created": created, "restored": updated, "rules": await effective_gate_rules(ctx.db, inp.to_state)}


# ── workflow controls (pause) ───────────────────────────────────────────────
def _check_pause_key(key: str) -> str:
    key = key.strip()
    if key == "global":
        return key
    if key.startswith("workflow:") and len(key) > len("workflow:"):
        return key
    if key.startswith("thread:") and len(key) > len("thread:"):
        return key
    raise ValidationFailed("key must be global, workflow:<name> or thread:<id>")


def serialize_control(c: WorkflowControl) -> dict:
    return {"key": c.key, "paused": bool(c.paused), "reason": c.reason, "changed_by": c.changed_by,
            "changed_at": c.changed_at.isoformat() if c.changed_at else None}


async def outstanding_actions(db) -> dict:
    """What pausing leaves in flight (spec §11.5: show pending/unknown actions when pausing)."""
    rows = (await db.execute(select(ExternalAction.state, func.count()).group_by(ExternalAction.state))).all()
    by_state = {s: int(n) for s, n in rows}
    arows = (await db.execute(select(Approval.status, func.count()).group_by(Approval.status))).all()
    approvals = {s: int(n) for s, n in arows}
    return {
        "external_actions": {
            "pending": by_state.get("intent", 0) + by_state.get("claimed", 0),
            "executing": by_state.get("executing", 0),
            "unknown": by_state.get("unknown", 0),
            "by_state": by_state,
        },
        "approvals": {
            "pending": approvals.get("pending", 0),
            "queued": approvals.get("approved", 0) + approvals.get("queued", 0) + approvals.get("executing", 0),
            "result_unknown": approvals.get("result_unknown", 0),
        },
    }


async def list_controls(db) -> list[dict]:
    rows = (await db.execute(select(WorkflowControl).order_by(WorkflowControl.key))).scalars().all()
    out = [serialize_control(c) for c in rows]
    if not any(c["key"] == "global" for c in out):
        out.insert(0, {"key": "global", "paused": False, "reason": None, "changed_by": None, "changed_at": None})
    return out


class PauseIn(BaseModel):
    key: str = "global"
    paused: bool = True
    reason: str | None = None


@command("settings.pause", input=PauseIn, perm="settings", action_class="owner_only", approval_kind="other",
         summary=lambda p: f"{'Pause' if p.paused else 'Resume'} automation: {p.key}",
         description="Deterministic pause/resume switch (global, per workflow, per thread) read by the policy engine. "
                     "Resuming does not replay anything; queued work is revalidated by its own domain.")
async def settings_pause(ctx: CommandContext, inp: PauseIn) -> dict:
    key = _check_pause_key(inp.key)
    c = (await ctx.db.execute(select(WorkflowControl).where(WorkflowControl.key == key).with_for_update())).scalar_one_or_none()
    if c is None:
        c = WorkflowControl(key=key, paused=False)
        ctx.db.add(c)
    was = bool(c.paused)
    c.paused = inp.paused
    c.reason = inp.reason
    c.changed_by = ctx.actor.user_id
    c.changed_at = ctx.now
    await ctx.db.flush()
    outstanding = await outstanding_actions(ctx.db)
    ctx.changed.append({"kind": "workflow_control", "id": key, "version": 0})
    verb = "Paused" if inp.paused else "Resumed"
    ctx.record(f"{verb} automation: {key}" + (f" — {inp.reason}" if inp.reason else ""), entity_kind="workflow_control",
               entity_id=key, kind="system", state="paused" if inp.paused else "active", exception=inp.paused,
               details={"was_paused": was, "outstanding": outstanding})
    ctx.emit("workflow.control_changed", aggregate_type="workflow_control", aggregate_id=key,
             payload={"key": key, "paused": inp.paused, "reason": inp.reason, "was_paused": was})
    if was and not inp.paused:
        ctx.emit("workflow.resumed", aggregate_type="workflow_control", aggregate_id=key,
                 payload={"key": key, "revalidate": True})
    return {"control": serialize_control(c), "outstanding": outstanding, "changed": was != inp.paused}


async def connection_freshness(db) -> list[dict]:
    """Truthful placeholder until adapters own it: rows from `connections`, or an explicit empty state."""
    try:
        from ..models.comms import Connection
        rows = (await db.execute(select(Connection).order_by(Connection.provider))).scalars().all()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for c in rows:
        out.append({"id": c.id, "provider": c.provider, "label": c.label, "status": c.status,
                    "account_identity": c.account_identity,
                    "last_attempt_at": c.last_attempt_at.isoformat() if c.last_attempt_at else None,
                    "last_success_at": c.last_success_at.isoformat() if c.last_success_at else None,
                    "coverage_to": c.coverage_to.isoformat() if c.coverage_to else None,
                    "watch_expires_at": c.watch_expires_at.isoformat() if c.watch_expires_at else None,
                    "failure": c.failure or {}, "freshness": "unknown" if not c.last_success_at else c.status})
    return out


def now() -> datetime:
    return datetime.now(timezone.utc)


def money_or_none(v: Any) -> Decimal | None:
    return None if v is None else parse_amount(v)
