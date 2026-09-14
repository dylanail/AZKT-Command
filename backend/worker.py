"""Always-on worker: durable jobs, scheduled deliveries, sweeps, outbox (spec §13.1–13.2).

    python -m backend.worker            # loop
    python -m backend.worker --once     # one pass (tests / cron)
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import socket
import uuid
from datetime import datetime, timezone

from .app.core.config import settings
from .app.db import SessionLocal
from .app.domain import events as events_mod
from .app.domain import jobs as jobs_mod
from .app.main import _import_commands

log = logging.getLogger("azkt.worker")

# Periodic sweeps are registered in backend.app.domain.jobs (SWEEPS / @sweep) so services can
# register them without importing this module.
SWEEPS = jobs_mod.SWEEPS
sweep = jobs_mod.sweep


async def _heartbeat(session_factory, worker_id: str, detail: dict) -> None:
    from sqlalchemy import select
    from .app.models.legacy import SyncState
    try:
        async with session_factory() as db:
            row = (await db.execute(select(SyncState).where(SyncState.key == "worker_heartbeat"))).scalar_one_or_none()
            if row is None:
                row = SyncState(key="worker_heartbeat", value={})
                db.add(row)
            row.value = {"worker_id": worker_id, **detail}
            row.updated_at = datetime.now(timezone.utc)
            await db.commit()
    except Exception:  # noqa: BLE001
        log.exception("heartbeat failed")


async def run_once(session_factory=None) -> dict:
    """One full pass: recover leases, run due jobs, run every sweep, dispatch outbox."""
    sf = session_factory or SessionLocal
    worker_id = f"{socket.gethostname()}:{uuid.uuid4().hex[:6]}"
    out: dict = {}
    async with sf() as db:
        out["recovered"] = await jobs_mod.recover_expired_leases(db)
    out["sweeps"] = {}
    for name, (fn, _) in SWEEPS.items():
        try:
            out["sweeps"][name] = await fn(sf)
        except Exception as e:  # noqa: BLE001
            log.exception("sweep %s failed", name)
            out["sweeps"][name] = f"error: {e}"
    out["jobs"] = await jobs_mod.run_due(sf, worker_id)
    out["events"] = await events_mod.dispatch_pending(sf)
    await _heartbeat(sf, worker_id, {"jobs": out["jobs"], "events": out["events"], "once": True})
    return out


async def loop() -> None:
    _import_commands()
    stop = asyncio.Event()
    with contextlib.suppress(NotImplementedError):
        for s in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(s, stop.set)
    worker_id = f"{socket.gethostname()}:{uuid.uuid4().hex[:6]}"
    last_run: dict[str, datetime] = {}
    log.info("worker %s started", worker_id)
    while not stop.is_set():
        try:
            async with SessionLocal() as db:
                await jobs_mod.recover_expired_leases(db)
            now = datetime.now(timezone.utc)
            for name, (fn, interval) in SWEEPS.items():
                if name not in last_run or (now - last_run[name]).total_seconds() >= interval:
                    last_run[name] = now
                    try:
                        await fn(SessionLocal)
                    except Exception:  # noqa: BLE001
                        log.exception("sweep %s failed", name)
            n = await jobs_mod.run_due(SessionLocal, worker_id)
            m = await events_mod.dispatch_pending(SessionLocal)
            await _heartbeat(SessionLocal, worker_id, {"jobs": n, "events": m})
            if n == 0 and m == 0:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=settings.WORKER_POLL_SECONDS)
        except Exception:  # noqa: BLE001
            log.exception("worker loop error")
            await asyncio.sleep(settings.WORKER_POLL_SECONDS)
    log.info("worker %s stopping (graceful: no new claims; leases expire)", worker_id)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
    if args.once:
        _import_commands()
        print(asyncio.run(run_once()))
    else:
        asyncio.run(loop())


if __name__ == "__main__":
    main()
