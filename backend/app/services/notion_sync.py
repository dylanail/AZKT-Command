"""Bidirectional Notion mirror. Postgres is the source of truth.

Property names come ONLY from config/notion_schema.json — never guessed. Any
mapping left blank is skipped and reported via schema_gaps() so the health
page shows exactly what still needs filling instead of silently breaking sync.

Direction:
  dashboard edit -> push to Notion immediately (push_vehicle/customer/irq)
  Notion edit    -> reflected by poll_once() within NOTION_POLL_SECONDS
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import select

from ..core.config import CONFIG_DIR, settings
from ..models import IRQ, Customer, SyncState, Vehicle

_SCHEMA = CONFIG_DIR / "notion_schema.json"
NOTION_API = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {settings.NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def load_schema() -> dict:
    return json.loads(_SCHEMA.read_text())


def schema_gaps() -> list[str]:
    """Human-readable list of unfilled mappings — shown on the health page."""
    s = load_schema()
    gaps: list[str] = []

    def walk(prefix: str, node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k.startswith("_"):
                    continue
                walk(f"{prefix}.{k}", v)
        elif isinstance(node, str) and node == "":
            gaps.append(prefix)

    walk("VEHICLES", s.get("VEHICLES", {}))
    walk("CUSTOMERS", s.get("CUSTOMERS", {}))
    walk("IRQS", s.get("IRQS", {}))
    if not settings.NOTION_TOKEN:
        gaps.append("env.NOTION_TOKEN")
    for db in ("VEHICLES", "CUSTOMERS", "IRQS"):
        if not getattr(settings, f"NOTION_{db}_DB_ID"):
            gaps.append(f"env.NOTION_{db}_DB_ID")
    return gaps


def _title(v: str) -> dict:
    return {"title": [{"text": {"content": v or ""}}]}


def _rich(v: str) -> dict:
    return {"rich_text": [{"text": {"content": v or ""}}]}


def _num(v):
    return {"number": v}


def _date(dt: datetime | None) -> dict:
    return {"date": {"start": dt.isoformat()} if dt else None}


def _status(opt: str) -> dict:
    return {"status": {"name": opt}}


def vehicle_to_properties(v: Vehicle) -> dict:
    """Only emit properties whose Notion names are configured."""
    s = load_schema()["VEHICLES"]
    f, props = s["fields"], {}

    def put(name: str, payload: dict):
        if name:
            props[name] = payload

    put(f.get("title", ""), _title(v.title))
    put(f.get("auction_url", ""), _rich(v.auction_url or ""))
    put(f.get("sold_price_usd", ""), _num(v.sold_price_usd))
    put(f.get("landed_cost_usd", ""), _num(v.landed_cost_usd))
    put(f.get("won_date", ""), _date(v.won_date))
    put(f.get("sold_date", ""), _date(v.sold_date))

    stage_cfg = s["stage"]
    opt_label = stage_cfg["options"].get(v.stage)
    if stage_cfg.get("property") and opt_label:
        props[stage_cfg["property"]] = _status(opt_label)

    # Discrete per-stage entry timestamps.
    for skey, prop in s["stage_transition_timestamps"].items():
        if skey.startswith("_") or not prop:
            continue
        ts = (v.stage_timestamps or {}).get(skey)
        if ts:
            props[prop] = {"date": {"start": ts}}
    return props


async def _patch_page(page_id: str, properties: dict) -> None:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.patch(f"{NOTION_API}/pages/{page_id}", headers=HEADERS,
                           json={"properties": properties})
        r.raise_for_status()


async def _create_page(db_id: str, properties: dict) -> str:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{NOTION_API}/pages", headers=HEADERS,
                          json={"parent": {"database_id": db_id}, "properties": properties})
        r.raise_for_status()
        return r.json()["id"]


async def push_vehicle(v: Vehicle) -> str | None:
    """Push a dashboard-side vehicle to Notion. No-op if unconfigured."""
    if not settings.NOTION_TOKEN or not settings.NOTION_VEHICLES_DB_ID:
        return None
    props = vehicle_to_properties(v)
    if not props:
        return None
    if v.notion_page_id:
        await _patch_page(v.notion_page_id, props)
        return v.notion_page_id
    return await _create_page(settings.NOTION_VEHICLES_DB_ID, props)


# ── Notion -> dashboard reflection ─────────────────────────────────────────
def _read_prop(page: dict, name: str):
    if not name:
        return None
    p = page.get("properties", {}).get(name)
    if not p:
        return None
    t = p.get("type")
    val = p.get(t)
    if t == "title" or t == "rich_text":
        return "".join(x.get("plain_text", "") for x in val) if val else ""
    if t in ("number",):
        return val
    if t == "date":
        return val.get("start") if val else None
    if t == "status" or t == "select":
        return val.get("name") if val else None
    return val


async def _query_db(db_id: str, since_iso: str | None):
    body: dict = {"page_size": 100}
    if since_iso:
        body["filter"] = {"timestamp": "last_edited_time",
                          "last_edited_time": {"on_or_after": since_iso}}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{NOTION_API}/databases/{db_id}/query", headers=HEADERS, json=body)
        r.raise_for_status()
        return r.json().get("results", [])


async def poll_once(db) -> int:
    """Reflect Notion edits made since last poll back into Postgres.

    Postgres stays source of truth: we only ingest fields, never delete, and
    dashboard pushes win on the next edit.
    """
    if not settings.NOTION_TOKEN:
        return 0
    s = load_schema()
    cursor = (await db.execute(select(SyncState).where(SyncState.key == "notion_cursor"))).scalar_one_or_none()
    since = cursor.value.get("since") if cursor else None
    changed = 0

    if settings.NOTION_VEHICLES_DB_ID and s["VEHICLES"]["fields"].get("title"):
        vf = s["VEHICLES"]["fields"]
        stage_cfg = s["VEHICLES"]["stage"]
        rev_opts = {label: key for key, label in stage_cfg["options"].items()}
        for page in await _query_db(settings.NOTION_VEHICLES_DB_ID, since):
            pid = page["id"]
            row = (await db.execute(select(Vehicle).where(Vehicle.notion_page_id == pid))).scalar_one_or_none()
            if row is None:
                row = Vehicle(notion_page_id=pid)
                db.add(row)
            row.title = _read_prop(page, vf.get("title", "")) or row.title
            sp = _read_prop(page, vf.get("sold_price_usd", ""))
            if sp is not None:
                row.sold_price_usd = sp
            lc = _read_prop(page, vf.get("landed_cost_usd", ""))
            if lc is not None:
                row.landed_cost_usd = lc
            if stage_cfg.get("property"):
                label = _read_prop(page, stage_cfg["property"])
                if label in rev_opts:
                    row.stage = rev_opts[label]
            changed += 1

    now = datetime.now(timezone.utc).isoformat()
    if cursor is None:
        cursor = SyncState(key="notion_cursor", value={})
        db.add(cursor)
    cursor.value = {"since": now}
    cursor.updated_at = datetime.now(timezone.utc)
    await _set_sync_state(db, "notion_last_sync", {"at": now})
    await db.commit()
    return changed


async def _set_sync_state(db, key: str, value: dict) -> None:
    st = (await db.execute(select(SyncState).where(SyncState.key == key))).scalar_one_or_none()
    if st is None:
        st = SyncState(key=key)
        db.add(st)
    st.value = value
    st.updated_at = datetime.now(timezone.utc)


async def last_sync_at(db) -> str | None:
    st = (await db.execute(select(SyncState).where(SyncState.key == "notion_last_sync"))).scalar_one_or_none()
    return st.value.get("at") if st else None
