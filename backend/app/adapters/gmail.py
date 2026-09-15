"""Gmail adapter (spec §4.1–4.2, §12.3).

One interface, two implementations:

  * ``GmailAdapter``  — live Gmail REST calls over :class:`adapters.google_oauth.GoogleApi`.
  * ``FakeGmail``     — the same interface over ``backend/tests/fixtures_gmail.py`` fixtures.
    Every test uses the fake; no live provider call is ever made from a test.

Contract notes that the rest of the slice depends on:

* Typed errors only. Everything raises :class:`core.errors.ProviderError` with a ``kind`` of
  ``invalid_input | permission_denied | auth_expired | rate_limited | transient | schema_changed |
  conflict | unknown_result``. An invalid ``historyId`` (Gmail 404 on ``users.history.list``) is
  ``schema_changed`` with code ``history_invalid`` so the caller can run a bounded resync (B02).
* Capabilities that the connection has not been granted return :class:`core.errors.Unsupported`,
  never an empty success (spec §12.3). Label/archive operations sit behind an explicit capability
  flag (``connection.capabilities['labels']``) because mailbox mutation is a separate permission
  (spec §11.2).
* Gmail has no idempotency key for ``users.messages.send``. The caller therefore mints the RFC822
  ``Message-ID`` itself and stores it **before** sending; :meth:`find_sent_by_message_id` is the
  reconciliation query used after an unknown result (spec §11.5.4, H02/B11).
"""
from __future__ import annotations

import base64
import re
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, parsedate_to_datetime
from typing import Any

from ..core.errors import ProviderError, Unsupported
from ..models.comms import Connection
from .google_oauth import GoogleApi

API = "https://gmail.googleapis.com/gmail/v1/users/me"

ERROR_KINDS = ("invalid_input", "permission_denied", "auth_expired", "rate_limited", "transient",
               "schema_changed", "conflict", "unknown_result")

HEADER_KEYS = ("message-id", "in-reply-to", "references", "list-unsubscribe", "auto-submitted",
               "precedence", "return-path", "x-autoreply", "x-autorespond", "x-failed-recipients",
               "content-type", "reply-to", "delivered-to", "x-original-to", "authentication-results")


def error_kind(exc: Exception) -> str:
    """The typed kind of a ProviderError (``unknown`` for anything else)."""
    detail = getattr(exc, "detail", None) or {}
    return detail.get("kind") or "unknown"


def make_message_id(domain: str = "azkeitrucks.com") -> str:
    """Our own RFC822 Message-ID, minted before a send so an unknown result can be reconciled."""
    return f"<{uuid.uuid4().hex}.{int(datetime.now(timezone.utc).timestamp())}@{domain}>"


# ── normalization ────────────────────────────────────────────────────────────
def _b64url(data: str | None) -> bytes:
    if not data:
        return b""
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _addresses(raw: str | None) -> list[str]:
    if not raw:
        return []
    out = []
    for _, addr in getaddresses([raw]):
        a = (addr or "").strip()
        if a:
            out.append(a)
    return out


def _walk_parts(part: dict, text: list[str], html: list[str], attachments: list[dict]) -> None:
    mime = (part.get("mimeType") or "").lower()
    body = part.get("body") or {}
    filename = part.get("filename") or ""
    if part.get("parts"):
        for p in part["parts"]:
            _walk_parts(p, text, html, attachments)
        return
    if filename or body.get("attachmentId"):
        attachments.append({"filename": filename or "attachment", "mime": mime or "application/octet-stream",
                            "size": int(body.get("size") or 0), "attachment_id": body.get("attachmentId")})
        return
    data = _b64url(body.get("data"))
    if not data:
        return
    decoded = data.decode("utf-8", "replace")
    if mime == "text/html":
        html.append(decoded)
    else:
        text.append(decoded)


_TAG_RE = re.compile(r"<[^>]+>")
_QUOTE_RE = re.compile(r"^(?:On .{0,120}wrote:|-{2,}\s*Original Message\s*-{2,}|_{5,}|From: .{0,200})$",
                       re.IGNORECASE | re.MULTILINE)


def html_to_text(html: str) -> str:
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</p\s*>", "\n\n", s)
    s = _TAG_RE.sub(" ", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return re.sub(r"[ \t]+", " ", s).strip()


def strip_quoted(text: str) -> str:
    """The new text of a reply: quoted history and simple signatures removed (best effort, lossless original kept)."""
    body = (text or "").replace("\r\n", "\n")
    m = _QUOTE_RE.search(body)
    if m:
        body = body[: m.start()]
    lines: list[str] = []
    for line in body.split("\n"):
        if line.strip().startswith(">"):
            continue
        if re.fullmatch(r"\s*(--|__|—)\s*", line):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def normalize_message(raw: dict, *, format: str = "full") -> dict:
    """Gmail message resource -> the normalized shape the inbox stores."""
    payload = raw.get("payload") or {}
    headers: dict[str, str] = {}
    for h in payload.get("headers") or []:
        name = (h.get("name") or "").lower()
        if name not in HEADER_KEYS and name not in ("from", "to", "cc", "bcc", "subject", "date"):
            continue
        if name in headers:
            # A repeated header is either threading history (References, joined) or a spoofing attempt
            # (a second From: or Message-ID:). The FIRST occurrence is the one every downstream check,
            # and find_sent_by_message_id, must agree on — a later copy never replaces it.
            if name == "references":
                headers[name] = f"{headers[name]}, {h.get('value', '')}"
            continue
        headers[name] = h.get("value", "")
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict] = []
    if payload:
        _walk_parts(payload, text_parts, html_parts, attachments)
    text = "\n".join(t for t in text_parts if t).strip()
    html = "\n".join(h for h in html_parts if h).strip()
    if not text and html:
        text = html_to_text(html)
    sent_at = None
    if headers.get("date"):
        try:
            sent_at = parsedate_to_datetime(headers["date"])
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            sent_at = None
    if sent_at is None and raw.get("internalDate"):
        try:
            sent_at = datetime.fromtimestamp(int(raw["internalDate"]) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            sent_at = None
    return {
        "provider_message_id": raw.get("id"),
        "thread_id": raw.get("threadId"),
        "history_id": str(raw.get("historyId")) if raw.get("historyId") is not None else None,
        "label_ids": list(raw.get("labelIds") or []),
        "headers": {k: v for k, v in headers.items() if k in HEADER_KEYS},
        "from": (_addresses(headers.get("from")) or [None])[0],
        "from_raw": headers.get("from", ""),
        "to": _addresses(headers.get("to")),
        "cc": _addresses(headers.get("cc")),
        "date": sent_at,
        "subject": headers.get("subject", ""),
        "snippet": raw.get("snippet", "") or "",
        "text": text,
        "html": html,
        "new_text": strip_quoted(text),
        "attachments": attachments,
        "format": format,
        "size_estimate": int(raw.get("sizeEstimate") or 0),
    }


def build_mime(*, to: list[str], subject: str, body: str, from_addr: str | None = None, cc: list[str] | None = None,
               message_id: str | None = None, in_reply_to: str | None = None, references: str | None = None,
               attachments: list[dict] | None = None) -> str:
    """base64url RFC822 for users.messages.send / drafts. `message_id` is OUR id, minted before sending."""
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if from_addr:
        msg["From"] = from_addr
    msg["Subject"] = subject
    if message_id:
        msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg["Date"] = formatdate(localtime=False)
    msg.set_content(body or "")
    for att in attachments or []:
        data = att.get("data")
        if not data:
            continue
        maintype, _, subtype = (att.get("mime") or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream",
                           filename=att.get("filename") or "attachment")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


# ── live adapter ─────────────────────────────────────────────────────────────
class GmailAdapter:
    """Live Gmail. Every method maps provider failures onto typed ProviderError kinds."""

    provider = "gmail"
    is_fake = False

    def __init__(self, db, connection: Connection):
        self.db = db
        self.conn = connection
        self.api = GoogleApi(db, connection)

    # capability discovery (spec §12.3)
    def capabilities(self) -> dict:
        granted = " ".join(self.conn.granted_scopes or [])
        flags = dict(self.conn.capabilities or {})
        return {
            "read": "gmail.readonly" in granted or "gmail.modify" in granted or not granted,
            "send": bool(flags.get("send")) and ("gmail.send" in granted or "gmail.compose" in granted or not granted),
            "drafts": bool(flags.get("drafts")) and ("gmail.compose" in granted or "gmail.modify" in granted or not granted),
            "labels": bool(flags.get("labels")) and ("gmail.modify" in granted or not granted),
            "watch": True,
        }

    def _require(self, capability: str) -> None:
        if not self.capabilities().get(capability):
            raise Unsupported(f"gmail capability '{capability}' is not enabled for {self.conn.provider}",
                              capability=capability, provider=self.conn.provider)

    async def _get(self, path: str, params: dict | None = None) -> dict:
        return await self.api.request("GET", f"{API}{path}", params={k: v for k, v in (params or {}).items() if v is not None})

    async def profile(self) -> dict:
        r = await self._get("/profile")
        return {"email_address": r.get("emailAddress"), "history_id": str(r.get("historyId")) if r.get("historyId") else None,
                "messages_total": r.get("messagesTotal")}

    async def history(self, start_history_id: str, *, page_token: str | None = None, max_results: int = 100,
                      label_id: str | None = None) -> dict:
        """One page of users.history.list. A 404 means the cursor is too old -> bounded resync (B02)."""
        try:
            r = await self._get("/history", {"startHistoryId": str(start_history_id), "pageToken": page_token,
                                             "maxResults": max_results, "labelId": label_id})
        except ProviderError as e:
            if "404" in str(e) or error_kind(e) == "invalid_input":
                raise ProviderError("gmail history cursor is no longer valid; bounded resync required",
                                    kind="schema_changed", code="history_invalid",
                                    start_history_id=str(start_history_id)) from e
            raise
        return {"history": list(r.get("history") or []), "next_page_token": r.get("nextPageToken"),
                "history_id": str(r.get("historyId")) if r.get("historyId") else None}

    async def list_messages(self, query: str | None = None, page_token: str | None = None,
                            max_results: int = 50, label_ids: list[str] | None = None) -> dict:
        r = await self._get("/messages", {"q": query, "pageToken": page_token, "maxResults": max_results,
                                          "labelIds": label_ids})
        return {"messages": [{"id": m.get("id"), "thread_id": m.get("threadId")} for m in (r.get("messages") or [])],
                "next_page_token": r.get("nextPageToken"),
                "result_size_estimate": r.get("resultSizeEstimate")}

    async def get_message(self, message_id: str, format: str = "full") -> dict:
        if format not in ("full", "metadata", "minimal", "raw"):
            raise ProviderError(f"unsupported message format {format!r}", kind="invalid_input")
        params: dict[str, Any] = {"format": format}
        if format == "metadata":
            params["metadataHeaders"] = list(HEADER_KEYS) + ["From", "To", "Cc", "Subject", "Date"]
        r = await self._get(f"/messages/{message_id}", params)
        return normalize_message(r, format=format)

    async def get_thread(self, thread_id: str, format: str = "metadata") -> dict:
        r = await self._get(f"/threads/{thread_id}", {"format": format})
        return {"thread_id": r.get("id"), "messages": [normalize_message(m, format=format) for m in (r.get("messages") or [])]}

    async def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        r = await self._get(f"/messages/{message_id}/attachments/{attachment_id}")
        return _b64url(r.get("data"))

    # ── watch (Pub/Sub push) ────────────────────────────────────────────────
    async def watch(self, topic: str, label_ids: list[str] | None = None) -> dict:
        if not topic:
            raise Unsupported("no Cloud Pub/Sub topic configured (GOOGLE_PUBSUB_TOPIC)", capability="watch")
        r = await self.api.request("POST", f"{API}/watch", json={"topicName": topic, "labelIds": label_ids or None,
                                                                 "labelFilterBehavior": "INCLUDE" if label_ids else None})
        exp = r.get("expiration")
        expires_at = datetime.fromtimestamp(int(exp) / 1000, tz=timezone.utc) if exp else None
        self.conn.watch_expires_at = expires_at
        return {"history_id": str(r.get("historyId")) if r.get("historyId") else None, "expires_at": expires_at}

    async def stop(self) -> dict:
        await self.api.request("POST", f"{API}/stop")
        self.conn.watch_expires_at = None
        return {"stopped": True}

    # ── drafts (mirrored Gmail drafts, spec §4.4) ───────────────────────────
    async def create_draft(self, *, raw: str, thread_id: str | None = None) -> dict:
        self._require("drafts")
        body: dict = {"message": {"raw": raw}}
        if thread_id:
            body["message"]["threadId"] = thread_id
        r = await self.api.request("POST", f"{API}/drafts", json=body)
        return {"draft_id": r.get("id"), "message_id": (r.get("message") or {}).get("id"),
                "thread_id": (r.get("message") or {}).get("threadId")}

    async def update_draft(self, draft_id: str, *, raw: str, thread_id: str | None = None) -> dict:
        self._require("drafts")
        body: dict = {"message": {"raw": raw}}
        if thread_id:
            body["message"]["threadId"] = thread_id
        r = await self.api.request("PUT", f"{API}/drafts/{draft_id}", json=body)
        return {"draft_id": r.get("id"), "message_id": (r.get("message") or {}).get("id"),
                "thread_id": (r.get("message") or {}).get("threadId")}

    async def get_draft(self, draft_id: str) -> dict | None:
        self._require("drafts")
        try:
            r = await self._get(f"/drafts/{draft_id}", {"format": "metadata"})
        except ProviderError as e:
            if "404" in str(e):
                return None
            raise
        return {"draft_id": r.get("id"), "message": normalize_message(r.get("message") or {}, format="metadata")}

    async def delete_draft(self, draft_id: str) -> dict:
        self._require("drafts")
        await self.api.request("DELETE", f"{API}/drafts/{draft_id}")
        return {"deleted": True, "draft_id": draft_id}

    # ── send ────────────────────────────────────────────────────────────────
    async def send(self, raw: str, thread_id: str | None = None) -> dict:
        """Send RFC822. Gmail offers no idempotency key: the caller must have stored OUR Message-ID first
        and reconcile with find_sent_by_message_id() when the result is unknown (spec §11.5)."""
        self._require("send")
        body: dict = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        r = await self.api.request("POST", f"{API}/messages/send", json=body)
        return {"message_id": r.get("id"), "thread_id": r.get("threadId"), "label_ids": list(r.get("labelIds") or [])}

    async def find_sent_by_message_id(self, rfc_message_id: str) -> dict | None:
        """Reconciliation query: search Sent for OUR Message-ID header before any retry."""
        rid = (rfc_message_id or "").strip()
        if not rid:
            return None
        res = await self.list_messages(query=f'rfc822msgid:{rid.strip("<>")}', max_results=5, label_ids=["SENT"])
        for m in res.get("messages") or []:
            msg = await self.get_message(m["id"], format="metadata")
            if (msg.get("headers") or {}).get("message-id", "").strip() == rid:
                return {"message_id": msg["provider_message_id"], "thread_id": msg["thread_id"],
                        "rfc_message_id": rid, "date": msg.get("date")}
        return None

    # ── label operations (capability-gated, spec §4.1) ──────────────────────
    async def modify_labels(self, message_id: str, *, add: list[str] | None = None, remove: list[str] | None = None) -> dict:
        self._require("labels")
        r = await self.api.request("POST", f"{API}/messages/{message_id}/modify",
                                   json={"addLabelIds": add or [], "removeLabelIds": remove or []})
        return {"message_id": r.get("id"), "label_ids": list(r.get("labelIds") or [])}

    async def archive(self, message_id: str) -> dict:
        return await self.modify_labels(message_id, remove=["INBOX"])


# ── fake adapter used by every test ──────────────────────────────────────────
class FakeGmail:
    """In-memory Gmail driven by fixtures. Same interface, same typed errors, no network.

    ``fixtures`` shape::

        {"profile": {...}, "messages": {id: raw_gmail_message}, "history": [{...}],
         "invalid_history_before": "1000", "drafts": {...}}
    """

    provider = "gmail"
    is_fake = True

    def __init__(self, fixtures: dict | None = None, *, connection: Connection | None = None,
                 capabilities: dict | None = None, db=None):
        f = dict(fixtures or {})
        self.db = db
        self.conn = connection
        self.messages: dict[str, dict] = dict(f.get("messages") or {})
        self.history_records: list[dict] = list(f.get("history") or [])
        self.profile_data: dict = dict(f.get("profile") or {"emailAddress": "info@azkeitrucks.com", "historyId": "1"})
        self.invalid_history_before: str | None = f.get("invalid_history_before")
        self.drafts: dict[str, dict] = dict(f.get("drafts") or {})
        self.page_size: int = int(f.get("page_size") or 2)
        self._caps = capabilities or {"read": True, "send": True, "drafts": True, "labels": False, "watch": True}
        # observability for tests
        self.sent: list[dict] = []
        self.calls: list[tuple[str, Any]] = []
        self.fetched_full: list[str] = []
        self.fetched_metadata: list[str] = []
        self.fail_next_send: Exception | None = None
        self.send_then_lose_result: bool = False
        self.watch_calls: int = 0
        self.stop_calls: int = 0
        self._seq = 0

    # -- test helpers ---------------------------------------------------------
    def add_message(self, raw: dict, *, history_id: str | None = None, history_type: str = "messagesAdded") -> dict:
        self.messages[raw["id"]] = raw
        hid = history_id or str(int(self.profile_data.get("historyId", "1")) + 1)
        self.profile_data["historyId"] = hid
        self.history_records.append({"id": hid, history_type: [{"message": {"id": raw["id"], "threadId": raw.get("threadId")}}]})
        return raw

    def capabilities(self) -> dict:
        caps = dict(self._caps)
        if self.conn is not None:
            for k, v in (self.conn.capabilities or {}).items():
                caps[k] = bool(v)
        return caps

    def _require(self, capability: str) -> None:
        if not self.capabilities().get(capability):
            raise Unsupported(f"gmail capability '{capability}' is not enabled", capability=capability)

    # -- interface ------------------------------------------------------------
    async def profile(self) -> dict:
        self.calls.append(("profile", None))
        return {"email_address": self.profile_data.get("emailAddress"),
                "history_id": str(self.profile_data.get("historyId")),
                "messages_total": len(self.messages)}

    async def history(self, start_history_id: str, *, page_token: str | None = None, max_results: int = 100,
                      label_id: str | None = None) -> dict:
        self.calls.append(("history", (start_history_id, page_token)))
        if self.invalid_history_before is not None and int(start_history_id) < int(self.invalid_history_before):
            raise ProviderError("gmail history cursor is no longer valid; bounded resync required",
                                kind="schema_changed", code="history_invalid", start_history_id=str(start_history_id))
        records = [h for h in self.history_records if int(h["id"]) > int(start_history_id)]
        offset = int(page_token or 0)
        page = records[offset: offset + self.page_size]
        nxt = str(offset + self.page_size) if offset + self.page_size < len(records) else None
        return {"history": page, "next_page_token": nxt, "history_id": str(self.profile_data.get("historyId"))}

    async def list_messages(self, query: str | None = None, page_token: str | None = None, max_results: int = 50,
                            label_ids: list[str] | None = None) -> dict:
        self.calls.append(("list_messages", (query, page_token)))
        ids = sorted(self.messages)
        if label_ids:
            ids = [i for i in ids if set(label_ids) & set(self.messages[i].get("labelIds") or [])]
        if query and query.startswith("rfc822msgid:"):
            want = query.split(":", 1)[1].strip()
            ids = [i for i in ids if _header_of(self.messages[i], "message-id").strip("<> ") == want.strip("<> ")]
        offset = int(page_token or 0)
        page = ids[offset: offset + max_results]
        nxt = str(offset + max_results) if offset + max_results < len(ids) else None
        return {"messages": [{"id": i, "thread_id": self.messages[i].get("threadId")} for i in page],
                "next_page_token": nxt, "result_size_estimate": len(ids)}

    async def get_message(self, message_id: str, format: str = "full") -> dict:
        raw = self.messages.get(message_id)
        if raw is None:
            raise ProviderError(f"message {message_id} not found", kind="invalid_input")
        (self.fetched_metadata if format == "metadata" else self.fetched_full).append(message_id)
        self.calls.append(("get_message", (message_id, format)))
        if format == "metadata":
            trimmed = {k: v for k, v in raw.items() if k != "payload"}
            payload = dict(raw.get("payload") or {})
            trimmed["payload"] = {"headers": payload.get("headers") or [], "mimeType": payload.get("mimeType")}
            return normalize_message(trimmed, format="metadata")
        return normalize_message(raw, format=format)

    async def get_thread(self, thread_id: str, format: str = "metadata") -> dict:
        ids = [i for i, m in sorted(self.messages.items()) if m.get("threadId") == thread_id]
        return {"thread_id": thread_id, "messages": [await self.get_message(i, format=format) for i in ids]}

    async def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        raw = self.messages.get(message_id) or {}
        for att in (raw.get("_attachment_data") or {}).items():
            if att[0] == attachment_id:
                return att[1] if isinstance(att[1], bytes) else str(att[1]).encode()
        raise ProviderError("attachment not found", kind="invalid_input")

    async def watch(self, topic: str, label_ids: list[str] | None = None) -> dict:
        if not topic:
            raise Unsupported("no Cloud Pub/Sub topic configured (GOOGLE_PUBSUB_TOPIC)", capability="watch")
        self.watch_calls += 1
        from datetime import timedelta
        expires = datetime.now(timezone.utc) + timedelta(days=7)
        if self.conn is not None:
            self.conn.watch_expires_at = expires
        return {"history_id": str(self.profile_data.get("historyId")), "expires_at": expires}

    async def stop(self) -> dict:
        self.stop_calls += 1
        if self.conn is not None:
            self.conn.watch_expires_at = None
        return {"stopped": True}

    async def create_draft(self, *, raw: str, thread_id: str | None = None) -> dict:
        self._require("drafts")
        self._seq += 1
        did = f"fdraft-{self._seq}"
        self.drafts[did] = {"raw": raw, "thread_id": thread_id, "message_id": f"fdmsg-{self._seq}"}
        return {"draft_id": did, "message_id": self.drafts[did]["message_id"], "thread_id": thread_id}

    async def update_draft(self, draft_id: str, *, raw: str, thread_id: str | None = None) -> dict:
        self._require("drafts")
        if draft_id not in self.drafts:
            raise ProviderError("draft not found", kind="conflict", code="draft_missing")
        self.drafts[draft_id] = {"raw": raw, "thread_id": thread_id, "message_id": self.drafts[draft_id]["message_id"]}
        return {"draft_id": draft_id, "message_id": self.drafts[draft_id]["message_id"], "thread_id": thread_id}

    async def get_draft(self, draft_id: str) -> dict | None:
        self._require("drafts")
        d = self.drafts.get(draft_id)
        if d is None:
            return None
        return {"draft_id": draft_id, "message": {"provider_message_id": d["message_id"], "thread_id": d.get("thread_id")}}

    async def delete_draft(self, draft_id: str) -> dict:
        self._require("drafts")
        self.drafts.pop(draft_id, None)
        return {"deleted": True, "draft_id": draft_id}

    async def send(self, raw: str, thread_id: str | None = None) -> dict:
        self._require("send")
        if self.fail_next_send is not None:
            err, self.fail_next_send = self.fail_next_send, None
            raise err
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
        self._seq += 1
        mid = f"fsent-{self._seq}"
        rfc = ""
        m = re.search(r"(?im)^Message-ID:\s*(<[^>]+>)\s*$", decoded)
        if m:
            rfc = m.group(1)
        record = {"message_id": mid, "thread_id": thread_id or f"fthread-{self._seq}", "raw": decoded,
                  "rfc_message_id": rfc}
        self.sent.append(record)
        self.messages[mid] = _sent_resource(mid, record["thread_id"], decoded)
        if self.send_then_lose_result:
            self.send_then_lose_result = False
            raise ProviderError("network dropped after send; result unknown", kind="unknown_result",
                                provider_ref=rfc)
        return {"message_id": mid, "thread_id": record["thread_id"], "label_ids": ["SENT"]}

    async def find_sent_by_message_id(self, rfc_message_id: str) -> dict | None:
        rid = (rfc_message_id or "").strip()
        for rec in self.sent:
            if rec.get("rfc_message_id", "").strip() == rid and rid:
                return {"message_id": rec["message_id"], "thread_id": rec["thread_id"], "rfc_message_id": rid}
        return None

    async def modify_labels(self, message_id: str, *, add: list[str] | None = None, remove: list[str] | None = None) -> dict:
        self._require("labels")
        raw = self.messages.get(message_id)
        if raw is None:
            raise ProviderError("message not found", kind="invalid_input")
        labels = [l for l in (raw.get("labelIds") or []) if l not in (remove or [])] + list(add or [])
        raw["labelIds"] = sorted(set(labels))
        return {"message_id": message_id, "label_ids": raw["labelIds"]}

    async def archive(self, message_id: str) -> dict:
        return await self.modify_labels(message_id, remove=["INBOX"])


def _sent_resource(message_id: str, thread_id: str, raw_mime: str) -> dict:
    """The Gmail resource a real send creates, so a later sync sees the same message we sent."""
    import email as _email
    parsed = _email.message_from_string(raw_mime)
    headers = [{"name": k, "value": v} for k, v in parsed.items()]
    if parsed.is_multipart():
        body = "".join((p.get_payload(decode=True) or b"").decode("utf-8", "replace")
                       for p in parsed.walk() if p.get_content_type() == "text/plain")
    else:
        body = (parsed.get_payload(decode=True) or b"").decode("utf-8", "replace")
    now = datetime.now(timezone.utc)
    return {"id": message_id, "threadId": thread_id, "labelIds": ["SENT"],
            "historyId": str(int(now.timestamp())), "internalDate": str(int(now.timestamp() * 1000)),
            "snippet": body[:120], "sizeEstimate": len(raw_mime),
            "payload": {"mimeType": "text/plain", "headers": headers,
                        "parts": [{"mimeType": "text/plain", "filename": "",
                                   "body": {"size": len(body), "data": base64.urlsafe_b64encode(body.encode()).decode()}}]}}


def _header_of(raw: dict, name: str) -> str:
    for h in ((raw.get("payload") or {}).get("headers") or []):
        if (h.get("name") or "").lower() == name:
            return h.get("value") or ""
    return ""


# ── factory ──────────────────────────────────────────────────────────────────
FACTORY = None  # tests install a callable(db, connection) -> adapter here


def build(db, connection: Connection):
    """The adapter for a gmail connection. Tests set ``gmail.FACTORY`` to a FakeGmail builder;
    without a factory and without a connected account the caller gets Unsupported, never a fake success."""
    if FACTORY is not None:
        return FACTORY(db, connection)
    if connection is None or connection.status in ("disconnected", None):
        raise Unsupported("Gmail is not connected; connect the account in Settings → Connections",
                          provider=(connection.provider if connection else "gmail_business"), setup_blocked=True)
    return GmailAdapter(db, connection)
