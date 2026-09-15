"""Square webhook endpoint (spec §6.3, §12.3).

Contract: read the **raw** body, verify the HMAC-SHA256 signature against the configured notification
URL, store the event durably namespaced by merchant, acknowledge promptly, and process asynchronously
through the `square.process_event` job. A bad signature is rejected with 401 and nothing is stored; a
duplicate `event_id` is acknowledged with no second effect (E06).
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters import square as sq
from ..auth.deps import require
from ..db import get_db
from ..domain import events as events_mod
from ..services import square_sync

log = logging.getLogger("azkt.webhooks.square")
router = APIRouter(tags=["webhooks"])

MAX_BODY_BYTES = 512 * 1024
SIGNATURE_HEADER = "x-square-hmacsha256-signature"


@router.post("/api/webhooks/square")
async def square_webhook(request: Request,
                         x_square_hmacsha256_signature: str | None = Header(default=None),
                         db: AsyncSession = Depends(get_db)) -> Response:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return Response(status_code=413)
    conn = await square_sync.connection(db)
    key = sq.webhook_signature_key(conn)
    url = sq.notification_url(conn)
    if not key or not url:
        # Not configured: refuse rather than storing unverifiable content as trusted (setup blocked).
        log.warning("square webhook received while the signature key / notification URL is not configured")
        return Response(status_code=401, content=json.dumps(
            {"error": "unverified", "message": "Square webhook signature key or notification URL is not configured"}),
            media_type="application/json")
    if not sq.verify_signature(url, raw, x_square_hmacsha256_signature, key):
        log.warning("rejected a Square webhook with an invalid signature")
        return Response(status_code=401, content=json.dumps({"error": "bad_signature"}), media_type="application/json")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return Response(status_code=400, content=json.dumps({"error": "invalid_json"}), media_type="application/json")
    if not isinstance(body, dict):
        return Response(status_code=400, content=json.dumps({"error": "invalid_json"}), media_type="application/json")
    event_id = str(body.get("event_id") or body.get("id") or "")
    merchant_id = str(body.get("merchant_id") or "")
    if not event_id:
        return Response(status_code=400, content=json.dumps({"error": "missing_event_id"}), media_type="application/json")
    row, is_new = await events_mod.record_provider_event(
        db, provider="square", connection_key=merchant_id, provider_event_id=event_id,
        payload=body, event_type=str(body.get("type") or ""), signature_ok=True)
    if is_new:
        await square_sync.enqueue_event(db, row)
    await db.commit()
    return Response(status_code=200, media_type="application/json",
                    content=json.dumps({"ok": True, "duplicate": not is_new, "event_id": event_id}))


@router.get("/api/webhooks/square/status")
async def square_webhook_status(db: AsyncSession = Depends(get_db), actor=Depends(require("connections"))) -> dict:
    """Setup surface: is the webhook verifiable, and what arrived recently. No side effects."""
    conn = await square_sync.connection(db)
    return {"configured": bool(sq.webhook_signature_key(conn)) and bool(sq.notification_url(conn)),
            "notification_url": sq.notification_url(conn) or None,
            "merchant_id": sq.merchant_id_of(conn) or None,
            "signature_key_present": bool(sq.webhook_signature_key(conn)),
            "recent_events": await square_sync.recent_events(db)}
