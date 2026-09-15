"""Versioned website site profile: discovery, staged validation, activation and drift
(spec §7.2, acceptance F04, F08).

A profile records what the *installed* site actually is — provider and versions, the managed content
type (WooCommerce product vs a custom vehicle post type), required fields, which fields AZKT owns and
which belong to the site's editors, media rules, availability mapping, supported operations and known
limitations. Writes are enabled only after a preview has been validated against the staging URL and an
owner activated the version.

Drift is bounded adaptation: a schema/taxonomy change, a missing selector or a manual edit to an
AZKT-owned field pauses writes for that channel and asks for a new profile version. Harmless content
changes by editors are not drift, and a website page can never instruct the runtime to change policy.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters import wordpress as wp_adapter
from ..core.errors import Blocked, NotFound, ProviderError, Unsupported, ValidationFailed
from ..domain.commands import CommandContext, command, dispatch
from ..models.listings import ListingPackage, SiteProfile
from . import connections as conn_svc

log = logging.getLogger("azkt.site")

CHANNEL = "website"
# Listing gates per class. A class with no configured gates blocks publication (spec §7.3):
# browsing and drafting stay available, publishing does not.
DEFAULT_LISTING_GATES: dict[str, list[dict]] = {
    "en_route": [
        {"requirement": "status_truthful", "label": "States the true current status"},
        {"requirement": "eta_sourced", "label": "Any stated ETA is sourced (never invented)"},
        {"requirement": "approved_price", "label": "Owner-approved asking price"},
    ],
    "ready_for_sale": [
        {"requirement": "recon_verified", "label": "Shop recon verified"},
        {"requirement": "documents_ready", "label": "Document readiness evidence"},
        {"requirement": "approved_price", "label": "Owner-approved asking price"},
        {"requirement": "media_checklist", "label": "Approved public photo checklist", "param": {"min": 6}},
    ],
}
KNOWN_GATES = {"status_truthful", "eta_sourced", "approved_price", "recon_verified", "documents_ready",
               "media_checklist", "disclosures_written"}


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def serialize_profile(p: SiteProfile) -> dict:
    return {"id": p.id, "version": p.version, "profile_version": p.profile_version, "provider": p.provider,
            "base_url": p.base_url, "staging_url": p.staging_url, "content_type": p.content_type,
            "status": p.status, "discovered": dict(p.discovered or {}), "field_map": dict(p.field_map or {}),
            "field_ownership": dict(p.field_ownership or {}), "media_rules": dict(p.media_rules or {}),
            "availability_map": dict(p.availability_map or {}), "validation": dict(p.validation or {}),
            "supported_ops": list(p.supported_ops or []), "limitations": list(p.limitations or []),
            "listing_gates": dict(p.listing_gates or {}), "drift": dict(p.drift or {}),
            "writes_paused": bool(p.writes_paused), "pause_reason": p.pause_reason,
            "preview": dict(p.preview or {}), "connection_id": p.connection_id,
            "validated_at": iso(p.validated_at), "activated_at": iso(p.activated_at), "activated_by": p.activated_by,
            "discovered_at": iso(p.discovered_at), "drift_detected_at": iso(p.drift_detected_at),
            "created_at": iso(p.created_at)}


async def active_profile(db: AsyncSession, *, channel: str = CHANNEL) -> SiteProfile | None:
    return (await db.execute(select(SiteProfile).where(SiteProfile.status.in_(("active", "drift")))
                             .order_by(SiteProfile.profile_version.desc()))).scalars().first()


async def latest_profile(db: AsyncSession) -> SiteProfile | None:
    return (await db.execute(select(SiteProfile).order_by(SiteProfile.profile_version.desc()))).scalars().first()


def profile_dict(p: SiteProfile | None) -> dict:
    """The adapter-facing view of a profile (what render/validate/availability read)."""
    if p is None:
        return {}
    d = serialize_profile(p)
    d["rest_base"] = (p.discovered or {}).get("custom_post_type") or "posts"
    return d


def gates_for(profile: SiteProfile | None, listing_class: str) -> tuple[list[dict], str | None]:
    """Configured gates for a listing class, or (\\[], reason) when they are not configured."""
    configured = dict((profile.listing_gates if profile else None) or {})
    if not configured:
        configured = DEFAULT_LISTING_GATES
    if listing_class not in configured:
        return [], f"no gates are configured for listing class {listing_class!r}"
    rules = configured.get(listing_class)
    if rules is None:
        return [], f"gates for listing class {listing_class!r} are explicitly unconfigured"
    unknown = [r.get("requirement") for r in rules if r.get("requirement") not in KNOWN_GATES]
    if unknown:
        return list(rules), f"unconfigured gate requirement(s): {', '.join(str(u) for u in unknown)}"
    return list(rules), None


async def website_adapter(db: AsyncSession, *, staging: bool = False):
    wp_conn = await conn_svc.get(db, "wordpress")
    woo_conn = await conn_svc.get(db, "woocommerce")
    return wp_adapter.adapter_for(wp_conn, woo_conn, staging=staging)


# ── discover ─────────────────────────────────────────────────────────────────
class DiscoverIn(BaseModel):
    base_url: str | None = None
    staging_url: str | None = None
    note: str | None = None


def _ownership(discovered: dict) -> dict:
    owned = {f: "azkt" for f in wp_adapter.AZKT_FIELDS}
    for slug in (discovered.get("post_types") or {}):
        owned.setdefault(f"post_type:{slug}", "site")
    owned["categories"] = "site"
    owned["tags"] = "site"
    owned["seo"] = "site"
    owned["theme_settings"] = "site"
    owned["payment_tax_shipping"] = "site"
    return owned


@command("site.discover", input=DiscoverIn, perm="connections", action_class="owner_only",
         approval_kind="site_profile", summary=lambda p: f"Discover website schema at {p.base_url or 'the configured site'}",
         description="Inspect the installed site (REST namespaces and versions, product vs custom vehicle post type, "
                     "auth capabilities, taxonomy, media) and store a new site profile draft. Reads only.")
async def site_discover(ctx: CommandContext, inp: DiscoverIn) -> dict:
    ad = await website_adapter(ctx.db)
    try:
        discovered = await ad.discover()
    except (ProviderError, Unsupported) as e:
        raise Blocked(f"website discovery failed: {e}", kind=wp_adapter.error_kind(e) if isinstance(e, ProviderError) else "unsupported")
    prior = await latest_profile(ctx.db)
    content_type = discovered.get("content_type") or "product"
    base_url = inp.base_url or (discovered.get("site") or {}).get("url") or getattr(ad, "base_url", "")
    validation = {"required_fields": ["headline", "body"], "sku_convention": None}
    if content_type == "product":
        validation["sku_convention"] = "stock_no"
    wp_conn = await conn_svc.get(ctx.db, "wordpress")
    staging = (inp.staging_url or (prior.staging_url if prior else None)
               or ((wp_conn.config or {}).get("staging_url") if wp_conn is not None else None))
    p = SiteProfile(connection_id=wp_conn.id if wp_conn is not None else None,
                    provider="woocommerce" if content_type == "product" else "wordpress",
                    base_url=base_url, staging_url=staging,
                    profile_version=(prior.profile_version + 1) if prior else 1, content_type=content_type,
                    discovered=discovered, field_map=wp_adapter.field_map_for({"content_type": content_type}),
                    field_ownership=_ownership(discovered),
                    # the site's own minimum (WooCommerce requires none); the *listing* photo checklist
                    # lives in the class gates, where a missing shot blocks publication.
                    media_rules={"min": 0, "formats": ["image/jpeg", "image/png", "image/webp"],
                                 "checksum_mapping": True, "orphan_review": True},
                    availability_map=wp_adapter.availability_map_for({"content_type": content_type}),
                    validation=validation, supported_ops=list(discovered.get("supported_ops") or []),
                    limitations=list(discovered.get("limitations") or []),
                    listing_gates=dict((prior.listing_gates if prior else None) or DEFAULT_LISTING_GATES),
                    status="draft", discovered_at=ctx.now, preview={},
                    created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
    ctx.db.add(p)
    await ctx.db.flush()
    ctx.changed.append({"kind": "site_profile", "id": p.id, "version": p.version})
    changes = _schema_delta(prior, p) if prior else []
    ctx.record(f"Website profile v{p.profile_version} discovered: {content_type} on {base_url}",
               entity_kind="site_profile", entity_id=p.id, kind="connection", state="draft", visibility="owner",
               details={"content_type": content_type, "limitations": p.limitations, "changes": changes,
                        "vehicle_post_types": discovered.get("vehicle_post_types"), "note": inp.note})
    ctx.emit("site_profile.changed", aggregate_type="site_profile", aggregate_id=p.id,
             payload={"status": "draft", "profile_version": p.profile_version, "changes": changes})
    return {"profile": serialize_profile(p), "changes": changes,
            "next": "preview one draft package on the staging URL, then activate"}


def _schema_delta(prior: SiteProfile, current: SiteProfile) -> list[str]:
    out = []
    if prior.content_type != current.content_type:
        out.append(f"managed content type changed {prior.content_type} → {current.content_type}")
    pa = {c.get("slug") for c in (prior.discovered or {}).get("product_attributes") or []}
    ca = {c.get("slug") for c in (current.discovered or {}).get("product_attributes") or []}
    if pa - ca:
        out.append(f"product attributes removed: {sorted(pa - ca)}")
    pc = {c.get("slug") for c in (prior.discovered or {}).get("product_categories") or []}
    cc = {c.get("slug") for c in (current.discovered or {}).get("product_categories") or []}
    if pc - cc:
        out.append(f"categories removed: {sorted(pc - cc)}")
    pn = set((prior.discovered or {}).get("namespaces") or [])
    cn = set((current.discovered or {}).get("namespaces") or [])
    if pn - cn:
        out.append(f"REST namespaces removed: {sorted(pn - cn)}")
    prev_auth = ((prior.discovered or {}).get("auth") or {}).get("woocommerce") or {}
    cur_auth = ((current.discovered or {}).get("auth") or {}).get("woocommerce") or {}
    if prev_auth.get("write") and not cur_auth.get("write"):
        out.append("WooCommerce write capability is no longer proven")
    return out


# ── validate (preview on staging only) ───────────────────────────────────────
class ValidateIn(BaseModel):
    profile_id: str
    package_id: str | None = None
    staging_url: str | None = None


@command("site.validate", input=ValidateIn, perm="listings.draft", action_class="internal",
         description="Render one draft package against the staging URL and store the preview on the profile. "
                     "No write reaches the site; a profile without a passing preview can never publish (F04).")
async def site_validate(ctx: CommandContext, inp: ValidateIn) -> dict:
    p = await ctx.db.get(SiteProfile, inp.profile_id)
    if p is None:
        raise NotFound("site profile not found")
    if p.status == "superseded":
        raise Blocked("profile is superseded; discover a new version")
    staging = inp.staging_url or p.staging_url
    if not staging:
        raise Blocked("no staging URL is configured for preview; set one before validating",
                      setup_blocked="wordpress.staging_url")
    if staging.rstrip("/") == (p.base_url or "").rstrip("/"):
        raise ValidationFailed("the staging URL must differ from the live site; a preview never touches production")
    pkg = None
    if inp.package_id:
        pkg = await ctx.db.get(ListingPackage, inp.package_id)
        if pkg is None:
            raise NotFound("listing package not found")
    package = _package_for_preview(pkg)
    p.staging_url = staging
    ad = await website_adapter(ctx.db, staging=True)
    prof = {**profile_dict(p), "staging_url": staging}
    try:
        preview = await ad.preview(package, prof)
    except (ProviderError, Unsupported) as e:
        raise Blocked(f"preview failed: {e}")
    p.preview = {"at": ctx.now.isoformat(), "staging_url": staging, "package_id": inp.package_id,
                 "ok": bool(preview.get("ok")), "errors": list(preview.get("errors") or []),
                 "warnings": list(preview.get("warnings") or []), "payload": preview.get("payload") or {},
                 "written": False}
    p.validation = {**(p.validation or {}), "last_preview_ok": bool(preview.get("ok"))}
    if preview.get("ok"):
        p.status = "validated" if p.status in ("draft", "validated") else p.status
        p.validated_at = ctx.now
    ctx.touch(p, "site_profile")
    ctx.record(f"Website profile v{p.profile_version} previewed on staging: "
               f"{'passed' if preview.get('ok') else 'failed'}", entity_kind="site_profile", entity_id=p.id,
               kind="connection", state=p.status, visibility="owner",
               details={"errors": preview.get("errors"), "warnings": preview.get("warnings"), "staging_url": staging})
    return {"profile": serialize_profile(p), "preview": p.preview}


def _package_for_preview(pkg: ListingPackage | None) -> dict:
    if pkg is None:
        return {"headline": "Preview listing", "body": "Rendered preview only — nothing is written.",
                "short_description": "Preview", "price": "0.00", "currency": "USD", "media": [],
                "availability": "available", "specs": [], "disclosures": [], "package_hash": "preview",
                "vehicle_id": None, "status": "draft"}
    return package_payload(pkg)


def package_payload(pkg: ListingPackage) -> dict:
    return {"headline": pkg.headline, "body": pkg.body, "short_description": pkg.short_description,
            "price": str(pkg.price) if pkg.price is not None else None, "currency": pkg.currency,
            "media": list(pkg.media or []), "availability": pkg.availability, "specs": list(pkg.specs or []),
            "disclosures": list(pkg.disclosures or []), "package_hash": pkg.package_hash,
            "vehicle_id": pkg.vehicle_id, "sku": (pkg.evidence or {}).get("sku"), "status": "draft"}


# ── activate ─────────────────────────────────────────────────────────────────
class ProfileRefIn(BaseModel):
    profile_id: str
    note: str | None = None
    expected_version: int | None = None


@command("site.activate", input=ProfileRefIn, perm="connections", action_class="owner_only",
         approval_kind="site_profile", summary=lambda p: f"Activate site profile {p.profile_id[:8]}",
         description="Owner activates a validated profile version as the write authority; the previous version is "
                     "superseded and any paused writes for this channel resume.")
async def site_activate(ctx: CommandContext, inp: ProfileRefIn) -> dict:
    p = await ctx.db.get(SiteProfile, inp.profile_id)
    if p is None:
        raise NotFound("site profile not found")
    if inp.expected_version is not None and p.version != inp.expected_version:
        raise Blocked("profile changed since you loaded it", current_version=p.version)
    if p.status == "active":
        return {"profile": serialize_profile(p), "activated": False}
    if p.status != "validated" or not (p.preview or {}).get("ok"):
        raise Blocked("preview the profile on staging before activating it (F04)", status=p.status)
    others = (await ctx.db.execute(select(SiteProfile).where(SiteProfile.status.in_(("active", "drift")),
                                                              SiteProfile.id != p.id))).scalars().all()
    for o in others:
        o.status = "superseded"
        o.bump(ctx.actor.user_id)
    p.status = "active"
    p.writes_paused = False
    p.pause_reason = None
    p.activated_at = ctx.now
    p.activated_by = ctx.actor.user_id
    ctx.touch(p, "site_profile")
    ctx.record(f"Website profile v{p.profile_version} activated ({p.content_type})", entity_kind="site_profile",
               entity_id=p.id, kind="connection", state="active", visibility="owner",
               details={"superseded": [o.id for o in others], "note": inp.note})
    ctx.emit("site_profile.changed", aggregate_type="site_profile", aggregate_id=p.id,
             payload={"status": "active", "profile_version": p.profile_version})
    return {"profile": serialize_profile(p), "activated": True, "superseded": [o.id for o in others]}


# ── drift (F08) ──────────────────────────────────────────────────────────────
MANAGED_COMPARE = (("title", "title"), ("price", "price"), ("body", "body"),
                   ("short_description", "short_description"), ("stock_status", "stock_status"),
                   ("visibility", "visibility"))


def compare_managed(expected_payload: dict, observed: dict, profile: SiteProfile | None) -> dict:
    """Which AZKT-owned fields differ from what AZKT last wrote, and which editor-owned fields exist."""
    fmap = wp_adapter.field_map_for(profile_dict(profile))
    mismatches: list[dict] = []
    for logical, observed_key in MANAGED_COMPARE:
        target = fmap.get(logical, logical)
        expected = expected_payload.get(target)
        if expected is None:
            continue
        got = observed.get(observed_key)
        if got is None:
            continue
        if str(expected).strip() != str(got).strip():
            mismatches.append({"field": logical, "expected": str(expected), "observed": str(got)})
    editor_fields = sorted(k for k in (observed.get("meta") or {}) if not str(k).startswith("azkt_"))
    return {"mismatches": mismatches, "editor_fields": editor_fields,
            "edited_by": (observed.get("raw") or {}).get("_edited_by")}


async def record_drift(ctx: CommandContext, profile: SiteProfile, *, reasons: list[str], source: str,
                       detail: dict | None = None, channel: str = CHANNEL) -> dict:
    """Pause writes for this channel and ask for a new profile version (spec §7.2, F08)."""
    if not reasons:
        return {"drift": False}
    profile.status = "drift"
    profile.writes_paused = True
    profile.pause_reason = "; ".join(reasons)[:500]
    profile.drift_detected_at = ctx.now
    profile.drift = {"at": ctx.now.isoformat(), "source": source, "reasons": reasons, "detail": detail or {},
                     "channel": channel}
    ctx.touch(profile, "site_profile")
    ctx.record(f"Website writes paused — profile drift: {profile.pause_reason}", entity_kind="site_profile",
               entity_id=profile.id, kind="connection", state="drift", visibility="owner", exception=True,
               details={"source": source, **(detail or {})})
    ctx.emit("site_profile.changed", aggregate_type="site_profile", aggregate_id=profile.id,
             payload={"status": "drift", "reasons": reasons, "channel": channel})
    try:
        await dispatch(ctx.child(), "tasks.create", {
            "title": "Review website profile drift and re-validate",
            "type": "operational", "priority": "high",
            "notes": profile.pause_reason,
            "source_kind": "site_profile", "source_id": profile.id,
            "extra": {"channel": channel, "profile_id": profile.id, "reasons": reasons}}, commit=False)
    except Exception as e:  # noqa: BLE001 - a task failure must not hide the pause
        log.warning("drift task could not be created: %s", e)
    return {"drift": True, "reasons": reasons, "profile_id": profile.id}


class ResumeIn(BaseModel):
    profile_id: str
    note: str | None = None


@command("site.resume_writes", input=ResumeIn, perm="connections", action_class="owner_only",
         approval_kind="site_profile", summary=lambda p: f"Resume website writes for profile {p.profile_id[:8]}",
         description="Owner resumes writes after reviewing drift. Only a profile whose current preview passes may "
                     "resume; the drift record is kept.")
async def site_resume_writes(ctx: CommandContext, inp: ResumeIn) -> dict:
    p = await ctx.db.get(SiteProfile, inp.profile_id)
    if p is None:
        raise NotFound("site profile not found")
    if not p.writes_paused:
        return {"profile": serialize_profile(p), "resumed": False}
    if not (p.preview or {}).get("ok"):
        raise Blocked("re-validate the profile on staging before resuming writes")
    p.writes_paused = False
    p.status = "active"
    p.pause_reason = None
    p.drift = {**(p.drift or {}), "resolved_at": ctx.now.isoformat(), "resolved_by": ctx.actor.user_id,
               "note": inp.note}
    ctx.touch(p, "site_profile")
    ctx.record(f"Website writes resumed for profile v{p.profile_version}", entity_kind="site_profile",
               entity_id=p.id, kind="connection", state="active", visibility="owner", details={"note": inp.note})
    ctx.emit("site_profile.changed", aggregate_type="site_profile", aggregate_id=p.id,
             payload={"status": "active", "resumed": True})
    return {"profile": serialize_profile(p), "resumed": True}


class GatesIn(BaseModel):
    profile_id: str
    listing_gates: dict = Field(default_factory=dict)


@command("site.set_listing_gates", input=GatesIn, perm="settings", action_class="owner_only",
         approval_kind="site_profile", summary=lambda p: f"Configure listing gates on profile {p.profile_id[:8]}",
         description="Configure the publication gates per listing class. An unknown or missing gate blocks "
                     "publication for that class; it is never skipped (spec §7.3).")
async def site_set_listing_gates(ctx: CommandContext, inp: GatesIn) -> dict:
    p = await ctx.db.get(SiteProfile, inp.profile_id)
    if p is None:
        raise NotFound("site profile not found")
    for cls, rules in (inp.listing_gates or {}).items():
        if rules is None:
            continue
        if not isinstance(rules, list):
            raise ValidationFailed(f"gates for {cls} must be a list of requirements")
    p.listing_gates = dict(inp.listing_gates or {})
    ctx.touch(p, "site_profile")
    ctx.record(f"Listing gates configured on profile v{p.profile_version}", entity_kind="site_profile",
               entity_id=p.id, kind="connection", state=p.status, visibility="owner",
               details={"classes": sorted((inp.listing_gates or {}).keys())})
    return {"profile": serialize_profile(p)}


# ── reads ────────────────────────────────────────────────────────────────────
async def profile_overview(db: AsyncSession) -> dict:
    rows = (await db.execute(select(SiteProfile).order_by(SiteProfile.profile_version.desc()))).scalars().all()
    active = next((r for r in rows if r.status in ("active", "drift")), None)
    return {"active": serialize_profile(active) if active else None,
            "versions": [serialize_profile(r) for r in rows[:20]],
            "writes_paused": bool(active.writes_paused) if active else True,
            "reason": (active.pause_reason if active else "no active site profile — discover and validate one first"),
            "listing_gates": dict((active.listing_gates if active else None) or DEFAULT_LISTING_GATES)}


def writable(profile: SiteProfile | None) -> tuple[bool, str | None]:
    if profile is None:
        return False, "no site profile has been discovered and activated"
    if profile.status != "active":
        return False, f"site profile is {profile.status}" + (f" — {profile.pause_reason}" if profile.pause_reason else "")
    if profile.writes_paused:
        return False, profile.pause_reason or "writes are paused for this channel"
    if not (profile.preview or {}).get("ok"):
        return False, "the active profile has no passing staging preview (F04)"
    return True, None
