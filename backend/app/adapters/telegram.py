"""Telegram Bot API client (official HTTPS API; no personal-account automation) — spec §5.5."""
from __future__ import annotations

import hmac
import logging
from typing import Any

import httpx

from ..core.config import settings
from ..core.errors import ProviderError, Unsupported

log = logging.getLogger("azkt.telegram")
API = "https://api.telegram.org"


class TelegramClient:
    def __init__(self, token: str | None = None):
        self.token = token or settings.TELEGRAM_BOT_TOKEN
        self._recorded: list[dict] = []  # test transport when token == "test"

    @property
    def configured(self) -> bool:
        return bool(self.token)

    async def call(self, method: str, **params: Any) -> dict:
        if not self.token:
            raise Unsupported("TELEGRAM_BOT_TOKEN not configured")
        if self.token == "test":
            self._recorded.append({"method": method, **params})
            return {"ok": True, "result": {"message_id": len(self._recorded), **({"text": params.get("text")} if "text" in params else {})}}
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{API}/bot{self.token}/{method}", json=params)
        if r.status_code == 429:
            retry = (r.json().get("parameters") or {}).get("retry_after", 5) if r.content else 5
            raise ProviderError("telegram rate limited", kind="rate_limited", retry_after=retry)
        if r.status_code == 401:
            raise ProviderError("telegram bot token invalid or rotated", kind="auth_expired")
        data = r.json() if r.content else {}
        if r.status_code >= 500:
            raise ProviderError(f"telegram error {r.status_code}", kind="transient")
        if not data.get("ok"):
            desc = data.get("description", "")
            if "blocked" in desc.lower() or "deactivated" in desc.lower():
                raise ProviderError(f"telegram delivery failed: {desc}", kind="blocked")
            raise ProviderError(f"telegram request failed: {desc}", kind="invalid_input")
        return data

    async def send_message(self, chat_id: int, text: str, *, reply_markup: dict | None = None,
                           disable_preview: bool = True, parse_mode: str | None = None) -> dict:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text[:4000],
                                  "link_preview_options": {"is_disabled": disable_preview}}
        if reply_markup:
            params["reply_markup"] = reply_markup
        if parse_mode:
            params["parse_mode"] = parse_mode
        return await self.call("sendMessage", **params)

    async def answer_callback(self, callback_query_id: str, text: str | None = None) -> dict:
        return await self.call("answerCallbackQuery", callback_query_id=callback_query_id, text=(text or "")[:200])

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None) -> dict:
        params: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000]}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return await self.call("editMessageText", **params)

    async def set_webhook(self, url: str, secret: str) -> dict:
        return await self.call("setWebhook", url=url, secret_token=secret, allowed_updates=["message", "callback_query"],
                               drop_pending_updates=False)

    async def get_file(self, file_id: str) -> bytes:
        info = await self.call("getFile", file_id=file_id)
        path = info["result"]["file_path"]
        if self.token == "test":
            return b""
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.get(f"{API}/file/bot{self.token}/{path}")
            r.raise_for_status()
            return r.content

    async def get_me(self) -> dict:
        return await self.call("getMe")


def webhook_secret_ok(header_value: str | None) -> bool:
    secret = settings.TELEGRAM_WEBHOOK_SECRET
    if not secret:
        return False  # never accept unauthenticated updates
    return bool(header_value) and hmac.compare_digest(header_value, secret)


def deep_link(start_token: str) -> str:
    return f"https://t.me/{settings.TELEGRAM_BOT_USERNAME}?start={start_token}" if settings.TELEGRAM_BOT_USERNAME else ""
