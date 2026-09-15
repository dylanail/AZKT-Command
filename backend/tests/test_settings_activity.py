"""Settings (allowlist, gate rules, pause controls; H05) and the activity feed (A02 visibility/scope)."""
from __future__ import annotations

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from backend.app.core.errors import Denied
from backend.app.domain.commands import REGISTRY, command, dispatch
from backend.app.models.runtime import ActivityEntry, WorkflowControl
from backend.app.models.vehicles import ShopGateRule
from backend.app.services import settings_store
from backend.tests.conftest import ctx_for, login


class FinanceNoteIn(BaseModel):
    text: str
    amount: str = "1234.56"


if "acttest.finance_note" not in REGISTRY:
    @command("acttest.finance_note", input=FinanceNoteIn, perm="finance.write", action_class="internal")
    async def _finance_note(ctx, inp):
        ctx.record(f"finance: {inp.text}", entity_kind="cost_item", entity_id="ci-1", kind="payment", visibility="finance",
                   details={"amount": inp.amount, "currency": "USD", "vendor": "Acme"},
                   receipt={"provider_ref": "sq-1", "total_amount": inp.amount}, sources=[{"kind": "ledger", "ref": "row-9"}])
        ctx.record(f"finance-all: {inp.text}", entity_kind="cost_item", entity_id="ci-1", kind="payment", visibility="all",
                   details={"amount": inp.amount, "note": "public note"}, exception=True)
        return {"ok": True}


# ── settings ─────────────────────────────────────────────────────────────────
async def test_settings_defaults_visible_to_owner_only(db, client, owner, manager, mechanic):
    login(client, owner)
    r = await client.get("/api/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    eff = body["effective"]
    assert eff["reminders"]["digest"]["local_time"] == "08:00" and eff["reminders"]["digest"]["timezone"] == "America/Phoenix"
    assert eff["reminders"]["overdue"]["delay_minutes"] == 60 and eff["reminders"]["deposit_confirmed"]["owner_only"] is True
    assert eff["automation"]["parts_cap"] is None and eff["automation"]["spend_caps"]["per_action"] is None
    assert isinstance(body["connections"], list) and isinstance(body["gate_rules"], list)
    assert body["controls"][0]["key"] == "global"
    assert "external_actions" in body["outstanding"] and "approvals" in body["outstanding"]
    for u in (manager, mechanic):
        login(client, u)
        for path in ("/api/settings", "/api/settings/reminders", "/api/settings/pause", "/api/settings/gate-rules"):
            r = await client.get(path)
            assert r.status_code == 403 and r.json() == {"detail": "not allowed"}, path
        r = await client.put("/api/settings/reminders", json={"value": {"overdue": {"delay_minutes": 5}}})
        assert r.status_code == 403
        r = await client.post("/api/settings/pause", json={"key": "global", "paused": True})
        assert r.status_code == 403
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, manager), "settings.update", {"key": "reminders", "value": {"overdue": {"delay_minutes": 5}}})


async def test_settings_update_is_validated_and_versioned(db, client, owner):
    login(client, owner)
    r = await client.put("/api/settings/reminders", json={"value": {"overdue": {"delay_minutes": 90}, "channels": {"digest": "telegram_email"}}})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["value"]["overdue"]["delay_minutes"] == 90 and d["value"]["channels"]["digest"] == "telegram_email"
    assert d["value"]["digest"]["local_time"] == "08:00"  # untouched defaults stay merged
    v = d["version"]
    r = await client.get("/api/settings/reminders")
    assert r.json()["version"] == v and r.json()["recorded"] is True and r.json()["updated_by"] == owner.id
    # stale version -> 409; unknown key -> 422; bad shape -> 422
    r = await client.put("/api/settings/reminders", json={"value": {"overdue": {"delay_minutes": 30}}, "expected_version": v + 5})
    assert r.status_code == 409
    r = await client.put("/api/settings/not-a-key", json={"value": {}})
    assert r.status_code == 422
    r = await client.put("/api/settings/reminders", json={"value": {"digest": {"local_time": "25:99"}}})
    assert r.status_code == 422
    r = await client.put("/api/settings/reminders", json={"value": {"channels": {"digest": "carrier_pigeon"}}})
    assert r.status_code == 422
    r = await client.put("/api/settings/automation", json={"value": {"parts_cap": "abc"}})
    assert r.status_code == 422
    r = await client.put("/api/settings/automation", json={"value": {"parts_cap": "250.00", "spend_caps": {"currency": "usd"}}})
    assert r.status_code == 200 and r.json()["data"]["value"]["parts_cap"] == "250.00" and r.json()["data"]["value"]["spend_caps"]["currency"] == "USD"
    # readers use the effective view
    assert (await settings_store.get_effective(db, "automation"))["parts_cap"] == "250.00"
    assert (await settings_store.get_effective(db, "reminders"))["overdue"]["delay_minutes"] == 90


async def test_gate_rules_defaults_and_crud(db, client, owner):
    login(client, owner)
    r = await client.get("/api/settings/gate-rules?to_state=ready_for_sale")
    assert r.status_code == 200
    rules = {x["requirement"]: x for x in r.json()["items"]}
    if all(x["source"] == "default" for x in rules.values()):
        assert rules["photos_min"]["param"] == {"min": 6} and rules["photos_min"]["overridable"] is False
        assert set(rules) == {"photos_min", "recon_verified", "disclosures_written"}
    r = await client.get("/api/settings/gate-rules?to_state=in_recon")
    assert {x["requirement"] for x in r.json()["items"]} >= {"inspection_logged"}
    # factual gate cannot be made overridable
    r = await client.post("/api/settings/gate-rules", json={"to_state": "ready_for_sale", "requirement": "recon_verified", "overridable": True})
    assert r.status_code == 422
    r = await client.post("/api/settings/gate-rules", json={"to_state": "ready_for_sale", "requirement": "photos_min", "param": {"min": 0}})
    assert r.status_code == 422
    # raise the photo minimum: persists that rule and materializes the other defaults for the state
    r = await client.post("/api/settings/gate-rules", json={"to_state": "ready_for_sale", "requirement": "photos_min", "param": {"min": 8}})
    assert r.status_code == 200, r.text
    rule = r.json()["data"]["rule"]
    assert rule["param"] == {"min": 8} and rule["source"] == "persisted"
    eff = await settings_store.effective_gate_rules(db, "ready_for_sale")
    by = {x["requirement"]: x for x in eff}
    assert by["photos_min"]["param"] == {"min": 8} and by["recon_verified"]["source"] == "persisted" and by["disclosures_written"]["active"]
    # deactivate then reset restores defaults
    r = await client.post(f"/api/settings/gate-rules/{rule['id']}/deactivate", json={"expected_version": rule["version"]})
    assert r.status_code == 200
    assert "photos_min" not in {x["requirement"] for x in await settings_store.effective_gate_rules(db, "ready_for_sale")}
    r = await client.post("/api/settings/gate-rules/reset", json={"to_state": "ready_for_sale"})
    assert r.status_code == 200
    by = {x["requirement"]: x for x in await settings_store.effective_gate_rules(db, "ready_for_sale")}
    assert by["photos_min"]["param"] == {"min": 6} and by["photos_min"]["active"] is True
    rows = (await db.execute(select(ShopGateRule).where(ShopGateRule.to_state == "ready_for_sale", ShopGateRule.requirement == "photos_min"))).scalars().all()
    assert len(rows) == 1  # reset never duplicates rows


# ── H05 ──────────────────────────────────────────────────────────────────────
async def test_H05_global_pause_blocks_agents_not_humans(db, client, owner):
    login(client, owner)
    try:
        r = await client.post("/api/settings/pause", json={"key": "global", "paused": True, "reason": "vendor outage"})
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["control"]["paused"] is True and d["control"]["reason"] == "vendor outage"
        assert "pending" in d["outstanding"]["external_actions"] and "unknown" in d["outstanding"]["external_actions"]
        r = await client.get("/api/settings/pause")
        assert r.json()["any_paused"] is True and any(c["key"] == "global" and c["paused"] for c in r.json()["items"])
        # an automated actor's internal command is blocked at the policy boundary
        with pytest.raises(Denied) as ei:
            await dispatch(ctx_for(db, owner, kind="agent"), "tasks.create", {"title": "Paused agent task"})
        assert "paused" in str(ei.value)
        # a human keeps working
        res = await dispatch(ctx_for(db, owner), "tasks.create", {"title": "Human task during pause"})
        assert res.status == "ok"
        # invalid keys are refused
        r = await client.post("/api/settings/pause", json={"key": "everything", "paused": True})
        assert r.status_code == 422
        # per-workflow and per-thread switches are separate rows
        r = await client.post("/api/settings/pause", json={"key": "thread:conv-1", "paused": True, "reason": "taken over"})
        assert r.status_code == 200
        ctrl = await db.get(WorkflowControl, "thread:conv-1")
        assert ctrl.paused and ctrl.changed_by == owner.id
    finally:
        r = await client.post("/api/settings/pause", json={"key": "global", "paused": False, "reason": "resolved"})
        assert r.status_code == 200 and r.json()["data"]["control"]["paused"] is False
        await client.post("/api/settings/pause", json={"key": "thread:conv-1", "paused": False})
    # resumed: the agent works again; resume revalidates (event) rather than replaying anything
    res = await dispatch(ctx_for(db, owner, kind="agent"), "tasks.create", {"title": "Agent task after resume"})
    assert res.status == "ok"
    from backend.app.models.runtime import Event
    evs = (await db.execute(select(Event).where(Event.type == "workflow.resumed", Event.aggregate_id == "global"))).scalars().all()
    assert evs and evs[-1].payload["revalidate"] is True
    acts = (await db.execute(select(ActivityEntry).where(ActivityEntry.entity_kind == "workflow_control", ActivityEntry.entity_id == "global"))).scalars().all()
    assert acts and any(a.exception and a.state == "paused" for a in acts) and any(a.state == "active" for a in acts)


# ── A02 ──────────────────────────────────────────────────────────────────────
async def test_A02_activity_visibility_and_record_scope(db, client, owner, manager, mechanic):
    # owner-visible (team) + finance-visible + all-visible entries
    inv_id = (await dispatch(ctx_for(db, owner), "team.invite", {"display_name": "Vis Test", "email": "vis@example.com", "role": "mechanic"})).data["invitation"]["id"]
    await dispatch(ctx_for(db, owner), "acttest.finance_note", {"text": "paid vendor"})
    # a task assigned to the mechanic: its activity is on the mechanic's record scope
    t = await dispatch(ctx_for(db, owner), "tasks.create", {"title": "Rotate tires on the mechanic's truck", "owner_user_id": mechanic.id,
                                                            "vehicle_id": None})
    task_id = t.data["task"]["id"]
    fin_hidden = (await db.execute(select(ActivityEntry).where(ActivityEntry.what == "finance: paid vendor"))).scalars().first()
    fin_all = (await db.execute(select(ActivityEntry).where(ActivityEntry.what == "finance-all: paid vendor"))).scalars().first()
    owner_entry = (await db.execute(select(ActivityEntry).where(ActivityEntry.entity_kind == "invitation", ActivityEntry.entity_id == inv_id))).scalars().first()
    assert owner_entry.visibility == "owner"
    # owner sees everything, including money and receipts in detail only (targeted filters: the feed may hold
    # thousands of rows from other work, and the page is capped at 500)
    login(client, owner)
    r = await client.get("/api/activity?entity_kind=cost_item&entity_id=ci-1&limit=500")
    assert r.status_code == 200
    ids = {e["id"] for e in r.json()["items"]}
    assert {fin_hidden.id, fin_all.id} <= ids and r.json()["money_hidden"] is False
    assert all("receipt" not in e and "sources" not in e for e in r.json()["items"])
    r = await client.get(f"/api/activity?entity_kind=invitation&entity_id={inv_id}")
    assert owner_entry.id in {e["id"] for e in r.json()["items"]}
    r = await client.get(f"/api/activity/{fin_hidden.id}")
    assert r.json()["details"]["amount"] == "1234.56" and r.json()["receipt"]["provider_ref"] == "sq-1" and r.json()["sources"]
    # mechanic: 200, but only entries on assigned tasks/vehicles or own actions; never owner/finance rows; no money
    login(client, mechanic)
    r = await client.get("/api/activity?limit=500")
    assert r.status_code == 200
    items = r.json()["items"]
    ids = {e["id"] for e in items}
    assert fin_hidden.id not in ids and owner_entry.id not in ids and fin_all.id not in ids
    assert all(e["visibility"] == "all" for e in items) and r.json()["scoped"] is True
    for path in ("/api/activity?entity_kind=cost_item&entity_id=ci-1", f"/api/activity?entity_kind=invitation&entity_id={inv_id}"):
        assert (await client.get(path)).json()["total"] == 0, path
    r = await client.get(f"/api/activity?entity_kind=task&entity_id={task_id}")
    assert r.json()["total"] >= 1 and all(e["entity_id"] == task_id for e in r.json()["items"])
    r = await client.get(f"/api/activity/{fin_hidden.id}")
    assert r.status_code == 404 and "1234" not in r.text
    r = await client.get(f"/api/activity/{fin_all.id}")
    assert r.status_code == 404 and "1234" not in r.text  # outside the mechanic's record scope
    r = await client.get(f"/api/activity/{owner_entry.id}")
    assert r.status_code == 404
    # owner-only routes: generic 403, no sensitive detail
    for path in ("/api/approvals", "/api/approvals/summary", "/api/settings", "/api/team/invitations"):
        r = await client.get(path)
        assert r.status_code == 403 and r.json() == {"detail": "not allowed"}, path
    # manager (activity.read, no costs.read): sees the all-visible finance row with money scrubbed, never the finance-visible one
    login(client, manager)
    r = await client.get("/api/activity?entity_kind=cost_item&entity_id=ci-1&limit=500")
    ids = {e["id"] for e in r.json()["items"]}
    assert fin_all.id in ids and fin_hidden.id not in ids and r.json()["money_hidden"] is True
    assert (await client.get(f"/api/activity?entity_kind=invitation&entity_id={inv_id}")).json()["total"] == 0
    r = await client.get(f"/api/activity/{fin_all.id}")
    assert r.status_code == 200
    d = r.json()
    assert d["details"]["amount"] is None and d["details"]["money_hidden"] is True and d["details"]["note"] == "public note"
    assert "1234" not in r.text
    assert (await client.get(f"/api/activity/{fin_hidden.id}")).status_code == 404
    # the owner's AI Manager keeps owner visibility
    assert (await client.get("/api/approvals")).status_code == 403


async def test_activity_filters(db, client, owner, mechanic):
    await dispatch(ctx_for(db, owner), "tasks.report_blocker", {"task_id": (await dispatch(ctx_for(db, owner), "tasks.create",
                                                                              {"title": "Filter me", "dedupe": False})).data["task"]["id"],
                                                                "reason": "waiting on parts"})
    login(client, owner)
    r = await client.get("/api/activity?exceptions_only=true&kind=task&limit=500")
    assert r.status_code == 200 and r.json()["items"] and all(e["exception"] and e["kind"] == "task" for e in r.json()["items"])
    r = await client.get(f"/api/activity?actor={owner.id}&limit=5")
    assert all(e["actor"]["user_id"] == owner.id for e in r.json()["items"]) and r.json()["total"] >= len(r.json()["items"])
    r = await client.get("/api/activity?entity_kind=task&q=Filter me")
    assert r.json()["total"] >= 2
    r = await client.get("/api/activity?since=2099-01-01T00:00:00Z")
    assert r.json()["total"] == 0 and r.json()["items"] == []
    r = await client.get("/api/activity?kind=access")
    assert all(e["kind"] == "access" for e in r.json()["items"])
    assert (await client.get("/api/activity/does-not-exist")).status_code == 404


async def test_activity_unknown_visibility_fails_closed_for_non_owners(db, client, owner, manager, mechanic):
    """A visibility label this build does not know is owner-only, and list and detail agree."""
    ctx = ctx_for(db, owner)
    e = ctx.record("Labelled with a visibility this build does not know", entity_kind="vehicle", entity_id="v-unknown-vis",
                   kind="system", visibility="internal", details={"note": "owner-only until classified"})
    await db.commit()
    login(client, owner)
    r = await client.get("/api/activity?q=visibility this build&limit=500")
    assert e.id in {x["id"] for x in r.json()["items"]}
    assert (await client.get(f"/api/activity/{e.id}")).status_code == 200
    for u in (manager, mechanic):
        login(client, u)
        r = await client.get("/api/activity?q=visibility this build&limit=500")
        assert r.status_code == 200 and e.id not in {x["id"] for x in r.json()["items"]}
        assert (await client.get(f"/api/activity/{e.id}")).status_code == 404


async def test_activity_money_scrub_keeps_references(db, client, owner, manager):
    from backend.app.routers.activity import scrub_money
    out = scrub_money({"amount": "12.50", "cost_item_id": "ci-9", "payment_state": "paid", "fee_kind": "port",
                       "nested": {"total_amount": "1", "vendor_name": "Acme"}, "items": [{"price": "3"}]})
    assert out["amount"] is None and out["money_hidden"] is True
    assert out["cost_item_id"] == "ci-9" and out["payment_state"] == "paid" and out["fee_kind"] == "port"
    assert out["nested"]["total_amount"] is None and out["nested"]["vendor_name"] == "Acme" and out["items"][0]["price"] is None


async def test_gate_rule_edit_cannot_collide_with_another_rule(db, client, owner):
    login(client, owner)
    made = []
    try:
        r1 = await client.post("/api/settings/gate-rules", json={"to_state": "finalization", "requirement": "docs_complete"})
        r2 = await client.post("/api/settings/gate-rules", json={"to_state": "finalization", "requirement": "inspection_logged"})
        assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
        made = [r1.json()["data"]["rule"], r2.json()["data"]["rule"]]
        r3 = await client.post("/api/settings/gate-rules", json={"rule_id": made[1]["id"], "to_state": "finalization", "requirement": "docs_complete"})
        assert r3.status_code == 409 and r3.json()["error"] == "conflict" and r3.json()["rule_id"] == made[0]["id"]
        rows = (await db.execute(select(ShopGateRule).where(ShopGateRule.to_state == "finalization", ShopGateRule.requirement == "docs_complete"))).scalars().all()
        assert len(rows) == 1
        # the same upsert repeated (no rule_id) updates in place rather than adding a row
        r4 = await client.post("/api/settings/gate-rules", json={"to_state": "finalization", "requirement": "docs_complete", "label": "Documents complete"})
        assert r4.status_code == 200 and r4.json()["data"]["created"] is False
        rows = (await db.execute(select(ShopGateRule).where(ShopGateRule.to_state == "finalization", ShopGateRule.requirement == "docs_complete")
                                 .execution_options(populate_existing=True))).scalars().all()
        assert len(rows) == 1 and rows[0].label == "Documents complete"
    finally:
        # leave finalization ungated for the rest of the suite
        for rule in made:
            await client.post(f"/api/settings/gate-rules/{rule['id']}/deactivate", json={})
    db.expire_all()  # the app committed through its own session; drop this session's cached rows
    assert not [x for x in await settings_store.effective_gate_rules(db, "finalization") if x["active"]]


async def test_health_reports_ephemeral_photo_storage_on_railway(client, owner, monkeypatch):
    """With local storage and no mounted volume, Railway throws every uploaded photo away on the next
    deploy. That only surfaces later as a listing whose approved photos have no bytes to publish, so
    the health page has to say it plainly."""
    from backend.app.services import health as health_svc
    login(client, owner)

    r = await client.get("/api/health")
    assert r.status_code == 200
    base = r.json()["storage"]
    assert base["platform"] == "host" and base["durable"] is True and base["writable"] is True

    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.delenv(health_svc.RAILWAY_VOLUME_ENV, raising=False)
    r = await client.get("/api/health")
    body = r.json()
    assert body["storage"]["platform"] == "railway" and body["storage"]["durable"] is False
    assert "volume" in body["storage"]["reason"] and body["ok"] is False

    # a mounted volume that DATA_DIR actually sits inside is durable again
    from backend.app.core.config import settings as cfg
    monkeypatch.setenv(health_svc.RAILWAY_VOLUME_ENV, cfg.DATA_DIR)
    r = await client.get("/api/health")
    assert r.json()["storage"]["durable"] is True

    # so is a configured bucket, whatever the platform
    monkeypatch.setattr(cfg, "STORAGE_BACKEND", "s3")
    monkeypatch.setattr(cfg, "S3_BUCKET", "azkt-assets")
    assert health_svc.storage_state() == {"backend": "s3", "durable": True, "writable": None,
                                          "reason": None, "platform": "railway", "bucket": "azkt-assets"}
    monkeypatch.setattr(cfg, "S3_BUCKET", "")
    assert health_svc.storage_state()["durable"] is False
