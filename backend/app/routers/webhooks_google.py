"""Google Cloud Pub/Sub push endpoint for Gmail (spec §4.2, §12.3).

Contract: authenticate the delivery, bound the input size, store the accepted event durably,
acknowledge promptly (204) and process asynchronously through the `gmail.sync` job. A notification
is identified by ``historyId + emailAddress``, so a duplicate or replayed push is acknowledged
without repeating a business effect (spec §12.1 event envelope, invariant 1).
"""
from __future__ import annotations

import base64
import hmac
import json
import logging

from fastapi import APIRouter, Header, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from ..core.config import settings
from ..db import get_db
from ..models.comms import Connection
from ..services import inbox as inbox_svc

log = logging.getLogger("azkt.webhooks.google")
router = APIRouter(tags=["webhooks"])

MAX_BODY_BYTES = 64 * 1024
GMAIL_PROVIDERS = inbox_svc.GMAIL_PROVIDERS


def _token_ok(token: str | None, authorization: str | None) -> bool:
    """Constant-time comparison: the verification token is the only thing standing between an
    anonymous POST and a stored, trusted provider event, so it is never compared byte-by-byte."""
    expected = settings.GOOGLE_PUBSUB_VERIFICATION_TOKEN
    if not expected:
        return False
    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization.split(" ", 1)[1].strip()
    return hmac.compare_digest(token or "", expected) or hmac.compare_digest(bearer, expected)


@router.post("/api/webhooks/gmail", status_code=204)
async def gmail_push(request: Request, response: Response, token: str | None = Query(default=None),
                     authorization: str | None = Header(default=None),
                     db: AsyncSession = Depends(get_db)) -> Response:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return Response(status_code=413)
    if not _token_ok(token, authorization):
        # unverified delivery: nothing is stored as trusted and nothing is processed
        log.warning("rejected an unverified Gmail push delivery")
        return Response(status_code=401)
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return Response(status_code=400)
    message = (body or {}).get("message") or {}
    data = message.get("data")
    try:
        decoded = json.loads(base64.b64decode(data + "=" * (-len(data) % 4)).decode()) if data else {}
    except (ValueError, TypeError):
        decoded = {}
    email_address = str(decoded.get("emailAddress") or "")
    history_id = str(decoded.get("historyId") or "")
    if not email_address or not history_id:
        return Response(status_code=400)
    # exact, case-insensitive equality: `ilike` would read the delivered address as a LIKE pattern, so a
    # mailbox with `_` or `%` in it could be steered onto another account's connection.
    conn = (await db.execute(select(Connection).where(
        Connection.provider.in_(GMAIL_PROVIDERS),
        func.lower(Connection.account_identity) == email_address.lower()).order_by(
        Connection.created_at))).scalars().first()
    if conn is None:
        # an unknown mailbox is acknowledged (Pub/Sub would retry forever otherwise) but never processed
        log.warning("Gmail push for an unconnected mailbox")
        return Response(status_code=204)
    row, is_new = await inbox_svc.record_push(db, conn, email_address=email_address, history_id=history_id,
                                              raw={"message_id": message.get("messageId"),
                                                   "publish_time": message.get("publishTime"),
                                                   "subscription": body.get("subscription"),
                                                   "history_id": history_id, "email_address": email_address})
    if is_new:
        await inbox_svc.enqueue_sync(db, conn, "push")
    await db.commit()
    return Response(status_code=204)
