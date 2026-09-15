"""The generic external-agent connector (spec §10.8, invariant 14, acceptance J01–J06).

One service backs both transports — the remote MCP server at `/mcp` and the versioned HTTP API under
`/api/integrations/v1/` — so a client without MCP gets identical behaviour on the same canonical records
and the same command layer.

Guarantees implemented here:

* **Bounded credentials.** A client gets its own bearer token (sha256 stored, prefix kept), a scope
  subset of `models.external.CLIENT_SCOPES`, an optional record scope and an expiry. Never Dylan's
  browser session, never a provider secret, never a universal key. Rotation and revocation are immediate.
* **No privilege laundering.** Asking the owner-capable Manager cannot widen a grant: the connector's
  Actor carries `client_scopes` / `client_record_scope`, `domain.policy` intersects them with the owner's
  rights on every command, and `domain.access` applies the record scope to every read.
* **One logical request.** `(client, request_key)` is unique: a retry after a dropped connection returns
  the same `DelegatedRequest` and the same mission, never a second one.
* **Loop safety.** Causation ids, a bounded delegation depth and per-client quotas. An AZKT status reply
  is data, never a new instruction; `"Dylan already approved"` in a request is text, never an approval.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..core.config import settings
from ..core.crypto import encrypt
from ..core.errors import Denied, DomainError, NotFound, ValidationFailed
from ..core.ids import sha256_hex
from ..domain.actors import Actor
from ..domain.commands import CommandContext, command, dispatch
from ..domain.policy import effective_perms
from ..models.auth import User
from ..models.external import CLIENT_SCOPES, DelegatedRequest, ExternalClient
from ..models.runtime import Mission

log = logging.getLogger("azkt.connector")

TOKEN_PREFIX = "azkt_ec_"
DEFAULT_QUOTA = {"per_minute": 60, "concurrent": 4, "per_day": 2000}
# This module deliberately declares no COMMANDS_FOR_MANAGER entries: connector credentials are changed by
# the owner in Settings, never through a Manager tool (see agent/tools.EXCLUDED_COMMANDS, spec §10.8).


class QuotaExceeded(DomainError):
    status_code = 429
    code = "rate_limited"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _host(url: str | None) -> str:
    """Host only. An activity row says where a callback goes without repeating a path that may carry a token."""
    from urllib.parse import urlparse
    return urlparse(url or "").netloc or ""


# ── credentials ──────────────────────────────────────────────────────────────
def mint_token() -> tuple[str, str, str]:
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return raw, sha256_hex(raw), raw[:len(TOKEN_PREFIX) + 6]


def is_active(client: ExternalClient | None) -> bool:
    if client is None or client.status != "active":
        return False
    if client.expires_at and client.expires_at <= _now():
        return False
    return True


def serialize_client(c: ExternalClient, *, include_health: bool = True) -> dict:
    d = {"id": c.id, "name": c.name, "transport": c.transport, "status": c.status,
         "token_prefix": c.token_prefix, "scopes": list(c.scopes or []),
         "record_scope": dict(c.record_scope or {}), "quota": dict(c.quota or DEFAULT_QUOTA),
         "expires_at": _iso(c.expires_at), "revoked_at": _iso(c.revoked_at),
         "last_used_at": _iso(c.last_used_at), "use_count": int(c.use_count or 0),
         "callback_url": c.callback_url, "callback_configured": bool(c.callback_url),
         # A destination without a signing key cannot be signed, and AZKT never pushes an unsigned body, so
         # the owner must see the difference between "address saved" and "callbacks will actually be sent".
         # Deliberately *named* without the word the secret is stored under: nothing that even looks like a
         # secret key belongs in a serialized client (the connector tests assert exactly that).
         "callback_signing_configured": bool(c.callback_secret_enc),
         "callbacks_enabled": bool(c.callback_url and c.callback_secret_enc),
         "owner_user_id": c.owner_user_id, "notes": c.notes, "version": c.version,
         "created_at": _iso(c.created_at), "rotated_from_id": c.rotated_from_id}
    if include_health:
        d["health"] = dict(c.health or {})
    return d


async def _load(ctx: CommandContext, client_id: str, expected_version: int | None = None) -> ExternalClient:
    c = (await ctx.db.execute(select(ExternalClient).where(ExternalClient.id == client_id)
                              .with_for_update())).scalar_one_or_none()
    if c is None:
        raise NotFound("external client not found")
    if expected_version is not None and c.version != expected_version:
        from ..core.errors import Conflict
        raise Conflict("client changed — review again", current_version=c.version)
    return c


# ── commands (owner only) ────────────────────────────────────────────────────
class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    transport: str = Field(default="both", description="mcp | http | both")
    scopes: list[str] = Field(default_factory=list)
    record_scope: dict = Field(default_factory=dict)
    quota: dict = Field(default_factory=dict)
    expires_at: datetime | None = None
    notes: str = ""


def _clean_grant(inp: RegisterIn) -> tuple[list[str], dict]:
    """Validate the grant with structured, JSON-clean errors (never a raw exception in the response)."""
    bad = [s for s in inp.scopes if s not in CLIENT_SCOPES]
    if bad:
        raise ValidationFailed(f"unknown scopes: {', '.join(sorted(bad))}", unknown_scopes=sorted(bad),
                               allowed_scopes=list(CLIENT_SCOPES))
    if inp.transport not in ("mcp", "http", "both"):
        raise ValidationFailed("transport must be mcp | http | both", transport=inp.transport)
    extra = set(inp.record_scope or {}) - {"vehicle_ids"}
    if extra:
        raise ValidationFailed("record_scope supports vehicle_ids only", unsupported_keys=sorted(extra))
    ids = (inp.record_scope or {}).get("vehicle_ids") or []
    if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
        raise ValidationFailed("record_scope.vehicle_ids must be a list of vehicle ids")
    return sorted(set(inp.scopes)), ({"vehicle_ids": sorted(set(ids))} if ids else {})


@command("external_clients.register", input=RegisterIn, perm="connections", action_class="owner_only",
         approval_kind="permission", summary=lambda p: f"Register external agent client “{p.name}”",
         description="Register a generic external agent client and issue its bearer token once. The token is "
                     "stored only as a sha256 hash; the plain value is returned exactly once.")
async def register(ctx: CommandContext, inp: RegisterIn) -> dict:
    scopes, record_scope = _clean_grant(inp)
    raw, digest, prefix = mint_token()
    quota = {**DEFAULT_QUOTA, **{k: int(v) for k, v in (inp.quota or {}).items() if str(v).isdigit()}}
    c = ExternalClient(name=inp.name, owner_user_id=ctx.actor.user_id, transport=inp.transport,
                       token_hash=digest, token_prefix=prefix, scopes=scopes,
                       record_scope=record_scope, status="active", quota=quota,
                       expires_at=inp.expires_at, notes=inp.notes, health={"state": "registered"},
                       usage_window={}, created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
    ctx.db.add(c)
    await ctx.db.flush()
    ctx.changed.append({"kind": "external_client", "id": c.id, "version": c.version})
    ctx.record(f"External agent client registered: {c.name}", entity_kind="external_client", entity_id=c.id,
               kind="access", state="active", visibility="owner",
               details={"scopes": list(c.scopes or []), "record_scope": dict(c.record_scope or {}),
                        "transport": c.transport, "token_prefix": prefix})
    ctx.emit("external_client.changed", aggregate_type="external_client", aggregate_id=c.id,
             aggregate_version=c.version, payload={"change": "registered", "scopes": list(c.scopes or [])})
    return {"client": serialize_client(c), "token": raw,
            "token_note": "Store this now — AZKT keeps only its hash and can never show it again.",
            "mcp_url": f"{settings.api_base}/mcp", "http_base": f"{settings.api_base}/api/integrations/v1"}


class ClientRef(BaseModel):
    client_id: str
    expected_version: int | None = None
    reason: str = ""


@command("external_clients.rotate", input=ClientRef, perm="connections", action_class="owner_only",
         summary=lambda p: "Rotate an external agent client credential",
         description="Issue a new bearer token for a client; the previous token stops working immediately.")
async def rotate(ctx: CommandContext, inp: ClientRef) -> dict:
    c = await _load(ctx, inp.client_id, inp.expected_version)
    if c.status == "revoked":
        from ..core.errors import Blocked
        raise Blocked("client is revoked; register a new one")
    raw, digest, prefix = mint_token()
    old_prefix = c.token_prefix
    c.token_hash, c.token_prefix, c.rotated_from_id = digest, prefix, c.id
    c.health = {**(c.health or {}), "state": "rotated", "rotated_at": _iso(ctx.now)}
    ctx.touch(c, "external_client")
    ctx.record(f"External agent credential rotated: {c.name}", entity_kind="external_client", entity_id=c.id,
               kind="access", state="rotated", visibility="owner",
               details={"previous_prefix": old_prefix, "token_prefix": prefix, "reason": inp.reason})
    ctx.emit("external_client.changed", aggregate_type="external_client", aggregate_id=c.id,
             aggregate_version=c.version, payload={"change": "rotated"})
    return {"client": serialize_client(c), "token": raw,
            "token_note": "Store this now — the previous token no longer works."}


@command("external_clients.revoke", input=ClientRef, perm="connections", action_class="owner_only",
         summary=lambda p: "Revoke an external agent client",
         description="Revoke a client immediately: its token is rejected from the next request, and its in-flight "
                     "missions are paused so nothing keeps running on a withdrawn grant.")
async def revoke(ctx: CommandContext, inp: ClientRef) -> dict:
    c = await _load(ctx, inp.client_id, inp.expected_version)
    c.status, c.revoked_at = "revoked", ctx.now
    c.health = {**(c.health or {}), "state": "revoked", "reason": inp.reason or "revoked by the owner"}
    ctx.touch(c, "external_client")
    paused = 0
    rows = (await ctx.db.execute(select(Mission).where(
        Mission.client_id == c.id,
        Mission.status.in_(("open", "running", "waiting_approval", "waiting_external", "waiting_until",
                            "needs_information"))))).scalars().all()
    from ..agent import runtime as rt
    for m in rows:
        m.status = "paused"
        m.paused_reason = "external client access revoked"
        m.next_check_at = None
        rt.add_update(m, "paused", "External client access was revoked; this mission stopped.")
        m.bump(ctx.actor.user_id)
        paused += 1
    await ctx.db.execute(
        DelegatedRequest.__table__.update()
        .where(DelegatedRequest.client_id == c.id,
               DelegatedRequest.status.in_(("accepted", "running", "waiting", "needs_input")))
        .values(status="cancelled", cancelled_at=ctx.now, error="client revoked"))
    ctx.record(f"External agent client revoked: {c.name}", entity_kind="external_client", entity_id=c.id,
               kind="access", state="revoked", visibility="owner",
               details={"missions_paused": paused, "reason": inp.reason})
    ctx.emit("external_client.changed", aggregate_type="external_client", aggregate_id=c.id,
             aggregate_version=c.version, payload={"change": "revoked", "missions_paused": paused})
    return {"client": serialize_client(c), "missions_paused": paused}


class CallbackIn(BaseModel):
    client_id: str
    expected_version: int | None = None
    callback_url: str | None = None
    callback_secret: str | None = None


@command("external_clients.set_callback", input=CallbackIn, perm="connections", action_class="owner_only",
         summary=lambda p: "Set the callback destination for an external agent client",
         description="Configure the optional signed callback destination for a client. Only the owner sets this; a "
                     "URL supplied inside a prompt or a request payload is never used (spec §10.8).")
async def set_callback(ctx: CommandContext, inp: CallbackIn) -> dict:
    url = (inp.callback_url or "").strip() or None
    if url and not url.startswith("https://"):
        raise ValidationFailed("callback_url must be an https URL configured by the owner", callback_url=url)
    c = await _load(ctx, inp.client_id, inp.expected_version)
    secret = (inp.callback_secret or "").strip() or None
    if url and secret is None and not c.callback_secret_enc:
        # An unsigned push to the internet is not something AZKT offers: without a shared secret the
        # receiver cannot tell an AZKT callback from anyone else's POST (spec §10.8 "signed callbacks").
        raise ValidationFailed("a callback destination needs a signing secret so the receiving agent can "
                               "verify the body really came from AZKT", callback_url=url)
    c.callback_url = url
    if url is None:
        c.callback_secret_enc = None        # clearing the destination clears the secret with it
    elif secret is not None:
        c.callback_secret_enc = encrypt(secret)
    ctx.touch(c, "external_client")
    ctx.record(f"External agent callback {'set' if inp.callback_url else 'cleared'}: {c.name}",
               entity_kind="external_client", entity_id=c.id, kind="access", state="updated", visibility="owner",
               details={"callback_configured": bool(url), "secret_rotated": bool(secret),
                        "destination_host": _host(url)})
    ctx.emit("external_client.changed", aggregate_type="external_client", aggregate_id=c.id,
             aggregate_version=c.version, payload={"change": "callback"})
    return {"client": serialize_client(c)}


# ── authentication, quotas ───────────────────────────────────────────────────
async def actor_for(db, client: ExternalClient, *, delegation_depth: int = 0) -> Actor:
    """The connector's Actor: the owner's effective rights, to be intersected with the client grant by policy."""
    owner = await db.get(User, client.owner_user_id)
    perms = effective_perms(owner.role, owner.perms) if owner is not None else {}
    return Actor(kind="external", user_id=client.owner_user_id, role=(owner.role if owner else "owner"),
                 scope="all", perms=perms, display_name=client.name, client_id=client.id,
                 client_name=client.name, client_scopes=list(client.scopes or []),
                 client_record_scope=dict(client.record_scope or {}), delegation_depth=int(delegation_depth))


async def authenticate(db, authorization: str | None, *, delegation_depth_header: str | None = None,
                       transport: str = "http") -> tuple[ExternalClient, Actor]:
    """Bearer token -> active client -> Actor. No session cookie is ever accepted here (spec §10.8)."""
    raw = (authorization or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if not raw:
        raise Denied("a bearer token is required for the external-agent connector", www_authenticate="Bearer")
    client = (await db.execute(select(ExternalClient)
                               .where(ExternalClient.token_hash == sha256_hex(raw)))).scalar_one_or_none()
    if client is None:
        raise Denied("unknown or rotated credential")
    if not is_active(client):
        raise Denied(f"client credential is {('expired' if client.status == 'active' else client.status)}")
    if client.transport not in ("both", transport):
        raise Denied(f"this client is registered for {client.transport}, not {transport}")
    depth = 0
    if delegation_depth_header not in (None, ""):
        try:
            depth = int(str(delegation_depth_header).strip())
        except ValueError:
            raise ValidationFailed("X-AZKT-Delegation-Depth must be an integer")
        if depth < 0:
            raise ValidationFailed("X-AZKT-Delegation-Depth must not be negative")
        if depth > int(settings.MAX_DELEGATION_DEPTH):
            raise Denied(f"delegation depth {depth} exceeds the limit of {settings.MAX_DELEGATION_DEPTH}; "
                         "agent-to-agent chains are bounded", delegation_depth=depth,
                         max_delegation_depth=int(settings.MAX_DELEGATION_DEPTH))
    return client, await actor_for(db, client, delegation_depth=depth)


async def check_rate(db, client: ExternalClient, *, commit: bool = True) -> None:
    """Per-minute request limit, charged once per inbound request on either transport (spec §10.8).

    Reads are metered too — a connector cannot poll or search without bound — but they never consume a
    concurrency slot, which belongs to work in flight.
    """
    quota = {**DEFAULT_QUOTA, **(client.quota or {})}
    minute = _now().strftime("%Y-%m-%dT%H:%M")
    window = dict(client.usage_window or {})
    count = int(window.get("count") or 0) if window.get("minute") == minute else 0
    if count >= int(quota.get("per_minute") or DEFAULT_QUOTA["per_minute"]):
        raise QuotaExceeded("per-minute quota reached for this client", retry_after=60,
                            quota=quota, scope="per_minute")
    client.usage_window = {"minute": minute, "count": count + 1}
    client.last_used_at = _now()
    client.use_count = int(client.use_count or 0) + 1
    client.health = {**(client.health or {}), "state": "ok", "last_seen": _iso(client.last_used_at)}
    if commit:
        await db.commit()
    else:
        await db.flush()


def role_work_cap() -> int:
    """How much work one agent role may start in a single sweep pass (spec §10.4).

    The connector's own per-client `concurrent` quota bounds one caller; this bounds the *role*, so a
    burst of due missions is drained over successive passes instead of starting all at once."""
    return max(1, int(settings.AGENT_ROLE_CONCURRENCY or 1))


async def check_concurrency(db, client: ExternalClient) -> None:
    """How much work this client may have in flight at once (spec §10.8)."""
    quota = {**DEFAULT_QUOTA, **(client.quota or {})}
    in_flight = await db.scalar(select(func.count()).select_from(Mission).where(
        Mission.client_id == client.id, Mission.status.in_(("open", "running"))))
    if int(in_flight or 0) >= int(quota.get("concurrent") or DEFAULT_QUOTA["concurrent"]):
        raise QuotaExceeded("too many concurrent missions for this client", retry_after=10,
                            quota=quota, scope="concurrent", in_flight=int(in_flight or 0))


# ── delegated requests ───────────────────────────────────────────────────────
STATE_FOR_MISSION = {"succeeded": "done", "failed": "failed", "cancelled": "cancelled",
                     "waiting_approval": "waiting", "waiting_external": "waiting", "waiting_until": "waiting",
                     "needs_information": "needs_input", "paused": "waiting", "open": "running",
                     "running": "running"}


def _ctx(db, actor: Actor, *, correlation_id: str | None = None, causation_id: str | None = None,
         channel: str = "http", request_id: str | None = None) -> CommandContext:
    return CommandContext(db=db, actor=actor, request_id=request_id, correlation_id=correlation_id,
                          causation_id=causation_id, channel=channel)


async def _assert_assets(db, actor: Actor, asset_ids: list[str]) -> None:
    """Only this client's own finalized assets may be used (J05: a foreign asset id is rejected)."""
    if not asset_ids:
        return
    from ..models.assets import Asset
    from ..services import assets as assets_svc
    for aid in asset_ids:
        a = await db.get(Asset, aid)
        if a is None or not assets_svc.owns_asset(actor, a):
            raise Denied("asset is not available to this client", asset_id=aid)


def request_envelope(req: DelegatedRequest, mission: Mission | None, *, cursor: int = 0,
                     include_updates: bool = True) -> dict:
    result = dict((mission.result if mission else req.result) or {})
    approvals = result.get("approvals") or []
    env = {
        "request_id": req.id,
        "request_key": req.request_key,
        "mission_id": req.mission_id,
        "state": req.status,
        "summary": result.get("summary") or req.result.get("summary") or "",
        "changed": result.get("changed") or [],
        "citations": result.get("citations") or [],
        "receipts": result.get("receipts") or [],
        "needed_input": result.get("needed_input"),
        "review_links": [{"approval_id": a.get("id"), "title": a.get("title"),
                          "url": f"{settings.PUBLIC_ORIGIN}{a.get('review_path') or '/approvals/' + str(a.get('id'))}"}
                         for a in approvals if a.get("id")],
        "cursor": int(mission.cursor if mission else req.cursor or 0),
        "correlation_id": req.correlation_id,
        "error": req.error,
        "created_at": _iso(req.created_at),
    }
    if mission is not None:
        env["mission_status"] = mission.status
        env["waiting_on"] = mission.waiting_on
        env["next_check_at"] = _iso(mission.next_check_at)
        if include_updates:
            from ..agent import runtime as rt
            env["updates"] = rt.updates_since(mission, cursor)
    return env


async def ask(db, actor: Actor, client: ExternalClient, *, message: str, request_key: str,
              entity_refs: list | None = None, asset_ids: list | None = None,
              conversation_id: str | None = None, channel: str = "http") -> tuple[dict, bool]:
    """Ask Manager a question or start authorized work. Returns (envelope, accepted) —
    accepted=True means long work is running and the caller should poll (HTTP 202)."""
    if not (message or "").strip():
        raise ValidationFailed("message is required")
    if not (request_key or "").strip():
        raise ValidationFailed("request_key is required so a retry maps to the same logical request")
    existing = (await db.execute(select(DelegatedRequest).where(
        DelegatedRequest.client_id == client.id, DelegatedRequest.request_key == request_key))).scalar_one_or_none()
    if existing is not None:
        # A retry after a dropped connection is the SAME logical request (J02), so it is answered from the
        # stored request *before* the concurrency gate: re-reading work already in flight must never be
        # refused as "too much work in flight".
        mission = await db.get(Mission, existing.mission_id) if existing.mission_id else None
        existing.last_polled_at = _now()
        await db.commit()
        return request_envelope(existing, mission), existing.status in ("accepted", "running", "waiting", "needs_input")

    await check_concurrency(db, client)          # only genuinely new work takes a concurrency slot
    depth = int(actor.delegation_depth or 0)
    if depth >= int(settings.MAX_DELEGATION_DEPTH):
        raise Denied(f"delegation depth {depth} reached the limit of {settings.MAX_DELEGATION_DEPTH}; "
                     "this chain cannot start more AZKT work", delegation_depth=depth)
    echo = await _echo_of(db, client, message)
    if echo is not None:
        # An AZKT status reply fed back in is data, not a new instruction (spec §10.8 echo-loop prevention).
        mission = await db.get(Mission, echo.mission_id) if echo.mission_id else None
        return ({**request_envelope(echo, mission),
                 "echo_suppressed": True,
                 "note": ("this message repeats an AZKT status reply for request "
                          f"{echo.id}; AZKT status replies are never re-submitted as instructions. "
                          "Poll that request, or send a new instruction.")}, False)
    await _assert_assets(db, actor, list(asset_ids or []))

    req = DelegatedRequest(client_id=client.id, request_key=request_key, kind="ask", message=message[:8000],
                           entity_refs=list(entity_refs or []), asset_ids=list(asset_ids or []),
                           status="accepted", depth=depth, correlation_id=f"dr-{secrets.token_hex(8)}",
                           result={}, created_by=client.id, updated_by=client.id)
    db.add(req)
    await db.flush()
    req.causation_id = req.id
    await db.commit()

    from ..agent import manager
    thread_key = f"client:{client.id}:{conversation_id or req.id}"
    try:
        out = await manager.handle_message(
            db, actor, message, channel=channel, thread_key=thread_key,
            context={"entity_refs": list(entity_refs or []), "client": client.name},
            attachments=list(asset_ids or []), role="manager",
            mission_kwargs={"client_id": client.id, "delegated_request_id": req.id, "depth": depth + 1,
                            "trigger": "mcp" if channel == "mcp" else "http",
                            "entity_refs": list(entity_refs or []),
                            "correlation_id": req.correlation_id})
    except Denied as e:
        req.status, req.error = "denied", e.message
        req.result = {"summary": e.message}
        await db.commit()
        raise
    except Exception as e:  # noqa: BLE001
        # The request row is already durable, and `(client, request_key)` is unique: without recording the
        # failure a retry would keep replaying an "accepted" request that never has a mission. A failed
        # request says so truthfully, with no invented progress.
        await db.rollback()
        req = await db.get(DelegatedRequest, req.id)
        if req is not None:
            req.status, req.error = "failed", f"{type(e).__name__}: {str(e)[:300]}"
            req.result = {"summary": "AZKT could not start this work; nothing was changed by it."}
            await db.commit()
        raise
    req = await db.get(DelegatedRequest, req.id)
    mission = await db.get(Mission, out.get("mission_id")) if out.get("mission_id") else None
    req.mission_id = out.get("mission_id")
    req.status = STATE_FOR_MISSION.get(mission.status, "running") if mission is not None else "answered"
    req.cursor = int(mission.cursor or 0) if mission is not None else 0
    req.result = {"summary": out.get("text") or "", "changed": out.get("changed") or [],
                  "approvals": out.get("approvals") or [], "citations": out.get("citations") or [],
                  "needed_input": out.get("needed_input")}
    req.bump(client.id)
    await db.commit()
    accepted = req.status in ("accepted", "running", "waiting", "needs_input")
    return request_envelope(req, mission), accepted


def _normalize(text: str) -> str:
    return " ".join((text or "").split()).strip().lower()[:2000]


async def _echo_of(db, client: ExternalClient, message: str) -> DelegatedRequest | None:
    """Is this message simply an AZKT answer this client was handed back? (loop prevention, spec §10.8)."""
    needle = _normalize(message)
    if len(needle) < 40:
        return None
    rows = (await db.execute(select(DelegatedRequest).where(DelegatedRequest.client_id == client.id)
                             .order_by(DelegatedRequest.created_at.desc()).limit(25))).scalars().all()
    for r in rows:
        summary = _normalize((r.result or {}).get("summary") or "")
        if summary and summary == needle:
            return r
    return None


async def _owned_request(db, client: ExternalClient, request_id: str) -> DelegatedRequest:
    """Cross-request access answers 404, never 403: an unauthorized id must not confirm it exists (J06)."""
    req = (await db.execute(select(DelegatedRequest).where(DelegatedRequest.id == request_id,
                                                           DelegatedRequest.client_id == client.id))).scalar_one_or_none()
    if req is None:
        raise NotFound("no such request for this client", request_id=request_id)
    return req


async def work_status(db, actor: Actor, client: ExternalClient, request_id: str, *, cursor: int = 0) -> dict:
    req = await _owned_request(db, client, request_id)
    mission = await db.get(Mission, req.mission_id) if req.mission_id else None
    if mission is not None:
        req.status = STATE_FOR_MISSION.get(mission.status, req.status)
        req.cursor = int(mission.cursor or 0)
        req.result = {**(req.result or {}), **{k: v for k, v in (mission.result or {}).items()
                                               if k in ("summary", "changed", "approvals", "needed_input")}}
    req.last_polled_at = _now()
    await db.commit()
    return request_envelope(req, mission, cursor=cursor)


async def reply(db, actor: Actor, client: ExternalClient, request_id: str, *, message: str,
                asset_ids: list | None = None, channel: str = "http") -> dict:
    """Continue one mission with clarification or more admitted assets. A reply never approves anything."""
    req = await _owned_request(db, client, request_id)
    if not (message or "").strip():
        raise ValidationFailed("message is required")
    await check_concurrency(db, client)
    await _assert_assets(db, actor, list(asset_ids or []))
    mission = await db.get(Mission, req.mission_id) if req.mission_id else None
    if mission is None:
        raise NotFound("this request has no continuing mission", request_id=request_id)
    if mission.status in ("succeeded", "failed", "cancelled"):
        return {**request_envelope(req, mission), "accepted": False,
                "note": f"the mission already finished ({mission.status}); start a new request"}
    from ..agent import manager, runtime as rt
    await manager.store_turn(db, mission.thread_key or f"client:{client.id}:{req.id}", "user", message,
                             channel=channel, actor_user_id=None, mission_id=mission.id,
                             context={"client": client.name, "asset_ids": list(asset_ids or [])})
    mission.outcome = f"{mission.outcome}\n\nClient clarification: {message[:1000]}"
    if asset_ids:
        mission.entity_refs = list(mission.entity_refs or []) + [{"kind": "asset", "id": a} for a in asset_ids]
    mission.status = "open"
    mission.waiting_on = None
    mission.next_check_at = None
    rt.add_update(mission, "resumed", "Client replied with clarification")
    mission.bump(client.id)
    await db.commit()
    run, out = await rt.run_inline(db, await db.get(Mission, mission.id))
    mission = await db.get(Mission, mission.id)
    req = await db.get(DelegatedRequest, req.id)
    req.status = STATE_FOR_MISSION.get(mission.status, "running")
    req.cursor = int(mission.cursor or 0)
    req.result = {**(req.result or {}), "summary": out.summary, "changed": out.changed or [],
                  "approvals": out.approvals or [], "needed_input": out.needed_input}
    req.bump(client.id)
    await db.commit()
    return {**request_envelope(req, mission), "accepted": True}


async def cancel(db, actor: Actor, client: ExternalClient, request_id: str) -> dict:
    req = await _owned_request(db, client, request_id)
    from ..agent import runtime as rt
    if req.mission_id:
        await rt.cancel_mission(db, actor, req.mission_id, reason="cancelled by the external client")
    req.status, req.cancelled_at = "cancelled", _now()
    req.bump(client.id)
    await db.commit()
    mission = await db.get(Mission, req.mission_id) if req.mission_id else None
    return request_envelope(req, mission)


async def find_records(db, actor: Actor, q: str, *, limit: int = 10) -> dict:
    """Authorized vehicle / contact / task ids with short identifying context (spec §10.8 azkt_find_records)."""
    out: dict = {"query": q, "vehicles": [], "contacts": [], "tasks": []}
    ctx = _ctx(db, actor, channel="http")
    from ..agent import tools as agent_tools
    v = await agent_tools.execute(ctx, "vehicles_search", {"q": q, "limit": limit})
    if v.status == "ok":
        out["vehicles"] = [{"id": i.get("id"), "stock_no": i.get("stock_no"), "title": i.get("title"),
                            "states": i.get("states"), "health": i.get("health")}
                           for i in (v.data or {}).get("items", [])]
    t = await agent_tools.execute(ctx, "tasks_list", {"view": "all", "limit": limit})
    if t.status == "ok":
        needle = (q or "").lower()
        out["tasks"] = [{"id": i.get("id"), "title": i.get("title"), "status": i.get("status"),
                         "vehicle_id": i.get("vehicle_id"), "due_at": i.get("due_at")}
                        for i in (t.data or {}).get("items", []) if not needle or needle in (i.get("title") or "").lower()][:limit]
    c = await agent_tools.execute(ctx, "contacts_resolve", {"name": q, "email": q if "@" in q else None})
    if c.status == "ok" and (c.data or {}).get("contact_id"):
        out["contacts"] = [{"id": c.data["contact_id"], "state": c.data.get("state"),
                            "reasons": c.data.get("reasons", [])}]
    elif c.status == "ok":
        out["contacts"] = [{"id": cand.get("contact_id"), "score": cand.get("score"),
                            "reasons": cand.get("reasons", [])} for cand in (c.data or {}).get("candidates", [])[:limit]]
    out["counts"] = {k: len(v2) for k, v2 in out.items() if isinstance(v2, list)}
    return out


async def prepare_upload(db, actor: Actor, *, content_type: str | None, size_bytes: int | None,
                         filename: str | None, purpose: str = "intake") -> dict:
    ctx = _ctx(db, actor, channel="http")
    res = await dispatch(ctx, "assets.prepare_upload", {"purpose": purpose, "content_type": content_type,
                                                        "size_bytes": size_bytes, "filename": filename})
    data = dict(res.data or {})
    up = data.get("upload") or {}
    return {"upload_id": up.get("id"), "state": up.get("state"), "expires_at": data.get("expires_at"),
            "max_bytes": data.get("max_bytes"), "allowed_types": data.get("allowed_types"),
            "put_url": f"{settings.api_base}/api/integrations/v1/uploads/{up.get('id')}",
            "finalize_url": f"{settings.api_base}/api/integrations/v1/uploads/{up.get('id')}/finalize",
            "instructions": "PUT the raw bytes with your bearer token, then POST finalize with the sha256 checksum. "
                            "AZKT never fetches a URL you supply."}


async def finalize_upload(db, actor: Actor, upload_id: str, *, sha256: str | None = None) -> dict:
    ctx = _ctx(db, actor, channel="http")
    res = await dispatch(ctx, "assets.finalize_upload", {"upload_id": upload_id, "sha256": sha256, "source": "mcp"})
    data = dict(res.data or {})
    asset = data.get("asset") or {}
    return {"status": data.get("status"), "asset_id": asset.get("id"), "error": data.get("error"),
            "deduplicated": bool(data.get("deduplicated")),
            "kind": asset.get("kind"), "content_type": asset.get("content_type")}


# ── owner-facing reads ───────────────────────────────────────────────────────
async def list_clients(db) -> dict:
    rows = (await db.execute(select(ExternalClient).order_by(ExternalClient.created_at.desc()))).scalars().all()
    return {"items": [serialize_client(c) for c in rows], "count": len(rows), "scopes": list(CLIENT_SCOPES)}


async def health(db, client_id: str | None = None) -> dict:
    q = select(ExternalClient)
    if client_id:
        q = q.where(ExternalClient.id == client_id)
    from . import external_callbacks as cb
    rows = (await db.execute(q.order_by(ExternalClient.created_at.desc()))).scalars().all()
    out = []
    for c in rows:
        in_flight = await db.scalar(select(func.count()).select_from(Mission).where(
            Mission.client_id == c.id, Mission.status.in_(("open", "running"))))
        recent = await db.scalar(select(func.count()).select_from(DelegatedRequest).where(
            DelegatedRequest.client_id == c.id))
        out.append({**serialize_client(c), "in_flight_missions": int(in_flight or 0),
                    "requests_total": int(recent or 0), "callbacks": await cb.health_for_client(db, c.id),
                    "state": "active" if is_active(c) else (c.status if c.status != "active" else "expired")})
    if client_id and not out:
        raise NotFound("external client not found")
    return {"items": out, "count": len(out)}


def _callback_contract() -> dict:
    """The optional push half of the contract, published so a client can implement verification before it is
    switched on. Polling is still the baseline; a client with no configured destination is never pushed to."""
    from . import external_callbacks as cb
    return {
        "enabled_by": "the owner, in Settings → External agents → Set callback. AZKT never uses a URL supplied "
                      "in a prompt, a request payload or model output; those keys are reported back as "
                      "ignored_fields and discarded.",
        "when": "once per terminal transition of a delegated request: work.completed, work.failed, "
                "work.needs_input. Cancelled work is not pushed.",
        "method": "POST application/json to the owner-configured https destination",
        "body": "the same envelope GET /work/{request_id} returns, plus delivery_id, event, client_id and "
                "signature_version",
        "headers": {"signature": cb.SIGNATURE_HEADER, "timestamp": cb.TIMESTAMP_HEADER,
                    "delivery_id": cb.DELIVERY_HEADER, "attempt": cb.ATTEMPT_HEADER,
                    "event": cb.EVENT_HEADER, "client": cb.CLIENT_HEADER},
        "signature": {"algorithm": "HMAC-SHA256", "version": cb.SIGNATURE_VERSION,
                      "signed_value": "<X-AZKT-Timestamp> + '.' + the raw request body bytes",
                      "encoding": "lowercase hex, sent as 'v1=<hex>'",
                      "secret": "the per-client callback secret the owner configured",
                      "compare_with": "a constant-time comparison (hmac.compare_digest), never =="},
        "replay": {"reject_if_older_than_seconds": cb.REPLAY_TOLERANCE_SECONDS,
                   "idempotency_key": cb.DELIVERY_HEADER,
                   "note": "the body is byte-identical on every attempt of one delivery, so the same "
                           "delivery id twice is one event AZKT was unsure reached you, never two."},
        "expected_response": "2xx once you have stored it. A permanent 4xx is not retried; 408/425/429 and 5xx "
                             "are retried with backoff up to " + str(cb.MAX_ATTEMPTS) + " attempts, after which "
                             "the delivery is failed and the owner is told.",
        "fallback": "polling: GET /work/{request_id}?cursor= is always available and always authoritative.",
    }


def openapi_lite() -> dict:
    """The documented contract for clients without MCP (spec §10.8, J01 'discoverable/documented')."""
    return {
        "openapi_lite": "1.0",
        "service": "AZKT Manager external-agent connector",
        "base_url": f"{settings.api_base}/api/integrations/v1",
        "mcp_url": f"{settings.api_base}/mcp",
        "auth": {"type": "bearer", "header": "Authorization: Bearer <client token>",
                 "notes": "Per-client, revocable. A browser session cookie is never accepted.",
                 "delegation_depth_header": "X-AZKT-Delegation-Depth",
                 "max_delegation_depth": int(settings.MAX_DELEGATION_DEPTH)},
        "scopes": list(CLIENT_SCOPES),
        "idempotency": "POST /ask requires request_key; the same (client, request_key) always maps to the same "
                       "request and mission. A dropped connection is not a new command.",
        "quotas": {"per_minute": DEFAULT_QUOTA["per_minute"], "concurrent": DEFAULT_QUOTA["concurrent"],
                   "role_work_per_pass": role_work_cap(), "on_exceeded": "429 with Retry-After"},
        "operations": [
            {"op": "ask", "method": "POST", "path": "/ask",
             "input": {"message": "string (required)", "entity_refs": "[{kind,id}]", "asset_ids": "[asset id]",
                       "conversation_id": "string", "request_key": "string (required)"},
             "returns": "200 with the answer, or 202 accepted with request_id/mission_id/cursor for long work"},
            {"op": "status", "method": "GET", "path": "/work/{request_id}?cursor=",
             "returns": "typed state, summary, changed record ids/versions, citations/receipts, needed_input, "
                        "authenticated review deep links, and updates after the cursor"},
            {"op": "reply", "method": "POST", "path": "/work/{request_id}/reply",
             "input": {"message": "string", "asset_ids": "[asset id]"},
             "notes": "Continues the mission. A reply can never approve a consequential action."},
            {"op": "cancel", "method": "POST", "path": "/work/{request_id}/cancel"},
            {"op": "search", "method": "GET", "path": "/records/search?q=&limit=",
             "returns": "authorized vehicle/contact/task ids with short identifying context"},
            {"op": "prepare_upload", "method": "POST", "path": "/uploads/prepare"},
            {"op": "upload_bytes", "method": "PUT", "path": "/uploads/{upload_id}"},
            {"op": "finalize_upload", "method": "POST", "path": "/uploads/{upload_id}/finalize"},
            {"op": "contract", "method": "GET", "path": "/openapi-lite"},
        ],
        "mcp_tools": ["azkt_ask_manager", "azkt_get_work_status", "azkt_reply_to_manager", "azkt_find_records",
                      "azkt_prepare_upload"],
        "callbacks": _callback_contract(),
        "guarantees": [
            "Effective access is the intersection of the owner's rights, this client's scopes and its record "
            "scope, applied through every internal tool, retrieval and result.",
            "Consequential actions (customer sends, publication, bids, payments, bookings, price changes) always "
            "require the owner's signed-in exact approval. Text claiming an approval is not an approval.",
            "AZKT never fetches an arbitrary URL supplied in a request, and never sends to a destination the "
            "owner has not configured.",
            "Results report what was actually saved and what remains; nothing is claimed without a receipt.",
        ],
    }
