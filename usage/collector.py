"""Backfill token usage from OpenClaw session transcripts into the ledger.

OpenClaw stores per-agent session transcripts under
  $OPENCLAW_HOME/agents/<agentId>/sessions/
This collector tails those files and extracts any per-call usage it finds
(model + input/output tokens), appending to the same crash-safe JSONL ledger
the shim writes. It is defensive about transcript shape because the exact
schema varies by OpenClaw build — it looks for usage-like objects rather than
assuming one layout. A cursor file prevents double-counting across restarts.

Run:  python -m usage.collector            (one pass)
      python -m usage.collector --watch    (loop every 30s)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shim import usage_ledger  # noqa: E402

import yaml  # noqa: E402

OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", "~/.openclaw")).expanduser()
CONFIG = Path(__file__).resolve().parent.parent / "config" / "agents.yaml"
CURSOR = Path(__file__).resolve().parent / ".collector-cursor.json"

_USAGE_KEYS = ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens")


def _load_cursor() -> dict:
    if CURSOR.exists():
        try:
            return json.loads(CURSOR.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_cursor(c: dict) -> None:
    CURSOR.write_text(json.dumps(c))


def _iter_usage(obj):
    """Yield (model, in, out, cw, cr) from any usage-shaped dict, recursively."""
    if isinstance(obj, dict):
        if any(k in obj for k in _USAGE_KEYS):
            yield (
                obj.get("model") or obj.get("model_id") or "",
                obj.get("input_tokens", obj.get("prompt_tokens", 0)) or 0,
                obj.get("output_tokens", obj.get("completion_tokens", 0)) or 0,
                obj.get("cache_creation_input_tokens", obj.get("cache_write_tokens", 0)) or 0,
                obj.get("cache_read_input_tokens", obj.get("cache_read_tokens", 0)) or 0,
            )
        for v in obj.values():
            yield from _iter_usage(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_usage(v)


def _scan_file(path: Path):
    """Yield usage tuples from a transcript (JSONL preferred, JSON fallback)."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return
    parsed_any = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield from _iter_usage(json.loads(line))
            parsed_any = True
        except json.JSONDecodeError:
            continue
    if not parsed_any:
        try:
            yield from _iter_usage(json.loads(text))
        except json.JSONDecodeError:
            pass


def collect_once() -> int:
    agents = {a["key"]: a for a in yaml.safe_load(CONFIG.read_text()).get("agents", [])}
    cursor = _load_cursor()
    emitted = 0
    for key, a in agents.items():
        agent_id = a.get("agent_id")
        if not agent_id:
            continue
        sess_dir = OPENCLAW_HOME / "agents" / agent_id / "sessions"
        if not sess_dir.exists():
            continue
        for f in sorted(sess_dir.rglob("*")):
            if not f.is_file():
                continue
            stat = f.stat()
            ckey = str(f)
            seen = cursor.get(ckey, {"mtime": 0, "size": 0})
            if stat.st_mtime <= seen["mtime"] and stat.st_size <= seen["size"]:
                continue
            for model, ti, to, cw, cr in _scan_file(f):
                usage_ledger.record(
                    agent_key=key,
                    agent_id=agent_id,
                    model=model or "unknown",
                    input_tokens=ti,
                    output_tokens=to,
                    cache_write_tokens=cw,
                    cache_read_tokens=cr,
                    session_id=f.stem,
                    run_kind="transcript-backfill",
                )
                emitted += 1
            cursor[ckey] = {"mtime": stat.st_mtime, "size": stat.st_size}
    _save_cursor(cursor)
    return emitted


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=int, default=30)
    args = ap.parse_args()
    if args.watch:
        while True:
            n = collect_once()
            print(f"[collector] emitted {n} usage rows")
            time.sleep(args.interval)
    else:
        print(f"[collector] emitted {collect_once()} usage rows")


if __name__ == "__main__":
    main()
