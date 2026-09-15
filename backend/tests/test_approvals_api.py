"""Approvals API over the exact-approval protocol: queue, detail, approve/decline/edit, review deep link (C06)."""
from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import func, select

from backend.app.domain.commands import REGISTRY, command, dispatch
from backend.app.models.runtime import ActivityEntry, Approval, Event
from backend.app.services import approvals as approvals_svc
from backend.tests.conftest import ctx_for, login, run_worker_once


class ApiSendIn(BaseModel):
    to: str
    body: str


if "apitest.send" not in REGISTRY:
    @command("apitest.send", input=ApiSendIn, perm="inbox.send", action_class="consequential", approval_kind="send_message",
             summary=lambda p: f"Send to {p.to}", limits=lambda p: {"recipients": [p.to]},
             consequence=lambda p: {"targets": {"recipients": [p.to]}, "moves_money": False})
    async def _send(ctx, inp):
        act = await approvals_svc.intend_external_action(ctx, command_name="apitest.send", payload=inp.model_dump(),
                                                         dedupe_key=f"apitest.send:{inp.to}:{inp.body}", provider="test")
        return {"external_action_id": act.id}

    @approvals_svc.executor("apitest.send")
    async def _exec(db, act):
        return {"provider_ref": f"msg-{act.id[:6]}", "sent": True}


async def _pending(db, owner, to: str, body: str) -> Approval:
    res = await dispatch(ctx_for(db, owner, kind="agent"), "apitest.send", {"to": to, "body": body})
    assert res.status == "needs_review"
    return await db.get(Approval, res.approval_id)


async def _snapshot(db, approval_id: str) -> tuple:
    a = await db.get(Approval, approval_id)
    await db.refresh(a)
    n_act = await db.scalar(select(func.count()).select_from(ActivityEntry))
    n_ev = await db.scalar(select(func.count()).select_from(Event))
    n_appr = await db.scalar(select(func.count()).select_from(Approval))
    return (a.status, a.approval_version, a.payload_hash, a.authorized_by, a.decided_at, n_act, n_ev, n_appr)


async def test_pending_list_detail_and_summary(db, client, owner, manager):
    a = await _pending(db, owner, "list@example.com", "hello")
    login(client, owner)
    r = await client.get("/api/approvals")
    assert r.status_code == 200
    items = {x["id"]: x for x in r.json()["items"]}
    assert a.id in items and items[a.id]["status"] == "pending" and items[a.id]["targets"]["recipients"] == ["list@example.com"]
    assert r.json()["total"] >= 1 and all(x["status"] == "pending" for x in r.json()["items"])
    r = await client.get("/api/approvals?status=declined,expired&kind=send_message")
    assert a.id not in {x["id"] for x in r.json()["items"]}
    assert (await client.get("/api/approvals?status=bogus")).status_code == 422
    r = await client.get(f"/api/approvals?entity_kind=none&entity_id=none")
    assert r.json()["total"] == 0
    r = await client.get("/api/approvals/summary")
    assert r.status_code == 200 and r.json()["pending"] >= 1 and set(r.json()) >= {"pending", "executing", "unknown"}
    r = await client.get(f"/api/approvals/{a.id}")
    d = r.json()
    assert d["payload"] == {"to": "list@example.com", "body": "hello"} and d["payload_hash"] == a.payload_hash
    assert d["version"] == 1 and d["can_decide"] is True and d["checks"] and d["action_class"] == "consequential"
    assert d["invalidation"]["invalidated"] is False and d["external_action"] is None
    # non-owner: generic 403 everywhere
    login(client, manager)
    for path in ("/api/approvals", "/api/approvals/summary", f"/api/approvals/{a.id}", f"/api/approvals/{a.id}/review"):
        r = await client.get(path)
        assert r.status_code == 403 and r.json() == {"detail": "not allowed"}, path
    r = await client.post(f"/api/approvals/{a.id}/approve", json={"expected_version": 1})
    assert r.status_code == 403
    assert (await _snapshot(db, a.id))[0] == "pending"


async def test_approve_requires_exact_version_then_executes_with_receipt(db, client, owner):
    a = await _pending(db, owner, "approve@example.com", "go")
    login(client, owner)
    r = await client.post(f"/api/approvals/{a.id}/approve", json={"expected_version": 99})
    assert r.status_code == 409 and r.json()["error"] == "conflict" and r.json()["current_version"] == 1
    r = await client.post(f"/api/approvals/{a.id}/approve", json={})
    assert r.status_code == 422  # expected_version is required
    await db.refresh(a)
    assert a.status == "pending"
    r = await client.post(f"/api/approvals/{a.id}/approve", json={"expected_version": 1, "note": "ok to send"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok" and body["data"]["executed"] is True and body["data"]["approval"]["status"] == "queued"
    r = await client.get(f"/api/approvals/{a.id}")
    d = r.json()
    assert d["status"] == "queued" and d["authorized_by"] == owner.id and d["external_action"]["state"] == "intent"
    await run_worker_once()
    r = await client.get(f"/api/approvals/{a.id}")
    d = r.json()
    assert d["status"] == "confirmed" and d["receipt"]["provider_ref"].startswith("msg-") and d["external_action"]["state"] == "confirmed"
    r = await client.get("/api/approvals?status=confirmed")
    assert a.id in {x["id"] for x in r.json()["items"]}
    # approving again is refused: not pending
    r = await client.post(f"/api/approvals/{a.id}/approve", json={"expected_version": 1})
    assert r.status_code == 409 and r.json()["error"] == "blocked"


async def test_decline(db, client, owner):
    a = await _pending(db, owner, "decline@example.com", "no")
    login(client, owner)
    r = await client.post(f"/api/approvals/{a.id}/decline", json={"expected_version": 2})
    assert r.status_code == 409
    r = await client.post(f"/api/approvals/{a.id}/decline", json={"expected_version": 1, "note": "wrong wording"})
    assert r.status_code == 200 and r.json()["data"]["approval"]["status"] == "declined"
    r = await client.get(f"/api/approvals/{a.id}")
    assert r.json()["status"] == "declined" and r.json()["decision_note"] == "wrong wording" and r.json()["can_decide"] is False
    r = await client.get("/api/approvals")
    assert a.id not in {x["id"] for x in r.json()["items"]}


async def test_edit_creates_new_version_and_invalidates_old(db, client, owner):
    a = await _pending(db, owner, "edit@example.com", "v1 text")
    login(client, owner)
    r = await client.post(f"/api/approvals/{a.id}/edit", json={"payload": {"body": "v2 text"}})
    assert r.status_code == 200, r.text
    new = r.json()["data"]["approval"]
    old = r.json()["data"]["invalidated"]
    assert new["version"] == 2 and new["payload"]["body"] == "v2 text" and new["supersedes_id"] == a.id and new["status"] == "pending"
    assert old["id"] == a.id and old["status"] == "invalidated" and "review again" in old["invalidated_reason"]
    r = await client.get(f"/api/approvals/{a.id}")
    assert r.json()["status"] == "invalidated" and r.json()["superseded_by_id"] == new["id"] and r.json()["invalidation"]["invalidated"]
    r = await client.get(f"/api/approvals/{new['id']}")
    assert r.json()["previous_version"]["id"] == a.id and r.json()["payload_hash"] != a.payload_hash
    # the old version can no longer be approved; the new one needs its own exact version
    r = await client.post(f"/api/approvals/{a.id}/approve", json={"expected_version": 1})
    assert r.status_code == 409
    r = await client.post(f"/api/approvals/{new['id']}/approve", json={"expected_version": 1})
    assert r.status_code == 409
    r = await client.post(f"/api/approvals/{new['id']}/approve", json={"expected_version": 2})
    assert r.status_code == 200 and r.json()["data"]["approval"]["status"] == "queued"


async def test_C06_review_deep_link_never_mutates(db, client, owner):
    a = await _pending(db, owner, "scanner@example.com", "click me")
    before = await _snapshot(db, a.id)
    # an email security scanner follows the link without a session: nothing happens
    client.cookies.clear()
    for path in (f"/api/approvals/{a.id}/review?token=abc123", f"/api/approvals/{a.id}/review", f"/api/approvals/{a.id}"):
        r = await client.get(path)
        assert r.status_code == 401
    assert await _snapshot(db, a.id) == before
    # a signed-in owner opening the deep link only renders the exact detail
    login(client, owner)
    for _ in range(3):
        r = await client.get(f"/api/approvals/{a.id}/review?token=abc123")
        assert r.status_code == 200
        d = r.json()
        assert d["mutation"] == "none" and d["status"] == "pending" and d["version"] == 1
        assert d["actions"]["approve"].endswith("/approve") and d["actions"]["expected_version"] == 1
        assert d["payload"]["body"] == "click me"
    assert await _snapshot(db, a.id) == before
    # a deliberate authenticated POST is what changes state
    r = await client.post(f"/api/approvals/{a.id}/decline", json={"expected_version": 1})
    assert r.status_code == 200
    assert (await _snapshot(db, a.id))[0] == "declined"
