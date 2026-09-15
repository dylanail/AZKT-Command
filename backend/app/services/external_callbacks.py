"""Signed callbacks to an external client's owner-configured destination (spec §10.8).

Polling stays the baseline. `GET /api/integrations/v1/work/{request_id}?cursor=` is always the truth, and a
client that has no callback configured is polling-only — nothing is ever pushed to it. This module adds the
optional half of the spec sentence: *"optional signed callbacks use owner-configured destinations and never
arbitrary URLs supplied in prompts."*

Shape, borrowed wholesale from `services.reminders` because the problem is identical:

    mission finishes ──► `@on_event("mission.updated")` writes ONE `CallbackDelivery` row (transactional,
                          deduped by `(request, terminal state, cursor)`)
                     ──► `@sweep("external_callbacks.deliver_due")` claims it, signs it, POSTs it once and
                          records what actually happened.

The enqueue never does network I/O (it runs inside the outbox's savepoint), and the send never invents a
result: a 2xx is `accepted`, a refusal is `failed` with its status, and a connection that broke *after* the
request went out is `unknown` — it may have arrived, so it is neither called delivered nor blindly re-sent.

Destination safety, none of it optional:

* The destination is read from `ExternalClient.callback_url` **at send time**, never from the delivery row's
  snapshot and never from anything a caller said. A URL in a prompt, a request payload or model output is
  ignored (`routers/integrations_v1` already strips those keys and reports them back as `ignored_fields`).
* https only. It is validated when the owner sets it, and checked again here, because a row written before
  that validation existed is still a row.
* Every send passes `core.destinations.assert_destination_allowed` (H08), exactly like `adapters/email` and
  `adapters/wordpress`: outside production a real customer endpoint is unreachable unless the owner put it in
  `NON_PROD_DESTINATION_ALLOWLIST`.
* The signing secret is stored encrypted (`core.crypto`) and is decrypted only inside `_deliver_one`, for the
  length of one HMAC. It is never logged, never written to an activity row, never returned by an API and
  never placed in an error message.

────────────────────────────────────────────────────────────────────────────────────────────────────────────
VERIFYING A CALLBACK — the exact recipe for the receiving agent
────────────────────────────────────────────────────────────────────────────────────────────────────────────
AZKT sends `POST <your callback URL>` with `Content-Type: application/json` and these headers:

    X-AZKT-Signature: v1=<hex>     HMAC-SHA256, lowercase hex
    X-AZKT-Timestamp: <unix seconds, UTC, as a decimal string>
    X-AZKT-Delivery:  <delivery id — stable across retries of the same delivery>
    X-AZKT-Attempt:   <1-based attempt number for this delivery>
    X-AZKT-Event:     work.completed | work.failed | work.needs_input | callback.test
    X-AZKT-Client:    <your client id>

To verify, in this order:

 1. Read the **raw request body as bytes**. Do not parse and re-serialize it first — JSON key order and
    spacing would change and the signature would not match.
 2. Reject the request unless `X-AZKT-Timestamp` is within 300 seconds of your own clock. This is what makes
    a captured delivery un-replayable; without it a signature is valid forever.
 3. Build the signed string as bytes:  `f"{timestamp}.".encode() + raw_body`  (the timestamp, an ASCII full
    stop, then the body bytes).
 4. `expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()` where `secret` is the value
    you gave the owner to paste into Settings → External agents → Set callback.
 5. Strip the `v1=` prefix from `X-AZKT-Signature` and compare with `hmac.compare_digest(expected, got)`.
    Never `==`.
 6. Only now parse the JSON.

    import hashlib, hmac, time
    def azkt_callback_is_valid(raw_body: bytes, headers, secret: str, tolerance: int = 300) -> bool:
        ts = headers.get("X-AZKT-Timestamp", "")
        sig = headers.get("X-AZKT-Signature", "")
        if not ts.isdigit() or abs(time.time() - int(ts)) > tolerance:
            return False                                   # replayed or badly clocked
        expected = hmac.new(secret.encode(), f"{ts}.".encode() + raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig.split("=", 1)[-1])

Answer 2xx as soon as you have stored the delivery; anything else is a failure AZKT records and shows the
owner. Treat `X-AZKT-Delivery` as the idempotency key: the same id arriving twice is one event AZKT was not
sure reached you, never two pieces of work. The body is byte-identical on every attempt, so hashing it works
as a second dedupe key.

The body is the same envelope `GET /work/{request_id}` returns — `request_id`, `mission_id`, typed `state`,
`summary`, `changed` record ids/versions, `citations`, `receipts`, `needed_input`, `error`, `review_links`
(authenticated deep links), `cursor`, `updates` — plus `delivery_id`, `event`, `client_id` and
`signature_version`. A client can act on it without a second call, and `cursor` is what it should send on its
next poll so nothing is replayed as new.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.crypto import decrypt
from ..core.destinations import assert_destination_allowed
from ..core.errors import Blocked, NotFound, ProviderError, ValidationFailed
from ..core.ids import new_id
from ..domain.actors import SYSTEM_ACTOR
from ..domain.commands import CommandContext, command
from ..domain.events import on_event
from ..domain.jobs import sweep
from ..models.external import CallbackDelivery, DelegatedRequest, ExternalClient
from ..models.runtime import Mission

log = logging.getLogger("azkt.connector.callbacks")

# Engineering constants live here rather than in core/config: a callback is an internal delivery mechanism,
# not something the owner tunes from Settings, and nothing here is deployment specific.
SIGNATURE_VERSION = "v1"
SIGNATURE_HEADER = "X-AZKT-Signature"
TIMESTAMP_HEADER = "X-AZKT-Timestamp"
DELIVERY_HEADER = "X-AZKT-Delivery"
ATTEMPT_HEADER = "X-AZKT-Attempt"
EVENT_HEADER = "X-AZKT-Event"
CLIENT_HEADER = "X-AZKT-Client"
REPLAY_TOLERANCE_SECONDS = 300        # the window the receiver must enforce; documented in openapi_lite
MAX_ATTEMPTS = 5                      # bounded: a destination that never answers is a visible problem, not a loop
BACKOFF_SECONDS = (30, 120, 600, 1800)  # after attempt 1, 2, 3, 4+ — the same shape as the domain outbox
TIMEOUT_SECONDS = 15.0
CLAIM_BATCH = 25
LEASE_SECONDS = 120
MAX_RETRY_AFTER_SECONDS = 3600

# A delegated request is finished *for the client* in these states: AZKT will not move it on its own again.
# `cancelled` is deliberately absent — the client cancelled it itself, or the owner revoked the grant, and a
# withdrawn grant is not a destination AZKT keeps pushing to.
TERMINAL_REQUEST_STATES = ("done", "failed", "needs_input")
EVENT_FOR_STATE = {"done": "work.completed", "failed": "work.failed", "needs_input": "work.needs_input"}
TEST_EVENT = "callback.test"

# A 4xx AZKT cannot fix by waiting. Retrying these for ever would turn the owner's broken endpoint into a
# permanent outbound loop instead of one visible failure. 408/425/429 are excluded on purpose: those mean
# "not now", which is exactly what backoff is for.
PERMANENT_STATUS = frozenset({400, 401, 403, 404, 405, 406, 409, 410, 411, 413, 414, 415, 418, 422, 451})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ── signing ──────────────────────────────────────────────────────────────────
def canonical_body(payload: dict) -> bytes:
    """The exact bytes that are signed and sent. Deterministic, so every attempt of one delivery is
    byte-identical and a receiver may hash the body as a second idempotency key."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()


def signed_string(timestamp: str, raw_body: bytes) -> bytes:
    """`<timestamp>.<raw body>` — the timestamp is inside the MAC so a captured body cannot be replayed
    under a fresh timestamp, and it travels in its own header so the receiver can reject a stale one."""
    return f"{timestamp}.".encode() + raw_body


def sign(secret: str, timestamp: str, raw_body: bytes) -> str:
    return hmac.new(secret.encode(), signed_string(timestamp, raw_body), hashlib.sha256).hexdigest()


def signature_header(secret: str, timestamp: str, raw_body: bytes) -> str:
    return f"{SIGNATURE_VERSION}={sign(secret, timestamp, raw_body)}"


def verify(secret: str, timestamp: str, raw_body: bytes, header_value: str | None,
           *, tolerance: int = REPLAY_TOLERANCE_SECONDS, now: float | None = None) -> bool:
    """The receiver's side of the recipe above. AZKT never calls this in production — it exists so the
    contract is executable, and so the tests verify the same way the owner's agent will have to."""
    if not header_value or not (timestamp or "").isdigit():
        return False
    if abs((now if now is not None else time.time()) - int(timestamp)) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, timestamp, raw_body), header_value.split("=", 1)[-1])


# ── the body ─────────────────────────────────────────────────────────────────
def callback_payload(row: CallbackDelivery, envelope: dict, *, client: ExternalClient) -> dict:
    """The status-read envelope plus the delivery identity. Flat on purpose: a client that already parses
    `GET /work/{request_id}` parses this with no new code."""
    return {**envelope, "delivery_id": row.id, "event": row.event, "client_id": client.id,
            "signature_version": SIGNATURE_VERSION}


# ── enqueue (no network; runs inside the producing transaction) ───────────────
def callback_configured(client: ExternalClient | None) -> bool:
    """A destination without a secret cannot be signed, and AZKT does not push unsigned bodies to the
    internet, so an unsigned configuration is not a configuration."""
    return bool(client is not None and client.callback_url and client.callback_secret_enc)


async def enqueue(db: AsyncSession, *, client: ExternalClient, event: str, dedupe: str,
                  envelope: dict, request_id: str | None = None, mission_id: str | None = None,
                  request_state: str | None = None,
                  max_attempts: int = MAX_ATTEMPTS) -> CallbackDelivery | None:
    """Write at most one delivery row. Returns None when this client is polling-only."""
    if not callback_configured(client):
        return None
    existing = (await db.execute(select(CallbackDelivery)
                                 .where(CallbackDelivery.dedupe_key == dedupe))).scalar_one_or_none()
    if existing is not None:
        return existing
    row = CallbackDelivery(client_id=client.id, delegated_request_id=request_id, mission_id=mission_id,
                           event=event, request_state=request_state, dedupe_key=dedupe,
                           url=client.callback_url or "", payload={}, state="pending", attempts=0,
                           max_attempts=int(max_attempts), next_attempt_at=_now(), attempt_log=[],
                           created_by=client.id, updated_by=client.id)
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        # Two workers reached the same terminal transition at once. The unique dedupe key is what makes
        # "exactly one callback" true; the loser reads the winner's row instead of writing a second.
        try:
            db.expunge(row)
        except Exception:  # noqa: BLE001
            pass
        return (await db.execute(select(CallbackDelivery)
                                 .where(CallbackDelivery.dedupe_key == dedupe))).scalar_one_or_none()
    row.payload = callback_payload(row, envelope, client=client)
    return row


@on_event("mission.updated")
async def _on_mission_updated(db: AsyncSession, ev) -> None:
    """One delivery per terminal transition of a delegated request (spec §10.8).

    The outbox runs this after the producing transaction committed, so the mission's result is final by the
    time it is read. The stored request is brought up to date here too: a client that relies on callbacks may
    never poll, and a `DelegatedRequest` left at `waiting` while its mission succeeded would be a lie in the
    owner's own Settings screen.
    """
    from . import external_clients as ec
    payload = dict(ev.payload or {})
    request_id = payload.get("delegated_request_id") or ev.causation_id
    if not request_id:
        return
    req = await db.get(DelegatedRequest, request_id)
    if req is None:
        return
    mission = await db.get(Mission, payload.get("mission_id") or ev.aggregate_id or "")
    if mission is None or mission.delegated_request_id != req.id:
        return
    state = ec.STATE_FOR_MISSION.get(mission.status)
    if state not in TERMINAL_REQUEST_STATES:
        return
    req.status = state
    req.cursor = int(mission.cursor or 0)
    req.result = {**(req.result or {}), **{k: v for k, v in (mission.result or {}).items()
                                           if k in ("summary", "changed", "approvals", "needed_input")}}
    client = await db.get(ExternalClient, req.client_id)
    if not ec.is_active(client):
        return          # a revoked or expired grant is not a destination
    await enqueue(db, client=client, event=EVENT_FOR_STATE[state],
                  dedupe=f"callback:{req.id}:{state}:{int(mission.cursor or 0)}",
                  envelope=ec.request_envelope(req, mission), request_id=req.id,
                  mission_id=mission.id, request_state=state)


# ── delivery ─────────────────────────────────────────────────────────────────
def transport_error(exc: Exception) -> ProviderError:
    """The only two honest answers to a transport failure.

    A connection that never opened delivered nothing, so trying again cannot duplicate anything — that is a
    retry. Anything after the request was written may already have been processed by the far side, so it is
    `unknown`: neither "delivered" nor safe to send a second time (the same rule the website adapter applies
    to a non-GET, F06).
    """
    import httpx
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError,
                        httpx.UnsupportedProtocol, httpx.InvalidURL)):
        return ProviderError(f"callback endpoint unreachable: {type(exc).__name__}", kind="transient",
                             retryable=True)
    return ProviderError(f"no usable response after the callback was sent: {type(exc).__name__}",
                         kind="unknown")


async def _post(url: str, raw_body: bytes, headers: dict) -> tuple[int, str, dict]:
    """One POST, and only ever one. Redirects are not followed: a 30x would move the delivery to an address
    the owner never configured, which is exactly what this whole module refuses to do."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False) as c:
            r = await c.post(url, content=raw_body, headers=headers)
    except httpx.HTTPError as e:
        raise transport_error(e) from e
    return r.status_code, (r.text or "")[:300], {k.lower(): v for k, v in r.headers.items()}


def _log_attempt(row: CallbackDelivery, *, outcome: str, status: int | None = None, error: str | None = None,
                 duration_ms: int | None = None) -> None:
    """Every attempt is recorded with what actually happened — accepted, retry, failed with its status, or
    unknown. The owner reads this list, so it carries the destination *host* rather than the full address
    (a webhook path can itself be a secret), and never the signing secret or the signature."""
    entry = {"n": int(row.attempts or 0), "at": _iso(_now()), "outcome": outcome, "status": status,
             "error": (error or "")[:300] or None, "duration_ms": duration_ms, "host": _host(row.url)}
    row.attempt_log = (list(row.attempt_log or []) + [entry])[-25:]


def _attempts_left(row: CallbackDelivery) -> bool:
    return int(row.attempts or 0) < int(row.max_attempts or MAX_ATTEMPTS)


def _scrub(text: str, secret: str) -> str:
    """An endpoint that echoes the shared secret back in its response must not make AZKT store it. The
    recorded reason is still useful; the value it repeated is not AZKT's to keep in plaintext."""
    return text.replace(secret, "[redacted]") if (secret and text) else text


def _finish(row: CallbackDelivery, state: str, *, error: str | None = None, status: int | None = None) -> str:
    row.state = state
    row.response_status = status if status is not None else row.response_status
    row.last_error = (error or "")[:2000] or None
    row.lease_token = None
    row.lease_until = None
    row.next_attempt_at = None
    row.finished_at = _now()
    return state


def _cancel(row: CallbackDelivery, reason: str) -> str:
    row.cancel_reason = reason[:300]
    _log_attempt(row, outcome="cancelled", error=reason)
    return _finish(row, "cancelled", error=reason)


def _retry(row: CallbackDelivery, reason: str, *, status: int | None = None,
           retry_after: int | None = None) -> str | None:
    """Bounded backoff. Returns None once the attempts are used up, so the caller can fail it visibly."""
    attempts = int(row.attempts or 0)
    if attempts >= int(row.max_attempts or MAX_ATTEMPTS):
        return None
    delay = BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS)) - 1]
    if retry_after:
        delay = max(delay, min(int(retry_after), MAX_RETRY_AFTER_SECONDS))
    row.state = "pending"
    row.response_status = status
    row.next_attempt_at = _now() + timedelta(seconds=delay)
    row.last_error = f"{reason}; retrying in {delay}s (attempt {attempts} of {row.max_attempts})"[:2000]
    row.lease_token = None
    row.lease_until = None
    return "retrying"


def _retry_after(status: int, headers: dict | None = None) -> int | None:
    """Honour a 429's own Retry-After when the endpoint sends one; otherwise the backoff table decides."""
    if status != 429:
        return None
    raw = str((headers or {}).get("retry-after") or "").strip()
    return int(raw) if raw.isdigit() else 60


async def _setup_failure(db: AsyncSession, client: ExternalClient, row: CallbackDelivery, reason: str) -> str:
    """A destination AZKT cannot legitimately send to. Retrying cannot fix an address or a missing key, so
    this is final — and it is raised to the owner rather than left quietly in a list nobody opens, because
    the client it was meant for is now silently waiting on a push that will never come."""
    _log_attempt(row, outcome="failed", error=reason)
    _finish(row, "failed", error=reason)
    await _raise_connection_issue(db, client, row, reason)
    return "failed"


async def _deliver_one(db: AsyncSession, delivery_id: str) -> str:
    row = (await db.execute(select(CallbackDelivery).where(CallbackDelivery.id == delivery_id)
                            .with_for_update())).scalar_one_or_none()
    if row is None or row.state != "sending":
        return "cancelled"
    from . import external_clients as ec
    client = await db.get(ExternalClient, row.client_id)
    if client is None or not ec.is_active(client):
        return _cancel(row, "the client's access is no longer active")

    # The destination is whatever the OWNER has configured right now — never the snapshot on this row, and
    # never anything a caller supplied. If the owner cleared it between enqueue and send, nothing is sent.
    url = (client.callback_url or "").strip()
    if not url:
        return _cancel(row, "the owner cleared the callback destination")
    if url != row.url:
        row.url = url
    if not url.startswith("https://"):
        # validated when it was set, checked again because a row may predate that validation
        return await _setup_failure(db, client, row,
                                    "setup_blocked: the callback destination must be an https URL")
    if not client.callback_secret_enc:
        return await _setup_failure(db, client, row, "setup_blocked: no signing secret is configured, so "
                                                     "this callback cannot be signed")
    try:
        secret = decrypt(client.callback_secret_enc)
    except ValueError:
        # the reason deliberately says nothing about the secret itself
        return await _setup_failure(db, client, row, "setup_blocked: the stored signing secret could not be "
                                                     "read (encryption key rotated?); set the callback "
                                                     "secret again")
    try:
        # H08: this transport really delivers, so outside production it may only reach an allowlisted host.
        assert_destination_allowed("callback", url)
    except Blocked as e:
        return await _setup_failure(db, client, row, e.message)

    payload = dict(row.payload or {})
    raw = canonical_body(payload)
    timestamp = str(int(time.time()))
    headers = {"Content-Type": "application/json", "User-Agent": "AZKT-Manager-Callback/1.0",
               "Accept": "application/json",
               SIGNATURE_HEADER: signature_header(secret, timestamp, raw),
               TIMESTAMP_HEADER: timestamp, DELIVERY_HEADER: row.id,
               ATTEMPT_HEADER: str(int(row.attempts or 1)), EVENT_HEADER: row.event, CLIENT_HEADER: client.id}

    started = time.perf_counter()
    row.sent_at = row.sent_at or _now()
    try:
        status, text, response_headers = await _post(url, raw, headers)
        text = _scrub(text, secret)
    except Exception as raw_exc:  # noqa: BLE001
        # A transport that raised something the client did not classify is still one of the same two cases;
        # it is never allowed to become an unhandled error that leaves the row stuck in `sending`.
        e = raw_exc if isinstance(raw_exc, ProviderError) else transport_error(raw_exc)
        took = int((time.perf_counter() - started) * 1000)
        kind = (getattr(e, "detail", {}) or {}).get("kind")
        if kind == "unknown":
            # The request left the building. It may have been processed, so it is never "delivered" and
            # never re-sent: a second POST would risk a duplicate the receiver never asked for (F06).
            _log_attempt(row, outcome="unknown", error=e.message, duration_ms=took)
            _finish(row, "unknown", error=e.message)
            await _raise_connection_issue(db, client, row, e.message)
            return "unknown"
        _log_attempt(row, outcome="retry" if _attempts_left(row) else "failed", error=e.message,
                     duration_ms=took)
        again = _retry(row, e.message)
        if again:
            return again
        _finish(row, "failed", error=f"{e.message} (after {row.attempts} attempts)")
        await _raise_connection_issue(db, client, row, e.message)
        return "failed"
    took = int((time.perf_counter() - started) * 1000)

    if 200 <= status < 300:
        _log_attempt(row, outcome="accepted", status=status, duration_ms=took)
        _record_activity(db, client, row, f"Signed callback accepted by {client.name}", state="accepted")
        return _finish(row, "accepted", status=status)

    if 300 <= status < 400:
        # Following it would deliver to an address the owner never configured — the one thing this module
        # exists to prevent. It is a setup problem, and waiting will not change it.
        reason = (f"the callback endpoint redirected ({status}); AZKT does not follow a redirect to an "
                  "address the owner did not configure — point the stored URL at the final destination")
        _log_attempt(row, outcome="failed", status=status, error=reason, duration_ms=took)
        _finish(row, "failed", error=reason, status=status)
        await _raise_connection_issue(db, client, row, reason)
        return "failed"

    reason = f"the callback endpoint answered {status}: {text}" if text else f"the callback endpoint answered {status}"
    if status in PERMANENT_STATUS:
        _log_attempt(row, outcome="failed", status=status, error=reason, duration_ms=took)
        _finish(row, "failed", error=reason, status=status)
        await _raise_connection_issue(db, client, row, reason)
        return "failed"
    _log_attempt(row, outcome="retry" if _attempts_left(row) else "failed", status=status, error=reason,
                 duration_ms=took)
    again = _retry(row, reason, status=status, retry_after=_retry_after(status, response_headers))
    if again:
        return again
    _finish(row, "failed", error=f"{reason} (after {row.attempts} attempts)", status=status)
    await _raise_connection_issue(db, client, row, reason)
    return "failed"


def _record_activity(db: AsyncSession, client: ExternalClient, row: CallbackDelivery, what: str,
                     *, state: str, exception: bool = False) -> None:
    """One activity row per terminal outcome, not per attempt: the per-attempt detail lives in
    `attempt_log` and the owner reads it on the client's callback list. Never carries the secret."""
    ctx = CommandContext(db=db, actor=SYSTEM_ACTOR, channel="worker")
    ctx.record(what, entity_kind="external_client", entity_id=client.id, kind="connection", state=state,
               visibility="owner", exception=exception,
               receipt={"delivery_id": row.id, "event": row.event, "status": row.response_status,
                        "attempts": int(row.attempts or 0)},
               details={"delivery_id": row.id, "request_id": row.delegated_request_id, "event": row.event,
                        "attempts": int(row.attempts or 0), "destination_host": _host(row.url),
                        "error": (row.last_error or "")[:300] or None})


def _host(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url or "").netloc or ""


async def _raise_connection_issue(db: AsyncSession, client: ExternalClient, row: CallbackDelivery,
                                  reason: str) -> None:
    """A destination AZKT cannot reach is a provider problem the owner must see, reported the same way a
    degraded connection is (spec §12.4): one grouped, reopening notification per client plus an activity
    exception — never a silent retry that stops one day."""
    outcome = "could not be delivered" if row.state == "failed" else "may not have arrived"
    _record_activity(db, client, row,
                     f"Signed callback to {client.name} {outcome} ({row.state})",
                     state=row.state, exception=True)
    try:
        from . import reminders
        owners = await reminders.owner_users(db)
    except Exception:  # noqa: BLE001  - the notification is a courtesy; the activity row is the record
        log.exception("callback connection issue could not reach the notification feed")
        return
    title = f"Callback to {client.name} needs attention"
    body = (f"AZKT {outcome} to the callback address you configured for {client.name} "
            f"after {int(row.attempts or 0)} attempt(s). {reason} "
            "That agent has not been told this work finished — it can still read it by polling "
            "GET /api/integrations/v1/work/{request_id}.")
    for u in owners:
        try:
            await reminders.notify(
                db, user_id=u.id, kind="connection_issue", urgency="today", title=title, body=body,
                entity_kind="external_client", entity_id=client.id,
                deep_link=f"{settings.PUBLIC_ORIGIN}/settings/connections",
                group_key=f"external_callback:{client.id}",
                dedupe=f"external_callback:{client.id}:{u.id}",
                payload={"client_id": client.id, "client_name": client.name, "delivery_id": row.id,
                         "state": row.state, "attempts": int(row.attempts or 0),
                         "destination_host": _host(row.url), "reason": reason[:300]})
        except Exception:  # noqa: BLE001
            log.exception("callback connection issue notification failed for %s", u.id)


async def _claim(db: AsyncSession, limit: int) -> list[str]:
    rows = (await db.execute(
        select(CallbackDelivery).where(
            CallbackDelivery.state == "pending",
            or_(CallbackDelivery.next_attempt_at.is_(None), CallbackDelivery.next_attempt_at <= _now()))
        .order_by(CallbackDelivery.next_attempt_at, CallbackDelivery.created_at)
        .limit(limit).with_for_update(skip_locked=True))).scalars().all()
    token = new_id()
    claimed = []
    for r in rows:
        r.state = "sending"
        r.lease_token = token
        r.lease_until = _now() + timedelta(seconds=LEASE_SECONDS)
        r.attempts = int(r.attempts or 0) + 1
        claimed.append(r.id)
    await db.commit()
    return claimed


@sweep("external_callbacks.deliver_due", 15)
async def deliver_due(session_factory) -> dict:
    """Claim due deliveries (FOR UPDATE SKIP LOCKED), send each one in its own session, record the truth."""
    async with session_factory() as db:
        # a worker that died mid-send leaves a row in `sending`; an expired lease returns it to the queue.
        # It counts as an attempt already spent, so a crash loop still ends inside the bound.
        stuck = (await db.execute(select(CallbackDelivery).where(
            CallbackDelivery.state == "sending", CallbackDelivery.lease_until < _now()))).scalars().all()
        for r in stuck:
            r.state = "pending"
            r.lease_token = None
            r.lease_until = None
            r.next_attempt_at = _now()
            r.last_error = "delivery lease expired; requeued"
        if stuck:
            await db.commit()
        claimed = await _claim(db, CLAIM_BATCH)
    out = {"claimed": len(claimed), "requeued": len(stuck), "accepted": 0, "failed": 0, "unknown": 0,
           "cancelled": 0, "retrying": 0}
    for rid in claimed:
        async with session_factory() as db:
            try:
                res = await _deliver_one(db, rid)
                await db.commit()
            except Exception as e:  # noqa: BLE001
                await db.rollback()
                log.exception("callback delivery %s failed", rid)
                res = "failed"
                async with session_factory() as db2:
                    row = await db2.get(CallbackDelivery, rid)
                    if row is not None and row.state == "sending":
                        _log_attempt(row, outcome="failed", error=f"{type(e).__name__}: {e}")
                        res = _retry(row, f"{type(e).__name__}: {e}") or _finish(
                            row, "failed", error=f"{type(e).__name__}: {e}")
                        await db2.commit()
            out[res] = out.get(res, 0) + 1
    return out


async def deliver_now(session, delivery_id: str) -> CallbackDelivery | None:
    """Send one delivery in this session, right now. Used by the owner's "send a test callback" button, so
    the answer is the real outcome of a real signed POST rather than "queued, look again later"."""
    row = await session.get(CallbackDelivery, delivery_id)
    if row is None:
        return None
    if row.state == "pending":
        row.state = "sending"
        row.lease_token = new_id()
        row.lease_until = _now() + timedelta(seconds=LEASE_SECONDS)
        row.attempts = int(row.attempts or 0) + 1
        await session.commit()
    await _deliver_one(session, delivery_id)
    await session.commit()
    return await session.get(CallbackDelivery, delivery_id)


# ── owner-facing reads ───────────────────────────────────────────────────────
def serialize_delivery(row: CallbackDelivery) -> dict:
    """Owner-only view of one delivery. The signing secret and the signature are deliberately absent: the
    owner already holds the secret, and nothing that can reproduce a signature belongs in an API response."""
    return {"id": row.id, "client_id": row.client_id, "request_id": row.delegated_request_id,
            "mission_id": row.mission_id, "event": row.event, "request_state": row.request_state,
            "state": row.state, "attempts": int(row.attempts or 0), "max_attempts": int(row.max_attempts or 0),
            "response_status": row.response_status, "error": row.last_error,
            "cancel_reason": row.cancel_reason, "destination_host": _host(row.url), "signed": True,
            "signature_version": SIGNATURE_VERSION,
            "next_attempt_at": _iso(row.next_attempt_at), "sent_at": _iso(row.sent_at),
            "finished_at": _iso(row.finished_at), "created_at": _iso(row.created_at),
            "attempt_log": list(row.attempt_log or []),
            "summary": (row.payload or {}).get("summary") or ""}


async def recent(db: AsyncSession, client_id: str, *, limit: int = 25) -> dict:
    rows = (await db.execute(select(CallbackDelivery).where(CallbackDelivery.client_id == client_id)
                             .order_by(CallbackDelivery.created_at.desc()).limit(limit))).scalars().all()
    return {"items": [serialize_delivery(r) for r in rows], "count": len(rows),
            "states": {s: sum(1 for r in rows if r.state == s)
                       for s in ("pending", "sending", "accepted", "failed", "unknown", "cancelled")},
            "note": ("Accepted means the endpoint answered 2xx. Unknown means the connection broke after the "
                     "callback was sent — it may have arrived, so AZKT neither claims delivery nor re-sends "
                     "it. Polling is always available: GET /api/integrations/v1/work/{request_id}.")}


async def health_for_client(db: AsyncSession, client_id: str) -> dict:
    """Compact per-client callback state for Settings → External agents."""
    rows = (await db.execute(select(CallbackDelivery).where(CallbackDelivery.client_id == client_id)
                             .order_by(CallbackDelivery.created_at.desc()).limit(50))).scalars().all()
    last = rows[0] if rows else None
    return {"total": len(rows),
            "pending": sum(1 for r in rows if r.state in ("pending", "sending")),
            "failed": sum(1 for r in rows if r.state in ("failed", "unknown")),
            "last_state": last.state if last else None,
            "last_event": last.event if last else None,
            "last_at": _iso(last.finished_at or last.created_at) if last else None,
            "last_error": (last.last_error if last and last.state in ("failed", "unknown") else None)}


# ── the owner's test send ────────────────────────────────────────────────────
class TestCallbackIn(BaseModel):
    client_id: str


def sample_envelope(client: ExternalClient) -> dict:
    """A body with the shape of a real one and no business data in it: a test must prove the endpoint,
    the signature and the destination guard work without handing a record to an unverified address."""
    return {"request_id": f"test-{new_id()[:8]}", "request_key": "azkt-callback-test", "mission_id": None,
            "state": "done", "summary": f"Test callback from AZKT for “{client.name}”. No business data is "
                                        "included, and no work was performed.",
            "changed": [], "citations": [], "receipts": [], "needed_input": None, "review_links": [],
            "cursor": 0, "correlation_id": None, "error": None, "created_at": _iso(_now()),
            "updates": [], "test": True}


@command("external_clients.test_callback", input=TestCallbackIn, perm="connections", action_class="owner_only",
         summary=lambda p: "Send a test callback to an external agent client",
         description="POST one signed test body to the owner-configured callback destination so the receiving "
                     "agent's verification can be proved before real work depends on it. The body carries no "
                     "business data, and the destination is the stored one — never a URL supplied in a request.")
async def test_callback(ctx: CommandContext, inp: TestCallbackIn) -> dict:
    c = await ctx.db.get(ExternalClient, inp.client_id)
    if c is None:
        raise NotFound("external client not found")
    if not (c.callback_url or "").strip():
        raise ValidationFailed("this client has no callback destination; set one first, or leave it "
                               "polling-only", client_id=c.id)
    if not c.callback_secret_enc:
        raise ValidationFailed("this client has no signing secret, so a callback cannot be signed; set the "
                               "secret before testing", client_id=c.id)
    # One attempt only: a probe the owner is watching must answer now. A real callback earns its retries
    # because nobody is standing over it; a test that said "queued, try later" would prove nothing.
    row = await enqueue(ctx.db, client=c, event=TEST_EVENT, dedupe=f"callback:test:{c.id}:{new_id()}",
                        envelope=sample_envelope(c), request_state="test", max_attempts=1)
    ctx.record(f"Test callback queued for {c.name}", entity_kind="external_client", entity_id=c.id,
               kind="connection", state="pending", visibility="owner",
               details={"delivery_id": row.id, "destination_host": _host(c.callback_url or "")})
    return {"delivery": serialize_delivery(row), "client_id": c.id}


async def run_test_callback(db: AsyncSession, delivery_id: str) -> dict:
    """Deliver the queued test and report the real outcome, including a guard refusal, truthfully."""
    row = await deliver_now(db, delivery_id)
    if row is None:
        raise NotFound("callback delivery not found")
    delivered = row.state == "accepted"
    return {"delivery": serialize_delivery(row), "accepted": delivered,
            "message": {"accepted": "Your endpoint verified and accepted the signed test callback.",
                        "failed": f"The test callback was not accepted: {row.last_error or 'no reason given'}",
                        "unknown": "The connection broke after the test callback was sent. It may have "
                                   "arrived; AZKT will not claim it did and did not send it twice.",
                        "cancelled": f"Nothing was sent: {row.cancel_reason or 'the destination is gone'}",
                        }.get(row.state, f"The test callback is {row.state}.")}

