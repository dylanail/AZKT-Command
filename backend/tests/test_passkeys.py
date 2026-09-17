"""Ways to sign in (spec §11.1 — passkeys only, no passwords).

A passkey cannot move between devices, so a person who set one up on a desktop needs a second one
on their phone. These cover the whole ceremony with a software authenticator: adding a passkey on
the device you are already using, minting a one-time link for another device and spending it there,
and every way that link is supposed to die (used, expired, cancelled, replaced, access changed).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from backend.app.auth.passkey import SESSION_COOKIE
from backend.app.domain.commands import dispatch
from backend.app.models.auth import Credential, DeviceEnrollment
from backend.app.models.runtime import ActivityEntry
from backend.tests.conftest import ctx_for, login, make_user
from backend.tests.fixtures_webauthn import SoftPasskey

ORIGIN = "http://testserver"


async def fresh_client(app) -> httpx.AsyncClient:
    """A device of its own: no cookies, nothing shared with the signed-in dash."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)


async def register(client: httpx.AsyncClient, device: SoftPasskey, **body) -> httpx.Response:
    """The two-call ceremony the browser runs: options → the authenticator → verify."""
    opts = await client.post("/auth/register/options", json=body)
    assert opts.status_code == 200, opts.text
    return await client.post("/auth/register/verify", json=device.register(opts.json(), ORIGIN))


async def sign_in(client: httpx.AsyncClient, device: SoftPasskey) -> httpx.Response:
    opts = await client.post("/auth/login/options")
    assert opts.status_code == 200, opts.text
    return await client.post("/auth/login/verify", json=device.authenticate(opts.json(), ORIGIN))


async def desktop_user(db, client, handle: str) -> tuple:
    """Someone already signed in on their desktop, with the passkey they set up there.

    A manager, not an owner: passkeys are everyone's, and the reminder sweeps fan out to every
    active owner, so a test owner left behind would be counted by other files' assertions.
    """
    user = await make_user(db, handle, "manager", display_name=handle.title())
    login(client, user)
    device = SoftPasskey()
    assert (await register(client, device, label="Desktop")).status_code == 200
    return user, device


# ── the passkey you already have ─────────────────────────────────────────────
async def test_add_a_passkey_on_this_device_then_sign_in_with_it(db, client):
    user, desktop = await desktop_user(db, client, "pk-desktop")
    creds = (await client.get("/auth/credentials")).json()
    assert [c["label"] for c in creds] == ["Desktop"]
    assert creds[0]["transports"] == ["internal"] and creds[0]["last_used_at"] is None

    client.cookies.clear()
    assert (await client.get("/auth/me")).status_code == 401
    r = await sign_in(client, desktop)
    assert r.status_code == 200 and r.json()["user"]["handle"] == "pk-desktop"
    assert (await client.get("/auth/me")).json()["id"] == user.id
    assert (await client.get("/auth/credentials")).json()[0]["last_used_at"] is not None


async def test_options_exclude_the_passkeys_this_account_already_has(db, client):
    _, _desktop = await desktop_user(db, client, "pk-exclude")
    opts = (await client.post("/auth/register/options", json={"label": "again"})).json()
    existing = (await client.get("/auth/credentials")).json()
    assert len(opts["excludeCredentials"]) == len(existing) == 1  # the browser says "already registered"


async def test_adding_a_passkey_needs_a_live_session(db, client, app):
    await desktop_user(db, client, "pk-session")
    phone = await fresh_client(app)
    try:
        r = await phone.post("/auth/register/options", json={"label": "Phone"})
        assert r.status_code == 401
    finally:
        await phone.aclose()


# ── the second device ────────────────────────────────────────────────────────
async def test_add_my_phone_from_the_dash_with_a_one_time_link(db, client, app):
    user, desktop = await desktop_user(db, client, "pk-phone")

    minted = await client.post("/auth/device-link", json={"label": "iPhone"})
    assert minted.status_code == 200
    link = minted.json()
    assert link["token"] and link["url"].endswith(link["path"]) and link["path"].startswith("/add-device/")
    assert len(link["qr"]["rows"]) == len(link["qr"]["rows"][0])  # a square of modules for the dash to draw
    assert link["enrollment"]["status"] == "pending" and link["enrollment"]["label"] == "iPhone"
    assert (await client.get("/auth/device-link")).json()["enrollment"]["id"] == link["enrollment"]["id"]

    phone = await fresh_client(app)
    try:
        preview = await phone.get(f"/auth/device-link/{link['token']}")
        assert preview.status_code == 200
        assert preview.json()["display_name"] == "Pk-Phone" and preview.json()["label"] == "iPhone"

        handset = SoftPasskey(transports=["internal", "hybrid"])
        r = await register(phone, handset, device_token=link["token"])
        assert r.status_code == 200, r.text
        assert r.json()["user"]["id"] == user.id
        assert phone.cookies.get(SESSION_COOKIE)  # the phone is signed in the moment it enrolls
        assert (await phone.get("/auth/me")).json()["id"] == user.id

        # and it can sign in on its own from then on
        phone.cookies.clear()
        assert (await sign_in(phone, handset)).status_code == 200

        labelled = {c["label"]: c for c in (await phone.get("/auth/credentials")).json()}
        assert set(labelled) == {"Desktop", "iPhone"}
        assert labelled["iPhone"]["transports"] == ["internal", "hybrid"]

        # one use only: the same link cannot enrol a third device
        again = await phone.post("/auth/register/options", json={"device_token": link["token"]})
        assert again.status_code == 403 and "already used" in again.json()["detail"]
    finally:
        await phone.aclose()

    enrollment = (await db.execute(select(DeviceEnrollment).where(DeviceEnrollment.id == link["enrollment"]["id"]))).scalar_one()
    await db.refresh(enrollment)
    assert enrollment.status == "used" and enrollment.used_at and enrollment.credential_id
    assert (await client.get("/auth/device-link")).json()["enrollment"] is None
    what = {a.what for a in (await db.execute(select(ActivityEntry).where(
        ActivityEntry.actor["user_id"].as_string() == user.id))).scalars().all()}
    assert what == {"Issued an add-a-device link", 'Added passkey "Desktop"', 'Added passkey "iPhone"'}


async def test_the_link_is_stored_only_as_a_hash(db, client):
    await desktop_user(db, client, "pk-hash")
    token = (await client.post("/auth/device-link", json={})).json()["token"]
    rows = (await db.execute(select(DeviceEnrollment))).scalars().all()
    assert token not in [r.token_hash for r in rows]
    assert all(len(r.token_hash) == 64 for r in rows)


async def test_a_link_expires(db, client, app):
    await desktop_user(db, client, "pk-expiry")
    link = (await client.post("/auth/device-link", json={})).json()
    row = await db.get(DeviceEnrollment, link["enrollment"]["id"])
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()

    phone = await fresh_client(app)
    try:
        assert (await phone.get(f"/auth/device-link/{link['token']}")).status_code == 404
        r = await phone.post("/auth/register/options", json={"device_token": link["token"]})
        assert r.status_code == 403
    finally:
        await phone.aclose()
    await db.refresh(row)
    assert row.status == "expired"
    assert (await client.get("/auth/device-link")).json()["enrollment"] is None


async def test_cancelling_and_replacing_a_link(db, client, app):
    await desktop_user(db, client, "pk-cancel")
    first = (await client.post("/auth/device-link", json={})).json()
    second = (await client.post("/auth/device-link", json={})).json()
    assert second["replaced"] == [first["enrollment"]["id"]]  # one live link at a time

    phone = await fresh_client(app)
    try:
        assert (await phone.get(f"/auth/device-link/{first['token']}")).status_code == 404
        assert (await phone.get(f"/auth/device-link/{second['token']}")).status_code == 200
        cancelled = await client.delete("/auth/device-link")
        assert cancelled.json()["cancelled"] == [second["enrollment"]["id"]]
        assert (await phone.get(f"/auth/device-link/{second['token']}")).status_code == 404
    finally:
        await phone.aclose()


async def test_access_change_kills_a_live_link(db, client, owner, app):
    person = await make_user(db, "pk-disabled", "manager", display_name="Marco")
    login(client, person)
    assert (await register(client, SoftPasskey(), label="Desktop")).status_code == 200
    link = (await client.post("/auth/device-link", json={})).json()

    res = await dispatch(ctx_for(db, owner), "team.disable_person", {"user_id": person.id, "reason": "left"})
    assert res.data["device_links_revoked"] == [link["enrollment"]["id"]]

    phone = await fresh_client(app)
    try:
        assert (await phone.get(f"/auth/device-link/{link['token']}")).status_code == 404
        assert (await phone.post("/auth/register/options", json={"device_token": link["token"]})).status_code == 403
    finally:
        await phone.aclose()


async def test_minting_a_link_needs_a_session(client):
    client.cookies.clear()
    assert (await client.post("/auth/device-link", json={})).status_code == 401
    assert (await client.get("/auth/device-link")).status_code == 401
    assert (await client.delete("/auth/device-link")).status_code == 401


# ── taking one away ──────────────────────────────────────────────────────────
async def test_your_only_passkey_cannot_be_revoked(db, client, app):
    user, _desktop = await desktop_user(db, client, "pk-revoke")
    only = (await client.get("/auth/credentials")).json()
    r = await client.delete(f"/auth/credentials/{only[0]['id']}")
    assert r.status_code == 400 and "only passkey" in r.json()["detail"]

    link = (await client.post("/auth/device-link", json={"label": "iPhone"})).json()
    phone = await fresh_client(app)
    try:
        assert (await register(phone, SoftPasskey(), device_token=link["token"])).status_code == 200
    finally:
        await phone.aclose()
    assert (await client.delete(f"/auth/credentials/{only[0]['id']}")).status_code == 200
    left = (await client.get("/auth/credentials")).json()
    assert [c["label"] for c in left] == ["iPhone"]
    rows = (await db.execute(select(ActivityEntry).where(ActivityEntry.entity_id == user.id))).scalars().all()
    revoked = [a for a in rows if a.what == 'Revoked passkey "Desktop"']
    assert len(revoked) == 1 and revoked[0].details["credential_id"] == only[0]["id"]
    assert revoked[0].details["remaining"] == 1 and revoked[0].kind == "access"


async def test_a_revoked_passkey_can_no_longer_sign_in(db, client, app):
    user, desktop = await desktop_user(db, client, "pk-dead")
    link = (await client.post("/auth/device-link", json={})).json()
    phone_device = SoftPasskey()
    phone = await fresh_client(app)
    try:
        assert (await register(phone, phone_device, device_token=link["token"], label="iPhone")).status_code == 200
    finally:
        await phone.aclose()
    desktop_cred = [c for c in (await client.get("/auth/credentials")).json() if c["label"] == "Desktop"][0]
    assert (await client.delete(f"/auth/credentials/{desktop_cred['id']}")).status_code == 200

    client.cookies.clear()
    r = await sign_in(client, desktop)
    assert r.status_code == 401 and "unknown credential" in r.json()["detail"]
    assert (await sign_in(client, phone_device)).status_code == 200
    rows = (await db.execute(select(Credential).where(Credential.user_id == user.id))).scalars().all()
    assert [c.credential_id for c in rows] == [phone_device.credential_id]


async def test_renaming_a_passkey(db, client):
    await desktop_user(db, client, "pk-rename")
    cid = (await client.get("/auth/credentials")).json()[0]["id"]
    assert (await client.post(f"/auth/credentials/{cid}/rename", json={"label": "Studio iMac"})).status_code == 200
    assert (await client.get("/auth/credentials")).json()[0]["label"] == "Studio iMac"
    assert (await client.post(f"/auth/credentials/{cid}/rename", json={"label": "  "})).status_code == 400


async def test_you_cannot_touch_someone_elses_passkey(db, client, app):
    _, _ = await desktop_user(db, client, "pk-mine")
    cid = (await client.get("/auth/credentials")).json()[0]["id"]
    other = await make_user(db, "pk-theirs", "manager", display_name="Theirs")
    intruder = await fresh_client(app)
    try:
        login(intruder, other)
        assert (await intruder.delete(f"/auth/credentials/{cid}")).status_code == 404
        assert (await intruder.post(f"/auth/credentials/{cid}/rename", json={"label": "no"})).status_code == 404
    finally:
        await intruder.aclose()
