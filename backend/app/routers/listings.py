"""Listings and site-profile API (spec §7.2–7.3, §12.3).

Reads apply record scope; every write dispatches a command. The preview endpoint renders the mapped
payload without touching the site (invariant 11: no side effect from a GET or a preview).
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import command_context, require
from ..db import get_db
from ..domain.access import assert_vehicle_visible, visible_vehicle_ids
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..models.listings import ListingPackage, SiteProfile
from ..services import listings as svc
from ..services import site_profile as site_svc

router = APIRouter(tags=["listings"])


# ── packages ─────────────────────────────────────────────────────────────────
@router.get("/api/listings/vehicles/{vehicle_id}/package")
async def vehicle_package(vehicle_id: str, channel: str = Query(default=svc.WEBSITE),
                          db: AsyncSession = Depends(get_db),
                          actor: Actor = Depends(require("listings.read"))) -> dict:
    await assert_vehicle_visible(db, actor, vehicle_id)
    return await svc.package_view(db, vehicle_id, channel=channel)


@router.get("/api/listings/packages/{package_id}/preview")
async def package_preview(package_id: str, db: AsyncSession = Depends(get_db),
                          actor: Actor = Depends(require("listings.read"))) -> dict:
    pkg = await db.get(ListingPackage, package_id)
    if pkg is None:
        raise HTTPException(404, "listing package not found")
    await assert_vehicle_visible(db, actor, pkg.vehicle_id)
    profile = await site_svc.active_profile(db)
    return {"package": svc.serialize_package(pkg), **await svc.preview_payload(db, pkg, profile)}


@router.post("/api/listings/vehicles/{vehicle_id}/build")
async def build_package(vehicle_id: str, payload: dict = Body(default={}),
                        ctx: CommandContext = Depends(command_context)) -> dict:
    res = await dispatch(ctx, "listings.build_package", {**(payload or {}), "vehicle_id": vehicle_id})
    return res.to_dict()


@router.get("/api/listings/vehicles/{vehicle_id}/diff")
async def package_diff(vehicle_id: str, channel: str = Query(default=svc.WEBSITE),
                       ctx: CommandContext = Depends(command_context)) -> dict:
    res = await dispatch(ctx, "listings.diff", {"vehicle_id": vehicle_id, "channel": channel})
    return res.to_dict()


@router.post("/api/listings/packages/{package_id}/submit")
async def submit(package_id: str, payload: dict = Body(default={}),
                 ctx: CommandContext = Depends(command_context)) -> dict:
    res = await dispatch(ctx, "listings.submit_for_review", {**(payload or {}), "package_id": package_id})
    return res.to_dict()


@router.post("/api/listings/packages/{package_id}/publish")
async def publish(package_id: str, payload: dict = Body(default={}),
                  ctx: CommandContext = Depends(command_context)) -> dict:
    res = await dispatch(ctx, "listings.publish", {**(payload or {}), "package_id": package_id})
    return res.to_dict()


@router.post("/api/listings/vehicles/{vehicle_id}/link")
async def link_existing(vehicle_id: str, payload: dict = Body(default={}),
                        ctx: CommandContext = Depends(command_context)) -> dict:
    """Bind this vehicle to a listing that already exists on the site (or unlink with no external_id)."""
    res = await dispatch(ctx, "listings.link_existing", {**(payload or {}), "vehicle_id": vehicle_id})
    return res.to_dict()


@router.post("/api/listings/publish-availability")
async def publish_availability(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)) -> dict:
    """Update the desired availability (and enqueue the channel updates under the applicable permission)."""
    res = await dispatch(ctx, "listings.update_availability", payload)
    return res.to_dict()


@router.get("/api/listings/publications")
async def publications(vehicle_id: str | None = Query(default=None), db: AsyncSession = Depends(get_db),
                       actor: Actor = Depends(require("listings.read"))) -> dict:
    limit = await visible_vehicle_ids(db, actor)
    items = await svc.publications(db, vehicle_id=vehicle_id, visible=limit)
    return {"items": items, "channels": {"supported": list(svc.SUPPORTED_CHANNELS)}}


# ── site profile ─────────────────────────────────────────────────────────────
@router.get("/api/site/profile")
async def get_profile(db: AsyncSession = Depends(get_db), actor: Actor = Depends(require("listings.read"))) -> dict:
    return await site_svc.profile_overview(db)


@router.post("/api/site/profile/{action}")
async def profile_action(action: str, payload: dict = Body(default={}),
                         ctx: CommandContext = Depends(command_context)) -> dict:
    commands = {"discover": "site.discover", "validate": "site.validate", "activate": "site.activate",
                "resume": "site.resume_writes", "gates": "site.set_listing_gates",
                "sku": "site.set_sku_strategy", "product-mapping": "site.set_product_mapping"}
    name = commands.get(action)
    if name is None:
        raise HTTPException(404, f"unknown site profile action {action}")
    res = await dispatch(ctx, name, payload or {})
    return res.to_dict()


@router.get("/api/site/profile/{profile_id}")
async def profile_detail(profile_id: str, db: AsyncSession = Depends(get_db),
                         actor: Actor = Depends(require("listings.read"))) -> dict:
    p = await db.get(SiteProfile, profile_id)
    if p is None:
        raise HTTPException(404, "site profile not found")
    return site_svc.serialize_profile(p)
