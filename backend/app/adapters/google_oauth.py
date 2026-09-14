"""Server-side Google OAuth (authorization code + PKCE) shared by Gmail, Drive and Sheets.
Tokens are stored encrypted on the Connection row; refresh is transparent (spec §4.1, §7.1)."""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.errors import ProviderError, Unsupported
from ..models.comms import Connection
from ..services import connections as conn_svc

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

SCOPES = {
    # Request read access for ingestion; send/modify only when a feature is enabled (spec §4.1).
    "gmail_business": ["openid", "email", "https://www.googleapis.com/auth/gmail.readonly"],
    "gmail_business_send": ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.compose"],
    "gmail_business_modify": ["https://www.googleapis.com/auth/gmail.modify"],
    "gmail_personal": ["openid", "email", "https://www.googleapis.com/auth/gmail.readonly"],
    "drive": ["openid", "email", "https://www.googleapis.com/auth/drive.readonly"],
    "sheets": ["openid", "email", "https://www.googleapis.com/auth/spreadsheets.readonly",
               "https://www.googleapis.com/auth/drive.metadata.readonly"],
}

_state = URLSafeTimedSerializer(settings.SESSION_SECRET, salt="google-oauth-state")


def configured() -> bool:
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


def redirect_uri() -> str:
    return f"{settings.api_base}/api/connections/google/callback"


def start(provider: str, user_id: str, extra_scopes: list[str] | None = None) -> dict:
    """Return {url, state}. The state is signed and carries a PKCE verifier."""
    if not configured():
        raise Unsupported("Google OAuth client not configured (GOOGLE_CLIENT_ID/SECRET)")
    if provider not in SCOPES:
        raise Unsupported(f"unknown google provider {provider}")
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    scopes = list(SCOPES[provider]) + list(extra_scopes or [])
    state = _state.dumps({"p": provider, "u": user_id, "v": verifier, "n": secrets.token_urlsafe(8), "s": scopes})
    params = {"client_id": settings.GOOGLE_CLIENT_ID, "redirect_uri": redirect_uri(), "response_type": "code",
              "scope": " ".join(scopes), "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
              "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
    return {"url": f"{AUTH_URL}?{urlencode(params)}", "state": state, "scopes": scopes}


def parse_state(state: str, max_age: int = 900) -> dict:
    try:
        return _state.loads(state, max_age=max_age)
    except BadSignature as e:
        raise ProviderError("invalid or expired OAuth state") from e


async def exchange(db: AsyncSession, state: str, code: str) -> Connection:
    data = parse_state(state)
    provider = data["p"]
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(TOKEN_URL, data={"code": code, "client_id": settings.GOOGLE_CLIENT_ID,
                                          "client_secret": settings.GOOGLE_CLIENT_SECRET, "redirect_uri": redirect_uri(),
                                          "grant_type": "authorization_code", "code_verifier": data["v"]})
        if r.status_code != 200:
            raise ProviderError(f"token exchange failed: {r.text[:200]}")
        tok = r.json()
        info = {}
        ui = await c.get(USERINFO_URL, headers={"Authorization": f"Bearer {tok['access_token']}"})
        if ui.status_code == 200:
            info = ui.json()
    conn = await conn_svc.get(db, provider, create=True)
    identity = info.get("email")
    expected = settings.BUSINESS_EMAIL if provider == "gmail_business" else (conn.config or {}).get("expected_identity")
    if expected and identity and identity.lower() != expected.lower():
        # A07: identity mismatch is surfaced, never silently accepted.
        conn.status = "error"
        conn.failure = {"kind": "identity_mismatch", "message": f"Google account {identity} is not {expected}",
                        "at": datetime.now(timezone.utc).isoformat()}
        await db.flush()
        raise ProviderError(f"Signed in as {identity}, expected {expected}. Connection not saved.")
    granted = tok.get("scope", "").split()
    conn_svc.set_secret(conn, {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
                               "expires_at": time.time() + int(tok.get("expires_in", 3600)), "token_type": tok.get("token_type")})
    conn.account_identity = identity
    conn.scopes = data.get("s", [])
    conn.granted_scopes = granted
    conn.status = "connected"
    conn.connected_by = data["u"]
    conn.connected_at = datetime.now(timezone.utc)
    conn.failure = {}
    conn.environment = settings.ENV
    await db.flush()
    return conn


async def access_token(db: AsyncSession, conn: Connection) -> str:
    sec = conn_svc.get_secret(conn)
    if not sec.get("access_token"):
        raise ProviderError("connection has no token; reconnect required")
    if sec.get("expires_at", 0) - 60 > time.time():
        return sec["access_token"]
    if not sec.get("refresh_token"):
        await conn_svc.mark_failure(db, conn, "auth_expired", "no refresh token; reconnect")
        raise ProviderError("token expired and no refresh token; reconnect")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(TOKEN_URL, data={"client_id": settings.GOOGLE_CLIENT_ID, "client_secret": settings.GOOGLE_CLIENT_SECRET,
                                          "refresh_token": sec["refresh_token"], "grant_type": "refresh_token"})
    if r.status_code != 200:
        await conn_svc.mark_failure(db, conn, "auth_expired", f"refresh failed: {r.text[:200]}")
        raise ProviderError("token refresh failed; reconnect")
    tok = r.json()
    sec["access_token"] = tok["access_token"]
    sec["expires_at"] = time.time() + int(tok.get("expires_in", 3600))
    conn_svc.set_secret(conn, sec)
    await db.flush()
    return sec["access_token"]


async def revoke(db: AsyncSession, conn: Connection) -> None:
    sec = conn_svc.get_secret(conn)
    token = sec.get("refresh_token") or sec.get("access_token")
    if token:
        async with httpx.AsyncClient(timeout=15) as c:
            try:
                await c.post(REVOKE_URL, params={"token": token})
            except httpx.HTTPError:
                pass
    conn.secret_enc = None
    conn.status = "disconnected"
    conn.disconnected_at = datetime.now(timezone.utc)


class GoogleApi:
    """Minimal authenticated client for Google REST APIs with typed error mapping."""

    def __init__(self, db: AsyncSession, conn: Connection):
        self.db = db
        self.conn = conn

    async def request(self, method: str, url: str, **kw) -> dict:
        token = await access_token(self.db, self.conn)
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.request(method, url, headers={"Authorization": f"Bearer {token}", **kw.pop("headers", {})}, **kw)
        if r.status_code == 401:
            await conn_svc.mark_failure(self.db, self.conn, "auth_expired", "401 from Google")
            raise ProviderError("google auth expired", kind="auth_expired")
        if r.status_code == 403:
            await conn_svc.mark_failure(self.db, self.conn, "permission_denied", r.text[:200])
            raise ProviderError("google permission denied", kind="permission_denied")
        if r.status_code == 429:
            await conn_svc.mark_failure(self.db, self.conn, "rate_limited", "429")
            raise ProviderError("google rate limited", kind="rate_limited")
        if r.status_code >= 500:
            await conn_svc.mark_failure(self.db, self.conn, "transient", f"{r.status_code}")
            raise ProviderError(f"google error {r.status_code}", kind="transient")
        if r.status_code >= 400:
            raise ProviderError(f"google request failed {r.status_code}: {r.text[:200]}", kind="invalid_input")
        try:
            return r.json() if r.content else {}
        except ValueError:
            return {"raw": r.text}
