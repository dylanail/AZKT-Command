"""Uploads and private assets (spec §7.1, §7.4 step 2): resumable PUT, magic-byte validation, sha256 dedupe,
EXIF/derivatives, honest HEIC/unsupported states, access through linked records, sensitive never public."""
from __future__ import annotations

import io
import struct
import uuid

import pytest
from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.core.errors import Denied, ValidationFailed
from backend.app.domain.commands import dispatch
from backend.app.models.assets import Asset, AssetLink, UploadSession
from backend.app.services.assets import part_key
from backend.app.services.storage import storage
from backend.tests.conftest import ctx_for, login, make_user


def _u() -> str:
    return uuid.uuid4().hex[:8]


def jpeg_bytes(seed: int = 1, size: tuple[int, int] = (640, 480), exif_time: str | None = None) -> bytes:
    """Small distinct JPEG (colour depends on seed); optionally with an EXIF DateTimeOriginal tag."""
    from PIL import Image
    im = Image.new("RGB", size, ((seed * 37) % 256, (seed * 91) % 256, (seed * 53) % 256))
    buf = io.BytesIO()
    kwargs = {}
    if exif_time:
        exif = Image.Exif()
        ifd = exif.get_ifd(0x8769)
        ifd[36867] = exif_time
        exif[306] = exif_time
        kwargs["exif"] = exif.tobytes()
    im.save(buf, format="JPEG", quality=80, **kwargs)
    return buf.getvalue()


def png_bytes(seed: int = 1) -> bytes:
    from PIL import Image
    im = Image.new("RGBA", (40, 30), ((seed * 5) % 256, 20, 200, 255))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def wav_bytes(seconds: float = 0.05, seed: int = 0) -> bytes:
    """Minimal PCM WAV; `seed` varies the samples so two tests never dedupe to the same asset."""
    n = int(8000 * seconds)
    data = struct.pack("<h", seed % 32000) * n
    hdr = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16)
    return hdr + b"data" + struct.pack("<I", len(data)) + data


def heic_bytes() -> bytes:
    return b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64


async def upload_asset(client, data: bytes, name: str = "photo.jpg", content_type: str = "image/jpeg", *, chunk: int | None = None,
                       **finalize) -> dict:
    """Prepare -> PUT (optionally chunked with Content-Range) -> finalize. Returns the finalize result data."""
    r = await client.post("/api/uploads", json={"purpose": "intake", "content_type": content_type, "size_bytes": len(data), "filename": name})
    assert r.status_code == 200, r.text
    up = r.json()["data"]["upload"]
    if chunk:
        off = 0
        while off < len(data):
            part = data[off:off + chunk]
            r = await client.put(up["put_url"], content=part, headers={"Content-Type": "application/octet-stream",
                                                                       "Content-Range": f"bytes {off}-{off + len(part) - 1}/{len(data)}"})
            assert r.status_code == 200, r.text
            off += len(part)
    else:
        r = await client.put(up["put_url"], content=data, headers={"Content-Type": "application/octet-stream"})
        assert r.status_code == 200, r.text
    r = await client.post(f"/api/uploads/{up['id']}/finalize", json=finalize)
    assert r.status_code == 200, r.text
    return r.json()["data"]


async def drain_outbox(max_rounds: int = 50) -> int:
    """Dispatch every pending outbox event (the worker's single pass is bounded by OUTBOX_BATCH; a shared test
    database can hold many events from other files, so keep going until the outbox is empty)."""
    from backend.app import db as dbmod
    from backend.app.domain import events as events_mod
    total = 0
    for _ in range(max_rounds):
        n = await events_mod.dispatch_pending(dbmod.SessionLocal, limit=500)
        total += n
        if n == 0:
            break
    return total


async def upload_via_commands(db, user, data: bytes, name: str = "photo.jpg", content_type: str = "image/jpeg") -> str:
    res = await dispatch(ctx_for(db, user), "assets.prepare_upload", {"purpose": "intake", "content_type": content_type,
                                                                     "size_bytes": len(data), "filename": name})
    up = res.data["upload"]
    storage().append_part(part_key(up["id"]), data)
    await dispatch(ctx_for(db, user), "assets.record_chunk", {"upload_id": up["id"], "received_bytes": len(data), "complete": True})
    fin = await dispatch(ctx_for(db, user), "assets.finalize_upload", {"upload_id": up["id"]})
    assert fin.data["status"] == "ready", fin.data
    return fin.data["asset"]["id"]


# ── upload flow ──────────────────────────────────────────────────────────────
async def test_resumable_upload_builds_private_derivatives(client, owner, db):
    login(client, owner)
    data = jpeg_bytes(11, exif_time="2026:09:01 14:30:00")
    r = await client.post("/api/uploads", json={"purpose": "intake", "content_type": "image/jpeg", "size_bytes": len(data), "filename": "front.jpg"})
    body = r.json()
    assert body["status"] == "ok" and body["data"]["max_bytes"] == settings.UPLOAD_MAX_BYTES and "image/heic" in body["data"]["allowed_types"]
    up = body["data"]["upload"]
    assert up["state"] == "open" and up["expires_at"]
    half = len(data) // 2
    r = await client.put(up["put_url"], content=data[:half], headers={"Content-Range": f"bytes 0-{half - 1}/{len(data)}"})
    assert r.status_code == 200 and r.json()["next_offset"] == half and r.json()["complete"] is False
    # wrong offset after a "reconnect" -> 409 with the offset to resume from
    r = await client.put(up["put_url"], content=data[half + 5:], headers={"Content-Range": f"bytes {half + 5}-{len(data) - 1}/{len(data)}"})
    assert r.status_code == 409 and r.json()["detail"]["next_offset"] == half
    r = await client.get(f"/api/uploads/{up['id']}")
    assert r.json()["next_offset"] == half and r.json()["upload"]["state"] == "received"
    r = await client.put(up["put_url"], content=data[half:], headers={"Content-Range": f"bytes {half}-{len(data) - 1}/{len(data)}"})
    assert r.status_code == 200 and r.json()["complete"] is True
    r = await client.post(f"/api/uploads/{up['id']}/finalize", json={})
    fin = r.json()["data"]
    assert fin["status"] == "ready" and fin["deduplicated"] is False
    a = fin["asset"]
    assert a["kind"] == "photo" and a["content_type"] == "image/jpeg" and a["size_bytes"] == len(data) and a["sha256"]
    assert a["width"] == 640 and a["height"] == 480 and a["metadata_stripped"] is True
    assert a["derivatives"]["status"] == "ready" and a["derivatives"]["thumb"]["width"] <= 320 and a["derivatives"]["web"]["width"] <= 1600
    assert a["captured_at"] is not None and a["exif"]["captured_at_source"] == "exif" and a["uploaded_at"] != a["captured_at"]
    assert a["public_eligible"] is False and a["visibility"] == "internal" and a["classification"] == "unknown"
    # bytes come back only through the authorized route; derivatives carry no EXIF
    r = await client.get(f"/api/assets/{a['id']}/original")
    assert r.status_code == 200 and r.content == data and r.headers["content-type"] == "image/jpeg"
    r = await client.get(f"/api/assets/{a['id']}/thumb")
    assert r.status_code == 200 and r.headers["cache-control"].startswith("private")
    from PIL import Image
    im = Image.open(io.BytesIO(r.content))
    assert max(im.size) <= 320 and not im.getexif()
    # finalizing again is idempotent
    r = await client.post(f"/api/uploads/{up['id']}/finalize", json={})
    assert r.json()["data"]["asset"]["id"] == a["id"]


async def test_identical_bytes_dedupe_to_one_asset(client, owner, db):
    login(client, owner)
    data = jpeg_bytes(22)
    first = await upload_asset(client, data, "a.jpg")
    second = await upload_asset(client, data, "copy-of-a.jpg")
    assert first["asset"]["id"] == second["asset"]["id"] and second["deduplicated"] is True
    rows = (await db.execute(select(Asset).where(Asset.sha256 == first["asset"]["sha256"]))).scalars().all()
    assert len(rows) == 1


async def test_invalid_type_and_oversize_are_visible_failures(client, owner, monkeypatch):
    login(client, owner)
    # text bytes declared as an image: refused by magic bytes, session stays failed and retryable elsewhere
    r = await client.post("/api/uploads", json={"purpose": "intake", "content_type": "image/jpeg", "filename": "notes.txt"})
    up = r.json()["data"]["upload"]
    r = await client.put(up["put_url"], content=b"this is not a picture at all, just text bytes")
    assert r.status_code == 200
    r = await client.post(f"/api/uploads/{up['id']}/finalize", json={})
    fin = r.json()["data"]
    assert fin["status"] == "failed" and "unrecognized" in fin["error"] and fin["asset"] is None
    r = await client.get(f"/api/uploads/{up['id']}")
    assert r.json()["upload"]["state"] == "failed"
    # declared type not allowed
    r = await client.post("/api/uploads", json={"purpose": "intake", "content_type": "application/x-msdownload", "filename": "x.exe"})
    assert r.status_code == 422 and "allowed_types" in r.json()
    # oversize refused before any bytes are stored
    monkeypatch.setattr(settings, "UPLOAD_MAX_BYTES", 1000)
    r = await client.post("/api/uploads", json={"purpose": "intake", "content_type": "image/jpeg", "size_bytes": 5000, "filename": "big.jpg"})
    assert r.status_code == 422 and r.json()["max_bytes"] == 1000
    r = await client.post("/api/uploads", json={"purpose": "intake", "content_type": "image/jpeg", "filename": "big.jpg"})
    up = r.json()["data"]["upload"]
    r = await client.put(up["put_url"], content=jpeg_bytes(3))
    assert r.status_code == 413
    # checksum mismatch fails the file (client re-uploads)
    monkeypatch.setattr(settings, "UPLOAD_MAX_BYTES", 25 * 1024 * 1024)
    fin = await upload_asset(client, jpeg_bytes(4), "c.jpg", sha256="0" * 64)
    assert fin["status"] == "failed" and "checksum" in fin["error"]


async def test_heic_original_kept_and_derivatives_marked_unsupported(client, owner):
    login(client, owner)
    try:
        import pillow_heif  # noqa: F401
        pytest.skip("pillow-heif present: HEIC derivatives are produced")
    except ImportError:
        pass
    fin = await upload_asset(client, heic_bytes(), "IMG_0001.HEIC", content_type="image/heic")
    a = fin["asset"]
    assert fin["status"] == "ready" and a["content_type"] == "image/heic" and a["kind"] == "photo"
    assert a["derivatives"]["status"] == "unsupported" and "pillow-heif" in a["derivatives"]["reason"]
    r = await client.get(f"/api/assets/{a['id']}/web")
    assert r.status_code == 404 and r.json()["status"] == "unsupported"
    r = await client.get(f"/api/assets/{a['id']}/original")
    assert r.status_code == 200 and r.content == heic_bytes()


async def test_audio_voice_note_and_transcript(client, owner):
    login(client, owner)
    fin = await upload_asset(client, wav_bytes(), "note.wav", content_type="audio/wav")
    a = fin["asset"]
    assert a["kind"] == "audio" and a["classification"] == "voice_note" and a["derivatives"]["status"] == "not_applicable"
    r = await client.post(f"/api/assets/{a['id']}/transcript", json={"transcript": "needs tires", "source": "typed"})
    assert r.json()["data"]["asset"]["transcript"] == "needs tires"


# ── access and classification ───────────────────────────────────────────────
async def test_access_follows_linked_record_scope(client, owner, mechanic, db):
    login(client, owner)
    a = (await upload_asset(client, jpeg_bytes(31), "scoped.jpg"))["asset"]
    v = (await dispatch(ctx_for(db, owner), "vehicles.create", {"make": "Suzuki", "model": "Carry"})).data["vehicle"]
    await dispatch(ctx_for(db, owner), "assets.link", {"asset_id": a["id"], "entity_kind": "vehicle", "entity_id": v["id"], "role": "photo"})
    login(client, mechanic)
    r = await client.get(f"/api/assets/{a['id']}/thumb")
    assert r.status_code == 403   # vehicle not assigned to the mechanic
    await dispatch(ctx_for(db, owner), "tasks.create", {"title": f"Check tires {_u()}", "vehicle_id": v["id"], "owner_user_id": mechanic.id})
    r = await client.get(f"/api/assets/{a['id']}/thumb")
    assert r.status_code == 200
    r = await client.get("/api/assets", params={"entity_kind": "vehicle", "entity_id": v["id"]})
    assert r.status_code == 200 and [x["id"] for x in r.json()["items"]] == [a["id"]]
    # a mechanic cannot use someone else's unlinked asset in a new flow
    login(client, owner)
    b = (await upload_asset(client, jpeg_bytes(32), "private.jpg"))["asset"]
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, mechanic), "assets.link", {"asset_id": b["id"], "entity_kind": "vehicle", "entity_id": v["id"], "role": "photo"})


async def test_sensitive_documents_never_public_and_link_removal_keeps_evidence(client, owner, db):
    login(client, owner)
    a = (await upload_asset(client, jpeg_bytes(41), "invoice-scan.jpg"))["asset"]
    v = (await dispatch(ctx_for(db, owner), "vehicles.create", {"make": "Honda", "model": "Acty"})).data["vehicle"]
    res = await dispatch(ctx_for(db, owner), "assets.classify", {"asset_id": a["id"], "classification": "invoice", "public_eligible": True})
    assert res.data["asset"]["sensitive"] is True and res.data["asset"]["public_eligible"] is False
    with pytest.raises(ValidationFailed):
        await dispatch(ctx_for(db, owner), "assets.link", {"asset_id": a["id"], "entity_kind": "vehicle", "entity_id": v["id"], "role": "photo"})
    link = (await dispatch(ctx_for(db, owner), "assets.link", {"asset_id": a["id"], "entity_kind": "vehicle", "entity_id": v["id"], "role": "document"})).data["link"]
    # listing_photo can be made public-eligible only deliberately
    res = await dispatch(ctx_for(db, owner), "assets.classify", {"asset_id": a["id"], "classification": "listing_photo", "sensitive": False})
    assert res.data["asset"]["public_eligible"] is False
    res = await dispatch(ctx_for(db, owner), "assets.remove_link", {"link_id": link["id"], "reason": "wrong truck"})
    assert res.data["removed"] is True
    row = await db.get(AssetLink, link["id"])
    await db.refresh(row)
    assert row.removed_at is not None and (await db.get(Asset, a["id"])) is not None
    # re-linking restores the same link row (evidence never deleted)
    again = (await dispatch(ctx_for(db, owner), "assets.link", {"asset_id": a["id"], "entity_kind": "vehicle", "entity_id": v["id"], "role": "document"})).data
    assert again["link"]["id"] == link["id"] and again["created"] is True


async def test_upload_slot_ownership_and_png(client, owner, db):
    login(client, owner)
    other = await make_user(db, f"sales-{_u()}", "sales")
    r = await client.post("/api/uploads", json={"purpose": "intake", "content_type": "image/png", "filename": "p.png"})
    up = r.json()["data"]["upload"]
    login(client, other)
    r = await client.put(up["put_url"], content=png_bytes())
    assert r.status_code == 404   # not their slot
    login(client, owner)
    r = await client.put(up["put_url"], content=png_bytes())
    assert r.status_code == 200
    fin = (await client.post(f"/api/uploads/{up['id']}/finalize", json={})).json()["data"]
    assert fin["status"] == "ready" and fin["asset"]["content_type"] == "image/png" and fin["asset"]["derivatives"]["status"] == "ready"
    s = await db.get(UploadSession, up["id"])
    await db.refresh(s)
    assert s.state == "finalized" and s.asset_id == fin["asset"]["id"]
