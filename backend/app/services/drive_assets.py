"""Importer Drive: folder selection, incremental index, vehicle matching and asset import
(spec §7.1, acceptance F01–F03, A09).

Truths this module keeps:

* The selected root is an explicit choice. A duplicate folder name prompts a selection; it is never
  guessed (A09).
* The index is keyed by Drive file id (never by name or position). Incremental scans use Drive's
  changes feed and re-check allowed-root ancestry after a move; leaving the root or losing access
  clears `retrieval_eligible` so no new retrieval happens (F03).
* A folder maps to a vehicle only with evidence: a unique stock/frame reference corroborated by the
  folder contents or an importer message. Model/year/colour similarity is a proposal (F01).
* Originals are copied through the normal upload/finalize path (checksum dedupe, EXIF, derivatives).
  Source sharing is never changed, sensitive documents are never public-eligible (F02) and importer
  photos are marked pre-arrival.
* Interrupted processing is recoverable: each file keeps `import_status` + attempts and the
  `drive.import_assets` job retries without duplicating an asset.

It also owns the sibling *Google source selection* for the ledger sheet (`ledger.select_sheet`) and
the live ledger sync job/sweep, because both persist a chosen Google source on a connection and the
worker only imports `services/*` (routers are not loaded in the worker process).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters import drive as drive_adapter
from ..adapters import sheets as sheets_adapter
from ..core.errors import Blocked, NotFound, ProviderError, Unsupported, ValidationFailed
from ..core.ids import sha256_hex
from ..domain import jobs
from ..domain.actors import SYSTEM_ACTOR
from ..domain.commands import CommandContext, command, dispatch
from ..domain.jobs import sweep
from ..models.assets import Asset
from ..models.comms import Connection
from ..models.finance import LedgerMapping
from ..models.listings import DriveFile
from ..models.vehicles import Vehicle
from . import connections as conn_svc
from . import matching as matching_svc
from .storage import storage

log = logging.getLogger("azkt.drive")

SCAN_SECONDS = 15 * 60
LEDGER_SYNC_SECONDS = 15 * 60
MAX_ANCESTRY_DEPTH = 12
IMPORTABLE_MIME = ("image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "application/pdf")
# Document hints, most specific first: "bill of lading" is shipping paperwork, not an invoice, and a
# short hint like "bl" only counts as a whole word so "blue bed liner.jpg" stays a photo.
DOC_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("id_document", ("passport", "licence", "license", "id card", "id-card", "identity", "customer id", "drivers")),
    ("shipping_paper", ("bill of lading", "b/l", "bl", "export", "customs", "shipping", "manifest", "title",
                        "packing list")),
    ("invoice", ("invoice", "inv-", "請求", "receipt", "bill")),
    ("screenshot", ("screenshot", "screen shot", "capture")),
)
SHORT_HINTS = {"bl", "b/l", "bill", "title", "inv-"}   # matched on word boundaries only
SENSITIVE = {"invoice", "id_document", "shipping_paper"}


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def serialize_drive_file(f: DriveFile) -> dict:
    return {"id": f.id, "file_id": f.file_id, "name": f.name, "mime_type": f.mime_type, "is_folder": f.is_folder,
            "parent_id": f.parent_id, "path": f.path, "owner_email": f.owner_email, "modified_time": f.modified_time,
            "checksum": f.checksum, "revision": f.revision, "size_bytes": f.size_bytes, "web_link": f.web_link,
            "in_root": f.in_root, "removed": f.removed, "retrieval_eligible": f.retrieval_eligible,
            "ineligible_reason": f.ineligible_reason, "vehicle_id": f.vehicle_id, "match_state": f.match_state,
            "match_evidence": dict(f.match_evidence or {}), "confirmed_by": f.confirmed_by, "asset_id": f.asset_id,
            "classification": f.classification, "import_status": f.import_status, "import_error": f.import_error,
            "import_attempts": f.import_attempts, "last_seen_at": iso(f.last_seen_at), "imported_at": iso(f.imported_at)}


# ── connection helpers ───────────────────────────────────────────────────────
async def drive_connection(db: AsyncSession, *, create: bool = False) -> Connection | None:
    return await conn_svc.get(db, "drive", create=create)


def root_id_of(conn: Connection | None) -> str | None:
    return ((conn.config or {}).get("folder_id")) if conn else None


def adapter(db: AsyncSession, conn: Connection | None):
    return drive_adapter.adapter_for(db, conn)


async def _index_row(db: AsyncSession, conn: Connection, file_id: str) -> DriveFile | None:
    return (await db.execute(select(DriveFile).where(DriveFile.connection_id == conn.id,
                                                     DriveFile.file_id == file_id))).scalar_one_or_none()


def _hit(low: str, hint: str) -> bool:
    if hint in SHORT_HINTS:
        return re.search(rf"(?<![a-z0-9]){re.escape(hint)}(?![a-z0-9])", low) is not None
    return hint in low


def classify_name(name: str, mime: str) -> str:
    """Classify by file name, then by type. A document is never a listing photo (F02)."""
    low = (name or "").lower()
    for cls, hints in DOC_HINTS:
        if any(_hit(low, h) for h in hints):
            return cls
    if mime == "application/pdf":
        return "shipping_paper"   # an unnamed PDF in an importer folder is paperwork, never public media
    if mime.startswith("image/"):
        return "listing_photo"
    return "unknown"


def identity_tokens(text: str) -> list[str]:
    items = matching_svc.extract_items(text or "")
    return sorted({str(i["norm"]) for i in items if i["kind"] in ("stock_no", "frame_no") and i["norm"]})


# ── root selection (A09) ─────────────────────────────────────────────────────
class SelectRootIn(BaseModel):
    folder_id: str | None = None
    name: str | None = None
    note: str | None = None


async def _folder_path(ad, meta: dict) -> tuple[str, str | None]:
    """Walk parents for the human path; a shared drive may stop early (bounded)."""
    parts = [meta.get("name") or ""]
    parent = (meta.get("parents") or [None])[0]
    first_parent = parent
    depth = 0
    while parent and depth < MAX_ANCESTRY_DEPTH:
        try:
            p = await ad.get_file_meta(parent)
        except (ProviderError, Unsupported):
            break
        parts.append(p.get("name") or "")
        parent = (p.get("parents") or [None])[0]
        depth += 1
    return "/".join(reversed([p for p in parts if p])), first_parent


@command("drive.select_root", input=SelectRootIn, perm="connections", action_class="owner_only",
         approval_kind="connection", summary=lambda p: f"Select importer Drive folder {p.folder_id or p.name}",
         description="Owner selects the importer root folder. A duplicate folder name must be resolved by id; "
                     "the chosen folder id, its parent path and the account are persisted on the connection.")
async def drive_select_root(ctx: CommandContext, inp: SelectRootIn) -> dict:
    conn = await drive_connection(ctx.db, create=True)
    ad = adapter(ctx.db, conn)
    if not inp.folder_id and not inp.name:
        raise ValidationFailed("give folder_id (preferred) or name")
    if inp.folder_id:
        meta = await ad.get_file_meta(inp.folder_id)
        if not drive_adapter.is_folder(meta):
            raise ValidationFailed(f"{meta.get('name')!r} is not a folder")
    else:
        cands = await ad.search_folders(inp.name)
        exact = [c for c in cands if (c.get("name") or "").strip().lower() == inp.name.strip().lower()]
        if len(exact) != 1:
            raise Blocked("more than one folder has this name — choose the exact folder"
                          if exact else f"no folder named {inp.name!r} is visible to this account",
                          candidates=[{"id": c["id"], "name": c.get("name"),
                                       "owner": ((c.get("owners") or [{}])[0]).get("emailAddress"),
                                       "modified_time": c.get("modifiedTime"),
                                       "link": c.get("webViewLink")} for c in exact or cands])
        meta = exact[0]
    # coverage check: the selected root must actually be readable with the granted scopes
    try:
        listing = await ad.list_children(meta["id"])
    except ProviderError as e:
        await conn_svc.mark_failure(ctx.db, conn, drive_adapter.error_kind(e), str(e))
        raise Blocked(f"the selected folder cannot be read with the granted access: {e}",
                      kind=drive_adapter.error_kind(e))
    path, parent_id = await _folder_path(ad, meta)
    duplicates = [c for c in await ad.search_folders(meta.get("name"))
                  if c["id"] != meta["id"] and (c.get("name") or "") == (meta.get("name") or "")]
    owner = ((meta.get("owners") or [{}])[0]).get("emailAddress")
    conn.config = {**(conn.config or {}), "folder_id": meta["id"], "folder_name": meta.get("name"),
                   "folder_path": path, "parent_id": parent_id, "folder_link": meta.get("webViewLink"),
                   "folder_owner": owner, "selected_at": ctx.now.isoformat(),
                   "duplicate_names": [{"id": c["id"], "name": c.get("name"), "link": c.get("webViewLink")} for c in duplicates]}
    conn.account_identity = conn.account_identity or owner
    if conn.status == "disconnected":
        conn.status = "connected"
        conn.connected_at = conn.connected_at or ctx.now
        conn.connected_by = conn.connected_by or ctx.actor.user_id
    ctx.touch(conn, "connection")
    ctx.record(f"Importer Drive root selected: {path or meta.get('name')} ({meta['id']})", entity_kind="connection",
               entity_id=conn.id, kind="connection", state="configured", visibility="owner",
               details={"folder_id": meta["id"], "children": len(listing.get("files") or []),
                        "duplicate_names": len(duplicates), "note": inp.note})
    ctx.emit("connection.changed", aggregate_type="connection", aggregate_id=conn.id,
             payload={"provider": "drive", "change": "root_selected", "folder_id": meta["id"]})
    return {"connection_id": conn.id, "folder": {"id": meta["id"], "name": meta.get("name"), "path": path,
                                                 "owner": owner, "link": meta.get("webViewLink")},
            "children": len(listing.get("files") or []), "duplicate_names": conn.config["duplicate_names"]}


# ── scan ─────────────────────────────────────────────────────────────────────
class ScanIn(BaseModel):
    full: bool = False
    corroboration: list[str] = Field(default_factory=list)  # importer message refs the owner supplied
    limit: int = Field(default=2000, ge=1, le=20000)


def _apply_meta(row: DriveFile, meta: dict, *, path: str, now: datetime) -> None:
    row.name = meta.get("name") or row.name
    row.mime_type = meta.get("mimeType") or row.mime_type
    row.is_folder = drive_adapter.is_folder(meta)
    row.parent_id = (meta.get("parents") or [None])[0]
    row.path = path
    row.owner_email = ((meta.get("owners") or [{}])[0]).get("emailAddress") or row.owner_email
    row.modified_time = meta.get("modifiedTime") or row.modified_time
    new_checksum = meta.get("md5Checksum")
    if new_checksum and row.checksum and new_checksum != row.checksum and row.import_status == "imported":
        # a corrected version of an already imported file: re-import keeps the lineage (F03)
        row.import_status = "pending"
        row.import_error = None
    if new_checksum:
        row.checksum = new_checksum
    row.revision = str(meta.get("version") or row.revision or "")
    try:
        row.size_bytes = int(meta.get("size")) if meta.get("size") is not None else row.size_bytes
    except (TypeError, ValueError):
        pass
    row.web_link = meta.get("webViewLink") or row.web_link
    row.removed = bool(meta.get("trashed"))
    row.last_seen_at = now


async def _upsert_file(ctx: CommandContext, conn: Connection, meta: dict, *, path: str) -> DriveFile:
    row = await _index_row(ctx.db, conn, meta["id"])
    created = row is None
    if created:
        row = DriveFile(connection_id=conn.id, file_id=meta["id"], first_seen_at=ctx.now,
                        import_status="pending" if not drive_adapter.is_folder(meta) else "skipped",
                        created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
        ctx.db.add(row)
    _apply_meta(row, meta, path=path, now=ctx.now)
    if not row.removed and row.in_root:
        row.retrieval_eligible = True
        row.ineligible_reason = None
    if created:
        await ctx.db.flush()
    return row


async def _disable_subtree(ctx: CommandContext, conn: Connection, folder_id: str, reason: str) -> int:
    """A folder that left the root (or lost access) takes its indexed descendants with it: nothing under
    it stays retrievable, however deep (spec §7.1, F03)."""
    disabled, frontier, seen = 0, [folder_id], {folder_id}
    while frontier and len(seen) <= 10_000:
        batch = frontier
        frontier = []
        rows = (await ctx.db.execute(select(DriveFile).where(DriveFile.connection_id == conn.id,
                                                             DriveFile.parent_id.in_(batch)))).scalars().all()
        for child in rows:
            if child.file_id in seen:
                continue
            seen.add(child.file_id)
            if child.in_root or child.retrieval_eligible:
                child.in_root = False
                child.retrieval_eligible = False
                child.ineligible_reason = reason
                child.updated_by = ctx.actor.user_id
                disabled += 1
            frontier.append(child.file_id)
    return disabled


async def _ancestry_ok(ad, meta: dict, root_id: str, cache: dict[str, bool]) -> bool:
    """Is this file still under the selected root? Re-checked after every move (spec §7.1)."""
    seen = 0
    parent = (meta.get("parents") or [None])[0]
    while parent and seen < MAX_ANCESTRY_DEPTH:
        if parent == root_id:
            return True
        if parent in cache:
            return cache[parent]
        try:
            p = await ad.get_file_meta(parent)
        except (ProviderError, Unsupported):
            return False
        parent = (p.get("parents") or [None])[0]
        seen += 1
    return False


async def _walk(ctx: CommandContext, conn: Connection, ad, root_id: str, root_name: str, limit: int) -> list[DriveFile]:
    out: list[DriveFile] = []
    queue: list[tuple[str, str]] = [(root_id, root_name)]
    visited: set[str] = set()
    while queue and len(out) < limit:
        folder_id, prefix = queue.pop(0)
        if folder_id in visited:
            continue
        visited.add(folder_id)
        page_token = None
        while True:
            try:
                page = await ad.list_children(folder_id, page_token)
            except ProviderError as e:
                if drive_adapter.error_kind(e) == "permission_denied":
                    reason = "access to this folder was revoked at the source"
                    row = await _index_row(ctx.db, conn, folder_id)
                    if row is not None:
                        row.retrieval_eligible = False
                        row.ineligible_reason = reason
                    await _disable_subtree(ctx, conn, folder_id, reason)
                    break
                raise
            for meta in page.get("files") or []:
                path = f"{prefix}/{meta.get('name')}"
                row = await _upsert_file(ctx, conn, meta, path=path)
                row.in_root = True
                out.append(row)
                if drive_adapter.is_folder(meta):
                    queue.append((meta["id"], path))
                if len(out) >= limit:
                    break
            page_token = page.get("next_page_token")
            if not page_token or len(out) >= limit:
                break
    return out


@command("drive.scan", input=ScanIn, perm="connections", action_class="internal",
         description="Index the selected root (ids, parents, owner, type, modified time, checksum, version). "
                     "Incremental through Drive's changes feed; a file that leaves the root or loses access stops "
                     "being retrievable. Never downloads content.")
async def drive_scan(ctx: CommandContext, inp: ScanIn) -> dict:
    conn = await drive_connection(ctx.db)
    root = root_id_of(conn)
    if conn is None or not root:
        raise Blocked("no importer folder is selected yet (Settings → Connections → Drive)", setup_blocked="drive.folder")
    ad = adapter(ctx.db, conn)
    await conn_svc.mark_attempt(ctx.db, conn)
    cursor = await conn_svc.cursor_get(ctx.db, conn, "changes")
    root_name = (conn.config or {}).get("folder_name") or "root"
    counts = {"indexed": 0, "changed": 0, "removed": 0, "left_root": 0}
    mode = "full" if (inp.full or not cursor.get("page_token")) else "incremental"
    try:
        if mode == "full":
            rows = await _walk(ctx, conn, ad, root, root_name, inp.limit)
            counts["indexed"] = len(rows)
            token = await ad.changes_start_page_token()
        else:
            token = cursor["page_token"]
            cache: dict[str, bool] = {root: True}
            while True:
                page = await ad.changes_list(token)
                for ch in page.get("changes") or []:
                    fid = ch.get("fileId")
                    row = await _index_row(ctx.db, conn, fid) if fid else None
                    if ch.get("removed") or (ch.get("file") or {}).get("trashed"):
                        if row is not None:
                            row.removed = True
                            row.retrieval_eligible = False
                            row.ineligible_reason = "removed or trashed at the source"
                            row.updated_by = ctx.actor.user_id
                            counts["removed"] += 1
                        continue
                    meta = ch.get("file") or {}
                    if not meta.get("id"):
                        continue
                    inside = await _ancestry_ok(ad, meta, root, cache)
                    cache[meta["id"]] = inside
                    if not inside:
                        if row is not None and row.in_root:
                            reason = "moved outside the selected importer folder"
                            row.in_root = False
                            row.retrieval_eligible = False
                            row.ineligible_reason = reason
                            counts["left_root"] += 1
                            counts["left_root"] += await _disable_subtree(ctx, conn, row.file_id, reason)
                        continue
                    parent = (meta.get("parents") or [None])[0]
                    prow = await _index_row(ctx.db, conn, parent) if parent else None
                    prefix = prow.path if prow is not None else root_name
                    row = await _upsert_file(ctx, conn, meta, path=f"{prefix}/{meta.get('name')}")
                    row.in_root = True
                    counts["changed"] += 1
                    if drive_adapter.is_folder(meta):
                        counts["indexed"] += len(await _walk(ctx, conn, ad, meta["id"], row.path, inp.limit))
                token = page.get("new_start_page_token") or page.get("next_page_token") or token
                if not page.get("next_page_token"):
                    break
    except ProviderError as e:
        kind = drive_adapter.error_kind(e)
        await conn_svc.mark_failure(ctx.db, conn, kind, str(e))
        ctx.record(f"Drive scan failed ({kind}): {e}", entity_kind="connection", entity_id=conn.id, kind="connection",
                   state="failed", exception=True, visibility="owner")
        raise
    await conn_svc.cursor_set(ctx.db, conn, "changes", {"page_token": token, "at": ctx.now.isoformat(), "mode": mode})
    matches = await rematch_folders(ctx, conn, corroboration=inp.corroboration)
    await conn_svc.mark_success(ctx.db, conn, coverage_to=ctx.now)
    pending = await ctx.db.execute(select(DriveFile).where(DriveFile.connection_id == conn.id,
                                                            DriveFile.is_folder.is_(False),
                                                            DriveFile.import_status == "pending",
                                                            DriveFile.retrieval_eligible.is_(True)))
    pending_rows = pending.scalars().all()
    ctx.record(f"Drive scan ({mode}): {counts['indexed']} indexed, {counts['changed']} changed, "
               f"{counts['removed']} removed, {counts['left_root']} left the root", entity_kind="connection",
               entity_id=conn.id, kind="connection", state="scanned", visibility="owner",
               details={**counts, "pending_import": len(pending_rows), "matches": matches["summary"]})
    ctx.emit("drive.scanned", aggregate_type="connection", aggregate_id=conn.id,
             payload={"mode": mode, **counts, "pending_import": len(pending_rows)})
    return {"mode": mode, **counts, "pending_import": len(pending_rows), "matches": matches,
            "coverage": {"root": root, "path": (conn.config or {}).get("folder_path")}}


# ── folder → vehicle matching (F01) ──────────────────────────────────────────
async def _importer_corroboration(db: AsyncSession, folder_name: str) -> list[str]:
    """Identity tokens from importer messages that mention this folder by name (spec §7.1)."""
    try:
        from ..models.comms import Message
        rows = (await db.execute(select(Message.body_text, Message.subject)
                                 .where(Message.body_text.ilike(f"%{folder_name}%")).limit(10))).all()
    except Exception:  # noqa: BLE001 - inbox domain may be unavailable
        return []
    out: set[str] = set()
    for body, subject in rows:
        out.update(identity_tokens(f"{subject or ''} {body or ''}"))
    return sorted(out)


async def _match_folder(db: AsyncSession, folder: DriveFile, children: list[DriveFile],
                        extra_corroboration: list[str]) -> dict:
    child_text = " ".join(c.name for c in children)
    folder_tokens = identity_tokens(folder.name)
    child_tokens = identity_tokens(child_text)
    corroboration = list(extra_corroboration)
    agreeing = sorted(set(folder_tokens) & set(child_tokens))
    if agreeing:
        corroboration.append(f"folder and contents agree on {', '.join(agreeing)}")
    msg_tokens = await _importer_corroboration(db, folder.name)
    if set(msg_tokens) & set(folder_tokens):
        corroboration.append("importer message references the same identifier")
    year = None
    m = re.search(r"\b(19|20)\d{2}\b", folder.name or "")
    if m:
        year = int(m.group(0))
    res = await matching_svc.resolve_vehicle(db, text=f"{folder.name} {child_text}", corroboration=corroboration,
                                             model=_model_hint(db, folder.name), model_year=year)
    return {"state": res.state, "vehicle_id": res.entity_id, "candidates": res.candidates,
            "reasons": res.reasons, "evidence": {**res.evidence, "folder_tokens": folder_tokens,
                                                 "content_tokens": child_tokens, "corroboration": corroboration}}


_MODEL_WORDS = ("hijet", "acty", "carry", "sambar", "minicab", "clipper", "scrum", "porter", "vamos")


def _model_hint(_db, name: str) -> str | None:
    low = (name or "").lower()
    for w in _MODEL_WORDS:
        if w in low:
            return w
    return None


def _signature(folder: DriveFile, children: list[DriveFile]) -> str:
    toks = identity_tokens(folder.name + " " + " ".join(c.name for c in children))
    return sha256_hex("|".join(toks))[:32]


async def rematch_folders(ctx: CommandContext, conn: Connection, *, corroboration: list[str] | None = None) -> dict:
    rows = (await ctx.db.execute(select(DriveFile).where(DriveFile.connection_id == conn.id,
                                                          DriveFile.removed.is_(False),
                                                          DriveFile.in_root.is_(True)))).scalars().all()
    by_parent: dict[str, list[DriveFile]] = {}
    for r in rows:
        if r.parent_id:
            by_parent.setdefault(r.parent_id, []).append(r)
    summary = {"matched": 0, "proposed": 0, "ambiguous": 0, "unmatched": 0, "rechecked": 0, "kept_confirmed": 0}
    out: list[dict] = []
    for folder in [r for r in rows if r.is_folder]:
        children = by_parent.get(folder.file_id, [])
        if not children:
            continue
        sig = _signature(folder, children)
        if folder.match_state in ("confirmed", "corrected", "left"):
            if folder.identity_signature == sig:
                summary["kept_confirmed"] += 1
                continue
            summary["rechecked"] += 1  # identity-bearing contents changed → recheck (spec §7.1)
        res = await _match_folder(ctx.db, folder, children, list(corroboration or []))
        folder.identity_signature = sig
        prior_state, prior_vehicle = folder.match_state, folder.vehicle_id
        folder.match_state = res["state"]
        folder.match_evidence = {**res["evidence"], "reasons": res["reasons"], "candidates": res["candidates"][:5],
                                 "at": ctx.now.isoformat(), "recheck_of": prior_state if prior_state != "unmatched" else None}
        folder.vehicle_id = res["vehicle_id"] if res["state"] in ("matched", "proposed") else None
        folder.updated_by = ctx.actor.user_id
        summary[res["state"]] = summary.get(res["state"], 0) + 1
        out.append({"folder_id": folder.file_id, "name": folder.name, "state": res["state"],
                    "vehicle_id": folder.vehicle_id, "reasons": res["reasons"]})
        if res["state"] == "matched" and prior_vehicle != folder.vehicle_id:
            ctx.record(f"Drive folder {folder.name!r} matched to a vehicle on evidence",
                       entity_kind="vehicle", entity_id=folder.vehicle_id, kind="fact", state="matched",
                       details={"folder_id": folder.file_id, "reasons": res["reasons"]})
        elif res["state"] in ("proposed", "ambiguous"):
            ctx.record(f"Drive folder {folder.name!r} needs review ({res['state']})", entity_kind="connection",
                       entity_id=conn.id, kind="fact", state=res["state"],
                       details={"folder_id": folder.file_id, "candidates": res["candidates"][:5],
                                "reasons": res["reasons"]})
    return {"summary": summary, "folders": out}


class ConfirmMatchIn(BaseModel):
    file_id: str
    decision: str = "confirm"        # confirm|correct|leave
    vehicle_id: str | None = None
    note: str | None = None


@command("drive.confirm_match", input=ConfirmMatchIn, perm="vehicles.write", action_class="internal",
         records=lambda p: [("vehicle", p.vehicle_id)] if p.vehicle_id else [],
         description="Confirm, correct or leave a proposed folder → vehicle mapping. A confirmation persists on the "
                     "mapping and on every imported asset link, and is rechecked when identity-bearing contents change.")
async def drive_confirm_match(ctx: CommandContext, inp: ConfirmMatchIn) -> dict:
    conn = await drive_connection(ctx.db)
    if conn is None:
        raise NotFound("drive connection not found")
    folder = await _index_row(ctx.db, conn, inp.file_id)
    if folder is None or not folder.is_folder:
        raise NotFound("drive folder not found in the index")
    if inp.decision not in ("confirm", "correct", "leave"):
        raise ValidationFailed("decision must be confirm|correct|leave")
    if inp.decision == "leave":
        folder.match_state, folder.vehicle_id, folder.confirmed_by = "left", None, ctx.actor.user_id
        folder.match_evidence = {**(folder.match_evidence or {}), "left_reason": inp.note, "at": ctx.now.isoformat()}
        ctx.touch(folder, "drive_file")
        ctx.record(f"Drive folder {folder.name!r} left unmatched", entity_kind="connection", entity_id=conn.id,
                   kind="fact", state="left", details={"folder_id": folder.file_id, "note": inp.note})
        return {"folder": serialize_drive_file(folder), "relinked": 0}
    vehicle_id = inp.vehicle_id or (folder.vehicle_id if inp.decision == "confirm" else None)
    if not vehicle_id:
        raise ValidationFailed("give vehicle_id (a correction always names the vehicle)")
    v = await ctx.db.get(Vehicle, vehicle_id)
    if v is None:
        raise NotFound("vehicle not found")
    prior = folder.vehicle_id
    folder.vehicle_id = vehicle_id
    folder.match_state = "confirmed" if inp.decision == "confirm" else "corrected"
    folder.confirmed_by = ctx.actor.user_id
    folder.match_evidence = {**(folder.match_evidence or {}), "decision": inp.decision, "note": inp.note,
                             "by": ctx.actor.user_id, "at": ctx.now.isoformat(), "previous_vehicle_id": prior}
    ctx.touch(folder, "drive_file")
    relinked = 0
    children = (await ctx.db.execute(select(DriveFile).where(DriveFile.connection_id == conn.id,
                                                              DriveFile.parent_id == folder.file_id,
                                                              DriveFile.asset_id.is_not(None)))).scalars().all()
    for child in children:
        if prior and prior != vehicle_id:
            await dispatch(ctx.child(), "assets.remove_link", {"asset_id": child.asset_id, "entity_kind": "vehicle",
                                                               "entity_id": prior,
                                                               "reason": f"folder mapping corrected to {vehicle_id[:8]}"},
                           commit=False)
        role = "document" if (child.classification in SENSITIVE) else "photo"
        await dispatch(ctx.child(), "assets.link", {"asset_id": child.asset_id, "entity_kind": "vehicle",
                                                    "entity_id": vehicle_id, "role": role, "confirmed": True,
                                                    "match_evidence": dict(folder.match_evidence or {})}, commit=False)
        child.vehicle_id = vehicle_id
        relinked += 1
    ctx.record(f"Drive folder {folder.name!r} {folder.match_state} → vehicle {vehicle_id[:8]} ({relinked} asset link(s))",
               entity_kind="vehicle", entity_id=vehicle_id, kind="fact", state=folder.match_state,
               details={"folder_id": folder.file_id, "note": inp.note, "previous_vehicle_id": prior})
    ctx.emit("drive.match_confirmed", aggregate_type="vehicle", aggregate_id=vehicle_id,
             payload={"folder_id": folder.file_id, "decision": inp.decision, "relinked": relinked})
    return {"folder": serialize_drive_file(folder), "relinked": relinked}


# ── import (F02, F03) ────────────────────────────────────────────────────────
class ImportAssetsIn(BaseModel):
    folder_id: str | None = None
    file_ids: list[str] = Field(default_factory=list)
    limit: int = Field(default=25, ge=1, le=200)
    include_documents: bool = True


async def _link_target(db: AsyncSession, conn: Connection, row: DriveFile) -> tuple[str | None, dict]:
    """Only a matched/confirmed folder mapping links an imported asset to a vehicle (F01)."""
    parent = await _index_row(db, conn, row.parent_id) if row.parent_id else None
    if parent is None or parent.match_state not in ("matched", "confirmed", "corrected") or not parent.vehicle_id:
        return None, {"reason": "folder mapping is not evidenced yet", "folder_state": parent.match_state if parent else None}
    return parent.vehicle_id, {**(parent.match_evidence or {}), "folder_id": parent.file_id,
                               "confirmed_by": parent.confirmed_by}


async def _store_original(ctx: CommandContext, row: DriveFile, data: bytes, *, classification: str) -> dict:
    """Copy through the normal upload path: sniffing, checksum dedupe, EXIF and private derivatives."""
    from .assets import part_key
    prep = await dispatch(ctx.child(), "assets.prepare_upload", {
        "purpose": "document" if classification in SENSITIVE else "intake",
        "content_type": row.mime_type, "size_bytes": len(data), "filename": row.name}, commit=False)
    upload_id = prep.data["upload"]["id"]
    st = storage()
    key = part_key(upload_id)
    if st.exists(key):
        st.delete(key)
    st.append_part(key, data)
    await dispatch(ctx.child(), "assets.record_chunk", {"upload_id": upload_id, "received_bytes": len(data),
                                                        "complete": True}, commit=False)
    fin = await dispatch(ctx.child(), "assets.finalize_upload", {
        "upload_id": upload_id, "source": "drive", "classification": classification,
        "pre_arrival": classification == "listing_photo"}, commit=False)
    return fin.data or {}


@command("drive.import_assets", input=ImportAssetsIn, perm="documents.write", action_class="internal",
         description="Copy permitted originals into AZKT storage preserving source link, checksum and revision. "
                     "Identical bytes deduplicate while keeping lineage; sensitive documents are never public-eligible; "
                     "importer photos are marked pre-arrival. Failures stay retryable and never invent a photo.")
async def drive_import_assets(ctx: CommandContext, inp: ImportAssetsIn) -> dict:
    conn = await drive_connection(ctx.db)
    if conn is None or not root_id_of(conn):
        raise Blocked("no importer folder is selected yet", setup_blocked="drive.folder")
    ad = adapter(ctx.db, conn)
    q = select(DriveFile).where(DriveFile.connection_id == conn.id, DriveFile.is_folder.is_(False),
                                DriveFile.removed.is_(False))
    if inp.file_ids:
        q = q.where(DriveFile.file_id.in_(inp.file_ids))
    else:
        q = q.where(DriveFile.import_status.in_(("pending", "failed")))
    if inp.folder_id:
        q = q.where(DriveFile.parent_id == inp.folder_id)
    rows = (await ctx.db.execute(q.order_by(DriveFile.name).limit(inp.limit))).scalars().all()
    out = {"imported": 0, "deduplicated": 0, "skipped": 0, "failed": 0, "blocked": 0}
    items: list[dict] = []
    for row in rows:
        if not row.retrieval_eligible:
            row.import_status = "skipped"
            row.import_error = row.ineligible_reason or "retrieval is not permitted for this file"
            out["blocked"] += 1
            items.append({"file_id": row.file_id, "name": row.name, "status": "blocked", "reason": row.import_error})
            continue
        if row.mime_type not in IMPORTABLE_MIME:
            row.import_status = "skipped"
            row.import_error = f"unsupported type {row.mime_type}"
            out["skipped"] += 1
            items.append({"file_id": row.file_id, "name": row.name, "status": "skipped", "reason": row.import_error})
            continue
        classification = classify_name(row.name, row.mime_type)
        if classification in SENSITIVE and not inp.include_documents:
            row.import_status = "skipped"
            row.import_error = "documents excluded from this import"
            out["skipped"] += 1
            continue
        row.import_attempts = (row.import_attempts or 0) + 1
        try:
            data = await ad.download(row.file_id)
        except ProviderError as e:
            kind = drive_adapter.error_kind(e)
            if kind in ("permission_denied", "auth_expired"):
                row.retrieval_eligible = False
                row.ineligible_reason = f"source access revoked ({kind})"
                row.import_status = "failed"
                out["blocked"] += 1
            else:
                row.import_status = "failed"   # retryable; the job picks it up again
                out["failed"] += 1
            row.import_error = f"{kind}: {e}"[:500]
            items.append({"file_id": row.file_id, "name": row.name, "status": row.import_status, "reason": row.import_error})
            ctx.record(f"Drive import failed for {row.name}: {row.import_error}", entity_kind="connection",
                       entity_id=conn.id, kind="intake", state="failed", exception=True,
                       details={"file_id": row.file_id, "attempts": row.import_attempts})
            continue
        except Unsupported as e:
            row.import_status = "skipped"
            row.import_error = str(e)[:500]
            out["skipped"] += 1
            continue
        vehicle_id, evidence = await _link_target(ctx.db, conn, row)
        res = await _store_original(ctx, row, data, classification=classification)
        asset = res.get("asset") or {}
        if res.get("status") != "ready" or not asset.get("id"):
            row.import_status = "failed"
            row.import_error = res.get("error") or "asset could not be stored"
            out["failed"] += 1
            items.append({"file_id": row.file_id, "name": row.name, "status": "failed", "reason": row.import_error})
            continue
        a = await ctx.db.get(Asset, asset["id"])
        # Identical bytes may already exist from another origin (a shop photo taken after recon).
        # Lineage always records this Drive source; provenance labels are only (re)written for an
        # asset this import owns — a post-recon photo never becomes "importer / pre-arrival" (§7.1).
        drive_owned = (not res.get("deduplicated")) or a.source == "drive"
        lineage = list((a.analysis or {}).get("drive_sources") or [])
        if not any(s.get("file_id") == row.file_id for s in lineage):
            lineage.append({"file_id": row.file_id, "name": row.name, "checksum": row.checksum,
                            "revision": row.revision, "path": row.path, "link": row.web_link,
                            "imported_at": ctx.now.isoformat()})
        a.analysis = {**(a.analysis or {}), "drive_sources": lineage}
        if drive_owned:
            a.provider_ref = f"drive:{row.file_id}"
            a.provider_revision = row.revision
            a.provider_link = row.web_link
            a.source = "drive"
        if classification in SENSITIVE:
            a.sensitive, a.public_eligible = True, False
            a.visibility = "owner" if classification == "id_document" else "internal"
        elif drive_owned:
            a.pre_arrival = True      # importer photo: never proof of post-recon condition
        a.bump(ctx.actor.user_id)
        row.asset_id = a.id
        row.classification = classification
        row.vehicle_id = vehicle_id
        row.imported_at = ctx.now
        row.import_error = None
        row.import_status = "deduplicated" if res.get("deduplicated") else "imported"
        out["deduplicated" if res.get("deduplicated") else "imported"] += 1
        if vehicle_id:
            role = "document" if classification in SENSITIVE else "photo"
            parent = await _index_row(ctx.db, conn, row.parent_id) if row.parent_id else None
            await dispatch(ctx.child(), "assets.link", {
                "asset_id": a.id, "entity_kind": "vehicle", "entity_id": vehicle_id, "role": role,
                "match_evidence": evidence, "confirmed": bool(parent and parent.confirmed_by)}, commit=False)
        items.append({"file_id": row.file_id, "name": row.name, "status": row.import_status, "asset_id": a.id,
                      "classification": classification, "vehicle_id": vehicle_id,
                      "public_eligible": bool(a.public_eligible), "sensitive": bool(a.sensitive)})
    ctx.record(f"Drive import: {out['imported']} new, {out['deduplicated']} duplicate, {out['failed']} failed, "
               f"{out['blocked']} blocked, {out['skipped']} skipped", entity_kind="connection", entity_id=conn.id,
               kind="intake", state="imported", visibility="owner", details=out)
    if out["imported"] or out["deduplicated"]:
        ctx.emit("drive.assets_imported", aggregate_type="connection", aggregate_id=conn.id,
                 payload={**out, "items": items[:50]})
    return {**out, "items": items}


# ── jobs and sweeps ──────────────────────────────────────────────────────────
def _system_ctx(db: AsyncSession, correlation_id: str | None = None) -> CommandContext:
    return CommandContext(db=db, actor=SYSTEM_ACTOR, correlation_id=correlation_id, channel="worker")


@jobs.job("drive.scan")
async def _scan_job(jctx: jobs.JobContext, payload: dict) -> dict:
    try:
        res = await dispatch(_system_ctx(jctx.db), "drive.scan", {"full": bool(payload.get("full"))})
    except Blocked as e:
        return {"skipped": e.message}
    except Unsupported as e:
        # no credential / no client: setup blocked is a state, not a failing job
        return {"setup_blocked": str(e)}
    except Unsupported as e:
        # no usable Google client (setup blocked): say so, never retry forever and never invent an index
        return {"setup_blocked": e.message}
    data = res.data or {}
    if data.get("pending_import"):
        await jobs.enqueue(jctx.db, "drive.import_assets", {}, dedupe_key="drive:import")
        await jctx.db.commit()
    return {k: v for k, v in data.items() if k != "matches"}


@jobs.job("drive.import_assets")
async def _import_job(jctx: jobs.JobContext, payload: dict) -> dict:
    try:
        res = await dispatch(_system_ctx(jctx.db), "drive.import_assets",
                             {"folder_id": payload.get("folder_id"), "file_ids": payload.get("file_ids") or [],
                              "limit": int(payload.get("limit") or 25)})
    except Blocked as e:
        return {"skipped": e.message}
    except Unsupported as e:
        return {"setup_blocked": str(e)}
    except Unsupported as e:
        return {"setup_blocked": e.message}
    data = res.data or {}
    return {k: v for k, v in data.items() if k != "items"}


@sweep("drive.scan", SCAN_SECONDS)
async def drive_scan_sweep(session_factory) -> dict:
    async with session_factory() as db:
        conn = await drive_connection(db)
        if conn is None or conn.status == "disconnected" or not root_id_of(conn):
            return {"skipped": "drive root not selected"}
        try:
            adapter(db, conn)          # no reachable client: nothing is queued (setup blocked)
        except Unsupported as e:
            return {"setup_blocked": str(e)}
        await jobs.enqueue(db, "drive.scan", {}, dedupe_key="drive:scan")
        await db.commit()
        return {"enqueued": True}


# ── ledger sheet selection and live sync (spec §6.1) ─────────────────────────
class SelectSheetIn(BaseModel):
    sheet_id: str
    tab_title: str | None = None
    name: str | None = None


@command("ledger.select_sheet", input=SelectSheetIn, perm="connections", action_class="owner_only",
         approval_kind="connection", summary=lambda p: f"Select ledger sheet {p.sheet_id}",
         description="Owner selects the ledger spreadsheet. The sheet id/tab are persisted on the sheets connection; "
                     "the source stays read-only (writeback is a separate approved feature).")
async def ledger_select_sheet(ctx: CommandContext, inp: SelectSheetIn) -> dict:
    conn = await conn_svc.get(ctx.db, "sheets", create=True)
    sheets_adapter.assert_read_only(list(conn.granted_scopes or []))
    src = _fixture_source()
    if src is None:
        src = await sheets_adapter.install_live_source(ctx.db, conn)
    meta = await src.get_sheet_meta(inp.sheet_id)
    tab = next((t for t in meta.tabs if inp.tab_title in (t.title, t.tab_id)), None) if inp.tab_title else (meta.tabs[0] if meta.tabs else None)
    if tab is None:
        raise NotFound("tab not found in the selected sheet", tabs=[t.title for t in meta.tabs])
    conn.config = {**(conn.config or {}), "sheet_id": inp.sheet_id, "sheet_title": meta.title,
                   "tab_title": tab.title, "sheet_tab_id": tab.tab_id, "selected_at": ctx.now.isoformat(),
                   "read_only": True}
    if conn.status == "disconnected":
        conn.status = "connected"
        conn.connected_at = conn.connected_at or ctx.now
        conn.connected_by = conn.connected_by or ctx.actor.user_id
    ctx.touch(conn, "connection")
    ctx.record(f"Ledger sheet selected: {meta.title} / {tab.title} (read-only)", entity_kind="connection",
               entity_id=conn.id, kind="connection", state="configured", visibility="finance",
               details={"sheet_id": inp.sheet_id, "tab": tab.title, "revision": meta.revision})
    ctx.emit("connection.changed", aggregate_type="connection", aggregate_id=conn.id,
             payload={"provider": "sheets", "change": "sheet_selected", "sheet_id": inp.sheet_id})
    return {"connection_id": conn.id, "sheet": {"id": inp.sheet_id, "title": meta.title, "tab": tab.title,
                                                "tab_id": tab.tab_id, "revision": meta.revision,
                                                "headers": tab.headers, "row_count": tab.row_count},
            "tabs": [{"tab_id": t.tab_id, "title": t.title, "row_count": t.row_count} for t in meta.tabs]}


def _fixture_source():
    from ..adapters import sheets_ledger
    return sheets_ledger._SOURCE  # installed by tests / the setup preview


async def active_mapping(db: AsyncSession, sheet_id: str | None = None) -> LedgerMapping | None:
    q = select(LedgerMapping).where(LedgerMapping.status == "active")
    if sheet_id:
        q = q.where(LedgerMapping.sheet_id == sheet_id)
    return (await db.execute(q.order_by(LedgerMapping.mapping_version.desc()))).scalars().first()


@jobs.job("ledger.sync")
async def _ledger_sync_job(jctx: jobs.JobContext, payload: dict) -> dict:
    db = jctx.db
    conn = await conn_svc.get(db, "sheets")
    if conn is None or conn.status == "disconnected":
        return {"skipped": "sheets not connected"}
    mapping_id = payload.get("mapping_id")
    m = await db.get(LedgerMapping, mapping_id) if mapping_id else await active_mapping(db, (conn.config or {}).get("sheet_id"))
    if m is None or m.status != "active":
        return {"skipped": "no active ledger mapping"}
    if _fixture_source() is None:
        try:
            await sheets_adapter.install_live_source(db, conn)
        except Unsupported as e:
            return {"setup_blocked": str(e)}
    await conn_svc.mark_attempt(db, conn)
    try:
        res = await dispatch(_system_ctx(db), "ledger.import_rows", {"mapping_id": m.id})
    except (ProviderError, Unsupported) as e:
        await conn_svc.mark_failure(db, conn, "transient", f"ledger import failed: {e}")
        await db.commit()
        return {"error": str(e)}
    await conn_svc.mark_success(db, conn, coverage_to=datetime.now(timezone.utc))
    await db.commit()
    data = res.data or {}
    return {"mapping_id": m.id, "counts": data.get("counts"), "source_revision": data.get("source_revision")}


@sweep("ledger.sync", LEDGER_SYNC_SECONDS)
async def ledger_sync_sweep(session_factory) -> dict:
    async with session_factory() as db:
        conn = await conn_svc.get(db, "sheets")
        if conn is None or conn.status == "disconnected" or not (conn.config or {}).get("sheet_id"):
            return {"skipped": "ledger sheet not selected"}
        m = await active_mapping(db, (conn.config or {}).get("sheet_id"))
        if m is None:
            return {"skipped": "no active ledger mapping"}
        if _fixture_source() is None:
            try:
                sheets_adapter.make_source(db, conn)
            except Unsupported as e:
                return {"setup_blocked": str(e)}
        await jobs.enqueue(db, "ledger.sync", {"mapping_id": m.id}, dedupe_key="ledger:sync")
        await db.commit()
        return {"enqueued": True, "mapping_id": m.id}


# ── reads for the router ─────────────────────────────────────────────────────
async def coverage(db: AsyncSession) -> dict:
    conn = await drive_connection(db)
    if conn is None:
        return {"connected": False, "root": None, "counts": {}, "freshness": conn_svc.freshness(None)}
    rows = (await db.execute(select(DriveFile).where(DriveFile.connection_id == conn.id))).scalars().all()
    counts = {"files": len([r for r in rows if not r.is_folder]), "folders": len([r for r in rows if r.is_folder]),
              "pending": len([r for r in rows if r.import_status == "pending" and not r.is_folder]),
              "imported": len([r for r in rows if r.import_status in ("imported", "deduplicated")]),
              "failed": len([r for r in rows if r.import_status == "failed"]),
              "blocked": len([r for r in rows if not r.retrieval_eligible and not r.is_folder]),
              "left_root": len([r for r in rows if not r.in_root]), "removed": len([r for r in rows if r.removed])}
    cursor = await conn_svc.cursor_get(db, conn, "changes")
    return {"connected": conn.status != "disconnected", "root": {"folder_id": root_id_of(conn),
                                                                 "name": (conn.config or {}).get("folder_name"),
                                                                 "path": (conn.config or {}).get("folder_path"),
                                                                 "link": (conn.config or {}).get("folder_link"),
                                                                 "owner": (conn.config or {}).get("folder_owner")},
            "duplicate_names": (conn.config or {}).get("duplicate_names") or [],
            "counts": counts, "cursor": {"page_token": cursor.get("page_token"), "at": cursor.get("at"),
                                         "mode": cursor.get("mode")},
            "freshness": conn_svc.freshness(conn), "failure": conn.failure or {}}


async def proposed_matches(db: AsyncSession, *, states: tuple[str, ...] = ("proposed", "ambiguous")) -> list[dict]:
    conn = await drive_connection(db)
    if conn is None:
        return []
    rows = (await db.execute(select(DriveFile).where(DriveFile.connection_id == conn.id, DriveFile.is_folder.is_(True),
                                                      DriveFile.match_state.in_(states),
                                                      DriveFile.removed.is_(False)))).scalars().all()
    out = []
    for r in rows:
        cands = []
        for c in (r.match_evidence or {}).get("candidates") or []:
            v = await db.get(Vehicle, c.get("vehicle_id")) if c.get("vehicle_id") else None
            cands.append({**c, "title": v.title if v else None, "stock_no": v.stock_no if v else None})
        out.append({**serialize_drive_file(r), "candidates": cands})
    return out


async def files_in(db: AsyncSession, folder_id: str | None = None, *, limit: int = 200) -> list[dict]:
    conn = await drive_connection(db)
    if conn is None:
        return []
    q = select(DriveFile).where(DriveFile.connection_id == conn.id)
    if folder_id:
        q = q.where(DriveFile.parent_id == folder_id)
    rows = (await db.execute(q.order_by(DriveFile.is_folder.desc(), DriveFile.name).limit(limit))).scalars().all()
    return [serialize_drive_file(r) for r in rows]
