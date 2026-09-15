"""Listing package lifecycle and channel publication (spec §7.3, acceptance F05–F10, K04).

A package is a canonical, versioned, hashed statement about one vehicle: class (en-route /
ready for sale), headline, body, approved price, specifications *with evidence*, required disclosures,
ordered approved media, availability, the site profile version it was built against, and a diff.

Rules enforced here rather than prompted:

* Specifications come from recorded facts (confirmed or reported, labelled). Nothing is invented — no
  A/C, mileage, condition, warranty or arrival date. An en-route listing states the true status and
  only ever repeats an ETA that has a source (F09).
* Media are approved, public-eligible photos in order. Intake/importer photos need explicit
  eligibility; sensitive documents can never enter the set (F02).
* Gates are configured per listing class on the site profile. A gate that is not configured blocks
  publication — it is never skipped.
* Publication is an external effect: an exact approval binds package hash + profile version + channel,
  and is revalidated immediately before execution. The effect itself is an ExternalAction executed once
  (dedupe on package + version + channel).
* `Accepted`, `Published` and `Verified` are different outcomes. A lost response after the site
  accepted the write is `unknown` and is reconciled by mapping before any retry — never a second post
  (F06). A stale public page is `pending_verification`, never a false `verified` (F07).
* Reservation/sale updates the desired availability immediately, cancels incompatible queued
  publications, and keeps a cleanup task until the channel is verified (F10). Channels without an
  adapter are `unsupported` with a manual checklist, never a fake success.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters import wordpress as wp_adapter
from ..adapters.model import ModelClient, ModelRefused, ModelUnavailable, available as model_available
from ..core.errors import Blocked, DomainError, NotFound, ProviderError, Unsupported, ValidationFailed
from ..core.ids import stable_hash
from ..domain import jobs
from ..domain.actors import SYSTEM_ACTOR
from ..domain.commands import CommandContext, command, dispatch
from ..domain.events import on_event
from ..domain.jobs import sweep
from ..models.assets import Asset, AssetLink
from ..models.listings import ListingPackage, Publication, SiteMedia, SiteProfile
from ..models.runtime import ExternalAction
from ..models.vehicles import ReconIssue, Vehicle, VehicleFact, VehicleMilestone
from . import approvals as approvals_svc
from . import assets as assets_svc
from . import site_profile as site_svc

log = logging.getLogger("azkt.listings")

WEBSITE = "website"
SUPPORTED_CHANNELS = (WEBSITE,)
LISTING_CLASSES = ("en_route", "ready_for_sale")
EN_ROUTE_STATES = ("candidate", "purchased", "export_pending", "on_vessel", "at_port", "released", "domestic_transit")
VERIFY_MAX_ATTEMPTS = 3
VERIFY_SECONDS = 15 * 60
# Routine availability scan across everything currently live on the site (spec §12.3). The verify job
# does the reading, so the scan only has to decide which publications are due.
AVAILABILITY_SCAN_SECONDS = 60 * 60
LIVE_STATES = ("published", "verified", "mismatch")
DATE_LIKE = re.compile(r"\b(?:\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2})\b",
                       re.IGNORECASE)
STATUS_PHRASES = {
    "candidate": "not yet purchased", "purchased": "purchased in Japan, not yet shipped",
    "export_pending": "awaiting export clearance in Japan", "on_vessel": "on the vessel",
    "at_port": "at the US port", "released": "released from the port",
    "domestic_transit": "in domestic transit", "received": "received at our shop",
}


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def now() -> datetime:
    return datetime.now(timezone.utc)


# ── serialization ────────────────────────────────────────────────────────────
def serialize_package(p: ListingPackage) -> dict:
    return {"id": p.id, "version": p.version, "vehicle_id": p.vehicle_id, "package_version": p.package_version,
            "listing_class": p.listing_class, "channel": p.channel, "headline": p.headline, "body": p.body,
            "short_description": p.short_description,
            "price": str(p.price) if p.price is not None else None, "currency": p.currency,
            "specs": list(p.specs or []), "disclosures": list(p.disclosures or []), "media": list(p.media or []),
            "media_detail": list(p.media_detail or []), "availability": p.availability,
            "profile_id": p.profile_id, "profile_version": p.profile_version, "package_hash": p.package_hash,
            "diff": dict(p.diff or {}), "readiness": list(p.readiness or []), "status": p.status,
            "approval_id": p.approval_id, "supersedes_id": p.supersedes_id, "evidence": dict(p.evidence or {}),
            "generated_by": p.generated_by, "blocked_reasons": list(p.blocked_reasons or []), "ready": bool(p.ready),
            "built_at": iso(p.built_at), "created_at": iso(p.created_at)}


def serialize_publication(pub: Publication) -> dict:
    return {"id": pub.id, "version": pub.version, "package_id": pub.package_id, "vehicle_id": pub.vehicle_id,
            "channel": pub.channel, "external_id": pub.external_id, "external_url": pub.external_url,
            "desired_state": pub.desired_state, "observed_state": pub.observed_state, "state": pub.state,
            "external_action_id": pub.external_action_id, "receipt": dict(pub.receipt or {}),
            "media_map": dict(pub.media_map or {}), "last_verified_at": iso(pub.last_verified_at),
            "error": pub.error, "error_kind": pub.error_kind, "manual_task_id": pub.manual_task_id,
            "profile_id": pub.profile_id, "profile_version": pub.profile_version,
            "package_version": pub.package_version, "package_hash": pub.package_hash, "attempts": pub.attempts,
            "verification": dict(pub.verification or {}), "history": list(pub.history or []),
            "unsupported_reason": pub.unsupported_reason, "cleanup_required": bool(pub.cleanup_required)}


def package_payload(pkg: ListingPackage) -> dict:
    """The adapter-facing package (media carry their checksum so media mapping stays stable)."""
    return {"headline": pkg.headline, "body": pkg.body, "short_description": pkg.short_description,
            "price": str(pkg.price) if pkg.price is not None else None, "currency": pkg.currency,
            "media": list(pkg.media_detail or []), "availability": pkg.availability,
            "specs": list(pkg.specs or []), "disclosures": list(pkg.disclosures or []),
            "package_hash": pkg.package_hash, "vehicle_id": pkg.vehicle_id,
            "sku": (pkg.evidence or {}).get("sku"), "status": "draft"}


# ── package building ─────────────────────────────────────────────────────────
async def _vehicle(db: AsyncSession, vehicle_id: str) -> Vehicle:
    v = await db.get(Vehicle, vehicle_id)
    if v is None:
        raise NotFound("vehicle not found")
    return v


async def collect_specs(db: AsyncSession, v: Vehicle) -> list[dict]:
    """Recorded facts only, each labelled with its status and source (spec §7.3)."""
    out: list[dict] = []
    for key, value in (("make", v.make), ("model", v.model), ("model_year", v.model_year), ("color", v.color),
                       ("grade", v.grade)):
        if value not in (None, ""):
            out.append({"key": key, "value": str(value), "status": "recorded", "source": "vehicle record"})
    rows = (await db.execute(select(VehicleFact).where(VehicleFact.vehicle_id == v.id,
                                                        VehicleFact.is_current.is_(True),
                                                        VehicleFact.visibility == "all",
                                                        VehicleFact.status.in_(("confirmed", "reported"))))).scalars().all()
    for f in rows:
        out.append({"key": f.key, "value": f.value, "unit": f.unit, "status": f.status,
                    "source": f.source_kind, "source_ref": f.source_ref, "observed_at": iso(f.observed_at),
                    "fact_id": f.id})
    return out


async def collect_disclosures(db: AsyncSession, v: Vehicle) -> list[dict]:
    rows = (await db.execute(select(ReconIssue).where(ReconIssue.vehicle_id == v.id,
                                                       ReconIssue.disclosure_required.is_(True)))).scalars().all()
    out = [{"text": (r.title + (f" — {r.detail}" if r.detail else "")), "source": "recon_issue", "issue_id": r.id,
            "status": r.status} for r in rows]
    for d in (v.disclosures or []):
        text = d.get("text") if isinstance(d, dict) else str(d)
        if text and not any(text == o["text"] for o in out):
            out.append({"text": text, "source": "vehicle disclosure"})
    return out


async def collect_media(db: AsyncSession, v: Vehicle) -> list[dict]:
    """Ordered approved photos. Only `public_eligible`, never sensitive documents (F02)."""
    rows = (await db.execute(select(AssetLink, Asset).join(Asset, Asset.id == AssetLink.asset_id).where(
        AssetLink.entity_kind == "vehicle", AssetLink.entity_id == v.id, AssetLink.removed_at.is_(None),
        AssetLink.role.in_(("photo", "gallery")), Asset.kind == "photo", Asset.status == "ready")
        .order_by(AssetLink.position, AssetLink.created_at))).all()
    out = []
    for link, a in rows:
        if a.sensitive or not a.public_eligible:
            continue
        out.append({"asset_id": a.id, "sha256": a.sha256, "slot": link.slot, "position": link.position,
                    "pre_arrival": bool(a.pre_arrival), "url": f"/api/assets/{a.id}/web",
                    "alt": f"{v.model_year or ''} {v.make or ''} {v.model or ''}".strip(),
                    "source": a.source, "provider_link": a.provider_link})
    return out


async def eta_evidence(db: AsyncSession, v: Vehicle) -> dict | None:
    """An ETA is only ever repeated from a sourced milestone; a missing one stays missing (F09)."""
    rows = (await db.execute(select(VehicleMilestone).where(
        VehicleMilestone.vehicle_id == v.id, VehicleMilestone.is_current.is_(True),
        VehicleMilestone.kind.in_(("received", "arrived_port", "released")),
        VehicleMilestone.status.in_(("planned", "estimated"))).order_by(VehicleMilestone.at))).scalars().all()
    for m in rows:
        if m.at and m.source_kind and m.source_kind not in ("", "unknown", "assumed", "inferred"):
            return {"kind": m.kind, "at": iso(m.at), "status": m.status, "source": m.source_kind,
                    "source_ref": m.source_ref, "milestone_id": m.id}
    return None


def derive_class(v: Vehicle) -> str:
    if v.logistics_state in EN_ROUTE_STATES:
        return "en_route"
    return "ready_for_sale"


def availability_for(v: Vehicle, listing_class: str) -> str:
    if v.allocation == "sold" or v.commercial_state in ("sold", "delivered"):
        return "sold"
    if v.allocation == "reserved" or v.commercial_state == "reserved":
        return "reserved"
    return "en_route" if listing_class == "en_route" else "available"


def _template_copy(v: Vehicle, listing_class: str, specs: list[dict], disclosures: list[dict],
                   eta: dict | None) -> tuple[str, str, str]:
    name = " ".join(str(x) for x in (v.model_year, v.make, v.model) if x) or (v.title or "Kei truck")
    headline = name if not v.grade else f"{name} {v.grade}"
    lines = []
    if listing_class == "en_route":
        phrase = STATUS_PHRASES.get(v.logistics_state, "in transit")
        lines.append(f"Status: this truck is {phrase}. It is not yet available for inspection.")
        if eta:
            label = "estimated" if eta.get("status") != "completed" else "confirmed"
            lines.append(f"Estimated arrival: {str(eta['at'])[:10]} ({label}, source: {eta['source']}).")
        else:
            lines.append("Arrival date is not confirmed yet — we update this listing when the date is sourced.")
    else:
        lines.append("Status: in our Arizona shop and ready for sale.")
    facts = [f"{s['key'].replace('_', ' ').title()}: {s['value']}" for s in specs if s.get("value")]
    if facts:
        lines.append("Recorded details — " + "; ".join(facts[:12]) + ".")
    if disclosures:
        lines.append("Disclosures: " + "; ".join(d["text"] for d in disclosures[:8]) + ".")
    lines.append("Questions or an inspection request? Contact Arizona Kei Trucks.")
    body = "\n\n".join(lines)
    short = lines[0]
    return headline, body, short


async def _model_copy(db: AsyncSession, v: Vehicle, listing_class: str, specs: list[dict], disclosures: list[dict],
                      eta: dict | None) -> tuple[str, str, str, str]:
    """Copy from the model when it is available; the deterministic template otherwise. Model output is
    scrubbed: a date that is not in the evidence never reaches a listing."""
    headline, body, short = _template_copy(v, listing_class, specs, disclosures, eta)
    if not model_available():
        return headline, body, short, "template"
    try:
        client = ModelClient(db, workflow="listing_copy")
        res = await client.complete(
            system=("You write short, factual used-vehicle listings for Arizona Kei Trucks. Use ONLY the supplied "
                    "recorded facts. Never invent mileage, condition, options, warranty, arrival dates or legal "
                    "claims. If a fact is missing, omit it. Return plain text: first line headline, then body."),
            messages=[{"role": "user", "content": str({"class": listing_class, "vehicle": {
                "make": v.make, "model": v.model, "year": v.model_year, "color": v.color, "grade": v.grade,
                "status": STATUS_PHRASES.get(v.logistics_state, v.logistics_state)},
                "specs": specs, "disclosures": [d["text"] for d in disclosures], "eta": eta})}],
            max_tokens=900)
        text = (res.text or "").strip()
        if not text:
            return headline, body, short, "template"
        first, _, rest = text.partition("\n")
        gen_headline, gen_body = first.strip()[:200], (rest.strip() or body)
        # A date in the copy may only ever be the sourced one: with no sourced ETA no date is allowed
        # at all, and with one, a *different* date is just as invented (F09).
        sourced = str((eta or {}).get("at") or "")[:10]
        allowed = set(DATE_LIKE.findall(sourced)) if sourced else set()
        if set(DATE_LIKE.findall(gen_body)) - allowed:
            log.warning("model copy contained a date that is not the sourced arrival; falling back to the template")
            return headline, body, short, "template"
        return gen_headline or headline, gen_body, (gen_body.split("\n")[0][:300] or short), "model"
    except (ModelUnavailable, ModelRefused, Exception) as e:  # noqa: BLE001 - copy never blocks a draft
        log.info("listing copy fell back to the template: %s", e)
        return headline, body, short, "template"


# ── gates ────────────────────────────────────────────────────────────────────
async def evaluate_gates(db: AsyncSession, v: Vehicle, profile: SiteProfile | None, listing_class: str,
                         package: dict) -> list[dict]:
    rules, unconfigured = site_svc.gates_for(profile, listing_class)
    out: list[dict] = []
    if unconfigured:
        out.append({"requirement": "configuration", "label": "Listing gates configured", "ok": False,
                    "detail": unconfigured + " — publication is blocked until the gates are configured",
                    "blocking": True})
    for rule in rules:
        req = rule.get("requirement")
        param = rule.get("param") or {}
        ok, detail = False, ""
        if req == "status_truthful":
            phrase = STATUS_PHRASES.get(v.logistics_state)
            ok = bool(package.get("body")) and (listing_class != "en_route" or package.get("availability") == "en_route")
            detail = f"states: {phrase or v.logistics_state}" if ok else "the draft does not state the true status"
        elif req == "eta_sourced":
            eta = (package.get("evidence") or {}).get("eta")
            if eta is None:
                ok, detail = True, "no arrival date is claimed (unknown stays unknown)"
            else:
                ok = bool(eta.get("source"))
                detail = f"estimated {str(eta.get('at'))[:10]} from {eta.get('source')}" if ok else "ETA has no source"
        elif req == "approved_price":
            ok = v.asking_price is not None and v.price_approved_at is not None
            detail = "approved asking price recorded" if ok else "no owner-approved asking price"
        elif req == "recon_verified":
            try:
                from . import shop as shop_svc
                res = await shop_svc.evaluate_gate(db, v, {"requirement": "recon_verified", "label": "Recon verified"})
                ok, detail = bool(res.get("ok")) and v.recon_state == "ready_for_sale", res.get("detail") or ""
                if ok is False and v.recon_state != "ready_for_sale":
                    detail = (detail + "; " if detail else "") + f"recon state is {v.recon_state}"
            except Exception as e:  # noqa: BLE001 - shop domain unavailable: never assume verified
                ok, detail = False, f"recon verification could not be checked ({type(e).__name__})"
        elif req == "documents_ready":
            ok = v.documents_state == "complete"
            detail = f"documents are {v.documents_state}"
        elif req == "media_checklist":
            need = int(param.get("min", 6))
            media = package.get("media_detail") or []
            slots = {m.get("slot") for m in media if m.get("slot")}
            missing = [s for s in (v.photo_requirements or []) if s not in slots]
            ok = len(media) >= need and not missing
            detail = f"{len(media)} of {need} approved public photos" + (f"; missing {', '.join(missing)}" if missing else "")
        elif req == "disclosures_written":
            ok = bool(package.get("disclosures"))
            detail = f"{len(package.get('disclosures') or [])} disclosure(s)"
        else:
            ok, detail = False, f"gate {req!r} is not configured in this build"
        out.append({"requirement": req, "label": rule.get("label") or req, "ok": ok, "detail": detail,
                    "blocking": True, "param": param})
    return out


def package_hash_of(package: dict) -> str:
    return stable_hash({"headline": package.get("headline"), "body": package.get("body"),
                        "short_description": package.get("short_description"),
                        "price": package.get("price"), "currency": package.get("currency"),
                        "specs": package.get("specs"), "disclosures": package.get("disclosures"),
                        "media": [m.get("sha256") or m.get("asset_id") for m in (package.get("media_detail") or [])],
                        "availability": package.get("availability"), "listing_class": package.get("listing_class"),
                        "channel": package.get("channel")})


def diff_packages(prev: ListingPackage | None, current: dict) -> dict:
    if prev is None:
        return {"first_version": True, "changed": sorted(k for k in ("headline", "body", "price", "media",
                                                                      "availability", "disclosures", "specs"))}
    changed: list[str] = []
    prev_payload = {"headline": prev.headline, "body": prev.body, "short_description": prev.short_description,
                    "price": str(prev.price) if prev.price is not None else None,
                    "availability": prev.availability, "listing_class": prev.listing_class,
                    "disclosures": [d.get("text") for d in (prev.disclosures or [])],
                    "specs": [(s.get("key"), s.get("value")) for s in (prev.specs or [])],
                    "media": [m.get("sha256") or m.get("asset_id") for m in (prev.media_detail or [])]}
    cur_payload = {"headline": current.get("headline"), "body": current.get("body"),
                   "short_description": current.get("short_description"), "price": current.get("price"),
                   "availability": current.get("availability"), "listing_class": current.get("listing_class"),
                   "disclosures": [d.get("text") for d in (current.get("disclosures") or [])],
                   "specs": [(s.get("key"), s.get("value")) for s in (current.get("specs") or [])],
                   "media": [m.get("sha256") or m.get("asset_id") for m in (current.get("media_detail") or [])]}
    detail = {}
    for k, v in cur_payload.items():
        if prev_payload.get(k) != v:
            changed.append(k)
            detail[k] = {"from": prev_payload.get(k), "to": v}
    return {"first_version": False, "changed": changed, "detail": detail,
            "from_version": prev.package_version, "from_hash": prev.package_hash}


async def latest_package(db: AsyncSession, vehicle_id: str, *, channel: str = WEBSITE) -> ListingPackage | None:
    return (await db.execute(select(ListingPackage).where(ListingPackage.vehicle_id == vehicle_id,
                                                           ListingPackage.channel == channel)
                             .order_by(ListingPackage.package_version.desc()))).scalars().first()


async def build(db: AsyncSession, v: Vehicle, *, listing_class: str | None, channel: str,
                profile: SiteProfile | None, use_model: bool = True) -> dict:
    listing_class = listing_class or derive_class(v)
    if listing_class not in LISTING_CLASSES:
        raise ValidationFailed(f"listing_class must be one of {LISTING_CLASSES}")
    specs = await collect_specs(db, v)
    disclosures = await collect_disclosures(db, v)
    media = await collect_media(db, v)
    eta = await eta_evidence(db, v) if listing_class == "en_route" else None
    if use_model:
        headline, body, short, generated_by = await _model_copy(db, v, listing_class, specs, disclosures, eta)
    else:
        headline, body, short = _template_copy(v, listing_class, specs, disclosures, eta)
        generated_by = "template"
    price = v.asking_price if (v.asking_price is not None and v.price_approved_at is not None) else None
    package = {
        "vehicle_id": v.id, "listing_class": listing_class, "channel": channel, "headline": headline,
        "body": body, "short_description": short, "price": str(price) if price is not None else None,
        "currency": v.asking_currency or "USD", "specs": specs, "disclosures": disclosures,
        "media": [m["asset_id"] for m in media], "media_detail": media,
        "availability": availability_for(v, listing_class),
        "evidence": {"eta": eta, "price": {"amount": str(price) if price is not None else None,
                                           "approved_at": iso(v.price_approved_at),
                                           "missing": price is None},
                     "sku": v.stock_no, "stock_no": v.stock_no, "frame_no": v.frame_no_raw,
                     "logistics_state": v.logistics_state, "recon_state": v.recon_state,
                     "documents_state": v.documents_state,
                     "specs_from": "vehicle facts (confirmed/reported) and the vehicle record",
                     "media_from": "approved public-eligible photos only"},
        "generated_by": generated_by,
    }
    package["readiness"] = await evaluate_gates(db, v, profile, listing_class, package)
    package["blocked_reasons"] = [f"{g['label']}: {g['detail']}" for g in package["readiness"] if not g["ok"]]
    package["ready"] = not package["blocked_reasons"]
    package["package_hash"] = package_hash_of(package)
    return package


class BuildPackageIn(BaseModel):
    vehicle_id: str
    listing_class: str | None = None
    channel: str = WEBSITE
    use_model: bool = True
    note: str | None = None


@command("listings.build_package", input=BuildPackageIn, perm="listings.draft", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)],
         description="Build the canonical listing package from recorded facts with evidence. Specs come from "
                     "confirmed/reported facts, disclosures from recon issues, media from approved public photos, "
                     "price from the owner-approved asking price. Readiness is evaluated against the configured "
                     "gates for the listing class; an unconfigured gate blocks publication.")
async def listings_build_package(ctx: CommandContext, inp: BuildPackageIn) -> dict:
    v = await _vehicle(ctx.db, inp.vehicle_id)
    profile = await site_svc.active_profile(ctx.db)
    package = await build(ctx.db, v, listing_class=inp.listing_class, channel=inp.channel, profile=profile,
                          use_model=inp.use_model)
    prev = await latest_package(ctx.db, v.id, channel=inp.channel)
    diff = diff_packages(prev, package)
    if prev is not None and prev.package_hash == package["package_hash"] and prev.status in ("draft", "review"):
        prev.readiness = package["readiness"]
        prev.blocked_reasons = package["blocked_reasons"]
        prev.ready = package["ready"]
        prev.profile_id = profile.id if profile else None
        prev.profile_version = profile.profile_version if profile else None
        ctx.touch(prev, "listing_package")
        return {"package": serialize_package(prev), "diff": diff, "created": False,
                "readiness": prev.readiness, "ready": prev.ready}
    pkg = ListingPackage(
        vehicle_id=v.id, package_version=(prev.package_version + 1) if prev else 1,
        listing_class=package["listing_class"], channel=inp.channel, headline=package["headline"],
        body=package["body"], short_description=package["short_description"],
        price=Decimal(package["price"]) if package["price"] is not None else None, currency=package["currency"],
        specs=package["specs"], disclosures=package["disclosures"], media=package["media"],
        media_detail=package["media_detail"], availability=package["availability"],
        profile_id=profile.id if profile else None, profile_version=profile.profile_version if profile else None,
        package_hash=package["package_hash"], diff=diff, readiness=package["readiness"], status="draft",
        supersedes_id=prev.id if prev else None, evidence=package["evidence"],
        generated_by=package["generated_by"], blocked_reasons=package["blocked_reasons"], ready=package["ready"],
        built_at=ctx.now, created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
    ctx.db.add(pkg)
    await ctx.db.flush()
    if prev is not None and prev.status in ("draft", "review"):
        prev.status = "superseded"
        prev.bump(ctx.actor.user_id)
    ctx.changed.append({"kind": "listing_package", "id": pkg.id, "version": pkg.version})
    ctx.record(f"Listing package v{pkg.package_version} built ({pkg.listing_class}): "
               f"{'ready' if pkg.ready else 'not ready — ' + '; '.join(pkg.blocked_reasons[:3])}",
               entity_kind="vehicle", entity_id=v.id, kind="listing", state="draft",
               details={"package_id": pkg.id, "hash": pkg.package_hash[:12], "changed": diff.get("changed"),
                        "generated_by": pkg.generated_by, "media": len(pkg.media or [])})
    ctx.emit("listing.changed", aggregate_type="listing_package", aggregate_id=pkg.id, aggregate_version=pkg.version,
             payload={"vehicle_id": v.id, "change": "built", "ready": pkg.ready, "listing_class": pkg.listing_class})
    return {"package": serialize_package(pkg), "diff": diff, "created": True, "readiness": pkg.readiness,
            "ready": pkg.ready}


class DiffIn(BaseModel):
    vehicle_id: str
    channel: str = WEBSITE


@command("listings.diff", input=DiffIn, perm="listings.read", action_class="read",
         records=lambda p: [("vehicle", p.vehicle_id)],
         description="What would change if the package were rebuilt now (no write, no side effect).")
async def listings_diff(ctx: CommandContext, inp: DiffIn) -> dict:
    v = await _vehicle(ctx.db, inp.vehicle_id)
    profile = await site_svc.active_profile(ctx.db)
    candidate = await build(ctx.db, v, listing_class=None, channel=inp.channel, profile=profile, use_model=False)
    prev = await latest_package(ctx.db, v.id, channel=inp.channel)
    return {"current": serialize_package(prev) if prev else None, "diff": diff_packages(prev, candidate),
            "readiness": candidate["readiness"], "ready": candidate["ready"],
            "would_hash": candidate["package_hash"]}


# ── review ───────────────────────────────────────────────────────────────────
class SubmitIn(BaseModel):
    package_id: str
    channel: str = WEBSITE
    note: str | None = None


@command("listings.submit_for_review", input=SubmitIn, perm="listings.draft", action_class="internal",
         description="Freeze a ready package for owner review: binds the site profile version and the package hash "
                     "and raises the review task. Publication itself is a separate approved action.")
async def listings_submit_for_review(ctx: CommandContext, inp: SubmitIn) -> dict:
    pkg = await ctx.db.get(ListingPackage, inp.package_id)
    if pkg is None:
        raise NotFound("listing package not found")
    if pkg.status in ("published", "superseded"):
        raise Blocked(f"package is {pkg.status}; build a new version")
    if not pkg.ready:
        raise Blocked("package does not pass its gates", blocked_reasons=list(pkg.blocked_reasons or []),
                      readiness=list(pkg.readiness or []))
    profile = await site_svc.active_profile(ctx.db)
    if inp.channel in SUPPORTED_CHANNELS:
        ok, why = site_svc.writable(profile)
        if not ok:
            raise Blocked(f"website writes are not available: {why}", channel=inp.channel)
        pkg.profile_id, pkg.profile_version = profile.id, profile.profile_version
    pkg.status = "review"
    ctx.touch(pkg, "listing_package")
    task = await dispatch(ctx.child(), "tasks.create", {
        "title": f"Review listing for {pkg.headline[:80]}", "type": "operational", "priority": "normal",
        "vehicle_id": pkg.vehicle_id, "notes": inp.note or "", "source_kind": "listing_package",
        "source_id": pkg.id, "extra": {"package_id": pkg.id, "channel": inp.channel,
                                       "package_hash": pkg.package_hash}}, commit=False)
    ctx.record(f"Listing package v{pkg.package_version} submitted for review ({inp.channel})",
               entity_kind="vehicle", entity_id=pkg.vehicle_id, kind="listing", state="review",
               details={"package_id": pkg.id, "profile_version": pkg.profile_version,
                        "hash": (pkg.package_hash or "")[:12]})
    ctx.emit("listing.changed", aggregate_type="listing_package", aggregate_id=pkg.id, aggregate_version=pkg.version,
             payload={"vehicle_id": pkg.vehicle_id, "change": "submitted", "channel": inp.channel})
    preview = await preview_payload(ctx.db, pkg, profile)
    return {"package": serialize_package(pkg), "preview": preview,
            "review_task_id": ((task.data or {}).get("task") or {}).get("id") if task and task.data else None}


async def preview_payload(db: AsyncSession, pkg: ListingPackage, profile: SiteProfile | None) -> dict:
    """Render the mapped payload without touching the site (invariant 11)."""
    prof = site_svc.profile_dict(profile)
    payload = wp_adapter.render_payload(package_payload(pkg), prof)
    validation = wp_adapter.validate_package(package_payload(pkg), prof)
    return {"payload": payload, "valid": validation["ok"], "errors": validation["errors"],
            "warnings": validation["warnings"], "profile_version": profile.profile_version if profile else None,
            "target": (profile.base_url if profile else None), "written": False}


# ── publish ──────────────────────────────────────────────────────────────────
class PublishIn(BaseModel):
    package_id: str
    channel: str = WEBSITE
    expected_package_hash: str | None = None
    expected_profile_version: int | None = None
    note: str | None = None


def _publish_summary(p: PublishIn) -> str:
    return f"Publish listing package {p.package_id[:8]} to {p.channel}"


def _publish_consequence(p: PublishIn) -> dict:
    return {"scope": f"public listing on {p.channel}", "moves_money": False,
            "targets": {"channel": p.channel, "package_id": p.package_id}}


async def publish_blockers(db: AsyncSession, pkg: ListingPackage | None, *, channel: str,
                           expected_package_hash: str | None = None,
                           expected_profile_version: int | None = None) -> list[str]:
    """Everything that must still be exactly true immediately before a channel is written
    (invariant 9). Shared by the exact-approval revalidate hook and by the handler itself, so a
    standing permission can never take the shortcut past these checks."""
    reasons: list[str] = []
    if pkg is None:
        return ["listing package no longer exists"]
    if expected_package_hash and expected_package_hash != pkg.package_hash:
        reasons.append("the package changed since it was approved — review again")
    if pkg.status in ("superseded", "invalidated", "published"):
        reasons.append(f"package is {pkg.status} — review again")
    newer = await latest_package(db, pkg.vehicle_id, channel=channel)
    if newer is not None and newer.id != pkg.id and newer.package_hash != pkg.package_hash:
        reasons.append("a newer package version was built — review again")
    profile = await site_svc.active_profile(db)
    if channel in SUPPORTED_CHANNELS:
        if profile is None:
            reasons.append("no active site profile")
        else:
            want = expected_profile_version or pkg.profile_version
            if want and profile.profile_version != want:
                reasons.append("the site profile changed version — review again")
            ok, why = site_svc.writable(profile)
            if not ok:
                reasons.append(f"website writes are paused: {why}")
    v = await db.get(Vehicle, pkg.vehicle_id)
    if v is not None:
        if availability_for(v, pkg.listing_class) != pkg.availability:
            reasons.append(f"availability changed to {availability_for(v, pkg.listing_class)} — review again")
        approved = str(v.asking_price) if (v.asking_price is not None and v.price_approved_at is not None) else None
        if approved != (str(pkg.price) if pkg.price is not None else None):
            reasons.append("the approved price changed — review again")
        gates = await evaluate_gates(db, v, profile, pkg.listing_class, {**package_payload(pkg),
                                                                        "media_detail": list(pkg.media_detail or []),
                                                                        "evidence": dict(pkg.evidence or {})})
        failing = [g["label"] for g in gates if not g["ok"]]
        if failing:
            reasons.append("gates no longer pass: " + ", ".join(failing))
    return reasons


async def publish_revalidate(ctx: CommandContext, inp: PublishIn, approval) -> list[str]:
    """Immediately before execution: the approved package, profile version, gates and availability
    must still be exactly what the owner approved (invariant 9)."""
    bound = approval.payload or {}
    return await publish_blockers(
        ctx.db, await ctx.db.get(ListingPackage, inp.package_id), channel=inp.channel,
        expected_package_hash=bound.get("expected_package_hash") or inp.expected_package_hash,
        expected_profile_version=bound.get("expected_profile_version") or inp.expected_profile_version)


async def dedupe_key_for(db: AsyncSession, base: str, *, limit: int = 50) -> str:
    """The dedupe key for a new external action.

    The same logical send is never repeated: while an action for `base` is alive (intent, executing,
    confirmed or unknown) the intent is reused. A *dead* attempt is different — an action that failed
    at the provider or was cancelled can never execute again, so reusing it would leave the owner's
    new approval reading `queued`/`confirmed` while nothing is ever sent. Those get their own key."""
    key = base
    for n in range(1, limit + 1):
        act = (await db.execute(select(ExternalAction).where(ExternalAction.dedupe_key == key))).scalar_one_or_none()
        if act is None or act.state not in ("failed", "cancelled"):
            return key
        key = f"{base}:retry{n}"
    return key


async def _publication_for(ctx: CommandContext, pkg: ListingPackage, channel: str) -> Publication:
    pub = (await ctx.db.execute(select(Publication).where(Publication.vehicle_id == pkg.vehicle_id,
                                                           Publication.channel == channel))).scalars().first()
    if pub is None:
        pub = Publication(package_id=pkg.id, vehicle_id=pkg.vehicle_id, channel=channel, desired_state="published",
                          state="queued", created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
        ctx.db.add(pub)
        await ctx.db.flush()
        ctx.changed.append({"kind": "publication", "id": pub.id, "version": pub.version})
    return pub


def _history(pub: Publication, state: str, detail: str = "", **extra) -> None:
    pub.history = list(pub.history or []) + [{"state": state, "at": now().isoformat(), "detail": detail, **extra}]


class LinkIn(BaseModel):
    vehicle_id: str
    external_id: str | None = None
    channel: str = WEBSITE
    confirm_unlink: bool = False


@command("listings.link_existing", input=LinkIn, perm="listings.publish", action_class="owner_only",
         approval_kind="publish", records=lambda p: [("vehicle", p.vehicle_id)],
         summary=lambda p: (f"Link {p.vehicle_id[:8]} to site listing {p.external_id}" if p.external_id
                            else f"Unlink {p.vehicle_id[:8]} from its site listing"),
         description="Bind a vehicle's publication to a listing that already exists on the site, after a person "
                     "confirmed it is the same truck. Passing no external_id unlinks instead. Linking never "
                     "writes to the site — the next publish does, against the confirmed listing.")
async def listings_link_existing(ctx: CommandContext, inp: LinkIn) -> dict:
    v = await _vehicle(ctx.db, inp.vehicle_id)
    pub = (await ctx.db.execute(select(Publication).where(Publication.vehicle_id == v.id,
                                                          Publication.channel == inp.channel))).scalars().first()
    if pub is None:
        pub = Publication(vehicle_id=v.id, channel=inp.channel, desired_state="published", state="queued",
                          created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
        ctx.db.add(pub)
        await ctx.db.flush()
    if not inp.external_id:
        if pub.external_id and not inp.confirm_unlink:
            raise Blocked("unlinking leaves the site listing in place and unmanaged; pass confirm_unlink to proceed",
                          external_id=pub.external_id)
        previous, pub.external_id = pub.external_id, None
        pub.external_url = None
        pub.media_map = {}
        pub.state = "queued"
        pub.error = None
        _history(pub, "queued", f"unlinked from site listing {previous}")
        ctx.touch(pub, "publication")
        ctx.record(f"Unlinked {v.stock_no or v.id} from site listing {previous}", entity_kind="vehicle",
                   entity_id=v.id, kind="listing", state="queued", details={"external_id": previous})
        return {"publication": serialize_publication(pub), "linked": False}
    profile = await site_svc.active_profile(ctx.db)
    prof = site_svc.profile_dict(profile)
    ad = await _adapter(ctx.db)
    rows = await _search(ad, prof, external_id=inp.external_id)
    row = next((r for r in rows if str(r.get("external_id")) == str(inp.external_id)), None)
    if row is None:
        raise NotFound(f"the site has no listing {inp.external_id}")
    claimed = (row.get("meta") or {}).get("azkt_vehicle_id")
    if claimed and claimed != v.id:
        raise Blocked("that site listing is already bound to a different vehicle in AZKT",
                      external_id=inp.external_id, azkt_vehicle_id=claimed)
    other = (await ctx.db.execute(select(Publication).where(Publication.channel == inp.channel,
                                                            Publication.external_id == str(inp.external_id),
                                                            Publication.vehicle_id != v.id))).scalars().first()
    if other is not None:
        raise Blocked("another vehicle's publication already points at that site listing",
                      external_id=inp.external_id, vehicle_id=other.vehicle_id)
    pub.external_id = str(inp.external_id)
    pub.external_url = row.get("url") or pub.external_url
    pub.state = "accepted" if pub.package_id else "queued"
    pub.error = None
    pub.media_map = {}
    _history(pub, pub.state, f"linked by {ctx.actor.user_id or 'owner'} to existing site listing {pub.external_id}",
             title=row.get("title"), sku=row.get("sku"))
    ctx.touch(pub, "publication")
    ctx.record(f"Linked {v.stock_no or v.id} to site listing {pub.external_id}", entity_kind="vehicle",
               entity_id=v.id, kind="listing", state=pub.state,
               details={"external_id": pub.external_id, "title": row.get("title"), "url": pub.external_url,
                        "sku": row.get("sku")})
    return {"publication": serialize_publication(pub), "linked": True,
            "listing": {"external_id": pub.external_id, "title": row.get("title"), "url": pub.external_url,
                        "sku": row.get("sku"), "status": row.get("status")}}


@command("listings.publish", input=PublishIn, perm="listings.publish", action_class="consequential",
         approval_kind="publish", records=lambda p: [("listing_package", p.package_id)],
         summary=_publish_summary, consequence=_publish_consequence,
         limits=lambda p: {"records": [p.package_id], "fields": ["listing"]}, revalidate=publish_revalidate,
         description="Publish an approved package to a channel. The approval binds package hash + site profile "
                     "version + channel. The effect is a persisted external action executed exactly once; "
                     "an existing listing is imported before anything is created.")
async def listings_publish(ctx: CommandContext, inp: PublishIn) -> dict:
    pkg = await ctx.db.get(ListingPackage, inp.package_id)
    if pkg is None:
        raise NotFound("listing package not found")
    if not pkg.ready:
        raise Blocked("package does not pass its gates", blocked_reasons=list(pkg.blocked_reasons or []))
    if ctx.approval is None:
        # No exact approval is bound (a standing permission allowed this): the revalidate hook never
        # ran, so the same bindings are checked here rather than trusted.
        blockers = await publish_blockers(ctx.db, pkg, channel=inp.channel,
                                          expected_package_hash=inp.expected_package_hash,
                                          expected_profile_version=inp.expected_profile_version)
        if blockers:
            raise Blocked("; ".join(blockers), reasons=blockers)
    pub = await _publication_for(ctx, pkg, inp.channel)
    pub.package_id = pkg.id
    pub.package_version = pkg.package_version
    pub.package_hash = pkg.package_hash
    if inp.channel not in SUPPORTED_CHANNELS:
        # No verified adapter for this channel: copy + photos + checklist go to a person (spec §7.3).
        pub.state = "unsupported"
        pub.unsupported_reason = (f"{inp.channel} has no verified adapter; posting and cleanup are assigned to a "
                                  f"person and tracked independently")
        pub.desired_state = "published"
        pub.cleanup_required = True
        _history(pub, "unsupported", pub.unsupported_reason)
        task = await dispatch(ctx.child(), "tasks.create", {
            "title": f"Post listing manually on {inp.channel}", "type": "operational", "priority": "normal",
            "vehicle_id": pkg.vehicle_id,
            "instructions": _manual_checklist(pkg, inp.channel),
            "source_kind": "publication", "source_id": pub.id,
            "extra": {"channel": inp.channel, "package_id": pkg.id, "photos": list(pkg.media or [])}}, commit=False)
        pub.manual_task_id = ((task.data or {}).get("task") or {}).get("id") if task and task.data else None
        ctx.touch(pub, "publication")
        ctx.record(f"Channel {inp.channel} is unsupported — manual posting task created", entity_kind="vehicle",
                   entity_id=pkg.vehicle_id, kind="listing", state="unsupported",
                   details={"publication_id": pub.id, "task_id": pub.manual_task_id})
        return {"publication": serialize_publication(pub), "state": "unsupported", "queued": False}
    profile = await site_svc.active_profile(ctx.db)
    ok, why = site_svc.writable(profile)
    if not ok:
        raise Blocked(f"website writes are not available: {why}")
    pub.profile_id, pub.profile_version = profile.id, profile.profile_version
    pub.desired_state = pkg.availability      # what the public listing must show once it is live
    pub.state = "queued"
    pub.error = None
    _history(pub, "queued", f"package v{pkg.package_version}")
    act = await approvals_svc.intend_external_action(
        ctx, command_name="listings.publish", provider="wordpress", entity_kind="publication", entity_id=pub.id,
        dedupe_key=await dedupe_key_for(ctx.db, f"listing:publish:{pkg.id}:{pkg.package_version}:{inp.channel}"),
        payload={"package_id": pkg.id, "publication_id": pub.id, "channel": inp.channel,
                 "package_hash": pkg.package_hash, "profile_id": profile.id,
                 "profile_version": profile.profile_version})
    pub.external_action_id = act.id
    pkg.status = "approved"
    pkg.approval_id = ctx.approval.id if ctx.approval else pkg.approval_id
    ctx.touch(pub, "publication")
    ctx.touch(pkg, "listing_package")
    ctx.record(f"Listing publication queued for {inp.channel} (package v{pkg.package_version})",
               entity_kind="vehicle", entity_id=pkg.vehicle_id, kind="listing", state="queued",
               details={"publication_id": pub.id, "external_action_id": act.id,
                        "profile_version": profile.profile_version})
    ctx.emit("listing.changed", aggregate_type="publication", aggregate_id=pub.id, aggregate_version=pub.version,
             payload={"vehicle_id": pkg.vehicle_id, "change": "queued", "channel": inp.channel})
    return {"publication": serialize_publication(pub), "state": "queued", "queued": True,
            "external_action_id": act.id}


def _manual_checklist(pkg: ListingPackage, channel: str) -> str:
    lines = [f"Post this listing on {channel} by hand (no verified API adapter).", "",
             f"Headline: {pkg.headline}", "", pkg.body or "", "",
             f"Price: {pkg.price} {pkg.currency}" if pkg.price is not None else "Price: not approved yet", "",
             f"Photos ({len(pkg.media or [])}): " + ", ".join(f"/api/assets/{a}/web" for a in (pkg.media or [])[:12]),
             "", "Cleanup: when the truck is reserved or sold, update or remove this listing and mark the task done."]
    if pkg.disclosures:
        lines += ["", "Required disclosures: " + "; ".join(d.get("text", "") for d in pkg.disclosures)]
    return "\n".join(lines)


# ── executor: the only place that writes to the site ─────────────────────────
async def _adapter(db: AsyncSession):
    return await site_svc.website_adapter(db)


def _site_key(profile: SiteProfile | None, ad) -> str:
    """Which media library the ids belong to. Two AZKT profile versions of the same site share one."""
    return ((profile.base_url if profile else "") or getattr(ad, "base_url", "") or "site").rstrip("/")


async def _known_media(db: AsyncSession, site_key: str, checksums: list[str]) -> dict:
    if not checksums:
        return {}
    rows = (await db.execute(select(SiteMedia).where(SiteMedia.site_key == site_key,
                                                     SiteMedia.sha256.in_(checksums)))).scalars().all()
    return {r.sha256: r.media_id for r in rows if not r.missing}


async def _record_media(db: AsyncSession, site_key: str, sha: str, entry: dict, item: dict) -> None:
    row = (await db.execute(select(SiteMedia).where(SiteMedia.site_key == site_key,
                                                    SiteMedia.sha256 == sha))).scalars().first()
    if row is None:
        row = SiteMedia(site_key=site_key, sha256=sha, media_id=str(entry.get("id")),
                        asset_id=item.get("asset_id"), uploaded_at=now())
        db.add(row)
    row.media_id = str(entry.get("id"))
    row.source_url = entry.get("src")
    row.filename = item.get("filename")
    row.alt = item.get("alt")
    row.bytes_len = len(item.get("data") or b"")
    row.asset_id = item.get("asset_id") or row.asset_id
    row.missing = False
    row.last_seen_at = now()


async def upload_package_media(db: AsyncSession, ad, pkg: ListingPackage, prof: dict,
                               profile: SiteProfile | None, pub: Publication | None = None) -> tuple[dict, dict]:
    """Put every approved photo in the site's own media library and return (package, report).

    The site can never fetch `/api/assets/...`: the dashboard needs a signed-in session, so a product
    written with `src` URLs publishes with no images. Each photo is uploaded once, addressed by its
    checksum, and reused on every later publish of this or any other vehicle.
    """
    package = package_payload(pkg)
    media = list(package.get("media") or [])
    report = {"uploaded": 0, "reused": 0, "total": len(media), "skipped": []}
    if not media:
        return package, report
    site_key = _site_key(profile, ad)
    checksums = [m.get("sha256") for m in media if m.get("sha256")]
    # The per-site map is the general one; this publication's own map wins where they disagree,
    # because it was proved against this exact listing.
    known = await _known_media(db, site_key, checksums)
    if pub is not None:
        known = {**known, **dict(pub.media_map or {})}
    items: list[dict] = []
    for m in media:
        sha = m.get("sha256")
        asset_id = m.get("asset_id")
        if not sha or not asset_id:
            report["skipped"].append({"asset_id": asset_id, "reason": "no checksum on the asset"})
            continue
        asset = await db.get(Asset, asset_id)
        if asset is None:
            report["skipped"].append({"asset_id": asset_id, "reason": "asset row is gone"})
            continue
        got = _asset_bytes(asset)
        if not got:
            report["skipped"].append({"asset_id": asset_id,
                                      "reason": "no readable image bytes (the web rendition is missing; the "
                                                "original is never published because its EXIF is not stripped)"})
            continue
        data, content_type = got
        items.append({"sha256": sha, "asset_id": asset_id, "data": data, "content_type": content_type,
                      "filename": _media_filename(pkg, m, sha, content_type), "alt": m.get("alt") or pkg.headline,
                      "title": m.get("alt") or pkg.headline})
    if report["skipped"]:
        raise Blocked("some approved photos cannot be uploaded to the website: "
                      + "; ".join(f"{s['asset_id']}: {s['reason']}" for s in report["skipped"]),
                      skipped=report["skipped"])
    entries = await ad.ensure_media(items, known)
    by_item = {i["sha256"]: i for i in items}
    for sha, entry in entries.items():
        if entry.get("reused"):
            report["reused"] += 1
        else:
            report["uploaded"] += 1
        await _record_media(db, site_key, sha, entry, by_item.get(sha, {}))
    package["media"] = [{**m, "media_id": (entries.get(m.get("sha256")) or {}).get("id"),
                         "media_src": (entries.get(m.get("sha256")) or {}).get("src")} for m in media]
    report["map"] = {sha: str(e.get("id")) for sha, e in entries.items() if e.get("id") is not None}
    return package, report


def package_with_known_media(pkg: ListingPackage, pub: Publication | None) -> dict:
    """The package payload with the media ids this publication already proved, and no upload.

    Verification and reconciliation compare against what AZKT last wrote, so they need the same ids
    the publish used. They must never upload: reading the site is not a write.
    """
    package = package_payload(pkg)
    known = dict((pub.media_map if pub is not None else None) or {})
    if not known:
        return package
    package["media"] = [{**m, "media_id": known.get(m.get("sha256")) or known.get(m.get("asset_id"))}
                        for m in (package.get("media") or [])]
    return package


def _asset_bytes(asset: Asset) -> tuple[bytes, str] | None:
    """The web rendition, and only ever that.

    The rendition is the one copy with its metadata stripped (services/assets.py), so the original is
    deliberately not a fallback: publishing it would put the camera's EXIF — including the GPS fix of
    wherever the truck was photographed — on a public product page. A vehicle whose rendition is
    missing blocks the publish with a reason instead, which is recoverable; a leaked location is not.
    A file missing from storage is honestly None rather than an exception that would read as a
    website failure.
    """
    try:
        got = assets_svc.variant_bytes(asset, "web")
    except Exception:  # noqa: BLE001 - storage object is gone or unreadable
        return None
    return got if got and got[0] else None


def _media_filename(pkg: ListingPackage, m: dict, sha: str, content_type: str) -> str:
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(content_type, "jpg")
    stock = (pkg.evidence or {}).get("stock_no") or (pkg.evidence or {}).get("sku") or pkg.vehicle_id[:8]
    slot = m.get("slot") or f"photo-{(m.get('position') or 0) + 1}"
    return f"{stock}-{slot}-{sha[:8]}.{ext}"


def _candidate(r: dict) -> dict:
    return {"external_id": r["external_id"], "title": r.get("title"), "sku": r.get("sku"),
            "url": r.get("url"), "price": r.get("price"), "status": r.get("status"),
            "azkt_vehicle_id": (r.get("meta") or {}).get("azkt_vehicle_id")}


async def _search(ad, prof: dict, **kw) -> list[dict]:
    try:
        return await ad.read_existing(profile=prof, **kw)
    except (ProviderError, Unsupported):
        return []


async def _find_existing(ad, package: dict, prof: dict, pkg: ListingPackage) -> tuple[str | None, dict | None]:
    """Import an existing listing before creating one (F05).

    The only evidence that automatically binds a site listing to a vehicle is AZKT's own
    `azkt_vehicle_id` meta, or a SKU that AZKT itself owns. On the live site the SKU column belongs to
    the shop (`sku_strategy = preserve`), so a stock number that happens to equal somebody's SKU is
    *not* a match. Everything weaker is a proposal a person confirms — never an automatic mapping.
    """
    stock_no = package.get("sku")
    strategy = wp_adapter.sku_strategy_for(prof)
    rows: list[dict] = []
    if strategy != "preserve":
        site_sku = wp_adapter.site_sku(package, prof)
        if site_sku:
            rows = await _search(ad, prof, sku=site_sku)
            exact = [r for r in rows if (r.get("sku") or "") == site_sku]
            if len(exact) == 1:
                return exact[0]["external_id"], None
            if len(exact) > 1:
                return None, {"reason": "several site listings share this SKU",
                              "candidates": [_candidate(r) for r in exact[:5]]}
    # AZKT's own marker is the one automatic binding: it was written by a previous publish.
    seen: dict[str, dict] = {r["external_id"]: r for r in rows}
    for term in (stock_no, package.get("headline")):
        if not term:
            continue
        for r in await _search(ad, prof, title=term):
            seen.setdefault(r["external_id"], r)
    mine = [r for r in seen.values() if (r.get("meta") or {}).get("azkt_vehicle_id") == pkg.vehicle_id]
    if len(mine) == 1:
        return mine[0]["external_id"], None
    if len(mine) > 1:
        return None, {"reason": "several site listings claim this vehicle — resolve the duplicates on the site",
                      "candidates": [_candidate(r) for r in mine[:5]]}
    # A listing AZKT already bound to a *different* vehicle is definitely not this one, so it is not a
    # candidate. Without this, a second truck of the same make, model and year would stop for a
    # confirmation against its stablemate's listing every time.
    unclaimed = [r for r in seen.values() if not (r.get("meta") or {}).get("azkt_vehicle_id")]
    if unclaimed:
        reason = ("a site listing looks like this vehicle but carries no AZKT marker — confirm the mapping "
                  "before publishing, or publish as a new listing")
        return None, {"reason": reason, "candidates": [_candidate(r) for r in unclaimed[:5]]}
    return None, None


def _verify(expected_payload: dict, readback: dict, profile: SiteProfile | None) -> dict:
    api = readback.get("api") or {}
    public = readback.get("public") or {}
    api_cmp = site_svc.compare_managed(expected_payload, api, profile)
    out = {"checked_at": now().isoformat(), "api": {"ok": not api_cmp["mismatches"], **api_cmp},
           "public": {"fetched": bool(public.get("fetched"))}}
    if not public.get("fetched"):
        out["public"]["ok"] = None
        out["public"]["reason"] = public.get("error") or "public page could not be fetched"
        return out
    rendered = public.get("rendered")
    if rendered:
        pub_cmp = site_svc.compare_managed(expected_payload, rendered, profile,
                                           fields=site_svc.PUBLIC_COMPARE_FIELDS)
        out["public"] = {"fetched": True, "ok": not pub_cmp["mismatches"], **pub_cmp, "cache": public.get("cache")}
        return out
    html = public.get("html") or ""
    checks = {}
    for logical in site_svc.PUBLIC_COMPARE_FIELDS:
        target = wp_adapter.field_map_for(site_svc.profile_dict(profile)).get(logical, logical)
        value = expected_payload.get(target)
        if value is None:
            continue
        checks[logical] = str(value) in html
    out["public"] = {"fetched": True, "ok": all(checks.values()) if checks else None, "checks": checks,
                     "cache": public.get("cache")}
    return out


async def _apply_verification(db: AsyncSession, pub: Publication, pkg: ListingPackage, profile: SiteProfile | None,
                              readback: dict, expected_payload: dict, *, scan: bool = False) -> str:
    """Compare the site against what AZKT last wrote and move the publication's state.

    `scan` marks the routine hourly pass over listings that are already live. A real difference in an
    AZKT-owned field is drift either way, but a public page that merely has not caught up is only
    meaningful while a publish is still settling: on a scan it leaves a verified listing verified
    rather than reopening it every hour because a CDN is holding an old copy.
    """
    verification = _verify(expected_payload, readback, profile)
    pub.verification = verification
    pub.observed_state = (readback.get("api") or {}).get("status")
    pub.last_verified_at = now()
    api_ok = verification["api"]["ok"]
    public_ok = verification["public"].get("ok")
    if not api_ok:
        pub.state = "mismatch"
        pub.error = "the site's API state differs from the approved package: " + \
                    ", ".join(f"{m['field']} {m['observed']!r} (expected {m['expected']!r})"
                              for m in verification["api"]["mismatches"])
        _history(pub, "mismatch", pub.error)
        edited_by = verification["api"].get("edited_by")
        if profile is not None and edited_by != "azkt":
            # The site's own API disagrees with exactly what AZKT wrote on a field AZKT owns: someone
            # edited it outside AZKT. A live WordPress/WooCommerce row carries no "who edited this",
            # so the pause is driven by the comparison itself and only *names* an editor when the site
            # reports one (F08). Writes for the channel stop until a new version is validated.
            fields = ", ".join(m["field"] for m in verification["api"]["mismatches"])
            who = f" (last edited by {edited_by})" if edited_by else ""
            ctx = CommandContext(db=db, actor=SYSTEM_ACTOR, channel="worker")
            await site_svc.record_drift(ctx, profile, source="readback",
                                        reasons=[f"a manual edit changed AZKT-owned field(s) {fields}{who}"],
                                        detail={"publication_id": pub.id,
                                                "mismatches": verification["api"]["mismatches"],
                                                "edited_by": edited_by,
                                                "editor_fields_preserved": verification["api"].get("editor_fields")})
        return pub.state
    if public_ok is True:
        pub.state = "verified"
        pub.cleanup_required = False
        pub.error = None
        _history(pub, "verified", "API and public page agree" + (" (routine scan)" if scan else ""))
        return pub.state
    if scan:
        # the API agrees with AZKT; only the rendered page is behind or unreadable
        _history(pub, pub.state, "routine scan: API matches; " +
                 (verification["public"].get("reason") or "the public page could not be compared"))
        return pub.state
    if (pub.attempts or 0) >= VERIFY_MAX_ATTEMPTS:
        pub.state = "mismatch"
        pub.error = "the public page still does not show the published values after bounded retries"
        _history(pub, "mismatch", pub.error)
        return pub.state
    pub.state = "pending_verification"
    pub.error = verification["public"].get("reason") or "the public page has not caught up yet"
    _history(pub, "pending_verification", pub.error)
    return pub.state


@approvals_svc.executor("listings.publish")
async def _exec_publish(db: AsyncSession, act: ExternalAction) -> dict:
    payload = dict(act.payload or {})
    pub = await db.get(Publication, payload.get("publication_id"))
    pkg = await db.get(ListingPackage, payload.get("package_id"))
    if pub is None or pkg is None:
        raise DomainError("publication or package missing", code="publication_missing")
    profile = await db.get(SiteProfile, payload.get("profile_id")) if payload.get("profile_id") else None
    ok, why = site_svc.writable(profile)
    if not ok:
        pub.state = "failed"
        pub.error = f"website writes are not available: {why}"
        pub.cleanup_required = True
        _history(pub, "failed", pub.error)
        await db.commit()
        raise Blocked(pub.error)
    prof = site_svc.profile_dict(profile)
    ad = await _adapter(db)
    try:
        package, media_report = await upload_package_media(db, ad, pkg, prof, profile, pub)
    except (ProviderError, Unsupported, Blocked) as e:
        # Blocked is the local half of this: an approved photo whose bytes are gone from storage.
        # Either way the product is never written without its images, and the row says which it was.
        kind = ("unreadable_media" if isinstance(e, Blocked)
                else wp_adapter.error_kind(e) if isinstance(e, ProviderError) else "unsupported")
        pub.state = "failed"
        pub.error = f"listing photos could not be uploaded to the site media library: {e}"
        pub.error_kind = kind
        pub.cleanup_required = True
        _history(pub, "failed", pub.error)
        await db.commit()
        raise
    if media_report.get("map"):
        pub.media_map = {**dict(pub.media_map or {}), **media_report["map"]}
    if media_report["total"]:
        _history(pub, pub.state, f"media library: {media_report['uploaded']} uploaded, "
                                 f"{media_report['reused']} reused of {media_report['total']}")
    await db.commit()
    expected_payload = wp_adapter.render_payload({**package, "status": "publish"}, prof)
    pub.attempts = (pub.attempts or 0) + 1
    if not pub.external_id:
        found, proposal = await _find_existing(ad, package, prof, pkg)
        if proposal is not None:
            pub.state = "needs_review"
            pub.error = proposal["reason"]
            _history(pub, "needs_review", proposal["reason"], candidates=proposal.get("candidates"))
            task = await _task(db, title=f"Confirm the existing website listing for {pkg.headline[:60]}",
                               vehicle_id=pkg.vehicle_id, source_id=pub.id,
                               instructions=f"{proposal['reason']}\nCandidates: {proposal.get('candidates')}")
            pub.manual_task_id = task
            await db.commit()
            return {"handed_off": True, "sent": False, "state": "needs_review", "reason": proposal["reason"],
                    "candidates": proposal.get("candidates")}
        pub.external_id = found
        if found:
            _history(pub, "mapped", f"imported existing site listing {found}")
            await db.commit()
    try:
        draft = await ad.upsert_draft(package, prof, pub.external_id)
        pub.external_id = draft.get("external_id") or pub.external_id
        pub.external_url = draft.get("external_url") or pub.external_url
        pub.state = "accepted"
        _history(pub, "accepted", f"draft accepted as {pub.external_id}")
        await db.commit()
        published = await ad.publish(pub.external_id, package, prof)
        pub.external_url = published.get("external_url") or pub.external_url
        pub.state = "published"
        _history(pub, "published", f"site reports {published.get('status')}")
        await db.commit()
    except wp_adapter.UnknownWriteResult as e:
        pub.state = "unknown"
        pub.error = str(e)
        pub.error_kind = "unknown_result"
        pub.cleanup_required = True
        _history(pub, "unknown", str(e))
        await db.commit()
        raise approvals_svc.UnknownResult(str(e), provider_ref=e.provider_ref or pub.external_id)
    except (ProviderError, Unsupported) as e:
        kind = wp_adapter.error_kind(e) if isinstance(e, ProviderError) else "unsupported"
        pub.state = "failed"
        pub.error = str(e)
        pub.error_kind = kind
        pub.cleanup_required = True
        _history(pub, "failed", f"{kind}: {e}")
        await db.commit()
        # a provider refusal is a failure, not a hand-off: re-raise so the approval reads `failed`
        raise
    try:
        readback = await ad.read_back(pub.external_id, profile=prof)
    except (ProviderError, Unsupported) as e:
        readback = {"api": {}, "public": {"fetched": False, "error": str(e)}}
    state = await _apply_verification(db, pub, pkg, profile, readback, expected_payload)
    pub.media_map = {**dict(pub.media_map or {}),
                     **_media_map(pkg, (readback.get("api") or {}).get("images") or [])}
    pub.verification = {**dict(pub.verification or {}),
                        "media": _verify_media(package, (readback.get("api") or {}).get("images") or [])}
    pkg.status = "published"
    pkg.bump(None)
    if state == "verified":
        await _record_listed_milestone(db, pub, pkg)
    elif state == "pending_verification":
        await jobs.enqueue(db, "listings.verify", {"publication_id": pub.id},
                           run_at=now() + timedelta(minutes=5), dedupe_key=f"listing:verify:{pub.id}")
    await db.commit()
    return {"sent": True, "state": state, "provider_ref": pub.external_id, "external_url": pub.external_url,
            "verification": pub.verification}


def _verify_media(package: dict, images: list) -> dict:
    """Did the site keep the media ids AZKT sent, in order?

    This is what proves photos actually landed. Comparing checksums would prove nothing: the site
    does not store ours. The ids do, and they came back from the site's own media library.
    """
    sent = [str(m.get("media_id")) for m in (package.get("media") or []) if m.get("media_id")]
    observed = [str(i.get("id")) for i in images if isinstance(i, dict) and i.get("id") is not None]
    missing = [m for m in sent if m not in observed]
    return {"sent": len(sent), "observed": len(observed), "missing": missing,
            "order_ok": observed[:len(sent)] == sent, "ok": not missing}


def _media_map(pkg: ListingPackage, images: list) -> dict:
    """asset checksum → the media id the site reports for it.

    Matched on the checksum the payload carried whenever the site echoes it back; position is only
    trusted when the site returned exactly the images that were sent. A map that cannot be proved is
    left empty rather than pairing a checksum with somebody else's media id."""
    wanted = list(pkg.media_detail or [])
    out: dict = {}
    for i, m in enumerate(wanted):
        key = m.get("sha256") or m.get("asset_id")
        if not key:
            continue
        img = next((x for x in images if isinstance(x, dict) and x.get("sha256") and x.get("sha256") == m.get("sha256")), None)
        if img is None and len(images) == len(wanted) and isinstance(images[i], dict):
            img = images[i]
        if img and img.get("id"):
            out[key] = img["id"]
    return out


async def _task(db: AsyncSession, *, title: str, vehicle_id: str | None, source_id: str, instructions: str = "",
                priority: str = "normal") -> str | None:
    ctx = CommandContext(db=db, actor=SYSTEM_ACTOR, channel="worker")
    try:
        res = await dispatch(ctx, "tasks.create", {"title": title, "type": "operational", "priority": priority,
                                                   "vehicle_id": vehicle_id, "instructions": instructions,
                                                   "source_kind": "publication", "source_id": source_id},
                             commit=False)
        return ((res.data or {}).get("task") or {}).get("id")
    except DomainError as e:
        log.warning("listing task could not be created: %s", e)
        return None


async def _record_listed_milestone(db: AsyncSession, pub: Publication, pkg: ListingPackage) -> None:
    """K04: a verified publication is the source of the `listed` milestone and of the listed state."""
    ctx = CommandContext(db=db, actor=SYSTEM_ACTOR, channel="worker")
    try:
        await dispatch(ctx, "vehicles.record_milestone", {
            "vehicle_id": pkg.vehicle_id, "kind": "listed", "status": "completed", "at": ctx.now,
            "source_kind": "provider", "source_ref": f"publication:{pub.id}",
            "note": f"Verified on {pub.channel}: {pub.external_url or pub.external_id}"}, commit=False)
    except DomainError as e:
        log.warning("listed milestone could not be recorded: %s", e)
    v = await db.get(Vehicle, pkg.vehicle_id)
    if v is not None and v.commercial_state == "not_listed":
        try:
            await dispatch(ctx, "vehicles.set_states", {
                "vehicle_id": pkg.vehicle_id, "commercial_state": "listed",
                "reason": f"verified publication on {pub.channel}"}, commit=False)
        except DomainError as e:
            log.warning("listed state could not be recorded: %s", e)


# ── availability (F10) ───────────────────────────────────────────────────────
class AvailabilityIn(BaseModel):
    vehicle_id: str
    availability: str | None = None     # available|reserved|sold|en_route (derived when absent)
    reason: str | None = None


@command("listings.update_availability", input=AvailabilityIn, perm="listings.draft", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)],
         description="A reservation or sale updates the desired availability immediately in AZKT, cancels "
                     "incompatible queued publications and enqueues the channel update under the applicable "
                     "permission. A failed or unapproved channel update keeps a cleanup task until verified.")
async def listings_update_availability(ctx: CommandContext, inp: AvailabilityIn) -> dict:
    v = await _vehicle(ctx.db, inp.vehicle_id)
    pubs = (await ctx.db.execute(select(Publication).where(Publication.vehicle_id == v.id))).scalars().all()
    if not pubs:
        return {"publications": [], "changed": False, "note": "nothing is published for this vehicle"}
    out = []
    for pub in pubs:
        pkg = await ctx.db.get(ListingPackage, pub.package_id) if pub.package_id else None
        listing_class = pkg.listing_class if pkg else derive_class(v)
        desired = inp.availability or availability_for(v, listing_class)
        previous = pub.desired_state
        pub.desired_state = desired
        cancelled = await _cancel_incompatible(ctx, pub, desired)
        ctx.touch(pub, "publication")
        _history(pub, "desired_state", f"{previous} → {desired}", reason=inp.reason)
        entry = {"publication_id": pub.id, "channel": pub.channel, "desired_state": desired,
                 "cancelled_queued": cancelled, "queued": False}
        if previous == desired and pub.state == "verified" and not pub.cleanup_required:
            entry["status"] = "already_verified"
            out.append(entry)
            continue
        if pub.channel not in SUPPORTED_CHANNELS or not pub.external_id:
            pub.cleanup_required = True
            pub.manual_task_id = pub.manual_task_id or await _cleanup_task(ctx, pub, v, desired)
            entry["manual"] = True
            out.append(entry)
            continue
        try:
            res = await dispatch(ctx.child(), "listings.push_availability",
                                 {"publication_id": pub.id, "availability": desired, "reason": inp.reason},
                                 commit=False)
            entry["queued"] = res.status == "ok"
            entry["status"] = res.status
            if res.status != "ok":
                pub.cleanup_required = True
                pub.manual_task_id = pub.manual_task_id or await _cleanup_task(ctx, pub, v, desired)
                entry["approval_id"] = res.approval_id
        except DomainError as e:
            pub.state = "cleanup_pending"
            pub.error = e.message
            pub.cleanup_required = True
            pub.manual_task_id = pub.manual_task_id or await _cleanup_task(ctx, pub, v, desired)
            entry["error"] = e.message
        out.append(entry)
    ctx.record(f"Listing availability updated to {inp.availability or availability_for(v, derive_class(v))} "
               f"on {len(out)} channel(s)", entity_kind="vehicle", entity_id=v.id, kind="listing",
               state="availability", details={"reason": inp.reason, "channels": out})
    ctx.emit("listing.changed", aggregate_type="vehicle", aggregate_id=v.id,
             payload={"change": "availability", "channels": out})
    return {"publications": out, "changed": True}


async def _cancel_incompatible(ctx: CommandContext, pub: Publication, desired: str) -> list[str]:
    """A queued channel write that contradicts the new desired state never leaves (F10).

    That covers the availability reply *and* a publication that has not gone out yet: an approved
    package states an availability, so a package that says "available" must not reach the site once
    the truck is reserved or sold."""
    rows = (await ctx.db.execute(select(ExternalAction).where(
        ExternalAction.entity_kind == "publication", ExternalAction.entity_id == pub.id,
        ExternalAction.state.in_(("intent",))))).scalars().all()
    cancelled, reasons = [], []
    for act in rows:
        why = None
        if act.command_name == "listings.push_availability":
            wanted = (act.payload or {}).get("availability")
            if wanted and wanted != desired:
                why = f"no longer compatible: the desired availability is now {desired}"
        elif act.command_name == "listings.publish":
            queued_pkg = await ctx.db.get(ListingPackage, (act.payload or {}).get("package_id"))
            stated = queued_pkg.availability if queued_pkg is not None else None
            if stated != desired:
                why = (f"the queued publication states availability {stated or 'unknown'} but the truck is now "
                       f"{desired}; it is no longer compatible")
        if why is None:
            continue
        act.state = "cancelled"
        act.error = why
        cancelled.append(act.id)
        reasons.append(why)
        if act.approval_id:
            # the approval is bound to this exact action: it can no longer be executed either
            await dispatch(ctx.child(), "approvals.invalidate",
                           {"approval_id": act.approval_id,
                            "reason": f"availability changed to {desired} — review again"}, commit=False)
    if cancelled:
        await approvals_svc.invalidate_for_entity(ctx, "publication", pub.id,
                                                  f"availability changed to {desired} — review again")
        _history(pub, "cancelled_queued", f"{len(cancelled)} queued action(s) cancelled", reasons=reasons)
        if pub.state in ("queued", "accepted") and not pub.external_id:
            # nothing reached the site and nothing will: say so instead of leaving a stale "queued"
            pub.state = "cleanup_pending"
            pub.cleanup_required = True
            pub.error = ("the approved publication was cancelled before it was sent; rebuild the package for the "
                         f"current availability ({desired}) and approve it again")
    return cancelled


async def _cleanup_task(ctx: CommandContext, pub: Publication, v: Vehicle, desired: str) -> str | None:
    res = await dispatch(ctx.child(), "tasks.create", {
        "title": f"Update {pub.channel} listing to {desired}", "type": "operational", "priority": "high",
        "vehicle_id": v.id,
        "instructions": (f"The {pub.channel} listing must show {desired}. This task stays open until the channel is "
                         f"verified. Listing: {pub.external_url or pub.external_id or 'not mapped'}"),
        "source_kind": "publication", "source_id": pub.id,
        "extra": {"publication_id": pub.id, "desired_state": desired, "cleanup": True}}, commit=False)
    return ((res.data or {}).get("task") or {}).get("id")


class PushAvailabilityIn(BaseModel):
    publication_id: str
    availability: str
    reason: str | None = None


@command("listings.push_availability", input=PushAvailabilityIn, perm="listings.publish",
         action_class="consequential", approval_kind="publish",
         records=lambda p: [("publication", p.publication_id)],
         summary=lambda p: f"Set the website listing to {p.availability}",
         consequence=lambda p: {"scope": "public listing availability", "moves_money": False,
                                "targets": {"publication_id": p.publication_id, "availability": p.availability}},
         limits=lambda p: {"records": [p.publication_id], "fields": ["availability"]},
         description="Push the desired availability to the channel. A reserved truck must never become purchasable; "
                     "the mapping comes from the active site profile.")
async def listings_push_availability(ctx: CommandContext, inp: PushAvailabilityIn) -> dict:
    pub = await ctx.db.get(Publication, inp.publication_id)
    if pub is None:
        raise NotFound("publication not found")
    if not pub.external_id:
        raise Blocked("this listing has no site mapping yet")
    profile = await site_svc.active_profile(ctx.db)
    ok, why = site_svc.writable(profile)
    if not ok:
        raise Blocked(f"website writes are not available: {why}")
    if inp.availability not in wp_adapter.availability_map_for(site_svc.profile_dict(profile)):
        raise Unsupported(f"the active site profile has no availability mapping for {inp.availability!r}")
    pub.desired_state = inp.availability
    pub.cleanup_required = True   # stays true until the channel is verified (F10)
    act = await approvals_svc.intend_external_action(
        ctx, command_name="listings.push_availability", provider="wordpress", entity_kind="publication",
        entity_id=pub.id,
        dedupe_key=await dedupe_key_for(ctx.db, f"listing:availability:{pub.id}:{inp.availability}:{pub.version}"),
        payload={"publication_id": pub.id, "availability": inp.availability, "reason": inp.reason,
                 "profile_id": profile.id, "profile_version": profile.profile_version})
    pub.external_action_id = act.id
    _history(pub, "availability_queued", inp.availability)
    ctx.touch(pub, "publication")
    ctx.record(f"Availability {inp.availability} queued for the {pub.channel} listing", entity_kind="vehicle",
               entity_id=pub.vehicle_id, kind="listing", state="queued",
               details={"publication_id": pub.id, "external_action_id": act.id})
    return {"publication": serialize_publication(pub), "external_action_id": act.id, "queued": True}


@approvals_svc.executor("listings.push_availability")
async def _exec_push_availability(db: AsyncSession, act: ExternalAction) -> dict:
    payload = dict(act.payload or {})
    pub = await db.get(Publication, payload.get("publication_id"))
    if pub is None:
        raise DomainError("publication missing", code="publication_missing")
    profile = await db.get(SiteProfile, payload.get("profile_id")) if payload.get("profile_id") else None
    ok, why = site_svc.writable(profile)
    if not ok:
        pub.error = f"website writes are not available: {why}"
        pub.cleanup_required = True
        _history(pub, "failed", pub.error)
        await db.commit()
        raise Blocked(pub.error)
    prof = site_svc.profile_dict(profile)
    ad = await _adapter(db)
    desired = payload.get("availability") or pub.desired_state
    pub.attempts = (pub.attempts or 0) + 1
    try:
        res = await ad.update_availability(pub.external_id, desired, prof)
    except wp_adapter.UnknownWriteResult as e:
        pub.state = "unknown"
        pub.error = str(e)
        pub.error_kind = "unknown_result"
        pub.cleanup_required = True
        _history(pub, "unknown", str(e))
        await db.commit()
        raise approvals_svc.UnknownResult(str(e), provider_ref=pub.external_id)
    except (ProviderError, Unsupported) as e:
        kind = wp_adapter.error_kind(e) if isinstance(e, ProviderError) else "unsupported"
        pub.state = "cleanup_pending"
        pub.error = f"{kind}: {e}"
        pub.error_kind = kind
        pub.cleanup_required = True
        _history(pub, "cleanup_pending", pub.error)
        await db.commit()
        # the channel refused the change: a failure the owner sees, never a silent hand-off
        raise
    pub.state = "accepted"
    _history(pub, "accepted", f"availability {desired} accepted")
    await db.commit()
    expected = wp_adapter.render_payload({"availability": desired, "status": "publish"}, prof)
    try:
        readback = await ad.read_back(pub.external_id, profile=prof)
    except (ProviderError, Unsupported) as e:
        readback = {"api": {}, "public": {"fetched": False, "error": str(e)}}
    api = readback.get("api") or {}
    mapping = wp_adapter.availability_map_for(prof).get(desired, {})
    api_ok = all(str(api.get(k)) == str(val) for k, val in mapping.items() if k in ("stock_status", "catalog_visibility", "status"))
    public = readback.get("public") or {}
    rendered = public.get("rendered") or {}
    public_ok = None
    if public.get("fetched") and rendered:
        public_ok = all(str(rendered.get(k)) == str(val) for k, val in mapping.items() if k in ("stock_status", "catalog_visibility"))
    pub.observed_state = api.get("stock_status") or api.get("status")
    pub.last_verified_at = now()
    pub.verification = {"checked_at": now().isoformat(), "desired": desired, "expected": expected,
                        "api_ok": api_ok, "public_ok": public_ok, "observed": pub.observed_state}
    if api_ok and public_ok is not False:
        pub.state = "verified"
        pub.cleanup_required = False
        pub.error = None
        _history(pub, "verified", f"availability {desired} verified")
        await _complete_cleanup_task(db, pub)
    else:
        pub.state = "pending_verification" if (pub.attempts or 0) < VERIFY_MAX_ATTEMPTS else "mismatch"
        pub.error = "the channel has not shown the new availability yet"
        _history(pub, pub.state, pub.error)
        await jobs.enqueue(db, "listings.verify", {"publication_id": pub.id, "availability": desired},
                           run_at=now() + timedelta(minutes=5), dedupe_key=f"listing:verify:{pub.id}")
    await db.commit()
    return {"sent": True, "state": pub.state, "provider_ref": pub.external_id, "verification": pub.verification}


async def _complete_cleanup_task(db: AsyncSession, pub: Publication) -> None:
    if not pub.manual_task_id:
        return
    ctx = CommandContext(db=db, actor=SYSTEM_ACTOR, channel="worker")
    try:
        await dispatch(ctx, "tasks.complete", {"task_id": pub.manual_task_id}, commit=False)
        pub.manual_task_id = None
    except DomainError as e:
        log.info("cleanup task not completed: %s", e)


# ── events: reservation / sale / vehicle state ───────────────────────────────
@on_event("vehicle.state_changed")
async def _on_vehicle_state(db: AsyncSession, ev) -> None:
    vehicle_id = ev.aggregate_id or (ev.payload or {}).get("vehicle_id")
    if not vehicle_id:
        return
    await _availability_from_event(db, vehicle_id, reason=f"vehicle state changed ({(ev.payload or {}).get('to')})")


@on_event("sale.changed")
async def _on_sale_changed(db: AsyncSession, ev) -> None:
    payload = ev.payload or {}
    vehicle_id = payload.get("vehicle_id")
    if not vehicle_id:
        return
    await _availability_from_event(db, vehicle_id, reason=f"sale {payload.get('change') or 'changed'}")


async def _availability_from_event(db: AsyncSession, vehicle_id: str, *, reason: str) -> None:
    pubs = (await db.execute(select(Publication).where(Publication.vehicle_id == vehicle_id))).scalars().all()
    if not pubs:
        return
    ctx = CommandContext(db=db, actor=SYSTEM_ACTOR, channel="worker")
    try:
        await dispatch(ctx, "listings.update_availability", {"vehicle_id": vehicle_id, "reason": reason},
                       commit=False)
    except DomainError as e:
        log.warning("availability update from event failed: %s", e)


# ── verification job + reconciliation sweep ──────────────────────────────────
@jobs.job("listings.verify")
async def _verify_job(jctx: jobs.JobContext, payload: dict) -> dict:
    db = jctx.db
    pub = await db.get(Publication, payload.get("publication_id"))
    scan = bool(payload.get("scan"))
    if pub is None or not pub.external_id:
        return {"skipped": "no publication"}
    if pub.state == "unsupported" or (pub.state == "verified" and not scan):
        return {"skipped": pub.state}
    pkg = await db.get(ListingPackage, pub.package_id) if pub.package_id else None
    profile = await db.get(SiteProfile, pub.profile_id) if pub.profile_id else await site_svc.active_profile(db)
    prof = site_svc.profile_dict(profile)
    ad = await _adapter(db)
    if not scan:
        # attempts is the retry budget for a publish that is still settling, not a scan counter
        pub.attempts = (pub.attempts or 0) + 1
    try:
        readback = await ad.read_back(pub.external_id, profile=prof)
    except (ProviderError, Unsupported) as e:
        pub.error = str(e)
        _history(pub, pub.state, f"readback failed: {e}")
        await db.commit()
        return {"error": str(e)}
    if payload.get("availability"):
        mapping = wp_adapter.availability_map_for(prof).get(payload["availability"], {})
        api = readback.get("api") or {}
        rendered = (readback.get("public") or {}).get("rendered") or {}
        api_ok = all(str(api.get(k)) == str(v) for k, v in mapping.items() if k in ("stock_status", "catalog_visibility", "status"))
        public_ok = all(str(rendered.get(k)) == str(v) for k, v in mapping.items()
                        if k in ("stock_status", "catalog_visibility")) if rendered else None
        if api_ok and public_ok is not False:
            pub.state, pub.cleanup_required, pub.error = "verified", False, None
            _history(pub, "verified", f"availability {payload['availability']} verified")
            await _complete_cleanup_task(db, pub)
        elif (pub.attempts or 0) >= VERIFY_MAX_ATTEMPTS:
            pub.state = "mismatch"
            _history(pub, "mismatch", "availability not reflected after bounded retries")
        else:
            await jobs.enqueue(db, "listings.verify", payload, run_at=now() + timedelta(minutes=10),
                               dedupe_key=f"listing:verify:{pub.id}:{pub.attempts}")
        await db.commit()
        return {"state": pub.state}
    if pkg is None:
        return {"skipped": "no package"}
    expected = {**package_with_known_media(pkg, pub), "status": "publish"}
    if pub.desired_state in wp_adapter.availability_map_for(prof):
        # a later reserve/sell changed the intended availability without rebuilding the package: the
        # scan compares the site against what AZKT currently intends, not against a stale package
        expected["availability"] = pub.desired_state
    expected_payload = wp_adapter.render_payload(expected, prof)
    state = await _apply_verification(db, pub, pkg, profile, readback, expected_payload, scan=scan)
    if state == "verified":
        await _record_listed_milestone(db, pub, pkg)
    elif state == "pending_verification":
        await jobs.enqueue(db, "listings.verify", payload, run_at=now() + timedelta(minutes=10),
                           dedupe_key=f"listing:verify:{pub.id}:{pub.attempts}")
    await db.commit()
    return {"state": state}


async def reconcile_unknown(db: AsyncSession) -> dict:
    """An action whose result was lost is reconciled **by mapping** before any retry (F06):
    look the listing up by SKU and by the package hash AZKT wrote into the site's metadata."""
    rows = (await db.execute(select(ExternalAction).where(
        ExternalAction.command_name.in_(("listings.publish", "listings.push_availability")),
        ExternalAction.state == "unknown"))).scalars().all()
    out = {"checked": len(rows), "reconciled": 0, "still_unknown": 0}
    if not rows:
        return out
    for act in rows:
        payload = dict(act.payload or {})
        pub = await db.get(Publication, payload.get("publication_id"))
        pkg = await db.get(ListingPackage, payload.get("package_id")) if payload.get("package_id") else None
        if pub is None:
            continue
        profile = await db.get(SiteProfile, pub.profile_id) if pub.profile_id else await site_svc.active_profile(db)
        prof = site_svc.profile_dict(profile)
        try:
            ad = await _adapter(db)
        except Unsupported:
            out["still_unknown"] += 1
            continue
        external_id = pub.external_id
        if external_id is None and pkg is not None:
            found, _proposal = await _find_existing(ad, package_payload(pkg), prof, pkg)
            external_id = found
        if external_id is None:
            out["still_unknown"] += 1
            _history(pub, "unknown", "no matching listing found on the site; nothing was created")
            continue
        pub.external_id = external_id
        try:
            readback = await ad.read_back(external_id, profile=prof)
        except (ProviderError, Unsupported) as e:
            out["still_unknown"] += 1
            pub.error = str(e)
            continue
        pub.external_url = (readback.get("api") or {}).get("url") or pub.external_url
        if pkg is not None:
            expected = wp_adapter.render_payload({**package_with_known_media(pkg, pub), "status": "publish"}, prof)
            state = await _apply_verification(db, pub, pkg, profile, readback, expected)
            if state == "verified":
                await _record_listed_milestone(db, pub, pkg)
        else:
            pub.state = "accepted"
        act.state = "confirmed"
        act.receipt = {**(act.receipt or {}), "reconciled": True, "external_id": external_id,
                       "at": now().isoformat(), "note": "reconciled by mapping after an unknown result"}
        act.provider_ref = external_id
        from ..models.runtime import Approval
        if act.approval_id:
            a = await db.get(Approval, act.approval_id)
            if a is not None and a.status in ("result_unknown", "queued", "executing"):
                a.status = "confirmed"
                a.receipt = act.receipt
        _history(pub, pub.state, "reconciled by mapping after an unknown result")
        out["reconciled"] += 1
    await db.commit()
    return out


@sweep("listings.availability_scan", AVAILABILITY_SCAN_SECONDS)
async def listings_availability_scan(session_factory) -> dict:
    """Routine hourly scan of every listing that is live on the site (spec §12.3).

    Verification after a publish only re-checks publications that have not settled yet, so a price or
    stock change made later in wp-admin would never be noticed. This re-reads everything that is live
    and lets the ordinary verify job compare it against what AZKT last wrote; a difference in an
    AZKT-owned field pauses writes as drift, exactly as it does right after a publish.
    """
    async with session_factory() as db:
        try:
            await _adapter(db)
        except Unsupported as e:
            return {"setup_blocked": str(e)}
        rows = (await db.execute(select(Publication).where(
            Publication.channel == WEBSITE, Publication.state.in_(LIVE_STATES),
            Publication.external_id.is_not(None)))).scalars().all()
        bucket = now().strftime("%Y%m%d%H")
        for pub in rows:
            await jobs.enqueue(db, "listings.verify", {"publication_id": pub.id, "scan": True},
                               dedupe_key=f"listing:scan:{pub.id}:{bucket}")
        await db.commit()
        return {"scanned": len(rows), "bucket": bucket}


@sweep("listings.reconcile", VERIFY_SECONDS)
async def listings_reconcile_sweep(session_factory) -> dict:
    async with session_factory() as db:
        try:
            await _adapter(db)          # no reachable site: nothing is queued (setup blocked)
        except Unsupported as e:
            return {"setup_blocked": str(e)}
        try:
            out = await reconcile_unknown(db)
        except Exception as e:  # noqa: BLE001
            log.exception("listing reconciliation failed")
            return {"error": f"{type(e).__name__}: {e}"}
        rows = (await db.execute(select(Publication).where(
            Publication.state.in_(("pending_verification", "cleanup_pending"))))).scalars().all()
        for pub in rows:
            await jobs.enqueue(db, "listings.verify", {"publication_id": pub.id},
                               dedupe_key=f"listing:verify:{pub.id}:sweep")
        await db.commit()
        return {**out, "verification_queued": len(rows)}


# ── reads ────────────────────────────────────────────────────────────────────
async def publications(db: AsyncSession, *, vehicle_id: str | None = None, visible: set[str] | None = None) -> list[dict]:
    q = select(Publication)
    if vehicle_id:
        q = q.where(Publication.vehicle_id == vehicle_id)
    rows = (await db.execute(q.order_by(Publication.created_at.desc()).limit(500))).scalars().all()
    if visible is not None:
        rows = [r for r in rows if r.vehicle_id in visible]
    return [serialize_publication(r) for r in rows]


async def package_view(db: AsyncSession, vehicle_id: str, *, channel: str = WEBSITE) -> dict:
    v = await _vehicle(db, vehicle_id)
    profile = await site_svc.active_profile(db)
    pkg = await latest_package(db, vehicle_id, channel=channel)
    candidate = await build(db, v, listing_class=pkg.listing_class if pkg else None, channel=channel,
                            profile=profile, use_model=False)
    pubs = await publications(db, vehicle_id=vehicle_id)
    return {"package": serialize_package(pkg) if pkg else None,
            "diff": diff_packages(pkg, candidate), "readiness": candidate["readiness"],
            "ready": candidate["ready"], "blocked_reasons": candidate["blocked_reasons"],
            "listing_class": candidate["listing_class"], "publications": pubs,
            "profile": {"id": profile.id, "version": profile.profile_version, "status": profile.status,
                        "writes_paused": bool(profile.writes_paused), "reason": profile.pause_reason}
            if profile else None,
            "preview": await preview_payload(db, pkg, profile) if pkg else None}
