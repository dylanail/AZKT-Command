from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth.passkey import current_user
from ..services import agents

router = APIRouter(prefix="/api/agents", tags=["agents"], dependencies=[Depends(current_user)])


@router.get("")
async def list_agents():
    out = []
    for a in agents.configured():
        entry = dict(a)
        if a["configured"]:
            try:
                entry["health"] = (await agents.call(a["key"], "GET", "/health"))["body"]
            except Exception as e:  # noqa: BLE001
                entry["health"] = {"status": "unreachable", "error": str(e)}
        out.append(entry)
    return out


@router.get("/{key}")
async def agent_detail(key: str):
    return {
        "health": (await agents.call(key, "GET", "/health"))["body"],
        "state": (await agents.call(key, "GET", "/state"))["body"],
        "prompt": (await agents.call(key, "GET", "/prompt"))["body"],
    }


@router.post("/{key}/run")
async def run(key: str):
    return (await agents.call(key, "POST", "/run"))["body"]


@router.post("/{key}/chat")
async def chat(key: str, body: dict):
    return (await agents.call(key, "POST", "/chat", {"message": body.get("message", "")}))["body"]


@router.post("/{key}/schedule/{state}")
async def schedule(key: str, state: str):
    return (await agents.call(key, "POST", f"/schedule/{state}"))["body"]


@router.put("/{key}/prompt")
async def set_prompt(key: str, body: dict):
    return (await agents.call(key, "PUT", "/prompt", body))["body"]


@router.post("/{key}/prompt/rollback")
async def rollback(key: str, body: dict):
    return (await agents.call(key, "POST", "/prompt/rollback", body))["body"]
