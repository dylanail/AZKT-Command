"""Passkey-only auth. No passwords, no email magic links.

First registration requires the one-time SETUP_TOKEN from .env so a random
first visitor can't claim the account. After one credential exists,
registration is closed (single user) unless an authenticated session adds
another device.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

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
from ..models import Credential, User

router = APIRouter(prefix="/auth", tags=["auth"])
_signer = URLSafeSerializer(settings.SESSION_SECRET, salt="session")
_chal = URLSafeSerializer(settings.SESSION_SECRET, salt="challenge")
SESSION_COOKIE = "azkt_session"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_session(resp: Response, user_id: str) -> None:
    token = _signer.dumps({"uid": user_id, "iat": datetime.now(timezone.utc).isoformat()})
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, secure=True,
                    samesite="strict", max_age=60 * 60 * 24 * 30)


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        raise HTTPException(401, "no session")
    try:
        data = _signer.loads(raw)
    except BadSignature:
        raise HTTPException(401, "bad session")
    user = await db.get(User, data["uid"])
    if not user:
        raise HTTPException(401, "unknown user")
    return user


@router.get("/state")
async def state(request: Request, db: AsyncSession = Depends(get_db)):
    count = await db.scalar(select(func.count()).select_from(User))
    authed = False
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        try:
            data = _signer.loads(raw)
            authed = await db.get(User, data["uid"]) is not None
        except (BadSignature, KeyError):
            authed = False
    return {"registered": count > 0, "authed": authed}


@router.post("/register/options")
async def register_options(request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.json()
    user_count = await db.scalar(select(func.count()).select_from(User))
    if user_count == 0:
        if body.get("setup_token") != settings.SETUP_TOKEN:
            raise HTTPException(403, "invalid setup token")
    else:
        await current_user(request, db)  # only an authed user can add devices

    handle = body.get("handle", "owner")
    label = (body.get("label") or "").strip()[:80] or "passkey"
    user = (await db.execute(select(User).where(User.handle == handle))).scalar_one_or_none()
    uid = user.id if user else handle
    opts = generate_registration_options(
        rp_id=settings.WEBAUTHN_RP_ID,
        rp_name=settings.WEBAUTHN_RP_NAME,
        user_name=handle,
        user_id=uid.encode(),
        authenticator_selection=AuthenticatorSelectionCriteria(
            # Login is usernameless (no allowCredentials), so the credential
            # MUST be discoverable or it can't be found at sign-in. Safari/iOS
            # do this by default; desktop Chrome + security keys do not.
            resident_key=ResidentKeyRequirement.REQUIRED,
            require_resident_key=True,
            user_verification=UserVerificationRequirement.REQUIRED,  # biometric
        ),
    )
    resp = Response(options_to_json(opts), media_type="application/json")
    resp.set_cookie("reg_chal", _chal.dumps({"c": _b64(opts.challenge), "h": handle, "l": label}),
                    httponly=True, secure=True, samesite="strict", max_age=300)
    return resp


@router.post("/register/verify")
async def register_verify(request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.json()
    try:
        ch = _chal.loads(request.cookies.get("reg_chal", ""))
    except BadSignature:
        raise HTTPException(400, "missing/invalid challenge")
    verification = verify_registration_response(
        credential=json.dumps(body),
        expected_challenge=_unb64(ch["c"]),
        expected_rp_id=settings.WEBAUTHN_RP_ID,
        expected_origin=settings.PUBLIC_ORIGIN,
        require_user_verification=True,
    )
    user = (await db.execute(select(User).where(User.handle == ch["h"]))).scalar_one_or_none()
    if user is None:
        user = User(handle=ch["h"], created_at=datetime.now(timezone.utc))
        db.add(user)
        await db.flush()
    db.add(Credential(
        user_id=user.id,
        credential_id=verification.credential_id,
        public_key=verification.credential_public_key,
        sign_count=verification.sign_count,
        label=ch.get("l") or "passkey",
        created_at=datetime.now(timezone.utc),
    ))
    await db.commit()
    resp = Response(json.dumps({"ok": True}), media_type="application/json")
    resp.delete_cookie("reg_chal")
    issue_session(resp, user.id)
    return resp


@router.post("/login/options")
async def login_options():
    opts = generate_authentication_options(
        rp_id=settings.WEBAUTHN_RP_ID,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    resp = Response(options_to_json(opts), media_type="application/json")
    resp.set_cookie("auth_chal", _chal.dumps({"c": _b64(opts.challenge)}),
                    httponly=True, secure=True, samesite="strict", max_age=300)
    return resp


@router.post("/login/verify")
async def login_verify(request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.json()
    try:
        ch = _chal.loads(request.cookies.get("auth_chal", ""))
    except BadSignature:
        raise HTTPException(400, "missing/invalid challenge")
    raw_id = _unb64(body["rawId"])
    cred = (await db.execute(
        select(Credential).where(Credential.credential_id == raw_id)
    )).scalar_one_or_none()
    if cred is None:
        raise HTTPException(401, "unknown credential")
    verification = verify_authentication_response(
        credential=json.dumps(body),
        expected_challenge=_unb64(ch["c"]),
        expected_rp_id=settings.WEBAUTHN_RP_ID,
        expected_origin=settings.PUBLIC_ORIGIN,
        credential_public_key=cred.public_key,
        credential_current_sign_count=cred.sign_count,
        require_user_verification=True,
    )
    cred.sign_count = verification.new_sign_count
    await db.commit()
    resp = Response(json.dumps({"ok": True}), media_type="application/json")
    resp.delete_cookie("auth_chal")
    issue_session(resp, cred.user_id)
    return resp


@router.post("/logout")
async def logout():
    resp = Response(json.dumps({"ok": True}), media_type="application/json")
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ── Passkey management (authed: name / list / revoke devices) ───────────────
@router.get("/credentials")
async def list_credentials(request: Request, db: AsyncSession = Depends(get_db)):
    user = await current_user(request, db)
    rows = (await db.execute(
        select(Credential).where(Credential.user_id == user.id)
        .order_by(Credential.created_at)
    )).scalars().all()
    return [{
        "id": c.id,
        "label": c.label,
        "transports": c.transports,
        "sign_count": c.sign_count,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    } for c in rows]


@router.post("/credentials/{cid}/rename")
async def rename_credential(cid: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await current_user(request, db)
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
async def revoke_credential(cid: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await current_user(request, db)
    cred = await db.get(Credential, cid)
    if cred is None or cred.user_id != user.id:
        raise HTTPException(404, "no such passkey")
    remaining = await db.scalar(
        select(func.count()).select_from(Credential).where(Credential.user_id == user.id)
    )
    if remaining <= 1:
        raise HTTPException(400, "cannot revoke your only passkey — add another first")
    await db.delete(cred)
    await db.commit()
    return {"ok": True}
