"""Private assets and uploads (spec §3.1 Asset/AssetLink, §7.1 photo handling, §7.4 step 2).

Upload flow: `assets.prepare_upload` (bounded, expiring session) → PUT bytes (router writes the part file, then
`assets.record_chunk` keeps the durable offset for resumable uploads) → `assets.finalize_upload` validates by
sniffing magic bytes, computes sha256, deduplicates identical bytes (same Asset id, new AssetLink), stores the
original untouched, extracts EXIF capture time and builds orientation-corrected, metadata-stripped thumb (320px)
and web (1600px) derivatives. HEIC originals are kept and their derivatives are honestly marked unsupported
unless pillow-heif is installed. Nothing is ever public by default; sensitive documents are never public-eligible.
"""
from __future__ import annotations

import asyncio
import io
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.errors import Blocked, Denied, NotFound, ValidationFailed
from ..core.ids import sha256_hex
from ..domain.access import visible_vehicle_ids
from ..domain.actors import Actor
from ..domain.commands import CommandContext, command
from ..domain.policy import has_perm
from ..models.assets import Asset, AssetLink, UploadSession
from ..models.tasks import Task
from ..models.vehicles import Part, ReconIssue, Vehicle
from .storage import storage
from .vehicles import iso, serialize_asset_brief

IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/heic", "image/heif")
AUDIO_TYPES = ("audio/webm", "audio/mp4", "audio/mpeg", "audio/ogg", "audio/wav")
DOCUMENT_TYPES = ("application/pdf",)
CLASSIFICATIONS = ("listing_photo", "invoice", "id_document", "shipping_paper", "screenshot", "unrelated", "unknown", "voice_note")
SENSITIVE_CLASSES = {"invoice", "id_document", "shipping_paper"}
LINK_ROLES = ("photo", "evidence", "source", "gallery", "document", "voice")
ENTITY_KINDS = ("vehicle", "task", "recon_issue", "intake", "shipment", "cost_item", "message", "listing_package", "part")
EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic", "image/heif": ".heif",
       "audio/webm": ".webm", "audio/mp4": ".m4a", "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/wav": ".wav",
       "application/pdf": ".pdf"}
THUMB_PX, WEB_PX = 320, 1600
UPLOAD_TTL = timedelta(hours=24)


def allowed_types() -> list[str]:
    return [t.strip() for t in (settings.UPLOAD_ALLOWED_TYPES or "").split(",") if t.strip()]


def max_bytes() -> int:
    return int(settings.UPLOAD_MAX_BYTES)


def part_key(upload_id: str) -> str:
    return f"uploads/{upload_id}.part"


def sniff(data: bytes) -> str | None:
    """Content type from magic bytes; the declared type is never trusted."""
    if len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"):
            return "image/heic"
        if brand in (b"M4A ", b"M4B ", b"mp42", b"isom", b"mp41", b"iso2", b"dash", b"avc1"):
            return "audio/mp4"
    if data[:4] == b"%PDF":
        return "application/pdf"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[:4] == b"\x1aE\xdf\xa3":
        return "audio/webm"
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    return None


def kind_of(content_type: str) -> str:
    if content_type in IMAGE_TYPES:
        return "photo"
    if content_type in AUDIO_TYPES:
        return "audio"
    if content_type in DOCUMENT_TYPES:
        return "document"
    return "other"


def _parse_exif_time(value: str | None, offset: str | None) -> tuple[datetime | None, bool]:
    if not value:
        return None, False
    try:
        dt = datetime.strptime(value.strip()[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None, False
    if offset and len(offset) >= 6 and offset[0] in "+-":
        try:
            sign = 1 if offset[0] == "+" else -1
            hh, mm = int(offset[1:3]), int(offset[4:6])
            return dt.replace(tzinfo=timezone(sign * timedelta(hours=hh, minutes=mm))).astimezone(timezone.utc), False
        except ValueError:
            pass
    return dt.replace(tzinfo=ZoneInfo(settings.DEFAULT_TIMEZONE)).astimezone(timezone.utc), True


def process_image(data: bytes, content_type: str) -> dict:
    """Synchronous Pillow work: EXIF capture time, size, orientation-fixed and metadata-stripped derivatives."""
    from PIL import Image, ImageOps
    out: dict = {"exif": {}, "captured_at": None, "captured_at_zone_assumed": False, "width": None, "height": None,
                 "derivatives": {}, "metadata_stripped": False}
    if content_type in ("image/heic", "image/heif"):
        try:
            import pillow_heif  # type: ignore
            pillow_heif.register_heif_opener()
        except ImportError:
            out["derivatives"] = {"status": "unsupported", "reason": "HEIC decoding is not available on this server "
                                                                     "(pillow-heif not installed); original kept"}
            return out
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as e:  # noqa: BLE001
        out["derivatives"] = {"status": "failed", "reason": f"image could not be decoded: {type(e).__name__}"}
        return out
    try:
        exif = im.getexif()
        base = {k: str(v)[:120] for k, v in exif.items() if isinstance(v, (str, int, float))}
        ifd = exif.get_ifd(0x8769) if hasattr(exif, "get_ifd") else {}
        dto = ifd.get(36867) or exif.get(306)
        offset = ifd.get(36881) if ifd else None
        cap, assumed = _parse_exif_time(str(dto) if dto else None, str(offset) if offset else None)
        out["exif"] = {"tags": base, "DateTimeOriginal": str(dto) if dto else None, "Orientation": exif.get(274),
                       "has_gps": bool(exif.get_ifd(0x8825)) if hasattr(exif, "get_ifd") else False}
        out["captured_at"] = cap
        out["captured_at_zone_assumed"] = assumed
    except Exception:  # noqa: BLE001
        out["exif"] = {}
    im = ImageOps.exif_transpose(im) or im
    out["width"], out["height"] = im.size
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    st = storage()
    for name, px in (("thumb", THUMB_PX), ("web", WEB_PX)):
        copy = im.copy()
        copy.thumbnail((px, px))
        buf = io.BytesIO()
        copy.save(buf, format="JPEG", quality=85, optimize=True)  # no exif/icc: metadata stripped
        key, _ = st.put(buf.getvalue(), ext=".jpg", prefix="derivatives")
        out["derivatives"][name] = {"key": key, "width": copy.size[0], "height": copy.size[1], "content_type": "image/jpeg"}
    out["derivatives"]["status"] = "ready"
    out["metadata_stripped"] = True
    return out


def serialize_upload(s: UploadSession) -> dict:
    return {"id": s.id, "version": s.version, "purpose": s.purpose, "intake_id": s.intake_id, "state": s.state,
            "expected_content_type": s.expected_content_type, "detected_content_type": s.detected_content_type,
            "max_bytes": s.max_bytes, "received_bytes": s.received_bytes, "expires_at": iso(s.expires_at),
            "asset_id": s.asset_id, "error": s.error, "original_name": s.original_name, "sha256": s.sha256,
            "finalized_at": iso(s.finalized_at), "deduplicated": bool(s.deduplicated), "put_url": f"/api/uploads/{s.id}",
            "allowed_types": list(s.allowed_types or []), "created_at": iso(s.created_at)}


def serialize_asset(a: Asset) -> dict:
    d = serialize_asset_brief(a)
    d.update({"version": a.version, "storage_key": None, "uploaded_by": a.uploaded_by, "source": a.source,
              "provider_ref": a.provider_ref, "provider_link": a.provider_link, "visibility": a.visibility,
              "metadata_stripped": bool(a.metadata_stripped), "exif": dict(a.exif or {}), "error": a.error,
              "analysis": dict(a.analysis or {}), "owner_client_id": a.owner_client_id})
    return d


def serialize_link(l: AssetLink) -> dict:
    return {"id": l.id, "version": l.version, "asset_id": l.asset_id, "entity_kind": l.entity_kind, "entity_id": l.entity_id,
            "role": l.role, "slot": l.slot, "position": l.position, "confirmed_by": l.confirmed_by,
            "match_evidence": dict(l.match_evidence or {}), "removed_at": iso(l.removed_at), "remove_reason": l.remove_reason,
            "linked_by": l.linked_by, "created_at": iso(l.created_at)}


def owns_session(actor: Actor, s: UploadSession) -> bool:
    if actor.kind == "external":
        return s.client_id == actor.client_id
    if actor.kind == "system":
        return True
    return s.created_by_user_id == actor.user_id or actor.role == "owner"


def owns_asset(actor: Actor, a: Asset) -> bool:
    """Ownership for using an asset id in a new flow (J05: another client's asset id is refused)."""
    if actor.kind == "external":
        return a.owner_client_id == actor.client_id
    if actor.kind == "system":
        return True
    return a.uploaded_by == actor.user_id or actor.role == "owner" or (actor.perms.get("vehicles.all", False) and not a.owner_client_id)


async def get_session(db: AsyncSession, actor: Actor, upload_id: str, *, lock: bool = False) -> UploadSession:
    q = select(UploadSession).where(UploadSession.id == upload_id)
    if lock:
        q = q.with_for_update()
    s = (await db.execute(q)).scalar_one_or_none()
    if s is None or not owns_session(actor, s):
        raise NotFound("upload not found")
    return s


async def active_links(db: AsyncSession, asset_id: str) -> list[AssetLink]:
    return list((await db.execute(select(AssetLink).where(AssetLink.asset_id == asset_id, AssetLink.removed_at.is_(None)))).scalars().all())


async def can_access_asset(db: AsyncSession, actor: Actor, a: Asset) -> bool:
    """Read access: via the linked entity and record scope. Sensitive documents need documents.read."""
    if actor.kind == "system":
        return True
    if actor.kind in ("user", "agent") and actor.role == "owner":
        return True
    if a.sensitive and not has_perm(actor, "documents.read"):
        return False
    if actor.kind == "external":
        if a.owner_client_id and a.owner_client_id == actor.client_id:
            return True
        if not has_perm(actor, "vehicles.read"):
            return False
    elif a.uploaded_by and a.uploaded_by == actor.user_id:
        return True
    links = await active_links(db, a.id)
    if not links:
        return False
    limit = await visible_vehicle_ids(db, actor)
    vehicle_ok = has_perm(actor, "vehicles.read")

    def vis(vid: str | None) -> bool:
        return bool(vid) and vehicle_ok and (limit is None or vid in limit)

    for l in links:
        if l.entity_kind == "vehicle" and vis(l.entity_id):
            return True
        if l.entity_kind == "task" and actor.kind != "external":
            t = await db.get(Task, l.entity_id)
            if t is not None and (t.owner_user_id == actor.user_id or (actor.scope == "all" and has_perm(actor, "tasks.read"))):
                return True
        if l.entity_kind == "recon_issue":
            i = await db.get(ReconIssue, l.entity_id)
            if i is not None and vis(i.vehicle_id):
                return True
        if l.entity_kind == "part":
            p = await db.get(Part, l.entity_id)
            if p is not None and vis(p.vehicle_id):
                return True
        if l.entity_kind == "intake" and actor.kind != "external":
            from ..models.intake import VehicleIntake
            it = await db.get(VehicleIntake, l.entity_id)
            if it is not None and it.owner_user_id == actor.user_id:
                return True
            if it is not None and it.vehicle_id and vis(it.vehicle_id):
                return True
    return False


# ── inputs ───────────────────────────────────────────────────────────────────
class PrepareUploadIn(BaseModel):
    purpose: str = "intake"  # intake|evidence|document|voice|listing
    intake_id: str | None = None
    content_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    filename: str | None = Field(default=None, max_length=255)


class RecordChunkIn(BaseModel):
    upload_id: str
    received_bytes: int = Field(ge=0)
    complete: bool = False


class FinalizeUploadIn(BaseModel):
    upload_id: str
    sha256: str | None = None  # client checksum; mismatch fails the upload
    captured_at: datetime | None = None  # client hint only when EXIF is absent
    pre_arrival: bool = False
    source: str = "upload"  # upload|telegram|mcp|drive|email
    classification: str | None = None


class LinkIn(BaseModel):
    asset_id: str
    entity_kind: str
    entity_id: str
    role: str = "photo"
    slot: str | None = None
    position: int | None = None
    match_evidence: dict = Field(default_factory=dict)
    confirmed: bool = False


class ClassifyIn(BaseModel):
    asset_id: str
    classification: str
    sensitive: bool | None = None
    public_eligible: bool | None = None
    note: str | None = None


class UnlinkIn(BaseModel):
    link_id: str | None = None
    asset_id: str | None = None
    entity_kind: str | None = None
    entity_id: str | None = None
    role: str | None = None
    reason: str | None = None


class TranscriptIn(BaseModel):
    asset_id: str
    transcript: str
    source: str = "typed"  # typed|service


def _records_for_link(p) -> list:
    if p.entity_kind == "vehicle":
        return [("vehicle", p.entity_id)]
    if p.entity_kind == "task":
        return [("task", p.entity_id)]
    return []


# ── commands ─────────────────────────────────────────────────────────────────
@command("assets.prepare_upload", input=PrepareUploadIn, perm="intake", action_class="internal",
         description="Open a bounded, expiring upload slot; returns limits and allowed types before any bytes are sent.")
async def assets_prepare_upload(ctx: CommandContext, inp: PrepareUploadIn) -> dict:
    allowed = allowed_types()
    if inp.content_type and inp.content_type.split(";")[0].strip().lower() not in allowed:
        raise ValidationFailed(f"unsupported type {inp.content_type}; allowed: {', '.join(allowed)}", allowed_types=allowed)
    if inp.size_bytes is not None and inp.size_bytes > max_bytes():
        raise ValidationFailed(f"file too large ({inp.size_bytes} bytes; limit {max_bytes()})", max_bytes=max_bytes())
    if inp.size_bytes == 0:
        raise ValidationFailed("empty file")
    s = UploadSession(created_by_user_id=ctx.actor.user_id if ctx.actor.kind != "external" else None,
                      client_id=ctx.actor.client_id, purpose=inp.purpose, intake_id=inp.intake_id,
                      expected_content_type=(inp.content_type or "").split(";")[0].strip().lower() or None,
                      max_bytes=max_bytes(), expires_at=ctx.now + UPLOAD_TTL, state="open", received_bytes=0,
                      original_name=inp.filename, allowed_types=allowed, created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
    ctx.db.add(s)
    await ctx.db.flush()
    s.storage_key = part_key(s.id)
    ctx.changed.append({"kind": "upload_session", "id": s.id, "version": s.version})
    ctx.record(f"Upload prepared: {inp.filename or inp.purpose}", entity_kind="upload_session", entity_id=s.id, kind="intake",
               state="open", visibility="owner", details={"purpose": inp.purpose, "intake_id": inp.intake_id})
    return {"upload": serialize_upload(s), "put_url": f"/api/uploads/{s.id}", "max_bytes": s.max_bytes,
            "allowed_types": allowed, "expires_at": iso(s.expires_at)}


@command("assets.record_chunk", input=RecordChunkIn, perm="intake", action_class="internal",
         description="Durably record the bytes received so far for a resumable upload (the router wrote them to the part file).")
async def assets_record_chunk(ctx: CommandContext, inp: RecordChunkIn) -> dict:
    s = await get_session(ctx.db, ctx.actor, inp.upload_id, lock=True)
    if s.state not in ("open", "received"):
        raise Blocked(f"upload is {s.state}")
    if s.expires_at and s.expires_at < ctx.now:
        s.state = "expired"
        raise Blocked("upload slot expired; prepare a new upload")
    if inp.received_bytes > s.max_bytes:
        s.state = "failed"
        s.error = f"file too large (limit {s.max_bytes} bytes)"
        raise ValidationFailed(s.error, max_bytes=s.max_bytes)
    s.received_bytes = inp.received_bytes
    s.state = "received" if inp.received_bytes > 0 else "open"
    ctx.touch(s, "upload_session")
    return {"upload": serialize_upload(s)}


@command("assets.finalize_upload", input=FinalizeUploadIn, perm="intake", action_class="internal",
         description="Validate (magic bytes, size, checksum), deduplicate by sha256, store the original, extract capture time and "
                     "build private derivatives. A failed file stays failed and retryable; nothing partial becomes an asset.")
async def assets_finalize_upload(ctx: CommandContext, inp: FinalizeUploadIn) -> dict:
    s = await get_session(ctx.db, ctx.actor, inp.upload_id, lock=True)
    if s.state == "finalized" and s.asset_id:
        a = await ctx.db.get(Asset, s.asset_id)
        return {"status": "ready", "asset": serialize_asset(a) if a else None, "upload": serialize_upload(s),
                "deduplicated": bool(s.deduplicated), "error": None}
    if s.state == "failed":
        return {"status": "failed", "asset": None, "upload": serialize_upload(s), "deduplicated": False, "error": s.error}
    if s.expires_at and s.expires_at < ctx.now:
        s.state = "expired"
        s.error = "upload slot expired"
        return {"status": "failed", "asset": None, "upload": serialize_upload(s), "deduplicated": False, "error": s.error}
    st = storage()
    key = s.storage_key or part_key(s.id)

    def fail(msg: str) -> dict:
        s.state = "failed"
        s.error = msg
        ctx.touch(s, "upload_session")
        ctx.record(f"Upload failed: {s.original_name or s.id} — {msg}", entity_kind="upload_session", entity_id=s.id, kind="intake",
                   state="failed", exception=True, visibility="owner", details={"intake_id": s.intake_id})
        return {"status": "failed", "asset": None, "upload": serialize_upload(s), "deduplicated": False, "error": msg}

    if not st.exists(key):
        return fail("no bytes received")
    data = st.get(key)
    if not data:
        return fail("empty file")
    if len(data) > s.max_bytes:
        return fail(f"file too large ({len(data)} bytes; limit {s.max_bytes})")
    if s.received_bytes and len(data) != s.received_bytes:
        return fail(f"incomplete upload ({len(data)} of {s.received_bytes} bytes)")
    ctype = sniff(data)
    if ctype is None:
        return fail("unrecognized file type (only JPEG, PNG, WEBP, HEIC, PDF and common audio are accepted)")
    if ctype not in (s.allowed_types or allowed_types()):
        return fail(f"unsupported type {ctype}")
    if s.expected_content_type and s.expected_content_type != ctype and kind_of(s.expected_content_type) != kind_of(ctype):
        return fail(f"declared {s.expected_content_type} but the bytes are {ctype}")
    sha = sha256_hex(data)
    if inp.sha256 and inp.sha256.lower() != sha:
        return fail("checksum mismatch; re-upload the file")
    s.detected_content_type = ctype
    s.sha256 = sha
    existing = (await ctx.db.execute(select(Asset).where(Asset.sha256 == sha, Asset.status == "ready"))).scalars().first()
    # identical bytes -> same Asset id (new links are made by the caller). External clients only dedupe against
    # assets they own so one client can never learn or reuse another client's asset id (J05).
    dedup = existing is not None and (ctx.actor.kind != "external" or existing.owner_client_id == ctx.actor.client_id)
    if dedup:
        a = existing
        st.delete(key)
        s.state = "finalized"
        s.asset_id = a.id
        s.finalized_at = ctx.now
        s.deduplicated = True
        ctx.touch(s, "upload_session")
        ctx.record(f"Upload deduplicated: identical to asset {a.id[:8]}", entity_kind="asset", entity_id=a.id, kind="intake",
                   state="ready", visibility="owner", details={"upload_id": s.id, "sha256": sha})
        return {"status": "ready", "asset": serialize_asset(a), "upload": serialize_upload(s), "deduplicated": True, "error": None}
    kind = kind_of(ctype)
    stored_key, _ = st.put(data, ext=EXT.get(ctype, ""))
    st.delete(key)
    a = Asset(kind=kind, storage_key=stored_key, original_name=s.original_name, content_type=ctype, size_bytes=len(data), sha256=sha,
              uploaded_at=ctx.now, uploaded_by=ctx.actor.user_id if ctx.actor.kind != "external" else None, source=inp.source,
              owner_client_id=ctx.actor.client_id if ctx.actor.kind == "external" else None,
              classification=inp.classification if inp.classification in CLASSIFICATIONS else ("voice_note" if kind == "audio" else "unknown"),
              sensitive=(inp.classification in SENSITIVE_CLASSES) if inp.classification else False, public_eligible=False,
              pre_arrival=inp.pre_arrival, visibility="internal", status="ready", derivatives={}, exif={},
              created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
    if kind == "photo":
        info = await asyncio.to_thread(process_image, data, ctype)
        a.width, a.height = info.get("width"), info.get("height")
        a.exif = dict(info.get("exif") or {})
        if info.get("captured_at"):
            a.captured_at = info["captured_at"]
            a.exif["captured_at_zone_assumed"] = bool(info.get("captured_at_zone_assumed"))
            a.exif["captured_at_source"] = "exif"
        elif inp.captured_at is not None:
            a.captured_at = inp.captured_at if inp.captured_at.tzinfo else inp.captured_at.replace(tzinfo=timezone.utc)
            a.exif["captured_at_source"] = "client_hint"
        a.derivatives = dict(info.get("derivatives") or {})
        a.metadata_stripped = bool(info.get("metadata_stripped"))
    elif kind == "audio":
        a.derivatives = {"status": "not_applicable"}
    else:
        a.derivatives = {"status": "not_applicable"}
    ctx.db.add(a)
    await ctx.db.flush()
    s.state = "finalized"
    s.asset_id = a.id
    s.finalized_at = ctx.now
    ctx.touch(s, "upload_session")
    ctx.changed.append({"kind": "asset", "id": a.id, "version": a.version})
    ctx.record(f"Saved {kind}: {s.original_name or a.id[:8]} ({ctype}, {len(data)} bytes)", entity_kind="asset", entity_id=a.id,
               kind="intake", state="ready", visibility="owner",
               details={"upload_id": s.id, "sha256": sha, "derivatives": (a.derivatives or {}).get("status"), "intake_id": s.intake_id})
    ctx.emit("asset.saved", aggregate_type="asset", aggregate_id=a.id, payload={"asset_id": a.id, "kind": kind, "sha256": sha,
                                                                               "intake_id": s.intake_id})
    return {"status": "ready", "asset": serialize_asset(a), "upload": serialize_upload(s), "deduplicated": False, "error": None}


@command("assets.link", input=LinkIn, perm="intake", action_class="internal", records=_records_for_link,
         description="Link a saved asset to a record (vehicle photo/evidence, task, intake, ...). Idempotent per (asset, entity, role).")
async def assets_link(ctx: CommandContext, inp: LinkIn) -> dict:
    if inp.entity_kind not in ENTITY_KINDS:
        raise ValidationFailed(f"entity_kind must be one of {ENTITY_KINDS}")
    if inp.role not in LINK_ROLES:
        raise ValidationFailed(f"role must be one of {LINK_ROLES}")
    a = await ctx.db.get(Asset, inp.asset_id)
    if a is None:
        raise NotFound("asset not found")
    if a.status != "ready":
        raise Blocked("asset upload is not complete", status=a.status)
    if not owns_asset(ctx.actor, a) and not await can_access_asset(ctx.db, ctx.actor, a):
        raise Denied("asset not accessible")
    if a.sensitive and inp.role in ("photo", "gallery"):
        raise ValidationFailed("a sensitive document cannot be linked as a photo; use role document")
    if inp.entity_kind == "vehicle":
        v = await ctx.db.get(Vehicle, inp.entity_id)
        if v is None:
            raise NotFound("vehicle not found")
    existing = (await ctx.db.execute(select(AssetLink).where(AssetLink.asset_id == a.id, AssetLink.entity_kind == inp.entity_kind,
                                                             AssetLink.entity_id == inp.entity_id, AssetLink.role == inp.role))).scalars().first()
    created = False
    if existing is not None:
        l = existing
        if l.removed_at is not None:
            l.removed_at, l.removed_by, l.remove_reason = None, None, None
            ctx.touch(l, "asset_link")
            created = True
        if inp.slot and l.slot != inp.slot:
            l.slot = inp.slot
            ctx.touch(l, "asset_link")
    else:
        n = await ctx.db.scalar(select(func.count()).select_from(AssetLink).where(
            AssetLink.entity_kind == inp.entity_kind, AssetLink.entity_id == inp.entity_id, AssetLink.removed_at.is_(None)))
        l = AssetLink(asset_id=a.id, entity_kind=inp.entity_kind, entity_id=inp.entity_id, role=inp.role, slot=inp.slot,
                      position=inp.position if inp.position is not None else int(n or 0),
                      confirmed_by=ctx.actor.user_id if inp.confirmed else None, match_evidence=inp.match_evidence,
                      linked_by=ctx.actor.user_id or ctx.actor.client_id, created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
        ctx.db.add(l)
        await ctx.db.flush()
        created = True
        ctx.changed.append({"kind": "asset_link", "id": l.id, "version": l.version})
    if inp.entity_kind == "vehicle" and inp.role in ("photo", "gallery") and a.kind == "photo":
        v = await ctx.db.get(Vehicle, inp.entity_id)
        if v is not None and not v.hero_asset_id:
            v.hero_asset_id = a.id
    if created:
        ctx.record(f"Linked {a.kind} to {inp.entity_kind} as {inp.role}", entity_kind=inp.entity_kind, entity_id=inp.entity_id,
                   kind="intake", state="linked", details={"asset_id": a.id, "link_id": l.id, "slot": inp.slot})
        ctx.emit("evidence.saved", aggregate_type=inp.entity_kind, aggregate_id=inp.entity_id,
                 payload={"asset_ids": [a.id], "link_id": l.id, "role": inp.role, "entity_kind": inp.entity_kind, "entity_id": inp.entity_id,
                          "vehicle_id": inp.entity_id if inp.entity_kind == "vehicle" else None})
    return {"link": serialize_link(l), "asset": serialize_asset(a), "created": created}


@command("assets.classify", input=ClassifyIn, perm="vehicles.write", action_class="internal",
         description="Classify an asset (listing photo, invoice, ID, shipping paper, screenshot, unrelated). Sensitive documents are never public-eligible.")
async def assets_classify(ctx: CommandContext, inp: ClassifyIn) -> dict:
    if inp.classification not in CLASSIFICATIONS:
        raise ValidationFailed(f"classification must be one of {CLASSIFICATIONS}")
    a = await ctx.db.get(Asset, inp.asset_id)
    if a is None:
        raise NotFound("asset not found")
    if not await can_access_asset(ctx.db, ctx.actor, a) and not owns_asset(ctx.actor, a):
        raise Denied("asset not accessible")
    before = (a.classification, a.sensitive, a.public_eligible)
    a.classification = inp.classification
    a.sensitive = True if inp.classification in SENSITIVE_CLASSES else (bool(inp.sensitive) if inp.sensitive is not None else a.sensitive)
    if a.sensitive:
        a.public_eligible = False
        a.visibility = "owner" if inp.classification == "id_document" else "internal"
    elif inp.public_eligible is not None:
        a.public_eligible = bool(inp.public_eligible) and inp.classification == "listing_photo"
    ctx.touch(a, "asset")
    ctx.record(f"Classified asset as {inp.classification}" + (" (sensitive)" if a.sensitive else ""), entity_kind="asset", entity_id=a.id,
               kind="intake", state=inp.classification, details={"before": before, "public_eligible": a.public_eligible, "note": inp.note})
    return {"asset": serialize_asset(a)}


@command("assets.remove_link", input=UnlinkIn, perm="intake", action_class="internal",
         records=lambda p: [("vehicle", p.entity_id)] if p.entity_kind == "vehicle" and p.entity_id else [],
         description="Retire a link (the asset and its evidence are kept). Reversible by linking again.")
async def assets_remove_link(ctx: CommandContext, inp: UnlinkIn) -> dict:
    if inp.link_id:
        l = await ctx.db.get(AssetLink, inp.link_id)
    elif inp.asset_id and inp.entity_kind and inp.entity_id:
        q = select(AssetLink).where(AssetLink.asset_id == inp.asset_id, AssetLink.entity_kind == inp.entity_kind, AssetLink.entity_id == inp.entity_id)
        if inp.role:
            q = q.where(AssetLink.role == inp.role)
        l = (await ctx.db.execute(q)).scalars().first()
    else:
        raise ValidationFailed("give link_id or asset_id + entity_kind + entity_id")
    if l is None:
        raise NotFound("link not found")
    if l.entity_kind == "vehicle" and not inp.link_id:
        pass
    if l.removed_at is not None:
        return {"link": serialize_link(l), "removed": False}
    l.removed_at = ctx.now
    l.removed_by = ctx.actor.user_id
    l.remove_reason = inp.reason
    ctx.touch(l, "asset_link")
    if l.entity_kind == "vehicle":
        v = await ctx.db.get(Vehicle, l.entity_id)
        if v is not None and v.hero_asset_id == l.asset_id:
            other = (await ctx.db.execute(select(AssetLink).where(AssetLink.entity_kind == "vehicle", AssetLink.entity_id == v.id,
                                                                  AssetLink.removed_at.is_(None), AssetLink.role.in_(("photo", "gallery")),
                                                                  AssetLink.asset_id != l.asset_id).order_by(AssetLink.position))).scalars().first()
            v.hero_asset_id = other.asset_id if other else None
    ctx.record(f"Unlinked asset from {l.entity_kind}" + (f" — {inp.reason}" if inp.reason else ""), entity_kind=l.entity_kind,
               entity_id=l.entity_id, kind="intake", state="unlinked", details={"asset_id": l.asset_id, "link_id": l.id})
    return {"link": serialize_link(l), "removed": True}


@command("assets.set_transcript", input=TranscriptIn, perm="intake", action_class="internal",
         description="Store the editable transcript of a voice note (typed by a person or returned by a transcription service).")
async def assets_set_transcript(ctx: CommandContext, inp: TranscriptIn) -> dict:
    a = await ctx.db.get(Asset, inp.asset_id)
    if a is None:
        raise NotFound("asset not found")
    if a.kind != "audio":
        raise ValidationFailed("transcripts belong to audio assets")
    if not owns_asset(ctx.actor, a) and not await can_access_asset(ctx.db, ctx.actor, a):
        raise Denied("asset not accessible")
    a.transcript = inp.transcript
    a.analysis = {**(a.analysis or {}), "transcript_source": inp.source, "transcript_at": ctx.now.isoformat()}
    ctx.touch(a, "asset")
    ctx.record("Transcript saved for voice note", entity_kind="asset", entity_id=a.id, kind="intake", state="transcribed",
               details={"source": inp.source, "chars": len(inp.transcript)})
    return {"asset": serialize_asset(a)}


# ── reads ────────────────────────────────────────────────────────────────────
def variant_bytes(a: Asset, variant: str) -> tuple[bytes, str] | None:
    """(bytes, content_type) for original|web|thumb, or None when the derivative honestly does not exist."""
    st = storage()
    if variant == "original":
        return st.get(a.storage_key), a.content_type
    d = (a.derivatives or {}).get(variant)
    if not isinstance(d, dict) or not d.get("key"):
        return None
    if not st.exists(d["key"]):
        return None
    return st.get(d["key"]), d.get("content_type", "image/jpeg")


async def links_of(db: AsyncSession, entity_kind: str, entity_id: str) -> list[tuple[AssetLink, Asset]]:
    rows = (await db.execute(select(AssetLink, Asset).join(Asset, Asset.id == AssetLink.asset_id).where(
        AssetLink.entity_kind == entity_kind, AssetLink.entity_id == entity_id, AssetLink.removed_at.is_(None))
        .order_by(AssetLink.position, AssetLink.created_at))).all()
    return [(l, a) for l, a in rows]
