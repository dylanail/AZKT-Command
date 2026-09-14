"""Team & access: invitations, self-promotion denial (A01), grants/revokes and session death (A03),
grant revoked before execution invalidates queued authorization (H03), disable/enable, own prefs."""
from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select

from backend.app.auth.passkey import SESSION_COOKIE, session_token
from backend.app.core.errors import Blocked, Denied, ValidationFailed
from backend.app.domain.actors import Actor
from backend.app.domain.commands import REGISTRY, CommandContext, command, dispatch
from backend.app.domain.policy import effective_perms
from backend.app.models.auth import Invitation
from backend.app.models.runtime import ActivityEntry, Approval, CommandLog, Event, ExternalAction, Permission, WorkflowControl
from backend.app.services import approvals as approvals_svc
from backend.tests.conftest import actor_of, ctx_for, login, make_user, run_worker_once

EXECUTIONS: list[str] = []


class PartRequestIn(BaseModel):
    vendor: str
    item: str


if "teamtest.request_part" not in REGISTRY:
    @command("teamtest.request_part", input=PartRequestIn, perm="parts.request", action_class="consequential",
             approval_kind="quote_request", summary=lambda p: f"Ask {p.vendor} for {p.item}",
             limits=lambda p: {"recipients": [p.vendor]}, consequence=lambda p: {"targets": {"recipients": [p.vendor]}})
    async def _request_part(ctx, inp):
        act = await approvals_svc.intend_external_action(ctx, command_name="teamtest.request_part", payload=inp.model_dump(),
                                                         dedupe_key=f"teamtest.request_part:{inp.vendor}:{inp.item}", provider="test")
        return {"external_action_id": act.id}

    @approvals_svc.executor("teamtest.request_part")
    async def _exec_request_part(db, act):
        EXECUTIONS.append(act.id)
        return {"provider_ref": "vendor-msg-1", "sent": True}


def _use_cookie(client, token: str) -> None:
    """Present exactly one session cookie (a stale one kept from before an access change)."""
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, token, domain="testserver")


# ── A01 ──────────────────────────────────────────────────────────────────────
async def test_A01_mechanic_self_promotion_denied_by_command_and_http(db, client, owner, mechanic):
    before_role, before_scope = mechanic.role, mechanic.scope
    # direct command: blocked by policy (owner-only + missing permission), nothing written
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, mechanic), "team.update_person", {"user_id": mechanic.id, "role": "owner", "scope": "all"})
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, mechanic), "team.grant", {"user_id": mechanic.id, "perm": "costs.read"})
    # the AI Manager acting for the mechanic inherits the mechanic's grant: still blocked
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, mechanic, kind="agent"), "team.update_person", {"user_id": mechanic.id, "role": "owner"})
    # HTTP: team route is owner-only -> 403 with no sensitive detail
    login(client, mechanic)
    r = await client.post(f"/api/team/{mechanic.id}/update", json={"role": "owner", "scope": "all", "perms": {"costs.read": True}})
    assert r.status_code == 403 and r.json()["detail"] == "not allowed"
    r = await client.post(f"/api/team/{mechanic.id}/grant", json={"perm": "costs.read"})
    assert r.status_code == 403 and r.json()["detail"] == "not allowed"
    # self-service prefs endpoint refuses access keys outright
    r = await client.patch("/api/me/prefs", json={"role": "owner"})
    assert r.status_code == 403
    r = await client.patch("/api/me/prefs", json={"perms": {"costs.read": True}, "timezone": "UTC"})
    assert r.status_code == 403
    await db.refresh(mechanic)
    assert mechanic.role == before_role and mechanic.scope == before_scope and not (mechanic.perms or {})
    assert effective_perms(mechanic.role, mechanic.perms)["costs.read"] is False
    # even the owner cannot change their own role/scope/perms
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, owner), "team.update_person", {"user_id": owner.id, "role": "manager"})
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, owner), "team.grant", {"user_id": owner.id, "perm": "costs.read"})


async def test_A01_invitations_are_scoped_and_manager_sees_scoped_people(db, client, owner, mechanic):
    # owner invites a manager (scope all) and a mechanic reporting to them (scope assigned)
    r1 = await dispatch(ctx_for(db, owner), "team.invite", {"display_name": "Nadia", "email": "Nadia@Example.com", "role": "manager"})
    assert r1.status == "ok"
    inv_m = r1.data
    assert inv_m["created"] and inv_m["token"] and inv_m["accept_path"] == f"/invite/{inv_m['token']}"
    assert inv_m["invitation"]["scope"] == "all" and inv_m["invitation"]["email"] == "nadia@example.com"
    # only the hash is stored; the response is the only place the token appears
    row = await db.get(Invitation, inv_m["invitation"]["id"])
    assert row.token_hash == hashlib.sha256(inv_m["token"].encode()).hexdigest()
    assert "token" not in inv_m["invitation"] and "token_hash" not in inv_m["invitation"]
    # invitation preview (auth router) renders role/name and nothing else
    r = await client.get(f"/auth/invitation/{inv_m['token']}")
    assert r.status_code == 200 and r.json()["role"] == "manager" and r.json()["display_name"] == "Nadia"
    # retry for the same person does not mint a second token or row
    r1b = await dispatch(ctx_for(db, owner), "team.invite", {"display_name": "Nadia", "email": "nadia@example.com", "role": "manager"})
    assert r1b.data["created"] is False and r1b.data["token"] is None and r1b.data["invitation"]["id"] == inv_m["invitation"]["id"]
    # owner-locked keys cannot be granted to a non-owner via invitation overrides
    with pytest.raises(ValidationFailed):
        await dispatch(ctx_for(db, owner), "team.invite", {"display_name": "X", "email": "x@example.com", "role": "manager",
                                                            "perms": {"approve": True}})
    # simulate acceptance (the passkey ceremony creates the user from the invitation; see /auth/register)
    nadia = await make_user(db, "nadia", "manager", display_name="Nadia")
    r2 = await dispatch(ctx_for(db, owner), "team.invite", {"display_name": "Pat", "email": "pat@example.com", "role": "mechanic",
                                                             "manager_id": nadia.id})
    assert r2.data["invitation"]["scope"] == "assigned" and r2.data["invitation"]["manager_id"] == nadia.id
    pat = await make_user(db, "pat", "mechanic", display_name="Pat")
    pat.manager_id = nadia.id
    await db.commit()
    # owner list: everyone with permission detail; invitations list never carries tokens
    login(client, owner)
    r = await client.get("/api/team")
    assert r.status_code == 200 and r.json()["scope"] == "all"
    ids = {p["id"]: p for p in r.json()["items"]}
    assert pat.id in ids and "perms" in ids[pat.id] and ids[pat.id]["perms"]["costs.read"] is False
    r = await client.get("/api/team/invitations")
    assert r.status_code == 200 and r.json()["total"] >= 2
    assert all("token" not in i and "token_hash" not in i for i in r.json()["items"])
    # invited manager: scoped People list (their reports + unassigned mechanics), no permission detail
    login(client, nadia)
    r = await client.get("/api/team")
    assert r.status_code == 200 and r.json()["scope"] == "reports"
    items = r.json()["items"]
    assert all("perms" not in p and "overrides" not in p for p in items)
    assert pat.id in {p["id"] for p in items}
    assert owner.id not in {p["id"] for p in items} and nadia.id not in {p["id"] for p in items}
    assert (await client.get("/api/team/invitations")).status_code == 403
    assert (await client.post("/api/team/invite", json={"display_name": "Z", "email": "z@example.com", "role": "mechanic"})).status_code == 403
    # mechanic: no people list at all
    login(client, mechanic)
    assert (await client.get("/api/team")).status_code == 403
    # revoke the mechanic invitation: token stops working
    rv = await dispatch(ctx_for(db, owner), "team.revoke_invitation", {"invitation_id": r2.data["invitation"]["id"], "reason": "hired elsewhere"})
    assert rv.data["invitation"]["status"] == "revoked"
    assert (await client.get(f"/auth/invitation/{r2.data['token']}")).status_code == 404


# ── A03 ──────────────────────────────────────────────────────────────────────
async def test_A03_grant_and_revoke_costs_read_ends_old_sessions(db, client, owner):
    mgr = await make_user(db, "grant-mgr", "manager", display_name="Grant Mgr")
    login(client, mgr)
    old_cookie = session_token(mgr)
    r = await client.get("/api/me")
    assert r.status_code == 200
    me = r.json()
    assert me["perms"]["finance.status"] is True and me["perms"]["costs.read"] is False and me["role"] == "manager"
    assert me["pairing"]["telegram"]["status"] == "not_paired"
    # owner grants costs.read over HTTP
    login(client, owner)
    r = await client.post(f"/api/team/{mgr.id}/grant", json={"perm": "costs.read", "note": "month-end review"})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    person = r.json()["data"]["person"]
    assert person["perms"]["costs.read"] is True and person["grants"][-1]["by"] == owner.id and person["grants"][-1]["perm"] == "costs.read"
    # the old session cookie is dead (session_version bumped): cached access is cleared
    _use_cookie(client, old_cookie)
    assert (await client.get("/api/me")).status_code == 401
    await db.refresh(mgr)
    assert mgr.session_version == 2
    login(client, mgr)
    r = await client.get("/api/me")
    assert r.status_code == 200 and r.json()["perms"]["costs.read"] is True
    # only the intended key changed
    assert r.json()["perms"]["finance.write"] is False and r.json()["perms"]["approve"] is False
    # owner-locked keys still cannot be granted to a manager
    login(client, owner)
    r = await client.post(f"/api/team/{mgr.id}/grant", json={"perm": "approve"})
    assert r.status_code == 422
    # revoke clears it again and ends the session again
    second_cookie = session_token(mgr)
    r = await client.post(f"/api/team/{mgr.id}/revoke-grant", json={"perm": "costs.read"})
    assert r.status_code == 200 and r.json()["data"]["person"]["perms"]["costs.read"] is False
    _use_cookie(client, second_cookie)
    assert (await client.get("/api/me")).status_code == 401
    await db.refresh(mgr)
    assert mgr.session_version == 3 and mgr.perms.get("costs.read") is False
    evs = (await db.execute(select(Event).where(Event.type == "permission.revoked", Event.aggregate_id == mgr.id))).scalars().all()
    assert evs and "costs.read" in evs[-1].payload["perms"]
    # the AI Manager acting for the owner keeps the owner's finance access
    assert actor_of(owner, kind="agent").perms["costs.read"] is True
    # activity for access changes is owner-visible
    acts = (await db.execute(select(ActivityEntry).where(ActivityEntry.entity_id == mgr.id, ActivityEntry.kind == "access"))).scalars().all()
    assert acts and all(a.visibility == "owner" for a in acts)


# ── H03 ──────────────────────────────────────────────────────────────────────
async def test_H03_grant_revoked_before_execution_invalidates_queued_approval(db, owner):
    EXECUTIONS.clear()
    mgr = await make_user(db, "h03-mgr", "manager", display_name="H03 Mgr")
    res = await dispatch(ctx_for(db, mgr), "teamtest.request_part", {"vendor": "parts@vendor.example", "item": "brake pads"})
    assert res.status == "needs_review" and res.approval_id
    ok = await dispatch(ctx_for(db, owner), "approvals.approve", {"approval_id": res.approval_id, "expected_version": 1})
    assert ok.status == "ok"
    a = await db.get(Approval, res.approval_id)
    await db.refresh(a)
    assert a.status == "queued" and a.external_action_id
    # grant revoked before the worker executes: approval invalidated, intent cancelled, visible reason
    rv = await dispatch(ctx_for(db, owner), "team.revoke_grant", {"user_id": mgr.id, "perm": "parts.request"})
    assert a.id in rv.data["approvals_invalidated"]
    await db.refresh(a)
    assert a.status == "invalidated" and "parts.request" in a.invalidated_reason
    act = await db.get(ExternalAction, a.external_action_id)
    await db.refresh(act)
    assert act.state == "cancelled"
    await run_worker_once()
    await db.refresh(act)
    await db.refresh(a)
    assert act.state == "cancelled" and a.status == "invalidated" and act.id not in EXECUTIONS


async def test_disable_person_ends_sessions_and_cancels_standing_permissions(db, client, owner):
    u = await make_user(db, "disable-me", "manager", display_name="Disable Me")
    db.add(Permission(subject_kind="user", subject_id=u.id, action_pattern="teamtest.*", status="active", authorized_by=owner.id))
    await db.commit()
    login(client, u)
    assert (await client.get("/api/me")).status_code == 200
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, owner), "team.disable_person", {"user_id": owner.id})
    res = await dispatch(ctx_for(db, owner), "team.disable_person", {"user_id": u.id, "reason": "left the company"})
    assert res.data["person"]["status"] == "disabled" and res.data["standing_permissions_revoked"]
    assert (await client.get("/api/me")).status_code == 401  # disabled users get 401 everywhere
    assert (await client.get("/api/activity")).status_code == 401
    perm = (await db.execute(select(Permission).where(Permission.subject_id == u.id))).scalars().first()
    await db.refresh(perm)
    assert perm.status == "revoked"
    res = await dispatch(ctx_for(db, owner), "team.enable_person", {"user_id": u.id})
    assert res.data["person"]["status"] == "active"
    await db.refresh(u)
    login(client, u)
    assert (await client.get("/api/me")).status_code == 200


async def test_update_person_strips_locked_keys_and_bumps_session(db, owner):
    u = await make_user(db, "demote-me", "owner", display_name="Second Owner", perms={})
    u.perms = {"approve": True}
    await db.commit()
    await db.refresh(u)
    res = await dispatch(ctx_for(db, owner), "team.update_person", {"user_id": u.id, "role": "manager", "expected_version": u.version})
    p = res.data["person"]
    assert p["role"] == "manager" and p["overrides"] == {} and p["perms"]["approve"] is False and "approve" in res.data["lost_perms"]
    await db.refresh(u)
    assert u.session_version == 2 and u.version == 2
    from backend.app.core.errors import Conflict
    with pytest.raises(Conflict):
        await dispatch(ctx_for(db, owner), "team.update_person", {"user_id": u.id, "scope": "assigned", "expected_version": 1})
    with pytest.raises(ValidationFailed):
        await dispatch(ctx_for(db, owner), "team.update_person", {"user_id": u.id, "perms": {"inbox.send": True}})
    # profile-only edits do not end sessions
    res = await dispatch(ctx_for(db, owner), "team.update_person", {"user_id": u.id, "display_name": "Renamed"})
    await db.refresh(u)
    assert u.display_name == "Renamed" and u.session_version == 2


async def test_me_update_prefs(db, client, mechanic):
    login(client, mechanic)
    r = await client.patch("/api/me/prefs", json={"timezone": "Asia/Tokyo", "reminder_email": "Marco.New@Example.com",
                                                  "notification_prefs": {"channels": {"task_reminder": "telegram_fallback_email"},
                                                                         "quiet_hours": {"start": "21:00", "end": "07:00"},
                                                                         "digest_time": "7:30"}})
    assert r.status_code == 200, r.text
    prefs = r.json()["data"]["prefs"]
    assert prefs["timezone"] == "Asia/Tokyo" and prefs["reminder_email"] == "marco.new@example.com"
    assert prefs["reminder_email_verified"] is False  # unverified until a verification flow confirms it
    assert prefs["notification_prefs"]["channels"]["task_reminder"] == "telegram_fallback_email"
    assert prefs["notification_prefs"]["channels"]["overdue"] == "email_only"  # default kept
    assert prefs["notification_prefs"]["quiet_hours"] == {"start": "21:00", "end": "07:00"}
    assert prefs["notification_prefs"]["digest_time"] == "07:30"
    assert (await client.patch("/api/me/prefs", json={"timezone": "Mars/Olympus"})).status_code == 422
    assert (await client.patch("/api/me/prefs", json={"notification_prefs": {"channels": {"task_reminder": "sms"}}})).status_code == 422
    assert (await client.patch("/api/me/prefs", json={"unknown_field": 1})).status_code == 422
    r = await client.get("/api/me")
    assert r.json()["prefs"]["timezone"] == "Asia/Tokyo" and r.json()["role"] == "mechanic"


# ── invitation links: issued once, never at rest ─────────────────────────────
async def test_invite_link_is_issued_once_and_never_persisted(db, client, owner):
    login(client, owner)
    key = "invite-idem-" + owner.id[:6]
    body = {"display_name": "Link Once", "email": "linkonce@example.com", "role": "mechanic"}
    # the frontend sends Idempotency-Key on every POST command: the token must not land in the command log
    r = await client.post("/api/team/invite", json=body, headers={"Idempotency-Key": key})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    tok = d["token"]
    assert d["created"] and d["link_issued"] and tok and d["accept_path"] == f"/invite/{tok}"
    assert await db.scalar(select(func.count()).select_from(CommandLog).where(CommandLog.request_id == key)) == 0
    inv = await db.get(Invitation, d["invitation"]["id"])
    assert inv.token_hash == hashlib.sha256(tok.encode()).hexdigest()
    acts = (await db.execute(select(ActivityEntry).where(ActivityEntry.entity_id == inv.id))).scalars().all()
    assert acts and all(tok not in json.dumps(a.details) and tok not in a.what for a in acts)
    # a retry never mints a second token or a second row
    r2 = await client.post("/api/team/invite", json=body, headers={"Idempotency-Key": key})
    assert r2.status_code == 200 and r2.json()["data"]["created"] is False and r2.json()["data"]["token"] is None
    assert await db.scalar(select(func.count()).select_from(Invitation).where(Invitation.email == "linkonce@example.com")) == 1
    # reissue rotates the hash: the old link dies, the new one is returned exactly once, nothing at rest
    assert (await client.get(f"/auth/invitation/{tok}")).status_code == 200
    r3 = await client.post(f"/api/team/invitations/{inv.id}/reissue", json={"expected_version": d["invitation"]["version"]},
                           headers={"Idempotency-Key": key + "-reissue"})
    assert r3.status_code == 200, r3.text
    tok2 = r3.json()["data"]["token"]
    assert tok2 and tok2 != tok and r3.json()["data"]["link_issued"] is True
    assert (await client.get(f"/auth/invitation/{tok}")).status_code == 404
    assert (await client.get(f"/auth/invitation/{tok2}")).status_code == 200
    assert await db.scalar(select(func.count()).select_from(CommandLog).where(CommandLog.request_id == key + "-reissue")) == 0
    await db.refresh(inv)
    assert inv.token_hash == hashlib.sha256(tok2.encode()).hexdigest() and inv.version == d["invitation"]["version"] + 1
    assert "token" not in r3.json()["data"]["invitation"] and "token_hash" not in r3.json()["data"]["invitation"]


async def test_agent_proposed_invitation_needs_owner_approval_and_leaks_no_link(db, client, owner):
    # the AI Manager acting for Dylan proposes an invitation: owner-only -> exact approval, nothing created yet
    res = await dispatch(ctx_for(db, owner, kind="agent"), "team.invite", {"display_name": "Proposed", "email": "proposed@example.com", "role": "mechanic"})
    assert res.status == "needs_review" and res.approval_id
    assert await db.scalar(select(func.count()).select_from(Invitation).where(Invitation.email == "proposed@example.com")) == 0
    ok = await dispatch(ctx_for(db, owner), "approvals.approve", {"approval_id": res.approval_id, "expected_version": 1})
    assert ok.status == "ok"
    data = ok.data["result"]["data"]
    assert data["created"] is True and data["link_issued"] is False and data["token"] is None and data["accept_path"] is None
    inv = await db.get(Invitation, data["invitation"]["id"])
    assert inv.status == "pending"
    a = await db.get(Approval, res.approval_id)
    await db.refresh(a)
    assert a.status == "confirmed" and a.result["token"] is None and a.result["accept_path"] is None
    login(client, owner)
    r = await client.get(f"/api/approvals/{a.id}")
    assert r.status_code == 200 and r.json()["result"]["token"] is None and r.json()["result"]["accept_path"] is None
    # the owner issues the link directly, once
    r = await client.post(f"/api/team/invitations/{inv.id}/reissue", json={})
    assert r.status_code == 200, r.text
    tok = r.json()["data"]["token"]
    assert tok and (await client.get(f"/auth/invitation/{tok}")).status_code == 200
    # an agent cannot obtain a link even through an approval: the approval execution is refused with a reason
    res2 = await dispatch(ctx_for(db, owner, kind="agent"), "team.reissue_invitation", {"invitation_id": inv.id})
    assert res2.status == "needs_review"
    with pytest.raises(Blocked) as ei:
        await dispatch(ctx_for(db, owner), "approvals.approve", {"approval_id": res2.approval_id, "expected_version": 1})
    assert "signed-in owner" in str(ei.value)
    await db.rollback()
    a2 = await db.get(Approval, res2.approval_id)
    await db.refresh(a2)
    await db.refresh(inv)
    assert a2.status == "pending" and inv.token_hash == hashlib.sha256(tok.encode()).hexdigest()
    assert (await client.get(f"/auth/invitation/{tok}")).status_code == 200


# ── owner-only matrix ────────────────────────────────────────────────────────
async def test_owner_only_commands_refuse_every_non_owner_actor(db, owner, manager, mechanic):
    cases = [("team.invite", {"display_name": "M", "email": "m-matrix@example.com", "role": "mechanic"}),
             ("team.grant", {"user_id": mechanic.id, "perm": "costs.read"}),
             ("team.update_person", {"user_id": mechanic.id, "scope": "all"}),
             ("team.disable_person", {"user_id": mechanic.id}),
             ("team.revoke_invitation", {"invitation_id": "none"}),
             ("settings.update", {"key": "intake", "value": {"require_condition_note": False}}),
             ("settings.pause", {"key": "global", "paused": True}),
             ("settings.gate_rule_upsert", {"to_state": "in_recon", "requirement": "docs_complete"})]
    # an MCP/HTTP client acting for the owner with broad scopes: owner-only keys have no client scope at all
    ext = Actor(kind="external", user_id=owner.id, role="owner", scope="all", perms=actor_of(owner).perms, client_id="mcp-1",
                client_scopes=["read:vehicles", "write:tasks", "read:costs", "write:contacts"])
    for name, payload in cases:
        for c in (ctx_for(db, manager), ctx_for(db, mechanic), ctx_for(db, manager, kind="agent"), ctx_for(db, mechanic, kind="agent"),
                  CommandContext(db=db, actor=ext, correlation_id="matrix")):
            with pytest.raises(Denied):
                await dispatch(c, name, payload)
        # the owner's AI Manager may propose but never execute: exact owner approval, no change yet
        res = await dispatch(ctx_for(db, owner, kind="agent"), name, payload)
        assert res.status == "needs_review" and res.approval_id, name
    await db.refresh(mechanic)
    assert mechanic.scope == "assigned" and mechanic.status == "active" and not (mechanic.perms or {})
    assert await db.scalar(select(func.count()).select_from(Invitation).where(Invitation.email == "m-matrix@example.com")) == 0
    ctrl = await db.get(WorkflowControl, "global")
    assert ctrl is None or not ctrl.paused
    from backend.app.services import settings_store
    assert (await settings_store.get_effective(db, "intake"))["require_condition_note"] is True


async def test_person_detail_hides_existence_outside_scope(db, client, owner, manager, mechanic):
    login(client, mechanic)
    assert (await client.get(f"/api/team/{owner.id}")).status_code == 403
    assert (await client.get("/api/team/no-such-person")).status_code == 403
    login(client, manager)
    assert (await client.get(f"/api/team/{owner.id}")).status_code == 404  # out of scope reads as absent
    assert (await client.get("/api/team/no-such-person")).status_code == 404
    r = await client.get(f"/api/team/{manager.id}")
    assert r.status_code == 200 and "perms" not in r.json() and "grants" not in r.json()
    login(client, owner)
    r = await client.get(f"/api/team/{mechanic.id}")
    assert r.status_code == 200 and r.json()["perms"]["costs.read"] is False
