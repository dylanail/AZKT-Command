"""Passkey-only auth (no passwords). Extended for roles, session versions and invitations.

First registration requires SETUP_TOKEN and creates the owner. Invited people register a
passkey with a one-use invite token. Access changes bump `users.session_version`, which
invalidates every existing session for that person (spec §3.1 Person / §11.1).
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from ..core.config import settings
from ..db import get_db
from ..models import Credential, Invitation, User

router = APIRouter(prefix="/auth", tags=["auth"])
_signer = URLSafeSerializer(settings.SESSION_SECRET, salt="session")
_chal = URLSafeSerializer(settings.SESSION_SECRET, salt="challenge")
SESSION_COOKIE = "azkt_session"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _cookie_secure() -> bool:
    return settings.PUBLIC_ORIGIN.startswith("https://")


def session_token(user: User) -> str:
    return _signer.dumps({"uid": user.id, "sv": user.session_version or 1,
                          "iat": datetime.now(timezone.utc).isoformat()})


def issue_session(resp: Response, user: User) -> None:
    resp.set_cookie(SESSION_COOKIE, session_token(user), httponly=True, secure=_cookie_secure(),
                    samesite="strict", max_age=settings.SESSION_MAX_AGE_SECONDS)


async def user_from_request(request: Request, db: AsyncSession) -> User | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        data = _signer.loads(raw)
    except BadSignature:
        return None
    user = await db.get(User, data.get("uid"))
    if not user or user.status != "active":
        return None
    if int(data.get("sv", 1)) != int(user.session_version or 1):
        return None  # access changed: session invalidated
    return user


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    user = await user_from_request(request, db)
    if user is None:
        raise HTTPException(401, "no session")
    return user


@router.get("/state")
async def state(request: Request, db: AsyncSession = Depends(get_db)):
    count = await db.scalar(select(func.count()).select_from(User))
    user = await user_from_request(request, db)
    return {"registered": count > 0, "authed": user is not None,
            "user": _me(user) if user else None}


def _me(u: User) -> dict:
    from ..domain.policy import effective_perms
    return {"id": u.id, "handle": u.handle, "display_name": u.display_name or u.handle, "role": u.role,
            "scope": u.scope, "email": u.email, "timezone": u.timezone,
            "perms": effective_perms(u.role, u.perms), "status": u.status}


@router.get("/me")
async def me(user: User = Depends(current_user)):
    return _me(user)


async def _invitation_by_token(db: AsyncSession, token: str) -> Invitation | None:
    h = hashlib.sha256(token.encode()).hexdigest()
    inv = (await db.execute(select(Invitation).where(Invitation.token_hash == h))).scalar_one_or_none()
    if inv is None or inv.status != "pending":
        return None
    if inv.expires_at and inv.expires_at < datetime.now(timezone.utc):
        inv.status = "expired"
        return None
    return inv


@router.post("/register/options")
async def register_options(request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.json()
    user_count = await db.scalar(select(func.count()).select_from(User))
    mode = "add_device"
    handle = body.get("handle", "owner")
    inv: Invitation | None = None
    if body.get("invite_token"):
        inv = await _invitation_by_token(db, body["invite_token"])
        if inv is None:
            raise HTTPException(403, "invalid or expired invitation")
        mode = "invite"
        handle = f"inv-{inv.id[:8]}"
    elif user_count == 0:
        if body.get("setup_token") != settings.SETUP_TOKEN:
            raise HTTPException(403, "invalid setup token")
        mode = "bootstrap"
        handle = "owner"
    else:
        user = await user_from_request(request, db)
        if user is None:
            raise HTTPException(401, "sign in to add a passkey")
        handle = user.handle  # only your own account
    label = (body.get("label") or "").strip()[:80] or "passkey"
    user = (await db.execute(select(User).where(User.handle == handle))).scalar_one_or_none()
    uid = user.id if user else handle
    opts = generate_registration_options(
        rp_id=settings.WEBAUTHN_RP_ID, rp_name=settings.WEBAUTHN_RP_NAME, user_name=handle, user_id=uid.encode(),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED, require_resident_key=True,
            user_verification=UserVerificationRequirement.REQUIRED),
    )
    resp = Response(options_to_json(opts), media_type="application/json")
    resp.set_cookie("reg_chal", _chal.dumps({"c": _b64(opts.challenge), "h": handle, "l": label, "m": mode,
                                             "inv": inv.id if inv else None}),
                    httponly=True, secure=_cookie_secure(), samesite="strict", max_age=300)
    return resp


@router.post("/register/verify")
async def register_verify(request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.json()
    try:
        ch = _chal.loads(request.cookies.get("reg_chal", ""))
    except BadSignature:
        raise HTTPException(400, "missing/invalid challenge")
    verification = verify_registration_response(
        credential=json.dumps(body), expected_challenge=_unb64(ch["c"]), expected_rp_id=settings.WEBAUTHN_RP_ID,
        expected_origin=settings.PUBLIC_ORIGIN, require_user_verification=True)
    now = datetime.now(timezone.utc)
    user = (await db.execute(select(User).where(User.handle == ch["h"]))).scalar_one_or_none()
    if user is None:
        if ch.get("m") == "invite":
            inv = await db.get(Invitation, ch.get("inv"))
            if inv is None or inv.status != "pending":
                raise HTTPException(403, "invitation no longer valid")
            user = User(handle=ch["h"], display_name=inv.display_name, email=inv.email, phone=inv.phone,
                        role=inv.role, scope=inv.scope, manager_id=inv.manager_id, perms=inv.perms or {},
                        status="active", created_at=now)
            db.add(user)
            await db.flush()
            inv.status = "accepted"
            inv.accepted_at = now
            inv.accepted_user_id = user.id
        elif ch.get("m") == "bootstrap":
            user = User(handle="owner", display_name="Owner", role="owner", scope="all", status="active", created_at=now)
            db.add(user)
            await db.flush()
        else:
            raise HTTPException(403, "registration closed")
    db.add(Credential(user_id=user.id, credential_id=verification.credential_id,
                      public_key=verification.credential_public_key, sign_count=verification.sign_count,
                      label=ch.get("l") or "passkey", created_at=now))
    await db.commit()
    resp = Response(json.dumps({"ok": True, "user": _me(user)}), media_type="application/json")
    resp.delete_cookie("reg_chal")
    issue_session(resp, user)
    return resp


@router.post("/login/options")
async def login_options():
    opts = generate_authentication_options(rp_id=settings.WEBAUTHN_RP_ID,
                                           user_verification=UserVerificationRequirement.REQUIRED)
    resp = Response(options_to_json(opts), media_type="application/json")
    resp.set_cookie("auth_chal", _chal.dumps({"c": _b64(opts.challenge)}), httponly=True,
                    secure=_cookie_secure(), samesite="strict", max_age=300)
    return resp


@router.post("/login/verify")
async def login_verify(request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.json()
    try:
        ch = _chal.loads(request.cookies.get("auth_chal", ""))
    except BadSignature:
        raise HTTPException(400, "missing/invalid challenge")
    raw_id = _unb64(body["rawId"])
    cred = (await db.execute(select(Credential).where(Credential.credential_id == raw_id))).scalar_one_or_none()
    if cred is None:
        raise HTTPException(401, "unknown credential")
    user = await db.get(User, cred.user_id)
    if user is None or user.status != "active":
        raise HTTPException(403, "account disabled")
    verification = verify_authentication_response(
        credential=json.dumps(body), expected_challenge=_unb64(ch["c"]), expected_rp_id=settings.WEBAUTHN_RP_ID,
        expected_origin=settings.PUBLIC_ORIGIN, credential_public_key=cred.public_key,
        credential_current_sign_count=cred.sign_count, require_user_verification=True)
    cred.sign_count = verification.new_sign_count
    user.last_seen_at = datetime.now(timezone.utc)
    await db.commit()
    resp = Response(json.dumps({"ok": True, "user": _me(user)}), media_type="application/json")
    resp.delete_cookie("auth_chal")
    issue_session(resp, user)
    return resp


@router.post("/logout")
async def logout():
    resp = Response(json.dumps({"ok": True}), media_type="application/json")
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.get("/invitation/{token}")
async def invitation_preview(token: str, db: AsyncSession = Depends(get_db)):
    inv = await _invitation_by_token(db, token)
    if inv is None:
        raise HTTPException(404, "invitation not found or expired")
    return {"display_name": inv.display_name, "role": inv.role, "expires_at": inv.expires_at.isoformat()}


# ── Passkey management (own devices) ────────────────────────────────────────
@router.get("/credentials")
async def list_credentials(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Credential).where(Credential.user_id == user.id).order_by(Credential.created_at))).scalars().all()
    return [{"id": c.id, "label": c.label, "transports": c.transports, "sign_count": c.sign_count,
             "created_at": c.created_at.isoformat() if c.created_at else None} for c in rows]


@router.post("/credentials/{cid}/rename")
async def rename_credential(cid: str, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    body = await request.json()
    label = (body.get("label") or "").strip()[:80]
    if not label:
        raise HTTPException(400, "label required")
    cred = await db.get(Credential, cid)
    if cred is None or cred.user_id != user.id:
        raise HTTPException(404, "no such passkey")
    cred.label = label
    await db.commit()
    return {"ok": True, "label": cred.label}


@router.delete("/credentials/{cid}")
async def revoke_credential(cid: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    cred = await db.get(Credential, cid)
    if cred is None or cred.user_id != user.id:
        raise HTTPException(404, "no such passkey")
    remaining = await db.scalar(select(func.count()).select_from(Credential).where(Credential.user_id == user.id))
    if remaining <= 1:
        raise HTTPException(400, "cannot revoke your only passkey — add another first")
    await db.delete(cred)
    await db.commit()
    return {"ok": True}


def invite_expiry(days: int = 7) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)
