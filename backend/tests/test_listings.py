"""Listing package lifecycle and channel publication (spec §7.3, acceptance F05–F10, K04).

What is proved here:

* a package is built only from recorded facts with evidence; an en-route listing states the true
  status and never invents an arrival date, and class gates are enforced — an unconfigured gate blocks
  publication instead of being skipped (F09);
* publication is an exact approval bound to package hash + profile version + channel, executed once as
  a persisted external action; an existing site listing is imported before anything is created and a
  title-only similarity is a proposal for a person (F05);
* a lost response after the site accepted the write is `unknown`, reconciled **by mapping** before any
  retry, and never produces a second post (F06);
* an API success with a stale public page is `pending_verification`, never a false `verified`, and
  resolves when the cache catches up (F07);
* reservation/sale updates the desired availability immediately, cancels incompatible queued channel
  work and keeps a cleanup task until the channel is verified; a channel without an adapter is
  `unsupported` with a manual checklist (F10);
* a verified publication is the source of the `listed` milestone on the vehicle timeline (K04).
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from backend.app.core.errors import Blocked
from backend.app.domain.commands import dispatch
from backend.app.models.listings import ListingPackage, Publication, SiteProfile
from backend.app.models.runtime import Approval, ExternalAction
from backend.app.models.tasks import Task
from backend.app.models.vehicles import VehicleMilestone
from backend.app.services import listings as svc
from backend.app.services import site_profile as site_svc
from backend.tests.conftest import ctx_for, login
from backend.tests.fixtures_providers import (STAGING_URL, install_wordpress, jpeg, run_jobs,
                                              wordpress_connections, wordpress_fake)
from backend.tests.test_assets import upload_via_commands

LIGHT_GATES = {
    "en_route": [{"requirement": "status_truthful", "label": "States the true current status"},
                 {"requirement": "eta_sourced", "label": "Any stated ETA is sourced"},
                 {"requirement": "approved_price", "label": "Owner-approved asking price"}],
    "ready_for_sale": [{"requirement": "approved_price", "label": "Owner-approved asking price"},
                       {"requirement": "media_checklist", "label": "Approved public photos", "param": {"min": 1}}],
}


def _u() -> str:
    return uuid.uuid4().hex[:8]


def _load(stmt):
    return stmt.execution_options(populate_existing=True)


async def _profile(db, owner, *, gates: dict | None = LIGHT_GATES) -> SiteProfile:
    """Discover → configure gates → preview on staging → activate. Writes are impossible before this."""
    await db.execute(delete(SiteProfile))
    await db.commit()
    p = (await dispatch(ctx_for(db, owner), "site.discover", {"staging_url": STAGING_URL})).data["profile"]
    if gates is not None:
        await dispatch(ctx_for(db, owner), "site.set_listing_gates", {"profile_id": p["id"], "listing_gates": gates})
    await dispatch(ctx_for(db, owner), "site.validate", {"profile_id": p["id"]})
    await dispatch(ctx_for(db, owner), "site.activate", {"profile_id": p["id"]})
    return await db.get(SiteProfile, p["id"])


async def _vehicle(db, owner, *, price: str | None = "12500.00", photos: int = 1, **kw) -> dict:
    v = (await dispatch(ctx_for(db, owner), "vehicles.create", {
        "make": "Daihatsu", "model": "Hijet", "model_year": 2018, "color": "white",
        "logistics_state": "received", "create_missing_task": False, **kw})).data["vehicle"]
    if price is not None:
        await dispatch(ctx_for(db, owner), "vehicles.set_asking_price",
                       {"vehicle_id": v["id"], "amount": price, "currency": "USD"})
    for i in range(photos):
        # a stable per-vehicle seed: `hash()` is randomized per interpreter run, which made this
        # scenario reuse another test's asset on some runs
        seed = 200 + i * 7 + int(v["id"].replace("-", "")[:8], 16) % 5000
        asset_id = await upload_via_commands(db, owner, jpeg(seed), f"photo-{i}.jpg")
        await dispatch(ctx_for(db, owner), "assets.link", {"asset_id": asset_id, "entity_kind": "vehicle",
                                                           "entity_id": v["id"], "role": "photo", "position": i})
        await dispatch(ctx_for(db, owner), "assets.classify", {"asset_id": asset_id, "classification": "listing_photo",
                                                               "public_eligible": True})
    return (await dispatch(ctx_for(db, owner), "vehicles.update", {"vehicle_id": v["id"], "notes": ""})).data["vehicle"]


async def _build(db, owner, vehicle_id: str, **kw) -> dict:
    res = await dispatch(ctx_for(db, owner), "listings.build_package",
                         {"vehicle_id": vehicle_id, "use_model": False, **kw})
    return res.data


async def _approve_publish(db, owner, package_id: str, *, channel: str = "website",
                           package_hash: str | None = None) -> tuple[Approval, dict]:
    res = await dispatch(ctx_for(db, owner), "listings.publish",
                         {"package_id": package_id, "channel": channel, "expected_package_hash": package_hash})
    assert res.status == "needs_review" and res.approval_id, res.to_dict()
    a = await db.get(Approval, res.approval_id)
    assert a.kind == "publish" and a.consequence["moves_money"] is False
    ok = await dispatch(ctx_for(db, owner), "approvals.approve",
                        {"approval_id": a.id, "expected_version": a.approval_version})
    assert ok.data["executed"] is True, ok.data["approval"].get("invalidated_reason") or ok.data
    await db.refresh(a)
    return a, ok.data


async def _publication(db, vehicle_id: str, channel: str = "website") -> Publication:
    return (await db.execute(_load(select(Publication).where(
        Publication.vehicle_id == vehicle_id, Publication.channel == channel)))).scalars().one()


# ── F09: truthful packages and class gates ──────────────────────────────────
async def test_F09_en_route_package_states_the_truth_and_never_invents_an_eta(db, owner):
    await wordpress_connections(db)
    with install_wordpress(wordpress_fake(with_existing=False)):
        await _profile(db, owner)
        v = await _vehicle(db, owner, logistics_state="on_vessel", stock_no=f"STK-{_u()[:4].upper()}")
        data = await _build(db, owner, v["id"])
        pkg = data["package"]
        assert pkg["listing_class"] == "en_route" and pkg["availability"] == "en_route"
        assert "on the vessel" in pkg["body"] and "not confirmed yet" in pkg["body"]
        assert pkg["evidence"]["eta"] is None
        assert svc.DATE_LIKE.search(pkg["body"]) is None          # no invented arrival date
        assert data["ready"] is True and pkg["generated_by"] == "template"
        assert {g["requirement"]: g["ok"] for g in data["readiness"]} == {
            "status_truthful": True, "eta_sourced": True, "approved_price": True}
        # specs come from the record and are labelled; nothing is asserted that was never recorded
        keys = {s["key"] for s in pkg["specs"]}
        assert {"make", "model", "model_year", "color"} <= keys and "odometer_km" not in keys
        assert all(s["status"] in ("recorded", "confirmed", "reported") for s in pkg["specs"])

        # a sourced estimate may be repeated, with its source and its estimated label
        await dispatch(ctx_for(db, owner), "vehicles.record_milestone", {
            "vehicle_id": v["id"], "kind": "received", "status": "estimated", "at": "2026-10-20T00:00:00Z",
            "source_kind": "exporter", "source_ref": "booking-9912"})
        again = await _build(db, owner, v["id"])
        eta = again["package"]["evidence"]["eta"]
        assert eta["source"] == "exporter" and eta["status"] == "estimated" and eta["at"].startswith("2026-10-20")
        assert "2026-10-20" in again["package"]["body"] and "estimated" in again["package"]["body"]
        assert again["package"]["package_version"] == pkg["package_version"] + 1
        assert "body" in again["diff"]["changed"]


async def test_F09_ready_for_sale_gates_block_a_package_that_is_not_ready(db, owner):
    await wordpress_connections(db)
    with install_wordpress(wordpress_fake(with_existing=False)):
        # the spec's configured gates: recon verified, documents ready, approved price, photo checklist
        await _profile(db, owner, gates=site_svc.DEFAULT_LISTING_GATES)
        v = await _vehicle(db, owner, price=None, photos=0, stock_no=f"STK-{_u()[:4].upper()}")
        data = await _build(db, owner, v["id"])
        assert data["package"]["listing_class"] == "ready_for_sale" and data["ready"] is False
        failing = {g["requirement"]: g["detail"] for g in data["readiness"] if not g["ok"]}
        assert set(failing) == {"recon_verified", "documents_ready", "approved_price", "media_checklist"}
        assert "no owner-approved asking price" in failing["approved_price"]
        assert "0 of 6 approved public photos" in failing["media_checklist"]
        assert data["package"]["price"] is None and data["package"]["evidence"]["price"]["missing"] is True
        with pytest.raises(Blocked, match="does not pass its gates"):
            await dispatch(ctx_for(db, owner), "listings.submit_for_review", {"package_id": data["package"]["id"]})
        await db.rollback()
        await db.refresh(owner)


async def test_F09_unconfigured_gates_block_publication_but_not_drafting(db, owner):
    await wordpress_connections(db)
    with install_wordpress(wordpress_fake(with_existing=False)):
        # gates exist for en-route only: a ready-for-sale package is blocked, never silently allowed
        await _profile(db, owner, gates={"en_route": LIGHT_GATES["en_route"]})
        v = await _vehicle(db, owner, stock_no=f"STK-{_u()[:4].upper()}")
        data = await _build(db, owner, v["id"])
        assert data["package"]["id"] and data["ready"] is False        # drafting still works
        cfg = next(g for g in data["readiness"] if g["requirement"] == "configuration")
        assert cfg["ok"] is False and "no gates are configured for listing class 'ready_for_sale'" in cfg["detail"]
        with pytest.raises(Blocked):
            await dispatch(ctx_for(db, owner), "listings.submit_for_review", {"package_id": data["package"]["id"]})
        await db.rollback()
        await db.refresh(owner)


async def test_media_comes_only_from_approved_public_photos_and_the_hash_covers_them(db, owner):
    await wordpress_connections(db)
    with install_wordpress(wordpress_fake(with_existing=False)):
        await _profile(db, owner)
        v = await _vehicle(db, owner, photos=2, stock_no=f"STK-{_u()[:4].upper()}")
        data = await _build(db, owner, v["id"])
        assert len(data["package"]["media"]) == 2
        assert all(m["sha256"] for m in data["package"]["media_detail"])
        first_hash = data["package"]["package_hash"]
        # an extra photo that nobody approved for the public set does not change the package
        extra = await upload_via_commands(db, owner, jpeg(999), "private.jpg")
        await dispatch(ctx_for(db, owner), "assets.link", {"asset_id": extra, "entity_kind": "vehicle",
                                                           "entity_id": v["id"], "role": "photo", "position": 9})
        same = await _build(db, owner, v["id"])
        assert same["package"]["package_hash"] == first_hash and same["created"] is False
        # approving it does change the package hash and shows in the diff
        await dispatch(ctx_for(db, owner), "assets.classify", {"asset_id": extra, "classification": "listing_photo",
                                                               "public_eligible": True})
        changed = await _build(db, owner, v["id"])
        assert changed["package"]["package_hash"] != first_hash and "media" in changed["diff"]["changed"]


# ── F05: import the existing listing before creating one ─────────────────────
async def test_F05_existing_product_is_matched_before_a_new_one_is_created(db, owner):
    sku = f"STK-{_u()[:4].upper()}"
    site = wordpress_fake(with_existing=False)
    site.add_product(sku=sku, name="2018 Daihatsu Hijet Jumbo", price="9000.00", external_id="501")
    await wordpress_connections(db)
    with install_wordpress(site):
        await _profile(db, owner)
        v = await _vehicle(db, owner, stock_no=sku)          # the site already lists this truck
        pkg = (await _build(db, owner, v["id"]))["package"]
        await dispatch(ctx_for(db, owner), "listings.submit_for_review", {"package_id": pkg["id"]})
        a, _ = await _approve_publish(db, owner, pkg["id"], package_hash=pkg["package_hash"])
        assert a.status == "queued" and a.external_action_id
        act = await db.get(ExternalAction, a.external_action_id)
        assert act.dedupe_key == f"listing:publish:{pkg['id']}:{pkg['package_version']}:website"
        assert await run_jobs() >= 1
        pub = await _publication(db, v["id"])
        assert pub.external_id == "501" and pub.state == "verified"
        assert len(site.items) == 1                       # the existing product was updated, not duplicated
        assert site.items["501"]["regular_price"] == "12500.00" and site.items["501"]["sku"] == sku
        assert [h["state"] for h in pub.history][:2] == ["queued", "mapped"]
        assert pub.package_hash == pkg["package_hash"] and pub.profile_version == 1


async def test_F05_title_only_similarity_is_a_proposal_not_an_automatic_mapping(db, owner):
    site = wordpress_fake()               # "2018 Daihatsu Hijet Jumbo" exists, with a different SKU
    await wordpress_connections(db)
    with install_wordpress(site):
        await _profile(db, owner)
        v = await _vehicle(db, owner, stock_no=None)      # no SKU to match on
        pkg = (await _build(db, owner, v["id"]))["package"]
        assert pkg["headline"] == "2018 Daihatsu Hijet"
        await _approve_publish(db, owner, pkg["id"], package_hash=pkg["package_hash"])
        await run_jobs()
        pub = await _publication(db, v["id"])
        assert pub.state == "needs_review" and "title only" in pub.error
        assert pub.external_id is None and len(site.items) == 1   # nothing created from a guess
        task = await db.get(Task, pub.manual_task_id)
        assert task is not None and "Confirm the existing website listing" in task.title
        assert "501" in task.instructions


# ── F06: accepted then lost → unknown → reconciled by mapping ────────────────
async def test_F06_network_failure_after_accept_is_unknown_and_reconciles_to_one_post(db, owner):
    site = wordpress_fake(with_existing=False)
    await wordpress_connections(db)
    with install_wordpress(site):
        await _profile(db, owner)
        sku = f"STK-{_u()[:4].upper()}"
        v = await _vehicle(db, owner, stock_no=sku)
        pkg = (await _build(db, owner, v["id"]))["package"]
        a, _ = await _approve_publish(db, owner, pkg["id"], package_hash=pkg["package_hash"])
        site.fail_after_accept = True                       # the site accepts, then the connection dies
        await run_jobs()
        pub = await _publication(db, v["id"])
        act = await db.get(ExternalAction, a.external_action_id)
        await db.refresh(act)
        await db.refresh(a)
        assert pub.state == "unknown" and pub.error_kind == "unknown_result" and pub.cleanup_required is True
        assert act.state == "unknown" and a.status == "result_unknown"
        assert len(site.items) == 1                         # the site did accept exactly one write
        created_id = next(iter(site.items))
        assert pub.external_id is None                      # AZKT does not claim what it cannot prove

        # reconciliation maps the listing back before any retry: one post, no duplicate
        out = await svc.reconcile_unknown(db)
        assert out["reconciled"] >= 1 and out["still_unknown"] == 0
        pub = await _publication(db, v["id"])
        await db.refresh(act)
        await db.refresh(a)
        assert pub.external_id == created_id and pub.state in ("verified", "pending_verification")
        assert act.state == "confirmed" and act.receipt["reconciled"] is True and a.status == "confirmed"
        assert len(site.items) == 1
        assert any(h["detail"].startswith("reconciled by mapping") for h in pub.history)


async def test_F06_a_provider_failure_before_accept_is_failed_and_keeps_cleanup(db, owner):
    site = wordpress_fake(with_existing=False)
    await wordpress_connections(db)
    with install_wordpress(site):
        await _profile(db, owner)
        v = await _vehicle(db, owner, stock_no=f"STK-{_u()[:4].upper()}")
        pkg = (await _build(db, owner, v["id"]))["package"]
        a, _ = await _approve_publish(db, owner, pkg["id"], package_hash=pkg["package_hash"])
        site.unsupported_ops.add("upsert_draft")
        await run_jobs()
        pub = await _publication(db, v["id"])
        assert pub.state == "failed" and pub.error_kind == "unsupported" and pub.cleanup_required is True
        assert len(site.items) == 0                      # nothing was written and nothing was invented
        await db.refresh(a)
        assert a.status == "failed"


# ── F07: API success, stale public page ──────────────────────────────────────
async def test_F07_stale_public_page_is_pending_verification_not_verified(db, owner):
    sku = f"STK-{_u()[:4].upper()}"
    site = wordpress_fake(with_existing=False)
    site.add_product(sku=sku, name="2018 Daihatsu Hijet", price="12500.00", external_id="501")
    await wordpress_connections(db)
    with install_wordpress(site):
        await _profile(db, owner)
        v = await _vehicle(db, owner, price="15000.00", stock_no=sku)
        site.set_public_stale("501", "regular_price", "12500.00")     # the CDN still serves the old price
        pkg = (await _build(db, owner, v["id"]))["package"]
        await _approve_publish(db, owner, pkg["id"], package_hash=pkg["package_hash"])
        await run_jobs()
        pub = await _publication(db, v["id"])
        assert pub.state == "pending_verification" and pub.external_id == "501"
        assert pub.verification["api"]["ok"] is True and pub.verification["public"]["ok"] is False
        assert pub.verification["public"]["cache"] == "HIT"
        assert [m["field"] for m in pub.verification["public"]["mismatches"]] == ["price"]
        assert pub.last_verified_at is not None
        # a pending publication is not a listed milestone
        assert await _milestones(db, v["id"]) == []

        # when the cache catches up, the bounded re-check verifies it
        site.public_cache_lag = False
        site.public["501"] = dict(site.items["501"])
        await svc.listings_reconcile_sweep(_sessions())
        assert await run_jobs() >= 1
        pub = await _publication(db, v["id"])
        assert pub.state == "verified" and pub.cleanup_required is False
        assert svc.VERIFY_MAX_ATTEMPTS == 3               # readback/correction is bounded, not endless


def _sessions():
    from backend.app import db as dbmod
    return dbmod.SessionLocal


async def _milestones(db, vehicle_id: str) -> list[VehicleMilestone]:
    return list((await db.execute(_load(select(VehicleMilestone).where(
        VehicleMilestone.vehicle_id == vehicle_id, VehicleMilestone.kind == "listed",
        VehicleMilestone.is_current.is_(True))))).scalars().all())


# ── K04: the listed milestone comes from the verified publication ────────────
async def test_K04_verified_publication_records_the_listed_milestone_with_its_source(db, owner):
    site = wordpress_fake(with_existing=False)
    await wordpress_connections(db)
    with install_wordpress(site):
        await _profile(db, owner)
        v = await _vehicle(db, owner, stock_no=f"STK-{_u()[:4].upper()}")
        pkg = (await _build(db, owner, v["id"]))["package"]
        assert await _milestones(db, v["id"]) == []
        await _approve_publish(db, owner, pkg["id"], package_hash=pkg["package_hash"])
        await run_jobs()
        pub = await _publication(db, v["id"])
        assert pub.state == "verified"
        milestones = await _milestones(db, v["id"])
        assert len(milestones) == 1
        m = milestones[0]
        assert m.status == "completed" and m.source_kind == "provider" and m.source_ref == f"publication:{pub.id}"
        assert m.at is not None and pub.external_url in (m.note or "")
        from backend.app.models.vehicles import Vehicle
        row = await db.get(Vehicle, v["id"])
        await db.refresh(row)
        assert row.listed_at is not None and row.listed_at == m.at
        # replaying the verification does not record a second listed milestone
        await svc._record_listed_milestone(db, pub, await db.get(ListingPackage, pub.package_id))
        await db.commit()
        assert len(await _milestones(db, v["id"])) == 1


# ── F10: reservation, queued channel work and cleanup ────────────────────────
async def test_F10_reservation_cancels_queued_channel_work_and_keeps_a_cleanup_task(db, owner):
    site = wordpress_fake(with_existing=False)
    await wordpress_connections(db)
    with install_wordpress(site):
        await _profile(db, owner)
        v = await _vehicle(db, owner, stock_no=f"STK-{_u()[:4].upper()}")
        pkg = (await _build(db, owner, v["id"]))["package"]
        await _approve_publish(db, owner, pkg["id"], package_hash=pkg["package_hash"])
        await run_jobs()
        pub = await _publication(db, v["id"])
        assert pub.state == "verified" and site.items[pub.external_id]["stock_status"] == "instock"

        # reserved: the desired state changes in AZKT immediately, the channel update awaits authorization
        res = (await dispatch(ctx_for(db, owner), "listings.update_availability",
                              {"vehicle_id": v["id"], "availability": "reserved",
                               "reason": "deposit received"})).data
        entry = res["publications"][0]
        assert entry["desired_state"] == "reserved" and entry["queued"] is False and entry["status"] == "needs_review"
        pub = await _publication(db, v["id"])
        assert pub.desired_state == "reserved" and pub.cleanup_required is True and pub.manual_task_id
        cleanup = await db.get(Task, pub.manual_task_id)
        assert cleanup.status not in ("completed", "cancelled") and "reserved" in cleanup.title
        # the owner authorizes it; the intent is persisted but has not run yet
        approvals = (await db.execute(_load(select(Approval).where(
            Approval.command_name == "listings.push_availability",
            Approval.status == "pending")))).scalars().all()
        pending = next(a for a in approvals if a.payload["publication_id"] == pub.id)
        assert pending.payload["availability"] == "reserved"
        await dispatch(ctx_for(db, owner), "approvals.approve",
                       {"approval_id": pending.id, "expected_version": pending.approval_version})
        queued = (await db.execute(_load(select(ExternalAction).where(
            ExternalAction.entity_id == pub.id,
            ExternalAction.command_name == "listings.push_availability")))).scalars().all()
        assert [q.state for q in queued] == ["intent"] and queued[0].payload["availability"] == "reserved"

        # the truck sells before that reply leaves: the incompatible queued action is cancelled
        sold = (await dispatch(ctx_for(db, owner), "listings.update_availability",
                               {"vehicle_id": v["id"], "availability": "sold", "reason": "sale completed"})).data
        assert sold["publications"][0]["cancelled_queued"] == [queued[0].id]
        await db.refresh(queued[0])
        await db.refresh(pending)
        assert queued[0].state == "cancelled" and "no longer compatible" in (queued[0].error or "") + (pending.invalidated_reason or "")
        assert pending.status == "invalidated"
        await run_jobs()
        await db.refresh(queued[0])
        assert queued[0].state == "cancelled"
        assert site.items[pub.external_id]["stock_status"] == "instock"   # the reserved reply never left
        pub = await _publication(db, v["id"])
        assert pub.desired_state == "sold" and pub.cleanup_required is True
        cleanup = await db.get(Task, pub.manual_task_id)
        await db.refresh(cleanup)
        assert cleanup.status not in ("completed", "cancelled")           # stays open until verified


async def test_F10_a_vehicle_state_change_event_updates_the_desired_availability(db, owner):
    site = wordpress_fake(with_existing=False)
    await wordpress_connections(db)
    with install_wordpress(site):
        await _profile(db, owner)
        v = await _vehicle(db, owner, stock_no=f"STK-{_u()[:4].upper()}")
        pkg = (await _build(db, owner, v["id"]))["package"]
        await _approve_publish(db, owner, pkg["id"], package_hash=pkg["package_hash"])
        await run_jobs()
        await dispatch(ctx_for(db, owner), "vehicles.set_states",
                       {"vehicle_id": v["id"], "commercial_state": "reserved", "reason": "deposit received"})
        from backend.tests.fixtures_providers import drain_events
        await drain_events()
        pub = await _publication(db, v["id"])
        assert pub.desired_state == "reserved" and pub.cleanup_required is True
        assert any(h["state"] == "desired_state" for h in pub.history)


async def test_F10_an_unsupported_channel_is_a_manual_task_with_a_checklist(db, owner):
    site = wordpress_fake(with_existing=False)
    await wordpress_connections(db)
    with install_wordpress(site):
        await _profile(db, owner)
        v = await _vehicle(db, owner, photos=2, stock_no=f"STK-{_u()[:4].upper()}")
        pkg = (await _build(db, owner, v["id"]))["package"]
        a, out = await _approve_publish(db, owner, pkg["id"], channel="facebook_marketplace",
                                        package_hash=pkg["package_hash"])
        pub = await _publication(db, v["id"], "facebook_marketplace")
        assert pub.state == "unsupported" and "no verified adapter" in pub.unsupported_reason
        assert pub.cleanup_required is True and pub.external_id is None
        task = await db.get(Task, pub.manual_task_id)
        assert task is not None and "facebook_marketplace" in task.title
        assert pkg["headline"] in task.instructions and "Photos (2)" in task.instructions
        assert "12500.00 USD" in task.instructions and "Cleanup:" in task.instructions
        assert len(site.items) == 0             # an unsupported channel never writes to the website


# ── approval binding and paused writes ───────────────────────────────────────
async def test_publish_approval_binds_the_package_hash_profile_version_and_channel(db, owner):
    site = wordpress_fake(with_existing=False)
    await wordpress_connections(db)
    with install_wordpress(site):
        profile = await _profile(db, owner)
        v = await _vehicle(db, owner, stock_no=f"STK-{_u()[:4].upper()}")
        pkg = (await _build(db, owner, v["id"]))["package"]
        res = await dispatch(ctx_for(db, owner), "listings.publish",
                             {"package_id": pkg["id"], "expected_package_hash": pkg["package_hash"]})
        a = await db.get(Approval, res.approval_id)
        assert a.payload["expected_package_hash"] == pkg["package_hash"]
        assert a.targets == {"channel": "website", "package_id": pkg["id"]}
        # the price changes after the owner was asked: the approval is invalidated, not executed
        await dispatch(ctx_for(db, owner), "vehicles.set_asking_price",
                       {"vehicle_id": v["id"], "amount": "13900.00", "currency": "USD"})
        await _build(db, owner, v["id"])
        out = (await dispatch(ctx_for(db, owner), "approvals.approve",
                              {"approval_id": a.id, "expected_version": a.approval_version})).data
        assert out["executed"] is False
        await db.refresh(a)
        assert a.status == "invalidated" and "review again" in a.invalidated_reason
        assert "price" in a.invalidated_reason or "package" in a.invalidated_reason
        assert len(site.items) == 0
        assert profile.profile_version == 1


async def test_paused_writes_block_submission_and_publication(db, owner):
    site = wordpress_fake(with_existing=False)
    await wordpress_connections(db)
    with install_wordpress(site):
        profile = await _profile(db, owner)
        v = await _vehicle(db, owner, stock_no=f"STK-{_u()[:4].upper()}")
        pkg = (await _build(db, owner, v["id"]))["package"]
        await site_svc.record_drift(ctx_for(db, owner), profile, source="readback",
                                    reasons=["a manual edit changed AZKT-owned field(s) price"])
        await db.commit()
        with pytest.raises(Blocked, match="writes are not available"):
            await dispatch(ctx_for(db, owner), "listings.submit_for_review", {"package_id": pkg["id"]})
        await db.rollback()
        await db.refresh(owner)
        # asking to publish still only *asks*; the binding is revalidated before anything is written
        res = await dispatch(ctx_for(db, owner), "listings.publish",
                             {"package_id": pkg["id"], "expected_package_hash": pkg["package_hash"]})
        assert res.status == "needs_review"
        a = await db.get(Approval, res.approval_id)
        out = (await dispatch(ctx_for(db, owner), "approvals.approve",
                              {"approval_id": a.id, "expected_version": a.approval_version})).data
        assert out["executed"] is False
        await db.refresh(a)
        assert a.status == "invalidated" and "paused" in a.invalidated_reason
        assert len(site.items) == 0


# ── router surface ───────────────────────────────────────────────────────────
async def test_listings_router_package_preview_and_publications(client, db, owner):
    site = wordpress_fake(with_existing=False)
    await wordpress_connections(db)
    login(client, owner)
    with install_wordpress(site):
        await _profile(db, owner)
        v = await _vehicle(db, owner, stock_no=f"STK-{_u()[:4].upper()}")
        view = (await client.get(f"/api/listings/vehicles/{v['id']}/package")).json()
        assert view["package"] is None and view["ready"] is True and view["listing_class"] == "ready_for_sale"
        assert view["profile"]["writes_paused"] is False
        built = (await client.post(f"/api/listings/vehicles/{v['id']}/build", json={"use_model": False})).json()
        assert built["status"] == "ok"
        pid = built["data"]["package"]["id"]
        preview = (await client.get(f"/api/listings/packages/{pid}/preview")).json()
        assert preview["written"] is False and preview["valid"] is True
        assert preview["payload"]["regular_price"] == "12500.00" and preview["payload"]["status"] == "draft"
        assert site.items == {}                                  # a preview never writes
        diff = (await client.get(f"/api/listings/vehicles/{v['id']}/diff")).json()
        assert diff["status"] == "ok" and diff["data"]["current"]["id"] == pid
        sub = (await client.post(f"/api/listings/packages/{pid}/submit", json={})).json()
        assert sub["status"] == "ok" and sub["data"]["preview"]["written"] is False
        pub = (await client.post(f"/api/listings/packages/{pid}/publish",
                                 json={"expected_package_hash": built["data"]["package"]["package_hash"]})).json()
        assert pub["status"] == "needs_review" and pub["approval_id"]
        empty = (await client.get("/api/listings/publications", params={"vehicle_id": v["id"]})).json()
        assert empty["items"] == [] and site.items == {}      # asking is not publishing
        detail = (await client.get(f"/api/approvals/{pub['approval_id']}")).json()
        assert detail["kind"] == "publish" and detail["action_class"] == "consequential"
        assert detail["can_decide"] is True and detail["payload"]["channel"] == "website"
        ok = await client.post(f"/api/approvals/{pub['approval_id']}/approve",
                               json={"expected_version": detail["version"]})
        assert ok.status_code == 200 and ok.json()["data"]["executed"] is True
        listed = (await client.get("/api/listings/publications", params={"vehicle_id": v["id"]})).json()
        assert listed["channels"]["supported"] == ["website"]
        assert [p["state"] for p in listed["items"]] == ["queued"]
        assert await run_jobs() >= 1
        done = (await client.get("/api/listings/publications", params={"vehicle_id": v["id"]})).json()
        assert done["items"][0]["state"] == "verified" and done["items"][0]["external_url"]


async def test_listings_reconcile_sweep_is_registered():
    from backend.app.domain.jobs import SWEEPS
    assert SWEEPS["listings.reconcile"][1] == svc.VERIFY_SECONDS == 15 * 60


# ── review regressions ───────────────────────────────────────────────────────
async def test_a_retry_after_a_failed_publish_actually_runs(db, owner):
    """A publication that failed at the provider must be re-publishable. Reusing the dead external
    action would leave the new approval reading 'confirmed' while nothing was ever sent."""
    site = wordpress_fake(with_existing=False)
    await wordpress_connections(db)
    with install_wordpress(site):
        await _profile(db, owner)
        v = await _vehicle(db, owner, stock_no=f"STK-{_u()[:4].upper()}")
        pkg = (await _build(db, owner, v["id"]))["package"]
        first, _ = await _approve_publish(db, owner, pkg["id"], package_hash=pkg["package_hash"])
        site.unsupported_ops.add("upsert_draft")
        await run_jobs()
        pub = await _publication(db, v["id"])
        assert pub.state == "failed" and len(site.items) == 0
        # the site is fixed and the owner approves the same package again
        site.unsupported_ops.discard("upsert_draft")
        second, _ = await _approve_publish(db, owner, pkg["id"], package_hash=pkg["package_hash"])
        assert second.id != first.id
        assert second.external_action_id and second.external_action_id != first.external_action_id
        assert second.status == "queued"          # an executable intent, not a confirmed no-op
        assert await run_jobs() >= 1
        pub = await _publication(db, v["id"])
        await db.refresh(second)
        assert pub.state == "verified" and second.status == "confirmed"
        assert len(site.items) == 1               # exactly one post, never two


async def test_a_reservation_cancels_a_queued_publication_before_it_lists_in_stock(db, owner):
    """The approved package says 'available'. If the truck is reserved before the worker runs, that
    queued publication must not go out — a reserved truck is never listed in stock (F10)."""
    site = wordpress_fake(with_existing=False)
    await wordpress_connections(db)
    with install_wordpress(site):
        await _profile(db, owner)
        v = await _vehicle(db, owner, stock_no=f"STK-{_u()[:4].upper()}")
        pkg = (await _build(db, owner, v["id"]))["package"]
        assert pkg["availability"] == "available"
        a, _ = await _approve_publish(db, owner, pkg["id"], package_hash=pkg["package_hash"])
        res = (await dispatch(ctx_for(db, owner), "listings.update_availability",
                              {"vehicle_id": v["id"], "availability": "reserved",
                               "reason": "deposit received"})).data
        assert res["publications"][0]["cancelled_queued"] == [a.external_action_id]
        await run_jobs()
        act = await db.get(ExternalAction, a.external_action_id)
        await db.refresh(act)
        assert act.state == "cancelled"
        assert site.items == {}                   # nothing was published as in stock
        pub = await _publication(db, v["id"])
        assert pub.desired_state == "reserved" and pub.cleanup_required is True


async def test_F08_a_changed_price_on_the_site_pauses_writes_without_an_editor_marker(db, owner):
    """Drift is detected by comparing the readback with what AZKT wrote. A real WordPress row carries
    no 'who edited this' field, so the pause must not depend on one."""
    sku = f"STK-{_u()[:4].upper()}"
    site = wordpress_fake(with_existing=False)
    site.add_product(sku=sku, name="2018 Daihatsu Hijet", price="1.00", external_id="501")
    await wordpress_connections(db)
    with install_wordpress(site):
        profile = await _profile(db, owner)
        v = await _vehicle(db, owner, stock_no=sku)
        site.set_public_stale("501", "regular_price", "1.00")      # the CDN lags: pending_verification
        pkg = (await _build(db, owner, v["id"]))["package"]
        await _approve_publish(db, owner, pkg["id"], package_hash=pkg["package_hash"])
        await run_jobs()
        pub = await _publication(db, v["id"])
        assert pub.state == "pending_verification"
        await db.refresh(profile)
        assert profile.writes_paused is False
        # someone edits the managed price in wp-admin; the live row records no editor
        site.manual_edit("501", "regular_price", "9900.00", editor=None)
        await svc.listings_reconcile_sweep(_sessions())
        await run_jobs()
        pub = await _publication(db, v["id"])
        assert pub.state == "mismatch" and "9900.00" in (pub.error or "")
        await db.refresh(profile)
        assert profile.status == "drift" and profile.writes_paused is True
        ok, why = site_svc.writable(profile)
        assert ok is False and "price" in (why or "")


async def test_site_validate_previews_a_real_package_without_writing(db, owner):
    """`site.validate` with a package renders that package's payload; media are objects, not ids."""
    site = wordpress_fake(with_existing=False)
    await wordpress_connections(db)
    with install_wordpress(site):
        await _profile(db, owner)
        v = await _vehicle(db, owner, photos=2, stock_no=f"STK-{_u()[:4].upper()}")
        pkg = (await _build(db, owner, v["id"]))["package"]
        profile = await site_svc.active_profile(db)
        res = (await dispatch(ctx_for(db, owner), "site.validate",
                              {"profile_id": profile.id, "package_id": pkg["id"]})).data
        payload = res["preview"]["payload"]
        assert res["preview"]["ok"] is True and res["preview"]["written"] is False
        assert payload["regular_price"] == "12500.00" and payload["status"] == "draft"
        assert len(payload["images"]) == 2 and all(i["sha256"] for i in payload["images"])
        assert site.items == {}


async def test_F09_model_copy_that_states_a_date_other_than_the_sourced_one_is_refused(db, owner, monkeypatch):
    """A sourced estimate may be repeated. A *different* date is invented, whether or not an ETA
    exists, so the deterministic template is used instead."""
    await wordpress_connections(db)
    with install_wordpress(wordpress_fake(with_existing=False)):
        await _profile(db, owner)
        v = await _vehicle(db, owner, logistics_state="on_vessel", stock_no=f"STK-{_u()[:4].upper()}")
        await dispatch(ctx_for(db, owner), "vehicles.record_milestone", {
            "vehicle_id": v["id"], "kind": "received", "status": "estimated", "at": "2026-10-20T00:00:00Z",
            "source_kind": "exporter", "source_ref": "booking-9912"})

        class _Res:
            text = "2018 Daihatsu Hijet\nArrives 2026-11-05, guaranteed."

        calls: list[dict] = []

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def complete(self, **kw):
                calls.append(kw)
                return _Res()

        monkeypatch.setattr(svc, "model_available", lambda: True)
        monkeypatch.setattr(svc, "ModelClient", _Client)
        data = await dispatch(ctx_for(db, owner), "listings.build_package",
                              {"vehicle_id": v["id"], "use_model": True})
        pkg = data.data["package"]
        assert len(calls) == 1                    # the model answered; the *scrub* rejected its date
        assert pkg["generated_by"] == "template"
        assert "2026-11-05" not in pkg["body"] and "2026-10-20" in pkg["body"]
        assert "estimated" in pkg["body"] and "exporter" in pkg["body"]
