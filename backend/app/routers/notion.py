"""Notion control surface.

Lets the `main` agent (or the dashboard) set Notion sync up end-to-end
without SSHing to the droplet:

  GET  /api/notion/status      what's configured + remaining gaps
  POST /api/notion/introspect  ask Notion for the exact property names
  GET  /api/notion/schema      the current mapping file
  PUT  /api/notion/schema      save a corrected mapping, re-check gaps

Postgres stays source of truth; this only edits the mapping config.
"""
from __future__ import annotations

import json

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.passkey import current_user
from ..core.config import CONFIG_DIR, settings
from ..db import get_db
from ..services import notion_sync

router = APIRouter(prefix="/api/notion", tags=["notion"],
                   dependencies=[Depends(current_user)])

_SCHEMA = CONFIG_DIR / "notion_schema.json"
_DRAFT = CONFIG_DIR / "notion_schema.draft.json"
_NOTION_API = "https://api.notion.com/v1"


@router.get("/status")
async def status(db: AsyncSession = Depends(get_db)):
    return {
        "env": {
            "NOTION_TOKEN": bool(settings.NOTION_TOKEN),
            "NOTION_VEHICLES_DB_ID": bool(settings.NOTION_VEHICLES_DB_ID),
            "NOTION_CUSTOMERS_DB_ID": bool(settings.NOTION_CUSTOMERS_DB_ID),
            "NOTION_IRQS_DB_ID": bool(settings.NOTION_IRQS_DB_ID),
            "poll_seconds": settings.NOTION_POLL_SECONDS,
        },
        "schema_gaps": notion_sync.schema_gaps(),
        "last_sync_at": await notion_sync.last_sync_at(db),
    }


@router.post("/introspect")
async def introspect():
    if not settings.NOTION_TOKEN:
        raise HTTPException(400, "NOTION_TOKEN not set in .env")
    dbs = {
        "VEHICLES": settings.NOTION_VEHICLES_DB_ID,
        "CUSTOMERS": settings.NOTION_CUSTOMERS_DB_ID,
        "IRQS": settings.NOTION_IRQS_DB_ID,
    }
    missing = [k for k, v in dbs.items() if not v]
    if missing:
        raise HTTPException(400, f"missing DB ids in .env: {', '.join(missing)}")
    headers = {"Authorization": f"Bearer {settings.NOTION_TOKEN}",
               "Notion-Version": "2022-06-28"}
    result: dict = {}
    async with httpx.AsyncClient(timeout=30) as c:
        for label, db_id in dbs.items():
            r = await c.get(f"{_NOTION_API}/databases/{db_id}", headers=headers)
            if r.status_code != 200:
                result[label] = {"error": f"HTTP {r.status_code}",
                                  "detail": r.text[:300]}
                continue
            props = r.json().get("properties", {})
            result[label] = {"properties": sorted(
                ({"name": n, "type": m.get("type")} for n, m in props.items()),
                key=lambda x: x["name"],
            )}
    _DRAFT.write_text(json.dumps(result, indent=2))
    return {"databases": result, "draft_written": str(_DRAFT)}


@router.get("/schema")
async def get_schema():
    return json.loads(_SCHEMA.read_text())


@router.put("/schema")
async def put_schema(request: Request):
    body = await request.json()
    if not isinstance(body, dict) or not {"VEHICLES", "CUSTOMERS", "IRQS"} & set(body):
        raise HTTPException(400, "expected a schema object with VEHICLES/CUSTOMERS/IRQS")
    _SCHEMA.write_text(json.dumps(body, indent=2))
    return {"ok": True, "schema_gaps": notion_sync.schema_gaps()}
