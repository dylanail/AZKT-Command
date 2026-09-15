"""Retrieval with ACL before context (spec §9.3, §11.1): records first, role-safe chunk filtering in SQL, historical
labelling, other-customer redaction, evaluation-target exclusion (G12), tombstones (A06), mechanic/external scope (A02),
current facts beat historical examples (B08)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from backend.app.domain.actors import Actor
from backend.app.domain.commands import dispatch
from backend.app.domain.policy import effective_perms
from backend.app.models.knowledge import CorpusChunk
from backend.app.models.vehicles import Vehicle
from backend.app.services import retrieval
from backend.tests.conftest import actor_of, ctx_for, login, make_user


def _u() -> str:
    return uuid.uuid4().hex[:8]


def _stk() -> str:
    return f"STK-{int(uuid.uuid4().hex[:8], 16) % 900000 + 100000}"


async def make_vehicle(db, **kw) -> Vehicle:
    stock = kw.pop("stock_no", _stk())
    v = Vehicle(stock_no=stock, title=stock, make=kw.pop("make", "Suzuki"), model=kw.pop("model", "Carry"),
                model_year=kw.pop("model_year", 1998), color=kw.pop("color", "white"), **kw)
    db.add(v)
    await db.commit()
    await db.refresh(v)
    return v


async def make_contact(db, user, name: str, email: str | None = None) -> dict:
    payload = {"name": name, "roles": ["buyer"]}
    if email:
        payload["identities"] = [{"kind": "email", "value": email}]
    return (await dispatch(ctx_for(db, user), "contacts.create", payload)).data["contact"]


async def index(db, user, **kw) -> dict:
    kw.setdefault("source_kind", "message")
    kw.setdefault("source_id", f"msg-{_u()}")
    return (await dispatch(ctx_for(db, user), "corpus.index_text", kw)).data


async def assign(db, owner, mechanic, vehicle_id: str) -> None:
    await dispatch(ctx_for(db, owner), "tasks.create", {"title": f"Inspect {vehicle_id[:6]} {_u()}", "vehicle_id": vehicle_id,
                                                        "owner_user_id": mechanic.id, "dedupe": False})


# ── B08: current facts win over an old "available" email ────────────────────
async def test_B08_current_reserved_fact_beats_historical_available_email(db, owner):
    v = await make_vehicle(db, allocation="reserved", commercial_state="reserved")
    grace = await make_contact(db, owner, f"Grace Hopper {_u()}", email=f"grace{_u()}@example.com")
    old = await index(db, owner, text=f"Hi Grace, yes the {v.stock_no} Suzuki Carry is still available. A $500 deposit holds it for you. Call me at 602-555-0199.",
                      contact_id=grace["id"], vehicle_id=v.id, happened_at=(datetime.now(timezone.utc) - timedelta(days=200)).isoformat(),
                      speaker="dylan@azkeitrucks.com")
    res = await retrieval.retrieve(db, actor_of(owner), f"Is {v.stock_no} still available and what deposit do you need?")
    facts = [f for f in res.current_facts if f["kind"] == "vehicle"]
    assert facts and facts[0]["id"] == v.id and facts[0]["states"]["commercial"] == "reserved" and facts[0]["availability"] == "reserved"
    assert facts[0]["label"] == "current" and res.resolved["vehicle_ids"] == [v.id]
    ex = [h for h in res.historical_examples if h["source_id"] == old["chunks"][0]["source_id"]]
    assert ex, res.historical_examples
    h = ex[0]
    assert h["is_historical"] is True and h["label"] == "historical" and h["trust"] == "untrusted_external" and h["authority"].startswith("none")
    # a different (unspecified) customer: identity redacted and deal terms stripped, tone retained
    assert h["other_customer"] is True and h["identity_redacted"] and h["deal_terms_stripped"]
    assert "Grace" not in h["text"] and "602-555" not in h["text"] and "$500" not in h["text"] and "still available" in h["text"]
    assert h["contact_id"] is None and h["speaker"] == "customer"
    d = res.to_dict()
    assert d["labels"]["historical_examples"].startswith("historical") and any(s["label"] == "historical" for s in d["sources"])
    # the same customer sees their own thread unredacted
    res2 = await retrieval.retrieve(db, actor_of(owner), f"deposit for {v.stock_no}", contact_id=grace["id"])
    mine = [h for h in res2.historical_examples if h["source_id"] == old["chunks"][0]["source_id"]]
    assert mine and mine[0]["other_customer"] is False and "$500" in mine[0]["text"] and mine[0]["contact_id"] == grace["id"]
    assert any(f["kind"] == "contact" and f["id"] == grace["id"] for f in res2.current_facts)


# ── G12: held-out targets and other customers' exceptions never contaminate ──
async def test_G12_eval_target_excluded_and_private_exception_scoped(db, owner):
    v = await make_vehicle(db)
    alice = await make_contact(db, owner, f"Alice Target {_u()}")
    bob = await make_contact(db, owner, f"Bob Other {_u()}")
    target_id = f"msg-target-{_u()}"
    await index(db, owner, source_id=target_id, text=f"Thanks Alice, the {v.stock_no} passes inspection on Friday and ships Monday. Deposit $750.",
                contact_id=alice["id"], vehicle_id=v.id)
    before = await retrieval.retrieve(db, actor_of(owner), f"{v.stock_no} inspection ships Monday")
    assert any(h["source_id"] == target_id for h in before.historical_examples)
    # register the message as an evaluation target: excluded from retrieval from now on
    r = await dispatch(ctx_for(db, owner), "eval.add_cases", {"set_name": f"replies-{_u()}", "cases": [
        {"input": {"question": "when does it ship?"}, "expected": {"facts": ["ships Monday"]}, "source_ref": f"message:{target_id}",
         "contact_id": alice["id"], "happened_at": "2026-05-01T00:00:00Z"}]})
    assert r.data["added"] == 1 and r.data["items"][0]["excluded_from_retrieval"] is True and r.data["items"][0]["target_ref"] == f"message:{target_id}"
    after = await retrieval.retrieve(db, actor_of(owner), f"{v.stock_no} inspection ships Monday")
    assert not any(h["source_id"] == target_id for h in after.historical_examples)
    src = await retrieval.search_sources(db, actor_of(owner), "inspection ships Monday")
    assert not any(i["source_id"] == target_id for i in src["items"])
    # Alice's sensitive exception (approved) is never returned for Bob or for an unscoped query
    exc = (await dispatch(ctx_for(db, owner), "knowledge.propose", {"kind": "exception", "title": f"Alice concession {_u()}",
                                                                     "content": "Alice pays no storage fee for 60 days and gets free Tucson delivery.",
                                                                     "scope": {"contact_id": alice["id"]}})).data["item"]
    await dispatch(ctx_for(db, owner), "knowledge.approve", {"item_id": exc["id"]})
    for_alice = await retrieval.retrieve(db, actor_of(owner), "storage fee delivery", contact_id=alice["id"])
    assert any(k["id"] == exc["id"] for k in for_alice.approved_knowledge)
    for_bob = await retrieval.retrieve(db, actor_of(owner), "storage fee delivery", contact_id=bob["id"])
    assert not any(k["id"] == exc["id"] for k in for_bob.approved_knowledge)
    assert not any(h["source_kind"] == "knowledge" for h in for_bob.historical_examples)
    leaked = str(for_bob.approved_knowledge) + str(for_bob.historical_examples) + str(for_bob.current_facts)
    assert "Alice" not in leaked and "no storage fee for 60 days" not in leaked
    unscoped = await retrieval.retrieve(db, actor_of(owner), "storage fee delivery")
    assert not any(k["id"] == exc["id"] for k in unscoped.approved_knowledge)
    # a general approved policy is returned for everyone
    pol = (await dispatch(ctx_for(db, owner), "knowledge.propose", {"kind": "policy", "title": f"Storage policy {_u()}",
                                                                     "content": "Storage fee applies after 14 days."})).data["item"]
    await dispatch(ctx_for(db, owner), "knowledge.approve", {"item_id": pol["id"]})
    for_bob2 = await retrieval.retrieve(db, actor_of(owner), "storage fee", contact_id=bob["id"])
    assert any(k["id"] == pol["id"] and k["authority"] == "policy" for k in for_bob2.approved_knowledge)


# ── A02 flavour: mechanic never sees finance / contact chunks ────────────────
async def test_A02_mechanic_retrieval_is_limited_to_assigned_vehicles_and_never_finance_or_contacts(db, owner, mechanic):
    mine = await make_vehicle(db, asking_price=9500, asking_currency="USD", purchase_amount=4200, purchase_currency="USD")
    other = await make_vehicle(db)
    await assign(db, owner, mechanic, mine.id)
    buyer = await make_contact(db, owner, f"Carla Buyer {_u()}", email=f"carla{_u()}@example.com")
    marker = f"zebra{_u()}"
    await index(db, owner, source_kind="task_note", source_id=f"t-{_u()}", text=f"Replace the {marker} tires on {mine.stock_no} before listing.", vehicle_id=mine.id)
    await index(db, owner, source_kind="task_note", source_id=f"t-{_u()}", text=f"Replace the {marker} tires on {other.stock_no} too.", vehicle_id=other.id)
    await index(db, owner, source_kind="note", source_id=f"n-{_u()}", text=f"Landed cost for {mine.stock_no} {marker} was 4200 USD.", vehicle_id=mine.id, visibility="finance")
    await index(db, owner, source_kind="note", source_id=f"n-{_u()}", text=f"Owner note {marker}: margin target 30%.", vehicle_id=mine.id, visibility="owner")
    await index(db, owner, source_kind="message", source_id=f"m-{_u()}", text=f"Carla: I want the {mine.stock_no} {marker}, my card ends 4242.",
                contact_id=buyer["id"], vehicle_id=mine.id)
    await index(db, owner, source_kind="note", source_id=f"n-{_u()}", text=f"Contact note {marker}: Carla prefers text messages.", contact_id=buyer["id"])
    res = await retrieval.retrieve(db, actor_of(mechanic), f"{marker} tires {mine.stock_no}", contact_id=buyer["id"])
    texts = [h["text"] for h in res.historical_examples]
    assert len(texts) == 1 and mine.stock_no in texts[0] and "tires" in texts[0]
    vf = [f for f in res.current_facts if f["kind"] == "vehicle"]
    assert vf and vf[0]["id"] == mine.id and vf[0].get("money_hidden") is True and "money" not in vf[0] and vf[0]["buyer_contact_id"] is None
    assert not any(f["kind"] in ("contact", "agreement", "payment", "invoice") for f in res.current_facts)
    assert res.resolved["contact_id"] is None and res.acl["costs"] is False and res.acl["contacts"] is False
    # the non-assigned vehicle is neither resolved nor searchable
    res_other = await retrieval.retrieve(db, actor_of(mechanic), f"{marker} tires {other.stock_no}")
    assert res_other.resolved["vehicle_ids"] == [] and all(other.stock_no not in h["text"] for h in res_other.historical_examples)
    src = await retrieval.search_sources(db, actor_of(mechanic), marker)
    assert len(src["items"]) == 1 and src["items"][0]["vehicle_id"] == mine.id
    assert "4200" not in str(src) and "margin" not in str(src) and "4242" not in str(src) and "Carla" not in str(src)
    # manager: operational, no costs -> sees the task notes and the customer message but not finance/owner notes
    manager = await make_user(db, f"mgr-{_u()}", "manager")
    res_m = await retrieval.retrieve(db, actor_of(manager), f"{marker} {mine.stock_no}")
    joined = " ".join(h["text"] for h in res_m.historical_examples)
    assert "tires" in joined and "4242" in joined and "4200" not in joined and "margin" not in joined
    # owner sees everything
    res_o = await retrieval.retrieve(db, actor_of(owner), f"{marker} {mine.stock_no}", limit=20)
    joined_o = " ".join(h["text"] for h in res_o.historical_examples)
    assert "4200" in joined_o and "margin" in joined_o and "4242" in joined_o


async def test_A02_external_client_sees_only_its_record_scope_and_never_personal(db, owner):
    allowed = await make_vehicle(db)
    hidden = await make_vehicle(db)
    marker = f"koala{_u()}"
    await index(db, owner, source_kind="note", source_id=f"n-{_u()}", text=f"{marker} {allowed.stock_no} arrives at the port Tuesday.", vehicle_id=allowed.id)
    await index(db, owner, source_kind="note", source_id=f"n-{_u()}", text=f"{marker} {hidden.stock_no} arrives Wednesday.", vehicle_id=hidden.id)
    await index(db, owner, source_kind="message", source_id=f"p-{_u()}", text=f"{marker} Sebastian personal thread about {allowed.stock_no}.",
                vehicle_id=allowed.id, personal_allowlisted=True, visibility="owner")
    ext = Actor(kind="external", user_id=owner.id, role="owner", perms=effective_perms("owner", {}), client_id="c-ret",
                client_scopes=["read:vehicles", "ask", "read:sources"], client_record_scope={"vehicle_ids": [allowed.id]})
    res = await retrieval.retrieve(db, ext, f"{marker} arrives", vehicle_id=hidden.id)
    assert res.resolved["vehicle_ids"] == [] and res.acl["external"] is True
    texts = [h["text"] for h in res.historical_examples]
    assert texts == [f"{marker} {allowed.stock_no} arrives at the port Tuesday."]
    res2 = await retrieval.retrieve(db, ext, f"{marker} {allowed.stock_no}", vehicle_id=allowed.id)
    assert res2.resolved["vehicle_ids"] == [allowed.id] and all("Sebastian" not in h["text"] for h in res2.historical_examples)


# ── A06: tombstone removes a chunk from retrieval immediately ───────────────
async def test_A06_tombstone_excludes_chunk_immediately(db, owner):
    marker = f"pelican{_u()}"
    sid = f"personal-{_u()}"
    await index(db, owner, source_id=sid, text=f"{marker}: unrelated participant joined; port fee details inside.", personal_allowlisted=True, visibility="owner")
    assert any(h["source_id"] == sid for h in (await retrieval.retrieve(db, actor_of(owner), f"{marker} port fee")).historical_examples)
    assert any(i["source_id"] == sid for i in (await retrieval.search_sources(db, actor_of(owner), marker))["items"])
    await dispatch(ctx_for(db, owner), "corpus.tombstone", {"source_kind": "message", "source_id": sid, "reason": "allowlist_narrowed"})
    assert not any(h["source_id"] == sid for h in (await retrieval.retrieve(db, actor_of(owner), f"{marker} port fee")).historical_examples)
    assert not any(i["source_id"] == sid for i in (await retrieval.search_sources(db, actor_of(owner), marker))["items"])
    ch = (await db.execute(select(CorpusChunk).where(CorpusChunk.source_id == sid))).scalar_one()
    assert ch.text == "" and ch.embedding is None and ch.tombstoned_at is not None


# ── ranking / labelling details ─────────────────────────────────────────────
async def test_rerank_prefers_trust_recency_and_record_match_and_dedupes(db, owner):
    v = await make_vehicle(db)
    marker = f"quokka{_u()}"
    now = datetime.now(timezone.utc)
    old = await index(db, owner, source_id=f"m-{_u()}", text=f"{marker} the truck needs an oil change.", happened_at=(now - timedelta(days=500)).isoformat())
    recent = await index(db, owner, source_id=f"m-{_u()}", text=f"{marker} the truck needs an oil change.", happened_at=(now - timedelta(days=3)).isoformat())
    note = await index(db, owner, source_kind="task_note", source_id=f"t-{_u()}", text=f"{marker} oil change done on {v.stock_no}.", vehicle_id=v.id,
                       happened_at=(now - timedelta(days=30)).isoformat())
    res = await retrieval.retrieve(db, actor_of(owner), f"{marker} oil change {v.stock_no}")
    ids = [h["chunk_id"] for h in res.historical_examples]
    # identical text is deduplicated (same content hash); the internal note tied to the resolved vehicle ranks first
    assert ids[0] == note["chunks"][0]["id"] and len([i for i in ids if i in (old["chunks"][0]["id"], recent["chunks"][0]["id"])]) == 1
    assert res.historical_examples[0]["trust"] == "internal" and res.historical_examples[0]["is_historical"] is False
    # ids extraction
    assert retrieval.extract_ids("STK-7 and stk 0412 frame HA4-1234567 bob@example.com") == {
        "stock_nos": ["STK-0007", "STK-0412"], "frame_nos": ["HA41234567"], "emails": ["bob@example.com"]}
    txt, flags = retrieval.redact_text("Grace Hopper (grace@example.com, 602-555-0199) paid a $500 deposit, 10% off.",
                                       ["Grace Hopper", "Grace"], strip_deal_terms=True)
    assert flags == {"identity_redacted": True, "deal_terms_stripped": True}
    assert "Grace" not in txt and "@" not in txt and "$500" not in txt and "10%" not in txt and "[customer]" in txt


async def test_search_api_is_acl_safe(client, db, owner, mechanic):
    v = await make_vehicle(db)
    marker = f"wombat{_u()}"
    await index(db, owner, source_kind="note", source_id=f"n-{_u()}", text=f"{marker} landed cost 4100 USD for {v.stock_no}", vehicle_id=v.id, visibility="finance")
    await index(db, owner, source_kind="task_note", source_id=f"t-{_u()}", text=f"{marker} detail the cab of {v.stock_no}", vehicle_id=v.id)
    await assign(db, owner, mechanic, v.id)
    login(client, mechanic)
    r = await client.get("/api/knowledge/search", params={"q": f"{marker} {v.stock_no}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "4100" not in r.text and any("detail the cab" in h["text"] for h in body["historical_examples"])
    assert body["current_facts"][0]["money_hidden"] is True and body["acl"]["costs"] is False
    r = await client.get("/api/knowledge/sources", params={"q": marker})
    assert r.status_code == 200 and "4100" not in r.text and r.json()["total"] == 1 and "snippet" in r.json()["items"][0]
    login(client, owner)
    r = await client.get("/api/knowledge/search", params={"q": f"{marker} {v.stock_no}"})
    assert "4100" in r.text and r.json()["current_facts"][0]["money"]["asking_price"] is None
    r = await client.get("/api/knowledge/search", params={"q": ""})
    assert r.status_code == 422


# ── A02/A05: contact-level content, owner-visibility notes and the personal allowlist ───────
async def test_A02_actors_without_contacts_read_never_receive_customer_linked_chunks(db, owner, mechanic):
    """A chunk about an assigned vehicle that is also linked to a customer is still customer material: the mechanic
    (no contacts.read) must not receive it at all, not merely a redacted copy."""
    v = await make_vehicle(db)
    await assign(db, owner, mechanic, v.id)
    buyer = await make_contact(db, owner, f"Nina Private {_u()}", email=f"nina{_u()}@example.com")
    marker = f"ocelot{_u()}"
    await index(db, owner, source_kind="note", source_id=f"n-{_u()}", text=f"{marker} torque the rear hubs on {v.stock_no}.", vehicle_id=v.id)
    await index(db, owner, source_kind="note", source_id=f"cn-{_u()}",
                text=f"{marker} the buyer asked us to call her at 602-555-0000 about {v.stock_no}; she may cancel.",
                contact_id=buyer["id"], vehicle_id=v.id)
    res = await retrieval.retrieve(db, actor_of(mechanic), f"{marker} {v.stock_no}")
    texts = [h["text"] for h in res.historical_examples]
    assert texts == [f"{marker} torque the rear hubs on {v.stock_no}."]
    assert all(h["contact_id"] is None for h in res.historical_examples)
    src = await retrieval.search_sources(db, actor_of(mechanic), marker)
    assert src["total"] == 1 and "may cancel" not in str(src) and "602-555" not in str(src)
    # the owner still sees both
    assert len((await retrieval.retrieve(db, actor_of(owner), f"{marker} {v.stock_no}", limit=20)).historical_examples) == 2


async def test_owner_visibility_notes_stay_with_the_owner_even_with_costs_read(db, owner):
    """costs.read opens finance visibility, not owner-only content (spec §11.1)."""
    v = await make_vehicle(db)
    marker = f"tapir{_u()}"
    await index(db, owner, source_kind="note", source_id=f"o-{_u()}", text=f"{marker} owner note: target margin 30% on {v.stock_no}.",
                vehicle_id=v.id, visibility="owner")
    await index(db, owner, source_kind="note", source_id=f"f-{_u()}", text=f"{marker} landed cost 4300 USD on {v.stock_no}.",
                vehicle_id=v.id, visibility="finance")
    books = await make_user(db, f"books-{_u()}", "books")  # costs.read = True
    res = await retrieval.retrieve(db, actor_of(books), f"{marker} {v.stock_no}", limit=20)
    joined = " ".join(h["text"] for h in res.historical_examples)
    assert res.acl["costs"] is True and "4300" in joined and "margin" not in joined
    assert "margin" in " ".join(h["text"] for h in (await retrieval.retrieve(db, actor_of(owner), f"{marker} {v.stock_no}", limit=20)).historical_examples)


async def test_A05_personal_allowlisted_content_is_owner_only_in_storage_and_retrieval(db, owner, manager, mechanic):
    """Storage/retrieval side of A05: the per-chunk allowlist flag is persisted and no non-owner actor — person,
    agent-for-a-person or connector — can reach allowlisted content through retrieve or sources.search.
    (Admission rules for new personal mail are the ingestion side, stage 2.)"""
    v = await make_vehicle(db)
    marker = f"gannet{_u()}"
    sid = f"personal-{_u()}"
    await index(db, owner, source_kind="message", source_id=sid, text=f"{marker} Sebastian: the port release fee is settled, unrelated to {v.stock_no}.",
                vehicle_id=v.id, personal_allowlisted=True, visibility="owner")
    ch = (await db.execute(select(CorpusChunk).where(CorpusChunk.source_id == sid))).scalar_one()
    assert ch.acl == {"visibility": "owner", "personal_allowlisted": True}
    assert any(h["source_id"] == sid for h in (await retrieval.retrieve(db, actor_of(owner), f"{marker} port release")).historical_examples)
    ext = Actor(kind="external", user_id=owner.id, role="owner", perms=effective_perms("owner", {}), client_id="c-a05",
                client_scopes=["read:vehicles", "read:sources", "read:contacts", "read:costs", "ask"],
                client_record_scope={"vehicle_ids": [v.id]})
    for who in (actor_of(manager), actor_of(manager, kind="agent"), actor_of(mechanic), ext):
        res = await retrieval.retrieve(db, who, f"{marker} port release", limit=20)
        assert not any(h["source_id"] == sid for h in res.historical_examples), who.kind
        assert "Sebastian" not in str(res.to_dict())
        assert not any(i["source_id"] == sid for i in (await retrieval.search_sources(db, who, marker))["items"])


async def test_unlinked_historical_example_still_loses_a_real_customer_identity(db, owner):
    """G12/B08: an example chunk that carries no contact link but names a real customer is still redacted — the
    contacts table, not the chunk, is the source of truth for identity."""
    marker = f"ibis{_u()}"
    c = await make_contact(db, owner, "Zelda Fitzgerald", email="zelda.fitzgerald@example.com")
    await index(db, owner, source_kind="example", source_id=f"ex-{_u()}",
                text=f"{marker} Good tone: we told Zelda Fitzgerald we would hold the truck and she paid a $400 deposit.")
    res = await retrieval.retrieve(db, actor_of(owner), f"{marker} hold the truck")
    h = [x for x in res.historical_examples if marker in x["text"]]
    assert h, res.historical_examples
    assert "Zelda" not in h[0]["text"] and "Fitzgerald" not in h[0]["text"] and "[customer]" in h[0]["text"]
    assert h[0]["identity_redacted"] is True and h[0]["deal_terms_stripped"] is True and "$400" not in h[0]["text"]
    assert "Good tone" in h[0]["text"] and h[0]["is_historical"] is True
    assert c["id"] not in str(h[0])


async def test_external_client_with_contacts_scope_cannot_name_a_customer_outside_its_records(db, owner):
    """Invariant 14: the client grant follows the read. A connector holding read:contacts still only reaches the
    customers reachable from its record scope."""
    mine = await make_vehicle(db)
    other = await make_vehicle(db)
    marker = f"civet{_u()}"
    buyer = await make_contact(db, owner, f"In Scope {_u()}", email=f"ins{_u()}@example.com")
    stranger = await make_contact(db, owner, f"Out Of Scope {_u()}", email=f"oos{_u()}@example.com")
    mine.buyer_contact_id = buyer["id"]
    other.buyer_contact_id = stranger["id"]
    await db.commit()
    await index(db, owner, source_kind="note", source_id=f"n-{_u()}", text=f"{marker} {mine.stock_no} is ready for pickup.",
                contact_id=buyer["id"], vehicle_id=mine.id)
    await index(db, owner, source_kind="note", source_id=f"n-{_u()}", text=f"{marker} {other.stock_no} is delayed.",
                contact_id=stranger["id"], vehicle_id=other.id)
    ext = Actor(kind="external", user_id=owner.id, role="owner", perms=effective_perms("owner", {}), client_id="c-scope",
                client_scopes=["read:vehicles", "read:contacts", "read:sources", "ask"],
                client_record_scope={"vehicle_ids": [mine.id]})
    res = await retrieval.retrieve(db, ext, marker, contact_id=stranger["id"])
    assert res.resolved["contact_id"] is None and res.acl["record_scoped_contacts"] is True
    assert not any(f["kind"] == "contact" for f in res.current_facts)
    assert [h["text"] for h in res.historical_examples] == [f"{marker} {mine.stock_no} is ready for pickup."]
    ok = await retrieval.retrieve(db, ext, marker, contact_id=buyer["id"])
    assert ok.resolved["contact_id"] == buyer["id"] and any(f["kind"] == "contact" for f in ok.current_facts)
