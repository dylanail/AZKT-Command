"""Listing package lifecycle and channel publication (spec §7.3, acceptance F05, F06, F07, F09, F10, K04).

What is proved here:

* F09 — an en-route truck states its true status and never invents an arrival date; a ready-for-sale
  truck without verification, documents, price or photos fails its class gates, and a listing class
  whose gates are not configured is blocked rather than skipped;
* F05 — an existing site listing is imported before anything is created; a title-only similarity is a
  proposal that waits for a person;
* F06 — the network dies after the site accepted the write: the result is `unknown`, reconciliation
  maps the existing post and at most one post exists;
* F07 — the API reports success while the public page still shows the old values: the publication is
  `pending_verification` / `mismatch`, never a false `verified`;
* F10 — a reservation updates the desired availability immediately, cancels the incompatible queued
  publication and keeps a cleanup task until the channel is verified; a channel without an adapter is
  `unsupported` with a manual checklist;
* K04 — a verified publication records the `listed` milestone with its source.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from backend.app.adapters import wordpress as wp
from backend.app.core.errors import Blocked
from backend.app.domain.commands import dispatch
from backend.app.models.assets import Asset, AssetLink
from backend.app.models.listings import ListingPackage, Publication, SiteProfile
from backend.app.models.runtime import Approval, ExternalAction
from backend.app.models.tasks import Task
from backend.app.models.vehicles import Vehicle, VehicleMilestone
from backend.app.services import listings as svc
from backend.app.services import site_profile as site_svc
from backend.tests.conftest import ctx_for, login
from backend.tests.fixtures_providers import (STAGING_URL, install_wordpress, run_jobs, wordpress_connections,
                                              wordpress_fake)


def _u() -> str:
    return uuid.uuid4().hex[:8]


def _load(stmt):
    return stmt.execution_options(populate_existing=True)


async def _reset_profiles(db):
    await db.execute(delete(SiteProfile))
    await db.commit()


async def activate_site(db, owner) -> SiteProfile:
    p = (await dispatch(ctx_for(db, owner), "site.discover", {"staging_url": STAGING_URL})).data["profile"]
    await dispatch(ctx_for(db, owner), "site.validate", {"profile_id": p["id"]})
    await dispatch(ctx_for(db, owner), "site.activate", {"profile_id": p["id"]})
    return await db.get(SiteProfile, p["id"])


async def make_vehicle(db, owner, *, logistics_state: str = "received", stock: str | None = None, **kw) -> Vehicle:
    res = await dispatch(ctx_for(db, owner), "vehicles.create", {
        "make": "Daihatsu", "model": "Hijet", "model_year": 2018, "color": "white",
        "stock_no": stock or f"STK-{uuid.uuid4().int % 9000 + 1000}", "logistics_state": logistics_state,
        "create_missing_task": False, **kw})
    return await db.get(Vehicle, res.data["vehicle"]["id"])


async def add_photos(db, vehicle: Vehicle, n: int = 6, *, public: bool = True, slots: list[str] | None = None) -> list[str]:
    ids = []
    for i in range(n):
        a = Asset(kind="photo", storage_key=f"assets/test/{_u()}.jpg", content_type="image/jpeg", size_bytes=1024,
                  sha256=f"sha-{_u()}", status="ready", classification="listing_photo", sensitive=False,
                  public_eligible=public, pre_arrival=False, visibility="internal", derivatives={"status": "ready"})
        db.add(a)
        await db.flush()
        db.add(AssetLink(asset_id=a.id, entity_kind="vehicle", entity_id=vehicle.id, role="photo", position=i,
                         slot=(slots[i] if slots and i < len(slots) else None)))
        ids.append(a.id)
    await db.commit()
    return ids


async def ready_for_sale(db, owner, **kw) -> Vehicle:
    """A truck that satisfies every configured ready-for-sale gate."""
    v = await make_vehicle(db, owner, **kw)
    await add_photos(db, v, 6)
    await dispatch(ctx_for(db, owner), "vehicles.set_asking_price",
                   {"vehicle_id": v.id, "amount": "12500.00", "currency": "USD"})
    v = (await db.execute(_load(select(Vehicle).where(Vehicle.id == v.id)))).scalar_one()
    v.recon_state = "ready_for_sale"
    v.documents_state = "complete"
    v.inspected_at = datetime.now(timezone.utc) - timedelta(days=1)
    v.disclosures = [{"text": "small dent on the left door", "by": owner.id}]
    await db.commit()
    return v


async def build(db, owner, vehicle: Vehicle, **kw) -> dict:
    return (await dispatch(ctx_for(db, owner), "listings.build_package",
                           {"vehicle_id": vehicle.id, "use_model": False, **kw})).data


async def approve(db, owner, approval_id: str) -> dict:
    return (await dispatch(ctx_for(db, owner), "approvals.approve", {"approval_id": approval_id})).data


async def publish(db, owner, package_id: str, *, channel: str = "website") -> tuple[dict, str | None]:
    """Request publication (needs an exact approval) and approve it. Returns (result, approval id)."""
    res = await dispatch(ctx_for(db, owner), "listings.publish", {"package_id": package_id, "channel": channel})
    if res.status == "needs_review":
        approval_id = res.approval_id
        out = await approve(db, owner, approval_id)
        return out, approval_id
    return res.to_dict(), None


async def publication_of(db, vehicle_id: str) -> Publication:
    return (await db.execute(_load(select(Publication).where(Publication.vehicle_id == vehicle_id)))).scalars().first()


# ── F09: truthful classes and class-specific gates ───────────────────────────
@pytest.mark.asyncio
async def test_F09_en_route_draft_is_truthful_and_never_invents_an_eta(db, owner):
    await wordpress_connections(db)
    await _reset_profiles(db)
    with install_wordpress(wordpress_fake()):
        await activate_site(db, owner)
        v = await make_vehicle(db, owner, logistics_state="on_vessel")
        await dispatch(ctx_for(db, owner), "vehicles.set_asking_price",
                       {"vehicle_id": v.id, "amount": "9800.00", "currency": "USD"})
        data = await build(db, owner, v)
        pkg = data["package"]
        assert pkg["listing_class"] == "en_route" and pkg["availability"] == "en_route"
        assert "on the vessel" in pkg["body"]
        assert "not confirmed yet" in pkg["body"], "an unknown arrival date stays unknown"
        assert pkg["evidence"]["eta"] is None
        assert data["ready"] is True, "en-route publishing does not require finished recon"
        gates = {g["requirement"]: g for g in pkg["readiness"]}
        assert gates["eta_sourced"]["ok"] is True and "no arrival date is claimed" in gates["eta_sourced"]["detail"]
        assert gates["status_truthful"]["ok"] is True
        assert "recon_verified" not in gates, "recon is not an en-route gate"

        # a sourced estimate may be repeated, labelled as an estimate with its source
        await dispatch(ctx_for(db, owner), "vehicles.record_milestone", {
            "vehicle_id": v.id, "kind": "arrived_port", "status": "estimated",
            "at": (datetime.now(timezone.utc) + timedelta(days=20)), "source_kind": "carrier",
            "source_ref": "vessel-schedule"})
        data = await build(db, owner, v)
        pkg = data["package"]
        assert pkg["evidence"]["eta"]["source"] == "carrier"
        assert "Estimated arrival" in pkg["body"] and "estimated, source: carrier" in pkg["body"]
        gate = {g["requirement"]: g for g in pkg["readiness"]}["eta_sourced"]
        assert gate["ok"] is True and "carrier" in gate["detail"]


@pytest.mark.asyncio
async def test_F09_ready_for_sale_gates_block_an_unverified_truck(db, owner):
    await wordpress_connections(db)
    await _reset_profiles(db)
    with install_wordpress(wordpress_fake()):
        await activate_site(db, owner)
        v = await make_vehicle(db, owner, logistics_state="received")   # received → ready_for_sale class
        data = await build(db, owner, v)
        gates = {g["requirement"]: g for g in data["package"]["readiness"]}
        assert data["ready"] is False
        assert gates["recon_verified"]["ok"] is False and "needs_inspection" in gates["recon_verified"]["detail"]
        assert gates["documents_ready"]["ok"] is False
        assert gates["approved_price"]["ok"] is False
        assert gates["media_checklist"]["ok"] is False and "0 of 6" in gates["media_checklist"]["detail"]
        assert data["package"]["price"] is None, "no approved price means no price, not a guess"
        # an unready package cannot be submitted or published
        with pytest.raises(Blocked, match="gates"):
            await dispatch(ctx_for(db, owner), "listings.submit_for_review",
                           {"package_id": data["package"]["id"]})
        await db.rollback()
        await db.refresh(owner)

        ready = await ready_for_sale(db, owner)
        data = await build(db, owner, ready)
        assert data["ready"] is True
        assert data["package"]["price"] == "12500.00"
        assert len(data["package"]["media"]) == 6
        assert data["package"]["disclosures"][0]["text"].startswith("small dent")


@pytest.mark.asyncio
async def test_unconfigured_gates_block_publication_instead_of_being_skipped(db, owner):
    await wordpress_connections(db)
    await _reset_profiles(db)
    with install_wordpress(wordpress_fake()):
        profile = await activate_site(db, owner)
        await dispatch(ctx_for(db, owner), "site.set_listing_gates", {
            "profile_id": profile.id,
            "listing_gates": {"en_route": [{"requirement": "status_truthful", "label": "True status"}],
                              "ready_for_sale": [{"requirement": "safety_certificate", "label": "Safety certificate"}]}})
        v = await ready_for_sale(db, owner)
        data = await build(db, owner, v)
        assert data["ready"] is False
        reasons = " ".join(data["package"]["blocked_reasons"])
        assert "not configured" in reasons
        # drafting and browsing still work — only publication is blocked
        assert data["package"]["headline"] and data["package"]["status"] == "draft"


# ── F05: an existing listing is imported before anything is created ──────────
@pytest.mark.asyncio
async def test_F05_existing_product_is_matched_before_creating_a_duplicate(db, owner):
    await wordpress_connections(db)
    await _reset_profiles(db)
    site = wordpress_fake()          # already has product 501 with sku STK-0412
    with install_wordpress(site):
        await activate_site(db, owner)
        v = await ready_for_sale(db, owner, stock="STK-0412")
        data = await build(db, owner, v)
        await dispatch(ctx_for(db, owner), "listings.submit_for_review", {"package_id": data["package"]["id"]})
        before = len(site.items)
        await publish(db, owner, data["package"]["id"])
        await run_jobs()
        pub = await publication_of(db, v.id)
        assert pub.external_id == "501", "the existing product is adopted, never duplicated"
        assert len(site.items) == before
        assert any(h["state"] == "mapped" for h in (pub.history or []))


@pytest.mark.asyncio
async def test_F05_title_only_similarity_is_a_proposal_that_waits_for_a_person(db, owner):
    await wordpress_connections(db)
    await _reset_profiles(db)
    site = wordpress_fake(with_existing=False)
    site.add_product(sku=None, name="2018 Daihatsu Hijet Jumbo", price="11000.00", external_id="777")
    with install_wordpress(site):
        await activate_site(db, owner)
        v = await ready_for_sale(db, owner)
        v.title = "2018 Daihatsu Hijet Jumbo"
        await db.commit()
        data = await build(db, owner, v)
        await dispatch(ctx_for(db, owner), "listings.submit_for_review", {"package_id": data["package"]["id"]})
        before = len(site.items)
        await publish(db, owner, data["package"]["id"])
        await run_jobs()
        pub = await publication_of(db, v.id)
        assert pub.state == "needs_review" and pub.external_id is None
        assert "title only" in (pub.error or "")
        assert len(site.items) == before, "a title guess never creates or overwrites a listing"
        task = (await db.execute(_load(select(Task).where(Task.id == pub.manual_task_id)))).scalar_one()
        assert "existing website listing" in task.title


# ── F06: accepted, then the network dies ─────────────────────────────────────
@pytest.mark.asyncio
async def test_F06_unknown_result_is_reconciled_by_mapping_and_creates_one_post(db, owner):
    await wordpress_connections(db)
    await _reset_profiles(db)
    site = wordpress_fake(with_existing=False)
    with install_wordpress(site):
        await activate_site(db, owner)
        v = await ready_for_sale(db, owner)
        data = await build(db, owner, v)
        await dispatch(ctx_for(db, owner), "listings.submit_for_review", {"package_id": data["package"]["id"]})
        site.fail_after_accept = True          # the site accepts the draft, the response is lost
        await publish(db, owner, data["package"]["id"])
        await run_jobs()

        pub = await publication_of(db, v.id)
        assert pub.state == "unknown" and pub.error_kind == "unknown_result"
        action = (await db.execute(_load(select(ExternalAction).where(
            ExternalAction.command_name == "listings.publish")))).scalars().first()
        assert action.state == "unknown"
        approval = await db.get(Approval, action.approval_id)
        assert approval.status == "result_unknown"
        assert len(site.items) == 1, "the site accepted exactly one write"

        # reconciliation maps the existing post before any retry — never a second post
        out = await svc.reconcile_unknown(db)
        assert out["reconciled"] == 1
        pub = await publication_of(db, v.id)
        assert pub.external_id is not None
        assert pub.state in ("verified", "published", "pending_verification")
        assert len(site.items) == 1
        await db.refresh(action)
        assert action.state == "confirmed" and action.receipt.get("reconciled") is True


# ── F07: API success, stale public page ──────────────────────────────────────
@pytest.mark.asyncio
async def test_F07_stale_public_page_is_pending_verification_not_verified(db, owner):
    await wordpress_connections(db)
    await _reset_profiles(db)
    site = wordpress_fake(with_existing=False)
    with install_wordpress(site):
        await activate_site(db, owner)
        v = await ready_for_sale(db, owner)
        data = await build(db, owner, v)
        await dispatch(ctx_for(db, owner), "listings.submit_for_review", {"package_id": data["package"]["id"]})
        site.public_cache_lag = True           # the CDN keeps serving the previous page
        await publish(db, owner, data["package"]["id"])
        await run_jobs()
        pub = await publication_of(db, v.id)
        assert pub.state in ("pending_verification", "mismatch")
        assert pub.state != "verified"
        assert pub.verification["api"]["ok"] is True
        assert pub.verification["public"]["ok"] in (False, None)
        # the cache catches up and a bounded re-check verifies it
        site.public_cache_lag = False
        site.public[pub.external_id] = dict(site.items[pub.external_id])
        await run_jobs()
        pub = await publication_of(db, v.id)
        assert pub.state == "verified" and pub.last_verified_at is not None


# ── K04: a verified publication records the listed milestone ─────────────────
@pytest.mark.asyncio
async def test_K04_verified_publication_records_the_listed_milestone_with_its_source(db, owner):
    await wordpress_connections(db)
    await _reset_profiles(db)
    site = wordpress_fake(with_existing=False)
    with install_wordpress(site):
        await activate_site(db, owner)
        v = await ready_for_sale(db, owner)
        data = await build(db, owner, v)
        await dispatch(ctx_for(db, owner), "listings.submit_for_review", {"package_id": data["package"]["id"]})
        await publish(db, owner, data["package"]["id"])
        await run_jobs()
        pub = await publication_of(db, v.id)
        assert pub.state == "verified" and pub.external_url

        milestone = (await db.execute(_load(select(VehicleMilestone).where(
            VehicleMilestone.vehicle_id == v.id, VehicleMilestone.kind == "listed",
            VehicleMilestone.is_current.is_(True))))).scalars().first()
        assert milestone is not None and milestone.status == "completed"
        assert milestone.source_kind == "provider"
        assert milestone.source_ref == f"publication:{pub.id}"
        vehicle = (await db.execute(_load(select(Vehicle).where(Vehicle.id == v.id)))).scalar_one()
        assert vehicle.listed_at is not None


# ── F10: reservation cancels queued work and keeps a cleanup task ────────────
@pytest.mark.asyncio
async def test_F10_reservation_cancels_queued_availability_and_keeps_cleanup_until_verified(db, owner):
    await wordpress_connections(db)
    await _reset_profiles(db)
    site = wordpress_fake(with_existing=False)
    with install_wordpress(site):
        await activate_site(db, owner)
        v = await ready_for_sale(db, owner)
        data = await build(db, owner, v)
        await dispatch(ctx_for(db, owner), "listings.submit_for_review", {"package_id": data["package"]["id"]})
        await publish(db, owner, data["package"]["id"])
        await run_jobs()
        pub = await publication_of(db, v.id)
        assert pub.state == "verified" and pub.desired_state == "published"

        # a queued "available" push is waiting when the truck is reserved
        queued = await dispatch(ctx_for(db, owner), "listings.push_availability",
                                {"publication_id": pub.id, "availability": "available"})
        assert queued.status in ("ok", "needs_review")
        res = await dispatch(ctx_for(db, owner), "listings.update_availability",
                             {"vehicle_id": v.id, "availability": "reserved", "reason": "deposit confirmed"})
        entry = res.data["publications"][0]
        pub = await publication_of(db, v.id)
        assert pub.desired_state == "reserved", "AZKT's desired state changes immediately"
        if queued.status == "ok":
            assert entry["cancelled_queued"], "an incompatible queued publication never leaves"
            cancelled = (await db.execute(_load(select(ExternalAction).where(
                ExternalAction.entity_id == pub.id,
                ExternalAction.command_name == "listings.push_availability")))).scalars().all()
            assert any(a.state == "cancelled" for a in cancelled)
        assert pub.cleanup_required is True
        # the channel update runs under the applicable permission and the cleanup task stays open
        await run_jobs()
        pub = await publication_of(db, v.id)
        if pub.state == "verified":
            assert pub.cleanup_required is False
            observed = site.items[pub.external_id]
            assert observed["stock_status"] == "outofstock" and observed["catalog_visibility"] == "visible"
        else:
            assert pub.cleanup_required is True and pub.manual_task_id
            task = await db.get(Task, pub.manual_task_id)
            assert task.status not in ("completed", "cancelled")


@pytest.mark.asyncio
async def test_F10_unsupported_channel_gets_a_manual_checklist_not_a_fake_success(db, owner):
    await wordpress_connections(db)
    await _reset_profiles(db)
    with install_wordpress(wordpress_fake(with_existing=False)):
        await activate_site(db, owner)
        v = await ready_for_sale(db, owner)
        data = await build(db, owner, v, channel="facebook_marketplace")
        await dispatch(ctx_for(db, owner), "listings.submit_for_review",
                       {"package_id": data["package"]["id"], "channel": "facebook_marketplace"})
        out, _ = await publish(db, owner, data["package"]["id"], channel="facebook_marketplace")
        pub = (await db.execute(_load(select(Publication).where(
            Publication.vehicle_id == v.id, Publication.channel == "facebook_marketplace")))).scalars().first()
        assert pub.state == "unsupported" and pub.unsupported_reason
        assert pub.cleanup_required is True and pub.manual_task_id
        task = await db.get(Task, pub.manual_task_id)
        assert "facebook_marketplace" in task.title
        assert "Photos" in task.instructions and "Cleanup" in task.instructions
        # each channel is tracked independently: the website publication is untouched
        website = (await db.execute(_load(select(Publication).where(
            Publication.vehicle_id == v.id, Publication.channel == "website")))).scalars().first()
        assert website is None


# ── approval binding ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_publication_approval_binds_the_package_hash_and_profile_version(db, owner):
    await wordpress_connections(db)
    await _reset_profiles(db)
    site = wordpress_fake(with_existing=False)
    with install_wordpress(site):
        await activate_site(db, owner)
        v = await ready_for_sale(db, owner)
        data = await build(db, owner, v)
        pkg_id = data["package"]["id"]
        await dispatch(ctx_for(db, owner), "listings.submit_for_review", {"package_id": pkg_id})
        res = await dispatch(ctx_for(db, owner), "listings.publish", {"package_id": pkg_id})
        assert res.status == "needs_review"
        approval_id = res.approval_id

        # the price changes after the owner saw the package: the approval no longer applies
        await dispatch(ctx_for(db, owner), "vehicles.set_asking_price",
                       {"vehicle_id": v.id, "amount": "11999.00", "currency": "USD"})
        rebuilt = await build(db, owner, v)
        assert rebuilt["package"]["package_hash"] != data["package"]["package_hash"]
        out = await approve(db, owner, approval_id)
        assert out["executed"] is False
        approval = await db.get(Approval, approval_id)
        assert approval.status == "invalidated" and "review again" in (approval.invalidated_reason or "")
        assert len(site.items) == 0, "an invalidated approval never reaches the site"


@pytest.mark.asyncio
async def test_paused_writes_block_publication_for_the_channel(db, owner):
    await wordpress_connections(db)
    await _reset_profiles(db)
    with install_wordpress(wordpress_fake(with_existing=False)):
        profile = await activate_site(db, owner)
        v = await ready_for_sale(db, owner)
        data = await build(db, owner, v)
        profile.writes_paused = True
        profile.status = "drift"
        profile.pause_reason = "a manual edit changed AZKT-owned field(s) price"
        await db.commit()
        with pytest.raises(Blocked, match="paused|not available"):
            await dispatch(ctx_for(db, owner), "listings.submit_for_review", {"package_id": data["package"]["id"]})


# ── package reads ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_package_view_and_router_surface(client, db, owner):
    await wordpress_connections(db)
    await _reset_profiles(db)
    with install_wordpress(wordpress_fake(with_existing=False)):
        await activate_site(db, owner)
        v = await ready_for_sale(db, owner)
        login(client, owner)
        r = await client.post(f"/api/listings/vehicles/{v.id}/build", json={"use_model": False})
        assert r.status_code == 200 and r.json()["status"] == "ok"
        package_id = r.json()["data"]["package"]["id"]

        r = await client.get(f"/api/listings/vehicles/{v.id}/package")
        body = r.json()
        assert body["package"]["id"] == package_id and body["ready"] is True
        assert body["profile"]["status"] == "active"
        assert body["preview"]["payload"]["name"] == body["package"]["headline"]
        assert body["preview"]["written"] is False

        r = await client.get(f"/api/listings/packages/{package_id}/preview")
        assert r.status_code == 200 and r.json()["valid"] is True
        r = await client.get(f"/api/listings/vehicles/{v.id}/diff")
        assert r.status_code == 200 and r.json()["data"]["diff"]["changed"] == []
        r = await client.get("/api/listings/publications")
        assert r.status_code == 200 and r.json()["channels"]["supported"] == ["website"]


@pytest.mark.asyncio
async def test_specs_carry_evidence_and_unknown_facts_are_omitted(db, owner):
    await wordpress_connections(db)
    await _reset_profiles(db)
    with install_wordpress(wordpress_fake(with_existing=False)):
        await activate_site(db, owner)
        v = await ready_for_sale(db, owner)
        await dispatch(ctx_for(db, owner), "vehicles.propose_fact", {
            "vehicle_id": v.id, "key": "odometer_km", "value": "48211", "status": "reported",
            "source_kind": "auction_sheet", "source_ref": "sheet-1"})
        data = await build(db, owner, v)
        specs = {s["key"]: s for s in data["package"]["specs"]}
        assert specs["odometer_km"]["status"] == "reported"
        assert specs["odometer_km"]["source"] == "auction_sheet"
        assert specs["make"]["value"] == "Daihatsu"
        assert "air_conditioning" not in specs, "an unrecorded fact is never invented"
        assert data["package"]["generated_by"] == "template"
