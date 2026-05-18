"""Per-agent HTTP sidecar. One process per OpenClaw agent, loopback-bound.

Endpoints the dashboard backend consumes:
  GET  /health   cron/heartbeat health + silent-failure verdict
  GET  /state    last run, schedule enabled, gateway reachability
  POST /run       trigger a one-off run (no SSH needed)
  POST /chat      send a message, get the reply
  GET  /prompt    current SOUL.md + version history
  PUT  /prompt    edit SOUL.md (new version commit)
  POST /prompt/rollback   roll back to a prior version
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .openclaw_client import OpenClawClient
from .prompt_history import PromptHistory


class ChatIn(BaseModel):
    message: str


class PromptIn(BaseModel):
    content: str
    message: str = "prompt update via dashboard"


class RollbackIn(BaseModel):
    sha: str


def create_app(agent_key: str, agent_id: str, workspace: str) -> FastAPI:
    app = FastAPI(title=f"AZKT shim · {agent_key}")
    oc = OpenClawClient(agent_id)
    hist = PromptHistory(Path(workspace))
    hist.ensure_repo()

    @app.get("/health")
    async def health():
        cron = oc.cron_status()
        hb = oc.heartbeat_last()
        # Silent-failure verdict: the Inbox-timer-stopped-since-May-5 class.
        degraded = bool(cron.get("scheduler_disabled"))
        return {
            "agent_key": agent_key,
            "agent_id": agent_id,
            "ts": time.time(),
            "scheduler_disabled": cron.get("scheduler_disabled"),
            "heartbeat_last": hb,
            "cron": cron,
            "status": "degraded" if degraded else "ok",
        }

    @app.get("/state")
    async def state():
        return {
            "agent_key": agent_key,
            "agent_id": agent_id,
            "heartbeat_last": oc.heartbeat_last(),
            "gateway": await oc.gateway_health(),
        }

    @app.post("/run")
    def run():
        return oc.run_once()

    @app.post("/chat")
    def chat(body: ChatIn):
        return oc.chat(body.message)

    @app.post("/schedule/{state}")
    def schedule(state: str):
        if state not in ("pause", "resume"):
            raise HTTPException(400, "state must be pause|resume")
        return oc.set_schedule_enabled(state == "resume")

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
