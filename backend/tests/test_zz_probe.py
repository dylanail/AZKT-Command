"""Scratch probes for the review (not part of the suite)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy import delete, func, select

from backend.app import db as dbmod
from backend.app.adapters import gmail as gmail_mod
from backend.app.adapters.gmail import FakeGmail
from backend.app.core.config import settings
from backend.app.core.errors import Unsupported
from backend.app.domain import jobs
from backend.app.models.comms import Connection, Conversation, Draft, Message, ProviderEvent, SyncCursor
from backend.app.models.runtime import Job
from backend.app.services import connections as conn_svc
from backend.tests import fixtures_gmail as fx
from backend.tests.conftest import ctx_for, login, run_worker_once

FAKES: dict[str, FakeGmail] = {}


def _factory(db, conn):
    fake = FAKES.get(conn.provider)
    if fake is None:
        raise Unsupported("no gmail fixture", setup_blocked=True)
    fake.conn = conn
    fake.db = db
    return fake


@pytest_asyncio.fixture(autouse=True, scope="module")
async def _gmail_module():
    gmail_mod.FACTORY = _factory
    yield
    gmail_mod.FACTORY = None
    FAKES.clear()


async def _reset(db, conn):
    conv_ids = (await db.execute(select(Conversation.id).where(Conversation.connection_id == conn.id))).scalars().all()
    if conv_ids:
        await db.execute(delete(Draft).where(Draft.conversation_id.in_(conv_ids)))
        await db.execute(delete(Message).where(Message.conversation_id.in_(conv_ids)))
        await db.execute(delete(Conversation).where(Conversation.id.in_(conv_ids)))
    await db.execute(delete(Message).where(Message.connection_id == conn.id))
    await db.execute(delete(SyncCursor).where(SyncCursor.connection_id == conn.id))


async def make_conn(db, provider, identity, config=None):
    now = datetime.now(timezone.utc)
    conn = await conn_svc.get(db, provider, create=True)
    await _reset(db, conn)
    conn.account_identity = identity
    conn.status = "connected"
    conn.config = config or {}
    conn.capabilities = {"send": True, "drafts": True, "labels": False}
    conn.last_success_at = now - timedelta(minutes=1)
    conn.coverage_gaps = []
    conn.excluded_counts = {}
    conn.catch_up_state = {}
    conn.failure = {}
    await db.commit()
    return conn


async def sync(db, conn):
    await jobs.enqueue(db, "gmail.sync", {"connection_id": conn.id}, dedupe_key=f"gmail.sync:{conn.id}")
    await db.commit()
    await run_worker_once()
    await db.refresh(conn)


async def test_probe_excluded_counts_double_on_replay(db, owner):
    conn = await make_conn(db, "gmail_personal", fx.PERSONAL, config=dict(fx.PERSONAL_ALLOWLIST))
    FAKES.clear()
    FAKES["gmail_personal"] = FakeGmail(fx.personal_fixture(), connection=conn)
    await conn_svc.cursor_set(db, conn, "history", {"history_id": "200"})
    await db.commit()
    await sync(db, conn)
    first = dict(conn.excluded_counts)
    # replay the same history window (as a bounded resync or a duplicate history record would)
    await conn_svc.cursor_set(db, conn, "history", {"history_id": "200"})
    await db.commit()
    await sync(db, conn)
    print("EXCLUDED first:", first, "second:", dict(conn.excluded_counts))
    assert conn.excluded_counts["total"] == first["total"], "excluded counts must not grow on a replay"


async def test_probe_webhook_underscore_wildcard(db, owner, client):
    settings.GOOGLE_PUBSUB_VERIFICATION_TOKEN = "push-secret"
    conn = await make_conn(db, "gmail_business", "a_b@azkeitrucks.com")
    FAKES.clear()
    FAKES["gmail_business"] = FakeGmail(fx.business_fixture(), connection=conn)
    await db.execute(delete(ProviderEvent).where(ProviderEvent.provider == "gmail"))
    await db.commit()
    payload = fx.push_payload("aXb@azkeitrucks.com", "999")
    r = await client.post("/api/webhooks/gmail?token=push-secret", json=payload)
    rows = (await db.execute(select(ProviderEvent).where(ProviderEvent.provider == "gmail"))).scalars().all()
    print("STATUS", r.status_code, "events", [(e.provider_event_id, e.connection_key) for e in rows])
    settings.GOOGLE_PUBSUB_VERIFICATION_TOKEN = ""
    assert not rows, "a push for aXb@ must not match the connection for a_b@"
