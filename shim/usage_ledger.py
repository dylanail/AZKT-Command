"""Crash-safe, append-only token-usage ledger.

One JSON object per line. Append-only is deliberate: a crash mid-write loses
at most the last line, never corrupts history or blocks the agent. The backend
reads this ledger to compute burn rate / cost; it is the retrofit that makes
cost reporting possible since OpenClaw agents don't log usage themselves.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

LEDGER_DIR = Path(os.environ.get("USAGE_LEDGER_DIR", Path(__file__).resolve().parent.parent / "usage"))
_lock = threading.Lock()


def _ledger_path() -> Path:
    # Daily file: bounded size, trivially rotated, safe to tail.
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    return LEDGER_DIR / f"usage-{time.strftime('%Y-%m-%d')}.jsonl"


def record(
    *,
    agent_key: str,
    agent_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
    session_id: str | None = None,
    run_kind: str = "unknown",
    outcome: dict[str, Any] | None = None,
) -> None:
    """Append one usage event. Never raises into the caller's hot path."""
    row = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "agent_key": agent_key,
        "agent_id": agent_id,
        "model": model,
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "cache_write_tokens": int(cache_write_tokens or 0),
        "cache_read_tokens": int(cache_read_tokens or 0),
        "session_id": session_id,
        "run_kind": run_kind,
        "outcome": outcome or {},
    }
    line = json.dumps(row, separators=(",", ":")) + "\n"
    try:
        with _lock:
            with open(_ledger_path(), "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
    except Exception:
        # Logging must never break the agent. Drop silently; the collector
        # backfill from session transcripts is the safety net.
        pass
