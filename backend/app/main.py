"""AZKT web/API service. Routers are auto-discovered from backend/app/routers/*.py
(each exposing `router`). The worker process is backend/worker.py."""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import pkgutil
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .auth.passkey import router as auth_router
from .core.config import settings
from .core.errors import DomainError
from .db import SessionLocal, engine

log = logging.getLogger("azkt")
logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))


def _include_routers(app: FastAPI) -> None:
    from . import routers as pkg
    for m in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda x: x.name):
        if m.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{pkg.__name__}.{m.name}")
        except Exception as e:  # noqa: BLE001
            if settings.is_production:
                raise
            log.warning("skipping router %s: %s: %s", m.name, type(e).__name__, e)
            continue
        r = getattr(mod, "router", None)
        if r is not None:
            app.include_router(r)


def _import_commands() -> None:
    """Import every module that registers commands / jobs / event handlers."""
    for pkg_name in ("backend.app.services", "backend.app.adapters", "backend.app.agent"):
        try:
            pkg = importlib.import_module(pkg_name)
        except ModuleNotFoundError:
            continue
        for m in pkgutil.iter_modules(pkg.__path__):
            if m.name.startswith("_"):
                continue
            try:
                importlib.import_module(f"{pkg_name}.{m.name}")
            except Exception as e:  # noqa: BLE001
                if settings.is_production:
                    raise
                log.warning("skipping module %s.%s: %s: %s", pkg_name, m.name, type(e).__name__, e)


def create_app() -> FastAPI:
    app = FastAPI(title="AZKT API", version="4.0")
    app.add_middleware(CORSMiddleware, allow_origins=[settings.PUBLIC_ORIGIN], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError):
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    _import_commands()
    app.include_router(auth_router)
    _include_routers(app)

    with contextlib.suppress(Exception):
        from .agent.mcp_server import mount_mcp
        mount_mcp(app)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "service": "api", "ts": datetime.now(timezone.utc).isoformat()}

    @app.get("/readyz")
    async def readyz():
        from sqlalchemy import text
        try:
            async with SessionLocal() as db:
                await db.execute(text("select 1"))
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return JSONResponse(status_code=503, content={"ok": False, "error": str(e)})

    @app.on_event("startup")
    async def _startup():
        if settings.ENV == "development":
            from .models import Base
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        if settings.LEGACY_BACKGROUND_LOOP:
            from .services import legacy_loop
            app.state.bg = asyncio.create_task(legacy_loop.run(SessionLocal))

    @app.on_event("shutdown")
    async def _shutdown():
        task = getattr(app.state, "bg", None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # Register the SPA last so API, auth, health and enrollment routes win.
    from .frontend import mount_frontend
    from .core.config import REPO_ROOT
    mount_frontend(app, REPO_ROOT / "frontend" / "dist")
    return app


app = create_app()


def main() -> None:
    """Serve the API.

    Railway assigns each deploy a port and injects it as `PORT`; a service that listens anywhere else
    never passes its health check. `PORT` therefore wins over `API_PORT`, which stays the setting for
    a droplet behind nginx where the port is ours to choose.
    """
    import os

    import uvicorn
    port = settings.API_PORT
    injected = (os.environ.get("PORT") or "").strip()
    if injected.isdigit():
        port = int(injected)
    uvicorn.run(app, host=settings.API_HOST, port=port)


if __name__ == "__main__":
    main()
