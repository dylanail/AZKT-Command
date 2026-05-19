from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .auth.passkey import router as auth_router
from .core.config import settings
from .db import Base, SessionLocal, engine
from .models import AgentState  # noqa: F401  (ensure metadata import)
from .routers.agents import router as agents_router
from .routers.enroll import router as enroll_router
from .routers.misc import router as misc_router
from .routers.vehicles import router as vehicles_router
from .services import agents as agent_svc
from .services import notifications, notion_sync
from .services.usage_ingest import ingest as ingest_usage

app = FastAPI(title="AZKT Command API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.PUBLIC_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(enroll_router)
app.include_router(agents_router)
app.include_router(vehicles_router)
app.include_router(misc_router)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}


async def _background():
    """Single loop: Notion reflection, usage ingest, notif eval, agent health.

    Each step is independently guarded so one failing integration (e.g. Notion
    not yet configured) never stalls the others.
    """
    while True:
        async with SessionLocal() as db:
            for step in (
                lambda: notion_sync.poll_once(db),
                lambda: ingest_usage(db),
                lambda: notifications.evaluate(db),
                lambda: _refresh_agent_health(db),
            ):
                with contextlib.suppress(Exception):
                    await step()
        await asyncio.sleep(settings.NOTION_POLL_SECONDS)


async def _refresh_agent_health(db):
    from sqlalchemy import select
    for a in agent_svc.configured():
        if not a["configured"]:
            continue
        st = (await db.execute(
            select(AgentState).where(AgentState.agent_key == a["key"])
        )).scalar_one_or_none()
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
                st.error_since = now  # start the "erroring > N min" clock
        except Exception as e:  # noqa: BLE001
            st.status = "unreachable"
            if st.error_since is None:
                st.error_since = datetime.now(timezone.utc)
            st.last_health = {"error": str(e)}
    await db.commit()


@app.on_event("startup")
async def _startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app.state.bg = asyncio.create_task(_background())


@app.on_event("shutdown")
async def _shutdown():
    task = getattr(app.state, "bg", None)
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def main() -> None:
    import uvicorn
    # LOOPBACK ONLY — public access exclusively via the reverse proxy.
    uvicorn.run(app, host=settings.API_HOST, port=settings.API_PORT)


if __name__ == "__main__":
    main()
