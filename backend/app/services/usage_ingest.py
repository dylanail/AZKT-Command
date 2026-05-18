"""Ingest the append-only JSONL ledger into UsageEvent rows.

Idempotent: a deterministic dedupe_key means re-reading a ledger file (after
a crash or restart) never double-counts. Cost is computed at ingest from the
editable pricing table.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..models import UsageEvent
from . import pricing

LEDGER_DIR = Path(__file__).resolve().parents[3] / "usage"


def _key(row: dict) -> str:
    raw = f"{row.get('ts')}|{row.get('agent_key')}|{row.get('model')}|{row.get('input_tokens')}|{row.get('output_tokens')}|{row.get('session_id')}"
    return hashlib.sha1(raw.encode()).hexdigest()


async def ingest(db) -> int:
    if not LEDGER_DIR.exists():
        return 0
    n = 0
    for fp in sorted(LEDGER_DIR.glob("usage-*.jsonl")):
        for line in fp.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # partial last line after a crash — skip safely
            dk = _key(row)
            exists = await db.scalar(select(UsageEvent.id).where(UsageEvent.dedupe_key == dk))
            if exists:
                continue
            cost, _ = pricing.cost_usd(
                row.get("model", "unknown"),
                row.get("input_tokens", 0), row.get("output_tokens", 0),
                row.get("cache_write_tokens", 0), row.get("cache_read_tokens", 0),
            )
            db.add(UsageEvent(
                ts=datetime.fromtimestamp(row.get("ts", 0), tz=timezone.utc),
                agent_key=row.get("agent_key", "unknown"),
                model=row.get("model", "unknown"),
                input_tokens=row.get("input_tokens", 0),
                output_tokens=row.get("output_tokens", 0),
                cache_write_tokens=row.get("cache_write_tokens", 0),
                cache_read_tokens=row.get("cache_read_tokens", 0),
                cost_usd=cost,
                session_id=row.get("session_id"),
                run_kind=row.get("run_kind", "unknown"),
                dedupe_key=dk,
            ))
            n += 1
    await db.commit()
    return n
