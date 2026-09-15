"""Deterministic provider fixtures for the integration slice (spec §12.3).

Every live provider is reached through an interface with an in-memory implementation; these builders
assemble the exact scenarios the acceptance tests need so that **no test ever touches the network**:

* `square_fake()`     — payments, refunds, disputes, payouts, invoices and customers (E05, E06, E09).
* `drive_fake()`      — two folders named "Dylan Nail Shipments", one evidenced vehicle folder holding
                        photos + a customer ID scan + an invoice + shipping papers, a duplicate photo,
                        a corrected version and a folder that gets moved out of the root (F01–F03, A09).
* `sheets_fake()`     — a ledger spreadsheet with values and formulas (§6.1).
* `wordpress_fake()`  — an existing product for a vehicle with no local mapping, a manual price edit by
                        a human editor, a public page that lags the API, and a write that is accepted
                        just before the connection dies (F04–F08).

`install_*` context managers put a fake in the adapter registry and always remove it again.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone

from backend.app.adapters import drive as drive_adapter
from backend.app.adapters import sheets as sheets_adapter
from backend.app.adapters import sheets_ledger
from backend.app.adapters import square as square_adapter
from backend.app.adapters import wordpress as wp_adapter
from backend.app.core.config import settings
from backend.app.services import connections as conn_svc

SQUARE_SIGNATURE_KEY = "test-square-signature-key"
SQUARE_NOTIFICATION_URL = "http://testserver/api/webhooks/square"
MERCHANT = "MERCH-AZKT"
LOCATION = "LOC-PHX"


# ── small deterministic media ────────────────────────────────────────────────
def jpeg(seed: int = 1, size: tuple[int, int] = (320, 240)) -> bytes:
    from PIL import Image
    im = Image.new("RGB", size, ((seed * 37) % 256, (seed * 91) % 256, (seed * 53) % 256))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=75)
    return buf.getvalue()


def pdf(text: str = "invoice") -> bytes:
    body = f"1 0 obj<</Type/Catalog>>endobj % {text}".encode()
    return b"%PDF-1.4\n" + body + b"\ntrailer<</Root 1 0 R>>\n%%EOF\n"


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


# ── Square ───────────────────────────────────────────────────────────────────
def money(amount_minor: int, currency: str = "USD") -> dict:
    return {"amount": amount_minor, "currency": currency}


def square_payment(payment_id: str, *, amount_minor: int = 250000, status: str = "COMPLETED",
                   created_at: str | None = None, order_id: str | None = None, invoice_id: str | None = None,
                   customer_id: str | None = None, buyer_email: str | None = None, fee_minor: int = 7500,
                   refunds: list[dict] | None = None, refunded_minor: int | None = None,
                   version: int = 1) -> dict:
    out = {"id": payment_id, "status": status, "amount_money": money(amount_minor),
           "processing_fee": [{"amount_money": money(fee_minor)}],
           "created_at": created_at or "2026-09-14T17:00:00.000Z", "updated_at": "2026-09-14T17:05:00.000Z",
           "location_id": LOCATION, "version": version, "source_type": "CARD"}
    if order_id:
        out["order_id"] = order_id
    if invoice_id:
        out["invoice_id"] = invoice_id
    if customer_id:
        out["customer_id"] = customer_id
    if buyer_email:
        out["buyer_email_address"] = buyer_email
    if refunds:
        out["refunds"] = refunds
    if refunded_minor is not None:
        out["refunded_money"] = money(refunded_minor)
    return out


def square_refund(refund_id: str, payment_id: str, *, amount_minor: int, status: str = "COMPLETED",
                  reason: str = "customer changed the spec") -> dict:
    return {"id": refund_id, "payment_id": payment_id, "status": status, "amount_money": money(amount_minor),
            "created_at": "2026-09-14T19:00:00.000Z", "reason": reason, "location_id": LOCATION}


def square_dispute(dispute_id: str, payment_id: str, *, amount_minor: int, state: str = "EVIDENCE_REQUIRED") -> dict:
    return {"id": dispute_id, "state": state, "amount_money": money(amount_minor),
            "disputed_payment": {"payment_id": payment_id}, "reason": "NO_KNOWLEDGE",
            "created_at": "2026-09-14T20:00:00.000Z"}


def square_payout(payout_id: str, *, amount_minor: int = 240000, status: str = "PAID") -> dict:
    return {"id": payout_id, "status": status, "amount_money": money(amount_minor),
            "created_at": "2026-09-15T02:00:00.000Z", "destination": {"type": "BANK_ACCOUNT"},
            "location_id": LOCATION, "version": 1}


def square_event(event_id: str, event_type: str, object_type: str, obj: dict, *, merchant_id: str = MERCHANT) -> dict:
    """The exact envelope Square posts (object nested under its own key)."""
    return {"merchant_id": merchant_id, "type": event_type, "event_id": event_id,
            "created_at": "2026-09-14T17:05:01Z", "data": {"type": object_type, "id": obj.get("id"),
                                                           "object": {object_type: obj}}}


def square_headers(body: bytes, *, key: str = SQUARE_SIGNATURE_KEY, url: str = SQUARE_NOTIFICATION_URL) -> dict:
    return {"x-square-hmacsha256-signature": square_adapter.signature_for(url, body, key),
            "content-type": "application/json"}


def square_body(payload: dict) -> bytes:
    return json.dumps(payload).encode()


def square_fake() -> square_adapter.FakeSquare:
    """A merchant with one completed deposit tied to an invoice, one payment identified only by
    customer id, a refund, a dispute and a bank payout."""
    fake = square_adapter.FakeSquare(merchant_id=MERCHANT, location_id=LOCATION)
    fake.customers["CUST-1"] = {"id": "CUST-1", "given_name": "Ana", "family_name": "Reyes",
                                "email_address": "ana@example.com"}
    fake.invoices["SQINV-1"] = {"id": "SQINV-1", "order_id": "SQORD-1", "status": "PAID",
                                "primary_recipient": {"customer_id": "CUST-1"}}
    fake.add_payment(square_payment("SQP-DEPOSIT", amount_minor=250000, invoice_id="SQINV-1", order_id="SQORD-1",
                                    customer_id="CUST-1", buyer_email="ana@example.com"))
    fake.add_payment(square_payment("SQP-CUSTOMER-ONLY", amount_minor=100000, customer_id="CUST-1",
                                    buyer_email="ana@example.com", created_at="2026-09-14T18:00:00.000Z"))
    fake.payouts["SQPAYOUT-1"] = square_payout("SQPAYOUT-1")
    fake.refunds["SQR-1"] = square_refund("SQR-1", "SQP-DEPOSIT", amount_minor=50000)
    return fake


@contextlib.contextmanager
def install_square(fake=None):
    fake = fake or square_fake()
    prev_key, prev_url = settings.SQUARE_WEBHOOK_SIGNATURE_KEY, settings.SQUARE_NOTIFICATION_URL
    settings.SQUARE_WEBHOOK_SIGNATURE_KEY = SQUARE_SIGNATURE_KEY
    settings.SQUARE_NOTIFICATION_URL = SQUARE_NOTIFICATION_URL
    square_adapter.set_adapter(fake)
    try:
        yield fake
    finally:
        square_adapter.set_adapter(None)
        settings.SQUARE_WEBHOOK_SIGNATURE_KEY, settings.SQUARE_NOTIFICATION_URL = prev_key, prev_url


async def square_connection(db, *, merchant_id: str = MERCHANT, connect: bool = True):
    conn = await conn_svc.get(db, "square", create=True)
    conn.config = {**(conn.config or {}), "merchant_id": merchant_id, "location_id": LOCATION,
                   "notification_url": SQUARE_NOTIFICATION_URL}
    conn_svc.set_secret(conn, {"access_token": "test-square-token", "location_id": LOCATION,
                               "webhook_signature_key": SQUARE_SIGNATURE_KEY})
    conn.account_identity = merchant_id
    conn.status = "connected" if connect else "disconnected"
    conn.connected_at = conn.connected_at or datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(conn)
    return conn


# ── Drive ────────────────────────────────────────────────────────────────────
ROOT_A = "FOLDER-ROOT-A"
ROOT_B = "FOLDER-ROOT-B"          # a second folder with the identical name (A09)
FOLDER_EVIDENCED = "FOLDER-STK-0412"
FOLDER_LOOKALIKE = "FOLDER-ACTY"
FOLDER_MOVED = "FOLDER-MOVED"
OUTSIDE = "FOLDER-OUTSIDE"


def drive_fake(*, owner: str = "dylxnxil@gmail.com", salt: int = 0) -> drive_adapter.FakeDrive:
    """`salt` shifts every image/PDF byte string so two scenarios in one test database never
    deduplicate against each other's stored assets."""
    d = drive_adapter.FakeDrive(owner=owner)
    d.add_folder(ROOT_A, "Dylan Nail Shipments", None, owner=owner)
    d.add_folder(ROOT_B, "Dylan Nail Shipments", None, owner=owner)   # duplicate name: never guessed
    d.add_folder(OUTSIDE, "Archive 2025", None, owner=owner)

    # an evidenced folder: the name and the file names agree on the same stock reference (F01)
    d.add_folder(FOLDER_EVIDENCED, "STK-0412 Hijet jumbo", ROOT_A, owner=owner)
    d.add_file("F-PHOTO-1", "STK-0412 front.jpg", FOLDER_EVIDENCED, jpeg(1 + salt))
    d.add_file("F-PHOTO-2", "STK-0412 rear.jpg", FOLDER_EVIDENCED, jpeg(2 + salt))
    d.add_file("F-PHOTO-DUP", "STK-0412 front (copy).jpg", FOLDER_EVIDENCED, jpeg(1 + salt))     # identical bytes (F03)
    d.add_file("F-ID", "customer id scan.jpg", FOLDER_EVIDENCED, jpeg(3 + salt))                 # sensitive (F02)
    d.add_file("F-INVOICE", "invoice 8891.pdf", FOLDER_EVIDENCED, pdf(f"8891-{salt}"), mime="application/pdf")
    d.add_file("F-BL", "bill of lading STK-0412.pdf", FOLDER_EVIDENCED, pdf(f"bl-{salt}"), mime="application/pdf")

    # a visually similar truck with no unique reference anywhere: a proposal at best (F01)
    d.add_folder(FOLDER_LOOKALIKE, "2019 Silver Acty", ROOT_A, owner=owner)
    d.add_file("F-ACTY-1", "silver acty side.jpg", FOLDER_LOOKALIKE, jpeg(4 + salt))
    d.add_file("F-ACTY-2", "silver acty interior.jpg", FOLDER_LOOKALIKE, jpeg(5 + salt))

    # a folder that later leaves the selected root
    d.add_folder(FOLDER_MOVED, "STK-0999 spare parts", ROOT_A, owner=owner)
    d.add_file("F-MOVED-1", "parts box.jpg", FOLDER_MOVED, jpeg(6 + salt))
    return d


@contextlib.contextmanager
def install_drive(fake=None):
    fake = fake or drive_fake()
    drive_adapter.set_adapter(fake)
    try:
        yield fake
    finally:
        drive_adapter.set_adapter(None)


async def drive_connection(db, *, connect: bool = True, scopes: list[str] | None = None):
    conn = await conn_svc.get(db, "drive", create=True)
    conn.granted_scopes = scopes if scopes is not None else list(drive_adapter.READ_SCOPES[:1])
    conn.status = "connected" if connect else "disconnected"
    conn.connected_at = conn.connected_at or datetime.now(timezone.utc)
    conn.account_identity = "dylxnxil@gmail.com"
    await db.commit()
    await db.refresh(conn)
    return conn


# ── Sheets (live ledger source) ──────────────────────────────────────────────
LEDGER_SHEET = "SHEET-LEDGER-1"


def sheets_fake() -> sheets_adapter.FakeSheets:
    src = sheets_adapter.FakeSheets()
    src.add_sheet(LEDGER_SHEET, "AZKT Ledger 2026", {
        "Costs": {"tab_id": "0", "headers": ["Date", "Vehicle", "Supplier", "Description", "Amount", "Currency", "Total"],
                  "rows": [["2026-08-02", "STK-0412", "Kanto Parts", "Water pump", "120.00", "USD", "132.00"],
                           ["2026-08-05", "STK-0412", "Yamato", "Freight", "310.00", "USD", "341.00"],
                           ["2026-08-09", "STK-0999", "Kanto Parts", "Tyres", "480.00", "USD", "528.00"]],
                  "formulas": {(0, "Total"): "=E2*1.1", (1, "Total"): "=E3*1.1", (2, "Total"): "=E4*1.1"}},
        "Notes": {"tab_id": "1", "headers": ["Note"], "rows": [["not a ledger tab"]]},
    }, revision="7")
    return src


@contextlib.contextmanager
def install_sheets(src=None):
    src = src or sheets_fake()
    sheets_ledger.set_source(src)
    try:
        yield src
    finally:
        sheets_ledger.set_source(None)


async def sheets_connection(db, *, connect: bool = True, scopes: list[str] | None = None):
    conn = await conn_svc.get(db, "sheets", create=True)
    conn.granted_scopes = scopes if scopes is not None else list(sheets_adapter.requested_scopes())
    conn.status = "connected" if connect else "disconnected"
    conn.connected_at = conn.connected_at or datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(conn)
    return conn


# ── WordPress / WooCommerce ──────────────────────────────────────────────────
SITE_URL = "https://azkeitrucks.example"
STAGING_URL = "https://staging.azkeitrucks.example"
EXISTING_PRODUCT_ID = "501"


def wordpress_fake(*, with_existing: bool = True, custom_type: bool = False) -> wp_adapter.FakeWordPress:
    site = wp_adapter.FakeWordPress(base_url=SITE_URL)
    if custom_type:
        site.add_custom_type("azkt_vehicle", "Vehicle", rest_base="vehicles")
    if with_existing:
        # a live product for a truck AZKT has no mapping for yet (F05)
        site.add_product(sku="STK-0412", name="2018 Daihatsu Hijet Jumbo", price="12500.00",
                         external_id=EXISTING_PRODUCT_ID)
    return site


@contextlib.contextmanager
def install_wordpress(site=None):
    site = site or wordpress_fake()
    wp_adapter.set_adapter(site)
    try:
        yield site
    finally:
        wp_adapter.set_adapter(None)


async def wordpress_connections(db, *, connect: bool = True, staging_url: str = STAGING_URL):
    wp = await conn_svc.get(db, "wordpress", create=True)
    wp.config = {**(wp.config or {}), "site_url": SITE_URL, "staging_url": staging_url}
    conn_svc.set_secret(wp, {"app_user": "azkt-integration", "app_password": "test-app-password"})
    wp.status = "connected" if connect else "disconnected"
    woo = await conn_svc.get(db, "woocommerce", create=True)
    woo.config = {**(woo.config or {}), "site_url": SITE_URL}
    conn_svc.set_secret(woo, {"consumer_key": "ck_test", "consumer_secret": "cs_test"})
    woo.status = "connected" if connect else "disconnected"
    await db.commit()
    return wp, woo


# ── shared helpers ───────────────────────────────────────────────────────────
async def run_jobs(limit: int = 50) -> int:
    """Run due jobs without the periodic sweeps (sweeps touch other domains' connections)."""
    from backend.app import db as dbmod
    from backend.app.domain import jobs as jobs_mod
    total = 0
    for _ in range(5):
        n = await jobs_mod.run_due(dbmod.SessionLocal, "test-worker", limit)
        total += n
        if n == 0:
            break
    return total


async def drain_events(max_rounds: int = 25) -> int:
    from backend.app import db as dbmod
    from backend.app.domain import events as events_mod
    total = 0
    for _ in range(max_rounds):
        n = await events_mod.dispatch_pending(dbmod.SessionLocal, limit=500)
        total += n
        if n == 0:
            break
    return total


def hours_ago(n: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=n)
