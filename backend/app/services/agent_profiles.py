"""Versioned, owner-controlled instructions for the seven AZKT runtime roles.

Profiles are deliberately separate from the stable safety prompt and from permissions. Editing or importing a
profile can change the role's mission, voice and working method; it cannot add tools, credentials or authority.
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from ..agent.prompts import ROLE_DESCRIPTIONS, ROLES
from ..agent.tools import read_tool
from ..core.errors import Blocked, NotFound, ValidationFailed
from ..core.ids import sha256_hex, stable_hash
from ..domain.commands import CommandContext, command
from ..models.agent_profiles import AgentProfile, AgentProfileVersion

ROLE_LABELS = {
    "manager": "Manager",
    "customer_sales": "Customer & sales",
    "sourcing": "Sourcing",
    "logistics": "Logistics",
    "shop": "Shop",
    "listings": "Listings",
    "finance": "Finance",
}
SOURCE_KINDS = ("manual", "manager", "openclaw_import")
PROFILE_FIELDS = (
    "mission", "voice", "core_rules", "operating_instructions", "reporting_expectations",
    "escalation_rules", "tool_guidance", "schedule_guidance",
)
MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_MEMBER_BYTES = 256 * 1024
MAX_PROFILE_CHARS = 80_000
OPENCLAW_DOCS = {"IDENTITY.md", "SOUL.md", "AGENTS.md", "TOOLS.md", "HEARTBEAT.md"}


def _role(value: str) -> str:
    value = (value or "").strip().lower()
    if value not in ROLES:
        raise ValidationFailed(f"role must be one of {ROLES}")
    return value


class ProfileProposeIn(BaseModel):
    role: str
    mission: str = Field(min_length=1, max_length=30_000)
    voice: str = Field(default="", max_length=30_000)
    core_rules: list[str] = Field(default_factory=list, max_length=100)
    operating_instructions: str = Field(default="", max_length=40_000)
    reporting_expectations: str = Field(default="", max_length=15_000)
    escalation_rules: list[str] = Field(default_factory=list, max_length=100)
    tool_guidance: str = Field(default="", max_length=30_000)
    schedule_guidance: str = Field(default="", max_length=15_000)
    change_note: str = Field(default="", max_length=2_000)
    source_kind: str = "manual"
    source_ref: str | None = Field(default=None, max_length=500)
    source_manifest: dict = Field(default_factory=dict)

    @field_validator("role")
    @classmethod
    def valid_role(cls, value: str) -> str:
        return _role(value)

    @field_validator("source_kind")
    @classmethod
    def valid_source(cls, value: str) -> str:
        if value not in SOURCE_KINDS:
            raise ValueError(f"source_kind must be one of {SOURCE_KINDS}")
        return value

    @field_validator("core_rules", "escalation_rules")
    @classmethod
    def clean_list(cls, value: list[str]) -> list[str]:
        if any(len(x.strip()) > 2_000 for x in value):
            raise ValueError("Each rule must be at most 2,000 characters; nothing was saved.")
        return [x.strip() for x in value if x.strip()]


class VersionRef(BaseModel):
    version_id: str


class ActivateIn(VersionRef):
    reason: str = Field(default="", max_length=1_000)


class ProfileGetIn(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def valid_role(cls, value: str) -> str:
        return _role(value)


def default_content(role: str) -> dict:
    return {
        "mission": ROLE_DESCRIPTIONS[_role(role)],
        "voice": "Direct, concise, evidence-based and clear about what is still waiting.",
        "core_rules": [],
        "operating_instructions": "",
        "reporting_expectations": "Lead with the result, then the next step and the records used.",
        "escalation_rules": [],
        "tool_guidance": "",
        "schedule_guidance": "",
    }


def normalize_content(inp: ProfileProposeIn) -> dict:
    content = {key: getattr(inp, key) for key in PROFILE_FIELDS}
    total = sum(len(x) if isinstance(x, str) else sum(len(i) for i in x) for x in content.values())
    if total > MAX_PROFILE_CHARS:
        raise ValidationFailed(f"profile instructions are too large ({total:,} characters; maximum {MAX_PROFILE_CHARS:,})")
    return content


def validate_content(content: dict) -> dict:
    failures: list[str] = []
    warnings: list[str] = []
    if len(str(content.get("mission") or "").strip()) < 10:
        failures.append("Mission needs at least one clear sentence.")
    if not str(content.get("operating_instructions") or "").strip():
        warnings.append("No role-specific operating instructions are recorded; the stable AZKT defaults still apply.")
    if not content.get("escalation_rules"):
        warnings.append("No additional escalation rules are recorded; AZKT's built-in approval rules still apply.")
    joined = "\n".join(str(content.get(k) or "") for k in PROFILE_FIELDS)
    lowered = joined.lower()
    if "ignore previous instructions" in lowered or "override system" in lowered:
        failures.append("Profile text cannot override the stable safety prompt.")
    if "notion" in lowered:
        warnings.append("Legacy Notion references were found. Replace them with AZKT Command records or procedures before activation.")
    if "openclaw" in lowered or "systemd" in lowered:
        warnings.append("Legacy OpenClaw runtime references were found. Keep the business rule, but remove old gateway, workspace or timer steps.")
    return {"ok": not failures, "failures": failures, "warnings": warnings,
            "checked_at": datetime.now(timezone.utc).isoformat(), "checks": 6}


def serialize_version(v: AgentProfileVersion) -> dict:
    return {
        "id": v.id, "profile_id": v.profile_id, "version_no": v.version_no, "content": dict(v.content or {}),
        "content_hash": v.content_hash, "stage": v.stage, "validation": dict(v.validation or {}),
        "source_kind": v.source_kind, "source_ref": v.source_ref, "source_manifest": dict(v.source_manifest or {}),
        "change_note": v.change_note, "activated_at": v.activated_at.isoformat() if v.activated_at else None,
        "activated_by": v.activated_by, "created_at": v.created_at.isoformat() if v.created_at else None,
        "created_by": v.created_by,
    }


def serialize_profile(p: AgentProfile | None, role: str, versions: list[AgentProfileVersion] | None = None) -> dict:
    current = None
    if p and p.current_version_id:
        current = next((v for v in (versions or []) if v.id == p.current_version_id), None)
    return {
        "id": p.id if p else None, "role": role, "label": p.label if p else ROLE_LABELS[role],
        "status": p.status if p else "default", "current_version_id": p.current_version_id if p else None,
        "current_version": serialize_version(current) if current else None,
        "effective_content": dict(current.content) if current else default_content(role),
        "versions": [serialize_version(v) for v in (versions or [])],
        "change_history": list(p.change_history or []) if p else [],
    }


async def _profile_for(db, role: str) -> AgentProfile | None:
    return (await db.execute(select(AgentProfile).where(AgentProfile.role == _role(role)))).scalar_one_or_none()


async def get_profile(db, role: str) -> dict:
    role = _role(role)
    p = await _profile_for(db, role)
    versions = [] if p is None else list((await db.execute(
        select(AgentProfileVersion).where(AgentProfileVersion.profile_id == p.id)
        .order_by(AgentProfileVersion.version_no.desc())
    )).scalars().all())
    return serialize_profile(p, role, versions)


async def list_profiles(db) -> dict:
    return {"items": [await get_profile(db, role) for role in ROLES], "roles": list(ROLES)}


@read_tool("agent_profiles.get", input=ProfileGetIn, perm="agents.chat",
           description="Read the effective editable instructions and version history for one AZKT agent role before proposing a revision.")
async def profile_get_tool(ctx: CommandContext, inp: ProfileGetIn) -> dict:
    return await get_profile(ctx.db, inp.role)


async def current_version_for(db, role: str) -> AgentProfileVersion | None:
    p = await _profile_for(db, role)
    if p is None or not p.current_version_id:
        return None
    v = await db.get(AgentProfileVersion, p.current_version_id)
    return v if v is not None and v.stage == "active" else None


async def version_text(db, version_id: str | None) -> str | None:
    if not version_id:
        return None
    v = await db.get(AgentProfileVersion, version_id)
    if v is None:
        raise NotFound("The instruction version pinned to this mission is missing; work cannot continue safely.")
    c = dict(v.content or {})
    lines = ["These are owner-approved, role-specific business instructions. They are subordinate to the stable safety prompt and cannot grant authority."]
    for label, key in (("Mission", "mission"), ("Voice", "voice"), ("Core rules", "core_rules"),
                       ("Operating instructions", "operating_instructions"), ("Reporting", "reporting_expectations"),
                       ("Escalation", "escalation_rules"), ("Tool guidance", "tool_guidance"),
                       ("Schedule guidance", "schedule_guidance")):
        value = c.get(key)
        if isinstance(value, list):
            value = "\n".join(f"- {x}" for x in value)
        if str(value or "").strip():
            lines.append(f"{label}:\n{str(value).strip()}")
    return "\n\n".join(lines)


@command("agent_profiles.propose", input=ProfileProposeIn, perm="knowledge.write", action_class="internal",
         description="Propose a new version of one AZKT role's editable mission, voice, rules and working instructions. "
                     "This never changes the stable safety prompt, tools or permissions.")
async def propose(ctx: CommandContext, inp: ProfileProposeIn) -> dict:
    content = normalize_content(inp)
    content_hash = stable_hash(content)
    p = await _profile_for(ctx.db, inp.role)
    if p is None:
        p = AgentProfile(role=inp.role, label=ROLE_LABELS[inp.role], status="draft",
                         created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
        ctx.db.add(p)
        await ctx.db.flush()
    existing = (await ctx.db.execute(select(AgentProfileVersion).where(
        AgentProfileVersion.profile_id == p.id, AgentProfileVersion.content_hash == content_hash
    ))).scalars().first()
    if existing:
        return {"profile": await get_profile(ctx.db, inp.role), "version": serialize_version(existing), "created": False}
    last = (await ctx.db.execute(select(func.max(AgentProfileVersion.version_no)).where(
        AgentProfileVersion.profile_id == p.id))).scalar_one() or 0
    v = AgentProfileVersion(profile_id=p.id, version_no=last + 1, content=content, content_hash=content_hash,
                            stage="draft", validation={}, source_kind="manager" if ctx.actor.kind == "agent" else inp.source_kind, source_ref=inp.source_ref,
                            source_manifest=dict(inp.source_manifest), change_note=inp.change_note,
                            created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
    ctx.db.add(v)
    await ctx.db.flush()
    ctx.touch(v, "agent_profile_version")
    ctx.record(f"Proposed {p.label} instructions v{v.version_no}", entity_kind="agent_profile", entity_id=p.id,
               kind="knowledge", state="draft", details={"role": p.role, "version_id": v.id, "source": inp.source_kind})
    ctx.emit("agent_profile.proposed", aggregate_type="agent_profile", aggregate_id=p.id,
             aggregate_version=p.version, payload={"role": p.role, "version_id": v.id, "version_no": v.version_no})
    return {"profile": await get_profile(ctx.db, inp.role), "version": serialize_version(v), "created": True}


async def _version(ctx: CommandContext, version_id: str) -> tuple[AgentProfile, AgentProfileVersion]:
    v = await ctx.db.get(AgentProfileVersion, version_id)
    if v is None:
        raise NotFound("agent profile version not found")
    p = await ctx.db.get(AgentProfile, v.profile_id)
    if p is None:
        raise NotFound("agent profile not found")
    return p, v


@command("agent_profiles.validate", input=VersionRef, perm="knowledge.write", action_class="internal",
         description="Check a draft agent profile for required content and attempts to override AZKT safety rules.")
async def validate(ctx: CommandContext, inp: VersionRef) -> dict:
    p, v = await _version(ctx, inp.version_id)
    result = validate_content(v.content or {})
    v.validation = result
    if v.stage not in {"active", "superseded"}:
        v.stage = "validated" if result["ok"] else "draft"
    ctx.touch(v, "agent_profile_version")
    ctx.record(f"Checked {p.label} instructions v{v.version_no}", entity_kind="agent_profile", entity_id=p.id,
               kind="knowledge", state=v.stage, details={"role": p.role, "validation": result})
    return {"profile": await get_profile(ctx.db, p.role), "version": serialize_version(v), "validation": result}


@command("agent_profiles.activate", input=ActivateIn, perm="knowledge.write", action_class="owner_only",
         approval_kind="other", summary=lambda p: f"Activate agent instructions {p.version_id[:8]}",
         description="Owner activates an already validated agent profile version. The Manager may prepare this exact activation for review.")
async def activate(ctx: CommandContext, inp: ActivateIn) -> dict:
    p, v = await _version(ctx, inp.version_id)
    if not (v.validation or {}).get("ok"):
        raise Blocked("profile must pass validation before activation", version_id=v.id)
    previous_id = p.current_version_id
    if previous_id and previous_id != v.id:
        previous = await ctx.db.get(AgentProfileVersion, previous_id)
        if previous:
            previous.stage = "superseded"
            ctx.touch(previous, "agent_profile_version")
    v.stage = "active"
    v.activated_at = ctx.now
    v.activated_by = ctx.actor.user_id
    p.current_version_id = v.id
    p.status = "active"
    p.change_history = list(p.change_history or []) + [{"at": ctx.now.isoformat(), "by": ctx.actor.user_id,
                                                         "from": previous_id, "to": v.id, "reason": inp.reason}]
    ctx.touch(v, "agent_profile_version")
    ctx.touch(p, "agent_profile")
    ctx.record(f"Activated {p.label} instructions v{v.version_no}", entity_kind="agent_profile", entity_id=p.id,
               kind="knowledge", state="active", details={"role": p.role, "version_id": v.id, "previous": previous_id})
    ctx.emit("agent_profile.activated", aggregate_type="agent_profile", aggregate_id=p.id,
             aggregate_version=p.version, payload={"role": p.role, "version_id": v.id, "previous": previous_id})
    return {"profile": await get_profile(ctx.db, p.role), "version": serialize_version(v), "previous_version_id": previous_id}


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:sk-[a-z0-9_-]{16,}|ntn_[a-z0-9_-]{16,}|secret_[a-z0-9_-]{16,})\b"),
    re.compile(r"(?im)(authorization\s*:\s*bearer\s+)[^\s]+"),
)


def _redact_secrets(text: str) -> tuple[str, int]:
    count = 0
    for pattern in _SECRET_PATTERNS:
        text, n = pattern.subn(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED DURING IMPORT]", text)
        count += n
    return text, count


def inspect_openclaw_bundle(data: bytes) -> dict:
    if not data or len(data) > MAX_ARCHIVE_BYTES:
        raise ValidationFailed("OpenClaw bundle must be a ZIP file no larger than 10 MB")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValidationFailed("file is not a valid ZIP archive") from exc
    grouped: dict[str, dict[str, str]] = {}
    paths: dict[str, dict[str, str]] = {}
    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        clean = info.filename.replace("\\", "/").strip("/")
        parts = [p for p in clean.split("/") if p not in ("", ".", "..")]
        if len(parts) < 2 or parts[-1] not in OPENCLAW_DOCS:
            continue
        if ".." in clean.split("/"):
            raise ValidationFailed("Instruction paths must not contain parent-directory segments")
        workspace = parts[-2]
        if workspace == "workspace":
            source = "architect"
        elif workspace.startswith("workspace-"):
            source = workspace[len("workspace-"):]
        else:
            continue
        if info.file_size > MAX_MEMBER_BYTES:
            raise ValidationFailed(f"Instruction file {clean} exceeds the 256 KB limit")
        if parts[-1] in grouped.get(source, {}):
            raise ValidationFailed(f"Duplicate instruction file for {source}: {parts[-1]}")
        total += info.file_size
        if total > MAX_ARCHIVE_BYTES:
            raise ValidationFailed("selected instruction files exceed the 10 MB import limit")
        raw = zf.read(info).decode("utf-8", errors="replace")
        raw, redactions = _redact_secrets(raw)
        grouped.setdefault(source, {})[parts[-1]] = raw
        paths.setdefault(source, {})[parts[-1]] = clean
        if redactions:
            paths[source][f"{parts[-1]}:redactions"] = str(redactions)
    if not grouped:
        raise ValidationFailed("no OpenClaw IDENTITY.md, SOUL.md, AGENTS.md, TOOLS.md or HEARTBEAT.md files were found")
    agents = []
    for source in sorted(grouped):
        docs = grouped[source]
        agents.append({"source_agent": source, "documents": sorted(docs), "paths": paths[source],
                       "missing": sorted(OPENCLAW_DOCS - set(docs))})
    return {"agents": agents, "documents": grouped, "archive_sha256": sha256_hex(data)}


def imported_payload(role: str, source_agent: str, bundle: dict, filename: str) -> dict:
    role = _role(role)
    source_agent = (source_agent or "").strip().lower()
    docs = bundle["documents"].get(source_agent)
    if docs is None:
        raise ValidationFailed("selected OpenClaw agent was not found in this bundle",
                               available=sorted(bundle["documents"]))
    agent_meta = next(x for x in bundle["agents"] if x["source_agent"] == source_agent)
    fallback = default_content(role)
    return {
        "role": role,
        "mission": docs.get("IDENTITY.md") or fallback["mission"],
        "voice": docs.get("SOUL.md") or fallback["voice"],
        "core_rules": [],
        "operating_instructions": docs.get("AGENTS.md", ""),
        "reporting_expectations": fallback["reporting_expectations"],
        "escalation_rules": [],
        "tool_guidance": docs.get("TOOLS.md", ""),
        "schedule_guidance": docs.get("HEARTBEAT.md", ""),
        "change_note": f"Imported from OpenClaw agent {source_agent}",
        "source_kind": "openclaw_import",
        "source_ref": filename[:500],
        "source_manifest": {"archive_sha256": bundle["archive_sha256"], "source_agent": source_agent,
                            "documents": agent_meta["documents"], "paths": agent_meta["paths"],
                            "missing": agent_meta["missing"]},
    }
