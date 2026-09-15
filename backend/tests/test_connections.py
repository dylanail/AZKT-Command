from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.app.adapters import google_oauth
from backend.app.core.config import settings
from backend.app.models.comms import Connection
from backend.app.services import connections as conn_svc
from backend.tests.conftest import login


async def test_freshness_states():
    now = datetime.now(timezone.utc)
    assert conn_svc.freshness(None)["state"] == "disconnected"
    c = Connection(provider="gmail_business", status="connected", last_success_at=now - timedelta(minutes=2))
    assert conn_svc.freshness(c, now)["state"] == "ok"
    c.last_success_at = now - timedelta(minutes=40)
    assert conn_svc.freshness(c, now)["state"] == "warn"
    c.status = "expired"
    assert conn_svc.freshness(c, now)["state"] == "expired"
    c.status = "connected"
    c.watch_expires_at = now - timedelta(hours=1)
    assert conn_svc.freshness(c, now)["state"] == "expired"


async def test_A07_google_state_round_trip_and_unconfigured(db, owner):
    settings.GOOGLE_CLIENT_ID = ""
    import pytest
    from backend.app.core.errors import Unsupported
    with pytest.raises(Unsupported):
        google_oauth.start("gmail_business", owner.id)
    settings.GOOGLE_CLIENT_ID = "cid"
    settings.GOOGLE_CLIENT_SECRET = "sec"
    res = google_oauth.start("gmail_business", owner.id)
    assert "code_challenge=" in res["url"] and "gmail.readonly" in " ".join(res["scopes"])
    data = google_oauth.parse_state(res["state"])
    assert data["p"] == "gmail_business" and data["u"] == owner.id and data["v"]
    settings.GOOGLE_CLIENT_ID = ""
    settings.GOOGLE_CLIENT_SECRET = ""


async def test_connections_list_hides_identities_from_non_owner(client, owner, mechanic, db):
    login(client, owner)
    r = await client.get("/api/connections")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 10 and "all_clear_possible" in body
    login(client, mechanic)
    r2 = await client.get("/api/connections")
    assert r2.status_code == 200
    assert all("account_identity" not in i for i in r2.json()["items"])
    r3 = await client.post("/api/connections/square/secret", json={"access_token": "x"})
    assert r3.status_code == 403


async def test_secret_is_encrypted_and_never_returned(client, owner, db):
    login(client, owner)
    r = await client.post("/api/connections/square/secret", json={"access_token": "sq0atp-secret"})
    assert r.status_code == 200
    body = r.json()
    assert "secret" not in str(body).lower() or "sq0atp" not in str(body)
    from sqlalchemy import select
    row = (await db.execute(select(Connection).where(Connection.provider == "square"))).scalar_one()
    assert row.secret_enc and "sq0atp" not in row.secret_enc
    assert conn_svc.get_secret(row)["access_token"] == "sq0atp-secret"


async def test_one_connection_row_per_provider_and_a_deterministic_pick(db):
    """The data model intends exactly one row per provider; the database now enforces it, and rows
    that predate the index are still read the same way every time."""
    import uuid

    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    provider = f"probe_{uuid.uuid4().hex[:6]}"
    first = await conn_svc.get(db, provider, create=True)
    await db.commit()
    assert (await conn_svc.get(db, provider, create=True)).id == first.id, "get() never makes a second row"

    db.add(Connection(provider=provider, status="connected", environment="test"))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()

    # legacy duplicates (written before uq_connection_provider): the live row wins, then the newest
    now = datetime.now(timezone.utc)
    sp = await db.begin_nested()
    try:
        await db.execute(text("ALTER TABLE connections DROP CONSTRAINT uq_connection_provider"))
        stale = Connection(provider=provider, status="expired", environment="test", created_at=now)
        live = Connection(provider=provider, status="connected", environment="test", created_at=now - timedelta(days=1))
        db.add_all([stale, live])
        await db.flush()
        picked = await conn_svc.get(db, provider)
        assert picked.id == live.id, "a live row beats a newer dead one"
        assert (await conn_svc.get(db, provider)).id == live.id, "and the pick does not move between calls"
        live.status = "disconnected"
        await db.flush()
        assert (await conn_svc.get(db, provider)).id == stale.id, "with none live, the newest row wins"
    finally:
        await sp.rollback()

    rows = await conn_svc.overview(db)
    assert len(rows) == len({r["provider"] for r in rows}), "overview shows each provider exactly once"
