"""Inbox ingestion, admission, classification and matching (spec §4.1, §4.2, §4.5, §3.3).

Acceptance: A04 account-qualified ids, A05 personal allowlist admission, A06 re-evaluation and
quarantine, A08 revoked → reconnected stays degraded until catch-up, B01 duplicates/reorder/crash,
B02 invalid cursor → bounded resync with a visible gap, B03 unknown inquiry + reversible spam,
B05 separately linked items, B12 bounce/Square/auto-reply/opt-out/dispute routing, F5/H11 stale
source blocks all-clear. Every provider call goes through FakeGmail.
"""
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
from backend.app.domain.commands import dispatch
from backend.app.models.comms import Connection, Conversation, Draft, Message, ProviderEvent, SyncCursor
from backend.app.models.contacts import Contact, ContactIdentity
from backend.app.models.knowledge import CorpusChunk
from backend.app.models.notify import Notification
from backend.app.models.runtime import Job
from backend.app.services import connections as conn_svc
from backend.tests import fixtures_gmail as fx
from backend.tests.conftest import actor_of, ctx_for, login, run_worker_once

FAKES: dict[str, FakeGmail] = {}
NOW = datetime.now(timezone.utc)


def _factory(db, conn):
    fake = FAKES.get(conn.provider)
    if fake is None:
        raise Unsupported("no gmail fixture installed for this connection", setup_blocked=True)
    fake.conn = conn
    fake.db = db
    return fake


@pytest_asyncio.fixture(autouse=True, scope="module")
async def _gmail_module():
    gmail_mod.FACTORY = _factory
    async with dbmod.SessionLocal() as db:
        await _disconnect_gmail(db)
    yield
    gmail_mod.FACTORY = None
    FAKES.clear()
    async with dbmod.SessionLocal() as db:
        await _disconnect_gmail(db)
    for _ in range(40):
        out = await run_worker_once()
        if not out.get("events") and not out.get("jobs"):
            break


async def _disconnect_gmail(db):
    rows = (await db.execute(select(Connection).where(
        Connection.provider.in_(("gmail_business", "gmail_personal"))))).scalars().all()
    for c in rows:
        c.status = "disconnected"
    await db.commit()


async def reset_mailbox(db, conn: Connection) -> None:
    """Start each scenario from a known mailbox: drop this connection's threads, messages, drafts and cursor.
    There is exactly one Connection row per provider, as in production."""
    conv_ids = (await db.execute(select(Conversation.id).where(Conversation.connection_id == conn.id))).scalars().all()
    if conv_ids:
        await db.execute(delete(Draft).where(Draft.conversation_id.in_(conv_ids)))
        await db.execute(delete(Message).where(Message.conversation_id.in_(conv_ids)))
        await db.execute(delete(Conversation).where(Conversation.id.in_(conv_ids)))
    await db.execute(delete(Message).where(Message.connection_id == conn.id))
    await db.execute(delete(SyncCursor).where(SyncCursor.connection_id == conn.id))


async def make_conn(db, provider: str, identity: str, *, config: dict | None = None, caps: dict | None = None,
                    fresh_minutes: int = 1, status: str = "connected", coverage_to_days: int = 0) -> Connection:
    now = datetime.now(timezone.utc)
    conn = await conn_svc.get(db, provider, create=True)
    await reset_mailbox(db, conn)
    conn.label = conn_svc.PROVIDER_LABELS.get(provider, provider)
    conn.account_identity = identity
    conn.status = status
    conn.config = config or {}
    conn.capabilities = caps or {"send": True, "drafts": True, "labels": False}
    conn.granted_scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
    conn.last_attempt_at = now
    conn.last_success_at = now - timedelta(minutes=fresh_minutes)
    conn.coverage_from = now - timedelta(days=60)
    conn.coverage_to = now - timedelta(days=coverage_to_days)
    conn.watch_expires_at = now + timedelta(days=6)
    conn.coverage_gaps = []
    conn.excluded_counts = {}
    conn.catch_up_state = {}
    conn.failure = {}
    conn.environment = "test"
    await db.commit()
    return conn


async def sync(db, conn: Connection) -> dict:
    await jobs.enqueue(db, "gmail.sync", {"connection_id": conn.id}, dedupe_key=f"gmail.sync:{conn.id}")
    await db.commit()
    out = await run_worker_once()
    await db.refresh(conn)
    row = (await db.execute(select(Job).where(Job.kind == "gmail.sync")
                            .order_by(Job.created_at.desc()).limit(1))).scalars().first()
    return {"worker": out, "job": row}


def raw_from(msg_id: str, thread_id: str, **kw) -> dict:
    return fx.raw_message(msg_id, thread_id, **kw)


def ingest_payload(conn: Connection, raw: dict, **extra) -> dict:
    from backend.app.adapters.gmail import normalize_message
    n = normalize_message(raw)
    return {"connection_id": conn.id, "provider_message_id": n["provider_message_id"], "thread_id": n["thread_id"],
            "headers": n["headers"], "from_addr": n["from"], "from_raw": n["from_raw"], "to": n["to"], "cc": n["cc"],
            "date": n["date"], "subject": n["subject"], "text": n["text"], "html": n["html"],
            "new_text": n["new_text"], "snippet": n["snippet"], "attachments": n["attachments"],
            "label_ids": n["label_ids"], "history_id": n["history_id"], **extra}


async def make_contact(db, owner, name: str, email: str, roles=("buyer",), verified=True) -> Contact:
    res = await dispatch(ctx_for(db, owner), "contacts.create", {
        "name": name, "roles": list(roles), "status": "active",
        "identities": [{"kind": "email", "value": email, "verified": verified, "source": "manual"}]})
    return await db.get(Contact, res.data["contact"]["id"])


# ── A04 ──────────────────────────────────────────────────────────────────────
async def test_A04_same_provider_message_id_from_two_accounts_stays_distinct(db, owner):
    biz = await make_conn(db, "gmail_business", "info@azkeitrucks.com")
    personal = await make_conn(db, "gmail_personal", "dylxnxil@gmail.com", config=fx.PERSONAL_ALLOWLIST)
    raw = raw_from("shared-id-1", "shared-thread-1", from_addr="kenji@exporter.example.jp",
                   to="info@azkeitrucks.com", subject="Same id, two mailboxes", text="Body from the business account.")
    r1 = await dispatch(ctx_for(db, owner), "inbox.ingest_message", ingest_payload(biz, raw))
    r2 = await dispatch(ctx_for(db, owner), "inbox.ingest_message",
                        ingest_payload(personal, raw, personal=True, admission_rule="sender"))
    assert r1.data["created"] and r2.data["created"]
    rows = (await db.execute(select(Message).where(Message.provider_message_id == "shared-id-1"))).scalars().all()
    assert len(rows) == 2
    assert {m.connection_id for m in rows} == {biz.id, personal.id}
    assert rows[0].conversation_id != rows[1].conversation_id
    # a repeat of the same provider id on the same account is not a second record
    again = await dispatch(ctx_for(db, owner), "inbox.ingest_message", ingest_payload(biz, raw))
    assert again.data["duplicate"] is True
    assert await db.scalar(select(func.count(Message.id)).where(Message.provider_message_id == "shared-id-1")) == 2


# ── A05 / A06 ────────────────────────────────────────────────────────────────
async def test_A05_personal_allowlist_admits_on_metadata_and_never_stores_rejected_content(db, owner):
    conn = await make_conn(db, "gmail_personal", fx.PERSONAL, config=dict(fx.PERSONAL_ALLOWLIST))
    fake = FakeGmail(fx.personal_fixture(), connection=conn)
    FAKES.clear()
    FAKES["gmail_personal"] = fake
    await conn_svc.cursor_set(db, conn, "history", {"history_id": "200"})
    await db.commit()
    await sync(db, conn)

    stored = (await db.execute(select(Message).where(Message.connection_id == conn.id))).scalars().all()
    assert sorted(m.provider_message_id for m in stored) == ["p-port", "p-seb"]
    assert {m.admission_rule for m in stored} == {"sender", "domain"}
    assert all(m.personal_allowlisted for m in stored)
    # the unrelated mail that merely mentions "Sebastian" never had its body fetched or stored
    assert "p-noise" not in fake.fetched_full and "p-med" not in fake.fetched_full
    assert "p-noise" in fake.fetched_metadata
    bodies = " ".join(m.body_text for m in stored)
    assert "bank account" not in bodies and "family" not in bodies.lower()
    chunks = (await db.execute(select(CorpusChunk).where(CorpusChunk.source_kind == "message"))).scalars().all()
    assert all("salad" not in (c.text or "") for c in chunks)
    await db.refresh(conn)
    assert conn.excluded_counts.get("total") == 2
    assert sum(conn.excluded_counts["by_reason"].values()) == 2


async def test_A06_new_participant_reevaluated_and_narrowed_allowlist_quarantines(db, owner):
    conn = await make_conn(db, "gmail_personal", fx.PERSONAL,
                           config={**fx.PERSONAL_ALLOWLIST, "allowlist_threads": ["p-t-seb"]})
    fake = FakeGmail(fx.personal_fixture(), connection=conn)
    FAKES.clear()
    FAKES["gmail_personal"] = fake
    await conn_svc.cursor_set(db, conn, "history", {"history_id": "200"})
    await db.commit()
    await sync(db, conn)
    port_msg = (await db.execute(select(Message).where(Message.connection_id == conn.id,
                                                       Message.provider_message_id == "p-port"))).scalar_one()
    assert port_msg.body_text and port_msg.admission_rule == "domain"

    # the approved thread gains an unrelated participant: the NEW message is re-evaluated and refused
    fake.add_message(fx.THREAD_NEW_PARTICIPANT, history_id="206")
    await sync(db, conn)
    assert (await db.execute(select(Message).where(Message.connection_id == conn.id,
                                                   Message.provider_message_id == "p-seb2"))).scalar_one_or_none() is None
    assert "p-seb2" not in fake.fetched_full

    # narrow the allowlist: the port domain is no longer approved
    conn.config = {**conn.config, "allowlist_domains": []}
    await db.commit()
    res = await dispatch(ctx_for(db, owner), "inbox.reapply_allowlist", {"provider": "gmail_personal", "connection_id": conn.id})
    assert port_msg.id in res.data["message_ids"]
    await db.refresh(port_msg)
    assert port_msg.admitted is False and port_msg.quarantined_at is not None
    assert port_msg.body_text == "" and port_msg.subject == "" and port_msg.attachments == []
    assert port_msg.provider_message_id == "p-port"      # minimal audit metadata is retained
    assert port_msg.excluded_reason
    chunks = (await db.execute(select(CorpusChunk).where(CorpusChunk.source_kind == "message",
                                                         CorpusChunk.source_id == port_msg.id))).scalars().all()
    assert chunks and all(c.tombstoned_at is not None and c.text == "" and c.embedding is None for c in chunks)
    # the still-authorized sender keeps his mail
    seb = (await db.execute(select(Message).where(Message.connection_id == conn.id,
                                                  Message.provider_message_id == "p-seb"))).scalar_one()
    assert seb.admitted is True and seb.body_text


# ── A08 ──────────────────────────────────────────────────────────────────────
async def test_A08_revoked_then_reconnected_stays_degraded_until_catch_up(db, owner):
    conn = await make_conn(db, "gmail_business", "catchup@azkeitrucks.com")
    FAKES.clear()
    FAKES["gmail_business"] = FakeGmail(fx.business_fixture(), connection=conn)
    await conn_svc.cursor_set(db, conn, "history", {"history_id": "100"})
    await db.commit()

    await conn_svc.mark_failure(db, conn, "auth_expired", "401 from Google (access revoked)")
    await db.commit()
    assert conn_svc.freshness(conn)["state"] == "expired"

    # reconnected, but nothing has been caught up yet
    conn.status = "connected"
    conn.last_success_at = None
    conn.failure = {}
    conn.catch_up_state = {"pending": True, "reason": "reconnected after revocation"}
    await db.commit()
    assert conn_svc.freshness(conn)["state"] == "degraded"
    cov = await _coverage_for(db, "gmail_business")
    assert cov["freshness"]["state"] == "degraded" and cov["catch_up"]["pending"] is True

    await sync(db, conn)
    await db.refresh(conn)
    assert conn.catch_up_state["pending"] is False
    assert conn_svc.freshness(conn)["state"] == "ok"
    assert await db.scalar(select(func.count(Message.id)).where(Message.connection_id == conn.id)) > 0


async def _coverage_for(db, provider: str) -> dict:
    from backend.app.services.inbox import coverage
    data = await coverage(db)
    return next(a for a in data["accounts"] if a["provider"] == provider)


# ── B01 ──────────────────────────────────────────────────────────────────────
class CrashingGmail(FakeGmail):
    """Crashes once part-way through a page so the replay path is exercised (B01)."""

    def __init__(self, *a, crash_after: int = 3, **kw):
        super().__init__(*a, **kw)
        self.crash_after = crash_after

    async def get_message(self, message_id: str, format: str = "full"):
        if format == "full" and self.crash_after >= 0 and len(self.fetched_full) >= self.crash_after:
            self.crash_after = -1
            raise RuntimeError("worker crashed mid-page")
        return await super().get_message(message_id, format=format)


async def test_B01_duplicate_reordered_notifications_and_a_crash_mid_page_converge(db, owner):
    conn = await make_conn(db, "gmail_business", "converge@azkeitrucks.com")
    fake = CrashingGmail(fx.business_fixture(), connection=conn, crash_after=3)
    FAKES.clear()
    FAKES["gmail_business"] = fake
    await conn_svc.cursor_set(db, conn, "history", {"history_id": "100"})
    await db.commit()

    await sync(db, conn)   # crashes part-way: the job is retried, not lost
    partial = await db.scalar(select(func.count(Message.id)).where(Message.connection_id == conn.id))
    assert 0 < partial < 10
    cursor = await conn_svc.cursor_get(db, conn, "history")
    assert cursor["history_id"] == "100", "the cursor must not advance past an unprocessed page"

    retry = (await db.execute(select(Job).where(Job.kind == "gmail.sync", Job.state == "queued",
                                                Job.dedupe_key.like(f"gmail.sync:{conn.id}:retry%")))).scalars().all()
    assert retry, "the unfinished sync is re-queued, not dropped"
    await run_worker_once()

    rows = (await db.execute(select(Message).where(Message.connection_id == conn.id))).scalars().all()
    ids = [m.provider_message_id for m in rows]
    assert sorted(ids) == sorted(set(ids)), "duplicate/reordered notifications must converge"
    assert set(ids) == set(fx.business_fixture()["messages"]), "no range may be missing"
    await db.refresh(conn)
    cursor = await conn_svc.cursor_get(db, conn, "history")
    assert cursor["history_id"] == "120" and cursor.get("page_token") is None


# ── B02 ──────────────────────────────────────────────────────────────────────
async def test_B02_invalid_history_cursor_triggers_bounded_resync_with_a_visible_gap(db, owner, client):
    conn = await make_conn(db, "gmail_business", "resync@azkeitrucks.com", coverage_to_days=30)
    fixture = {**fx.business_fixture(), "invalid_history_before": "1000"}
    FAKES.clear()
    FAKES["gmail_business"] = FakeGmail(fixture, connection=conn)
    await conn_svc.cursor_set(db, conn, "history", {"history_id": "100"})
    await db.commit()

    await sync(db, conn)
    await db.refresh(conn)
    gaps = [g for g in (conn.coverage_gaps or []) if g["kind"] == "history_invalid"]
    assert gaps, "an invalid cursor must record a coverage gap"
    assert gaps[0]["resolved_at"] is None, "a gap the bounded resync could not cover stays visible"
    rows = (await db.execute(select(Message).where(Message.connection_id == conn.id))).scalars().all()
    ids = [m.provider_message_id for m in rows]
    assert sorted(ids) == sorted(set(ids)) and len(ids) == len(fixture["messages"])
    cursor = await conn_svc.cursor_get(db, conn, "history")
    assert cursor["history_id"] == "120" and cursor.get("resynced_at")

    login(client, owner)
    r = await client.get("/api/inbox/coverage")
    assert r.status_code == 200
    account = next(a for a in r.json()["accounts"] if a["provider"] == "gmail_business")
    assert account["gaps"] and account["gaps"][0]["kind"] == "history_invalid"
    assert r.json()["all_clear_possible"] is False
    # a repeated sync does not duplicate anything
    await sync(db, conn)
    assert await db.scalar(select(func.count(Message.id)).where(Message.connection_id == conn.id)) == len(ids)


# ── B03 / B12 ────────────────────────────────────────────────────────────────
async def test_B03_unknown_inquiry_is_provisional_and_spam_is_reversible(db, owner):
    conn = await make_conn(db, "gmail_business", "triage@azkeitrucks.com")
    FAKES.clear()
    FAKES["gmail_business"] = FakeGmail(fx.business_fixture(), connection=conn)
    await conn_svc.cursor_set(db, conn, "history", {"history_id": "100"})
    await db.commit()
    await sync(db, conn)

    unknown = await _conv_of(db, conn, "t-unknown")
    assert unknown.classification == "customer"
    assert unknown.state == "unmatched" and unknown.triage_reason
    assert unknown.contact_id is not None
    provisional = await db.get(Contact, unknown.contact_id)
    assert provisional.status == "provisional"
    ident = (await db.execute(select(ContactIdentity).where(ContactIdentity.contact_id == provisional.id))).scalars().all()
    assert [i.value_norm for i in ident] == ["tomo.watanabe@example.jp"]
    assert any(n.kind == "inbox.triage" for n in await _notifications(db))

    spam = await _conv_of(db, conn, "t-spam")
    assert spam.classification == "spam" and spam.state == "no_reply_needed" and spam.spam_reason
    assert await db.scalar(select(func.count(Draft.id))) == 0, "no unsolicited auto-reply is ever drafted"
    from backend.app.models.runtime import ExternalAction
    assert await db.scalar(select(func.count(ExternalAction.id)).where(
        ExternalAction.command_name == "inbox.send")) == 0

    res = await dispatch(ctx_for(db, owner), "inbox.not_spam", {"conversation_id": spam.id, "reason": "real inquiry"})
    assert res.data["conversation"]["classification"] != "spam"
    assert res.data["conversation"]["state"] in ("needs_reply", "unmatched")
    assert res.data["conversation"]["spam_reason"] is None
    # the message itself is still there: nothing was deleted
    assert await db.scalar(select(func.count(Message.id)).where(
        Message.connection_id == conn.id, Message.provider_message_id == "m-spam")) == 1


async def test_B12_bounce_square_auto_reply_opt_out_and_dispute_route_correctly(db, owner):
    conn = await make_conn(db, "gmail_business", "routing@azkeitrucks.com")
    await make_contact(db, owner, "Maria Chen", "maria.chen@example.com")
    await make_contact(db, owner, "Kenji Sato", "kenji@exporter.example.jp", roles=("exporter",))
    FAKES.clear()
    FAKES["gmail_business"] = FakeGmail(fx.business_fixture(), connection=conn)
    await conn_svc.cursor_set(db, conn, "history", {"history_id": "100"})
    await db.commit()
    await sync(db, conn)

    expectations = {
        "t-bounce": ("bounce", "no_reply_needed"),
        "t-square": ("payment", "no_reply_needed"),
        "t-auto": ("automated", "no_reply_needed"),
        "t-news": ("newsletter", "no_reply_needed"),
        "t-optout": ("customer", "blocked"),
        "t-dispute": ("customer", "blocked"),
        "t-supplier": ("supplier", "needs_reply"),
    }
    for thread, (classification, state) in expectations.items():
        conv = await _conv_of(db, conn, thread)
        assert conv.classification == classification, f"{thread}: {conv.classification}"
        assert conv.state == state, f"{thread}: {conv.state}"
        if state == "no_reply_needed":
            assert conv.no_reply_reason
    dispute = await _conv_of(db, conn, "t-dispute")
    assert dispute.sensitivity == "dispute" and "owner" in dispute.no_reply_reason
    optout = await _conv_of(db, conn, "t-optout")
    contact = await db.get(Contact, optout.contact_id)
    await db.refresh(contact)
    assert contact.consent.get("opted_out_at"), "the opt-out is recorded on the contact"
    notes = await _notifications(db)
    assert any(n.kind == "inbox.dispute" and n.urgency == "high" for n in notes)
    assert any(n.kind == "inbox.bounce" for n in notes)


# ── B05: separately linked items ─────────────────────────────────────────────
async def test_B05_supplier_message_links_three_vehicles_and_two_invoices_separately(db, owner):
    for stock in ("STK-0412", "STK-0500", "STK-0501"):
        await dispatch(ctx_for(db, owner), "vehicles.create",
                       {"title": f"Kei truck {stock}", "stock_no": stock, "make": "Honda", "model": "Acty",
                        "model_year": 1995, "create_missing_task": False})
    conn = await make_conn(db, "gmail_business", "items@azkeitrucks.com")
    await make_contact(db, owner, "Kenji Sato", "kenji@exporter.example.jp", roles=("exporter",))
    res = await dispatch(ctx_for(db, owner), "inbox.ingest_message", ingest_payload(conn, fx.SUPPLIER_LIST))
    conv = await db.get(Conversation, res.data["conversation"]["id"])
    vehicle_links = [l for l in conv.links if l["kind"] == "vehicle"]
    assert len(vehicle_links) == 3, "each referenced truck is its own link"
    assert {l["evidence"]["value"].upper() for l in vehicle_links} == {"STK-0412", "STK-0500", "STK-0501"}
    assert all(l["match"] == "proposed" for l in vehicle_links), "a text reference alone never auto-links"
    assert all(l["evidence"]["message_id"] for l in vehicle_links)
    msg = await db.get(Message, res.data["message"]["id"])
    invoices = [i for i in msg.extracted["items"] if i["kind"] == "invoice_no"]
    assert sorted(i["norm"] for i in invoices) == ["INV-7781", "INV-7782"]
    # contact matching is independent of the record links
    assert conv.contact_match == "matched" and conv.classification == "supplier"


async def test_excluded_personal_mail_is_counted_once_even_when_a_page_is_replayed(db, owner):
    """The excluded number the owner sees means messages, not attempts (A05 + invariant 1)."""
    conn = await make_conn(db, "gmail_personal", fx.PERSONAL, config=dict(fx.PERSONAL_ALLOWLIST))
    FAKES.clear()
    FAKES["gmail_personal"] = FakeGmail(fx.personal_fixture(), connection=conn)
    await conn_svc.cursor_set(db, conn, "history", {"history_id": "200"})
    await db.commit()
    await sync(db, conn)
    assert conn.excluded_counts["total"] == 2

    # the same history window is delivered again (duplicate push, replayed page, bounded resync)
    await conn_svc.cursor_set(db, conn, "history", {"history_id": "200"})
    await db.commit()
    await sync(db, conn)
    await db.refresh(conn)
    assert conn.excluded_counts["total"] == 2, "a replay must not inflate the exclusion count"
    assert sum(conn.excluded_counts["by_reason"].values()) == 2
    # the replay guard is internal: the coverage view shows counts, never a list of rejected ids
    account = await _coverage_for(db, "gmail_personal")
    assert account["excluded"]["total"] == 2 and "seen" not in account["excluded"]
    # and still nothing of the rejected mail was stored
    stored = (await db.execute(select(Message).where(Message.connection_id == conn.id))).scalars().all()
    assert sorted(m.provider_message_id for m in stored) == ["p-port", "p-seb"]


async def test_a_persons_spam_decision_is_not_undone_by_the_next_message(db, owner):
    """A classification a person set is reversed by a person, never by the sender (§4.5, B03)."""
    conn = await make_conn(db, "gmail_business", "sticky@azkeitrucks.com")
    FAKES.clear()
    FAKES["gmail_business"] = FakeGmail(fx.business_fixture(), connection=conn)
    raw = raw_from("sticky-1", "sticky-t", from_addr="Deals <deals@marketing.example>",
                   to="sticky@azkeitrucks.com", subject="Is the truck available?",
                   text="Hi, is the kei truck still available and what is the price?")
    res = await dispatch(ctx_for(db, owner), "inbox.ingest_message", ingest_payload(conn, raw))
    conv_id = res.data["conversation"]["id"]
    await dispatch(ctx_for(db, owner), "inbox.mark_spam", {"conversation_id": conv_id, "reason": "known spammer"})

    raw2 = raw_from("sticky-2", "sticky-t", from_addr="Deals <deals@marketing.example>",
                    to="sticky@azkeitrucks.com", subject="Re: Is the truck available?",
                    text="Following up - can you confirm the price today?")
    await dispatch(ctx_for(db, owner), "inbox.ingest_message", ingest_payload(conn, raw2))
    conv = await db.get(Conversation, conv_id)
    await db.refresh(conv)
    assert conv.classification == "spam" and conv.classification_source == "human"
    assert conv.state == "no_reply_needed", "a follow-up must not walk the thread back into the reply queue"
    assert await db.scalar(select(func.count(Message.id)).where(Message.conversation_id == conv.id)) == 2
    # still fully reversible by a person, and nothing was deleted
    reversed_ = await dispatch(ctx_for(db, owner), "inbox.not_spam", {"conversation_id": conv_id, "reason": "real inquiry"})
    assert reversed_.data["conversation"]["classification"] != "spam"


# ── F5 / H11 ─────────────────────────────────────────────────────────────────
async def test_F5_H11_stale_source_blocks_the_all_clear(db, owner, client):
    conn = await make_conn(db, "gmail_business", "stale@azkeitrucks.com", fresh_minutes=120)
    login(client, owner)
    r = await client.get("/api/inbox/coverage")
    body = r.json()
    account = next(a for a in body["accounts"] if a["provider"] == "gmail_business")
    assert account["freshness"]["state"] == "warn"
    assert body["all_clear_possible"] is False and body["stale"]
    conn.last_success_at = datetime.now(timezone.utc)
    await db.commit()
    body2 = (await client.get("/api/inbox/coverage")).json()
    assert body2["all_clear_possible"] is True


# ── webhook ──────────────────────────────────────────────────────────────────
async def test_gmail_push_is_verified_stored_once_and_processed_asynchronously(db, owner, client):
    settings.GOOGLE_PUBSUB_VERIFICATION_TOKEN = "push-secret"
    conn = await make_conn(db, "gmail_business", "push@azkeitrucks.com")
    FAKES.clear()
    FAKES["gmail_business"] = FakeGmail(fx.business_fixture(), connection=conn)
    await conn_svc.cursor_set(db, conn, "history", {"history_id": "100"})
    await db.commit()
    payload = fx.push_payload("push@azkeitrucks.com", "121")

    bad = await client.post("/api/webhooks/gmail", json=payload)
    assert bad.status_code == 401
    assert await db.scalar(select(func.count(ProviderEvent.id)).where(ProviderEvent.provider == "gmail")) == 0

    ok = await client.post("/api/webhooks/gmail?token=push-secret", json=payload)
    assert ok.status_code == 204
    events = (await db.execute(select(ProviderEvent).where(ProviderEvent.provider == "gmail"))).scalars().all()
    assert len(events) == 1 and events[0].provider_event_id == "121:push@azkeitrucks.com"
    assert events[0].connection_key == conn.id and events[0].processed_at is None
    queued = (await db.execute(select(Job).where(Job.kind == "gmail.sync", Job.state == "queued"))).scalars().all()
    assert len(queued) == 1, "the push is processed asynchronously, not inside the request"

    # a replayed delivery is acknowledged without a second effect
    again = await client.post("/api/webhooks/gmail?token=push-secret", json=payload)
    assert again.status_code == 204
    assert await db.scalar(select(func.count(ProviderEvent.id)).where(ProviderEvent.provider == "gmail")) == 1

    await run_worker_once()
    for e in events:
        await db.refresh(e)
    assert events[0].processed_at is not None
    assert await db.scalar(select(func.count(Message.id)).where(Message.connection_id == conn.id)) > 0
    settings.GOOGLE_PUBSUB_VERIFICATION_TOKEN = ""


# ── API surface ──────────────────────────────────────────────────────────────
async def test_inbox_api_filters_apply_permissions_and_record_scope(db, owner, mechanic, client):
    conn = await make_conn(db, "gmail_business", "api@azkeitrucks.com")
    await make_contact(db, owner, "Maria Chen", "maria.chen@example.com")
    FAKES.clear()
    FAKES["gmail_business"] = FakeGmail(fx.business_fixture(), connection=conn)
    await conn_svc.cursor_set(db, conn, "history", {"history_id": "100"})
    await db.commit()
    await sync(db, conn)

    login(client, mechanic)
    assert (await client.get("/api/inbox/threads")).status_code == 403

    login(client, owner)
    r = await client.get("/api/inbox/threads?filter=all")
    assert r.status_code == 200 and r.json()["total"] >= 8
    unmatched = (await client.get("/api/inbox/threads?filter=unmatched")).json()
    assert all(i["state"] == "unmatched" for i in unmatched["items"])
    conv = await _conv_of(db, conn, "t-mixed")
    detail = (await client.get(f"/api/inbox/threads/{conv.id}")).json()
    assert detail["conversation"]["id"] == conv.id
    assert detail["messages"] and detail["messages"][0]["from"] == "maria.chen@example.com"
    assert detail["takeover"]["state"] is False
    assert detail["connection"]["provider"] == "gmail_business"
    assert "secret_enc" not in str(detail)


async def test_A02_non_owner_reads_are_scoped_and_carry_no_account_detail(db, owner, manager, client):
    """A manager may work the shared inbox; personal mail, account identities and scopes stay with the owner."""
    biz = await make_conn(db, "gmail_business", "shared@azkeitrucks.com")
    personal = await make_conn(db, "gmail_personal", "dylxnxil@gmail.com", config=dict(fx.PERSONAL_ALLOWLIST))
    biz = await make_conn(db, "gmail_business", "shared@azkeitrucks.com")
    FAKES.clear()
    FAKES["gmail_business"] = FakeGmail(fx.business_fixture(), connection=biz)
    await conn_svc.cursor_set(db, biz, "history", {"history_id": "100"})
    await db.commit()
    await sync(db, biz)
    res = await dispatch(ctx_for(db, owner), "inbox.ingest_message",
                         ingest_payload(personal, fx.SEBASTIAN_OK, personal=True, admission_rule="sender"))
    personal_conv_id = res.data["conversation"]["id"]

    login(client, manager)
    threads = (await client.get("/api/inbox/threads?filter=all")).json()
    assert threads["total"] > 0
    assert all(i["id"] != personal_conv_id for i in threads["items"]), "personal mail stays with the owner"
    assert (await client.get(f"/api/inbox/threads/{personal_conv_id}")).status_code == 404

    cov = (await client.get("/api/inbox/coverage")).json()
    for account in cov["accounts"]:
        assert "account_identity" not in account and "capabilities" not in account
        assert "excluded" not in account and "failure" not in account
        assert account["freshness"] and "coverage" in account

    login(client, owner)
    owner_cov = (await client.get("/api/inbox/coverage")).json()
    assert any(a.get("account_identity") for a in owner_cov["accounts"])
    assert (await client.get(f"/api/inbox/threads/{personal_conv_id}")).status_code == 200


async def test_paste_thread_is_the_setup_fallback_when_gmail_is_not_connected(db, owner, client):
    await _disconnect_gmail(db)
    login(client, owner)
    r = await client.post("/api/inbox/paste", json={
        "from_addr": "Walk In <walkin@example.com>", "subject": "Is the blue Carry available?",
        "body": "Hi, is the blue Suzuki Carry still available and how much is it?",
        "to": ["info@azkeitrucks.com"]})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["setup_fallback"] is True
    assert data["conversation"]["state"] in ("needs_reply", "unmatched")
    msg = data["message"]
    assert msg["extracted"]["questions"], "a pasted thread still produces an answer plan"
    assert msg["provider_message_id"] is None, "a manual entry is never a provider receipt"


# ── helpers ──────────────────────────────────────────────────────────────────
async def _conv_of(db, conn: Connection, thread_id: str) -> Conversation:
    c = (await db.execute(select(Conversation).where(Conversation.connection_id == conn.id,
                                                     Conversation.provider_thread_id == thread_id))).scalar_one_or_none()
    assert c is not None, f"conversation for {thread_id} was not created"
    await db.refresh(c)
    return c


async def _notifications(db) -> list[Notification]:
    return (await db.execute(select(Notification))).scalars().all()


# ── record scope on inbox writes ─────────────────────────────────────────────
async def test_a_scoped_person_cannot_act_on_a_thread_outside_their_record_scope(db, owner):
    """An assigned-scope holder of inbox.draft may only work threads whose linked vehicle is in their
    visible set — the same rule list_threads applies on the read side."""
    import uuid

    import pytest

    from backend.app.core.errors import Denied
    from backend.app.models.tasks import Task
    from backend.tests.conftest import make_user

    tag = uuid.uuid4().hex[:6]
    conn = await make_conn(db, "gmail_business", f"scope-{tag}@azkeitrucks.com")
    await make_contact(db, owner, f"Buyer {tag}", f"buyer-{tag}@example.com")

    async def vehicle(stock: str) -> str:
        res = await dispatch(ctx_for(db, owner), "vehicles.create",
                             {"title": f"Kei truck {stock}", "stock_no": stock, "make": "Honda", "model": "Acty",
                              "model_year": 1995, "create_missing_task": False})
        return res.data["vehicle"]["id"]

    mine, theirs = await vehicle(f"STK-{tag}-A"), await vehicle(f"STK-{tag}-B")
    sales = await make_user(db, f"sales{tag}", "sales", scope="assigned")
    db.add(Task(title=f"Prep {tag}", owner_user_id=sales.id, vehicle_id=mine, status="open"))
    await db.commit()

    async def thread(n: int, vehicle_id: str) -> str:
        raw = raw_from(f"m-scope-{tag}-{n}", f"t-scope-{tag}-{n}", from_addr=f"Buyer {tag} <buyer-{tag}@example.com>",
                       to="info@azkeitrucks.com", subject=f"Question {tag}-{n}", text="Is this truck still available?")
        res = await dispatch(ctx_for(db, owner), "inbox.ingest_message", ingest_payload(conn, raw))
        cid = res.data["conversation"]["id"]
        await dispatch(ctx_for(db, owner), "inbox.link_record",
                       {"conversation_id": cid, "kind": "vehicle", "id": vehicle_id, "match": "matched"})
        return cid

    visible, hidden = await thread(1, mine), await thread(2, theirs)
    drafts_before = await db.scalar(select(func.count()).select_from(Draft).where(Draft.conversation_id == hidden))

    for command, payload in (("reply.prepare", {"conversation_id": hidden}),
                             ("inbox.take_over", {"conversation_id": hidden}),
                             ("inbox.archive", {"conversation_id": hidden})):
        with pytest.raises(Denied) as e:
            await dispatch(ctx_for(db, sales), command, payload)
        assert "not assigned to you" in str(e.value), command

    assert await db.scalar(select(func.count()).select_from(Draft).where(Draft.conversation_id == hidden)) == drafts_before, \
        "a refused prepare leaves no draft behind"
    c = await db.get(Conversation, hidden)
    await db.refresh(c)
    assert c.state != "taken_over" and c.takeover_by is None, "nothing on the thread changed"

    # the same person works the thread that is in their scope
    res = await dispatch(ctx_for(db, sales), "reply.prepare", {"conversation_id": visible})
    assert res.status == "ok" and res.data["draft"]["id"]
    took = await dispatch(ctx_for(db, sales), "inbox.take_over", {"conversation_id": visible})
    assert took.status == "ok"


async def test_thread_list_carries_a_snippet_and_a_real_total_and_counts_match(client, db, owner):
    """`total` is the size of the scoped filter, not the size of the page, and every row carries the
    opening of its newest message so the list is readable without opening each thread."""
    import uuid

    tag = uuid.uuid4().hex[:6]
    account = f"counts-{tag}@azkeitrucks.com"
    conn = await make_conn(db, "gmail_business", account)
    await make_contact(db, owner, f"Asker {tag}", f"asker-{tag}@example.com")
    bodies = ["First question about the Acty.", "Second question about the Carry.", "Third question about the Hijet."]
    for i, body in enumerate(bodies):
        raw = raw_from(f"m-count-{tag}-{i}", f"t-count-{tag}-{i}", from_addr=f"Asker {tag} <asker-{tag}@example.com>",
                       to="info@azkeitrucks.com", subject=f"Question {tag}-{i}",
                       text=f"{body}\n\n> quoted history that is not the new text", at=NOW + timedelta(minutes=i))
        await dispatch(ctx_for(db, owner), "inbox.ingest_message", ingest_payload(conn, raw))

    login(client, owner)
    page = (await client.get("/api/inbox/threads", params={"filter": "needs_reply", "account": account, "limit": 1})).json()
    assert len(page["items"]) == 1 and page["total"] == 3, "the page is one row; the total counts the filter"
    assert page["limit"] == 1 and page["offset"] == 0
    snippet = page["items"][0]["snippet"]
    assert snippet and snippet in " ".join(bodies), f"unexpected snippet {snippet!r}"
    assert "quoted history" not in snippet and len(snippet) <= 160
    assert page["items"][0]["last_message"]["direction"] == "in"

    rest = (await client.get("/api/inbox/threads", params={"filter": "needs_reply", "account": account,
                                                           "limit": 50, "offset": 1})).json()
    assert len(rest["items"]) == 2 and rest["total"] == 3, "paging does not change the total"

    counts = (await client.get("/api/inbox/counts", params={"account": account})).json()
    assert set(counts) == {"needs_reply", "drafts", "taken_over", "unmatched", "all"}
    assert counts["needs_reply"] == 3 and counts["all"] == 3 and counts["taken_over"] == 0
    await dispatch(ctx_for(db, owner), "inbox.take_over", {"conversation_id": page["items"][0]["id"]})
    counts = (await client.get("/api/inbox/counts", params={"account": account})).json()
    assert counts["taken_over"] == 1 and counts["needs_reply"] == 2 and counts["all"] == 3


async def test_an_unmatched_thread_can_be_identified_by_contact_alone(db, owner):
    """B03: a thread from a person we know, about nothing recorded yet, gets an identity link without
    inventing a business record."""
    import uuid

    tag = uuid.uuid4().hex[:6]
    conn = await make_conn(db, "gmail_business", f"ident-{tag}@azkeitrucks.com")
    raw = raw_from(f"m-ident-{tag}", f"t-ident-{tag}", from_addr=f"Stranger {tag} <stranger-{tag}@example.com>",
                   to="info@azkeitrucks.com", subject=f"Hello {tag}", text="Do you ship to Nevada?")
    res = await dispatch(ctx_for(db, owner), "inbox.ingest_message", ingest_payload(conn, raw))
    conv = await db.get(Conversation, res.data["conversation"]["id"])
    assert conv.state == "unmatched" and conv.contact_match != "matched" and conv.links == []

    # the person writes from an address we have never recorded; someone recognises them by hand
    contact = await make_contact(db, owner, f"Known {tag}", f"known-{tag}@example.com")
    out = await dispatch(ctx_for(db, owner), "inbox.link_record",
                         {"conversation_id": conv.id, "kind": "contact", "id": contact.id,
                          "reason": "same address, confirmed on the phone"})
    assert out.status == "ok"
    await db.refresh(conv)
    assert conv.contact_id == contact.id and conv.contact_match == "matched"
    assert conv.links == [], "an identity link never invents a business record"
    assert conv.state == "needs_reply", "the thread is no longer unmatched"
    assert any("same address" in r for r in conv.match_reasons), conv.match_reasons


async def test_home_unmatched_is_scope_safe_and_capped(db, owner):
    """Home's "not matched to a record" group reads the same scoped set as the inbox, caps the list at
    ten and still reports the real total."""
    import uuid

    from backend.app.models.tasks import Task
    from backend.app.services import inbox as inbox_svc
    from backend.tests.conftest import make_user

    tag = uuid.uuid4().hex[:6]
    conn = await make_conn(db, "gmail_business", f"home-{tag}@azkeitrucks.com")
    base = (await inbox_svc.home_unmatched(db, actor_of(owner)))["total"]
    newest = NOW + timedelta(days=1)          # the newest threads in the database, so ordering is decidable
    for i in range(12):
        raw = raw_from(f"m-home-{tag}-{i}", f"t-home-{tag}-{i}",
                       from_addr=f"Nobody{i} {tag} <nobody{i}-{tag}@example.com>", to="info@azkeitrucks.com",
                       subject=f"Unknown {tag}-{i}", text="Who do I talk to about a truck?",
                       at=newest + timedelta(minutes=i))
        await dispatch(ctx_for(db, owner), "inbox.ingest_message", ingest_payload(conn, raw))

    out = await inbox_svc.home_unmatched(db, actor_of(owner))
    assert out["total"] == base + 12 and len(out["items"]) == 10, "the total is real, the list is capped"
    first = out["items"][0]
    assert first["kind"] == "conversation" and set(first) == {"kind", "id", "subject", "from", "at"}
    assert first["subject"].startswith(f"Unknown {tag}") and first["from"].endswith("@example.com") and first["at"]

    # a scoped person sees the unlinked threads too, but never one linked to a vehicle outside their scope
    sales = await make_user(db, f"home{tag}", "sales", scope="assigned")
    db.add(Task(title=f"Anything {tag}", owner_user_id=sales.id, status="open"))
    await db.commit()
    scoped_before = await inbox_svc.home_unmatched(db, actor_of(sales))
    assert first["id"] in {i["id"] for i in scoped_before["items"]}

    res = await dispatch(ctx_for(db, owner), "vehicles.create",
                         {"title": f"Kei truck {tag}", "stock_no": f"STK-HOME-{tag}", "make": "Honda",
                          "model": "Acty", "model_year": 1996, "create_missing_task": False})
    await dispatch(ctx_for(db, owner), "inbox.link_record",
                   {"conversation_id": first["id"], "kind": "vehicle", "id": res.data["vehicle"]["id"],
                    "match": "proposed"})

    scoped_after = await inbox_svc.home_unmatched(db, actor_of(sales))
    assert first["id"] not in {i["id"] for i in scoped_after["items"]}, "not on their Home"
    assert scoped_after["total"] == scoped_before["total"] - 1
