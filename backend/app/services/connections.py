"""Provider connections: status, freshness, secrets (spec §3.1 Connection, §12.4 freshness)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.crypto import decrypt, encrypt
from ..models.comms import Connection, SyncCursor

# Freshness targets (minutes) per provider; warn after this without a successful sync.
WARN_AFTER_MIN = {"gmail_business": 15, "gmail_personal": 15, "square": 30, "sheets": 30, "drive": 30,
                  "wordpress": 90, "woocommerce": 90, "telegram": 60 * 24, "model": 60 * 24, "smtp": 60 * 24}
PROVIDER_LABELS = {
    "gmail_business": "Business email (info@azkeitrucks.com)", "gmail_personal": "Personal email (allowlisted senders)",
    "drive": "Importer Drive folder", "sheets": "Ledger sheet", "square": "Square", "telegram": "Telegram",
    "wordpress": "WordPress", "woocommerce": "WooCommerce", "model": "AI model", "smtp": "Reminder email",
    "sms": "Business SMS", "calendar": "Calendar", "legacy_notion": "Notion mirror (legacy)",
    "legacy_openclaw": "OpenClaw agents (legacy)",
}


# One row per provider is the data model (every writer goes through `get(create=True)`, and the
# `uq_connection_provider` index enforces it). Rows predating that index are still read
# deterministically: the live row wins, then the newest, then the highest id.
_LIVE_RANK = case((Connection.status == "connected", 0),
                  (Connection.status.in_(("warn", "degraded")), 1),
                  (Connection.status.in_(("error", "expired")), 2),
                  else_=3)
_PICK_ORDER = (_LIVE_RANK, Connection.created_at.desc(), Connection.id.desc())


async def get(db: AsyncSession, provider: str, *, create: bool = False) -> Connection | None:
    row = (await db.execute(select(Connection).where(Connection.provider == provider)
                            .order_by(*_PICK_ORDER))).scalars().first()
    if row is None and create:
        row = Connection(provider=provider, label=PROVIDER_LABELS.get(provider, provider), status="disconnected",
                         environment=settings.ENV)
        db.add(row)
        await db.flush()
    return row


def set_secret(conn: Connection, secret: dict) -> None:
    conn.secret_enc = encrypt(json.dumps(secret))


def get_secret(conn: Connection) -> dict:
    if not conn.secret_enc:
        return {}
    try:
        return json.loads(decrypt(conn.secret_enc))
    except ValueError:
        return {}


def freshness(conn: Connection | None, now: datetime | None = None) -> dict:
    """warn/degraded = sync condition; expired = auth/watch condition; disconnected = absent (spec §12.4)."""
    now = now or datetime.now(timezone.utc)
    if conn is None or conn.status == "disconnected":
        return {"state": "disconnected", "label": "Not connected", "last_success_at": None}
    if conn.status == "expired":
        return {"state": "expired", "label": "Needs reconnect", "last_success_at": _iso(conn.last_success_at)}
    if conn.watch_expires_at and conn.watch_expires_at < now:
        return {"state": "expired", "label": "Watch expired", "last_success_at": _iso(conn.last_success_at)}
    warn = WARN_AFTER_MIN.get(conn.provider, 60)
    if conn.last_success_at is None:
        return {"state": "degraded", "label": "Connected, no successful sync yet", "last_success_at": None}
    age = (now - conn.last_success_at).total_seconds() / 60
    if age > warn:
        return {"state": "warn", "label": f"Behind by {int(age)} min", "last_success_at": _iso(conn.last_success_at)}
    return {"state": "ok", "label": "Fresh", "last_success_at": _iso(conn.last_success_at)}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


async def mark_attempt(db: AsyncSession, conn: Connection) -> None:
    conn.last_attempt_at = datetime.now(timezone.utc)


async def mark_success(db: AsyncSession, conn: Connection, *, coverage_to: datetime | None = None) -> None:
    now = datetime.now(timezone.utc)
    conn.last_attempt_at = now
    conn.last_success_at = now
    if coverage_to:
        conn.coverage_to = coverage_to
        if conn.coverage_from is None:
            conn.coverage_from = coverage_to
    if conn.status in ("warn", "degraded", "error"):
        conn.status = "connected"
    conn.failure = {}


async def mark_failure(db: AsyncSession, conn: Connection, kind: str, message: str) -> None:
    """kind: auth_expired | rate_limited | transient | schema_changed | permission_denied | unknown."""
    now = datetime.now(timezone.utc)
    conn.last_attempt_at = now
    conn.failure = {"kind": kind, "message": message[:500], "at": now.isoformat()}
    if kind == "auth_expired" or kind == "permission_denied":
        conn.status = "expired"
    elif conn.status == "connected":
        conn.status = "warn"
    from ..models.runtime import Event
    db.add(Event(type="connection.degraded", aggregate_type="connection", aggregate_id=conn.id,
                 payload={"provider": conn.provider, "kind": kind, "message": message[:200]}, happened_at=now))


async def cursor_get(db: AsyncSession, conn: Connection, name: str) -> dict:
    row = (await db.execute(select(SyncCursor).where(SyncCursor.connection_id == conn.id, SyncCursor.name == name))).scalar_one_or_none()
    return dict(row.value) if row else {}


async def cursor_set(db: AsyncSession, conn: Connection, name: str, value: dict) -> None:
    row = (await db.execute(select(SyncCursor).where(SyncCursor.connection_id == conn.id, SyncCursor.name == name))).scalar_one_or_none()
    if row is None:
        row = SyncCursor(connection_id=conn.id, name=name, value=value)
        db.add(row)
    else:
        row.value = value
    row.advanced_at = datetime.now(timezone.utc)


def serialize(conn: Connection | None, provider: str) -> dict:
    fr = freshness(conn)
    base = {"provider": provider, "label": PROVIDER_LABELS.get(provider, provider), "status": conn.status if conn else "disconnected",
            "freshness": fr, "account_identity": conn.account_identity if conn else None,
            "config": {k: v for k, v in (conn.config or {}).items() if not k.startswith("_")} if conn else {},
            "granted_scopes": conn.granted_scopes if conn else [], "requested_scopes": conn.scopes if conn else [],
            "last_attempt_at": _iso(conn.last_attempt_at) if conn else None,
            "last_success_at": _iso(conn.last_success_at) if conn else None,
            "coverage": {"from": _iso(conn.coverage_from), "to": _iso(conn.coverage_to)} if conn else {"from": None, "to": None},
            "watch_expires_at": _iso(conn.watch_expires_at) if conn else None,
            "failure": conn.failure if conn else {}, "dependent_workflows": conn.dependent_workflows if conn else [],
            "environment": conn.environment if conn else settings.ENV, "id": conn.id if conn else None,
            "connected_at": _iso(conn.connected_at) if conn else None}
    return base


async def overview(db: AsyncSession) -> list[dict]:
    rows: dict[str, Connection] = {}
    for c in (await db.execute(select(Connection).order_by(*_PICK_ORDER))).scalars().all():
        rows.setdefault(c.provider, c)      # same deterministic pick as get()
    out = []
    for provider in PROVIDER_LABELS:
        out.append(serialize(rows.get(provider), provider))
    return out


def all_clear_possible(conns: list[dict]) -> tuple[bool, list[str]]:
    """Home must not claim all-clear when a required source is stale (spec §2.2, H11)."""
    # A required source that has never connected is as blocking as a stale one: nothing has been synced, so
    # "no urgent items found in synced data" would be an all-clear over an empty set.
    stale = [c["label"] for c in conns if c["provider"] in ("gmail_business", "square", "sheets", "drive")
             and c["freshness"]["state"] in ("warn", "expired", "degraded", "disconnected")]
    return (len(stale) == 0), stale
