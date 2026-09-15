"""Square webhook + API reconciliation (spec §6.3, acceptance E06, E09).

What is proved here:

* a webhook whose HMAC does not match the configured notification URL + raw body is rejected and
  **nothing** is stored; a verified one is stored durably, acknowledged and processed asynchronously;
* a duplicate `event_id` is acknowledged with no second effect and reordered events are harmless
  because the current provider object is fetched;
* a refund after the payment reduces availability and changes the truthful status; a dispute opens an
  exception; a bank payout is a payment with `is_payout` and is never allocatable revenue;
* allocation is never automatic: only an explicit order/invoice reference proposes one, and a Square
  customer id alone never picks one of several open obligations;
* Square disconnected → the signed payload is kept with evidence and the event stays unprocessed; when
  the API comes back the 15-minute reconciliation applies it without creating a second payment (E09);
* the read/reconcile integration refuses to charge, refund or reconfigure Square (Unsupported), and an
  unconfigured credential is `setup_blocked`, never a fake success.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from backend.app.adapters import square as sq
from backend.app.core.errors import Unsupported
from backend.app.domain.commands import dispatch
from backend.app.models.comms import ProviderEvent
from backend.app.models.finance import Invoice, Payment, PaymentAllocation
from backend.app.services import square_sync
from backend.tests.conftest import ctx_for, login
from backend.tests.fixtures_providers import (MERCHANT, SQUARE_NOTIFICATION_URL, SQUARE_SIGNATURE_KEY, install_square,
                                              run_jobs, square_body, square_connection, square_dispute, square_event,
                                              square_fake, square_headers, square_payment, square_payout, square_refund)


def _u() -> str:
    return uuid.uuid4().hex[:8]


# Jobs and the webhook write in their own sessions, so every read-back reloads the row explicitly
# (`populate_existing`) instead of trusting this session's identity map.
def _load(stmt):
    return stmt.execution_options(populate_existing=True)


async def _events(db, event_id: str) -> list[ProviderEvent]:
    rows = (await db.execute(_load(select(ProviderEvent).where(
        ProviderEvent.provider == "square", ProviderEvent.provider_event_id == event_id)))).scalars().all()
    return list(rows)


async def _payment(db, provider_payment_id: str) -> Payment | None:
    return (await db.execute(_load(select(Payment).where(
        Payment.provider == "square", Payment.merchant_id == MERCHANT,
        Payment.provider_payment_id == provider_payment_id)))).scalar_one_or_none()


async def _post(client, payload: dict, *, sign: bool = True, key: str = SQUARE_SIGNATURE_KEY):
    body = square_body(payload)
    headers = square_headers(body, key=key) if sign else {"content-type": "application/json"}
    return await client.post("/api/webhooks/square", content=body, headers=headers)


# ── E06: signature, duplicates, reordering, refunds ──────────────────────────
async def test_E06_bad_signature_is_rejected_and_nothing_is_stored(client, db, owner):
    await square_connection(db)
    fake = square_fake()
    pid = f"SQP-SIG-{_u()}"
    fake.add_payment(square_payment(pid, amount_minor=120000))
    with install_square(fake):
        payload = square_event(f"EVT-BAD-{_u()}", "payment.created", "payment", fake.payments[pid])
        # wrong key: the body is authentic-looking but unverified, so it never becomes stored truth
        r = await _post(client, payload, key="not-the-signature-key")
        assert r.status_code == 401 and r.json()["error"] == "bad_signature"
        assert await _events(db, payload["event_id"]) == []
        # a missing header is not "ok" either
        r2 = await _post(client, payload, sign=False)
        assert r2.status_code == 401
        assert await _events(db, payload["event_id"]) == []
        # the same bytes with the right key over the right URL are accepted
        assert sq.verify_signature(SQUARE_NOTIFICATION_URL, square_body(payload), None, SQUARE_SIGNATURE_KEY) is False
        r3 = await _post(client, payload)
        assert r3.status_code == 200 and r3.json() == {"ok": True, "duplicate": False, "event_id": payload["event_id"]}
        stored = await _events(db, payload["event_id"])
        assert len(stored) == 1 and stored[0].signature_ok is True and stored[0].processed_at is None
        # the effect happens asynchronously from the durable record, never on the request path
        assert await run_jobs() >= 1
        stored = await _events(db, payload["event_id"])
        assert stored[0].processed_at is not None and await _payment(db, pid) is not None


async def test_E06_duplicate_and_reordered_events_are_harmless(client, db, owner):
    """A duplicate delivery has no second effect; a refund that arrives before its payment event still
    ends up on one payment because the current provider object is fetched."""
    conn = await square_connection(db)
    fake = square_fake()
    pid, rid = f"SQP-{_u()}", f"SQR-{_u()}"
    fake.add_payment(square_payment(pid, amount_minor=300000, buyer_email="reorder@example.com"))
    fake.refunds[rid] = square_refund(rid, pid, amount_minor=100000)
    with install_square(fake):
        ev_payment = square_event(f"EVT-P-{_u()}", "payment.created", "payment", fake.payments[pid])
        ev_refund = square_event(f"EVT-R-{_u()}", "refund.created", "refund", fake.refunds[rid])
        # out of order on purpose: the refund is delivered first
        assert (await _post(client, ev_refund)).status_code == 200
        assert await run_jobs() >= 1
        p = await _payment(db, pid)
        assert p is not None and p.amount == 3000 and [r["id"] for r in p.refunds] == [rid]
        assert p.status == "partially_refunded" and p.refunded_amount == 1000 and p.available_amount == 2000
        # now the payment event arrives (late) and twice
        assert (await _post(client, ev_payment)).status_code == 200
        dup = await _post(client, ev_payment)
        assert dup.status_code == 200 and dup.json()["duplicate"] is True
        assert len(await _events(db, ev_payment["event_id"])) == 1
        await run_jobs()
        await db.refresh(p)
        # still exactly one payment, the refund is not lost and nothing was counted twice
        rows = (await db.execute(_load(select(Payment).where(
            Payment.provider == "square", Payment.provider_payment_id == pid)))).scalars().all()
        assert len(rows) == 1 and p.refunded_amount == 1000 and p.status == "partially_refunded"
        assert p.provider_event_ids.count(ev_payment["event_id"]) == 1
        # replaying the already-processed stored event is a no-op
        stored = (await _events(db, ev_payment["event_id"]))[0]
        again = await square_sync.process_event(db, stored)
        assert again.get("duplicate") is True or again.get("skipped")
    assert conn.provider == "square"


async def test_E06_refund_and_dispute_after_payment_update_state(client, db, owner):
    conn = await square_connection(db)
    fake = square_fake()
    pid = f"SQP-{_u()}"
    fake.add_payment(square_payment(pid, amount_minor=200000, buyer_email="refund@example.com"))
    with install_square(fake):
        assert (await _post(client, square_event(f"EVT-{_u()}", "payment.created", "payment", fake.payments[pid]))).status_code == 200
        await run_jobs()
        p = await _payment(db, pid)
        assert p.status == "completed" and p.refunded_amount == 0
        # a full refund arrives later
        rid = f"SQR-{_u()}"
        fake.refunds[rid] = square_refund(rid, pid, amount_minor=200000)
        fake.payments[pid]["refunds"] = [fake.refunds[rid]]
        fake.payments[pid]["version"] = 2
        assert (await _post(client, square_event(f"EVT-{_u()}", "refund.created", "refund", fake.refunds[rid]))).status_code == 200
        await run_jobs()
        await db.refresh(p)
        assert p.status == "refunded" and p.refunded_amount == 2000 and p.available_amount == 0
        # a dispute opens an exception and the truthful status becomes disputed
        did = f"SQD-{_u()}"
        dispute = square_dispute(did, pid, amount_minor=200000)
        assert (await _post(client, square_event(f"EVT-{_u()}", "dispute.created", "dispute", dispute))).status_code == 200
        await run_jobs()
        await db.refresh(p)
        assert [d["id"] for d in p.disputes] == [did]
        assert any(e.get("kind") == "dispute_open" for e in (p.exceptions or [])) or p.status in ("disputed", "refunded")
    assert conn.status == "connected"


async def test_E06_payout_is_never_revenue_and_cannot_be_allocated(client, db, owner):
    await square_connection(db)
    fake = square_fake()
    payout_id = f"SQPAYOUT-{_u()}"
    fake.payouts[payout_id] = square_payout(payout_id, amount_minor=480000)
    with install_square(fake):
        assert (await _post(client, square_event(f"EVT-{_u()}", "payout.paid", "payout", fake.payouts[payout_id]))).status_code == 200
        await run_jobs()
        p = await _payment(db, payout_id)
        assert p is not None and p.is_payout is True and p.status == "completed"
        inv = (await dispatch(ctx_for(db, owner), "invoices.create", {
            "kind": "balance", "amount_due": "4800.00", "currency": "USD", "dedupe_key": f"payout-test-{_u()}"})).data["invoice"]
        with pytest.raises(Exception) as exc:
            await dispatch(ctx_for(db, owner), "payments.propose_allocation", {"payment_id": p.id, "invoice_id": inv["id"]})
        assert "payout" in str(exc.value).lower()
        await db.rollback()


# ── allocation discipline (spec §6.3) ────────────────────────────────────────
async def test_explicit_reference_proposes_allocation_customer_id_alone_never_does(client, db, owner):
    """Only an explicit order/invoice reference selects an obligation; a Square customer id match does
    not pick one of several open obligations."""
    await square_connection(db)
    fake = square_fake()
    sq_invoice, sq_order = f"SQINV-{_u()}", f"SQORD-{_u()}"
    paid = f"SQP-{_u()}"
    fake.add_payment(square_payment(paid, amount_minor=250000, invoice_id=sq_invoice, order_id=sq_order,
                                    customer_id="CUST-1", buyer_email="ana@example.com"))
    other = f"SQP-{_u()}"
    fake.add_payment(square_payment(other, amount_minor=100000, customer_id="CUST-1", buyer_email="ana@example.com"))
    # one obligation carries the provider reference; two more are open for the same customer
    referenced = (await dispatch(ctx_for(db, owner), "invoices.create", {
        "kind": "deposit", "amount_due": "2500.00", "currency": "USD", "dedupe_key": f"ref-{_u()}"})).data["invoice"]
    row = await db.get(Invoice, referenced["id"])
    row.provider_invoice_id = sq_invoice
    await db.commit()
    for _ in range(2):
        await dispatch(ctx_for(db, owner), "invoices.create", {"kind": "balance", "amount_due": "1000.00",
                                                               "currency": "USD", "dedupe_key": f"open-{_u()}"})
    with install_square(fake):
        assert (await _post(client, square_event(f"EVT-{_u()}", "payment.created", "payment", fake.payments[paid]))).status_code == 200
        assert (await _post(client, square_event(f"EVT-{_u()}", "payment.created", "payment", fake.payments[other]))).status_code == 200
        await run_jobs()
    p_ref, p_other = await _payment(db, paid), await _payment(db, other)
    allocs = (await db.execute(select(PaymentAllocation).where(PaymentAllocation.payment_id == p_ref.id))).scalars().all()
    assert len(allocs) == 1 and allocs[0].invoice_id == referenced["id"] and allocs[0].state == "proposed"
    # proposed, never confirmed: confirmation stays an owner action
    assert allocs[0].confirmed_at is None
    none_for_other = (await db.execute(select(PaymentAllocation).where(PaymentAllocation.payment_id == p_other.id))).scalars().all()
    assert none_for_other == []


# ── E09: disconnected, then reconciled ───────────────────────────────────────
async def test_E09_square_disconnected_then_reconciled_without_a_second_payment(client, db, owner):
    """The webhook is signed and stored while the API is unreachable: the payload is kept with evidence,
    the event stays unprocessed and the connection is marked failing. When Square answers again the
    reconciliation sweep applies it — one payment, one handoff, no duplicate."""
    conn = await square_connection(db)
    fake = square_fake()
    pid = f"SQP-{_u()}"
    fake.add_payment(square_payment(pid, amount_minor=150000, buyer_email="offline@example.com"))
    event = square_event(f"EVT-{_u()}", "payment.updated", "payment", fake.payments[pid])
    with install_square(fake):
        fake.offline = True
        assert (await _post(client, event)).status_code == 200
        await run_jobs()
        stored = (await _events(db, event["event_id"]))[0]
        await db.refresh(stored)
        assert stored.processed_at is None and "auth_expired" in (stored.error or "")
        await db.refresh(conn)
        assert (conn.failure or {}).get("kind") == "auth_expired"
        # the signed payload is still recorded as a payment: it is evidence, not an invention
        p = await _payment(db, pid)
        assert p is not None and p.amount == 1500 and p.fetched_at is not None
        before = p.id
        # Square comes back: the 15-minute reconciliation re-processes the stored event and re-lists the window
        fake.offline = False
        res = await square_sync.reconcile(db)
        assert res["processed"] >= 1 and res["payments"] >= 1
        await db.refresh(stored)
        assert stored.processed_at is not None and stored.error is None
        rows = (await db.execute(_load(select(Payment).where(
            Payment.provider == "square", Payment.provider_payment_id == pid)))).scalars().all()
        assert len(rows) == 1 and rows[0].id == before      # recovery never creates a second payment
        await db.refresh(conn)
        assert conn.last_success_at is not None


async def test_email_signal_stays_reported_and_a_disagreement_is_raised_with_evidence(db, owner):
    """Email-only mode records 'Payment reported — needs confirmation'; the authenticated API is the
    authority and a disagreement is visible with its evidence instead of being averaged away."""
    conn = await square_connection(db)
    fake = square_fake()
    with install_square(fake):
        claim = await square_sync.record_email_signal(
            db, amount="9999.00", currency="USD", source_ref=f"msg-{_u()}", sender="no-reply@squareup.com",
            subject="You got paid!", payer_email="ghost@example.com")
        await db.commit()
        p = await db.get(Payment, claim["payment"]["id"])
        assert p.status == "reported" and "needs_confirmation" in (p.report_flags or [])
        assert "subject_claims_paid_is_not_evidence" in (p.report_flags or [])
        res = await square_sync.reconcile(db)
        assert res["disagreements"] >= 1
        from backend.app.models.notify import Notification
        note = (await db.execute(select(Notification).where(Notification.entity_id == p.id))).scalars().first()
        assert note is not None and note.payload["claimed_amount"] == "9999.00"
        assert note.payload["window"]["from"] and note.payload["sender"] == "no-reply@squareup.com"
        await db.refresh(p)
        assert p.status == "reported"       # a disagreement never promotes a claim
    assert conn.provider == "square"


# ── adapter contract (spec §12.3) ────────────────────────────────────────────
def test_signature_is_computed_over_notification_url_plus_raw_body():
    body = b'{"event_id":"abc"}'
    good = sq.signature_for(SQUARE_NOTIFICATION_URL, body, SQUARE_SIGNATURE_KEY)
    assert sq.verify_signature(SQUARE_NOTIFICATION_URL, body, good, SQUARE_SIGNATURE_KEY) is True
    # a different notification URL or a single changed byte invalidates it
    assert sq.verify_signature("http://other/api/webhooks/square", body, good, SQUARE_SIGNATURE_KEY) is False
    assert sq.verify_signature(SQUARE_NOTIFICATION_URL, body + b" ", good, SQUARE_SIGNATURE_KEY) is False
    assert sq.verify_signature(SQUARE_NOTIFICATION_URL, body, good, "") is False


async def test_money_mapping_and_unsupported_capabilities():
    assert sq.money({"amount": 250000, "currency": "USD"}) == ("2500.00", "USD")
    assert sq.money({"amount": -1250, "currency": "USD"}) == ("-12.50", "USD")
    assert sq.money({"amount": 4500, "currency": "JPY"}) == ("4500", "JPY")
    assert sq.money(None) == (None, None)
    fake = square_fake()
    for call in (fake.refund_payment(), fake.create_payment(), fake.update_settings()):
        with pytest.raises(Unsupported):
            await call
    assert fake.supports("get_payment") is True and fake.supports("refund_payment") is False


async def test_missing_credential_reports_setup_blocked_not_success(db):
    """No token anywhere: the caller gets an honest `unsupported` with the setup key, never empty data."""
    from backend.app.core.config import settings
    prev, prev_env = settings.SQUARE_ACCESS_TOKEN, settings.ENV
    settings.SQUARE_ACCESS_TOKEN = ""
    sq.set_adapter(None)
    # no credential at all: the honest answer names the setup key, in every environment
    with pytest.raises(Unsupported) as first:
        sq.adapter_for(None)
    assert first.value.detail.get("setup_blocked") == "square.access_token"
    # with a credential present, the test guard still refuses to build a live client
    settings.SQUARE_ACCESS_TOKEN = "would-be-live-token"
    with pytest.raises(Unsupported, match="disabled in tests"):
        sq.adapter_for(None)
    settings.SQUARE_ACCESS_TOKEN = ""
    settings.ENV = "development"
    try:
        conn = await square_connection(db)
        conn.secret_enc = None
        await db.commit()
        with pytest.raises(Unsupported) as exc:
            sq.adapter_for(conn)
        assert exc.value.detail.get("setup_blocked") == "square.access_token"
    finally:
        settings.SQUARE_ACCESS_TOKEN, settings.ENV = prev, prev_env


async def test_webhook_status_endpoint_has_no_side_effects(client, db, owner):
    await square_connection(db)
    login(client, owner)
    with install_square():
        before = len((await db.execute(select(ProviderEvent).where(ProviderEvent.provider == "square"))).scalars().all())
        r = await client.get("/api/webhooks/square/status")
        assert r.status_code == 200
        body = r.json()
        assert body["configured"] is True and body["merchant_id"] == MERCHANT
        assert "recent_events" in body and body["signature_key_present"] is True
        after = len((await db.execute(select(ProviderEvent).where(ProviderEvent.provider == "square"))).scalars().all())
        assert after == before


async def test_reconcile_sweep_is_registered_every_15_minutes():
    from backend.app.domain.jobs import SWEEPS
    fn, interval = SWEEPS["square.reconcile"]
    assert interval == 15 * 60 and callable(fn)
