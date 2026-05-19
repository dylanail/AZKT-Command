"""Per-agent HTTP sidecar. One process per OpenClaw agent, loopback-bound.

  GET  /health    systemd timer state + gateway reachability + verdict
  GET  /state     timer detail + OpenClaw cron jobs + gateway
  POST /run        one-off run (starts the systemd --user service)
  POST /chat       message the agent via the Gateway; logs real token usage
  POST /schedule/{pause|resume}   stop/start the systemd --user timer
  GET  /prompt     current SOUL.md + version history
  PUT  /prompt     edit SOUL.md (new version commit)
  POST /prompt/rollback   roll back to a prior version
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .openclaw_client import OpenClawClient
from .prompt_history import PromptHistory
from . import usage_ledger


class ChatIn(BaseModel):
    message: str


class PromptIn(BaseModel):
    content: str
    message: str = "prompt update via dashboard"


class RollbackIn(BaseModel):
    sha: str


def create_app(
    agent_key: str,
    agent_id: str,
    workspace: str,
    model: str,
    timer_unit: str = "",
    service_unit: str = "",
) -> FastAPI:
    app = FastAPI(title=f"AZKT shim · {agent_key}")
    oc = OpenClawClient(agent_id, model, timer_unit, service_unit)
    hist = PromptHistory(Path(workspace))
    hist.ensure_repo()

    @app.get("/health")
    async def health():
        timer = oc.timer_health()
        gw = await oc.gateway_health()
        degraded = bool(timer.get("degraded")) or not gw.get("reachable", True)
        return {
            "agent_key": agent_key,
            "agent_id": agent_id,
            "model": model,
            "ts": time.time(),
            "timer": timer,
            "gateway": gw,
            "status": "degraded" if degraded else "ok",
        }

    @app.get("/state")
    async def state():
        return {
            "agent_key": agent_key,
            "agent_id": agent_id,
            "timer": oc.timer_health(),
            "cron_jobs": oc.cron_jobs(),
            "gateway": await oc.gateway_health(),
        }

    @app.post("/run")
    def run():
        return oc.run_once()

    @app.post("/chat")
    async def chat(body: ChatIn):
        res = await oc.chat(body.message)
        u = res.get("usage") or {}
        if u:
            # Real usage from the Gateway response -> crash-safe ledger.
            usage_ledger.record(
                agent_key=agent_key,
                agent_id=agent_id,
                model=u.get("model") or model,
                input_tokens=u.get("prompt_tokens", u.get("input_tokens", 0)),
                output_tokens=u.get("completion_tokens", u.get("output_tokens", 0)),
                cache_read_tokens=u.get("cache_read_input_tokens", 0),
                cache_write_tokens=u.get("cache_creation_input_tokens", 0),
                run_kind="dashboard-chat",
            )
        return res

    @app.post("/schedule/{action}")
    def schedule(action: str):
        if action not in ("pause", "resume"):
            raise HTTPException(400, "action must be pause|resume")
        return oc.set_timer_enabled(action == "resume")

    @app.get("/prompt")
    def get_prompt():
        return {"content": hist.read(), "history": hist.history()}

    @app.put("/prompt")
    def put_prompt(body: PromptIn):
        sha = hist.write(body.content, body.message)
        return {"sha": sha, "history": hist.history()}

    @app.post("/prompt/rollback")
    def rollback(body: RollbackIn):
        sha = hist.rollback(body.sha)
        return {"sha": sha, "history": hist.history()}

    return app
