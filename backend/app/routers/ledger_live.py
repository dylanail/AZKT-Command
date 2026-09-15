"""Live ledger source setup (spec §6.1, §12.3).

The mapping/preview/activation commands live in finance (`/api/finance/ledger/...`); this router adds
the *source* side: pick the spreadsheet in the connected Google account, persist it on the sheets
connection and trigger a sync through the live source. Read-only: no endpoint here can write a cell,
and the source refuses to run against a connection granted a write scope.

Path note: finance owns `POST /api/finance/ledger/{action}`, which would shadow a bare
`POST /api/finance/ledger/sync`, so the sync trigger lives at `POST /api/finance/ledger/source/sync`.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters import sheets as sheets_adapter
from ..adapters import sheets_ledger
from ..auth.deps import command_context, require
from ..core.errors import Unsupported
from ..db import get_db
from ..domain import jobs
from ..domain.actors import Actor
from ..domain.commands import CommandContext, dispatch
from ..services import connections as conn_svc
from ..services import drive_assets as svc

router = APIRouter(prefix="/api/finance/ledger", tags=["finance"])


async def _source(db: AsyncSession):
    """Fixture source when one is installed (tests / preview), else the live Google source."""
    src = sheets_ledger._SOURCE
    if src is not None:
        return src
    conn = await conn_svc.get(db, "sheets")
    return sheets_adapter.make_source(db, conn)


@router.get("/sheets")
async def candidate_sheets(q: str | None = Query(default=None), db: AsyncSession = Depends(get_db),
                           actor: Actor = Depends(require("finance.write"))) -> dict:
    conn = await conn_svc.get(db, "sheets")
    try:
        src = await _source(db)
        rows = await src.list_candidate_sheets(q)
    except Unsupported as e:
        return {"connected": False, "setup_blocked": e.message, "candidates": []}
    except AttributeError:
        return {"connected": True, "candidates": [], "note": "this ledger source cannot list candidate sheets"}
    return {"connected": True, "read_only": True, "selected": (conn.config or {}).get("sheet_id") if conn else None,
            "candidates": [{"id": r.get("id"), "name": r.get("name"), "modified_time": r.get("modifiedTime"),
                            "owner": ((r.get("owners") or [{}])[0]).get("emailAddress"),
                            "link": r.get("webViewLink"), "revision": r.get("version")} for r in rows]}


@router.post("/sheets/select")
async def select_sheet(payload: dict = Body(...), ctx: CommandContext = Depends(command_context)) -> dict:
    res = await dispatch(ctx, "ledger.select_sheet", payload)
    return res.to_dict()


@router.get("/source")
async def source_status(db: AsyncSession = Depends(get_db), actor: Actor = Depends(require("finance.status"))) -> dict:
    conn = await conn_svc.get(db, "sheets")
    mapping = await svc.active_mapping(db, (conn.config or {}).get("sheet_id") if conn else None)
    return {"connection": conn_svc.serialize(conn, "sheets"),
            "sheet": {"id": (conn.config or {}).get("sheet_id"), "title": (conn.config or {}).get("sheet_title"),
                      "tab": (conn.config or {}).get("tab_title")} if conn else {},
            "read_only": True, "requested_scopes": sheets_adapter.requested_scopes(),
            "active_mapping": {"id": mapping.id, "version": mapping.mapping_version,
                               "source_revision": mapping.source_revision,
                               "last_import_at": mapping.last_import_at.isoformat() if mapping and mapping.last_import_at else None}
            if mapping else None,
            "sync_interval_seconds": svc.LEDGER_SYNC_SECONDS}


@router.post("/source/sync")
async def sync(payload: dict = Body(default={}), db: AsyncSession = Depends(get_db),
               actor: Actor = Depends(require("finance.write"))) -> dict:
    """Enqueue a live import through the active mapping. The job does the work (no GET side effects,
    no inline provider call on the request path)."""
    conn = await conn_svc.get(db, "sheets")
    if conn is None or conn.status == "disconnected" or not (conn.config or {}).get("sheet_id"):
        return {"status": "setup_blocked", "reason": "no ledger sheet selected"}
    mapping_id = (payload or {}).get("mapping_id")
    if not mapping_id:
        m = await svc.active_mapping(db, (conn.config or {}).get("sheet_id"))
        if m is None:
            return {"status": "blocked", "reason": "no active ledger mapping; preview and activate one first"}
        mapping_id = m.id
    job = await jobs.enqueue(db, "ledger.sync", {"mapping_id": mapping_id}, dedupe_key="ledger:sync")
    await db.commit()
    return {"status": "queued" if job else "already_queued", "mapping_id": mapping_id, "job_id": job.id if job else None}
