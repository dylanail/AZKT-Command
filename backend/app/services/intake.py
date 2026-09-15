"""Photo / voice intake through Manager (spec §7.4; acceptance I01–I07, H13; invariant 13).

A durable `VehicleIntake` keeps photos, transcript, notes, analysis and applied results together across follow-up
messages and reconnects. Every write is a command:

    intake.start          durable session with explicit target (new | existing | find); idempotent per (client, request_key)
    intake.add_assets     finalized uploads join the intake; failed uploads are tracked per file, never silently dropped
    intake.add_note       verbatim owner text
    intake.add_transcript voice note (audio asset) with typed or service transcript; no transcript -> needs_info, photos kept
    intake.analyze        model extraction with vision blocks, or the deterministic fallback; provenance recorded
    intake.choose_vehicle resolve an ambiguous target explicitly (visual similarity never auto-selects)
    intake.apply          transactional: create/update the card, condition bullets, recon issues + verb-first tasks,
                          photo links, sourced milestones; retries never duplicate (dedupe on every row)
    intake.correct        edit/remove a bullet or observation, change a task assignee; history kept
    intake.undo           reverse the last apply's reversible changes (evidence retained)
    intake.abandon        close the session; saved assets stay private

Nothing here invents a frame number, price, date, diagnosis or readiness. Uncertain identifiers become
`needs_confirmation` observations with a focused task and are never written to the card.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.errors import Blocked, Conflict, Denied, DomainError, NotFound, ValidationFailed
from ..core.ids import sha256_hex
from ..domain.access import visible_vehicle_ids
from ..domain.actors import Actor
from ..domain.commands import CommandContext, command, dispatch
from ..domain.policy import has_perm
from ..models import User
from ..models.assets import Asset, AssetLink, UploadSession
from ..models.intake import IntakeObservation, VehicleIntake
from ..models.tasks import Task
from ..models.vehicles import Vehicle
from . import intake_analysis as ia
from .assets import can_access_asset, owns_asset, owns_session, serialize_upload, variant_bytes
from .vehicles import (CRITICAL_FACT_KEYS, current_bullets, get_vehicle, iso, loaded, norm_text, normalize_frame, normalize_stock_no,
                       serialize_asset_brief, serialize_vehicle, vehicle_title)

log = logging.getLogger("azkt.intake")

TARGET_MODES = ("new", "existing", "find")
OPEN_STATUSES = ("open", "analyzing", "analyzed", "needs_choice", "needs_info", "partially_applied", "undone")
CLOSED_STATUSES = ("abandoned",)
CONFIRM_TASK_TITLES = {"frame_no": "Confirm frame number", "stock_no": "Confirm stock number", "odometer_km": "Confirm odometer reading",
                       "model_year": "Confirm model year", "make": "Confirm make", "model": "Confirm model", "color": "Confirm color",
                       "received_date": "Confirm arrival date"}
CONDITION_SOURCE_FOR = {"owner_text": "owner_reported", "owner_voice": "owner_reported", "image": "image_observed",
                        "proposed_check": "proposed_check"}
ISSUE_SOURCE_FOR = {"owner_reported": "owner_reported", "image_observed": "image_observed", "proposed_check": "proposed_check"}


# ── helpers ──────────────────────────────────────────────────────────────────
def obs_key(intake_id: str, kind: str, text: str, field: str | None = None, value: str | None = None) -> str:
    return sha256_hex(f"{intake_id}|{kind}|{field or ''}|{norm_text(value or '')}|{norm_text(text)}")[:32]


def _owns_intake(actor: Actor, it: VehicleIntake) -> bool:
    if actor.kind == "system":
        return True
    if actor.kind == "external":
        return it.client_key == actor.key
    return it.owner_user_id == actor.user_id or actor.role == "owner"


async def load_intake(db: AsyncSession, actor: Actor, intake_id: str, *, lock: bool = False,
                      expected_version: int | None = None) -> VehicleIntake:
    q = select(VehicleIntake).where(VehicleIntake.id == intake_id)
    if lock:
        q = q.with_for_update()
    it = (await db.execute(q)).scalar_one_or_none()
    if it is None or not _owns_intake(actor, it):
        raise NotFound("intake not found")
    if expected_version is not None and it.version != expected_version:
        raise Conflict("intake changed since you loaded it", current_version=it.version)
    return it


async def observations_of(db: AsyncSession, intake_id: str) -> list[IntakeObservation]:
    return list((await db.execute(select(IntakeObservation).where(IntakeObservation.intake_id == intake_id)
                                  .order_by(IntakeObservation.created_at, IntakeObservation.id))).scalars().all())


def serialize_observation(o: IntakeObservation) -> dict:
    return {"id": o.id, "version": o.version, "intake_id": o.intake_id, "revision": o.revision, "kind": o.kind, "text": o.text,
            "source": o.source, "asset_id": o.asset_id, "confidence": o.confidence, "field": o.field, "value": o.value,
            "applied_kind": o.applied_kind, "applied_id": o.applied_id, "applied_command": o.applied_command,
            "applied_version": o.applied_version, "status": o.status, "removed": bool(o.removed), "error": o.error,
            "meta": dict(o.meta or {}), "history": list(o.history or []), "created_at": iso(o.created_at)}


def serialize_intake(it: VehicleIntake) -> dict:
    return {"id": it.id, "version": it.version, "owner_user_id": it.owner_user_id, "channel": it.channel, "client_key": it.client_key,
            "request_key": it.request_key, "target_mode": it.target_mode, "vehicle_id": it.vehicle_id,
            "candidate_vehicle_ids": list(it.candidate_vehicle_ids or []), "status": it.status, "revision": it.revision,
            "text_notes": it.text_notes, "transcript": it.transcript, "transcript_asset_ids": list(it.transcript_asset_ids or []),
            "asset_ids": list(it.asset_ids or []), "failed_asset_ids": list(it.failed_asset_ids or []),
            "failed_uploads": list(it.failed_uploads or []), "analysis": dict(it.analysis or {}), "result": dict(it.result or {}),
            "missing_fields": list(it.missing_fields or []), "last_error": it.last_error, "mission_id": it.mission_id,
            "telegram_media_group_id": it.telegram_media_group_id, "applied_at": iso(it.applied_at), "undone_at": iso(it.undone_at),
            "device_draft": dict(it.device_draft or {}), "choice": dict(it.choice or {}), "corrections": list(it.corrections or []),
            "applied": dict(it.applied or {}), "complete": it.status == "applied",
            "label": {"open": "Open", "analyzing": "Analyzing", "analyzed": "Ready to apply", "needs_choice": "Choose the vehicle",
                      "needs_info": "Needs information", "applied": "Saved to AZKT", "partially_applied": "Partially saved",
                      "failed": "Failed", "abandoned": "Abandoned", "undone": "Undone"}.get(it.status, it.status),
            "created_at": iso(loaded(it, "created_at")), "updated_at": iso(loaded(it, "updated_at"))}


async def intake_status(db: AsyncSession, actor: Actor, it: VehicleIntake) -> dict:
    """Per-item saved/failed status (spec §3.1 VehicleIntake): assets, failed uploads, observations, current summary."""
    obs = await observations_of(db, it.id)
    assets = []
    if it.asset_ids:
        rows = (await db.execute(select(Asset).where(Asset.id.in_(list(it.asset_ids))))).scalars().all()
        by_id = {a.id: a for a in rows}
        links: dict[str, AssetLink] = {}
        if it.vehicle_id:
            lrows = (await db.execute(select(AssetLink).where(AssetLink.asset_id.in_(list(it.asset_ids)), AssetLink.entity_kind == "vehicle",
                                                              AssetLink.entity_id == it.vehicle_id, AssetLink.removed_at.is_(None)))).scalars().all()
            links = {l.asset_id: l for l in lrows}
        for aid in it.asset_ids:
            a = by_id.get(aid)
            if a is None:
                assets.append({"asset_id": aid, "status": "failed", "saved_to_azkt": False, "error": "asset missing"})
                continue
            d = serialize_asset_brief(a)
            d.update({"saved_to_azkt": a.status == "ready", "status": "saved" if a.status == "ready" else "failed",
                      "linked_to_vehicle": aid in links, "error": a.error})
            assets.append(d)
    items = [*assets, *[{"upload_id": f.get("upload_id"), "name": f.get("name"), "status": "failed", "saved_to_azkt": False,
                         "error": f.get("error"), "retryable": True} for f in (it.failed_uploads or [])]]
    vehicle = None
    if it.vehicle_id:
        v = await db.get(Vehicle, it.vehicle_id)
        if v is not None:
            vehicle = {"id": v.id, "stock_no": v.stock_no, "title": vehicle_title(v), "intake_status": v.intake_status,
                       "missing_identity_fields": list(v.missing_identity_fields or []), "condition": current_bullets(v),
                       "condition_version": v.condition_version, "archived_at": iso(v.archived_at)}
    return {"intake": serialize_intake(it), "items": items, "observations": [serialize_observation(o) for o in obs],
            "vehicle": vehicle, "target": _target_label(it, vehicle),
            "saved_to_device": dict(it.device_draft or {}), "saved_to_azkt": {"assets": sum(1 for a in assets if a["status"] == "saved"),
                                                                              "failed": len(items) - sum(1 for a in assets if a["status"] == "saved"),
                                                                              "applied": it.status in ("applied", "partially_applied")}}


def _target_label(it: VehicleIntake, vehicle: dict | None) -> dict:
    if it.vehicle_id and vehicle:
        return {"mode": it.target_mode, "vehicle_id": it.vehicle_id, "label": f"{vehicle['stock_no'] or ''} {vehicle['title']}".strip()}
    if it.target_mode == "new":
        return {"mode": "new", "vehicle_id": None, "label": "New vehicle"}
    return {"mode": "find", "vehicle_id": None, "label": "Find the vehicle" if it.status != "needs_choice" else "Choose the vehicle"}


async def _vehicle_allowed(db: AsyncSession, actor: Actor, vehicle_id: str) -> None:
    """Record scope for writes into an existing card (assigned-scope people, record-limited clients)."""
    limit = await visible_vehicle_ids(db, actor)
    if limit is not None and vehicle_id not in limit:
        raise Denied(f"vehicle {vehicle_id} not accessible to you")


async def _resolve_user(db: AsyncSession, name: str | None) -> User | None:
    if not name:
        return None
    n = name.strip().lower()
    rows = (await db.execute(select(User).where(User.status == "active", or_(func.lower(User.handle) == n,
                                                                             func.lower(User.display_name) == n)))).scalars().all()
    if len(rows) == 1:
        return rows[0]
    rows = (await db.execute(select(User).where(User.status == "active", func.lower(User.display_name).like(f"{n}%")))).scalars().all()
    return rows[0] if len(rows) == 1 else None


def _candidate_context(v: Vehicle, reasons: list[str] | None = None, score: float | None = None) -> dict:
    return {"vehicle_id": v.id, "stock_no": v.stock_no, "title": vehicle_title(v), "frame_no_raw": v.frame_no_raw,
            "model_year": v.model_year, "color": v.color, "location": v.location, "recon_state": v.recon_state,
            "logistics_state": v.logistics_state, "hero_asset_id": v.hero_asset_id, "score": score, "reasons": reasons or []}


# ── inputs ───────────────────────────────────────────────────────────────────
class IntakeStartIn(BaseModel):
    target_mode: str = "find"  # new|existing|find
    vehicle_id: str | None = None
    channel: str = "web"  # web|telegram|mcp|http|agent
    request_key: str | None = None  # idempotency for the same client (reconnect, Telegram album, retry)
    text: str | None = None
    asset_ids: list[str] = Field(default_factory=list)
    telegram_media_group_id: str | None = None
    mission_id: str | None = None
    device_draft: dict = Field(default_factory=dict)


class IntakeAssetsIn(BaseModel):
    intake_id: str
    asset_ids: list[str] = Field(default_factory=list)
    upload_ids: list[str] = Field(default_factory=list)  # finalized -> asset; failed -> per-file failure
    failed: list[dict] = Field(default_factory=list)  # client-reported failures [{upload_id?, name, error}]
    clear_failed: list[str] = Field(default_factory=list)  # upload ids / names whose retry succeeded elsewhere
    expected_version: int | None = None


class IntakeNoteIn(BaseModel):
    intake_id: str
    text: str = Field(min_length=1, max_length=8000)
    expected_version: int | None = None


class IntakeTranscriptIn(BaseModel):
    intake_id: str
    text: str | None = None
    audio_asset_id: str | None = None
    source: str = "typed"  # typed|service
    expected_version: int | None = None


class IntakeRefIn(BaseModel):
    intake_id: str
    expected_version: int | None = None
    reason: str | None = None


class IntakeChooseIn(BaseModel):
    intake_id: str
    vehicle_id: str | None = None
    create_new: bool = False
    expected_version: int | None = None


class IntakeApplyIn(BaseModel):
    intake_id: str
    expected_version: int | None = None
    assignee_user_id: str | None = None
    priority: str | None = None
    analyze: bool = True  # (re)analyze when no analysis exists or content changed since the last one


class IntakeCorrectIn(BaseModel):
    intake_id: str
    observation_id: str | None = None
    bullet_id: str | None = None
    task_id: str | None = None
    text: str | None = None
    remove: bool = False
    assignee_user_id: str | None = None
    unassign: bool = False
    reason: str | None = None
    expected_version: int | None = None


# ── commands: session content ────────────────────────────────────────────────
@command("intake.start", input=IntakeStartIn, perm="intake", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)] if p.vehicle_id else [],
         description="Start (or resume, by request_key) a durable intake session with an explicit target: new vehicle, "
                     "an existing vehicle, or find the vehicle from the evidence.")
async def intake_start(ctx: CommandContext, inp: IntakeStartIn) -> dict:
    if inp.target_mode not in TARGET_MODES:
        raise ValidationFailed(f"target_mode must be one of {TARGET_MODES}")
    client_key = ctx.actor.key
    if inp.request_key:
        existing = (await ctx.db.execute(select(VehicleIntake).where(VehicleIntake.client_key == client_key,
                                                                     VehicleIntake.request_key == inp.request_key))).scalar_one_or_none()
        if existing is not None:
            return {"intake": serialize_intake(existing), "created": False}
    if inp.telegram_media_group_id:
        existing = (await ctx.db.execute(select(VehicleIntake).where(VehicleIntake.client_key == client_key,
                                                                     VehicleIntake.telegram_media_group_id == inp.telegram_media_group_id,
                                                                     VehicleIntake.status.in_(OPEN_STATUSES)))).scalars().first()
        if existing is not None:
            return {"intake": serialize_intake(existing), "created": False}
    vehicle_id = None
    if inp.target_mode == "existing":
        if not inp.vehicle_id:
            raise ValidationFailed("existing vehicle intake needs vehicle_id")
        v = await get_vehicle(ctx.db, inp.vehicle_id, lock=False)
        if v.archived_at:
            raise Blocked("vehicle is archived; restore it first")
        vehicle_id = v.id
    elif inp.vehicle_id:
        raise ValidationFailed("vehicle_id is only accepted with target_mode existing")
    owner_id = ctx.actor.user_id
    if owner_id is None:
        raise ValidationFailed("intake needs a person context (external clients act for the owner)")
    it = VehicleIntake(owner_user_id=owner_id, channel=inp.channel or ctx.channel, client_key=client_key, request_key=inp.request_key,
                       target_mode=inp.target_mode, vehicle_id=vehicle_id, status="open", revision=0, text_notes=(inp.text or "").strip(),
                       transcript="", asset_ids=[], failed_asset_ids=[], failed_uploads=[], analysis={}, result={}, missing_fields=[],
                       telegram_media_group_id=inp.telegram_media_group_id, mission_id=inp.mission_id or ctx.mission_id,
                       device_draft=dict(inp.device_draft or {}), created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
    ctx.db.add(it)
    await ctx.db.flush()
    ctx.changed.append({"kind": "vehicle_intake", "id": it.id, "version": it.version})
    ctx.record(f"Intake started ({inp.target_mode})", entity_kind="vehicle_intake", entity_id=it.id, kind="intake", state="open",
               details={"target_mode": inp.target_mode, "vehicle_id": vehicle_id, "channel": it.channel, "request_key": inp.request_key})
    ctx.emit("intake.started", aggregate_type="vehicle_intake", aggregate_id=it.id, aggregate_version=it.version,
             payload={"intake_id": it.id, "target_mode": inp.target_mode, "vehicle_id": vehicle_id, "channel": it.channel})
    added = None
    if inp.asset_ids:
        added = await _add_assets(ctx, it, inp.asset_ids, [], [], [])
    return {"intake": serialize_intake(it), "created": True, "assets": added}


async def _add_assets(ctx: CommandContext, it: VehicleIntake, asset_ids: list[str], upload_ids: list[str], failed: list[dict],
                      clear_failed: list[str]) -> dict:
    saved, failures, skipped = [], [], []
    resolved = list(asset_ids)
    for uid in upload_ids:
        s = (await ctx.db.execute(select(UploadSession).where(UploadSession.id == uid))).scalar_one_or_none()
        if s is None or not owns_session(ctx.actor, s):
            failures.append({"upload_id": uid, "name": None, "error": "upload not found"})
            continue
        if s.state == "finalized" and s.asset_id:
            resolved.append(s.asset_id)
        else:
            failures.append({"upload_id": uid, "name": s.original_name, "error": s.error or f"upload {s.state}",
                             "state": s.state, "received_bytes": s.received_bytes, "upload": serialize_upload(s)})
    for f in failed:
        failures.append({"upload_id": f.get("upload_id"), "name": f.get("name"), "error": str(f.get("error") or "upload failed")[:300],
                         "reported_by": "client"})
    current = list(it.asset_ids or [])
    audio = list(it.transcript_asset_ids or [])
    linked = []
    for aid in dict.fromkeys(resolved):
        a = await ctx.db.get(Asset, aid)
        if a is None:
            failures.append({"asset_id": aid, "name": None, "error": "asset not found"})
            continue
        if a.status != "ready":
            failures.append({"asset_id": aid, "name": a.original_name, "error": a.error or f"asset {a.status}"})
            continue
        if not owns_asset(ctx.actor, a) and not await can_access_asset(ctx.db, ctx.actor, a):
            raise Denied("asset not accessible")  # J05: never accept another client's asset id
        role = "voice" if a.kind == "audio" else ("document" if a.kind == "document" else "photo")
        res = await dispatch(ctx.child(), "assets.link", {"asset_id": a.id, "entity_kind": "intake", "entity_id": it.id, "role": role}, commit=False)
        linked.append((res.data or {}).get("link"))
        if aid not in current:
            current.append(aid)
        if a.kind == "audio" and aid not in audio:
            audio.append(aid)
        saved.append(serialize_asset_brief(a))
    it.asset_ids = current
    it.transcript_asset_ids = audio
    prior = list(it.failed_uploads or [])
    names_now = {a.get("original_name") for a in saved if a.get("original_name")}
    keep = [f for f in prior if f.get("upload_id") not in clear_failed and f.get("name") not in clear_failed
            and not (f.get("name") and f.get("name") in names_now)]
    keys = {(f.get("upload_id"), f.get("name"), f.get("error")) for f in keep}
    for f in failures:
        k = (f.get("upload_id"), f.get("name"), f.get("error"))
        if k not in keys:
            keep.append({k2: v for k2, v in f.items() if k2 != "upload"})
            keys.add(k)
    it.failed_uploads = keep
    it.failed_asset_ids = [f["asset_id"] for f in keep if f.get("asset_id")]
    if it.status in ("applied",) and (saved or failures):
        it.status = "partially_applied" if failures else "analyzed"  # new evidence after an apply: apply again to link it
    if it.status == "analyzed" and saved:
        it.analysis = {**(it.analysis or {}), "stale": True, "stale_reason": "assets added after analysis"}
    ctx.touch(it, "vehicle_intake")
    ctx.record(f"Intake assets: {len(saved)} saved, {len(failures)} failed", entity_kind="vehicle_intake", entity_id=it.id, kind="intake",
               state=it.status, exception=bool(failures), details={"saved": [a["id"] for a in saved], "failed": failures})
    if saved:
        ctx.emit("evidence.saved", aggregate_type="vehicle_intake", aggregate_id=it.id,
                 payload={"intake_id": it.id, "asset_ids": [a["id"] for a in saved], "vehicle_id": it.vehicle_id, "context": "intake"})
    return {"saved": saved, "failed": failures, "links": linked, "skipped": skipped, "failed_uploads": it.failed_uploads}


@command("intake.add_assets", input=IntakeAssetsIn, perm="intake", action_class="internal",
         description="Attach finalized uploads (or upload ids) to the intake. Failed uploads are recorded per file and stay "
                     "retryable; successful photos are never dropped because another file failed.")
async def intake_add_assets(ctx: CommandContext, inp: IntakeAssetsIn) -> dict:
    it = await load_intake(ctx.db, ctx.actor, inp.intake_id, lock=True, expected_version=inp.expected_version)
    if it.status in CLOSED_STATUSES:
        raise Blocked(f"intake is {it.status}")
    out = await _add_assets(ctx, it, inp.asset_ids, inp.upload_ids, inp.failed, inp.clear_failed)
    out["intake"] = serialize_intake(it)
    return out


@command("intake.add_note", input=IntakeNoteIn, perm="intake", action_class="internal",
         description="Append verbatim owner text to the intake (kept as the original note beside the extracted summary).")
async def intake_add_note(ctx: CommandContext, inp: IntakeNoteIn) -> dict:
    it = await load_intake(ctx.db, ctx.actor, inp.intake_id, lock=True, expected_version=inp.expected_version)
    if it.status in CLOSED_STATUSES:
        raise Blocked(f"intake is {it.status}")
    text = inp.text.strip()
    if text and text not in (it.text_notes or ""):
        it.text_notes = ((it.text_notes + "\n") if it.text_notes else "") + text
        if it.analysis:
            it.analysis = {**(it.analysis or {}), "stale": True, "stale_reason": "notes added after analysis"}
        if it.status == "applied":
            it.status = "analyzed"
        ctx.touch(it, "vehicle_intake")
        ctx.record("Intake note added", entity_kind="vehicle_intake", entity_id=it.id, kind="intake", state=it.status,
                   details={"chars": len(text)})
    return {"intake": serialize_intake(it)}


@command("intake.add_transcript", input=IntakeTranscriptIn, perm="intake", action_class="internal",
         description="Add a voice note and/or its editable transcript. Without a transcript the audio is kept, the intake asks for "
                     "text (needs_info) and photos are never blocked.")
async def intake_add_transcript(ctx: CommandContext, inp: IntakeTranscriptIn) -> dict:
    it = await load_intake(ctx.db, ctx.actor, inp.intake_id, lock=True, expected_version=inp.expected_version)
    if it.status in CLOSED_STATUSES:
        raise Blocked(f"intake is {it.status}")
    if not inp.text and not inp.audio_asset_id:
        raise ValidationFailed("give transcript text and/or an audio asset")
    audio: Asset | None = None
    transcription_error = None
    text = (inp.text or "").strip()
    source = inp.source
    if inp.audio_asset_id:
        audio = await ctx.db.get(Asset, inp.audio_asset_id)
        if audio is None or audio.status != "ready":
            raise NotFound("audio asset not found or not ready")
        if audio.kind != "audio":
            raise ValidationFailed("audio_asset_id must be an audio asset")
        if not owns_asset(ctx.actor, audio) and not await can_access_asset(ctx.db, ctx.actor, audio):
            raise Denied("asset not accessible")
        await dispatch(ctx.child(), "assets.link", {"asset_id": audio.id, "entity_kind": "intake", "entity_id": it.id, "role": "voice"}, commit=False)
        if audio.id not in (it.transcript_asset_ids or []):
            it.transcript_asset_ids = [*(it.transcript_asset_ids or []), audio.id]
        if audio.id not in (it.asset_ids or []):
            it.asset_ids = [*(it.asset_ids or []), audio.id]
        if not text and audio.transcript:
            text, source = audio.transcript.strip(), "asset"
        if not text:
            from ..adapters.model import TranscriptionUnavailable, transcribe
            try:
                got = variant_bytes(audio, "original")
                text = (await transcribe(got[0], got[1])).strip() if got else ""
                source = "service"
            except TranscriptionUnavailable as e:
                transcription_error = str(e)
            except Exception as e:  # noqa: BLE001 - a transcription outage never blocks the photos
                transcription_error = f"{type(e).__name__}: {e}"
        if text:
            await dispatch(ctx.child(), "assets.set_transcript", {"asset_id": audio.id, "transcript": text, "source": source}, commit=False)
    missing = [m for m in (it.missing_fields or []) if m != "transcript"]
    if text:
        if text not in (it.transcript or ""):
            it.transcript = ((it.transcript + "\n") if it.transcript else "") + text
        if it.analysis:
            it.analysis = {**(it.analysis or {}), "stale": True, "stale_reason": "transcript added after analysis"}
        if "vehicle" not in missing and it.status in ("needs_info", "applied", "analyzed"):
            it.status = "analyzed" if it.analysis else "open"
    else:
        missing.append("transcript")
        it.status = "needs_info"
        it.last_error = transcription_error or "no transcript yet"
    it.missing_fields = missing
    ctx.touch(it, "vehicle_intake")
    ctx.record("Voice note " + ("transcribed" if text else "saved; transcript needed"), entity_kind="vehicle_intake", entity_id=it.id,
               kind="intake", state=it.status, exception=not text,
               details={"audio_asset_id": audio.id if audio else None, "source": source if text else None, "error": transcription_error})
    return {"intake": serialize_intake(it), "transcript": text, "transcription_error": transcription_error,
            "needs_transcript": not text, "audio_asset_id": audio.id if audio else None}


# ── analysis ─────────────────────────────────────────────────────────────────
async def _photo_assets(db: AsyncSession, it: VehicleIntake) -> list[Asset]:
    if not it.asset_ids:
        return []
    rows = (await db.execute(select(Asset).where(Asset.id.in_(list(it.asset_ids)), Asset.kind == "photo", Asset.status == "ready"))).scalars().all()
    by_id = {a.id: a for a in rows}
    return [by_id[a] for a in it.asset_ids if a in by_id]


def _combined_text(it: VehicleIntake) -> str:
    parts = [p for p in (it.text_notes or "", it.transcript or "") if p and p.strip()]
    return "\n".join(parts)


async def _analyze(ctx: CommandContext, it: VehicleIntake) -> dict:
    """Extract observations for a new revision, dedupe against earlier ones, resolve the target."""
    from ..adapters import model as model_adapter
    text = _combined_text(it)
    photos = await _photo_assets(ctx.db, it)
    it.revision = (it.revision or 0) + 1
    it.status = "analyzing"
    provenance: dict = {"path": None, "reason": None, "model": None, "images": len(photos), "text_chars": len(text), "at": ctx.now.isoformat()}
    extraction: ia.IntakeExtraction | None = None
    if not text and not photos:
        raise Blocked("nothing to analyze yet: add photos, a note or a voice transcript")
    if model_adapter.available():
        images: list[tuple[bytes, str, str]] = []
        for a in photos:
            got = variant_bytes(a, "web")
            if got is not None:
                images.append((got[0], got[1], a.id))
        try:
            extraction, path = await ia.extract_with_model(ctx.db, text=text, images=images, now=ctx.now, run_id=ctx.run_id, mission_id=ctx.mission_id)
            provenance.update({"path": path, "model": settings.AZKT_MODEL_LIGHT, "images_sent": len(images)})
        except model_adapter.ModelUnavailable as e:
            provenance.update({"path": "deterministic", "reason": f"model unavailable: {e}"})
        except model_adapter.ModelRefused as e:
            provenance.update({"path": "deterministic", "reason": f"model refused: {e}"})
        except Exception as e:  # noqa: BLE001 - malformed model output is data; deterministic path continues
            provenance.update({"path": "deterministic", "reason": f"model output unusable: {type(e).__name__}: {e}"[:300]})
    else:
        provenance.update({"path": "deterministic", "reason": "no model API key configured"})
    if extraction is None:
        extraction = ia.deterministic_extract(text, now=ctx.now)
        if photos:
            provenance["images_note"] = f"{len(photos)} photo(s) saved and linked; not analyzed by a model (no image observations invented)"
    image_asset_ids = [a.id for a in photos]
    existing = {o.dedupe_key: o for o in await observations_of(ctx.db, it.id) if o.dedupe_key}
    seen: set[str] = set()
    new_ids: list[str] = []

    def _asset_for(idx: int | None) -> str | None:
        if idx is None or not (0 <= idx < len(image_asset_ids)):
            return None
        return image_asset_ids[idx]

    def upsert(kind: str, text_: str, *, source: str, confidence: str = "stated", field: str | None = None, value: str | None = None,
               asset_id: str | None = None, meta: dict | None = None) -> IntakeObservation:
        key = obs_key(it.id, kind, text_, field, value)
        seen.add(key)
        o = existing.get(key)
        if o is not None:
            m = dict(o.meta or {})
            revs = list(m.get("revisions") or [])
            if it.revision not in revs:
                revs.append(it.revision)
            m["revisions"] = revs
            if meta:
                m.update({k: v for k, v in meta.items() if v is not None})
            o.meta = m
            if asset_id and not o.asset_id:
                o.asset_id = asset_id
            return o
        o = IntakeObservation(intake_id=it.id, revision=it.revision, kind=kind, text=text_, source=source, asset_id=asset_id,
                              confidence=confidence, field=field, value=value, status="pending", dedupe_key=key,
                              meta={**(meta or {}), "revisions": [it.revision]}, history=[], created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
        ctx.db.add(o)
        existing[key] = o
        new_ids.append(key)
        return o

    voice = bool(it.transcript) and not it.text_notes
    cond_rows: list[IntakeObservation] = []
    for idx, c in enumerate(extraction.conditions):
        src = "image" if c.source == "image_observed" else ("proposed_check" if c.source == "proposed_check" else ("owner_voice" if voice else "owner_text"))
        conf = "observed" if c.source == "image_observed" else ("uncertain" if c.source == "proposed_check" else "stated")
        cond_rows.append(upsert("condition", c.text, source=src, confidence=conf, asset_id=_asset_for(c.image_index),
                                meta={"is_issue": c.is_issue, "panel": c.panel, "image_index": c.image_index, "condition_index": idx,
                                      "condition_source": c.source}))
    await ctx.db.flush()
    for r in extraction.requests:
        cond = cond_rows[r.condition_index] if r.condition_index is not None and 0 <= r.condition_index < len(cond_rows) else None
        src = ("image" if cond is not None and cond.source == "image" else ("owner_voice" if voice else "owner_text"))
        upsert("request", r.title, source=src, confidence="observed" if src == "image" else "stated",
               asset_id=_asset_for(r.image_index) or (cond.asset_id if cond is not None else None),
               meta={"detail": r.detail, "condition_observation_id": cond.id if cond is not None else None, "assignee": r.assignee,
                     "priority": r.priority, "condition_source": (cond.meta or {}).get("condition_source") if cond is not None else "owner_reported"})
    for i in extraction.identifiers:
        upsert("identifier", f"{i.field}: {i.value}", source="image" if i.image_index is not None else ("owner_voice" if voice else "owner_text"),
               confidence=i.confidence, field=i.field, value=i.value, asset_id=_asset_for(i.image_index), meta={"note": i.note})
    for m in extraction.milestones:
        upsert("milestone", m.stated_text or m.kind, source="owner_voice" if voice else "owner_text", confidence="stated" if m.date else "uncertain",
               field=m.kind, value=m.date, meta={"kind": m.kind, "date": m.date})
    if extraction.assignee:
        upsert("assignee", extraction.assignee, source="owner_voice" if voice else "owner_text", field="assignee", value=extraction.assignee)
    if extraction.priority:
        upsert("priority", extraction.priority, source="owner_voice" if voice else "owner_text", field="priority", value=extraction.priority)
    await ctx.db.flush()
    superseded = 0
    for key, o in existing.items():
        if key not in seen and o.status == "pending" and not o.removed:
            o.status = "skipped"
            o.error = f"not present in revision {it.revision}"
            superseded += 1
    it.analysis = {"revision": it.revision, "provenance": provenance, "extraction": extraction.model_dump(mode="json"),
                   "observations_new": len(new_ids), "observations_superseded": superseded, "stale": False,
                   "assignee": extraction.assignee, "priority": extraction.priority}
    # target resolution (spec §7.4 step 4)
    it.candidate_vehicle_ids = []
    it.choice = {}
    it.status = "analyzed"
    await _resolve_target(ctx, it, extraction)
    missing = [m for m in (it.missing_fields or []) if m != "transcript"]
    if it.transcript_asset_ids and not it.transcript:
        missing.append("transcript")
    it.missing_fields = missing
    ctx.record(f"Intake analyzed (revision {it.revision}, {provenance['path']})", entity_kind="vehicle_intake", entity_id=it.id, kind="intake",
               state=it.status, details={"provenance": provenance, "conditions": len(extraction.conditions), "requests": len(extraction.requests),
                                         "identifiers": len(extraction.identifiers), "milestones": len(extraction.milestones)})
    ctx.emit("intake.observation_saved", aggregate_type="vehicle_intake", aggregate_id=it.id, aggregate_version=it.version,
             payload={"intake_id": it.id, "revision": it.revision, "new": len(new_ids), "path": provenance["path"], "status": it.status})
    return it.analysis


async def _resolve_target(ctx: CommandContext, it: VehicleIntake, x: ia.IntakeExtraction) -> None:
    """Explicit vehicle wins; find mode uses the matching service (lazy import) or exact stock/frame lookups.
    Several candidates -> needs_choice with identifying context. Similarity never auto-selects."""
    stated = {i.field: i.value for i in x.identifiers if i.confidence == "stated"}
    if it.vehicle_id:
        return
    if it.target_mode == "new":
        dup = await _exact_duplicate(ctx.db, stated.get("frame_no"), stated.get("stock_no"))
        if dup is not None:
            it.status = "needs_choice"
            it.candidate_vehicle_ids = [dup.id]
            it.choice = {"reason": "an existing card already has this identity; update it or confirm a new vehicle",
                         "candidates": [_candidate_context(dup, ["exact identity match"], 1.0)], "allow_new": True}
        return
    # find mode
    year = None
    try:
        year = int(stated["model_year"]) if stated.get("model_year") else None
    except ValueError:
        year = None
    result = None
    try:
        from .matching import resolve_vehicle
        result = await resolve_vehicle(ctx.db, stock_no=stated.get("stock_no"), frame_no=stated.get("frame_no"), text=_combined_text(it),
                                       model=stated.get("model"), model_year=year, color=stated.get("color"))
    except ImportError:
        result = None
    except Exception as e:  # noqa: BLE001 - matching service errors fall back to exact lookups
        log.warning("matching.resolve_vehicle failed: %s", e)
        result = None
    if result is not None:
        cands = []
        for c in result.candidates[:6]:
            v = await ctx.db.get(Vehicle, c.get("vehicle_id"))
            if v is not None and not v.archived_at:
                cands.append(_candidate_context(v, c.get("reasons"), c.get("score")))
        if result.state == "matched" and result.vehicle_id:
            it.vehicle_id = result.vehicle_id
            it.choice = {"resolved": "matched", "reasons": result.reasons}
            return
        it.candidate_vehicle_ids = [c["vehicle_id"] for c in cands]
        if cands:
            it.status = "needs_choice"
            it.choice = {"reason": "several vehicles could match; choose one before anything is written" if result.state == "ambiguous"
                         else "similarity only; confirm the vehicle before anything is written", "state": result.state,
                         "candidates": cands, "allow_new": True, "reasons": result.reasons}
        else:
            it.status = "needs_info"
            it.missing_fields = list(dict.fromkeys([*(it.missing_fields or []), "vehicle"]))
            it.choice = {"reason": "no vehicle matched the evidence; pick the vehicle or start a new card", "state": result.state,
                         "candidates": [], "allow_new": True, "reasons": result.reasons}
        return
    # fallback: exact stock_no / frame_no_norm lookup only
    exact = await _exact_duplicate(ctx.db, stated.get("frame_no"), stated.get("stock_no"))
    if exact is not None:
        it.vehicle_id = exact.id
        it.choice = {"resolved": "exact", "reasons": ["exact stock/frame reference"]}
        return
    similar = await _similar(ctx.db, stated.get("model"), year, stated.get("color"))
    it.candidate_vehicle_ids = [v.id for v in similar]
    if similar:
        it.status = "needs_choice"
        it.choice = {"reason": "similar vehicles found; visual/model similarity never selects a vehicle by itself",
                     "candidates": [_candidate_context(v, ["similar model/year/color"]) for v in similar], "allow_new": True}
    else:
        it.status = "needs_info"
        it.missing_fields = list(dict.fromkeys([*(it.missing_fields or []), "vehicle"]))
        it.choice = {"reason": "no vehicle matched the evidence; pick the vehicle or start a new card", "candidates": [], "allow_new": True}


async def _exact_duplicate(db: AsyncSession, frame_no: str | None, stock_no: str | None) -> Vehicle | None:
    fn = normalize_frame(frame_no)
    if fn:
        v = (await db.execute(select(Vehicle).where(Vehicle.frame_no_norm == fn, Vehicle.archived_at.is_(None)))).scalars().first()
        if v is not None:
            return v
    sn = normalize_stock_no(stock_no)
    if sn:
        v = (await db.execute(select(Vehicle).where(Vehicle.stock_no == sn, Vehicle.archived_at.is_(None)))).scalars().first()
        if v is not None:
            return v
    return None


async def _similar(db: AsyncSession, model: str | None, year: int | None, color: str | None) -> list[Vehicle]:
    if not (model or year or color):
        return []
    q = select(Vehicle).where(Vehicle.archived_at.is_(None))
    if model:
        q = q.where(func.lower(Vehicle.model) == model.lower())
    if year:
        q = q.where(Vehicle.model_year == year)
    if color:
        q = q.where(func.lower(Vehicle.color) == color.lower())
    return list((await db.execute(q.order_by(Vehicle.created_at).limit(6))).scalars().all())


@command("intake.analyze", input=IntakeRefIn, perm="intake", action_class="internal",
         description="Extract condition / requests / identifiers / milestones from notes, transcript and photos (model with vision, "
                     "or the deterministic fallback) and resolve the target vehicle. Records which path was used.")
async def intake_analyze(ctx: CommandContext, inp: IntakeRefIn) -> dict:
    it = await load_intake(ctx.db, ctx.actor, inp.intake_id, lock=True, expected_version=inp.expected_version)
    if it.status in CLOSED_STATUSES:
        raise Blocked(f"intake is {it.status}")
    analysis = await _analyze(ctx, it)
    ctx.touch(it, "vehicle_intake")
    return {"intake": serialize_intake(it), "analysis": analysis, "observations": [serialize_observation(o) for o in await observations_of(ctx.db, it.id)]}


@command("intake.choose_vehicle", input=IntakeChooseIn, perm="intake", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)] if p.vehicle_id else [],
         description="Resolve an ambiguous target explicitly: pick one of the candidates (or any permitted vehicle) or confirm a new card.")
async def intake_choose_vehicle(ctx: CommandContext, inp: IntakeChooseIn) -> dict:
    it = await load_intake(ctx.db, ctx.actor, inp.intake_id, lock=True, expected_version=inp.expected_version)
    if it.status in CLOSED_STATUSES:
        raise Blocked(f"intake is {it.status}")
    if inp.create_new and inp.vehicle_id:
        raise ValidationFailed("choose a vehicle or create_new, not both")
    if not inp.create_new and not inp.vehicle_id:
        raise ValidationFailed("give vehicle_id or create_new")
    before = {"vehicle_id": it.vehicle_id, "target_mode": it.target_mode, "status": it.status}
    if inp.create_new:
        it.target_mode = "new"
        it.vehicle_id = None
    else:
        v = await get_vehicle(ctx.db, inp.vehicle_id, lock=False)
        if v.archived_at:
            raise Blocked("vehicle is archived; restore it first")
        it.vehicle_id = v.id
        it.target_mode = "existing"
    it.candidate_vehicle_ids = []
    it.choice = {"resolved": "chosen" if inp.vehicle_id else "new", "by": ctx.actor.user_id, "at": ctx.now.isoformat()}
    it.missing_fields = [m for m in (it.missing_fields or []) if m != "vehicle"]
    if it.status in ("needs_choice", "needs_info") and not ("transcript" in it.missing_fields):
        it.status = "analyzed" if it.analysis else "open"
    ctx.touch(it, "vehicle_intake")
    ctx.record("Intake target chosen: " + (f"vehicle {it.vehicle_id}" if it.vehicle_id else "new vehicle"), entity_kind="vehicle_intake",
               entity_id=it.id, kind="intake", state=it.status, details={"before": before, "vehicle_id": it.vehicle_id})
    return {"intake": serialize_intake(it)}


# ── apply ────────────────────────────────────────────────────────────────────
def _local_day_start(d: str) -> datetime:
    y, m, dd = (int(p) for p in d.split("-"))
    return datetime.combine(date(y, m, dd), time.min, tzinfo=ZoneInfo(settings.DEFAULT_TIMEZONE)).astimezone(timezone.utc)


async def _apply_observation(ctx: CommandContext, it: VehicleIntake, v: Vehicle, o: IntakeObservation, *, by_id: dict[str, IntakeObservation],
                             default_assignee: str | None, default_priority: str | None, result: dict, applied: dict, can_write_vehicle: bool) -> None:
    """One observation -> its command. Runs inside a savepoint so a failing observation is recorded, not fatal."""
    kind = o.kind
    if kind == "condition":
        src = CONDITION_SOURCE_FOR.get(o.source, "owner_reported")
        if o.source == "proposed_check":
            src = "proposed_check"
        res = await dispatch(ctx.child(), "vehicles.set_condition", {
            "vehicle_id": v.id, "mode": "append", "source_ref": f"intake:{it.id}",
            "bullets": [{"text": o.text, "source": src, "evidence": [o.asset_id] if o.asset_id else [], "observation_id": o.id}]}, commit=False)
        data = res.data or {}
        bid = None
        for b in data.get("bullets") or []:
            if o.id in (b.get("observation_ids") or []):
                bid = b["id"]
                break
        o.applied_kind, o.applied_id, o.applied_command = "condition_bullet", bid, "vehicles.set_condition"
        o.applied_version = data.get("condition_version")
        if data.get("added"):
            applied.setdefault("bullet_ids", []).extend(data["added"])
        elif data.get("updated"):
            applied.setdefault("bullets_updated", []).extend(data["updated"])
        result["condition_bullets"].append({"id": bid, "text": o.text, "source": src, "evidence": [o.asset_id] if o.asset_id else [],
                                            "observation_id": o.id, "new": bool(data.get("added"))})
    elif kind == "request":
        meta = o.meta or {}
        cond = by_id.get(meta.get("condition_observation_id") or "")
        issue_title = cond.text if cond is not None else o.text
        src = ISSUE_SOURCE_FOR.get(meta.get("condition_source") or "owner_reported", "owner_reported")
        if o.source == "image":
            src = "image_observed"
        assignee = None
        if meta.get("assignee"):
            u = await _resolve_user(ctx.db, meta.get("assignee"))
            assignee = u.id if u is not None else None
        assignee = assignee or default_assignee
        priority = meta.get("priority") or default_priority or "normal"
        notes = meta.get("detail") or ""
        if meta.get("assignee") and not assignee:
            notes = (notes + "\n" if notes else "") + f"Assignee stated: {meta['assignee']} (no matching person; left unassigned)"
        evidence = [o.asset_id] if o.asset_id else ([cond.asset_id] if cond is not None and cond.asset_id else [])
        res = await dispatch(ctx.child(), "shop.create_issue", {
            "vehicle_id": v.id, "title": issue_title[:200], "detail": notes if cond is None else (cond.text if cond.text != issue_title else ""),
            "severity": "normal", "source_kind": src, "source_ref": f"intake:{it.id}", "intake_observation_id": cond.id if cond is not None else o.id,
            "asset_ids": evidence, "create_task": True, "task_title": o.text[:200], "assignee_user_id": assignee, "priority": priority,
            "task_source_kind": "intake", "task_source_id": o.id}, commit=False)
        data = res.data or {}
        issue = data.get("issue") or {}
        task = (data.get("task") or {}).get("task") or {}
        o.applied_kind, o.applied_id, o.applied_command = "task", task.get("id"), "shop.create_issue"
        o.applied_version = task.get("version")
        o.meta = {**meta, "issue_id": issue.get("id"), "task_id": task.get("id"), "assignee_user_id": task.get("owner_user_id")}
        if data.get("created"):
            applied.setdefault("issue_ids", []).append(issue.get("id"))
            result["issues_created"].append(issue.get("id"))
        if (data.get("task") or {}).get("created"):
            applied.setdefault("task_ids", []).append(task.get("id"))
            result["tasks_created"].append({"id": task.get("id"), "title": task.get("title"), "owner_user_id": task.get("owner_user_id"),
                                            "issue_id": issue.get("id"), "observation_id": o.id})
        elif task.get("id"):
            applied.setdefault("tasks_merged", []).append(task.get("id"))
            result["tasks_updated"].append({"id": task.get("id"), "title": task.get("title"), "merged": True, "observation_id": o.id})
    elif kind == "identifier":
        field, value = o.field, o.value
        if o.confidence == "uncertain" or field == "frame_no" and o.confidence != "stated":
            o.status = "needs_confirmation"
            title = CONFIRM_TASK_TITLES.get(field, f"Confirm {field}")
            res = await dispatch(ctx.child(), "tasks.create", {
                "title": title, "type": "operational", "vehicle_id": v.id, "owner_user_id": default_assignee,
                "notes": f"Read at intake as {value!r} ({o.confidence}); not written to the card." + (f" {o.meta.get('note')}" if (o.meta or {}).get("note") else ""),
                "source_kind": "intake", "source_id": o.id, "dedupe": True, "extra": {"intake": True, "confirm_field": field, "candidate": value}}, commit=False)
            t = ((res.data or {}).get("task") or {})
            o.applied_kind, o.applied_id, o.applied_command = "task", t.get("id"), "tasks.create"
            if (res.data or {}).get("created"):
                applied.setdefault("task_ids", []).append(t.get("id"))
                result["tasks_created"].append({"id": t.get("id"), "title": t.get("title"), "observation_id": o.id, "confirm": field})
            result["needs_confirmation"].append({"observation_id": o.id, "field": field, "value": value, "task_id": t.get("id"),
                                                 "reason": "uncertain at intake; confirm before it is written"})
            return
        if field in ("frame_no", "stock_no") and applied.get("created") and _column_matches(v, field, value):
            o.applied_kind, o.applied_id, o.applied_command = "fact", v.id, "vehicles.create"
            o.applied_version = v.version
            return
        if field == "stock_no":
            if v.stock_no and normalize_stock_no(value) != v.stock_no:
                o.status = "needs_confirmation"
                result["needs_confirmation"].append({"observation_id": o.id, "field": field, "value": value,
                                                     "reason": f"card already has stock number {v.stock_no}; not renamed"})
                return
            o.status = "applied"
            return
        if not can_write_vehicle:
            o.status = "skipped"
            o.error = "recording identity facts needs vehicles.write; left for the owner/manager"
            result["needs_confirmation"].append({"observation_id": o.id, "field": field, "value": value, "reason": o.error})
            return
        res = await dispatch(ctx.child(), "vehicles.propose_fact", {
            "vehicle_id": v.id, "key": field, "value": value, "status": "reported", "source_kind": "intake", "source_ref": f"intake:{it.id}:{o.id}",
            "confidence_method": f"intake {o.source} ({o.confidence})"}, commit=False)
        data = res.data or {}
        fact = data.get("fact") or {}
        o.applied_kind, o.applied_id, o.applied_command = "fact", fact.get("id"), "vehicles.propose_fact"
        o.applied_version = fact.get("version")
        applied.setdefault("fact_ids", []).append(fact.get("id"))
        result["facts"].append({"id": fact.get("id"), "key": field, "value": value, "outcome": data.get("outcome"), "status": fact.get("status")})
        if data.get("conflict"):
            o.status = "needs_confirmation"
            result["needs_confirmation"].append({"observation_id": o.id, "field": field, "value": value, "fact_id": fact.get("id"),
                                                 "reason": "conflicts with the value already on the card; owner confirmation needed"})
            return
        if field in CRITICAL_FACT_KEYS:
            result["needs_confirmation"].append({"observation_id": o.id, "field": field, "value": value, "fact_id": fact.get("id"),
                                                 "reason": "critical fact recorded as reported; owner confirmation needed"})
    elif kind == "milestone":
        meta = o.meta or {}
        mkind, d = meta.get("kind") or o.field, meta.get("date") or o.value
        if mkind != "received":
            o.status = "skipped"
            o.error = f"milestone {mkind} is only recorded from sourced evidence, not from intake notes"
            return
        if not d:
            o.status = "needs_confirmation"
            res = await dispatch(ctx.child(), "tasks.create", {
                "title": CONFIRM_TASK_TITLES["received_date"], "type": "operational", "vehicle_id": v.id, "owner_user_id": default_assignee,
                "notes": f"Arrival was reported (\"{o.text}\") without a date.", "source_kind": "intake", "source_id": o.id, "dedupe": True,
                "extra": {"intake": True, "confirm_field": "received_date"}}, commit=False)
            t = ((res.data or {}).get("task") or {})
            o.applied_kind, o.applied_id, o.applied_command = "task", t.get("id"), "tasks.create"
            if (res.data or {}).get("created"):
                applied.setdefault("task_ids", []).append(t.get("id"))
                result["tasks_created"].append({"id": t.get("id"), "title": t.get("title"), "observation_id": o.id, "confirm": "received_date"})
            result["needs_confirmation"].append({"observation_id": o.id, "field": "received_date", "value": None, "task_id": t.get("id"),
                                                 "reason": "arrival reported without a date; not recorded"})
            return
        today = {ctx.now.date().isoformat(), ctx.now.astimezone(ZoneInfo(settings.DEFAULT_TIMEZONE)).date().isoformat()}
        at = ctx.now if d in today else _local_day_start(d)
        if not can_write_vehicle:
            o.status = "skipped"
            o.error = "recording a milestone needs vehicles.write"
            return
        res = await dispatch(ctx.child(), "vehicles.record_milestone", {
            "vehicle_id": v.id, "kind": "received", "status": "completed", "at": at.isoformat(), "source_kind": "owner_reported",
            "source_ref": f"intake:{it.id}:{o.id}", "note": o.text + ("" if at == ctx.now else " (date only; time not stated)")}, commit=False)
        m = (res.data or {}).get("milestone") or {}
        o.applied_kind, o.applied_id, o.applied_command = "milestone", m.get("id"), "vehicles.record_milestone"
        o.applied_version = m.get("version")
        if (res.data or {}).get("created"):
            applied.setdefault("milestone_ids", []).append(m.get("id"))
        result["milestones"].append({"id": m.get("id"), "kind": "received", "at": m.get("at"), "source_kind": "owner_reported", "observation_id": o.id})
    else:  # assignee / priority / note: context only
        o.status = "applied"
        o.applied_kind = "context"
        return


def _column_matches(v: Vehicle, field: str, value: str | None) -> bool:
    if field == "frame_no":
        return bool(v.frame_no_norm) and v.frame_no_norm == normalize_frame(value)
    if field == "stock_no":
        return bool(v.stock_no) and v.stock_no == normalize_stock_no(value)
    return False


@command("intake.apply", input=IntakeApplyIn, perm="intake", action_class="internal",
         description="Apply the intake: create or update the card, save sourced condition bullets, create/link recon issues and "
                     "verb-first tasks, link photos and sourced milestones. Transactional and retry-safe (dedupe everywhere); "
                     "uncertain identifiers become confirmation tasks and are never written to the card.")
async def intake_apply(ctx: CommandContext, inp: IntakeApplyIn) -> dict:
    it = await load_intake(ctx.db, ctx.actor, inp.intake_id, lock=True, expected_version=inp.expected_version)
    if it.status in CLOSED_STATUSES:
        raise Blocked(f"intake is {it.status}")
    if inp.analyze and (not it.analysis or (it.analysis or {}).get("stale")):
        await _analyze(ctx, it)
    if it.status == "needs_choice":
        ctx.touch(it, "vehicle_intake")
        return {"decision": "Blocked", "status": it.status, "intake": serialize_intake(it), "choice": it.choice,
                "reasons": [it.choice.get("reason") or "choose the vehicle before anything is written"]}
    if it.status == "needs_info" and "vehicle" in (it.missing_fields or []) and not it.vehicle_id and it.target_mode != "new":
        ctx.touch(it, "vehicle_intake")
        return {"decision": "Blocked", "status": it.status, "intake": serialize_intake(it), "choice": it.choice,
                "reasons": ["no vehicle identified; pick the vehicle or confirm a new card"]}
    obs = [o for o in await observations_of(ctx.db, it.id) if not o.removed and o.status not in ("skipped", "rejected")]
    by_id = {o.id: o for o in obs}
    x = ia.IntakeExtraction.model_validate((it.analysis or {}).get("extraction") or {})
    stated = {i.field: i.value for i in x.identifiers if i.confidence == "stated"}
    default_assignee = inp.assignee_user_id
    if not default_assignee and (it.analysis or {}).get("assignee"):
        u = await _resolve_user(ctx.db, it.analysis.get("assignee"))
        default_assignee = u.id if u is not None else None
    default_priority = inp.priority or (it.analysis or {}).get("priority")
    can_write_vehicle = has_perm(ctx.actor, "vehicles.write")
    applied: dict = dict(it.applied or {}) if it.applied and it.undone_at is None and it.applied.get("vehicle_id") else {}
    result: dict = {"vehicle_id": None, "created": False, "condition_bullets": [], "tasks_created": [], "tasks_updated": [], "issues_created": [],
                    "photos_saved": 0, "photos_failed": [], "missing": [], "needs_confirmation": [], "facts": [], "milestones": [],
                    "failed_observations": [], "revision": it.revision}
    # 1. target card
    created = False
    if it.vehicle_id:
        await _vehicle_allowed(ctx.db, ctx.actor, it.vehicle_id)
        v = await get_vehicle(ctx.db, it.vehicle_id)
        if v.archived_at:
            raise Blocked("target vehicle is archived; restore it or choose another")
    else:
        if it.target_mode != "new":
            raise Blocked("no vehicle identified; choose the vehicle first")
        if not can_write_vehicle:
            raise Denied("creating a vehicle card needs vehicles.write")
        payload = {"source_kind": "intake", "source_ref": it.id, "create_key": f"intake:{it.id}", "logistics_state": "received",
                   "notes": ""}
        for k in ("make", "model", "color"):
            if stated.get(k):
                payload[k] = stated[k]
        if stated.get("model_year"):
            try:
                payload["model_year"] = int(stated["model_year"])
            except ValueError:
                pass
        if stated.get("frame_no"):
            payload["frame_no_raw"] = stated["frame_no"]
        if stated.get("stock_no"):
            payload["stock_no"] = stated["stock_no"]
        try:
            res = await dispatch(ctx.child(), "vehicles.create", payload, commit=False)
        except Conflict as e:
            dup_id = (e.detail or {}).get("vehicle_id")
            it.status = "needs_choice"
            it.candidate_vehicle_ids = [dup_id] if dup_id else []
            dup = await ctx.db.get(Vehicle, dup_id) if dup_id else None
            it.choice = {"reason": f"{e.message}; update that card or change the identity", "candidates": [_candidate_context(dup, ["exact identity match"], 1.0)] if dup else [],
                         "allow_new": False}
            ctx.touch(it, "vehicle_intake")
            return {"decision": "Blocked", "status": it.status, "intake": serialize_intake(it), "choice": it.choice, "reasons": [e.message]}
        data = res.data or {}
        v = await get_vehicle(ctx.db, data["vehicle"]["id"])
        created = bool(data.get("created"))
        it.vehicle_id = v.id
        it.target_mode = "existing" if not created else it.target_mode
        if created:
            applied["created"] = True
            mt = data.get("missing_identity_task") or {}
            if mt.get("id"):
                applied.setdefault("task_ids", []).append(mt["id"])
                result["tasks_created"].append({"id": mt["id"], "title": mt.get("title"), "missing_identity": True})
    result["vehicle_id"] = v.id
    result["created"] = created
    result["card_created_by_intake"] = created or bool(applied.get("created"))
    applied["vehicle_id"] = v.id
    # 2. observations, each in its own savepoint
    order = {"condition": 0, "request": 1, "identifier": 2, "milestone": 3, "assignee": 4, "priority": 4, "note": 5}
    for o in sorted(obs, key=lambda r: (order.get(r.kind, 9), r.created_at or ctx.now, r.id)):
        oid, okind, otext = o.id, o.kind, o.text
        await ctx.db.flush()
        sp = await ctx.db.begin_nested()
        try:
            await _apply_observation(ctx, it, v, o, by_id=by_id, default_assignee=default_assignee, default_priority=default_priority,
                                     result=result, applied=applied, can_write_vehicle=can_write_vehicle)
            if o.status == "pending":
                o.status = "applied"
            o.error = None
            await sp.commit()
        except DomainError as e:
            await sp.rollback()
            for row in (o, v, it):
                await ctx.db.refresh(row)
            o.status = "failed"
            o.error = f"{e.code}: {e.message}"
            result["failed_observations"].append({"observation_id": oid, "kind": okind, "text": otext, "error": o.error})
    # 3. photos / voice notes -> vehicle
    photos_saved = 0
    for aid in list(it.asset_ids or []):
        a = await ctx.db.get(Asset, aid)
        if a is None or a.status != "ready":
            continue
        role = "voice" if a.kind == "audio" else ("document" if a.kind == "document" or a.sensitive else "photo")
        a_name = a.original_name
        await ctx.db.flush()
        sp = await ctx.db.begin_nested()
        try:
            res = await dispatch(ctx.child(), "assets.link", {"asset_id": a.id, "entity_kind": "vehicle", "entity_id": v.id, "role": role}, commit=False)
            await sp.commit()
        except DomainError as e:
            await sp.rollback()
            for row in (a, v, it):
                await ctx.db.refresh(row)
            result["photos_failed"].append({"asset_id": aid, "name": a_name, "error": f"{e.code}: {e.message}"})
            continue
        link = (res.data or {}).get("link") or {}
        if (res.data or {}).get("created") and link.get("id"):
            applied.setdefault("link_ids", []).append(link["id"])
        if a.kind == "photo":
            photos_saved += 1
    result["photos_saved"] = photos_saved
    result["photos_failed"].extend({"upload_id": f.get("upload_id"), "name": f.get("name"), "error": f.get("error"), "retryable": True}
                                   for f in (it.failed_uploads or []))
    # 4. summary
    await ctx.db.refresh(v)
    result["missing"] = list(v.missing_identity_fields or [])
    if it.transcript_asset_ids and not it.transcript:
        result["missing"].append("transcript")
    result["stock_no"], result["title"], result["intake_status"] = v.stock_no, vehicle_title(v), v.intake_status
    result["condition_version"] = v.condition_version
    result["card_path"] = f"/vehicles/{v.id}"
    partial = bool(result["photos_failed"] or result["failed_observations"])
    it.status = "partially_applied" if partial else ("needs_info" if "transcript" in result["missing"] else "applied")
    it.applied_at = ctx.now
    it.undone_at = None
    applied.update({"revision": it.revision, "at": ctx.now.isoformat(), "by": ctx.actor.user_id, "command": "intake.apply"})
    it.applied = applied
    it.result = result
    it.last_error = None if not partial else "; ".join([f.get("error") or "" for f in result["photos_failed"]] +
                                                        [f["error"] for f in result["failed_observations"]])[:1000]
    ctx.touch(it, "vehicle_intake")
    ctx.record(f"Intake {'applied' if not partial else 'partially applied'} to {v.stock_no}: {len(result['condition_bullets'])} bullets, "
               f"{len(result['tasks_created'])} tasks created, {len(result['tasks_updated'])} updated, {photos_saved} photos",
               entity_kind="vehicle", entity_id=v.id, kind="intake", state=it.status, exception=partial,
               details={"intake_id": it.id, "revision": it.revision, "created": result["created"], "failed": result["photos_failed"] + result["failed_observations"]})
    ctx.emit("intake.applied", aggregate_type="vehicle_intake", aggregate_id=it.id, aggregate_version=it.version,
             payload={"intake_id": it.id, "vehicle_id": v.id, "created": result["created"], "status": it.status, "revision": it.revision,
                      "tasks_created": [t.get("id") for t in result["tasks_created"]], "photos_saved": photos_saved})
    ctx.emit("metrics.invalidated", aggregate_type="vehicle", aggregate_id=v.id, payload={"reason": "intake.applied"})
    from .vehicles import recompute
    await recompute(ctx.db, v, ctx.now)
    return {"decision": "Allowed", "status": it.status, "result": result, "intake": serialize_intake(it), "vehicle": serialize_vehicle(v)}


# ── corrections / undo / abandon ─────────────────────────────────────────────
@command("intake.correct", input=IntakeCorrectIn, perm="intake", action_class="internal",
         description="Correct an applied intake: edit or remove a condition bullet / observation, or change a task's assignee. "
                     "The current summary is updated; prior versions and evidence are kept.")
async def intake_correct(ctx: CommandContext, inp: IntakeCorrectIn) -> dict:
    it = await load_intake(ctx.db, ctx.actor, inp.intake_id, lock=True, expected_version=inp.expected_version)
    if it.status in CLOSED_STATUSES:
        raise Blocked(f"intake is {it.status}")
    if not (inp.observation_id or inp.bullet_id or inp.task_id):
        raise ValidationFailed("give observation_id, bullet_id or task_id")
    change: dict = {"at": ctx.now.isoformat(), "by": ctx.actor.user_id, "reason": inp.reason, "results": []}
    o: IntakeObservation | None = None
    if inp.observation_id:
        o = await ctx.db.get(IntakeObservation, inp.observation_id)
        if o is None or o.intake_id != it.id:
            raise NotFound("observation not found")
    bullet_id = inp.bullet_id or (o.applied_id if o is not None and o.applied_kind == "condition_bullet" else None)
    task_id = inp.task_id or (o.applied_id if o is not None and o.applied_kind == "task" else None)
    if it.vehicle_id:
        await _vehicle_allowed(ctx.db, ctx.actor, it.vehicle_id)
    if o is not None:
        hist = list(o.history or [])
        if inp.text is not None and inp.text.strip() and inp.text.strip() != o.text:
            hist.append({"at": ctx.now.isoformat(), "by": ctx.actor.user_id, "text_before": o.text, "change": "text", "reason": inp.reason})
            change["observation_text"] = {"from": o.text, "to": inp.text.strip()}
            o.text = inp.text.strip()
        if inp.remove and not o.removed:
            hist.append({"at": ctx.now.isoformat(), "by": ctx.actor.user_id, "change": "removed", "reason": inp.reason})
            o.removed = True
            change["observation_removed"] = True
        o.history = hist
        o.bump(ctx.actor.user_id)
        ctx.changed.append({"kind": "intake_observation", "id": o.id, "version": o.version})
        change["observation_id"] = o.id
    if bullet_id and it.vehicle_id and (inp.text or inp.remove):
        res = await dispatch(ctx.child(), "vehicles.edit_condition_bullet", {
            "vehicle_id": it.vehicle_id, "bullet_id": bullet_id, "text": inp.text, "remove": inp.remove,
            "reason": inp.reason or "intake correction"}, commit=False)
        change["results"].append({"command": "vehicles.edit_condition_bullet", "bullet_id": bullet_id, "changed": (res.data or {}).get("changed")})
    if task_id:
        t = await ctx.db.get(Task, task_id)
        if t is None:
            raise NotFound("task not found")
        if inp.text and (o is None or o.kind == "request"):
            res = await dispatch(ctx.child(), "tasks.update", {"task_id": t.id, "title": inp.text.strip()}, commit=False)
            change["results"].append({"command": "tasks.update", "task_id": t.id})
        if inp.remove and t.status not in ("completed", "cancelled"):
            await dispatch(ctx.child(), "tasks.cancel", {"task_id": t.id, "reason": inp.reason or "removed at intake correction"}, commit=False)
            change["results"].append({"command": "tasks.cancel", "task_id": t.id})
            issue_id = (o.meta or {}).get("issue_id") if o is not None else None
            if issue_id and it.vehicle_id:
                await dispatch(ctx.child(), "shop.update_issue", {"issue_id": issue_id, "vehicle_id": it.vehicle_id, "status": "rejected",
                                                                  "reason": inp.reason or "removed at intake correction"}, commit=False)
                change["results"].append({"command": "shop.update_issue", "issue_id": issue_id, "status": "rejected"})
        if inp.assignee_user_id or inp.unassign:
            res = await dispatch(ctx.child(), "tasks.assign", {"task_id": t.id, "owner_user_id": None if inp.unassign else inp.assignee_user_id}, commit=False)
            change["results"].append({"command": "tasks.assign", "task_id": t.id, "owner_user_id": None if inp.unassign else inp.assignee_user_id})
            if o is not None:
                o.meta = {**(o.meta or {}), "assignee_user_id": None if inp.unassign else inp.assignee_user_id}
        change["task_id"] = t.id
    if o is not None and inp.remove and o.applied_kind == "condition_bullet" and it.vehicle_id and not bullet_id:
        pass
    it.corrections = [*(it.corrections or []), change]
    # refresh the current summary kept on the intake
    if it.vehicle_id:
        v = await get_vehicle(ctx.db, it.vehicle_id, lock=False)
        res_now = dict(it.result or {})
        res_now["condition_bullets"] = [{"id": b["id"], "text": b["text"], "source": b["source"], "evidence": b.get("evidence", []),
                                         "observation_id": (b.get("observation_ids") or [None])[0]} for b in current_bullets(v)]
        res_now["condition_version"] = v.condition_version
        res_now["corrections"] = len(it.corrections)
        it.result = res_now
    ctx.touch(it, "vehicle_intake")
    ctx.record("Intake correction" + (f" — {inp.reason}" if inp.reason else ""), entity_kind="vehicle_intake", entity_id=it.id, kind="intake",
               state=it.status, details=change)
    ctx.emit("intake.applied", aggregate_type="vehicle_intake", aggregate_id=it.id, aggregate_version=it.version,
             payload={"intake_id": it.id, "vehicle_id": it.vehicle_id, "correction": True, "status": it.status})
    return {"intake": serialize_intake(it), "change": change, "observation": serialize_observation(o) if o is not None else None}


@command("intake.undo", input=IntakeRefIn, perm="intake", action_class="internal",
         description="Undo the last apply's reversible changes: remove the bullets it added, cancel the tasks/issues it created, retire "
                     "the photo links it made and archive a card it created. Evidence, facts and milestones are kept (correct them explicitly).")
async def intake_undo(ctx: CommandContext, inp: IntakeRefIn) -> dict:
    it = await load_intake(ctx.db, ctx.actor, inp.intake_id, lock=True, expected_version=inp.expected_version)
    applied = dict(it.applied or {})
    if not applied.get("vehicle_id") or it.undone_at is not None:
        raise Blocked("nothing to undo")
    vid = applied["vehicle_id"]
    await _vehicle_allowed(ctx.db, ctx.actor, vid)
    reason = inp.reason or "intake undo"
    done: dict = {"bullets_removed": [], "tasks_cancelled": [], "issues_rejected": [], "links_removed": [], "vehicle_archived": False,
                  "kept": {"fact_ids": applied.get("fact_ids") or [], "milestone_ids": applied.get("milestone_ids") or [],
                           "tasks_merged": applied.get("tasks_merged") or [], "bullets_updated": applied.get("bullets_updated") or []}}
    for bid in applied.get("bullet_ids") or []:
        try:
            await dispatch(ctx.child(), "vehicles.edit_condition_bullet", {"vehicle_id": vid, "bullet_id": bid, "remove": True, "reason": reason}, commit=False)
            done["bullets_removed"].append(bid)
        except DomainError as e:
            done.setdefault("errors", []).append({"bullet_id": bid, "error": e.message})
    for tid in applied.get("task_ids") or []:
        t = await ctx.db.get(Task, tid)
        if t is not None and t.status not in ("completed", "cancelled"):
            await dispatch(ctx.child(), "tasks.cancel", {"task_id": tid, "reason": reason}, commit=False)
            done["tasks_cancelled"].append(tid)
    for iid in applied.get("issue_ids") or []:
        try:
            await dispatch(ctx.child(), "shop.update_issue", {"issue_id": iid, "vehicle_id": vid, "status": "rejected", "reason": reason}, commit=False)
            done["issues_rejected"].append(iid)
        except DomainError as e:
            done.setdefault("errors", []).append({"issue_id": iid, "error": e.message})
    for lid in applied.get("link_ids") or []:
        try:
            await dispatch(ctx.child(), "assets.remove_link", {"link_id": lid, "reason": reason}, commit=False)
            done["links_removed"].append(lid)
        except DomainError as e:
            done.setdefault("errors", []).append({"link_id": lid, "error": e.message})
    if applied.get("created"):
        await dispatch(ctx.child(), "vehicles.archive", {"vehicle_id": vid, "reason": reason}, commit=False)
        done["vehicle_archived"] = True
    for o in await observations_of(ctx.db, it.id):
        if o.status in ("applied", "needs_confirmation", "failed"):
            o.history = [*(o.history or []), {"at": ctx.now.isoformat(), "by": ctx.actor.user_id, "change": "undone", "applied_kind": o.applied_kind,
                                              "applied_id": o.applied_id}]
            o.status = "pending"
            o.applied_kind, o.applied_id, o.applied_command, o.applied_version = None, None, None, None
    it.undone_at = ctx.now
    it.status = "undone"
    it.applied = {**applied, "undone_at": ctx.now.isoformat(), "undo": done}
    it.result = {**(it.result or {}), "undone": True, "undo": done}
    ctx.touch(it, "vehicle_intake")
    ctx.record(f"Intake undone — {reason}", entity_kind="vehicle", entity_id=vid, kind="intake", state="undone", details={"intake_id": it.id, **done})
    ctx.emit("intake.applied", aggregate_type="vehicle_intake", aggregate_id=it.id, aggregate_version=it.version,
             payload={"intake_id": it.id, "vehicle_id": vid, "undone": True, "status": it.status})
    v = await get_vehicle(ctx.db, vid, lock=False)
    from .vehicles import recompute
    await recompute(ctx.db, v, ctx.now)
    return {"intake": serialize_intake(it), "undo": done}


@command("intake.abandon", input=IntakeRefIn, perm="intake", action_class="internal",
         description="Close an intake without applying (or after applying). Saved photos stay private on the intake; nothing is deleted.")
async def intake_abandon(ctx: CommandContext, inp: IntakeRefIn) -> dict:
    it = await load_intake(ctx.db, ctx.actor, inp.intake_id, lock=True, expected_version=inp.expected_version)
    if it.status == "abandoned":
        return {"intake": serialize_intake(it), "changed": False}
    before = it.status
    it.status = "abandoned"
    it.last_error = inp.reason
    ctx.touch(it, "vehicle_intake")
    ctx.record("Intake abandoned" + (f" — {inp.reason}" if inp.reason else ""), entity_kind="vehicle_intake", entity_id=it.id, kind="intake",
               state="abandoned", details={"before": before, "assets_kept": list(it.asset_ids or [])})
    return {"intake": serialize_intake(it), "changed": True}


# ── reads ────────────────────────────────────────────────────────────────────
async def list_intakes(db: AsyncSession, actor: Actor, *, status: str | None = None, vehicle_id: str | None = None,
                       limit: int = 50, offset: int = 0) -> tuple[list[VehicleIntake], int]:
    q = select(VehicleIntake)
    if actor.kind == "external":
        q = q.where(VehicleIntake.client_key == actor.key)
    elif actor.role != "owner":
        q = q.where(VehicleIntake.owner_user_id == actor.user_id)
    if status:
        q = q.where(VehicleIntake.status == status)
    if vehicle_id:
        q = q.where(VehicleIntake.vehicle_id == vehicle_id)
    total = await db.scalar(select(func.count()).select_from(q.subquery()))
    rows = (await db.execute(q.order_by(VehicleIntake.updated_at.desc()).limit(limit).offset(offset))).scalars().all()
    return list(rows), int(total or 0)

