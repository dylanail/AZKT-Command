"""Square adapter (spec §6.3, §12.3). Read and reconcile only.

Nothing here charges a card, issues a refund, moves money or changes Square business configuration:
those capabilities return `Unsupported` so the caller can never mistake silence for success.

* `verify_signature()` implements Square's webhook validation: HMAC-SHA256 over
  `notification_url + raw_body` with the subscription's signature key, base64 encoded, compared in
  constant time (https://developer.squareup.com/docs/webhooks/step3validate).
* `SquareAdapter` is the live HTTP client with typed errors
  (invalid_input | permission_denied | auth_expired | rate_limited | transient | schema_changed |
  conflict | unknown_result).
* `FakeSquare` is the in-memory implementation used by tests: it serves fixtures, records every call
  and can simulate a disconnected merchant. Tests never touch the network.
* `adapter_for()` resolves the connection secret first and the environment variable second; when
  neither is present the caller gets `Unsupported` ("setup blocked"), never a fake empty success.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime
from typing import Any

from ..core.config import settings
from ..core.errors import ProviderError, Unsupported
from ..models.comms import Connection

SQUARE_VERSION = "2025-07-16"
BASE_URLS = {"production": "https://connect.squareup.com", "sandbox": "https://connect.squareupsandbox.com"}
ERROR_KINDS = ("invalid_input", "permission_denied", "auth_expired", "rate_limited", "transient",
               "schema_changed", "conflict", "unknown_result")


def error_kind(exc: Exception) -> str:
    """The typed kind carried by a ProviderError (spec §12.3)."""
    if isinstance(exc, ProviderError):
        return str((exc.detail or {}).get("kind") or "unknown_result")
    return "unknown_result"


def _fail(kind: str, message: str, **extra) -> ProviderError:
    return ProviderError(message, kind=kind, provider="square", **extra)


# ── webhook signature (spec §6.3) ────────────────────────────────────────────
def signature_for(notification_url: str, raw_body: bytes, signature_key: str) -> str:
    mac = hmac.new(signature_key.encode(), (notification_url or "").encode() + (raw_body or b""), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


def verify_signature(notification_url: str, raw_body: bytes, header: str | None, signature_key: str) -> bool:
    """True only when the header matches the HMAC of url+body. Missing key or header is never 'ok'."""
    if not signature_key or not header or not notification_url:
        return False
    try:
        expected = signature_for(notification_url, raw_body, signature_key)
    except Exception:  # noqa: BLE001
        return False
    return hmac.compare_digest(expected, header.strip())


# ── normalized helpers shared by the live and fake clients ───────────────────
def money(obj: dict | None) -> tuple[str | None, str | None]:
    """Square money ({amount: minor units, currency}) → (decimal string, currency)."""
    if not isinstance(obj, dict) or obj.get("amount") is None:
        return None, None
    cur = (obj.get("currency") or "USD").upper()
    minor = int(obj["amount"])
    if cur in ("JPY", "KRW"):
        return str(minor), cur
    whole, frac = divmod(abs(minor), 100)
    return f"{'-' if minor < 0 else ''}{whole}.{frac:02d}", cur


class SquareCapabilities:
    READ = ("get_payment", "get_order", "get_invoice", "list_payments", "get_customer", "get_payout")
    WRITE: tuple[str, ...] = ()   # spec §6.3: no charging, refunding or configuration changes


class SquareAdapter:
    """Live Square client. Every method returns the provider object or raises a typed ProviderError."""

    kind = "live"

    def __init__(self, token: str, environment: str = "sandbox", *, merchant_id: str | None = None,
                 location_id: str | None = None, base_url: str | None = None, timeout: float = 30.0):
        if not token:
            raise Unsupported("Square access token is not configured (setup blocked)")
        self.token = token
        self.environment = environment if environment in BASE_URLS else "sandbox"
        self.merchant_id = merchant_id
        self.location_id = location_id
        self.base_url = (base_url or BASE_URLS[self.environment]).rstrip("/")
        self.timeout = timeout

    def supports(self, capability: str) -> bool:
        return capability in SquareCapabilities.READ

    async def _get(self, path: str, params: dict | None = None) -> dict:
        import httpx
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.get(url, params=params or None,
                                headers={"Authorization": f"Bearer {self.token}", "Square-Version": SQUARE_VERSION,
                                         "Accept": "application/json"})
        except httpx.TimeoutException as e:
            raise _fail("transient", f"square timeout: {e}") from e
        except httpx.HTTPError as e:
            raise _fail("transient", f"square transport error: {e}") from e
        return self._parse(r.status_code, r.text, path)

    def _parse(self, status: int, text: str, path: str) -> dict:
        import json
        if status == 401:
            raise _fail("auth_expired", "square token rejected (401); reconnect Square")
        if status == 403:
            raise _fail("permission_denied", "square permission denied (403)")
        if status == 404:
            raise _fail("invalid_input", f"square object not found: {path}")
        if status == 409:
            raise _fail("conflict", "square reported a conflict (409)")
        if status == 429:
            raise _fail("rate_limited", "square rate limited (429)")
        if status >= 500:
            raise _fail("transient", f"square server error ({status})")
        try:
            body = json.loads(text or "{}")
        except ValueError as e:
            raise _fail("schema_changed", "square returned a non-JSON body") from e
        if status >= 400:
            errs = body.get("errors") or [{}]
            raise _fail("invalid_input", f"square error: {errs[0].get('detail') or errs[0].get('code') or status}",
                        errors=errs)
        if not isinstance(body, dict):
            raise _fail("schema_changed", "square returned an unexpected payload shape")
        return body

    # ── reads ────────────────────────────────────────────────────────────
    async def get_payment(self, payment_id: str) -> dict:
        return (await self._get(f"/v2/payments/{payment_id}")).get("payment") or {}

    async def get_order(self, order_id: str) -> dict:
        return (await self._get(f"/v2/orders/{order_id}")).get("order") or {}

    async def get_invoice(self, invoice_id: str) -> dict:
        return (await self._get(f"/v2/invoices/{invoice_id}")).get("invoice") or {}

    async def get_customer(self, customer_id: str) -> dict:
        return (await self._get(f"/v2/customers/{customer_id}")).get("customer") or {}

    async def get_payout(self, payout_id: str) -> dict:
        return (await self._get(f"/v2/payouts/{payout_id}")).get("payout") or {}

    async def get_refund(self, refund_id: str) -> dict:
        return (await self._get(f"/v2/refunds/{refund_id}")).get("refund") or {}

    async def list_payments(self, begin: datetime | None = None, end: datetime | None = None, *,
                            cursor: str | None = None, limit: int = 100) -> tuple[list[dict], str | None]:
        params: dict[str, Any] = {"limit": limit}
        if begin:
            params["begin_time"] = begin.isoformat()
        if end:
            params["end_time"] = end.isoformat()
        if cursor:
            params["cursor"] = cursor
        if self.location_id:
            params["location_id"] = self.location_id
        body = await self._get("/v2/payments", params)
        return list(body.get("payments") or []), body.get("cursor")

    # ── explicitly unsupported (spec §6.3: read/reconcile only) ──────────
    async def refund_payment(self, *_a, **_kw) -> dict:
        raise Unsupported("this integration never issues Square refunds; refund in Square and it reconciles here")

    async def create_payment(self, *_a, **_kw) -> dict:
        raise Unsupported("this integration never charges cards")

    async def update_settings(self, *_a, **_kw) -> dict:
        raise Unsupported("this integration never changes Square business configuration")


class FakeSquare:
    """Deterministic in-memory Square used by tests and by the setup preview.

    `payments`/`orders`/`invoices`/`customers`/`payouts` are plain Square resources so the same
    normalizer runs in tests and in production. `offline=True` makes every call raise the same typed
    auth_expired error a disconnected merchant produces.
    """

    kind = "fake"

    def __init__(self, merchant_id: str = "MERCH-TEST", location_id: str = "LOC-1"):
        self.merchant_id = merchant_id
        self.location_id = location_id
        self.environment = "sandbox"
        self.payments: dict[str, dict] = {}
        self.orders: dict[str, dict] = {}
        self.invoices: dict[str, dict] = {}
        self.customers: dict[str, dict] = {}
        self.payouts: dict[str, dict] = {}
        self.refunds: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.offline = False
        self.rate_limited_once = False

    # test helpers
    def add_payment(self, payment: dict) -> dict:
        self.payments[payment["id"]] = payment
        return payment

    def supports(self, capability: str) -> bool:
        return capability in SquareCapabilities.READ

    def _check(self, op: str, ident: str) -> None:
        self.calls.append((op, ident))
        if self.offline:
            raise _fail("auth_expired", "square is disconnected")
        if self.rate_limited_once:
            self.rate_limited_once = False
            raise _fail("rate_limited", "square rate limited (429)")

    async def get_payment(self, payment_id: str) -> dict:
        self._check("get_payment", payment_id)
        p = self.payments.get(payment_id)
        if p is None:
            raise _fail("invalid_input", f"square payment {payment_id} not found")
        return dict(p)

    async def get_order(self, order_id: str) -> dict:
        self._check("get_order", order_id)
        o = self.orders.get(order_id)
        if o is None:
            raise _fail("invalid_input", f"square order {order_id} not found")
        return dict(o)

    async def get_invoice(self, invoice_id: str) -> dict:
        self._check("get_invoice", invoice_id)
        i = self.invoices.get(invoice_id)
        if i is None:
            raise _fail("invalid_input", f"square invoice {invoice_id} not found")
        return dict(i)

    async def get_customer(self, customer_id: str) -> dict:
        self._check("get_customer", customer_id)
        c = self.customers.get(customer_id)
        if c is None:
            raise _fail("invalid_input", f"square customer {customer_id} not found")
        return dict(c)

    async def get_payout(self, payout_id: str) -> dict:
        self._check("get_payout", payout_id)
        p = self.payouts.get(payout_id)
        if p is None:
            raise _fail("invalid_input", f"square payout {payout_id} not found")
        return dict(p)

    async def get_refund(self, refund_id: str) -> dict:
        self._check("get_refund", refund_id)
        r = self.refunds.get(refund_id)
        if r is None:
            raise _fail("invalid_input", f"square refund {refund_id} not found")
        return dict(r)

    async def list_payments(self, begin: datetime | None = None, end: datetime | None = None, *,
                            cursor: str | None = None, limit: int = 100) -> tuple[list[dict], str | None]:
        self._check("list_payments", f"{begin}..{end}")
        rows = []
        for p in self.payments.values():
            created = p.get("created_at")
            if begin and created and created < begin.isoformat():
                continue
            if end and created and created > end.isoformat():
                continue
            rows.append(dict(p))
        rows.sort(key=lambda r: r.get("created_at") or "")
        return rows[:limit], None

    async def refund_payment(self, *_a, **_kw) -> dict:
        raise Unsupported("this integration never issues Square refunds")

    async def create_payment(self, *_a, **_kw) -> dict:
        raise Unsupported("this integration never charges cards")

    async def update_settings(self, *_a, **_kw) -> dict:
        raise Unsupported("this integration never changes Square business configuration")


# ── factory (connection secret first, environment variable second) ───────────
_ADAPTER: Any | None = None


def set_adapter(adapter: Any | None) -> None:
    """Tests and the setup preview install the active client here (never a live call in tests)."""
    global _ADAPTER
    _ADAPTER = adapter


def current_adapter() -> Any | None:
    return _ADAPTER


def webhook_signature_key(conn: Connection | None) -> str:
    from ..services import connections as conn_svc
    if conn is not None:
        sec = conn_svc.get_secret(conn)
        if sec.get("webhook_signature_key"):
            return str(sec["webhook_signature_key"])
    return settings.SQUARE_WEBHOOK_SIGNATURE_KEY or ""


def notification_url(conn: Connection | None) -> str:
    if conn is not None and (conn.config or {}).get("notification_url"):
        return str(conn.config["notification_url"])
    return settings.SQUARE_NOTIFICATION_URL or ""


def merchant_id_of(conn: Connection | None) -> str:
    if conn is not None:
        return str((conn.config or {}).get("merchant_id") or conn.account_identity or "")
    return ""


def adapter_for(conn: Connection | None) -> Any:
    """The active Square client. Raises `Unsupported` (setup blocked) when no credential exists."""
    if _ADAPTER is not None:
        return _ADAPTER
    from ..services import connections as conn_svc
    secret = conn_svc.get_secret(conn) if conn is not None else {}
    token = secret.get("access_token") or settings.SQUARE_ACCESS_TOKEN
    if not token:
        raise Unsupported("Square is not connected (no access token); connect it under Settings → Connections",
                          setup_blocked="square.access_token")
    if settings.ENV == "test":
        # a live client is never constructed in tests: install a fixture with `set_adapter`
        raise Unsupported("live Square calls are disabled in tests; install a fixture adapter")
    cfg = (conn.config or {}) if conn is not None else {}
    return SquareAdapter(token, settings.SQUARE_ENVIRONMENT,
                         merchant_id=merchant_id_of(conn) or None,
                         location_id=secret.get("location_id") or cfg.get("location_id"))
