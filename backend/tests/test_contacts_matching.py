"""Contacts + identity matching: normalization, extraction (B05), independent contact/vehicle matching (B04),
dedupe, reviewed merges with an undoable snapshot, archive/restore, and the contacts API."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from backend.app.core.errors import Blocked, Denied
from backend.app.domain.commands import dispatch
from backend.app.models.contacts import Contact, ContactIdentity, ContactMerge
from backend.app.models.runtime import Approval, Event
from backend.app.models.sales import Opportunity
from backend.app.models.tasks import Task
from backend.app.models.vehicles import Vehicle
from backend.app.services import matching as m
from backend.tests.conftest import ctx_for, login


def _u() -> str:
    return uuid.uuid4().hex[:8]


def _stk(suffix: str = "") -> str:
    """Realistic stock number: STK-<digits> (matching extracts digit-only stock refs)."""
    return f"STK-{int(uuid.uuid4().hex[:8], 16) % 900000 + 100000}{suffix}"


async def make_vehicle(db, stock_no: str, *, frame: str | None = None, **kw) -> Vehicle:
    v = Vehicle(stock_no=stock_no, frame_no_raw=frame, frame_no_norm=m.normalize_frame(frame), title=kw.pop("title", stock_no),
                make=kw.pop("make", "Suzuki"), model=kw.pop("model", "Carry"), model_year=kw.pop("model_year", 1999),
                color=kw.pop("color", "white"), **kw)
    db.add(v)
    await db.commit()
    await db.refresh(v)
    return v


async def create_contact(db, user, **payload):
    res = await dispatch(ctx_for(db, user), "contacts.create", payload)
    return res.data["contact"]


# ── normalization (spec §3.3) ───────────────────────────────────────────────
def test_phone_normalizes_to_e164_with_country_default():
    assert m.normalize_phone("(602) 555-0142") == "+16025550142"
    assert m.normalize_phone("1-602-555-0142") == "+16025550142"
    assert m.normalize_phone("+81 90-1234-5678") == "+819012345678"
    assert m.normalize_phone("090-1234-5678", "JP") == "+819012345678"
    assert m.normalize_phone("602-555-0142 ext 12") == "+16025550142"
    assert m.normalize_phone("12345") is None


def test_email_lowercases_but_keeps_dots_and_plus_aliases():
    assert m.normalize_email(" John.Doe+azkt@Gmail.com ") == "john.doe+azkt@gmail.com"
    assert m.normalize_email("john.doe+azkt@gmail.com") != m.normalize_email("johndoe@gmail.com")
    assert m.normalize_email("not-an-email") is None


def test_frame_keeps_raw_and_normalizes_search_form():
    from backend.app.services.vehicles import normalize_frame as vehicles_frame, normalize_stock_no as vehicles_stock
    raw = "da63t-00123456"
    assert m.normalize_frame(raw) == "DA63T00123456" == vehicles_frame(raw)   # leading zeros kept, punctuation stripped
    assert m.normalize_stock_no("stk 0412") == "STK-0412"
    # matching must produce the same canonical stock reference the vehicles domain stores, or "STK-412"
    # in a message would never find the vehicle saved as STK-0412
    for raw_stock in ("STK-412", "stk 412", "STK0412", "stk-0412", "STK-123456"):
        assert m.normalize_stock_no(raw_stock) == vehicles_stock(raw_stock)
    assert m.normalize_handle("@AZ_Kei") == "az_kei"


# ── B05: message-level extraction ───────────────────────────────────────────
def test_B05_supplier_email_yields_separate_items_for_three_vehicles_and_two_invoices():
    text = ("Hi Dylan, we loaded STK-0412, STK-0413 and stk-0501 today. Invoice INV-2024-117 covers the first two "
            "(USD 1,250.00); invoice no. 88231 covers the third at $640. Frame DA63T-123456 is the white one. "
            "Call +1 (602) 555-0142 or ops@exporter.jp")
    items = m.extract_items(text)
    stocks = [i for i in items if i["kind"] == "stock_no"]
    invoices = [i for i in items if i["kind"] == "invoice_no"]
    assert [s["norm"] for s in stocks] == ["STK-0412", "STK-0413", "STK-0501"]
    assert [i["norm"] for i in invoices] == ["INV-2024-117", "88231"]
    assert [i["norm"] for i in items if i["kind"] == "frame_no"] == ["DA63T123456"]
    amounts = [i["norm"] for i in items if i["kind"] == "amount"]
    assert {"amount": "1250.00", "currency": "USD"} in amounts and {"amount": "640", "currency": "USD"} in amounts
    assert [i["norm"] for i in items if i["kind"] == "email"] == ["ops@exporter.jp"]
    assert [i["norm"] for i in items if i["kind"] == "phone"] == ["+16025550142"]
    spans = [tuple(i["span"]) for i in items]
    assert spans == sorted(spans) and len(set(spans)) == len(spans)   # deterministic, non-overlapping
    assert m.extract_items(text) == items and m.extract_items("") == []
    # words after "invoice" and dates/times are not references: unknown stays unknown rather than invented
    noise = m.extract_items("Please send the invoice for the trucks by 2026-09-14 10:30; STK-412 is ready.")
    assert [i["kind"] for i in noise] == ["stock_no"] and noise[0]["norm"] == "STK-0412"


# ── B04: contact and vehicle matching are independent ──────────────────────
async def test_B04_two_contacts_same_name_ambiguous_and_vehicle_match_independent(db, owner):
    tag = _u()
    a = await create_contact(db, owner, name=f"Ken Tanaka {tag}", identities=[{"kind": "email", "value": f"ken.{tag}@example.com"}])
    b = await create_contact(db, owner, name=f"Ken Tanaka {tag}", identities=[{"kind": "email", "value": f"tanaka.{tag}@example.org"}])
    by_name = await m.resolve_contact(db, name=f"Ken Tanaka {tag}")
    assert by_name.state == "ambiguous" and by_name.contact_id is None
    assert {c["contact_id"] for c in by_name.candidates} == {a["id"], b["id"]}
    by_email = await m.resolve_contact(db, email=f"KEN.{tag}@example.com", name=f"Ken Tanaka {tag}")
    assert by_email.state == "matched" and by_email.contact_id == a["id"]
    assert any("verified email" in r for r in by_email.reasons)
    # one customer discussing two vehicles: vehicle resolution does not depend on who is asking
    num = int(tag[:5], 16) % 90000 + 10000
    v1 = await make_vehicle(db, _stk("1"), frame=f"DA63T-1{num}")
    v2 = await make_vehicle(db, _stk("2"), frame=f"DA63T-2{num}")
    r1 = await m.resolve_vehicle(db, stock_no=v1.stock_no)
    r2 = await m.resolve_vehicle(db, frame_no=v2.frame_no_raw.lower())
    assert r1.state == "matched" and r1.vehicle_id == v1.id
    assert r2.state == "matched" and r2.vehicle_id == v2.id
    assert r1.entity_kind == "vehicle" and by_email.entity_kind == "contact"
    # a reference found only in free text needs corroboration before it auto-links
    text_only = await m.resolve_vehicle(db, text=f"question about {v1.stock_no} please")
    assert text_only.state == "proposed" and text_only.vehicle_id == v1.id
    corroborated = await m.resolve_vehicle(db, text=f"{v1.stock_no} frame {v1.frame_no_raw}")
    assert corroborated.state == "matched" and corroborated.vehicle_id == v1.id
    assert (await m.resolve_vehicle(db, text="nothing here")).state == "unmatched"


async def test_unverified_alias_is_proposed_and_provider_ref_is_matched(db, owner):
    tag = _u()
    c = await create_contact(db, owner, name=f"Provisional {tag}", source="inbox", status="provisional",
                             identities=[{"kind": "email", "value": f"p.{tag}@example.com", "source": "inbox"},
                                         {"kind": "provider", "value": f"square:customer:{tag}", "verified": True}])
    r = await m.resolve_contact(db, email=f"p.{tag}@example.com")
    assert r.state == "proposed" and r.contact_id == c["id"] and any("not verified" in x for x in r.reasons)
    r2 = await m.resolve_contact(db, provider_ref=f"square:customer:{tag}")
    assert r2.state == "matched" and r2.contact_id == c["id"]
    # authorized rules that disagree -> ambiguous, never a silent pick
    other = await create_contact(db, owner, name=f"Other {tag}", identities=[{"kind": "phone", "value": f"602555{tag[:4].encode().hex()[:4]}"}])
    r3 = await m.resolve_contact(db, provider_ref=f"square:customer:{tag}", phone=other["primary_phone"])
    assert r3.state == "ambiguous"


async def test_thread_mapping_matches_only_with_consistent_participants(db, owner):
    tag = _u()
    c = await create_contact(db, owner, name=f"Thread {tag}", identities=[{"kind": "email", "value": f"t.{tag}@example.com"}])
    ok = await m.resolve_contact(db, thread_mapping={"contact_id": c["id"], "participants": [f"t.{tag}@example.com"]},
                                 email=f"t.{tag}@example.com")
    assert ok.state == "matched"
    newcomer = await m.resolve_contact(db, thread_mapping={"contact_id": c["id"], "participants": [f"t.{tag}@example.com"]},
                                       email=f"someone-else.{tag}@example.net")
    assert newcomer.state == "proposed" and any("new participant" in r for r in newcomer.reasons)


# ── create / dedupe ─────────────────────────────────────────────────────────
async def test_create_dedupes_on_identity_and_source_ref_and_maintains_search_text(db, owner):
    tag = _u()
    first = await dispatch(ctx_for(db, owner), "contacts.create", {
        "name": f"Maria Lopez {tag}", "company": "Desert Haulers", "roles": ["carrier", "vendor"], "source_ref": f"msg-{tag}",
        "identities": [{"kind": "phone", "value": "(602) 555-0199"}, {"kind": "email", "value": f"Maria.{tag}@Haulers.com"}]})
    assert first.data["created"] is True
    c = first.data["contact"]
    assert c["primary_phone"] == "+16025550199" and c["primary_email"] == f"maria.{tag}@haulers.com"
    row = await db.get(Contact, c["id"])
    assert "desert haulers" in row.search_text and "+16025550199" in row.search_text and "carrier" in row.search_text
    again = await dispatch(ctx_for(db, owner), "contacts.create", {"name": "Maria L", "source_ref": f"msg-{tag}"})
    assert again.data["created"] is False and again.data["contact"]["id"] == c["id"]
    by_identity = await dispatch(ctx_for(db, owner), "contacts.create", {
        "name": "M. Lopez", "identities": [{"kind": "email", "value": f"MARIA.{tag}@haulers.com"}]})
    assert by_identity.data["created"] is False and by_identity.data["contact"]["id"] == c["id"]
    forced = await dispatch(ctx_for(db, owner), "contacts.create", {
        "name": "Maria's sister", "force_new": True, "identities": [{"kind": "phone", "value": "602-555-0199"}]})
    assert forced.data["created"] is True and forced.data["contact"]["id"] != c["id"]
    # the shared phone now belongs to two people: matching reports ambiguity instead of guessing
    r = await m.resolve_contact(db, phone="+1 602 555 0199")
    assert r.state == "ambiguous"
    # idempotent request key: a retried create never duplicates rows
    c1 = ctx_for(db, owner, request_id=f"req-{tag}")
    a = await dispatch(c1, "contacts.create", {"name": f"Retry {tag}", "identities": [{"kind": "email", "value": f"r.{tag}@x.io"}]})
    c2 = ctx_for(db, owner, request_id=f"req-{tag}")
    b = await dispatch(c2, "contacts.create", {"name": f"Retry {tag}", "identities": [{"kind": "email", "value": f"r.{tag}@x.io"}]})
    assert a.data["contact"]["id"] == b.data["contact"]["id"]
    n = (await db.execute(select(Contact).where(Contact.name == f"Retry {tag}"))).scalars().all()
    assert len(n) == 1


async def test_add_and_remove_identity_are_idempotent(db, owner):
    tag = _u()
    c = await create_contact(db, owner, name=f"Ident {tag}")
    r1 = await dispatch(ctx_for(db, owner), "contacts.add_identity", {"contact_id": c["id"], "kind": "telegram", "value": f"@Kei_{tag}"})
    r2 = await dispatch(ctx_for(db, owner), "contacts.add_identity", {"contact_id": c["id"], "kind": "telegram", "value": f"kei_{tag}"})
    assert r1.data["created"] is True and r2.data["created"] is False and r2.data["identity"]["id"] == r1.data["identity"]["id"]
    assert r1.data["identity"]["value_norm"] == f"kei_{tag}" and r1.data["identity"]["value"] == f"@Kei_{tag}"
    rem = await dispatch(ctx_for(db, owner), "contacts.remove_identity", {"contact_id": c["id"], "identity_id": r1.data["identity"]["id"]})
    assert rem.data["contact"]["identities"] == []
    with pytest.raises(Exception):
        await dispatch(ctx_for(db, owner), "contacts.add_identity", {"contact_id": c["id"], "kind": "email", "value": "nope"})


# ── merge with review; undoable ─────────────────────────────────────────────
async def test_manager_merge_propose_needs_review_and_owner_merge_is_direct(db, owner, manager):
    tag = _u()
    a = await create_contact(db, owner, name=f"Dup A {tag}", identities=[{"kind": "email", "value": f"a.{tag}@x.io"}])
    b = await create_contact(db, owner, name=f"Dup B {tag}", identities=[{"kind": "email", "value": f"b.{tag}@x.io"}])
    res = await dispatch(ctx_for(db, manager), "contacts.merge_propose", {"survivor_id": a["id"], "merged_id": b["id"], "reason": "same person"})
    assert res.status == "needs_review" and res.approval_id
    ap = await db.get(Approval, res.approval_id)
    assert ap.kind == "contact_merge" and ap.status == "pending"
    rb = await db.get(Contact, b["id"])
    assert rb.status == "active"  # nothing merged yet
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, manager), "contacts.merge", {"survivor_id": a["id"], "merged_id": b["id"]})
    ok = await dispatch(ctx_for(db, owner), "approvals.approve", {"approval_id": ap.id})
    assert ok.status == "ok" and ok.data["executed"] is True
    await db.refresh(rb)
    assert rb.status == "merged" and rb.merged_into_id == a["id"]
    await db.refresh(ap)
    assert ap.status == "confirmed"
    rec = (await db.execute(select(ContactMerge).where(ContactMerge.merged_id == b["id"]))).scalar_one()
    assert rec.approval_id == ap.id


async def test_merge_unmerge_round_trip_preserves_aliases_and_links(db, owner, manager):
    tag = _u()
    a = await create_contact(db, owner, name=f"Survivor {tag}", company="ACME", roles=["buyer"],
                             identities=[{"kind": "email", "value": f"s.{tag}@x.io"}], consent={"email": True})
    # b shares one alias with a: creating it needs force_new (otherwise create dedupes to a, which is correct)
    b = await create_contact(db, owner, name=f"Merged {tag}", company="ACME Ltd", roles=["vendor"], notes="likes blue",
                             identities=[{"kind": "email", "value": f"m.{tag}@x.io"}, {"kind": "email", "value": f"S.{tag}@x.io"}],
                             consent={"email": False}, force_new=True)
    assert b["id"] != a["id"]
    opp = (await dispatch(ctx_for(db, owner), "sales.create_opportunity", {"contact_id": b["id"], "pipeline": "irq", "enquiry": f"merge {tag}"})).data["opportunity"]
    task = (await dispatch(ctx_for(db, owner), "tasks.create", {"title": f"call merged {tag}", "type": "call", "contact_id": b["id"], "dedupe": False})).data["task"]
    res = await dispatch(ctx_for(db, owner), "contacts.merge", {"survivor_id": a["id"], "merged_id": b["id"], "reason": "duplicate"})
    s = res.data["survivor"]
    assert res.data["merge"]["id"]
    assert any(al["name"] == f"Merged {tag}" and al["company"] == "ACME Ltd" for al in s["aliases"])
    assert set(s["roles"]) == {"buyer", "vendor"} and "likes blue" in s["notes"] and s["consent"]["email"] is False
    norms = {i["value_norm"] for i in s["identities"]}
    assert norms == {f"s.{tag}@x.io", f"m.{tag}@x.io"}   # duplicate alias not doubled, other alias moved
    assert (await db.get(Opportunity, opp["id"])).contact_id == a["id"]
    assert (await db.get(Task, task["id"])).contact_id == a["id"]
    assert (await m.resolve_contact(db, email=f"m.{tag}@x.io")).contact_id == a["id"]
    evs = (await db.execute(select(Event).where(Event.type == "identity.match_resolved", Event.aggregate_id == a["id"]))).scalars().all()
    assert evs and evs[-1].payload["merged_id"] == b["id"]
    # managers cannot unmerge; owner restores exactly
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, manager), "contacts.unmerge", {"merge_id": res.data["merge"]["id"]})
    un = await dispatch(ctx_for(db, owner), "contacts.unmerge", {"merge_id": res.data["merge"]["id"], "reason": "not the same"})
    assert un.data["reverted"] is True
    s2, m2 = un.data["survivor"], un.data["merged"]
    assert s2["aliases"] == [] and s2["roles"] == ["buyer"] and s2["notes"] == "" and s2["consent"] == {"email": True}
    assert m2["status"] == "active" and m2["merged_into_id"] is None and m2["roles"] == ["vendor"]
    assert {i["value_norm"] for i in s2["identities"]} == {f"s.{tag}@x.io"}
    assert {i["value_norm"] for i in m2["identities"]} == {f"m.{tag}@x.io", f"s.{tag}@x.io"}
    assert (await db.get(Opportunity, opp["id"])).contact_id == b["id"]
    assert (await db.get(Task, task["id"])).contact_id == b["id"]
    again = await dispatch(ctx_for(db, owner), "contacts.unmerge", {"merge_id": res.data["merge"]["id"]})
    assert again.data["reverted"] is False   # idempotent
    # an archived contact cannot be merged until it is restored
    await dispatch(ctx_for(db, owner), "contacts.archive", {"contact_id": b["id"]})
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "contacts.merge", {"survivor_id": a["id"], "merged_id": b["id"]})


async def test_archive_restore_and_provisional(db, owner):
    tag = _u()
    c = await create_contact(db, owner, name=f"Arch {tag}")
    up = await dispatch(ctx_for(db, owner), "contacts.update", {"contact_id": c["id"], "expected_version": c["version"],
                                                                 "company": "Kei Co", "roles": ["vendor"], "consent": {"sms": False}})
    assert up.data["contact"]["version"] == c["version"] + 1 and up.data["contact"]["company"] == "Kei Co"
    assert "kei co" in (await db.get(Contact, c["id"])).search_text
    ar = await dispatch(ctx_for(db, owner), "contacts.archive", {"contact_id": c["id"], "reason": "spam"})
    assert ar.data["contact"]["status"] == "archived" and ar.data["contact"]["archived_at"]
    assert (await m.resolve_contact(db, name=f"Arch {tag}")).state == "unmatched"
    rs = await dispatch(ctx_for(db, owner), "contacts.restore", {"contact_id": c["id"]})
    assert rs.data["contact"]["status"] == "active"
    pv = await dispatch(ctx_for(db, owner), "contacts.mark_provisional", {"contact_id": c["id"], "reason": "unknown sender"})
    assert pv.data["contact"]["status"] == "provisional" and pv.data["contact"]["provisional_reason"] == "unknown sender"
    cf = await dispatch(ctx_for(db, owner), "contacts.mark_provisional", {"contact_id": c["id"], "provisional": False})
    assert cf.data["contact"]["status"] == "active" and cf.data["contact"]["verified"] is True


# ── API ─────────────────────────────────────────────────────────────────────
async def test_contacts_api_list_detail_resolve_and_permissions(client, db, owner, manager, mechanic):
    tag = _u()
    login(client, owner)
    r = await client.post("/api/contacts", json={"name": f"Api Buyer {tag}", "roles": ["buyer"],
                                                 "identities": [{"kind": "email", "value": f"api.{tag}@x.io"}]})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    cid = r.json()["data"]["contact"]["id"]
    r = await client.post("/api/contacts", json={"name": f"Api Carrier {tag}", "roles": ["carrier"]})
    carrier_id = r.json()["data"]["contact"]["id"]
    r = await client.get("/api/contacts", params={"tab": "buyers", "q": tag})
    assert r.status_code == 200 and r.json()["total"] == 1 and r.json()["items"][0]["id"] == cid
    r = await client.get("/api/contacts", params={"tab": "carriers", "q": tag})
    assert r.json()["total"] == 1 and r.json()["items"][0]["roles"] == ["carrier"]
    r = await client.post("/api/sales/opportunities", json={"contact_id": cid, "pipeline": "irq", "enquiry": "any kei truck",
                                                              "budget_amount": "9000", "budget_currency": "USD"})
    assert r.status_code == 200, r.text
    r = await client.get(f"/api/contacts/{cid}")
    body = r.json()
    assert body["contact"]["identities"][0]["value_norm"] == f"api.{tag}@x.io"
    assert body["opportunities"][0]["budget_amount"] == "9000.00" and body["merge_history"] == []
    r = await client.get("/api/contacts/resolve", params={"email": f"API.{tag}@x.io"})
    assert r.status_code == 200 and r.json()["state"] == "matched" and r.json()["decision"] == "Allowed"
    # manager: sees the contact but not money; may resolve
    login(client, manager)
    r = await client.get(f"/api/contacts/{cid}")
    assert r.status_code == 200 and r.json()["opportunities"][0]["budget_amount"] is None
    assert r.json()["opportunities"][0]["money_hidden"] is True
    r = await client.get("/api/contacts/resolve", params={"name": f"Api Buyer {tag}"})
    assert r.status_code == 200 and r.json()["state"] == "proposed"
    r = await client.post(f"/api/contacts/{cid}/merge-propose", json={"merged_id": carrier_id})
    assert r.status_code == 200 and r.json()["status"] == "needs_review"
    r = await client.post(f"/api/contacts/{cid}/merge-propose", json={"merged_id": cid})   # self-merge is invalid input
    assert r.status_code == 422
    # mechanic: no contacts.read at all; denial carries no detail
    login(client, mechanic)
    r = await client.get("/api/contacts")
    assert r.status_code == 403
    r = await client.get(f"/api/contacts/{cid}")
    assert r.status_code == 403 and tag not in r.text
    r = await client.get("/api/contacts/resolve", params={"email": f"api.{tag}@x.io"})
    assert r.status_code == 403


# ── owner-only merge across actor kinds; unverified aliases never auto-link automation ─────────
async def test_self_merge_rejected_and_merge_is_owner_only_for_every_actor_kind(db, owner, manager, mechanic):
    from backend.app.core.errors import ValidationFailed
    from backend.app.domain.actors import Actor
    from backend.app.domain.policy import effective_perms
    from backend.app.domain.commands import CommandContext
    tag = _u()
    a = await create_contact(db, owner, name=f"Solo A {tag}")
    b = await create_contact(db, owner, name=f"Solo B {tag}")
    for name in ("contacts.merge", "contacts.merge_propose"):
        with pytest.raises(ValidationFailed):
            await dispatch(ctx_for(db, owner), name, {"survivor_id": a["id"], "merged_id": a["id"]})
    assert (await db.execute(select(Approval).where(Approval.command_name == "contacts.merge_propose",
                                                    Approval.entity_id == a["id"]))).scalars().all() == []
    payload = {"survivor_id": a["id"], "merged_id": b["id"], "reason": "same"}
    # the AI Manager acting for the owner may only prepare it for exact approval
    agent = await dispatch(ctx_for(db, owner, kind="agent"), "contacts.merge", payload)
    assert agent.status == "needs_review" and agent.approval_id
    # an external client with the owner's grant and write:contacts scope is still blocked (owner-only action)
    ext = Actor(kind="external", user_id=owner.id, role="owner", scope="all", perms=effective_perms("owner", {}),
                client_id=f"client-{tag}", client_scopes=["read:contacts", "write:contacts"])
    with pytest.raises(Denied):
        await dispatch(CommandContext(db=db, actor=ext, correlation_id="test", channel="mcp"), "contacts.merge", payload)
    for person in (manager, mechanic):
        with pytest.raises(Denied):
            await dispatch(ctx_for(db, person), "contacts.merge", payload)
        with pytest.raises(Denied):
            await dispatch(ctx_for(db, person), "contacts.unmerge", {"merge_id": "none"})
    for row_id in (a["id"], b["id"]):
        assert (await db.get(Contact, row_id)).status == "active"   # nothing merged by any of the above


async def test_create_with_unverified_alias_blocks_automation_but_people_may_link(db, owner):
    tag = _u()
    email = f"seen.once.{tag}@example.com"
    # an alias observed in an ingested mail is unverified
    a = await create_contact(db, owner, name=f"Seen Once {tag}", source="inbox",
                             identities=[{"kind": "email", "value": email, "source": "inbox"}])
    assert a["identities"][0]["verified"] is False
    # automation (the Manager ingesting a new sender) may not silently attach the sender to that person
    with pytest.raises(Blocked) as ei:
        await dispatch(ctx_for(db, owner, kind="agent"), "contacts.create",
                       {"name": f"New Sender {tag}", "source": "inbox", "identities": [{"kind": "email", "value": email}]})
    assert ei.value.detail["match_state"] == "proposed" and ei.value.detail["candidates"][0]["contact_id"] == a["id"]
    assert (await db.execute(select(Contact).where(Contact.name == f"New Sender {tag}"))).scalars().all() == []
    # a signed-in person typing the same alias is an authorized link
    human = await dispatch(ctx_for(db, owner), "contacts.create",
                           {"name": f"New Sender {tag}", "identities": [{"kind": "email", "value": email}]})
    assert human.data["created"] is False and human.data["contact"]["id"] == a["id"]
    # once the owner verifies the alias, automation may reuse the contact through it
    await dispatch(ctx_for(db, owner), "contacts.add_identity", {"contact_id": a["id"], "kind": "email", "value": email, "verified": True})
    auto = await dispatch(ctx_for(db, owner, kind="agent"), "contacts.create",
                          {"name": f"New Sender {tag}", "source": "inbox", "identities": [{"kind": "email", "value": email}]})
    assert auto.data["created"] is False and auto.data["contact"]["id"] == a["id"]
    # a provisional survivor absorbing a confirmed person becomes active; unmerge restores it
    prov = await create_contact(db, owner, name=f"Prov {tag}", status="provisional", provisional_reason="unknown sender")
    res = await dispatch(ctx_for(db, owner), "contacts.merge", {"survivor_id": prov["id"], "merged_id": a["id"]})
    assert res.data["survivor"]["status"] == "active" and res.data["survivor"]["provisional_reason"] is None
    un = await dispatch(ctx_for(db, owner), "contacts.unmerge", {"merge_id": res.data["merge"]["id"]})
    assert un.data["survivor"]["status"] == "provisional" and un.data["survivor"]["provisional_reason"] == "unknown sender"
