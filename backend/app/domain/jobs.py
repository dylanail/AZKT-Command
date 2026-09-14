"""Durable Postgres job queue: leases, fencing, retries, dedupe (spec §13.2)."""
from __future__ import annotations

import asyncio
import logging
import random
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.ids import new_id
from ..models.runtime import Job

log = logging.getLogger("azkt.jobs")

HANDLERS: dict[str, Callable[..., Awaitable]] = {}
# Periodic sweeps run by backend/worker.py: name -> (async fn(session_factory), interval seconds)
SWEEPS: dict[str, tuple] = {}


def job(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


def sweep(name: str, interval_seconds: int):
    """Register a periodic worker sweep: `@sweep("reminders.deliver_due", 15)` on
    `async def fn(session_factory) -> Any`. The worker runs it every interval and in run_once()."""
    def deco(fn):
        SWEEPS[name] = (fn, interval_seconds)
        return fn
    return deco


@dataclass
class JobContext:
    db: AsyncSession
    job: Job
    worker_id: str
    lease_token: str

    async def heartbeat(self, seconds: int | None = None) -> bool:
        """Extend the lease; returns False if a newer fencing token exists (lost lease)."""
        until = datetime.now(timezone.utc) + timedelta(seconds=seconds or settings.JOB_LEASE_SECONDS)
        res = await self.db.execute(update(Job).where(Job.id == self.job.id, Job.lease_token == self.lease_token)
                                    .values(lease_until=until))
        await self.db.commit()
        return res.rowcount == 1


async def enqueue(db: AsyncSession, kind: str, payload: dict | None = None, *, run_at: datetime | None = None,
                  dedupe_key: str | None = None, priority: int = 0, max_attempts: int = 5,
                  correlation_id: str | None = None) -> Job | None:
    """Insert a job. With dedupe_key, an existing queued/running job wins and None is returned."""
    j = Job(kind=kind, payload=payload or {}, run_at=run_at or datetime.now(timezone.utc), dedupe_key=dedupe_key,
            priority=priority, max_attempts=max_attempts, correlation_id=correlation_id)
    if dedupe_key:
        existing = (await db.execute(select(Job).where(Job.dedupe_key == dedupe_key))).scalar_one_or_none()
        if existing is not None:
            if existing.state in ("queued", "running"):
                return None
            # finished job with same key: free the key by renaming it
            existing.dedupe_key = f"{dedupe_key}#{existing.id[:8]}"
            await db.flush()
    db.add(j)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return None
    return j


async def claim(db: AsyncSession, worker_id: str, limit: int) -> list[tuple[Job, str]]:
    now = datetime.now(timezone.utc)
    rows = (await db.execute(
        select(Job).where(Job.state == "queued", Job.run_at <= now)
        .order_by(Job.priority.desc(), Job.run_at).limit(limit).with_for_update(skip_locked=True)
    )).scalars().all()
    out = []
    for j in rows:
        token = new_id()
        j.state = "running"
        j.lease_token = token
        j.lease_until = now + timedelta(seconds=settings.JOB_LEASE_SECONDS)
        j.fencing_token = (j.fencing_token or 0) + 1
        j.attempts = (j.attempts or 0) + 1
        j.worker_id = worker_id
        out.append((j, token))
    await db.commit()
    return out


async def recover_expired_leases(db: AsyncSession) -> int:
    """Running jobs whose lease expired go back to queued (crash recovery)."""
    now = datetime.now(timezone.utc)
    res = await db.execute(update(Job).where(Job.state == "running", Job.lease_until < now)
                           .values(state="queued", lease_token=None, lease_until=None, last_error="lease expired; requeued"))
    await db.commit()
    return res.rowcount or 0


def _backoff(attempt: int) -> timedelta:
    base = min(2 ** attempt, 300)
    return timedelta(seconds=base + random.uniform(0, base * 0.25))


async def run_one(session_factory, j: Job, token: str, worker_id: str) -> None:
    handler = HANDLERS.get(j.kind)
    async with session_factory() as db:
        job_row = await db.get(Job, j.id)
        if job_row is None or job_row.lease_token != token:
            return  # lost the lease before we started
        if handler is None:
            job_row.state = "failed"
            job_row.last_error = f"no handler for {j.kind}"
            job_row.finished_at = datetime.now(timezone.utc)
            await db.commit()
            return
        ctx = JobContext(db=db, job=job_row, worker_id=worker_id, lease_token=token)
        try:
            result = await asyncio.wait_for(handler(ctx, dict(job_row.payload or {})), timeout=settings.MODEL_RUN_TIMEOUT_SECONDS * 4)
            await db.rollback() if db.in_transaction() and False else None
            # fenced completion: only the lease holder may finish the job
            res = await db.execute(update(Job).where(Job.id == job_row.id, Job.lease_token == token)
                                   .values(state="done", finished_at=datetime.now(timezone.utc),
                                           result=(result if isinstance(result, dict) else {}), lease_token=None))
            await db.commit()
            if res.rowcount != 1:
                log.warning("job %s finished by a stale worker; result discarded", job_row.id)
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            err = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-2000:]}"
            async with session_factory() as db2:
                row = await db2.get(Job, job_row.id)
                if row is None or row.lease_token != token:
                    return
                if row.attempts >= row.max_attempts:
                    row.state = "failed"
                    row.finished_at = datetime.now(timezone.utc)
                else:
                    row.state = "queued"
                    row.run_at = datetime.now(timezone.utc) + _backoff(row.attempts)
                row.last_error = err[:4000]
                row.lease_token = None
                await db2.commit()
            log.exception("job %s (%s) failed", job_row.id, job_row.kind)


async def run_due(session_factory, worker_id: str, limit: int | None = None) -> int:
    """Claim and run due jobs once. Used by the worker loop and by tests."""
    async with session_factory() as db:
        claimed = await claim(db, worker_id, limit or settings.WORKER_BATCH)
    for j, token in claimed:
        await run_one(session_factory, j, token, worker_id)
    return len(claimed)


async def queue_depth(db: AsyncSession) -> dict:
    rows = (await db.execute(text("select state, count(*) from jobs group by state"))).all()
    return {s: int(c) for s, c in rows}
