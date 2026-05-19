"""Launch one agent's shim. Refuses to start on unconfigured (guessed) values.

Usage:  python -m shim.run_shim <agent_key>
Reads config/agents.yaml. Binds 127.0.0.1 only — never public.
"""
from __future__ import annotations

import sys
from pathlib import Path

import uvicorn
import yaml

from .app import create_app

CONFIG = Path(__file__).resolve().parent.parent / "config" / "agents.yaml"


def load(agent_key: str) -> dict:
    spec = yaml.safe_load(CONFIG.read_text())
    for a in spec.get("agents", []):
        if a.get("key") == agent_key:
            return a
    raise SystemExit(f"unknown agent key '{agent_key}' in {CONFIG}")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m shim.run_shim <agent_key>")
    key = sys.argv[1]
    a = load(key)
    if not a.get("agent_id") or not a.get("workspace") or not a.get("port"):
        raise SystemExit(
            f"agent '{key}' is not configured in {CONFIG}: fill agent_id, "
            f"workspace and port before starting. (Refusing to guess.)"
        )
    app = create_app(
        key,
        a["agent_id"],
        a["workspace"],
        a.get("model", ""),
        a.get("timer_unit", "") or "",
        a.get("service_unit", "") or "",
    )
    # LOOPBACK ONLY. Public access is exclusively via the reverse proxy.
    uvicorn.run(app, host="127.0.0.1", port=int(a["port"]), log_level="info")


if __name__ == "__main__":
    main()
