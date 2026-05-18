from __future__ import annotations

import shutil

import psutil
from sqlalchemy import text

from . import notion_sync


async def droplet() -> dict:
    du = shutil.disk_usage("/")
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "load_avg": psutil.getloadavg(),
        "mem_percent": psutil.virtual_memory().percent,
        "disk_used_percent": round(du.used / du.total * 100, 1),
        "disk_free_gb": round(du.free / 1e9, 1),
    }


async def postgres(db) -> dict:
    try:
        await db.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


async def overview(db) -> dict:
    return {
        "droplet": await droplet(),
        "postgres": await postgres(db),
        "notion_last_sync": await notion_sync.last_sync_at(db),
        "notion_schema_gaps": notion_sync.schema_gaps(),
    }
