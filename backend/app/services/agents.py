from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import httpx
import yaml

from ..core.config import CONFIG_DIR

_AGENTS = CONFIG_DIR / "agents.yaml"


@lru_cache
def agent_map() -> dict[str, dict]:
    spec = yaml.safe_load(Path(_AGENTS).read_text())
    return {a["key"]: a for a in spec.get("agents", [])}


def _base(key: str) -> str:
    a = agent_map().get(key)
    if not a or not a.get("port"):
        raise KeyError(f"agent '{key}' not configured (fill config/agents.yaml)")
    return f"http://127.0.0.1:{a['port']}"


def configured() -> list[dict]:
    out = []
    for key, a in agent_map().items():
        out.append({
            "key": key,
            "configured": bool(a.get("agent_id") and a.get("workspace") and a.get("port")),
            "port": a.get("port") or None,
        })
    return out


async def call(key: str, method: str, path: str, json: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.request(method, _base(key) + path, json=json)
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:1000]}
        return {"status_code": r.status_code, "body": body}
