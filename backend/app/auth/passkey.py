"""Passkey-only auth (no passwords). Extended for roles, session versions and invitations.

First registration requires SETUP_TOKEN and creates the owner. Invited people register a
passkey with a one-use invite token. Access changes bump `users.session_version`, which
invalidates every existing session for that person (spec §3.1 Person / §11.1).

A passkey never leaves the device that made it, so every device you sign in from needs its own.
Three ways to add one to an account that already exists:

* on the device you are reading this from — `POST /auth/register/options` with a session and no
  token (mode `add_device`);
* on a second device — mint a one-time link here (`POST /auth/device-link`), open it there
  (mode `device`). The link is shown once, only its hash is stored, it expires in minutes, dies
  on first use, and any access change kills it;
* through the browser's own "use a phone or tablet" QR, which the registration options allow by
  not restricting the authenticator.
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
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from ..core.config import settings
from ..core.ids import sha256_hex, token as new_token
from ..db import get_db
from ..models import Credential, DeviceEnrollment, Invitation, User
from ..models.runtime import ActivityEntry

router = APIRouter(prefix="/auth", tags=["auth"])
_signer = URLSafeSerializer(settings.SESSION_SECRET, salt="session")
_chal = URLSafeSerializer(settings.SESSION_SECRET, salt="challenge")
SESSION_COOKIE = "azkt_session"
DEVICE_LINK_TTL_MINUTES = 15  # it adds a way in, so it is short-lived on purpose: scan it now or make another
DEVICE_LINK_PATH = "/add-device"  # the PWA screen a second device opens


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


def _record(db: AsyncSession, user: User, what: str, *, state: str | None = None, details: dict | None = None) -> None:
    """Security-relevant auth events land in Activity like every other access change (spec §2.3):
    the person is the entity, the passkey or link id is in the details."""
    db.add(ActivityEntry(at=datetime.now(timezone.utc),
                         actor={"kind": "user", "user_id": user.id, "name": user.display_name or user.handle},
                         what=what, entity_kind="user", entity_id=user.id, kind="access",
                         state=state, visibility="owner", details=details or {}))


def serialize_enrollment(e: DeviceEnrollment) -> dict:
    """Never includes the token or its hash."""
    return {"id": e.id, "label": e.label, "status": e.status,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "expires_at": e.expires_at.isoformat() if e.expires_at else None,
            "used_at": e.used_at.isoformat() if e.used_at else None}


async def _pending_enrollment(db: AsyncSession, enrollment_id: str | None) -> DeviceEnrollment | None:
    """A live link by id. Expiry is settled here so a stale row never counts as pending."""
    e = await db.get(DeviceEnrollment, enrollment_id) if enrollment_id else None
    if e is None or e.status != "pending":
        return None
    if e.expires_at and e.expires_at < datetime.now(timezone.utc):
        e.status = "expired"
        return None
    return e


async def _enrollment_by_token(db: AsyncSession, token: str) -> DeviceEnrollment | None:
    h = sha256_hex(token)
    e = (await db.execute(select(DeviceEnrollment).where(DeviceEnrollment.token_hash == h))).scalar_one_or_none()
    return await _pending_enrollment(db, e.id) if e is not None else None


async def revoke_device_links(db: AsyncSession, user_id: str, reason: str) -> list[str]:
    """Kill every live add-a-device link for this person: minting a new one, and any access change
    (a live link is a queued session — see team._invalidate_queued_authorization)."""
    rows = (await db.execute(select(DeviceEnrollment).where(
        DeviceEnrollment.user_id == user_id, DeviceEnrollment.status == "pending"))).scalars().all()
    now = datetime.now(timezone.utc)
    for e in rows:
        e.status = "revoked"
        e.revoked_at = now
        e.revoked_reason = reason
    return [e.id for e in rows]


async def _exclude_credentials(db: AsyncSession, user: User | None) -> list[PublicKeyCredentialDescriptor]:
    """Passkeys this account already has, so a device that holds one says "already registered"
    instead of silently making a second. It never blocks a *different* device."""
    if user is None:
        return []
    rows = (await db.execute(select(Credential).where(Credential.user_id == user.id))).scalars().all()
    return [PublicKeyCredentialDescriptor(id=c.credential_id) for c in rows]


def _qr(data: str) -> dict | None:
    """QR modules for the link, drawn by the dash. Scanning beats retyping or texting yourself a
    one-time credential. Optional: an environment without segno just shows the link."""
    try:
        import segno
    except ImportError:  # pragma: no cover - only on an environment that skipped requirements.txt
        return None
    matrix = segno.make(data, error="m", micro=False).matrix
    return {"rows": ["".join("1" if m else "0" for m in row) for row in matrix]}


@router.post("/register/options")
async def register_options(request: Request, db: AsyncSession = Depends(get_db)):
    """Options for a new passkey. Four ways in: an invitation, first-run setup, an add-a-device
    link opened on a second device, or a session on the device you are already using."""
    body = await request.json()
    user_count = await db.scalar(select(func.count()).select_from(User))
    mode = "add_device"
    handle = body.get("handle", "owner")
    inv: Invitation | None = None
    enrollment: DeviceEnrollment | None = None
    label_default = "passkey"
    if body.get("invite_token"):
        inv = await _invitation_by_token(db, body["invite_token"])
        if inv is None:
            raise HTTPException(403, "invalid or expired invitation")
        mode = "invite"
        handle = f"inv-{inv.id[:8]}"
    elif body.get("device_token"):
        enrollment = await _enrollment_by_token(db, body["device_token"])
        if enrollment is None:
            await db.commit()  # an expiry settled by the lookup
            raise HTTPException(403, "this add-a-device link has expired or was already used")
        owner = await db.get(User, enrollment.user_id)
        if owner is None or owner.status != "active":
            raise HTTPException(403, "that account can no longer sign in")
        mode = "device"
        handle = owner.handle
        label_default = enrollment.label or "passkey"
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
    label = (body.get("label") or "").strip()[:80] or label_default
    user = (await db.execute(select(User).where(User.handle == handle))).scalar_one_or_none()
    uid = user.id if user else handle
    opts = generate_registration_options(
        rp_id=settings.WEBAUTHN_RP_ID, rp_name=settings.WEBAUTHN_RP_NAME, user_name=handle, user_id=uid.encode(),
        user_display_name=(user.display_name or handle) if user else handle,
        # No authenticator_attachment: the browser may offer this device's own passkey *or* its
        # "use a phone or tablet" QR, which is the other way a desktop enrolls a phone.
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED, require_resident_key=True,
            user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=await _exclude_credentials(db, user),
    )
    await db.commit()  # persist any expiry settled above
    resp = Response(options_to_json(opts), media_type="application/json")
    resp.set_cookie("reg_chal", _chal.dumps({"c": _b64(opts.challenge), "h": handle, "l": label, "m": mode,
                                             "inv": inv.id if inv else None,
                                             "dev": enrollment.id if enrollment else None}),
                    httponly=True, secure=_cookie_secure(), samesite="strict", max_age=300)
    return resp


@router.post("/register/verify")
async def register_verify(request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.json()
    try:
        ch = _chal.loads(request.cookies.get("reg_chal", ""))
    except BadSignature:
        raise HTTPException(400, "missing/invalid challenge")
    now = datetime.now(timezone.utc)
    mode = ch.get("m") or "add_device"
    user = (await db.execute(select(User).where(User.handle == ch["h"]))).scalar_one_or_none()
    # Adding to an account that already exists is re-checked here, before the passkey is accepted:
    # the link can be used or revoked, and the session can end, in the minutes since options.
    enrollment: DeviceEnrollment | None = None
    if mode == "device":
        enrollment = await _pending_enrollment(db, ch.get("dev"))
        if enrollment is None or user is None or user.id != enrollment.user_id or user.status != "active":
            await db.commit()
            raise HTTPException(403, "this add-a-device link has expired or was already used")
    elif mode == "add_device":
        signed_in = await user_from_request(request, db)
        if signed_in is None or user is None or signed_in.id != user.id:
            raise HTTPException(401, "sign in to add a passkey")
    verification = verify_registration_response(
        credential=json.dumps(body), expected_challenge=_unb64(ch["c"]), expected_rp_id=settings.WEBAUTHN_RP_ID,
        expected_origin=settings.PUBLIC_ORIGIN, require_user_verification=True)
    if user is None:
        if mode == "invite":
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
        elif mode == "bootstrap":
            user = User(handle="owner", display_name="Owner", role="owner", scope="all", status="active", created_at=now)
            db.add(user)
            await db.flush()
        else:
            raise HTTPException(403, "registration closed")
    # "internal", "hybrid", "usb"… — the browser's hint about how this passkey is reached. Whatever
    # the client sends is bounded before it is stored; nothing reads it back as a decision.
    hints = (body.get("response") if isinstance(body.get("response"), dict) else {}).get("transports")
    transports = ",".join(t[:16] for t in hints if isinstance(t, str))[:120] if isinstance(hints, list) else ""
    cred = Credential(user_id=user.id, credential_id=verification.credential_id,
                      public_key=verification.credential_public_key, sign_count=verification.sign_count,
                      transports=transports, label=ch.get("l") or "passkey", created_at=now)
    db.add(cred)
    await db.flush()
    if enrollment is not None:
        enrollment.status = "used"
        enrollment.used_at = now
        enrollment.credential_id = cred.id
    if mode in ("device", "add_device"):
        _record(db, user, f'Added passkey "{cred.label}"', state="added",
                details={"credential_id": cred.id, "label": cred.label, "transports": transports,
                         "via": "add-a-device link" if enrollment is not None else "this device"})
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
    cred.last_used_at = datetime.now(timezone.utc)
    user.last_seen_at = cred.last_used_at
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
    """Every way this person can sign in. `transports` is a list ("internal" = this device's own
    biometrics, "hybrid" = a phone scanned from another device)."""
    rows = (await db.execute(select(Credential).where(Credential.user_id == user.id).order_by(Credential.created_at))).scalars().all()
    return [{"id": c.id, "label": c.label, "transports": [t for t in (c.transports or "").split(",") if t],
             "sign_count": c.sign_count,
             "created_at": c.created_at.isoformat() if c.created_at else None,
             "last_used_at": c.last_used_at.isoformat() if c.last_used_at else None} for c in rows]


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
    _record(db, user, f'Revoked passkey "{cred.label}"', state="revoked",
            details={"credential_id": cred.id, "label": cred.label, "remaining": int(remaining) - 1})
    await db.delete(cred)
    await db.commit()
    return {"ok": True}


# ── Adding a second device (spec §11.1: a passkey cannot move between devices) ──────────────
@router.post("/device-link")
async def mint_device_link(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Mint the one-time link that lets another device register its own passkey for this account.

    Handed to the signed-in person once and never stored: only the hash is kept, the way invitation
    links work. One live link per person — minting again kills the last one.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a bodyless POST is a link with no name on it
        body = {}
    label = (body.get("label") or "").strip()[:80]
    replaced = await revoke_device_links(db, user.id, "replaced by a newer link")
    now = datetime.now(timezone.utc)
    raw = new_token(32)
    enrollment = DeviceEnrollment(user_id=user.id, token_hash=sha256_hex(raw), label=label, status="pending",
                                  created_at=now, expires_at=now + timedelta(minutes=DEVICE_LINK_TTL_MINUTES))
    db.add(enrollment)
    await db.flush()
    _record(db, user, "Issued an add-a-device link", state="pending",
            details={"enrollment_id": enrollment.id, "label": label, "replaced": replaced,
                     "expires_at": enrollment.expires_at.isoformat()})
    await db.commit()
    url = f"{settings.PUBLIC_ORIGIN}{DEVICE_LINK_PATH}/{raw}"
    return {"enrollment": serialize_enrollment(enrollment), "token": raw, "url": url,
            "path": f"{DEVICE_LINK_PATH}/{raw}", "qr": _qr(url), "replaced": replaced,
            "expires_in_minutes": DEVICE_LINK_TTL_MINUTES}


@router.get("/device-link")
async def live_device_link(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """The live link for this person, without its token — so the dash can say one is out there."""
    rows = (await db.execute(select(DeviceEnrollment).where(
        DeviceEnrollment.user_id == user.id, DeviceEnrollment.status == "pending")
        .order_by(DeviceEnrollment.created_at.desc()))).scalars().all()
    live = [e for e in rows if not e.expires_at or e.expires_at > datetime.now(timezone.utc)]
    for e in rows:
        if e not in live:
            e.status = "expired"
    await db.commit()
    return {"enrollment": serialize_enrollment(live[0]) if live else None}


@router.delete("/device-link")
async def cancel_device_link(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    cancelled = await revoke_device_links(db, user.id, "cancelled by the account holder")
    if cancelled:
        _record(db, user, "Cancelled the add-a-device link", state="revoked", details={"enrollment_ids": cancelled})
    await db.commit()
    return {"ok": True, "cancelled": cancelled}


@router.get("/device-link/{token}")
async def device_link_preview(token: str, db: AsyncSession = Depends(get_db)):
    """Opened on the second device, before the passkey prompt: whose account, and until when."""
    enrollment = await _enrollment_by_token(db, token)
    owner = await db.get(User, enrollment.user_id) if enrollment else None
    await db.commit()  # an expiry settled by the lookup
    if enrollment is None or owner is None or owner.status != "active":
        raise HTTPException(404, "this add-a-device link has expired or was already used")
    return {"display_name": owner.display_name or owner.handle, "label": enrollment.label,
            "expires_at": enrollment.expires_at.isoformat()}


def invite_expiry(days: int = 7) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)
