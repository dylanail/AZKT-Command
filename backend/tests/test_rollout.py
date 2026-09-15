"""Rollout and operations acceptance (spec §13/§14): H07 legacy shadow/cutover/rollback, H08 staging must not reach
production destinations, H09 restore + replay of unfinished jobs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.app.core import destinations
from backend.app.core.config import settings
from backend.app.core.errors import Blocked
from backend.app.domain import jobs
from backend.app.domain.commands import REGISTRY
from backend.app.models.runtime import Job
from backend.tests.conftest import run_worker_once


# ── H07: legacy agents stay in shadow; the replacement is the only live path; rollback is a flag ──────────────
def test_H07_legacy_agents_are_shadowed_and_rollback_is_a_flag():
    from backend.app.main import create_app
    # The legacy Notion/OpenClaw polling loop never starts unless explicitly enabled (default off).
    assert settings.LEGACY_BACKGROUND_LOOP is False
    app = create_app()
    paths = set(app.openapi()["paths"])
    # Legacy routers stay mounted read-only for inspection/rollback of prompts...
    assert any(p.startswith("/api/legacy") or "/prompt/rollback" in p for p in paths), sorted(paths)[:5]
    # ...while the replacement reply workflow is the only registered write path for customer replies.
    assert "reply.prepare" in REGISTRY and "reply.submit_for_approval" in REGISTRY
    assert not any(name.startswith("legacy.") for name in REGISTRY)
    # Cutover is a single toggle: nothing in the runtime imports the legacy loop at module import time.
    import backend.app.agent.runtime as rt
    assert "legacy_loop" not in rt.__dict__


# ── H08: staging accidentally references a production destination ───────────────────────────────────────
class _Delivering:
    """Stands in for SMTP/Gmail: a transport that would really deliver."""
    sent: list = []

    async def send(self, msg):
        _Delivering.sent.append(msg.to)
        return {"provider": "fake-smtp", "accepted": True}


async def test_H08_staging_never_delivers_to_a_production_destination(monkeypatch):
    from backend.app.adapters import email as email_mod
    monkeypatch.setattr(settings, "ENV", "staging")
    monkeypatch.setattr(settings, "NON_PROD_DESTINATION_ALLOWLIST", "qa@example.test, @staging.example.test")
    monkeypatch.setattr(email_mod, "_transport", _Delivering())
    _Delivering.sent.clear()
    msg = email_mod.OutboundEmail(to=["dylan@azkeitrucks.com"], subject="Reminder", text="x")
    with pytest.raises(Blocked) as ei:
        await email_mod.send(msg)
    assert "production email destination" in str(ei.value) and _Delivering.sent == []
    # Allowlisted destinations (exact address or domain suffix) still deliver.
    await email_mod.send(email_mod.OutboundEmail(to=["qa@example.test"], subject="Reminder", text="x"))
    await email_mod.send(email_mod.OutboundEmail(to=["someone@staging.example.test"], subject="Reminder", text="x"))
    assert _Delivering.sent == [["qa@example.test"], ["someone@staging.example.test"]]
    # Production is not guarded (the allowlist is a non-production safety net only).
    monkeypatch.setattr(settings, "ENV", "production")
    destinations.assert_destination_allowed("email", "dylan@azkeitrucks.com")
    # The guard is generic: site and chat destinations use the same rule.
    monkeypatch.setattr(settings, "ENV", "staging")
    monkeypatch.setattr(settings, "NON_PROD_DESTINATION_ALLOWLIST", "https://staging.azkeitrucks.com, 12345")
    destinations.assert_destination_allowed("site", "https://staging.azkeitrucks.com/wp-json")
    destinations.assert_destination_allowed("telegram", "12345")
    with pytest.raises(Blocked):
        destinations.assert_destination_allowed("site", "https://azkeitrucks.com/wp-json")
    with pytest.raises(Blocked):
        destinations.assert_destination_allowed("telegram", "99999")


async def test_H08_non_delivering_transports_are_exempt(monkeypatch):
    from backend.app.adapters import email as email_mod
    monkeypatch.setattr(settings, "ENV", "staging")
    monkeypatch.setattr(settings, "NON_PROD_DESTINATION_ALLOWLIST", "")
    monkeypatch.setattr(email_mod, "_transport", email_mod.LogTransport())
    out = await email_mod.send(email_mod.OutboundEmail(to=["dylan@azkeitrucks.com"], subject="x", text="x"))
    assert out["accepted"] is False and out["provider"] == "log"  # logged, nothing left the process


# ── H09: restore to a clean environment and replay unfinished jobs exactly once ─────────────────────────
async def test_H09_unfinished_jobs_replay_exactly_once_after_a_crash(db):
    calls: list[dict] = []

    @jobs.job("test.h09_effect")
    async def _effect(ctx, payload):
        calls.append(payload)
        return {"done": payload["n"]}

    # Two jobs were queued before the "crash"; one had been claimed (lease held) when the process died.
    j1 = await jobs.enqueue(db, "test.h09_effect", {"n": 1}, dedupe_key="h09-1")
    j2 = await jobs.enqueue(db, "test.h09_effect", {"n": 2}, dedupe_key="h09-2")
    await db.commit()
    row = await db.get(Job, j1.id)
    row.state, row.lease_token = "running", "dead-worker"
    row.lease_until = datetime.now(timezone.utc) - timedelta(minutes=5)  # the crashed worker's lease has expired
    await db.commit()

    # A fresh worker on the restored database recovers the expired lease and runs both jobs...
    out = await run_worker_once()
    assert out["recovered"] == 1
    await db.refresh(row)
    r2 = await db.get(Job, j2.id)
    await db.refresh(r2)
    assert row.state == "done" and r2.state == "done"
    assert sorted(c["n"] for c in calls) == [1, 2]

    # ...and a second pass replays nothing: finished jobs are never re-run.
    await run_worker_once()
    assert sorted(c["n"] for c in calls) == [1, 2]
    states = (await db.execute(select(Job.state).where(Job.kind == "test.h09_effect"))).scalars().all()
    assert states == ["done", "done"]
    # A duplicate of a job that is still pending is dropped (dedupe_key), so a double-enqueue during recovery is
    # harmless; the same key may be reused once the earlier job has finished, which is a new job, not a replay.
    j3 = await jobs.enqueue(db, "test.h09_effect", {"n": 3}, dedupe_key="h09-3")
    dup = await jobs.enqueue(db, "test.h09_effect", {"n": 3}, dedupe_key="h09-3")
    await db.commit()
    assert j3 is not None and dup is None
