"""Droplet-era background loop (Notion reflection, OpenClaw health, usage ingest).
Only runs when LEGACY_BACKGROUND_LOOP=true; retained as a legacy adapter."""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone

from sqlalchemy import select

from ..core.config import settings
from ..models import AgentState
from . import agents as agent_svc
from . import notifications, notion_sync
from .usage_ingest import ingest as ingest_usage


async def _refresh_agent_health(db):
    for a in agent_svc.configured():
        if not a["configured"]:
            continue
        st = (await db.execute(select(AgentState).where(AgentState.agent_key == a["key"]))).scalar_one_or_none()
        if st is None:
            st = AgentState(agent_key=a["key"])
            db.add(st)
        try:
            h = (await agent_svc.call(a["key"], "GET", "/health"))["body"]
            st.last_health = h
            st.status = h.get("status", "unknown")
            now = datetime.now(timezone.utc)
            if st.status == "ok":
                st.last_ok_at = now
                st.error_since = None
            elif st.error_since is None:
                st.error_since = now
        except Exception as e:  # noqa: BLE001
            st.status = "unreachable"
            if st.error_since is None:
                st.error_since = datetime.now(timezone.utc)
            st.last_health = {"error": str(e)}
    await db.commit()


async def run(session_factory):
    while True:
        async with session_factory() as db:
            for step in (lambda: notion_sync.poll_once(db), lambda: ingest_usage(db),
                         lambda: notifications.evaluate(db), lambda: _refresh_agent_health(db)):
                with contextlib.suppress(Exception):
                    await step()
        await asyncio.sleep(settings.NOTION_POLL_SECONDS)
