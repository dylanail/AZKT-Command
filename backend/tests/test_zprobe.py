from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.models.notify import ScheduledDelivery
from backend.app.services import notifications_feed
from backend.tests.conftest import login, run_worker_once


def _u() -> str:
    return uuid.uuid4().hex[:8]


def _iso(d: timedelta) -> str:
    return (datetime.now(timezone.utc) + d).replace(microsecond=0).isoformat()


async def test_probe_deliveries_leak_other_peoples_destination(client, db, owner, mechanic):
    login(client, owner)
    tag = _u()
    t = (await client.post("/api/tasks", json={"title": f"Sales call {tag}", "type": "call",
                                               "due_at": _iso(timedelta(hours=-3)),
                                               "owner_user_id": mechanic.id, "dedupe": False})).json()["data"]["task"]
    for _ in range(2):
        await run_worker_once()
    login(client, mechanic)
    r = await client.get("/api/notifications/deliveries", params={"task_id": t["id"]})
    print("STATUS", r.status_code)
    for d in r.json()["deliveries"]:
        print("ROW", d["kind"], d["channel"], d["recipient_user_id"], d["receipt"])
    others = [d for d in r.json()["deliveries"] if d["recipient_user_id"] != mechanic.id]
    print("OTHERS", others)
    assert not any((d["receipt"] or {}).get("to") for d in others), "another person's address is exposed"


async def test_probe_set_webhook_twice(client, db, owner):
    from backend.app.services import telegram_bot
    old = (settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_WEBHOOK_SECRET)
    settings.TELEGRAM_BOT_TOKEN = "test"
    settings.TELEGRAM_WEBHOOK_SECRET = "probe-secret"
    telegram_bot.reset_client()
    try:
        login(client, owner)
        url = "https://probe.example.com/api/telegram/webhook"
        r1 = await client.post("/api/telegram/set-webhook", json={"url": url})
        print("R1", r1.json())
        await run_worker_once()
        n1 = len([m for m in telegram_bot.recorded() if m["method"] == "setWebhook"])
        r2 = await client.post("/api/telegram/set-webhook", json={"url": url})
        print("R2", r2.json())
        await run_worker_once()
        n2 = len([m for m in telegram_bot.recorded() if m["method"] == "setWebhook"])
        print("setWebhook calls", n1, n2)
        assert n2 == n1 + 1, "a second rotation request must actually re-register"
    finally:
        settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_WEBHOOK_SECRET = old
        telegram_bot.reset_client()


async def test_probe_feed_status_when_connections_blow_up(client, db, owner, monkeypatch):
    from backend.app.services import connections

    async def boom(_db):
        raise RuntimeError("overview exploded")
    monkeypatch.setattr(connections, "overview", boom)
    st = await notifications_feed.feed_status(db, 0)
    print("STATUS", st)
    assert st["all_clear"] is False, "an unchecked source must not read as all clear"


async def test_probe_process_update_partial_write(client, db, owner, monkeypatch):
    from backend.app.core.errors import Conflict
    from backend.app.models.runtime import ChatTurn
    from backend.app.services import telegram_bot
    from backend.tests.test_telegram import message, pair_owner, post_update, sent
    old = (settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_WEBHOOK_SECRET, settings.TELEGRAM_BOT_USERNAME)
    settings.TELEGRAM_BOT_TOKEN = "test"
    settings.TELEGRAM_WEBHOOK_SECRET = "hook-secret-for-tests"
    settings.TELEGRAM_BOT_USERNAME = "azkt_test_bot"
    telegram_bot.reset_client()
    try:
        import random
        tg, chat = random.randint(10_000_000, 99_999_999), random.randint(10_000_000, 99_999_999)
        await pair_owner(client, db, owner, tg, chat)
        before_turns = len((await db.execute(select(ChatTurn).where(
            ChatTurn.thread_key == f"{owner.id}:manager"))).scalars().all())
        before_sent = len(sent())

        async def blow(db_, actor, scope):
            raise Conflict("task changed since you loaded it")
        monkeypatch.setattr(telegram_bot, "_tasks_text", blow)
        await post_update(client, message(chat, tg, "/tasks"))
        await run_worker_once()
        after_turns = len((await db.execute(select(ChatTurn).where(
            ChatTurn.thread_key == f"{owner.id}:manager").execution_options(populate_existing=True))).scalars().all())
        print("turns", before_turns, after_turns, "sent", before_sent, len(sent()))
        assert len(sent()) > before_sent, "a refused update must still tell the chat something"
    finally:
        settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_WEBHOOK_SECRET, settings.TELEGRAM_BOT_USERNAME = old
        telegram_bot.reset_client()
