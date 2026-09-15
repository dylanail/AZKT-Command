from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import psutil
from sqlalchemy import text

from ..core.config import settings
from . import notion_sync

# Railway sets these in every service container; a mounted volume also sets its mount path.
RAILWAY_MARKERS = ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_SERVICE_ID", "RAILWAY_PROJECT_ID")
RAILWAY_VOLUME_ENV = "RAILWAY_VOLUME_MOUNT_PATH"


async def droplet() -> dict:
    du = shutil.disk_usage("/")
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "load_avg": psutil.getloadavg(),
        "mem_percent": psutil.virtual_memory().percent,
        "disk_used_percent": round(du.used / du.total * 100, 1),
        "disk_free_gb": round(du.free / 1e9, 1),
    }


def on_railway() -> bool:
    return any(os.environ.get(k) for k in RAILWAY_MARKERS)


def storage_state() -> dict:
    """Is the place photos live going to survive the next deploy?

    This matters more than it looks. Railway gives a service an ephemeral container filesystem: with
    `STORAGE_BACKEND=local` and no mounted volume, every uploaded photo is thrown away on the next
    deploy. Nothing fails loudly at that moment — it surfaces later as a listing that cannot be
    published because its approved photos have no bytes to upload to the website. So the
    configuration is reported honestly here rather than discovered during a publish.
    """
    backend = settings.STORAGE_BACKEND
    out: dict = {"backend": backend, "durable": None, "writable": None, "reason": None,
                 "platform": "railway" if on_railway() else "host"}
    if backend == "s3":
        out["bucket"] = settings.S3_BUCKET or None
        if settings.S3_BUCKET:
            out["durable"] = True
        else:
            out["durable"] = False
            out["reason"] = "STORAGE_BACKEND is s3 but S3_BUCKET is empty, so files fall back to local disk"
        return out
    data_dir = str(settings.DATA_DIR)
    out["path"] = data_dir
    volume = os.environ.get(RAILWAY_VOLUME_ENV) or ""
    out["volume_mount"] = volume or None
    if out["platform"] == "railway":
        on_volume = bool(volume) and _is_within(data_dir, volume)
        out["durable"] = on_volume
        if not on_volume:
            out["reason"] = (f"DATA_DIR ({data_dir}) is on the container filesystem, which Railway replaces on "
                             f"every deploy. Mount a volume and point DATA_DIR at it, or set STORAGE_BACKEND=s3 "
                             f"with S3_* credentials. Uploaded photos are lost on the next deploy as configured.")
    else:
        out["durable"] = True      # an ordinary host disk survives a restart
    try:
        p = Path(data_dir) / f".probe-{uuid.uuid4().hex[:8]}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"ok")
        p.unlink()
        out["writable"] = True
    except Exception as e:  # noqa: BLE001
        out["writable"] = False
        out["reason"] = out["reason"] or f"file storage is not writable: {e}"
    return out


def _is_within(path: str, root: str) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


async def postgres(db) -> dict:
    try:
        await db.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


async def overview(db) -> dict:
    return {
        "droplet": await droplet(),
        "storage": storage_state(),
        "postgres": await postgres(db),
        "notion_last_sync": await notion_sync.last_sync_at(db),
        "notion_schema_gaps": notion_sync.schema_gaps(),
    }
