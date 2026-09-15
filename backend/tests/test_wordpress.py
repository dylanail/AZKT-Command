"""Adaptive website adapter and the versioned site profile (spec §7.2, acceptance F04, F08).

What is proved here:

* discovery reports what the site *actually* is — WooCommerce products versus a custom vehicle post
  type — together with the auth capabilities of each credential, and records the limitation instead of
  assuming a write works (F04);
* a profile cannot become the write authority until a preview has been rendered against the staging
  URL: no preview, no writes, and a preview never touches production (F04, invariant 11);
* a manual edit by a human editor to an AZKT-owned field pauses writes for the channel and asks for a
  new profile version, while unrelated editor-owned fields survive every AZKT write (F08);
* unsupported operations return a typed reason, and sitewide payment/tax/plugin settings are never
  changed by a listing operation.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from backend.app.adapters import wordpress as wp
from backend.app.core.errors import Blocked, Denied, Unsupported, ValidationFailed
from backend.app.domain.commands import dispatch
from backend.app.models.listings import SiteProfile
from backend.app.services import site_profile as site_svc
from backend.tests.conftest import ctx_for, login
from backend.tests.fixtures_providers import (SITE_URL, STAGING_URL, install_wordpress, wordpress_connections,
                                              wordpress_fake)


def _u() -> str:
    return uuid.uuid4().hex[:8]


def _load(stmt):
    return stmt.execution_options(populate_existing=True)


async def _reset_profiles(db):
    """One site profile chain per database: start each scenario from no profile at all."""
    from sqlalchemy import delete
    await db.execute(delete(SiteProfile))
    await db.commit()


async def _discover(db, owner, **kw) -> dict:
    return (await dispatch(ctx_for(db, owner), "site.discover", {"staging_url": STAGING_URL, **kw})).data


# ── F04: discovery tells the truth about the installed site ──────────────────
async def test_F04_discovery_identifies_products_versus_a_custom_vehicle_type(db, owner):
    await wordpress_connections(db)
    await _reset_profiles(db)
    # a plain WooCommerce install: vehicles are products
    with install_wordpress(wordpress_fake()):
        data = await _discover(db, owner)
        p = data["profile"]
        assert p["content_type"] == "product" and p["provider"] == "woocommerce" and p["status"] == "draft"
        assert p["discovered"]["woocommerce"]["present"] is True
        assert p["discovered"]["auth"]["woocommerce"] == {"read": True, "write": True}
        assert "publish_posts" in p["discovered"]["auth"]["wordpress"]["capabilities"]
        assert p["field_map"]["price"] == "regular_price" and p["field_map"]["title"] == "name"
        assert p["validation"]["sku_convention"] == "stock_no"
        assert set(p["supported_ops"]) >= {"discover", "preview", "upsert_draft", "publish",
                                           "update_availability", "read_back", "archive"}
        # field ownership is explicit: AZKT owns the listing fields, the site owns its own
        assert p["field_ownership"]["price"] == "azkt" and p["field_ownership"]["seo"] == "site"
        assert p["field_ownership"]["payment_tax_shipping"] == "site"

    # the same site with a custom vehicle post type is *not* treated as a product catalogue
    await _reset_profiles(db)
    with install_wordpress(wordpress_fake(custom_type=True, with_existing=False)):
        data = await _discover(db, owner)
        p = data["profile"]
        assert p["content_type"] == "custom" and p["provider"] == "wordpress"
        assert p["discovered"]["custom_post_type"] == "vehicles"
        assert [t["slug"] for t in p["discovered"]["vehicle_post_types"]] == ["azkt_vehicle"]
        assert p["field_map"]["price"] == "meta.vehicle_price"   # custom fields, not Woo pricing
        assert site_svc.profile_dict(await site_svc.latest_profile(db))["rest_base"] == "vehicles"


async def test_F04_unproven_auth_capability_is_recorded_as_a_limitation(db, owner):
    """WooCommerce keys that cannot read system status do not prove a write: say so, do not assume."""
    await wordpress_connections(db)
    await _reset_profiles(db)
    site = wordpress_fake()
    site.woo_auth_ok = False
    with install_wordpress(site):
        p = (await _discover(db, owner))["profile"]
        assert any("writes are not proven" in l for l in p["limitations"])
        assert p["discovered"]["auth"]["woocommerce"]["write"] is False


async def test_F04_writes_need_a_passing_staging_preview_first(db, owner):
    await wordpress_connections(db)
    await _reset_profiles(db)
    with install_wordpress(wordpress_fake()):
        p = (await _discover(db, owner))["profile"]
        profile = await db.get(SiteProfile, p["id"])
        # no preview yet: the channel is not writable and activation is refused
        assert site_svc.writable(profile) == (False, "site profile is draft")
        with pytest.raises(Blocked, match="preview the profile on staging"):
            await dispatch(ctx_for(db, owner), "site.activate", {"profile_id": p["id"]})
        await db.rollback()
        await db.refresh(owner)
        # a preview renders the mapped payload and writes nothing
        site = wp.current_adapter()
        before = dict(site.items)
        res = (await dispatch(ctx_for(db, owner), "site.validate", {"profile_id": p["id"]})).data
        assert res["preview"]["ok"] is True and res["preview"]["written"] is False
        assert res["preview"]["staging_url"] == STAGING_URL and res["preview"]["payload"]["status"] == "draft"
        assert site.items == before and ("preview", "") in site.calls
        await db.refresh(profile)
        assert profile.status == "validated" and profile.validated_at is not None
        # the owner activates the version; only then is the channel writable
        act = (await dispatch(ctx_for(db, owner), "site.activate", {"profile_id": p["id"]})).data
        assert act["activated"] is True
        await db.refresh(profile)
        assert profile.status == "active" and site_svc.writable(profile) == (True, None)
        assert profile.activated_by == owner.id


async def test_preview_never_targets_the_live_site(db, owner):
    await wordpress_connections(db)
    await _reset_profiles(db)
    with install_wordpress(wordpress_fake()):
        p = (await _discover(db, owner))["profile"]
        with pytest.raises(ValidationFailed, match="must differ from the live site"):
            await dispatch(ctx_for(db, owner), "site.validate", {"profile_id": p["id"], "staging_url": SITE_URL})
        await db.rollback()
        await db.refresh(owner)
    # with no staging URL at all the answer is an honest setup block, not a production preview
    await _reset_profiles(db)
    await wordpress_connections(db, staging_url="")
    with install_wordpress(wordpress_fake()):
        p = (await dispatch(ctx_for(db, owner), "site.discover", {})).data["profile"]
        with pytest.raises(Blocked) as exc:
            await dispatch(ctx_for(db, owner), "site.validate", {"profile_id": p["id"]})
        assert exc.value.detail.get("setup_blocked") == "wordpress.staging_url"
        await db.rollback()
        await db.refresh(owner)
    await wordpress_connections(db)


async def test_discovery_is_owner_only_and_a_new_version_supersedes_the_previous(db, owner, manager):
    await wordpress_connections(db)
    await _reset_profiles(db)
    with install_wordpress(wordpress_fake()):
        with pytest.raises(Denied):
            await dispatch(ctx_for(db, manager), "site.discover", {"staging_url": STAGING_URL})
        await db.rollback()
        await db.refresh(owner)
        first = (await _discover(db, owner))["profile"]
        await dispatch(ctx_for(db, owner), "site.validate", {"profile_id": first["id"]})
        await dispatch(ctx_for(db, owner), "site.activate", {"profile_id": first["id"]})
        second = (await _discover(db, owner))["profile"]
        assert second["profile_version"] == first["profile_version"] + 1 and second["status"] == "draft"
        # the active version keeps writing until the new one is validated and activated
        assert (await site_svc.active_profile(db)).id == first["id"]
        await dispatch(ctx_for(db, owner), "site.validate", {"profile_id": second["id"]})
        out = (await dispatch(ctx_for(db, owner), "site.activate", {"profile_id": second["id"]})).data
        assert out["superseded"] == [first["id"]]
        assert (await site_svc.active_profile(db)).id == second["id"]
        old = await db.get(SiteProfile, first["id"])
        await db.refresh(old)
        assert old.status == "superseded"


# ── F08: manual edits and drift ──────────────────────────────────────────────
async def test_F08_manual_edit_to_an_owned_field_is_drift_and_unrelated_fields_survive(db, owner):
    """A person edits the price in wp-admin. That is a conflict on an AZKT-owned field: writes for the
    channel pause and a new profile version is requested. The editor's own metadata is untouched by
    every AZKT write, and nothing sitewide is changed."""
    await wordpress_connections(db)
    await _reset_profiles(db)
    site = wordpress_fake()
    site.add_product(sku="STK-DRIFT", name="2018 Daihatsu Hijet Drift", price="12500.00", external_id="777",
                     meta={"seo_title": "Best kei truck in Phoenix", "rank_math_focus_keyword": "kei truck"})
    with install_wordpress(site):
        p = (await _discover(db, owner))["profile"]
        await dispatch(ctx_for(db, owner), "site.validate", {"profile_id": p["id"]})
        await dispatch(ctx_for(db, owner), "site.activate", {"profile_id": p["id"]})
        profile = await db.get(SiteProfile, p["id"])
        expected = wp.render_payload({"headline": "2018 Daihatsu Hijet Drift", "body": "Ready for sale.",
                                      "price": "12500.00", "sku": "STK-DRIFT", "availability": "available",
                                      "package_hash": "hash-1", "vehicle_id": "veh-1"},
                                     site_svc.profile_dict(profile))
        await site.publish("777", {"headline": "2018 Daihatsu Hijet Drift", "body": "Ready for sale.",
                                   "price": "12500.00", "sku": "STK-DRIFT", "availability": "available",
                                   "package_hash": "hash-1", "vehicle_id": "veh-1"},
                           site_svc.profile_dict(profile))
        observed = (await site.read_back("777", profile=site_svc.profile_dict(profile)))["api"]
        # the editor's own metadata survived the AZKT write (unrelated fields preserved)
        assert observed["meta"]["seo_title"] == "Best kei truck in Phoenix"
        assert observed["meta"]["azkt_package_hash"] == "hash-1"
        assert site_svc.compare_managed(expected, observed, profile)["mismatches"] == []

        # now a human edits the managed price in wp-admin
        site.manual_edit("777", "regular_price", "9900.00", editor="human")
        observed = (await site.read_back("777", profile=site_svc.profile_dict(profile)))["api"]
        cmp = site_svc.compare_managed(expected, observed, profile)
        assert [m["field"] for m in cmp["mismatches"]] == ["price"]
        assert cmp["mismatches"][0]["observed"] == "9900.00" and cmp["edited_by"] == "human"
        assert "seo_title" in cmp["editor_fields"] and "rank_math_focus_keyword" in cmp["editor_fields"]

        drift = await site_svc.record_drift(ctx_for(db, owner), profile, source="readback",
                                            reasons=["a manual edit changed AZKT-owned field(s) price"],
                                            detail={"mismatches": cmp["mismatches"]})
        await db.commit()
        assert drift["drift"] is True
        await db.refresh(profile)
        assert profile.status == "drift" and profile.writes_paused is True
        assert "manual edit" in profile.pause_reason and profile.drift_detected_at is not None
        ok, why = site_svc.writable(profile)
        assert ok is False and "manual edit" in why
        # a review task is raised for the person, and nothing sitewide was touched
        from backend.app.models.tasks import Task
        task = (await db.execute(_load(select(Task).where(
            Task.source_kind == "site_profile",
            Task.title == "Review website profile drift and re-validate")))).scalars().first()
        assert task is not None and task.priority == "high"
        assert task.status not in ("completed", "cancelled")
        assert site.settings_changes == 0

        # resuming needs a fresh passing preview, then writes continue
        with pytest.raises(Blocked, match="re-validate"):
            profile.preview = {"ok": False}
            await dispatch(ctx_for(db, owner), "site.resume_writes", {"profile_id": profile.id})
        await db.rollback()
        await db.refresh(owner)
        await db.refresh(profile)
        await dispatch(ctx_for(db, owner), "site.validate", {"profile_id": profile.id})
        res = (await dispatch(ctx_for(db, owner), "site.resume_writes", {"profile_id": profile.id})).data
        assert res["resumed"] is True
        await db.refresh(profile)
        assert profile.writes_paused is False and profile.drift["resolved_by"] == owner.id


async def test_schema_delta_between_profile_versions_is_reported(db, owner):
    await wordpress_connections(db)
    await _reset_profiles(db)
    site = wordpress_fake()
    with install_wordpress(site):
        await _discover(db, owner)
        site.namespaces = [n for n in site.namespaces if n != "wc/v3"]   # WooCommerce disabled by a plugin change
        data = await _discover(db, owner)
        assert any("REST namespaces removed" in c for c in data["changes"])
        assert any("content type changed" in c for c in data["changes"])
        assert data["profile"]["content_type"] == "post"


# ── adapter contract (spec §12.3) ────────────────────────────────────────────
async def test_unsupported_operations_return_a_typed_reason_not_a_fake_success(db):
    site = wordpress_fake()
    with pytest.raises(Unsupported, match="never installs plugins"):
        await site.install_plugin("some-plugin")
    with pytest.raises(Unsupported, match="never changed"):
        await site.update_site_settings({"currency": "JPY"})
    site.unsupported_ops.add("archive")
    assert "archive" not in (await site.discover())["supported_ops"]
    with pytest.raises(Unsupported, match="not supported by this site profile"):
        await site.archive("501")
    # an availability the profile does not map is unsupported, never a guessed toggle
    with pytest.raises(Unsupported, match="no availability mapping"):
        await site.update_availability("501", "layaway", {"content_type": "product"})


def test_reserved_trucks_never_become_purchasable():
    """A generic stock toggle must not make a reserved truck buyable (spec §7.2)."""
    mapping = wp.availability_map_for({"content_type": "product"})
    assert mapping["reserved"] == {"stock_status": "outofstock", "catalog_visibility": "visible", "purchasable": False}
    assert mapping["sold"]["catalog_visibility"] == "hidden"
    assert mapping["available"]["purchasable"] is False        # inquiry-only checkout behaviour preserved
    assert mapping["en_route"]["stock_status"] == "onbackorder"
    payload = wp.render_payload({"headline": "x", "body": "y", "availability": "reserved", "price": "1"},
                                {"content_type": "product"})
    assert payload["stock_status"] == "outofstock" and payload["catalog_visibility"] == "visible"
    assert "purchasable" not in payload or payload["purchasable"] is False


def test_render_payload_maps_only_declared_fields_and_carries_media_checksums():
    package = {"headline": "2018 Daihatsu Hijet", "body": "Body copy.", "short_description": "Short.",
               "price": "12500.00", "sku": "STK-0412", "availability": "available",
               "media": [{"url": "/api/assets/a1/web", "sha256": "abc", "alt": "front"}],
               "disclosures": [{"text": "Rust on the left rocker"}], "specs": [{"key": "make", "value": "Daihatsu"}],
               "package_hash": "h1", "vehicle_id": "v1"}
    product = wp.render_payload(package, {"content_type": "product"})
    assert product["name"] == "2018 Daihatsu Hijet" and product["regular_price"] == "12500.00"
    assert product["sku"] == "STK-0412" and product["images"][0]["sha256"] == "abc"
    assert product["meta_data"]["azkt_package_hash"] == "h1" and product["meta_data"]["azkt_vehicle_id"] == "v1"
    post = wp.render_payload(package, {"content_type": "post"})
    assert post["title"] == "2018 Daihatsu Hijet" and post["meta"]["vehicle_price"] == "12500.00"
    assert post["meta"]["gallery"][0]["sha256"] == "abc" and post["meta"]["stock_no"] == "STK-0412"


def test_validate_package_refuses_a_package_without_an_approved_price():
    profile = {"content_type": "product", "validation": {"required_fields": ["headline", "body"]},
               "media_rules": {"min": 1}}
    res = wp.validate_package({"headline": "x", "body": "y", "price": None, "media": []}, profile)
    assert res["ok"] is False
    assert "no approved price" in res["errors"] and any("at least 1 images" in e for e in res["errors"])
    ok = wp.validate_package({"headline": "x", "body": "y", "price": "1.00",
                              "media": [{"url": "/a", "sha256": "s"}]}, profile)
    assert ok["ok"] is True and ok["errors"] == []


async def test_missing_website_credentials_report_setup_blocked(db):
    from backend.app.core.config import settings
    wp.set_adapter(None)
    prev = (settings.WP_BASE_URL, settings.WP_APP_USER, settings.WP_APP_PASSWORD,
            settings.WC_CONSUMER_KEY, settings.WC_CONSUMER_SECRET, settings.ENV)
    settings.WP_BASE_URL = settings.WP_APP_USER = settings.WP_APP_PASSWORD = ""
    settings.WC_CONSUMER_KEY = settings.WC_CONSUMER_SECRET = ""
    settings.ENV = "development"
    try:
        with pytest.raises(Unsupported) as exc:
            wp.adapter_for(None, None)
        assert exc.value.detail.get("setup_blocked") == "wordpress.site_url"
    finally:
        (settings.WP_BASE_URL, settings.WP_APP_USER, settings.WP_APP_PASSWORD,
         settings.WC_CONSUMER_KEY, settings.WC_CONSUMER_SECRET, settings.ENV) = prev


async def test_site_profile_router_reads_and_actions(client, db, owner):
    await wordpress_connections(db)
    await _reset_profiles(db)
    login(client, owner)
    with install_wordpress(wordpress_fake()):
        empty = (await client.get("/api/site/profile")).json()
        assert empty["active"] is None and empty["writes_paused"] is True
        assert "no active site profile" in empty["reason"]
        disc = (await client.post("/api/site/profile/discover", json={"staging_url": STAGING_URL})).json()
        assert disc["status"] == "ok"
        pid = disc["data"]["profile"]["id"]
        assert (await client.post("/api/site/profile/validate", json={"profile_id": pid})).json()["status"] == "ok"
        assert (await client.post("/api/site/profile/activate", json={"profile_id": pid})).json()["status"] == "ok"
        overview = (await client.get("/api/site/profile")).json()
        assert overview["active"]["id"] == pid and overview["writes_paused"] is False
        assert set(overview["listing_gates"]) == {"en_route", "ready_for_sale"}
        detail = (await client.get(f"/api/site/profile/{pid}")).json()
        assert detail["status"] == "active" and detail["preview"]["written"] is False
        assert (await client.post("/api/site/profile/nope", json={})).status_code == 404


async def test_F08_a_schema_change_pauses_the_version_that_is_currently_writing(db, owner):
    """A REST namespace disappearing under the active profile is drift, not just a report: the version
    that is writing right now no longer matches the site, so writes pause until a new one is validated."""
    await wordpress_connections(db)
    await _reset_profiles(db)
    site = wordpress_fake()
    with install_wordpress(site):
        first = (await _discover(db, owner))["profile"]
        await dispatch(ctx_for(db, owner), "site.validate", {"profile_id": first["id"]})
        await dispatch(ctx_for(db, owner), "site.activate", {"profile_id": first["id"]})
        active = await db.get(SiteProfile, first["id"])
        assert site_svc.writable(active) == (True, None)
        site.namespaces = [n for n in site.namespaces if n != "wc/v3"]   # a plugin change removes WooCommerce
        data = await _discover(db, owner)
        assert data["changes"] and data["paused_active_profile"] is True
        await db.refresh(active)
        assert active.status == "drift" and active.writes_paused is True
        assert "REST namespaces removed" in (active.pause_reason or "")
        ok, why = site_svc.writable(active)
        assert ok is False and "namespaces" in (why or "")
        # the new draft is the way forward; it is not writable until it is validated and activated
        assert data["profile"]["status"] == "draft"
        assert site_svc.writable(await db.get(SiteProfile, data["profile"]["id"]))[0] is False
