"""Notification tier engine. Disciplined: only `push` tier reaches the phone.

Tiers/thresholds come from config/notifications.yaml (fill from context doc).
Dedup via NotificationLog.dedupe_key so the phone isn't spammed on every tick.
Actual push transport (APNs/Web Push) is wired in the frontend phase; here we
classify, persist, and mark which ones are push-eligible.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from sqlalchemy import select

from ..core.config import CONFIG_DIR
from ..models import IRQ, AgentState, NotificationLog
from . import notion_sync

_CFG = CONFIG_DIR / "notifications.yaml"


def config() -> dict:
    return yaml.safe_load(Path(_CFG).read_text())


def _dk(rule_id: str, ident: str) -> str:
    return hashlib.sha1(f"{rule_id}:{ident}".encode()).hexdigest()


async def _emit(db, rule_id: str, tier: str, title: str, body: str, ident: str) -> None:
    dk = _dk(rule_id, ident)
    if await db.scalar(select(NotificationLog.id).where(NotificationLog.dedupe_key == dk)):
        return
    db.add(NotificationLog(rule_id=rule_id, tier=tier, title=title, body=body,
                           dedupe_key=dk, pushed=False))


async def evaluate(db) -> int:
    cfg = config()
    push_ids = {r["id"] for r in cfg["tiers"]["push"]}
    push_by_id = {r["id"]: r for r in cfg["tiers"]["push"]}
    now = datetime.now(timezone.utc)
    before = await db.scalar(select(NotificationLog.id).order_by(NotificationLog.created_at.desc()))

    # ── push: agent erroring longer than threshold ─────────────────────────
    thr_min = push_by_id.get("agent_erroring", {}).get("threshold_minutes", 5)
    for st in (await db.execute(select(AgentState))).scalars():
        if st.error_since and (now - st.error_since) > timedelta(minutes=thr_min):
            await _emit(db, "agent_erroring", "push",
                        f"Agent {st.agent_key} erroring",
                        f"Erroring since {st.error_since:%H:%M}",
                        f"{st.agent_key}:{st.error_since:%Y%m%d%H%M}")

    # ── push: IRQ assigned to me, unanswered past threshold ────────────────
    irq_thr = push_by_id.get("irq_assigned_unanswered", {}).get("threshold_minutes", 120)
    for irq in (await db.execute(
        select(IRQ).where(IRQ.status == "open", IRQ.answered_at.is_(None))
    )).scalars():
        if irq.received_at and (now - irq.received_at) > timedelta(minutes=irq_thr):
            await _emit(db, "irq_assigned_unanswered", "push",
                        "IRQ awaiting your reply", irq.title or irq.id,
                        f"{irq.id}:{irq.received_at:%Y%m%d%H}")

    # auction_closing_unbid: needs auction-close timestamps from the context
    # doc / Notion mapping; emitted by the scout/watcher shim once that field
    # is mapped. Left here as the wired push rule, not guessed.

    # ── dashboard tier (never pushed) ──────────────────────────────────────
    for irq in (await db.execute(
        select(IRQ).where(IRQ.status == "open")
    )).scalars():
        if irq.received_at and (now - irq.received_at) > timedelta(days=7):
            await _emit(db, "stale_irq_7d", "dashboard",
                        "Stale IRQ (>7d)", irq.title or irq.id, irq.id)

    last_sync = await notion_sync.last_sync_at(db)
    if last_sync:
        lag = now - datetime.fromisoformat(last_sync)
        if lag > timedelta(minutes=5):
            await _emit(db, "notion_sync_lag", "dashboard",
                        "Notion sync lagging", f"Last sync {last_sync}",
                        last_sync)

    await db.commit()
    after = await db.scalar(select(NotificationLog.id).order_by(NotificationLog.created_at.desc()))
    return 0 if after == before else 1
