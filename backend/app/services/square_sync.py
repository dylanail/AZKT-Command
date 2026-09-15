"""Square webhook processing and API reconciliation (spec §6.3, acceptance E05, E06, E09).

Contract:

* The router verifies the signature, stores the event durably (`events.record_provider_event`,
  namespaced by merchant) and acknowledges. Everything below runs asynchronously from the job queue,
  so a duplicate or out-of-order delivery is harmless.
* `process_event` fetches the current provider object when the event is partial or out of order and
  writes through finance's `payments.upsert_provider`. Payment identity is
  (provider, merchant_id, provider_payment_id) — one id from two merchants is never one payment.
* Allocation is never automatic. Only an explicit reference (Square order/invoice id that maps to a
  local `Invoice.provider_invoice_id`) produces a *proposed* allocation; a customer-id match alone
  never selects one of several open obligations (E07).
* Payouts to the bank become payments with `is_payout=True` and are never allocated or counted as
  vehicle revenue (E10).
* Email-only mode: `record_email_signal()` records "Payment reported — needs confirmation" through
  finance. Inbox calls it; this module never imports inbox.
* The reconciliation sweep runs every 15 minutes: it re-lists payments for the recent window, retries
  stored events that could not be processed (Square disconnected → later reconciles, E09) and raises a
  notification with evidence when an email signal and the API disagree.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters import square as sq
from ..core.errors import DomainError, ProviderError, Unsupported
from ..core.ids import sha256_hex
from ..domain import jobs
from ..domain.actors import SYSTEM_ACTOR
from ..domain.commands import CommandContext, dispatch
from ..domain.jobs import sweep
from ..models.comms import Connection, ProviderEvent
from ..models.finance import Invoice, Payment
from ..models.notify import Notification
from . import connections as conn_svc

log = logging.getLogger("azkt.square")

RECONCILE_SECONDS = 15 * 60
RECONCILE_WINDOW_HOURS = 24
PROVIDER = "square"

# Square payment/refund/payout status → the vocabulary finance understands (STATUS_MAP in services/finance).
PAYMENT_STATUS = {"COMPLETED": "completed", "APPROVED": "approved", "PENDING": "pending",
                  "CANCELED": "canceled", "CANCELLED": "canceled", "FAILED": "failed"}
REFUND_STATUS = {"COMPLETED": "completed", "PENDING": "pending", "REJECTED": "rejected", "FAILED": "failed"}
PAYOUT_STATUS = {"PAID": "completed", "SENT": "pending", "FAILED": "failed"}
DISPUTE_STATE = {"EVIDENCE_REQUIRED": "evidence_required", "PROCESSING": "processing", "INQUIRY_EVIDENCE_REQUIRED": "evidence_required",
                 "INQUIRY_PROCESSING": "processing", "WON": "won", "LOST": "lost", "ACCEPTED": "lost"}
HANDLED_OBJECTS = ("payment", "refund", "dispute", "payout", "invoice")
# A fetch failure that will pass on its own (the provider is down, throttling or disconnected) keeps the
# event open for the reconciliation sweep. A permanent answer (the object does not exist for this
# merchant, or we may not read it) is recorded once and never retried forever.
RETRYABLE_FETCH = ("auth_expired", "rate_limited", "transient", "unsupported", "unknown_result", "schema_changed")


def _retryable(api_error: str | None) -> bool:
    return bool(api_error) and api_error.split(":", 1)[0].strip() in RETRYABLE_FETCH


def _ctx(db: AsyncSession, correlation_id: str | None = None) -> CommandContext:
    return CommandContext(db=db, actor=SYSTEM_ACTOR, correlation_id=correlation_id, channel="webhook")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# ── mapping ──────────────────────────────────────────────────────────────────
def map_payment(obj: dict, *, merchant_id: str, event_id: str | None = None, connection_id: str | None = None,
                fetched_at: datetime | None = None, from_api: bool = False) -> dict:
    """A Square payment resource → `payments.upsert_provider` input (spec §6.3 stored fields)."""
    amount, currency = sq.money(obj.get("amount_money") or obj.get("total_money"))
    fee = None
    for pf in obj.get("processing_fee") or []:
        a, _ = sq.money(pf.get("amount_money"))
        if a is not None:
            fee = str(Decimal(fee or "0") + Decimal(a))
    refunded, _ = sq.money(obj.get("refunded_money"))
    status = PAYMENT_STATUS.get(str(obj.get("status") or "").upper(), "pending")
    refunds = []
    for r in obj.get("refunds") or []:
        refunds.append(map_refund(r))
    net = None
    if amount is not None and fee is not None:
        net = str(Decimal(amount) - Decimal(fee))
    out = {
        "provider": PROVIDER, "merchant_id": merchant_id, "provider_payment_id": obj.get("id"),
        "provider_event_id": event_id, "provider_version": str(obj.get("version") or obj.get("updated_at") or ""),
        "location_id": obj.get("location_id"), "connection_id": connection_id,
        "amount": amount, "currency": currency, "status": status,
        "fee_amount": fee, "net_amount": net,
        "refunds": refunds, "disputes": [],
        "payer_name": ((obj.get("shipping_address") or {}).get("name")
                       or (obj.get("billing_address") or {}).get("name")),
        "payer_email": obj.get("buyer_email_address"),
        "provider_customer_id": obj.get("customer_id"),
        "provider_order_id": obj.get("order_id"),
        "provider_invoice_id": obj.get("invoice_id") or ((obj.get("application_details") or {}).get("invoice_id")),
        "occurred_at": _parse_dt(obj.get("created_at")),
        "fetched_at": fetched_at if from_api else None,
        "is_payout": False,
        "raw": obj,
    }
    if refunded is not None and not refunds and Decimal(refunded) > 0:
        # Square reported a refunded total without the refund objects: keep it visible as one aggregate fact.
        out["refunds"] = [{"id": f"{obj.get('id')}:refunded_money", "amount": refunded, "currency": currency,
                           "status": "completed", "at": _iso(_parse_dt(obj.get("updated_at")))}]
    return {k: v for k, v in out.items() if v is not None or k in ("refunds", "disputes", "raw")}


def map_refund(obj: dict) -> dict:
    amount, currency = sq.money(obj.get("amount_money"))
    return {"id": obj.get("id"), "amount": amount, "currency": currency,
            "status": REFUND_STATUS.get(str(obj.get("status") or "").upper(), "pending"),
            "at": obj.get("created_at"), "reason": obj.get("reason")}


def map_dispute(obj: dict) -> dict:
    amount, currency = sq.money(obj.get("amount_money") or obj.get("disputed_money"))
    return {"id": obj.get("id"), "amount": amount, "currency": currency,
            "status": DISPUTE_STATE.get(str(obj.get("state") or "").upper(), "processing"),
            "at": obj.get("created_at"), "reason": obj.get("reason")}


def map_payout(obj: dict, *, merchant_id: str, event_id: str | None = None, connection_id: str | None = None) -> dict:
    amount, currency = sq.money(obj.get("amount_money"))
    return {"provider": PROVIDER, "merchant_id": merchant_id, "provider_payment_id": obj.get("id"),
            "provider_event_id": event_id, "connection_id": connection_id,
            "provider_version": str(obj.get("version") or obj.get("updated_at") or ""),
            "amount": amount, "currency": currency,
            "status": PAYOUT_STATUS.get(str(obj.get("status") or "").upper(), "pending"),
            "occurred_at": _parse_dt(obj.get("created_at")), "is_payout": True,
            "location_id": obj.get("location_id"), "raw": obj}


def event_parts(payload: dict) -> dict:
    data = (payload or {}).get("data") or {}
    obj = data.get("object") or {}
    otype = str(data.get("type") or "").lower()
    # Square nests the resource under its own key: {"object": {"payment": {...}}}
    resource = obj.get(otype) if isinstance(obj, dict) and otype in obj else None
    if resource is None and isinstance(obj, dict):
        for key in ("payment", "refund", "dispute", "payout", "invoice", "order"):
            if isinstance(obj.get(key), dict):
                otype, resource = key, obj[key]
                break
    return {"event_id": payload.get("event_id") or payload.get("id"), "type": payload.get("type"),
            "merchant_id": payload.get("merchant_id") or "", "object_type": otype,
            "object_id": data.get("id"), "object": resource if isinstance(resource, dict) else {}}


# ── connection helpers ───────────────────────────────────────────────────────
async def connection(db: AsyncSession, *, create: bool = False) -> Connection | None:
    return await conn_svc.get(db, PROVIDER, create=create)


def adapter(conn: Connection | None):
    return sq.adapter_for(conn)


async def _owner_user_id(db: AsyncSession) -> str | None:
    from ..models import User
    row = (await db.execute(select(User).where(User.role == "owner", User.status == "active")
                            .order_by(User.created_at))).scalars().first()
    return row.id if row else None


async def _notify_owner(db: AsyncSession, *, kind: str, title: str, body: str, dedupe: str,
                        entity_kind: str | None = None, entity_id: str | None = None,
                        urgency: str = "today", payload: dict | None = None) -> Notification | None:
    uid = await _owner_user_id(db)
    if uid is None:
        return None
    existing = (await db.execute(select(Notification).where(Notification.dedupe_key == dedupe))).scalar_one_or_none()
    if existing is not None:
        existing.occurrences = (existing.occurrences or 1) + 1
        existing.last_event_at = datetime.now(timezone.utc)
        if existing.state == "resolved":
            existing.state = "unread"
        if payload:
            existing.payload = {**(existing.payload or {}), **payload}
        return existing
    row = Notification(user_id=uid, kind=kind, urgency=urgency, title=title[:200], body=body,
                       entity_kind=entity_kind, entity_id=entity_id, dedupe_key=dedupe, state="unread",
                       group_key="square", occurrences=1, last_event_at=datetime.now(timezone.utc),
                       payload=payload or {})
    db.add(row)
    await db.flush()
    return row


# ── email-only signal (called by services/inbox; never imported from here) ───
async def record_email_signal(db: AsyncSession, *, amount, currency: str = "USD", source_ref: str | None = None,
                              sender: str | None = None, subject: str | None = None, payer_name: str | None = None,
                              payer_email: str | None = None, contact_id: str | None = None,
                              occurred_at: datetime | None = None, raw: dict | None = None,
                              actor=None, correlation_id: str | None = None) -> dict:
    """A Square-looking email claims a payment: record it as *reported — needs confirmation* (E05).

    Never confirms, never allocates and never triggers Deposit Paid. The reconciliation sweep compares
    the claim with the authenticated API and raises a disagreement with evidence when they differ.
    """
    ctx = CommandContext(db=db, actor=actor or SYSTEM_ACTOR, correlation_id=correlation_id, channel="email")
    res = await dispatch(ctx, "payments.record_reported", {
        "amount": amount, "currency": currency, "claimed_by": "email", "provider_hint": PROVIDER,
        "source_ref": source_ref, "sender": sender, "subject": subject, "payer_name": payer_name,
        "payer_email": payer_email, "contact_id": contact_id, "occurred_at": occurred_at, "raw": raw or {}})
    return res.data or {}


# ── event processing ─────────────────────────────────────────────────────────
async def _fetch_current(ad, object_type: str, object_id: str | None, fallback: dict) -> tuple[dict, str | None]:
    """Fetch the authoritative object; on a typed provider failure keep the signed payload and say why."""
    if not object_id:
        return fallback, "event carried no object id"
    getter = {"payment": "get_payment", "refund": "get_refund", "dispute": None, "payout": "get_payout",
              "invoice": "get_invoice"}.get(object_type)
    if getter is None:
        return fallback, None
    try:
        fresh = await getattr(ad, getter)(object_id)
        return (fresh or fallback), None
    except (ProviderError, Unsupported) as e:
        return fallback, f"{sq.error_kind(e) if isinstance(e, ProviderError) else 'unsupported'}: {e}"


async def _upsert(db: AsyncSession, payload: dict, *, correlation_id: str | None = None) -> dict:
    res = await dispatch(_ctx(db, correlation_id), "payments.upsert_provider", payload)
    return res.data or {}


async def _explicit_invoice(db: AsyncSession, *, order_id: str | None, invoice_id: str | None) -> Invoice | None:
    """Only an explicit provider reference selects an obligation (spec §6.3)."""
    refs = [r for r in (invoice_id, order_id) if r]
    if not refs:
        return None
    rows = (await db.execute(select(Invoice).where(Invoice.provider_invoice_id.in_(refs)))).scalars().all()
    return rows[0] if len(rows) == 1 else None


async def _maybe_propose(db: AsyncSession, payment_row: dict, *, correlation_id: str | None = None) -> dict:
    """Propose (never confirm) an allocation when the payment names an obligation explicitly."""
    pid = payment_row.get("id")
    if not pid:
        return {"allocation": "skipped", "why": "no payment"}
    p = await db.get(Payment, pid)
    if p is None or p.is_payout:
        return {"allocation": "skipped", "why": "payout or missing payment"}
    if p.status != "completed":
        return {"allocation": "skipped", "why": f"payment is {p.status}"}
    inv = await _explicit_invoice(db, order_id=p.provider_order_id, invoice_id=p.provider_invoice_id)
    if inv is None:
        why = ("a Square customer id alone does not select one of several open obligations"
               if p.provider_customer_id else "no explicit order/invoice reference on the payment")
        ctx = _ctx(db, correlation_id)
        ctx.record(f"Square payment {p.provider_payment_id or p.id[:8]} left unallocated — {why}",
                   entity_kind="payment", entity_id=p.id, kind="payment", state="needs_allocation",
                   visibility="finance", details={"customer_id": p.provider_customer_id, "order_id": p.provider_order_id})
        return {"allocation": "manual", "why": why}
    try:
        res = await dispatch(_ctx(db, correlation_id), "payments.propose_allocation",
                             {"payment_id": p.id, "invoice_id": inv.id,
                              "note": "Square reference matched this obligation"})
        return {"allocation": (res.data or {}).get("state", "proposed"), "invoice_id": inv.id}
    except DomainError as e:
        return {"allocation": "blocked", "why": e.message, "invoice_id": inv.id}


async def process_event(db: AsyncSession, ev: ProviderEvent) -> dict:
    """Process one stored Square event. Idempotent: replaying it never duplicates an effect."""
    parts = event_parts(dict(ev.payload or {}))
    merchant = ev.connection_key or parts["merchant_id"] or ""
    conn = await connection(db)
    otype, oid = parts["object_type"], parts["object_id"]
    out: dict[str, Any] = {"event_id": parts["event_id"], "type": ev.event_type, "object_type": otype}
    if otype not in HANDLED_OBJECTS:
        out["skipped"] = f"event type {ev.event_type!r} is not handled by this integration"
        ev.processed_at = datetime.now(timezone.utc)
        await db.commit()
        return out
    try:
        ad = adapter(conn)
        api_error = None
    except Unsupported as e:
        ad, api_error = None, f"unsupported: {e}"
    fallback = parts["object"] or {}
    fetched: dict = fallback
    if ad is not None:
        fetched, api_error = await _fetch_current(ad, otype, oid, fallback)
    out["api"] = "fetched" if api_error is None and ad is not None else "payload_only"
    if api_error:
        out["api_error"] = api_error
    now = datetime.now(timezone.utc)
    result: dict = {}

    if otype == "payment":
        payload = map_payment(fetched or fallback, merchant_id=merchant, event_id=parts["event_id"],
                              connection_id=conn.id if conn else None, fetched_at=now, from_api=api_error is None and ad is not None)
        if not payload.get("provider_payment_id"):
            out["skipped"] = "payment event without an id"
        else:
            result = await _upsert(db, payload, correlation_id=parts["event_id"])
            out["payment"] = (result.get("payment") or {}).get("id")
            out["duplicate"] = bool(result.get("duplicate"))
            if result.get("payment") and not result.get("duplicate"):
                out.update(await _maybe_propose(db, result["payment"], correlation_id=parts["event_id"]))
    elif otype == "refund":
        refund = map_refund(fetched or fallback)
        payment_id = (fetched or fallback).get("payment_id")
        if not payment_id:
            out["skipped"] = "refund event without a payment reference"
        else:
            base: dict = {"provider": PROVIDER, "merchant_id": merchant, "provider_payment_id": payment_id,
                          "provider_event_id": parts["event_id"], "connection_id": conn.id if conn else None,
                          "refunds": [refund], "fetched_at": now}
            if ad is not None and api_error is None:
                try:
                    fresh_payment = await ad.get_payment(payment_id)
                    mapped = map_payment(fresh_payment, merchant_id=merchant, event_id=parts["event_id"],
                                         connection_id=conn.id if conn else None, fetched_at=now, from_api=True)
                    others = [r for r in (mapped.get("refunds") or []) if r.get("id") != refund.get("id")]
                    base = {**mapped, "refunds": [refund] + others}
                except (ProviderError, Unsupported) as e:
                    api_error = f"{sq.error_kind(e) if isinstance(e, ProviderError) else 'unsupported'}: {e}"
                    out["api_error"] = api_error
            try:
                result = await _upsert(db, base, correlation_id=parts["event_id"])
            except DomainError as e:
                out["skipped"] = f"refund arrived before its payment ({e.message}); reconciliation will apply it"
                out["needs_reconciliation"] = True
                result = {}
            out["payment"] = (result.get("payment") or {}).get("id")
            out["refund"] = refund.get("id")
    elif otype == "dispute":
        dispute = map_dispute(fetched or fallback)
        payment_id = ((fetched or fallback).get("disputed_payment") or {}).get("payment_id") \
            or (fetched or fallback).get("payment_id")
        if not payment_id:
            out["skipped"] = "dispute event without a payment reference"
        else:
            try:
                result = await _upsert(db, {"provider": PROVIDER, "merchant_id": merchant, "provider_payment_id": payment_id,
                                            "provider_event_id": parts["event_id"], "connection_id": conn.id if conn else None,
                                            "disputes": [dispute], "fetched_at": now}, correlation_id=parts["event_id"])
            except DomainError as e:
                out["skipped"] = f"dispute arrived before its payment ({e.message}); reconciliation will apply it"
                out["needs_reconciliation"] = True
                result = {}
            out["payment"] = (result.get("payment") or {}).get("id")
            out["dispute"] = dispute.get("id")
    elif otype == "payout":
        payload = map_payout(fetched or fallback, merchant_id=merchant, event_id=parts["event_id"],
                             connection_id=conn.id if conn else None)
        if not payload.get("provider_payment_id"):
            out["skipped"] = "payout event without an id"
        else:
            result = await _upsert(db, payload, correlation_id=parts["event_id"])
            out["payout"] = (result.get("payment") or {}).get("id")
            out["is_payout"] = True
    elif otype == "invoice":
        inv_obj = fetched or fallback
        local = await _explicit_invoice(db, order_id=inv_obj.get("order_id"), invoice_id=inv_obj.get("id"))
        ctx = _ctx(db, parts["event_id"])
        ctx.record(f"Square invoice event {ev.event_type} for {inv_obj.get('id') or oid}"
                   + (f" → obligation {local.id[:8]}" if local else " (no local obligation mapped)"),
                   entity_kind="invoice", entity_id=local.id if local else None, kind="payment",
                   state="provider_event", visibility="finance", details={"square_invoice": inv_obj.get("id")})
        out["invoice"] = inv_obj.get("id")
        out["linked_invoice_id"] = local.id if local else None

    if api_error and not out.get("skipped") and _retryable(api_error):
        # Evidence recorded from the signed payload; the reconciliation sweep retries against the API (E09).
        ev.error = f"api unavailable: {api_error}"[:1000]
        ev.processed_at = None
        conn2 = conn or await connection(db, create=True)
        kind = api_error.split(":", 1)[0].strip()
        await conn_svc.mark_failure(db, conn2, kind if kind in ("auth_expired", "rate_limited", "transient",
                                                                "schema_changed") else "transient",
                                    f"square fetch failed: {api_error}")
        out["needs_reconciliation"] = True
    else:
        ev.processed_at = datetime.now(timezone.utc)
        ev.error = f"api answer: {api_error}"[:1000] if api_error else None
        if conn is not None and out.get("api") == "fetched":
            await conn_svc.mark_success(db, conn, coverage_to=now)
    await db.commit()
    return out


@jobs.job("square.process_event")
async def _process_event_job(jctx: jobs.JobContext, payload: dict) -> dict:
    ev = await jctx.db.get(ProviderEvent, payload.get("provider_event_id"))
    if ev is None:
        return {"skipped": "event row missing"}
    if ev.processed_at is not None:
        return {"skipped": "already processed", "event": ev.provider_event_id}
    if not ev.signature_ok:
        return {"skipped": "unverified event is never processed", "event": ev.provider_event_id}
    return await process_event(jctx.db, ev)


async def enqueue_event(db: AsyncSession, ev: ProviderEvent) -> None:
    await jobs.enqueue(db, "square.process_event", {"provider_event_id": ev.id},
                       dedupe_key=f"square:event:{ev.id}")


# ── reconciliation (spec §12.4: at least every 15 minutes) ───────────────────
async def _retry_pending_events(db: AsyncSession, limit: int = 50) -> dict:
    rows = (await db.execute(select(ProviderEvent).where(
        ProviderEvent.provider == PROVIDER, ProviderEvent.processed_at.is_(None),
        ProviderEvent.signature_ok.is_(True)).order_by(ProviderEvent.received_at).limit(limit))).scalars().all()
    done, still = 0, 0
    for ev in rows:
        try:
            res = await process_event(db, ev)
        except Exception as e:  # noqa: BLE001
            log.exception("square event %s failed during reconciliation", ev.provider_event_id)
            ev.error = f"{type(e).__name__}: {e}"[:1000]
            await db.commit()
            still += 1
            continue
        if res.get("needs_reconciliation"):
            still += 1
        else:
            done += 1
    return {"retried": len(rows), "processed": done, "still_pending": still}


async def _compare_email_signals(db: AsyncSession, api_payments: list[dict], begin: datetime, end: datetime) -> list[dict]:
    """An email said a payment happened; the authenticated API is the authority. Disagreements are
    visible with evidence and never averaged away (E05/E06)."""
    claims = (await db.execute(select(Payment).where(
        Payment.provider == "claim", Payment.status == "reported"))).scalars().all()
    out: list[dict] = []
    for c in claims:
        when = c.occurred_at or c.created_at
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when is not None and not (begin <= when <= end):
            continue
        amt = Decimal(str(c.amount))
        same_amount = [p for p in api_payments
                       if p.get("amount") is not None and Decimal(str(p["amount"])) == amt
                       and (p.get("currency") or "USD") == c.currency]
        same_payer = [p for p in api_payments
                      if c.payer_email and (p.get("payer_email") or "").lower() == c.payer_email.lower()]
        if same_amount:
            continue
        kind = "amount_mismatch" if same_payer else "not_found"
        evidence = {"claim_payment_id": c.id, "claimed_amount": str(c.amount), "currency": c.currency,
                    "claimed_by": (c.report_source or {}).get("claimed_by"),
                    "sender": (c.report_source or {}).get("sender"), "subject": (c.report_source or {}).get("subject"),
                    "flags": list(c.report_flags or []),
                    "api_payments_for_payer": [{"id": p.get("provider_payment_id"), "amount": p.get("amount"),
                                                "currency": p.get("currency"), "status": p.get("status")}
                                               for p in same_payer][:5],
                    "window": {"from": _iso(begin), "to": _iso(end)}}
        body = ("The Square API has no payment matching this reported amount in the reconciled window."
                if kind == "not_found" else
                "Square shows a different amount for this payer than the email claimed.")
        await _notify_owner(db, kind="connection_issue", urgency="today",
                            title=f"Square disagreement: reported {c.amount} {c.currency} not confirmed",
                            body=body, dedupe=f"square:disagreement:{c.id}:{kind}",
                            entity_kind="payment", entity_id=c.id, payload=evidence)
        ctx = _ctx(db)
        ctx.record(f"Square reconciliation disagreement ({kind}): reported {c.amount} {c.currency} is not confirmed by the API",
                   entity_kind="payment", entity_id=c.id, kind="payment", state="disagreement", visibility="finance",
                   exception=True, details=evidence)
        out.append({"payment_id": c.id, "kind": kind, "evidence": evidence})
    return out


async def reconcile(db: AsyncSession, *, window_hours: int = RECONCILE_WINDOW_HOURS) -> dict:
    """Catch missed events and compare email signal with the API. Safe to run repeatedly."""
    conn = await connection(db)
    if conn is None or conn.status == "disconnected":
        return {"skipped": "square is not connected"}
    retried = await _retry_pending_events(db)
    try:
        ad = adapter(conn)
    except Unsupported as e:
        return {**retried, "setup_blocked": str(e)}
    now = datetime.now(timezone.utc)
    cursor = await conn_svc.cursor_get(db, conn, "reconcile")
    begin = _parse_dt(cursor.get("to")) or (now - timedelta(hours=window_hours))
    begin = max(begin - timedelta(minutes=5), now - timedelta(days=7))  # small overlap; bounded lookback
    await conn_svc.mark_attempt(db, conn)
    merchant = sq.merchant_id_of(conn) or getattr(ad, "merchant_id", "") or ""
    try:
        rows, _cursor = await ad.list_payments(begin, now)
    except (ProviderError, Unsupported) as e:
        kind = sq.error_kind(e) if isinstance(e, ProviderError) else "unsupported"
        failure = kind if kind in ("auth_expired", "permission_denied", "rate_limited", "transient", "schema_changed") else "unknown"
        await conn_svc.mark_failure(db, conn, failure, f"square list_payments failed: {e}")
        await db.commit()
        return {**retried, "error": f"{kind}: {e}"}
    seen: list[dict] = []
    for obj in rows:
        payload = map_payment(obj, merchant_id=merchant, event_id=None, connection_id=conn.id,
                              fetched_at=now, from_api=True)
        if not payload.get("provider_payment_id"):
            continue
        res = await _upsert(db, payload)
        p = res.get("payment") or {}
        seen.append({"provider_payment_id": p.get("provider_payment_id"), "amount": p.get("amount"),
                     "currency": p.get("currency"), "status": p.get("status"), "payer_email": p.get("payer_email")})
        if res.get("created") or res.get("applied"):
            await _maybe_propose(db, p)
    disagreements = await _compare_email_signals(db, seen, begin, now)
    await conn_svc.cursor_set(db, conn, "reconcile", {"to": _iso(now), "count": len(seen)})
    await conn_svc.mark_success(db, conn, coverage_to=now)
    await db.commit()
    return {**retried, "payments": len(seen), "disagreements": len(disagreements),
            "window": {"from": _iso(begin), "to": _iso(now)}}


@sweep("square.reconcile", RECONCILE_SECONDS)
async def reconcile_sweep(session_factory) -> dict:
    async with session_factory() as db:
        try:
            return await reconcile(db)
        except Unsupported as e:
            return {"setup_blocked": str(e)}
        except Exception as e:  # noqa: BLE001
            log.exception("square reconciliation failed")
            return {"error": f"{type(e).__name__}: {e}"}


# ── read helpers for the settings / finance surfaces ─────────────────────────
def event_brief(ev: ProviderEvent) -> dict:
    parts = event_parts(dict(ev.payload or {}))
    return {"id": ev.id, "provider_event_id": ev.provider_event_id, "merchant_id": ev.connection_key,
            "type": ev.event_type, "object_type": parts["object_type"], "object_id": parts["object_id"],
            "received_at": _iso(ev.received_at), "processed_at": _iso(ev.processed_at),
            "signature_ok": bool(ev.signature_ok), "error": ev.error}


async def recent_events(db: AsyncSession, limit: int = 25) -> list[dict]:
    rows = (await db.execute(select(ProviderEvent).where(ProviderEvent.provider == PROVIDER)
                             .order_by(ProviderEvent.received_at.desc()).limit(limit))).scalars().all()
    return [event_brief(r) for r in rows]


def signal_fingerprint(sender: str | None, subject: str | None, amount, occurred_at: datetime | None) -> str:
    """Stable dedupe reference for an email signal when the message id is unavailable."""
    return sha256_hex("|".join([(sender or "").lower(), (subject or "").lower(), str(amount),
                                _iso(occurred_at) or ""]))[:32]
