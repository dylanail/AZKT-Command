"""Importer Drive index, matching and asset import, plus the live Sheets ledger source
(spec §7.1 and §6.1, acceptance F01–F03, A09).

What is proved here:

* the importer root is an explicit choice — a duplicate folder name is refused with both candidates
  and never guessed (A09); the chosen id, its parent path, owner and duplicates are persisted;
* the index is keyed by Drive file id with checksum + version; incremental scans use the changes feed,
  and a folder that leaves the selected root takes its descendants' retrieval eligibility with it;
* a folder auto-links to a vehicle only on evidence (a unique stock reference corroborated by the
  folder contents); model/year/colour similarity is a proposal that waits for a person (F01);
* imported originals keep the source link, checksum and revision; identical bytes deduplicate while
  keeping lineage; sensitive documents are never public-eligible and never enter listing media (F02);
* a corrected file version re-imports, revoked access blocks *new* retrieval, and an interrupted
  download stays retryable and never invents a photo (F03);
* the ledger source is read-only: a write scope is refused, the sheet choice is persisted on the
  connection and the sync runs from a job, not from the request path (§6.1).
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from backend.app.adapters import drive as drive_adapter
from backend.app.adapters import sheets as sheets_adapter
from backend.app.core.errors import Blocked, Unsupported
from backend.app.domain.commands import dispatch
from backend.app.models.assets import Asset, AssetLink
from backend.app.models.listings import DriveFile
from backend.app.models.runtime import Job
from backend.app.services import drive_assets as svc
from backend.tests.conftest import ctx_for, login
from backend.tests.fixtures_providers import (FOLDER_EVIDENCED, FOLDER_LOOKALIKE, FOLDER_MOVED, LEDGER_SHEET, OUTSIDE,
                                              ROOT_A, ROOT_B, drive_connection, drive_fake, install_drive,
                                              install_sheets, jpeg, sheets_connection, sheets_fake)


def _u() -> str:
    return uuid.uuid4().hex[:8]


def _load(stmt):
    return stmt.execution_options(populate_existing=True)


async def _rows(db, conn, **where) -> list[DriveFile]:
    q = select(DriveFile).where(DriveFile.connection_id == conn.id)
    for k, v in where.items():
        q = q.where(getattr(DriveFile, k) == v)
    return list((await db.execute(_load(q))).scalars().all())


async def _row(db, conn, file_id: str) -> DriveFile | None:
    rows = await _rows(db, conn, file_id=file_id)
    return rows[0] if rows else None


async def _vehicles(db, owner) -> tuple[dict, dict]:
    """One truck with a unique stock reference, one visually similar truck with none.

    The fixtures name real identifiers, so the pair is created once per test database and reused."""
    from backend.app.models.vehicles import Vehicle
    from backend.app.services.vehicles import serialize_vehicle
    existing = (await db.execute(_load(select(Vehicle).where(Vehicle.stock_no == "STK-7412")))).scalars().first()
    if existing is not None:
        acty_row = (await db.execute(_load(select(Vehicle).where(
            Vehicle.model == "Acty", Vehicle.model_year == 2019)))).scalars().first()
        return serialize_vehicle(existing), serialize_vehicle(acty_row)
    hijet = (await dispatch(ctx_for(db, owner), "vehicles.create", {
        "make": "Daihatsu", "model": "Hijet", "model_year": 2018, "color": "white", "stock_no": "STK-7412",
        "logistics_state": "received", "create_missing_task": False})).data["vehicle"]
    acty = (await dispatch(ctx_for(db, owner), "vehicles.create", {
        "make": "Honda", "model": "Acty", "model_year": 2019, "color": "silver",
        "logistics_state": "received", "create_missing_task": False})).data["vehicle"]
    return hijet, acty


async def _select_root(db, owner, folder_id: str = ROOT_A) -> dict:
    return (await dispatch(ctx_for(db, owner), "drive.select_root", {"folder_id": folder_id})).data


async def _fresh_index(db, salt: int):
    """One Drive connection is shared by the whole database, so each scenario starts from an empty
    index and its own file bytes — exactly what a fresh install looks like."""
    from sqlalchemy import delete
    from backend.app.models.comms import SyncCursor
    conn = await drive_connection(db)
    await db.execute(delete(DriveFile).where(DriveFile.connection_id == conn.id))
    await db.execute(delete(SyncCursor).where(SyncCursor.connection_id == conn.id))
    await db.commit()
    return drive_fake(salt=salt), conn


# ── A09: the root is chosen, never guessed ───────────────────────────────────
async def test_A09_duplicate_folder_name_must_be_chosen_explicitly(db, owner):
    fake, _conn = await _fresh_index(db, salt=100)
    with install_drive(fake):
        with pytest.raises(Blocked) as exc:
            await dispatch(ctx_for(db, owner), "drive.select_root", {"name": "Dylan Nail Shipments"})
        cands = {c["id"] for c in exc.value.detail["candidates"]}
        assert cands == {ROOT_A, ROOT_B} and "choose the exact folder" in exc.value.message
        await db.rollback()
        await db.refresh(owner)
        # by id the choice is explicit and the parent path / owner / duplicates are persisted
        data = await _select_root(db, owner, ROOT_A)
        assert data["folder"]["id"] == ROOT_A and data["folder"]["path"] == "Dylan Nail Shipments"
        assert data["folder"]["owner"] == "dylxnxil@gmail.com" and data["children"] == 3
        assert [d["id"] for d in data["duplicate_names"]] == [ROOT_B]
        conn = await svc.drive_connection(db)
        assert svc.root_id_of(conn) == ROOT_A and (conn.config or {})["folder_path"] == "Dylan Nail Shipments"


async def test_scan_indexes_ids_checksums_and_versions_then_goes_incremental(db, owner):
    fake, conn = await _fresh_index(db, salt=110)
    with install_drive(fake):
        await _select_root(db, owner)
        first = (await dispatch(ctx_for(db, owner), "drive.scan", {"full": True})).data
        assert first["mode"] == "full" and first["indexed"] >= 9
        photo = await _row(db, conn, "F-PHOTO-1")
        assert photo is not None and photo.checksum and photo.revision == "1"
        assert photo.path.endswith("STK-7412 Hijet jumbo/STK-7412 front.jpg")
        assert photo.owner_email == "dylxnxil@gmail.com" and photo.in_root is True
        # a second scan uses the changes feed, not a full re-walk
        fake.add_file("F-NEW", "STK-7412 bed.jpg", FOLDER_EVIDENCED, jpeg(11))
        second = (await dispatch(ctx_for(db, owner), "drive.scan", {})).data
        assert second["mode"] == "incremental" and second["changed"] >= 1
        assert await _row(db, conn, "F-NEW") is not None
        cursor = await _row(db, conn, "F-NEW")
        assert cursor.import_status == "pending"


async def test_F03_folder_moved_out_of_root_disables_retrieval_for_its_contents(db, owner):
    fake, conn = await _fresh_index(db, salt=120)
    with install_drive(fake):
        await _select_root(db, owner)
        await dispatch(ctx_for(db, owner), "drive.scan", {"full": True})
        assert (await _row(db, conn, "F-MOVED-1")).retrieval_eligible is True
        fake.move(FOLDER_MOVED, OUTSIDE)
        res = (await dispatch(ctx_for(db, owner), "drive.scan", {})).data
        assert res["left_root"] >= 2
        folder, child = await _row(db, conn, FOLDER_MOVED), await _row(db, conn, "F-MOVED-1")
        assert folder.in_root is False and folder.retrieval_eligible is False
        assert child.in_root is False and child.retrieval_eligible is False
        assert "outside the selected importer folder" in child.ineligible_reason


# ── F01: only evidenced mappings auto-link ───────────────────────────────────
async def test_F01_only_the_evidenced_folder_auto_links(db, owner):
    hijet, acty = await _vehicles(db, owner)
    fake, conn = await _fresh_index(db, salt=130)
    with install_drive(fake):
        await _select_root(db, owner)
        res = (await dispatch(ctx_for(db, owner), "drive.scan", {"full": True})).data
        evidenced = await _row(db, conn, FOLDER_EVIDENCED)
        lookalike = await _row(db, conn, FOLDER_LOOKALIKE)
        # the folder whose name and contents agree on one unique stock reference links itself
        assert evidenced.match_state == "matched" and evidenced.vehicle_id == hijet["id"]
        assert any("agree on STK-7412" in c for c in evidenced.match_evidence["corroboration"])
        # model / year / colour similarity alone never assigns the other truck
        assert lookalike.match_state in ("proposed", "ambiguous")
        assert lookalike.match_state != "matched"
        if lookalike.match_state == "proposed":
            assert lookalike.vehicle_id == acty["id"]
            assert any("similarity only" in r for r in lookalike.match_evidence["reasons"])
        assert res["matches"]["summary"]["matched"] >= 1
        # a person confirms the proposal explicitly; the decision is persisted with who made it
        out = (await dispatch(ctx_for(db, owner), "drive.confirm_match",
                              {"file_id": FOLDER_LOOKALIKE, "decision": "confirm", "vehicle_id": acty["id"],
                               "note": "frame photo checked"})).data
        assert out["folder"]["match_state"] == "confirmed" and out["folder"]["confirmed_by"] == owner.id
        # a confirmed mapping is not re-decided while the identity-bearing contents are unchanged
        again = (await dispatch(ctx_for(db, owner), "drive.scan", {"full": True})).data
        assert again["matches"]["summary"]["kept_confirmed"] >= 1
        assert (await _row(db, conn, FOLDER_LOOKALIKE)).match_state == "confirmed"


async def test_confirmed_mapping_is_rechecked_when_identity_bearing_contents_change(db, owner):
    hijet, acty = await _vehicles(db, owner)
    fake, conn = await _fresh_index(db, salt=140)
    with install_drive(fake):
        await _select_root(db, owner)
        await dispatch(ctx_for(db, owner), "drive.scan", {"full": True})
        await dispatch(ctx_for(db, owner), "drive.confirm_match",
                       {"file_id": FOLDER_LOOKALIKE, "decision": "confirm", "vehicle_id": acty["id"]})
        before = (await _row(db, conn, FOLDER_LOOKALIKE)).identity_signature
        # a frame-bearing document lands in the folder: the mapping is re-examined, not assumed
        fake.add_file("F-ACTY-FRAME", "export paper HA4-1234567.pdf", FOLDER_LOOKALIKE, b"%PDF-1.4\nframe\n%%EOF\n",
                      mime="application/pdf")
        res = (await dispatch(ctx_for(db, owner), "drive.scan", {})).data
        after = await _row(db, conn, FOLDER_LOOKALIKE)
        assert res["matches"]["summary"]["rechecked"] >= 1 and after.identity_signature != before
        assert after.match_evidence.get("recheck_of") == "confirmed"


# ── F02 / F03: import, classification, dedupe, recovery ──────────────────────
async def test_F02_sensitive_documents_are_imported_but_never_public_media(db, owner):
    hijet, _acty = await _vehicles(db, owner)
    fake, conn = await _fresh_index(db, salt=150)
    with install_drive(fake):
        await _select_root(db, owner)
        await dispatch(ctx_for(db, owner), "drive.scan", {"full": True})
        out = (await dispatch(ctx_for(db, owner), "drive.import_assets", {"folder_id": FOLDER_EVIDENCED})).data
        by_file = {i["file_id"]: i for i in out["items"]}
        assert by_file["F-PHOTO-1"]["classification"] == "listing_photo"
        assert by_file["F-ID"]["classification"] == "id_document"
        assert by_file["F-INVOICE"]["classification"] == "invoice"
        assert by_file["F-BL"]["classification"] == "shipping_paper"   # "bill of lading" is paperwork
        for fid in ("F-ID", "F-INVOICE", "F-BL"):
            a = await db.get(Asset, by_file[fid]["asset_id"])
            assert a.sensitive is True and a.public_eligible is False
            assert a.visibility in ("owner", "internal") and a.provider_ref == f"drive:{fid}"
        # importer photos are marked pre-arrival and are not public until deliberately made eligible
        photo = await db.get(Asset, by_file["F-PHOTO-1"]["asset_id"])
        assert photo.pre_arrival is True and photo.public_eligible is False
        assert photo.source == "drive" and photo.provider_revision == "1" and photo.provider_link
        # linked to the vehicle with the evidenced mapping, documents as documents
        links = (await db.execute(_load(select(AssetLink).where(AssetLink.entity_kind == "vehicle",
                                                                AssetLink.entity_id == hijet["id"])))).scalars().all()
        roles = {l.asset_id: l.role for l in links}
        assert roles[photo.id] == "photo" and roles[by_file["F-ID"]["asset_id"]] == "document"
        # the listing media set is built from approved public photos only (F02)
        from backend.app.models.vehicles import Vehicle
        from backend.app.services import listings as listings_svc
        v = await db.get(Vehicle, hijet["id"])
        assert await listings_svc.collect_media(db, v) == []
        await dispatch(ctx_for(db, owner), "assets.classify",
                       {"asset_id": photo.id, "classification": "listing_photo", "public_eligible": True})
        media = await listings_svc.collect_media(db, v)
        assert [m["asset_id"] for m in media] == [photo.id]
        assert all(m["asset_id"] != by_file["F-ID"]["asset_id"] for m in media)
        # AZKT never changes sharing on the source folder
        with pytest.raises(Unsupported):
            await fake.set_permission("F-ID", "anyone")


async def test_F03_duplicate_corrected_revoked_and_interrupted_media(db, owner):
    hijet, _acty = await _vehicles(db, owner)
    fake, conn = await _fresh_index(db, salt=160)
    with install_drive(fake):
        await _select_root(db, owner)
        await dispatch(ctx_for(db, owner), "drive.scan", {"full": True})
        # an interrupted download is retryable and never becomes a missing-photo success
        fake.fail_download_once.add("F-PHOTO-1")
        first = (await dispatch(ctx_for(db, owner), "drive.import_assets",
                                {"file_ids": ["F-PHOTO-1"]})).data
        assert first["failed"] == 1 and first["imported"] == 0
        row = await _row(db, conn, "F-PHOTO-1")
        # nothing partial is stored and the error is typed; a previously imported asset is retained
        assert row.import_status == "failed" and "transient" in row.import_error
        retry = (await dispatch(ctx_for(db, owner), "drive.import_assets", {"file_ids": ["F-PHOTO-1"]})).data
        assert retry["imported"] == 1
        original = await _row(db, conn, "F-PHOTO-1")
        assert original.import_status == "imported" and original.import_attempts == 2

        # identical bytes deduplicate to one asset while both Drive sources stay in the lineage
        dup = (await dispatch(ctx_for(db, owner), "drive.import_assets", {"file_ids": ["F-PHOTO-DUP"]})).data
        assert dup["deduplicated"] == 1 and dup["imported"] == 0
        dup_row = await _row(db, conn, "F-PHOTO-DUP")
        assert dup_row.asset_id == original.asset_id and dup_row.import_status == "deduplicated"
        asset = await db.get(Asset, original.asset_id)
        await db.refresh(asset)
        sources = {s["file_id"] for s in (asset.analysis or {}).get("drive_sources", [])}
        assert sources == {"F-PHOTO-1", "F-PHOTO-DUP"}

        # a corrected version of an already imported file is re-imported with its new checksum
        await dispatch(ctx_for(db, owner), "drive.import_assets", {"file_ids": ["F-PHOTO-2"]})
        before = await _row(db, conn, "F-PHOTO-2")
        old_asset, old_checksum = before.asset_id, before.checksum
        fake.new_version("F-PHOTO-2", jpeg(42))
        await dispatch(ctx_for(db, owner), "drive.scan", {})
        pending = await _row(db, conn, "F-PHOTO-2")
        assert pending.import_status == "pending" and pending.checksum != old_checksum and pending.revision == "2"
        await dispatch(ctx_for(db, owner), "drive.import_assets", {"file_ids": ["F-PHOTO-2"]})
        corrected = await _row(db, conn, "F-PHOTO-2")
        assert corrected.import_status == "imported" and corrected.asset_id != old_asset
        new_asset = await db.get(Asset, corrected.asset_id)
        assert new_asset.provider_revision == "2" and new_asset.provider_ref == "drive:F-PHOTO-2"

        # revoked source access blocks *new* retrieval; evidence already imported is retained
        fake.revoke("F-INVOICE")
        blocked = (await dispatch(ctx_for(db, owner), "drive.import_assets", {"file_ids": ["F-INVOICE"]})).data
        assert blocked["blocked"] == 1 and blocked["imported"] == 0
        revoked = await _row(db, conn, "F-INVOICE")
        assert revoked.retrieval_eligible is False and "revoked" in revoked.ineligible_reason
        assert (await db.get(Asset, original.asset_id)) is not None
        # a second attempt does not reach the provider again
        calls = len(fake.downloads)
        again = (await dispatch(ctx_for(db, owner), "drive.import_assets", {"file_ids": ["F-INVOICE"]})).data
        assert again["blocked"] == 1 and len(fake.downloads) == calls


# ── router surface ───────────────────────────────────────────────────────────
async def test_drive_router_picker_status_and_matches_have_no_side_effects(client, db, owner):
    await _vehicles(db, owner)
    fake, conn = await _fresh_index(db, salt=170)
    login(client, owner)
    with install_drive(fake):
        r = await client.get("/api/drive/folders", params={"q": "Dylan Nail Shipments"})
        body = r.json()
        assert body["connected"] is True and len(body["candidates"]) == 2
        assert all(c["duplicate_name"] is True for c in body["candidates"])
        assert body["selected"] is None or body["selected"] in (ROOT_A, ROOT_B)
        assert (await client.post("/api/drive/root", json={"folder_id": ROOT_A})).json()["status"] == "ok"
        assert (await client.post("/api/drive/scan", json={"full": True})).json()["status"] == "ok"
        status = (await client.get("/api/drive/status")).json()
        assert status["root"]["folder_id"] == ROOT_A and status["counts"]["files"] >= 8
        assert status["counts"]["pending"] >= 1 and status["freshness"]["state"] in ("ok", "fresh", "warn", "degraded")
        before = len(await _rows(db, conn))
        matches = (await client.get("/api/drive/matches")).json()["items"]
        assert any(m["file_id"] == FOLDER_LOOKALIKE for m in matches)
        assert all("candidates" in m for m in matches)
        assert len(await _rows(db, conn)) == before          # a GET never writes


# ── live Sheets ledger source (spec §6.1) ────────────────────────────────────
async def test_ledger_sheet_is_selected_explicitly_and_stays_read_only(client, db, owner):
    await sheets_connection(db)
    src = sheets_fake()
    login(client, owner)
    with install_sheets(src):
        listed = (await client.get("/api/finance/ledger/sheets", params={"q": "Ledger"})).json()
        assert listed["connected"] is True and listed["read_only"] is True
        assert [c["id"] for c in listed["candidates"]] == [LEDGER_SHEET] and listed["candidates"][0]["revision"] == "7"
        res = (await client.post("/api/finance/ledger/sheets/select",
                                 json={"sheet_id": LEDGER_SHEET, "tab_title": "Costs"})).json()
        assert res["status"] == "ok"
        sheet = res["data"]["sheet"]
        assert sheet["title"] == "AZKT Ledger 2026" and sheet["tab"] == "Costs" and sheet["revision"] == "7"
        assert sheet["headers"][:2] == ["Date", "Vehicle"] and sheet["row_count"] == 3
        conn = await sheets_connection(db)
        assert (conn.config or {})["sheet_id"] == LEDGER_SHEET and (conn.config or {})["read_only"] is True
        status = (await client.get("/api/finance/ledger/source")).json()
        assert status["read_only"] is True and status["sync_interval_seconds"] == 15 * 60
        assert all("/auth/spreadsheets.readonly" in s or "readonly" in s for s in status["requested_scopes"])
        # writeback is a separate approved feature: the source refuses it rather than faking success
        with pytest.raises(Unsupported):
            await src.write_rows(LEDGER_SHEET, "Costs", [])


async def test_ledger_source_reads_values_and_formulas_and_refuses_write_scopes(db, owner):
    src = sheets_fake()
    rows, revision = await src.read_rows(LEDGER_SHEET, "Costs")
    assert revision == "7" and [r.row_number for r in rows] == [2, 3, 4]
    assert rows[0].values["Amount"] == "120.00" and rows[0].formulas["Total"] == "=E2*1.1"
    meta = await src.get_sheet_meta(LEDGER_SHEET)
    assert {t.title for t in meta.tabs} == {"Costs", "Notes"}
    assert meta.permissions["writable"] is False
    costs = next(t for t in meta.tabs if t.title == "Costs")
    assert costs.formula_columns == ["Total"] and len(costs.sample_rows) == 3
    # a connection granted a write scope is a setup error, not a silent downgrade
    with pytest.raises(Unsupported, match="never requests write scopes"):
        sheets_adapter.assert_read_only(["https://www.googleapis.com/auth/spreadsheets"])
    sheets_adapter.assert_read_only(sheets_adapter.requested_scopes())
    with pytest.raises(Unsupported, match="write scopes are never requested"):
        drive_adapter.assert_read_only(["https://www.googleapis.com/auth/drive"])


async def test_ledger_sync_is_enqueued_as_a_job_and_blocked_without_a_mapping(client, db, owner):
    await sheets_connection(db)
    login(client, owner)
    with install_sheets():
        await client.post("/api/finance/ledger/sheets/select", json={"sheet_id": LEDGER_SHEET, "tab_title": "Costs"})
        blocked = (await client.post("/api/finance/ledger/source/sync", json={})).json()
        assert blocked["status"] == "blocked" and "mapping" in blocked["reason"]
        # with an active mapping the request only enqueues; the provider call happens in the worker
        prev = (await dispatch(ctx_for(db, owner), "ledger.propose_mapping", {
            "sheet_id": LEDGER_SHEET, "tab": "Costs",
            "columns": {"date": "Date", "vehicle_ref": "Vehicle", "vendor": "Supplier",
                        "description": "Description", "amount": "Amount", "currency": "Currency"}})).data
        await dispatch(ctx_for(db, owner), "ledger.preview", {"mapping_id": prev["mapping"]["id"]})
        await dispatch(ctx_for(db, owner), "ledger.activate_mapping", {"mapping_id": prev["mapping"]["id"]})
        queued = (await client.post("/api/finance/ledger/source/sync", json={})).json()
        assert queued["status"] in ("queued", "already_queued") and queued["mapping_id"] == prev["mapping"]["id"]
        job = (await db.execute(_load(select(Job).where(Job.kind == "ledger.sync")))).scalars().first()
        assert job is not None


async def test_drive_and_ledger_sweeps_are_registered_every_15_minutes():
    from backend.app.domain.jobs import SWEEPS
    assert SWEEPS["drive.scan"][1] == 15 * 60
    assert SWEEPS["ledger.sync"][1] == 15 * 60


async def test_missing_drive_connection_reports_setup_blocked(db):
    """No Google connection: an honest `unsupported` naming the setup step, never an empty file list."""
    drive_adapter.set_adapter(None)
    with pytest.raises(Unsupported) as exc:
        drive_adapter.adapter_for(db, None)
    assert exc.value.detail.get("setup_blocked") == "drive.oauth"
    # even connected, a live call is refused in tests so no scenario can reach the network
    conn = await drive_connection(db)
    with pytest.raises(Unsupported, match="disabled in tests"):
        drive_adapter.adapter_for(db, conn)


async def test_F03_a_source_that_no_longer_permits_download_stays_ineligible_across_scans(db, owner):
    """Retrieval eligibility follows the source. A file the account may no longer download is never
    retried, and a later scan must not quietly hand the eligibility back."""
    await _vehicles(db, owner)
    fake, conn = await _fresh_index(db, salt=180)
    with install_drive(fake):
        await _select_root(db, owner)
        await dispatch(ctx_for(db, owner), "drive.scan", {"full": True})
        assert (await _row(db, conn, "F-PHOTO-1")).retrieval_eligible is True
        # sharing is narrowed at the source: the file is still listed, its content is not retrievable
        fake.deny_download("F-PHOTO-1")
        await dispatch(ctx_for(db, owner), "drive.scan", {"full": True})
        row = await _row(db, conn, "F-PHOTO-1")
        assert row.retrieval_eligible is False and "no longer permits downloading" in row.ineligible_reason
        calls = len(fake.downloads)
        out = (await dispatch(ctx_for(db, owner), "drive.import_assets", {"file_ids": ["F-PHOTO-1"]})).data
        assert out["blocked"] == 1 and out["imported"] == 0 and len(fake.downloads) == calls

        # a revocation discovered at download time also survives the next scan
        fake.no_download.discard("F-PHOTO-1")
        fake.files["F-PHOTO-1"]["capabilities"] = {"canDownload": True}
        fake.revoke("F-ACTY-1")
        denied = (await dispatch(ctx_for(db, owner), "drive.import_assets", {"file_ids": ["F-ACTY-1"]})).data
        assert denied["blocked"] == 1
        fake.denied.discard("F-ACTY-1")            # the index must not forget because a list call succeeds
        await dispatch(ctx_for(db, owner), "drive.scan", {"full": True})
        acty = await _row(db, conn, "F-ACTY-1")
        assert acty.retrieval_eligible is False and "revoked" in acty.ineligible_reason
