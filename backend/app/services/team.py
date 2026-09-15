"""Team & access commands (spec §3.1 Person/membership, §11.1).

Owner-only administration of people: invitations (hashed one-use tokens), role/scope/manager/
permission changes, disable/enable, explicit grants. Every access change bumps
`users.session_version` (existing sessions die), invalidates that person's queued approvals and
revokes their user-scoped standing permissions (spec §3.1: "access changes invalidate sessions and
queued action authorization"; invariant 12). A person can never change their own role/scope/perms.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from ..core.errors import Blocked, Conflict, Denied, NotFound, ValidationFailed
from ..core.ids import sha256_hex, token as new_token
from ..domain.actors import PERM_KEYS
from ..domain.commands import REGISTRY, CommandContext, command
from ..domain.policy import OWNER_LOCKED, effective_perms
from ..models.auth import ROLES, Invitation, User
from ..models.notify import DELIVERY_KINDS
from ..models.runtime import Approval, Permission

SCOPES = ("all", "assigned")
CHANNEL_MODES = ("telegram_email", "telegram_fallback_email", "email_only")
INVITE_DAYS_DEFAULT = 7
INVITE_DAYS_MAX = 30


# ── serializers ─────────────────────────────────────────────────────────────
def serialize_person(u: User, *, detail: bool = False) -> dict:
    """Compact person row. `detail=True` adds permission detail (owner surfaces only)."""
    out = {
        "id": u.id, "version": u.version or 1, "handle": u.handle, "display_name": u.display_name or u.handle,
        "role": u.role, "status": u.status, "scope": u.scope, "manager_id": u.manager_id,
        "email": u.email, "phone": u.phone, "timezone": u.timezone,
        "last_seen_at": u.last_seen_at.isoformat() if u.last_seen_at else None,
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "disabled_at": u.disabled_at.isoformat() if u.disabled_at else None,
    }
    if detail:
        out.update({
            "perms": effective_perms(u.role, u.perms), "overrides": dict(u.perms or {}), "grants": list(u.grants or []),
            "access_changed_at": u.access_changed_at.isoformat() if u.access_changed_at else None,
            "access_changed_by": u.access_changed_by, "disabled_by": u.disabled_by, "disabled_reason": u.disabled_reason,
            "invited_by": u.invited_by, "invitation_id": u.invitation_id, "session_version": u.session_version or 1,
        })
    return out


def serialize_invitation(inv: Invitation) -> dict:
    """Never includes the token or its hash."""
    return {
        "id": inv.id, "version": inv.version or 1, "display_name": inv.display_name, "email": inv.email, "phone": inv.phone,
        "role": inv.role, "scope": inv.scope, "manager_id": inv.manager_id, "perms": dict(inv.perms or {}),
        "status": inv.status, "invited_by": inv.invited_by, "note": inv.note,
        "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
        "created_at": inv.created_at.isoformat() if inv.created_at else None,
        "accepted_at": inv.accepted_at.isoformat() if inv.accepted_at else None,
        "accepted_user_id": inv.accepted_user_id,
        "revoked_at": inv.revoked_at.isoformat() if inv.revoked_at else None, "revoked_by": inv.revoked_by,
    }


def serialize_prefs(u: User) -> dict:
    return {
        "timezone": u.timezone, "reminder_email": u.reminder_email,
        "reminder_email_verified": bool(u.reminder_email_verified_at),
        "reminder_email_verified_at": u.reminder_email_verified_at.isoformat() if u.reminder_email_verified_at else None,
        "notification_prefs": effective_notification_prefs(u.notification_prefs),
    }


def default_notification_prefs() -> dict:
    """Spec §5.5: email carries the four designed reminders until the owner changes preferences;
    conversational/case updates prefer Telegram with email fallback."""
    channels = {k: "email_only" for k in ("task_reminder", "overdue", "digest", "deposit_confirmed")}
    channels.update({"case_update": "telegram_fallback_email", "connection_issue": "telegram_fallback_email"})
    # the bell is the app's own surface, not an outbound send: on until a person turns it off
    return {"channels": channels, "inapp": True, "quiet_hours": None, "digest_time": "08:00"}


def effective_notification_prefs(stored: dict | None) -> dict:
    base = default_notification_prefs()
    stored = stored or {}
    base["channels"].update({k: v for k, v in (stored.get("channels") or {}).items() if k in DELIVERY_KINDS})
    inapp = stored.get("inapp")
    if isinstance(inapp, bool):
        base["inapp"] = inapp
    elif isinstance(inapp, dict):
        base["inapp"] = {k: bool(v) for k, v in inapp.items() if k in DELIVERY_KINDS}
    if stored.get("quiet_hours") is not None:
        base["quiet_hours"] = stored["quiet_hours"]
    if stored.get("digest_time"):
        base["digest_time"] = stored["digest_time"]
    return base


# ── helpers ─────────────────────────────────────────────────────────────────
def _check_role(role: str) -> None:
    if role not in ROLES:
        raise ValidationFailed(f"role must be one of {ROLES}")


def _check_scope(scope: str) -> None:
    if scope not in SCOPES:
        raise ValidationFailed(f"scope must be one of {SCOPES}")


def _check_perm_overrides(role: str, perms: dict) -> dict:
    """Validate override keys; owner-locked keys can never be granted to a non-owner."""
    out: dict[str, bool] = {}
    for k, v in (perms or {}).items():
        if k not in PERM_KEYS:
            raise ValidationFailed(f"unknown permission {k}")
        if k in OWNER_LOCKED and role != "owner" and bool(v):
            raise ValidationFailed(f"{k} is owner-only and cannot be granted to a {role}")
        out[k] = bool(v)
    return out


def _check_tz(tz: str) -> str:
    try:
        ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        raise ValidationFailed(f"unknown timezone {tz!r}")
    return tz


def _hhmm(value: str, label: str) -> str:
    try:
        h, m = value.split(":")
        if not (0 <= int(h) < 24 and 0 <= int(m) < 60):
            raise ValueError
    except Exception:  # noqa: BLE001
        raise ValidationFailed(f"{label} must be HH:MM")
    return f"{int(h):02d}:{int(m):02d}"


async def _get_user(ctx: CommandContext, user_id: str, expected_version: int | None = None) -> User:
    u = (await ctx.db.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one_or_none()
    if u is None:
        raise NotFound("person not found")
    if expected_version is not None and (u.version or 1) != expected_version:
        raise Conflict("person changed since you loaded it", current_version=u.version or 1)
    return u


async def _active_owner_count(ctx: CommandContext) -> int:
    return int(await ctx.db.scalar(select(func.count()).select_from(User).where(User.role == "owner", User.status == "active")) or 0)


async def _check_manager(ctx: CommandContext, manager_id: str | None, subject_id: str | None = None) -> None:
    if not manager_id:
        return
    if subject_id and manager_id == subject_id:
        raise ValidationFailed("a person cannot report to themself")
    m = await ctx.db.get(User, manager_id)
    if m is None or m.status != "active" or m.role not in ("owner", "manager"):
        raise ValidationFailed("manager must be an active owner or manager")


def _perms_lost(before: dict[str, bool], after: dict[str, bool]) -> list[str]:
    return sorted(k for k, v in before.items() if v and not after.get(k, False))


async def _invalidate_queued_authorization(ctx: CommandContext, user: User, reason: str, *, perms: list[str] | None = None) -> dict:
    """Access changed: this person's pending/approved/queued approvals are invalidated (their external
    action intents are cancelled by approvals.invalidate) and their user-scoped standing permissions
    are revoked. `perms=None` means every grant; a list limits standing-permission revocation to
    commands guarded by those perms."""
    from . import approvals as approvals_svc
    invalidated: list[str] = []
    rows = (await ctx.db.execute(select(Approval).where(
        Approval.status.in_(("pending", "approved", "queued")),
        Approval.requested_by["user_id"].as_string() == user.id))).scalars().all()
    for a in rows:
        spec = REGISTRY.get(a.command_name)
        if perms is not None and spec is not None and spec.perm not in perms:
            continue
        await approvals_svc.invalidate(ctx.child(), approvals_svc.ApprovalRef(approval_id=a.id, reason=reason))
        invalidated.append(a.id)
    revoked: list[str] = []
    prow = (await ctx.db.execute(select(Permission).where(
        Permission.subject_kind == "user", Permission.subject_id == user.id, Permission.status == "active"))).scalars().all()
    for p in prow:
        if perms is not None:
            guarded = {s.perm for n, s in REGISTRY.items() if _pattern_matches(p.action_pattern, n)}
            if not (guarded & set(perms)):
                continue
        p.status = "revoked"
        p.revoked_at = ctx.now
        p.bump(ctx.actor.user_id)
        revoked.append(p.id)
        ctx.emit("permission.revoked", aggregate_type="permission", aggregate_id=p.id, aggregate_version=p.version,
                 payload={"permission_id": p.id, "subject_kind": "user", "subject_id": user.id, "reason": reason})
    return {"approvals_invalidated": invalidated, "standing_permissions_revoked": revoked}


def _pattern_matches(pattern: str, name: str) -> bool:
    import fnmatch
    return fnmatch.fnmatchcase(name, pattern)


def _access_changed(ctx: CommandContext, u: User) -> None:
    u.session_version = (u.session_version or 1) + 1
    u.access_changed_at = ctx.now
    u.access_changed_by = ctx.actor.user_id


# ── invitations ─────────────────────────────────────────────────────────────
class TeamInviteIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    email: str | None = None
    phone: str | None = None
    role: str = "mechanic"
    scope: str | None = None
    manager_id: str | None = None
    perms: dict = Field(default_factory=dict)
    note: str = ""
    expires_days: int = Field(default=INVITE_DAYS_DEFAULT, ge=1, le=INVITE_DAYS_MAX)
    dedupe_key: str | None = None  # retry-safe: same key returns the existing pending invitation (no new token)


@command("team.invite", input=TeamInviteIn, perm="team", action_class="owner_only", approval_kind="permission",
         summary=lambda p: f"Invite {p.display_name} as {p.role}",
         description="Invite a person with a scoped role. Returns the one-time token exactly once; only its hash is stored.")
async def team_invite(ctx: CommandContext, inp: TeamInviteIn) -> dict:
    _check_role(inp.role)
    email = (inp.email or "").strip().lower() or None
    phone = (inp.phone or "").strip() or None
    if not email and not phone:
        raise ValidationFailed("an email or phone is required to deliver the invitation")
    scope = inp.scope or ("all" if inp.role in ("owner", "manager", "sales", "logistics", "books") else "assigned")
    _check_scope(scope)
    perms = _check_perm_overrides(inp.role, inp.perms)
    await _check_manager(ctx, inp.manager_id)
    dedupe = inp.dedupe_key or (f"email:{email}" if email else f"phone:{phone}")
    existing = (await ctx.db.execute(select(Invitation).where(
        Invitation.dedupe_key == dedupe, Invitation.status == "pending", Invitation.expires_at > ctx.now))).scalars().first()
    if existing is not None:
        return {"invitation": serialize_invitation(existing), "token": None, "accept_path": None, "created": False,
                "link_issued": False,
                "note": "An invitation is already pending for this person; reissue its link or revoke it to start over."}
    # The raw token is handed to a signed-in owner exactly once and never persisted. When this command runs
    # under an exact approval (the AI Manager proposed the invitation), the foundation stores the handler
    # result on the approval row, so no link is issued here: the invitation is created with an unreachable
    # hash and an owner issues the link directly with team.reissue_invitation.
    issued = ctx.approval is None and ctx.actor.kind == "user"
    raw = new_token(32)
    inv = Invitation(email=email, phone=phone, display_name=inp.display_name.strip(), role=inp.role, scope=scope,
                     manager_id=inp.manager_id, perms=perms, token_hash=sha256_hex(raw), invited_by=ctx.actor.user_id,
                     expires_at=ctx.now + timedelta(days=inp.expires_days), status="pending", created_at=ctx.now,
                     note=inp.note or "", dedupe_key=dedupe, updated_by=ctx.actor.user_id)
    ctx.db.add(inv)
    await ctx.db.flush()
    ctx.changed.append({"kind": "invitation", "id": inv.id, "version": inv.version or 1})
    ctx.record(f"Invited {inv.display_name} as {inv.role} ({scope})", entity_kind="invitation", entity_id=inv.id,
               kind="access", state="pending", visibility="owner",
               details={"role": inv.role, "scope": scope, "manager_id": inv.manager_id, "overrides": perms, "link_issued": issued})
    ctx.emit("team.invitation_changed", aggregate_type="invitation", aggregate_id=inv.id,
             payload={"invitation_id": inv.id, "status": "pending", "role": inv.role})
    if not issued:
        del raw  # discarded on purpose: nothing can accept this invitation until an owner issues a link
        return {"invitation": serialize_invitation(inv), "token": None, "accept_path": None, "created": True, "link_issued": False,
                "note": "Invitation recorded without a link. A signed-in owner issues the one-time link from Team & access."}
    return {"invitation": serialize_invitation(inv), "token": raw, "accept_path": f"/invite/{raw}", "created": True, "link_issued": True}


class InvitationRef(BaseModel):
    invitation_id: str
    expected_version: int | None = None
    reason: str | None = None


@command("team.revoke_invitation", input=InvitationRef, perm="team", action_class="owner_only", approval_kind="permission",
         description="Revoke a pending invitation so its token can no longer register a passkey.")
async def team_revoke_invitation(ctx: CommandContext, inp: InvitationRef) -> dict:
    inv = (await ctx.db.execute(select(Invitation).where(Invitation.id == inp.invitation_id).with_for_update())).scalar_one_or_none()
    if inv is None:
        raise NotFound("invitation not found")
    if inp.expected_version is not None and (inv.version or 1) != inp.expected_version:
        raise Conflict("invitation changed", current_version=inv.version or 1)
    if inv.status != "pending":
        raise Blocked(f"invitation is {inv.status}", status=inv.status)
    inv.status = "revoked"
    inv.revoked_at = ctx.now
    inv.revoked_by = ctx.actor.user_id
    inv.is_active_flag = False
    ctx.touch(inv, "invitation")
    ctx.record(f"Revoked invitation for {inv.display_name}", entity_kind="invitation", entity_id=inv.id, kind="access",
               state="revoked", visibility="owner", details={"reason": inp.reason})
    ctx.emit("team.invitation_changed", aggregate_type="invitation", aggregate_id=inv.id,
             payload={"invitation_id": inv.id, "status": "revoked"})
    return {"invitation": serialize_invitation(inv)}


@command("team.reissue_invitation", input=InvitationRef, perm="team", action_class="owner_only", approval_kind="permission",
         summary=lambda p: f"Reissue invitation link {p.invitation_id[:8]}",
         description="Mint a fresh one-time link for a pending invitation; the previous link stops working. "
                     "The link is handed only to a signed-in owner, never through an agent or an approval result.")
async def team_reissue_invitation(ctx: CommandContext, inp: InvitationRef) -> dict:
    if ctx.approval is not None or ctx.actor.kind != "user":
        # An approval result is persisted; a bearer credential must never be. Agents may propose the
        # invitation itself (team.invite), but the link is owner-issued work (spec §11.2: broaden access).
        raise Blocked("invitation links are issued directly by a signed-in owner, not through an agent or approval",
                      status="link_not_issued")
    inv = (await ctx.db.execute(select(Invitation).where(Invitation.id == inp.invitation_id).with_for_update())).scalar_one_or_none()
    if inv is None:
        raise NotFound("invitation not found")
    if inp.expected_version is not None and (inv.version or 1) != inp.expected_version:
        raise Conflict("invitation changed", current_version=inv.version or 1)
    if inv.status != "pending":
        raise Blocked(f"invitation is {inv.status}", status=inv.status)
    if inv.expires_at and inv.expires_at <= ctx.now:
        raise Blocked("invitation expired; revoke it and invite again", status="expired")
    raw = new_token(32)
    inv.token_hash = sha256_hex(raw)  # rotates: any earlier link is dead from this commit on
    ctx.touch(inv, "invitation")
    ctx.record(f"Reissued invitation link for {inv.display_name}", entity_kind="invitation", entity_id=inv.id, kind="access",
               state="pending", visibility="owner", details={"reason": inp.reason})
    ctx.emit("team.invitation_changed", aggregate_type="invitation", aggregate_id=inv.id, aggregate_version=inv.version,
             payload={"invitation_id": inv.id, "status": "pending", "link_reissued": True})
    return {"invitation": serialize_invitation(inv), "token": raw, "accept_path": f"/invite/{raw}", "link_issued": True}


# ── people ──────────────────────────────────────────────────────────────────
class PersonUpdateIn(BaseModel):
    user_id: str
    expected_version: int | None = None
    display_name: str | None = None
    email: str | None = None
    phone: str | None = None
    role: str | None = None
    scope: str | None = None
    manager_id: str | None = None
    clear_manager: bool = False
    perms: dict | None = None  # full replacement of per-person overrides
    reason: str | None = None


@command("team.update_person", input=PersonUpdateIn, perm="team", action_class="owner_only", approval_kind="permission",
         summary=lambda p: f"Change access for person {p.user_id[:8]}",
         description="Change a person's role/scope/manager/permission overrides or profile. Access changes end their sessions.")
async def team_update_person(ctx: CommandContext, inp: PersonUpdateIn) -> dict:
    u = await _get_user(ctx, inp.user_id, inp.expected_version)
    access_fields = any(x is not None for x in (inp.role, inp.scope, inp.manager_id, inp.perms)) or inp.clear_manager
    if access_fields and ctx.actor.user_id == u.id:
        # A person never changes their own role/scope/perms, owner included (A01).
        raise Denied("you cannot change your own role, scope or permissions")
    before = effective_perms(u.role, u.perms)
    new_role = inp.role or u.role
    if inp.role is not None:
        _check_role(inp.role)
        if u.role == "owner" and inp.role != "owner" and u.status == "active" and await _active_owner_count(ctx) <= 1:
            raise Blocked("cannot demote the only active owner")
    if inp.scope is not None:
        _check_scope(inp.scope)
    if inp.manager_id is not None:
        await _check_manager(ctx, inp.manager_id, subject_id=u.id)
    overrides = dict(u.perms or {})
    if inp.perms is not None:
        overrides = _check_perm_overrides(new_role, inp.perms)
    elif new_role != "owner":
        overrides = {k: v for k, v in overrides.items() if not (k in OWNER_LOCKED and v)}  # demotion strips locked keys
    changes: dict = {}
    if inp.display_name is not None and inp.display_name.strip():
        changes["display_name"] = (u.display_name, inp.display_name.strip())
        u.display_name = inp.display_name.strip()
    if inp.email is not None:
        changes["email"] = (u.email, inp.email.strip().lower() or None)
        u.email = inp.email.strip().lower() or None
    if inp.phone is not None:
        changes["phone"] = (u.phone, inp.phone.strip() or None)
        u.phone = inp.phone.strip() or None
    if inp.role is not None and inp.role != u.role:
        changes["role"] = (u.role, inp.role)
        u.role = inp.role
    if inp.scope is not None and inp.scope != u.scope:
        changes["scope"] = (u.scope, inp.scope)
        u.scope = inp.scope
    if inp.clear_manager and u.manager_id:
        changes["manager_id"] = (u.manager_id, None)
        u.manager_id = None
    elif inp.manager_id is not None and inp.manager_id != u.manager_id:
        changes["manager_id"] = (u.manager_id, inp.manager_id)
        u.manager_id = inp.manager_id
    if overrides != dict(u.perms or {}):
        changes["overrides"] = (dict(u.perms or {}), overrides)
        u.perms = overrides
    after = effective_perms(u.role, u.perms)
    lost = _perms_lost(before, after)
    cleanup: dict = {"approvals_invalidated": [], "standing_permissions_revoked": []}
    if access_fields:
        _access_changed(ctx, u)
        if "role" in changes or "scope" in changes:
            cleanup = await _invalidate_queued_authorization(ctx, u, "Access changed — review again")
        elif lost:
            cleanup = await _invalidate_queued_authorization(ctx, u, "Access changed — review again", perms=lost)
    ctx.touch(u, "user")
    what = f"Updated access for {u.display_name or u.handle}" if access_fields else f"Updated profile for {u.display_name or u.handle}"
    ctx.record(what, entity_kind="user", entity_id=u.id, kind="access", state=u.status, visibility="owner",
               details={"changes": {k: list(v) for k, v in changes.items()}, "lost_perms": lost, "reason": inp.reason, **cleanup})
    ctx.emit("team.person_changed", aggregate_type="user", aggregate_id=u.id, aggregate_version=u.version,
             payload={"user_id": u.id, "changes": sorted(changes), "access_changed": access_fields})
    if lost:
        ctx.emit("permission.revoked", aggregate_type="user", aggregate_id=u.id, aggregate_version=u.version,
                 payload={"user_id": u.id, "perms": lost, "by": ctx.actor.user_id})
    return {"person": serialize_person(u, detail=True), "changes": sorted(changes), "lost_perms": lost, **cleanup}


class PersonRef(BaseModel):
    user_id: str
    expected_version: int | None = None
    reason: str | None = None


@command("team.disable_person", input=PersonRef, perm="team", action_class="owner_only", approval_kind="permission",
         summary=lambda p: f"Disable person {p.user_id[:8]}",
         description="Disable a person: every session dies, queued authorization is invalidated.")
async def team_disable_person(ctx: CommandContext, inp: PersonRef) -> dict:
    u = await _get_user(ctx, inp.user_id, inp.expected_version)
    if u.id == ctx.actor.user_id:
        raise Denied("you cannot disable yourself")
    if u.status == "disabled":
        raise Blocked("person is already disabled", status=u.status)
    if u.role == "owner" and await _active_owner_count(ctx) <= 1:
        raise Blocked("cannot disable the only active owner")
    u.status = "disabled"
    u.disabled_at = ctx.now
    u.disabled_by = ctx.actor.user_id
    u.disabled_reason = inp.reason
    _access_changed(ctx, u)
    cleanup = await _invalidate_queued_authorization(ctx, u, "Person disabled — authorization withdrawn")
    ctx.touch(u, "user")
    ctx.record(f"Disabled {u.display_name or u.handle}", entity_kind="user", entity_id=u.id, kind="access", state="disabled",
               visibility="owner", details={"reason": inp.reason, **cleanup})
    ctx.emit("team.person_changed", aggregate_type="user", aggregate_id=u.id, aggregate_version=u.version,
             payload={"user_id": u.id, "changes": ["status"], "status": "disabled", "access_changed": True})
    ctx.emit("permission.revoked", aggregate_type="user", aggregate_id=u.id, aggregate_version=u.version,
             payload={"user_id": u.id, "perms": sorted(k for k, v in effective_perms(u.role, u.perms).items() if v),
                      "by": ctx.actor.user_id, "disabled": True})
    return {"person": serialize_person(u, detail=True), **cleanup}


@command("team.enable_person", input=PersonRef, perm="team", action_class="owner_only", approval_kind="permission",
         summary=lambda p: f"Re-enable person {p.user_id[:8]}",
         description="Re-enable a disabled person. They sign in again with their passkey; nothing queued is restored.")
async def team_enable_person(ctx: CommandContext, inp: PersonRef) -> dict:
    u = await _get_user(ctx, inp.user_id, inp.expected_version)
    if u.status == "active":
        raise Blocked("person is already active", status=u.status)
    u.status = "active"
    u.disabled_at = None
    u.disabled_by = None
    u.disabled_reason = None
    _access_changed(ctx, u)
    ctx.touch(u, "user")
    ctx.record(f"Re-enabled {u.display_name or u.handle}", entity_kind="user", entity_id=u.id, kind="access", state="active",
               visibility="owner", details={"reason": inp.reason})
    ctx.emit("team.person_changed", aggregate_type="user", aggregate_id=u.id, aggregate_version=u.version,
             payload={"user_id": u.id, "changes": ["status"], "status": "active", "access_changed": True})
    return {"person": serialize_person(u, detail=True)}


# ── explicit grants ─────────────────────────────────────────────────────────
class GrantIn(BaseModel):
    user_id: str
    perm: str
    expected_version: int | None = None
    note: str | None = None


@command("team.grant", input=GrantIn, perm="permissions", action_class="owner_only", approval_kind="permission",
         summary=lambda p: f"Grant {p.perm} to person {p.user_id[:8]}",
         description="Explicit owner grant of one permission key to a person (e.g. costs.read for a manager). Records who/when.")
async def team_grant(ctx: CommandContext, inp: GrantIn) -> dict:
    u = await _get_user(ctx, inp.user_id, inp.expected_version)
    if u.id == ctx.actor.user_id:
        raise Denied("you cannot change your own permissions")
    if inp.perm not in PERM_KEYS:
        raise ValidationFailed(f"unknown permission {inp.perm}")
    if inp.perm in OWNER_LOCKED and u.role != "owner":
        raise ValidationFailed(f"{inp.perm} is owner-only and cannot be granted to a {u.role}")
    if u.status != "active":
        raise Blocked("person is not active", status=u.status)
    already = effective_perms(u.role, u.perms).get(inp.perm, False)
    u.perms = {**(u.perms or {}), inp.perm: True}
    u.grants = list(u.grants or []) + [{"perm": inp.perm, "granted": True, "by": ctx.actor.user_id,
                                        "at": ctx.now.isoformat(), "note": inp.note}]
    _access_changed(ctx, u)  # A03: cached access is refreshed by a new session
    ctx.touch(u, "user")
    ctx.record(f"Granted {inp.perm} to {u.display_name or u.handle}", entity_kind="user", entity_id=u.id, kind="access",
               state="granted", visibility="owner", details={"perm": inp.perm, "note": inp.note, "already_effective": already})
    ctx.emit("permission.granted", aggregate_type="user", aggregate_id=u.id, aggregate_version=u.version,
             payload={"user_id": u.id, "perm": inp.perm, "by": ctx.actor.user_id})
    ctx.emit("team.person_changed", aggregate_type="user", aggregate_id=u.id, aggregate_version=u.version,
             payload={"user_id": u.id, "changes": ["overrides"], "access_changed": True})
    return {"person": serialize_person(u, detail=True), "perm": inp.perm, "already_effective": already}


@command("team.revoke_grant", input=GrantIn, perm="permissions", action_class="owner_only", approval_kind="permission",
         summary=lambda p: f"Revoke {p.perm} from person {p.user_id[:8]}",
         description="Withdraw one permission key from a person; their sessions, queued approvals and standing permissions for it end.")
async def team_revoke_grant(ctx: CommandContext, inp: GrantIn) -> dict:
    u = await _get_user(ctx, inp.user_id, inp.expected_version)
    if u.id == ctx.actor.user_id:
        raise Denied("you cannot change your own permissions")
    if inp.perm not in PERM_KEYS:
        raise ValidationFailed(f"unknown permission {inp.perm}")
    had = effective_perms(u.role, u.perms).get(inp.perm, False)
    u.perms = {**(u.perms or {}), inp.perm: False}
    u.grants = list(u.grants or []) + [{"perm": inp.perm, "granted": False, "by": ctx.actor.user_id,
                                        "at": ctx.now.isoformat(), "note": inp.note}]
    _access_changed(ctx, u)
    cleanup = await _invalidate_queued_authorization(ctx, u, f"Permission {inp.perm} revoked — review again", perms=[inp.perm])
    ctx.touch(u, "user")
    ctx.record(f"Revoked {inp.perm} from {u.display_name or u.handle}", entity_kind="user", entity_id=u.id, kind="access",
               state="revoked", visibility="owner", details={"perm": inp.perm, "note": inp.note, "had": had, **cleanup})
    ctx.emit("permission.revoked", aggregate_type="user", aggregate_id=u.id, aggregate_version=u.version,
             payload={"user_id": u.id, "perms": [inp.perm], "by": ctx.actor.user_id})
    ctx.emit("team.person_changed", aggregate_type="user", aggregate_id=u.id, aggregate_version=u.version,
             payload={"user_id": u.id, "changes": ["overrides"], "access_changed": True})
    return {"person": serialize_person(u, detail=True), "perm": inp.perm, "had": had, **cleanup}


# ── own preferences ─────────────────────────────────────────────────────────
class QuietHours(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str
    end: str


class NotificationPrefsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channels: dict[str, str] | None = None
    # in-app bell: one switch for everything, or one per reminder kind
    inapp: bool | dict[str, bool] | None = None
    quiet_hours: QuietHours | None = None
    clear_quiet_hours: bool = False
    digest_time: str | None = None


class MePrefsIn(BaseModel):
    """Only preferences. Role/scope/perms are not fields here: extra keys are rejected."""
    model_config = ConfigDict(extra="forbid")
    timezone: str | None = None
    reminder_email: str | None = None
    notification_prefs: NotificationPrefsIn | None = None
    display_name: str | None = None


@command("me.update_prefs", input=MePrefsIn, perm=None, action_class="internal",
         description="Update your own timezone, reminder email (unverified until verified) and notification preferences.")
async def me_update_prefs(ctx: CommandContext, inp: MePrefsIn) -> dict:
    if ctx.actor.kind != "user" or not ctx.actor.user_id:
        raise Denied("only a signed-in person can change their preferences")
    u = await _get_user(ctx, ctx.actor.user_id)
    changed: list[str] = []
    if inp.timezone is not None:
        u.timezone = _check_tz(inp.timezone)
        changed.append("timezone")
    if inp.display_name is not None and inp.display_name.strip():
        u.display_name = inp.display_name.strip()[:120]
        changed.append("display_name")
    if inp.reminder_email is not None:
        new = inp.reminder_email.strip().lower() or None
        if new and ("@" not in new or new.startswith("@") or new.endswith("@")):
            raise ValidationFailed("reminder_email must be an email address")
        if new != (u.reminder_email or None):
            u.reminder_email = new
            u.reminder_email_verified_at = None  # unverified until a verification flow confirms it
            changed.append("reminder_email")
    if inp.notification_prefs is not None:
        prefs = dict(u.notification_prefs or {})
        np = inp.notification_prefs
        if np.channels is not None:
            ch = dict(prefs.get("channels") or {})
            for kind, mode in np.channels.items():
                if kind not in DELIVERY_KINDS:
                    raise ValidationFailed(f"unknown reminder kind {kind}; expected one of {DELIVERY_KINDS}")
                if mode not in CHANNEL_MODES:
                    raise ValidationFailed(f"channel mode must be one of {CHANNEL_MODES}")
                ch[kind] = mode
            prefs["channels"] = ch
        if np.inapp is not None:
            if isinstance(np.inapp, bool):
                prefs["inapp"] = np.inapp
            else:
                cur = dict(prefs.get("inapp")) if isinstance(prefs.get("inapp"), dict) else {}
                for kind, on in np.inapp.items():
                    if kind not in DELIVERY_KINDS:
                        raise ValidationFailed(f"unknown reminder kind {kind}; expected one of {DELIVERY_KINDS}")
                    cur[kind] = bool(on)
                prefs["inapp"] = cur
        if np.clear_quiet_hours:
            prefs["quiet_hours"] = None
        elif np.quiet_hours is not None:
            prefs["quiet_hours"] = {"start": _hhmm(np.quiet_hours.start, "quiet_hours.start"),
                                    "end": _hhmm(np.quiet_hours.end, "quiet_hours.end")}
        if np.digest_time is not None:
            prefs["digest_time"] = _hhmm(np.digest_time, "digest_time")
        u.notification_prefs = prefs
        changed.append("notification_prefs")
    ctx.touch(u, "user")
    ctx.record("Updated own preferences", entity_kind="user", entity_id=u.id, kind="access", state="active",
               details={"changed": changed})
    ctx.emit("me.prefs_changed", aggregate_type="user", aggregate_id=u.id, aggregate_version=u.version,
             payload={"user_id": u.id, "changed": changed})
    return {"prefs": serialize_prefs(u), "changed": changed}


async def list_people(db, actor_user_id: str | None, *, full: bool) -> list[User]:
    """Owner: everyone. Manager: active people who report to them (plus unassigned mechanics they may assign)."""
    if full:
        return (await db.execute(select(User).order_by(User.created_at))).scalars().all()
    q = select(User).where(User.status == "active").where(
        (User.manager_id == actor_user_id) | ((User.manager_id.is_(None)) & (User.role == "mechanic"))).order_by(User.created_at)
    return (await db.execute(q)).scalars().all()


async def telegram_pairing_status(db, user_id: str) -> dict:
    """Read-only placeholder for Settings/Me: pairing is owned by the Telegram domain."""
    try:
        from ..models.notify import TelegramPairing
        row = (await db.execute(select(TelegramPairing).where(TelegramPairing.user_id == user_id, TelegramPairing.status == "active")
                                .order_by(TelegramPairing.created_at.desc()))).scalars().first()
    except Exception:  # noqa: BLE001
        return {"status": "unknown", "note": "Pairing status unavailable"}
    if row is None:
        return {"status": "not_paired"}
    return {"status": "active", "paired_at": row.confirmed_in_app_at.isoformat() if row.confirmed_in_app_at else None,
            "last_inbound_at": row.last_inbound_at.isoformat() if row.last_inbound_at else None,
            "delivery_failures": row.delivery_failures or 0}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
