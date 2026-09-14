"""Test harness: real Postgres (TEST_DATABASE_URL), fresh schema per session, ASGI client,
signed-session login helpers (no WebAuthn ceremony needed)."""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timezone

import pytest
import pytest_asyncio

os.environ.setdefault("ENV", "test")
os.environ.setdefault("PUBLIC_ORIGIN", "http://testserver")
os.environ.setdefault("WEBAUTHN_RP_ID", "testserver")
os.environ.setdefault("EMAIL_TRANSPORT", "memory")
os.environ.setdefault("OWNER_REMINDER_EMAIL", "owner@example.com")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="azkt-test-"))

from backend.app.core.config import settings  # noqa: E402

settings.ENV = "test"
settings.PUBLIC_ORIGIN = "http://testserver"
settings.WEBAUTHN_RP_ID = "testserver"
settings.EMAIL_TRANSPORT = "memory"
settings.OWNER_REMINDER_EMAIL = "owner@example.com"

from backend.app import db as dbmod  # noqa: E402


def _per_process_test_url() -> str:
    """Each pytest process gets its own database (azkt_test_<pid>) so concurrent runs never
    share a schema. Created here, dropped in _schema teardown. Falls back to TEST_DATABASE_URL
    when the role cannot create databases."""
    import asyncio
    import asyncpg
    base = settings.TEST_DATABASE_URL
    name = f"azkt_test_{os.getpid()}"
    admin = base.replace("postgresql+asyncpg://", "postgresql://")
    admin_db = admin.rsplit("/", 1)[0] + "/postgres"

    async def _create():
        conn = await asyncpg.connect(admin_db)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
            await conn.execute(f'CREATE DATABASE "{name}"')
            c2 = await asyncpg.connect(admin.rsplit("/", 1)[0] + f"/{name}")
            try:
                await c2.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            finally:
                await c2.close()
        finally:
            await conn.close()
    try:
        asyncio.run(_create())
        return base.rsplit("/", 1)[0] + f"/{name}"
    except Exception:  # noqa: BLE001
        return base


TEST_URL = _per_process_test_url()
dbmod.configure_engine(TEST_URL)

from backend.app.models import Base, User  # noqa: E402
from backend.app.auth.passkey import SESSION_COOKIE, session_token  # noqa: E402
from backend.app.domain.actors import Actor  # noqa: E402
from backend.app.domain.commands import CommandContext  # noqa: E402
from backend.app.domain.policy import effective_perms  # noqa: E402


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _schema():
    from backend.app.main import _import_commands
    _import_commands()
    async with dbmod.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await dbmod.engine.dispose()
    if TEST_URL != settings.TEST_DATABASE_URL:
        import asyncpg
        admin = settings.TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        conn = await asyncpg.connect(admin.rsplit("/", 1)[0] + "/postgres")
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{TEST_URL.rsplit("/", 1)[1]}"')
        finally:
            await conn.close()


@pytest_asyncio.fixture
async def db():
    async with dbmod.SessionLocal() as s:
        yield s


@pytest_asyncio.fixture(scope="session")
async def app():
    from backend.app.main import app as _app
    return _app


@pytest_asyncio.fixture
async def client(app):
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


async def make_user(db, handle: str, role: str = "owner", *, scope: str | None = None, perms: dict | None = None,
                    email: str | None = None, display_name: str | None = None) -> User:
    u = User(handle=handle, display_name=display_name or handle.title(), role=role,
             scope=scope or ("all" if role in ("owner", "manager", "sales", "logistics", "books") else "assigned"),
             perms=perms or {}, status="active", created_at=datetime.now(timezone.utc),
             email=email or f"{handle}@example.com", reminder_email=email or f"{handle}@example.com")
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


def login(client, user: User) -> None:
    client.cookies.set(SESSION_COOKIE, session_token(user))


def actor_of(user: User, kind: str = "user") -> Actor:
    return Actor(kind=kind, user_id=user.id, role=user.role, scope=user.scope,
                 perms=effective_perms(user.role, user.perms), display_name=user.display_name)


def ctx_for(db, user: User, kind: str = "user", **kw) -> CommandContext:
    return CommandContext(db=db, actor=actor_of(user, kind), correlation_id="test", channel=kw.pop("channel", "web"), **kw)


@pytest_asyncio.fixture
async def owner(db):
    from sqlalchemy import select
    u = (await db.execute(select(User).where(User.handle == "owner"))).scalar_one_or_none()
    if u is None:
        u = await make_user(db, "owner", "owner", email="owner@example.com", display_name="Dylan")
    return u


@pytest_asyncio.fixture
async def manager(db):
    from sqlalchemy import select
    u = (await db.execute(select(User).where(User.handle == "luis"))).scalar_one_or_none()
    if u is None:
        u = await make_user(db, "luis", "manager", display_name="Luis")
    return u


@pytest_asyncio.fixture
async def mechanic(db):
    from sqlalchemy import select
    u = (await db.execute(select(User).where(User.handle == "marco"))).scalar_one_or_none()
    if u is None:
        u = await make_user(db, "marco", "mechanic", display_name="Marco")
    return u


async def run_worker_once():
    from backend import worker
    return await worker.run_once(dbmod.SessionLocal)
