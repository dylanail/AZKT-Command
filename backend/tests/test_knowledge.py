"""Knowledge items (spec §9.1, §9.4), corpus manifests/chunks/tombstones (spec §9.2, A06 storage side), learning from
corrections (G11) and the /api/knowledge, /api/teach surfaces."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from backend.app.core.errors import Blocked, Denied, ValidationFailed
from backend.app.domain.commands import dispatch
from backend.app.models.knowledge import CorpusChunk, KnowledgeItem, WorkflowOutcome
from backend.app.models.runtime import ActivityEntry, Event, Permission
from backend.app.services import knowledge as ksvc
from backend.app.services import learning
from backend.tests.conftest import ctx_for, login


def _u() -> str:
    return uuid.uuid4().hex[:8]


async def create_contact(db, user, **payload) -> dict:
    res = await dispatch(ctx_for(db, user), "contacts.create", {"name": payload.pop("name", f"Buyer {_u()}"), **payload})
    return res.data["contact"]


# ── knowledge items ──────────────────────────────────────────────────────────
async def test_propose_is_retry_safe_and_starts_proposed(db, owner):
    key = f"kp-{_u()}"
    a = await dispatch(ctx_for(db, owner), "knowledge.propose", {"kind": "policy", "title": "Deposit holds 7 days",
                                                                 "content": "A deposit holds a truck for 7 days.", "dedupe_key": key})
    b = await dispatch(ctx_for(db, owner), "knowledge.propose", {"kind": "policy", "title": "Deposit holds 7 days",
                                                                 "content": "A deposit holds a truck for 7 days.", "dedupe_key": key})
    assert a.data["created"] is True and b.data["created"] is False and a.data["item"]["id"] == b.data["item"]["id"]
    item = a.data["item"]
    assert item["status"] == "proposed" and item["requires_owner"] is True and item["usable_as"] == ["policy"] and item["is_general"]
    with pytest.raises(ValidationFailed):
        await dispatch(ctx_for(db, owner), "knowledge.propose", {"kind": "exception", "title": "x", "content": "y"})  # needs contact scope
    with pytest.raises(ValidationFailed):
        await dispatch(ctx_for(db, owner), "knowledge.propose", {"kind": "rule", "title": "x", "content": "y"})


async def test_policy_and_exception_need_owner_and_never_touch_permissions(db, owner, manager):
    c = await create_contact(db, owner, name=f"Grace {_u()}")
    before = (await db.execute(select(Permission))).scalars().all()
    pol = (await dispatch(ctx_for(db, owner), "knowledge.propose", {"kind": "policy", "title": f"Policy {_u()}", "content": "Never quote landed cost before inspection."})).data["item"]
    exc = (await dispatch(ctx_for(db, owner), "knowledge.propose", {"kind": "exception", "title": f"Exception {_u()}", "content": "Free delivery within Phoenix.",
                                                                     "scope": {"contact_id": c["id"]}})).data["item"]
    # manager: cannot propose (no knowledge.write) and cannot approve (owner_only)
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, manager), "knowledge.propose", {"kind": "style", "title": "short", "content": "keep it short"})
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, manager), "knowledge.approve", {"item_id": pol["id"]})
    # owner-agent: owner decision required -> needs_review (exact approval), not silently applied
    res = await dispatch(ctx_for(db, owner, kind="agent"), "knowledge.approve", {"item_id": pol["id"]})
    assert res.status == "needs_review"
    ok = await dispatch(ctx_for(db, owner), "knowledge.approve", {"item_id": pol["id"], "expected_version": pol["version"]})
    assert ok.data["item"]["status"] == "approved" and ok.data["permission_change"] is False and ok.data["item"]["approved_by"] == owner.id
    ok2 = await dispatch(ctx_for(db, owner), "knowledge.approve", {"item_id": exc["id"]})
    assert ok2.data["item"]["status"] == "approved" and ok2.data["item"]["scope"] == {"contact_id": c["id"]}
    # approving indexed approved-trust chunks for retrieval
    chunks = (await db.execute(select(CorpusChunk).where(CorpusChunk.source_kind == "knowledge", CorpusChunk.source_id == pol["id"]))).scalars().all()
    assert chunks and all(ch.trust == "approved" and ch.is_historical is False and ch.kind == "policy" for ch in chunks)
    after = (await db.execute(select(Permission))).scalars().all()
    assert len(after) == len(before)  # no grant changed
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "knowledge.approve", {"item_id": pol["id"]})  # already approved
    evs = (await db.execute(select(Event).where(Event.type == "knowledge.changed", Event.aggregate_id == pol["id"]))).scalars().all()
    assert {e.payload["status"] for e in evs} == {"proposed", "approved"}


async def test_retire_and_reject_keep_history_and_leave_current_policy_retrieval(db, owner):
    pol = (await dispatch(ctx_for(db, owner), "knowledge.propose", {"kind": "policy", "title": f"Retire me {_u()}", "content": "Old rule about crates."})).data["item"]
    await dispatch(ctx_for(db, owner), "knowledge.approve", {"item_id": pol["id"]})
    r = await dispatch(ctx_for(db, owner), "knowledge.retire", {"item_id": pol["id"], "reason": "superseded"})
    assert r.data["item"]["status"] == "retired" and r.data["item"]["retired_reason"] == "superseded"
    chunks = (await db.execute(select(CorpusChunk).where(CorpusChunk.source_kind == "knowledge", CorpusChunk.source_id == pol["id"]))).scalars().all()
    assert chunks and all(ch.tombstoned_at is not None and ch.text == "" for ch in chunks)
    prop = (await dispatch(ctx_for(db, owner), "knowledge.propose", {"kind": "style", "title": f"Reject me {_u()}", "content": "Use emojis."})).data["item"]
    rj = await dispatch(ctx_for(db, owner), "knowledge.reject", {"item_id": prop["id"], "reason": "not our voice"})
    assert rj.data["item"]["status"] == "rejected" and rj.data["item"]["rejected_reason"] == "not our voice"
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "knowledge.approve", {"item_id": prop["id"]})


async def test_factual_lesson_needs_evidence_before_approval(db, owner):
    k = (await dispatch(ctx_for(db, owner), "knowledge.propose", {"kind": "lesson", "title": "ETA corrected", "content": "ETA is Oct 3", "classification": "factual",
                                                                   "scope": {"vehicle_id": "veh-x"}})).data["item"]
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "knowledge.approve", {"item_id": k["id"]})
    k2 = (await dispatch(ctx_for(db, owner), "knowledge.propose", {"kind": "lesson", "title": "ETA corrected (sourced)", "content": "ETA is Oct 3",
                                                                    "classification": "factual", "scope": {"vehicle_id": "veh-x"},
                                                                    "evidence": [{"kind": "message", "ref": "msg-1", "label": "carrier email"}]})).data["item"]
    ok = await dispatch(ctx_for(db, owner), "knowledge.approve", {"item_id": k2["id"]})
    assert ok.data["item"]["status"] == "approved"


# ── corpus ───────────────────────────────────────────────────────────────────
def test_chunker_keeps_overlap_and_boundaries():
    para = ("The truck arrived at the port on Monday. " * 12).strip()
    text = "\n\n".join([para] * 4)
    chunks = ksvc.chunk_text(text)
    assert len(chunks) >= 2 and all(len(c) <= ksvc.CHUNK_SIZE + 2 for c in chunks)
    # consecutive chunks overlap (the tail of one appears in the next)
    assert any(chunks[i][-40:] in chunks[i + 1] or chunks[i + 1][:40] in chunks[i] for i in range(len(chunks) - 1))
    assert ksvc.chunk_text("") == [] and ksvc.chunk_text("short") == ["short"]
    assert ksvc.detect_lang("この車は在庫があります") == "ja" and ksvc.detect_lang("in stock") == "en"


async def test_manifest_index_trust_labels_tsv_and_coverage(db, owner, monkeypatch):
    monkeypatch.delenv("EMBEDDINGS_URL", raising=False)
    m = await dispatch(ctx_for(db, owner), "corpus.create_manifest", {"label": f"pilot-{_u()}", "sources": [{"kind": "gmail_business", "scope": "info@azkeitrucks.com"}],
                                                                       "coverage_from": "2025-09-01T00:00:00Z", "coverage_to": "2026-09-01T00:00:00Z",
                                                                       "chunker_version": "v1"})
    man = m.data["manifest"]
    assert m.data["created"] and man["status"] == "active" and man["embedding"] == {"model": None, "dims": None} and man["chunker_version"] == "v1"
    mid = f"msg-{_u()}"
    r = await dispatch(ctx_for(db, owner), "corpus.index_text", {"source_kind": "message", "source_id": mid, "text": "Hi, is the Hijet still available? Thanks, Bob",
                                                                 "contact_id": None, "happened_at": "2026-01-05T10:00:00Z", "speaker": "bob@example.com"})
    assert r.data["created"] == 1 and r.data["embedding"] is None
    ch = (await db.execute(select(CorpusChunk).where(CorpusChunk.source_id == mid))).scalar_one()
    assert ch.trust == "untrusted_external" and ch.is_historical is True and ch.acl == {"visibility": "all", "personal_allowlisted": False}
    assert ch.manifest_id == man["id"] and ch.content_hash and ch.embedding is None
    from sqlalchemy import text as sqltext
    has_tsv = (await db.execute(sqltext("select tsv is not null from corpus_chunks where id = :i"), {"i": ch.id})).scalar_one()
    assert has_tsv is True
    # idempotent: same text again -> unchanged; changed text -> replaced, not duplicated
    r2 = await dispatch(ctx_for(db, owner), "corpus.index_text", {"source_kind": "message", "source_id": mid, "text": "Hi, is the Hijet still available? Thanks, Bob"})
    assert r2.data["unchanged"] is True and r2.data["created"] == 0
    r3 = await dispatch(ctx_for(db, owner), "corpus.index_text", {"source_kind": "message", "source_id": mid, "text": "Edited body text about the Hijet."})
    n = (await db.execute(select(CorpusChunk).where(CorpusChunk.source_id == mid, CorpusChunk.tombstoned_at.is_(None)))).scalars().all()
    assert len(n) == 1 and r3.data["created"] == 1
    # trust by source kind: tasks/notes are internal; approved trust is reserved for knowledge/procedures
    t = await dispatch(ctx_for(db, owner), "corpus.index_text", {"source_kind": "task_note", "source_id": f"task-{_u()}", "text": "Replace tires before listing."})
    assert t.data["chunks"][0]["trust"] == "internal" and t.data["chunks"][0]["is_historical"] is False
    with pytest.raises(ValidationFailed):
        await dispatch(ctx_for(db, owner), "corpus.index_text", {"source_kind": "message", "source_id": "m-bad", "text": "x", "trust": "approved"})
    cov = await ksvc.coverage(db)
    assert cov["defined"] is True and cov["counts"]["chunks"] >= 2 and cov["counts"]["by_trust"]["untrusted_external"] >= 1
    assert cov["embedding"]["configured"] is False and cov["manifest"]["id"] == man["id"] and "tombstoned_chunks" in cov["excluded"]


async def test_embedding_failure_is_recorded_not_faked(db, owner, monkeypatch):
    await dispatch(ctx_for(db, owner), "corpus.create_manifest", {"label": f"emb-{_u()}", "embedding_model": "test-embed", "embedding_dims": 4})
    monkeypatch.setenv("EMBEDDINGS_URL", "http://127.0.0.1:9/embeddings")  # nothing listens here
    r = await dispatch(ctx_for(db, owner), "corpus.index_text", {"source_kind": "note", "source_id": f"n-{_u()}", "text": "embedding attempt"})
    assert r.data["embedding"] is None and r.data["embedding_error"] and r.data["chunks"][0]["has_embedding"] is False
    cov = await ksvc.coverage(db)
    assert cov["failures"] and cov["failures"][-1]["stage"] == "embedding"
    # a manifest with a different dimension cannot become active alongside (never mix incompatible dims)
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "corpus.create_manifest", {"label": f"emb2-{_u()}", "embedding_model": "other", "embedding_dims": 8})


async def test_A06_tombstone_quarantines_text_and_reindex_needs_readmit(db, owner):
    sid = f"personal-{_u()}"
    await dispatch(ctx_for(db, owner), "corpus.index_text", {"source_kind": "message", "source_id": sid, "text": "Sebastian: the port release fee was paid.",
                                                             "personal_allowlisted": True, "visibility": "owner"})
    r = await dispatch(ctx_for(db, owner), "corpus.tombstone", {"source_kind": "message", "source_id": sid, "reason": "allowlist_narrowed"})
    assert r.data["tombstoned"] == 1
    ch = (await db.execute(select(CorpusChunk).where(CorpusChunk.source_id == sid))).scalar_one()
    assert ch.tombstoned_at is not None and ch.text == "" and ch.embedding is None and ch.tombstone_reason == "allowlist_narrowed"
    # indexing again without an explicit readmit is refused; readmit re-creates the chunk
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "corpus.index_text", {"source_kind": "message", "source_id": sid, "text": "again"})
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "corpus.reindex_source", {"source_kind": "message", "source_id": sid, "text": "again"})
    ok = await dispatch(ctx_for(db, owner), "corpus.reindex_source", {"source_kind": "message", "source_id": sid, "text": "again", "readmit": True})
    assert ok.data["created"] == 1
    acts = (await db.execute(select(ActivityEntry).where(ActivityEntry.entity_id == sid, ActivityEntry.state == "readmitted"))).scalars().all()
    assert acts and acts[0].visibility == "owner"
    evs = (await db.execute(select(Event).where(Event.type == "corpus.tombstoned", Event.aggregate_id == sid))).scalars().all()
    assert len(evs) == 1


# ── G11: learning from one owner review with four kinds of change ───────────
def test_classify_edit_splits_four_lessons():
    before = ("Hi Sam, thanks for reaching out about the 1998 Suzuki Carry. It is currently available and we would be delighted to "
              "assist you further with any questions you may have. The deposit is $500. Delivery to Tucson is $300. "
              "We look forward to hearing from you soon and hope you have a wonderful day.")
    after = ("Hi Sam, thanks for reaching out about the 1998 Suzuki Carry. It is currently reserved. The deposit is $500. "
             "As a one-time exception we will waive the $300 delivery fee for you. "
             "From now on all customers get a 7 day inspection window.")
    lessons = learning.classify_edit(before, after, {"contact_id": "c-1", "vehicle_id": "v-1", "workflow_key": "reply",
                                                     "evidence": [{"kind": "vehicle", "ref": "v-1", "label": "commercial_state=reserved"}]})
    kinds = {lesson.classification for lesson in lessons}
    assert kinds == {"style", "factual", "concession", "policy"}
    by = {lesson.classification: lesson for lesson in lessons}
    assert by["factual"].scope == {"vehicle_id": "v-1"} and "reserved" in by["factual"].diff["changed"] and by["factual"].needs_evidence is False
    assert by["concession"].scope == {"contact_id": "c-1"} and by["concession"].kind == "exception"
    assert by["policy"].scope == {} and by["policy"].kind == "policy" and by["policy"].usable_as == ["policy"]
    assert by["style"].kind == "style" and by["style"].usable_as == ["example"] and by["style"].scope == {"workflow": "reply"}
    # a pure shortening with unchanged facts is style only; an unchanged draft yields nothing
    only_style = learning.classify_edit(before, before.replace(" and we would be delighted to assist you further with any questions you may have", ""), {})
    assert {lesson.classification for lesson in only_style} == {"style"}
    assert learning.classify_edit(before, before, {}) == []
    assert learning.outcome_for(only_style, before, before + "x", None) == "style_edit"
    assert learning.outcome_for([], before, before, None) == "accepted"


async def test_G11_four_scoped_lessons_policy_needs_owner_no_grant_change(db, owner):
    c = await create_contact(db, owner, name=f"Sam Rivera {_u()}")
    perms_before = len((await db.execute(select(Permission))).scalars().all())
    before = ("Hi Sam, thanks for reaching out about the 1998 Suzuki Carry. It is currently available and we would be delighted to "
              "assist you further with any questions you may have. The deposit is $500. Delivery to Tucson is $300. "
              "We look forward to hearing from you soon and hope you have a wonderful day.")
    after = ("Hi Sam, thanks for reaching out about the 1998 Suzuki Carry. It is currently reserved. The deposit is $500. "
             "As a one-time exception we will waive the $300 delivery fee for you. "
             "From now on all customers get a 7 day inspection window.")
    payload = {"draft_before": before, "draft_after": after, "workflow_key": "reply", "entity_kind": "draft", "entity_id": f"d-{_u()}",
               "contact_id": c["id"], "vehicle_id": "veh-carry", "draft_version": 2, "review_seconds": 95,
               "evidence": [{"kind": "vehicle", "ref": "veh-carry", "label": "commercial_state=reserved"}]}
    res = await dispatch(ctx_for(db, owner, request_id=f"learn-{_u()}"), "learning.record_edit", payload)
    lessons = res.data["lessons"]
    assert {lesson["classification"] for lesson in lessons} == {"style", "factual", "concession", "policy"}
    by = {lesson["classification"]: lesson for lesson in lessons}
    assert all(lesson["status"] == "proposed" and lesson["requires_owner"] and lesson["decision"] == "Needs review" for lesson in lessons)
    assert by["concession"]["kind"] == "exception" and by["concession"]["scope"] == {"contact_id": c["id"]}
    assert by["factual"]["kind"] == "lesson" and by["factual"]["scope"] == {"vehicle_id": "veh-carry"} and by["factual"]["evidence"]
    assert by["policy"]["kind"] == "policy" and by["policy"]["is_general"] and by["policy"]["usable_as"] == ["policy"]
    assert by["style"]["kind"] == "style" and by["style"]["usable_as"] == ["example"]
    assert res.data["outcome"]["outcome"] == "factual_edit" and res.data["outcome"]["review_seconds"] == 95 and res.data["permission_change"] is False
    # retry (same payload) creates no second set of lessons or outcomes
    again = await dispatch(ctx_for(db, owner), "learning.record_edit", payload)
    assert {lesson["id"] for lesson in again.data["lessons"]} == {lesson["id"] for lesson in lessons} and all(not lesson["created"] for lesson in again.data["lessons"])
    outs = (await db.execute(select(WorkflowOutcome).where(WorkflowOutcome.entity_id == payload["entity_id"]))).scalars().all()
    assert len(outs) == 1
    # the policy proposal is not active knowledge until the owner approves it; nothing changed any grant
    assert (await db.get(KnowledgeItem, by["policy"]["id"])).status == "proposed"
    assert len((await db.execute(select(Permission))).scalars().all()) == perms_before
    act = (await db.execute(select(ActivityEntry).where(ActivityEntry.entity_id == payload["entity_id"]))).scalars().first()
    assert act.details["permission_change"] is False


async def test_learning_manager_edit_records_style_outcome(db, manager):
    before = "Hello, thank you very much for your message, we truly appreciate it. The truck is available."
    after = "Thanks for your message. The truck is available."
    res = await dispatch(ctx_for(db, manager), "learning.record_edit", {"draft_before": before, "draft_after": after, "workflow_key": "reply",
                                                                        "entity_kind": "draft", "entity_id": f"d-{_u()}", "review_seconds": 20})
    assert [lesson["classification"] for lesson in res.data["lessons"]] == ["style"] and res.data["outcome"]["outcome"] == "style_edit"
    acc = await dispatch(ctx_for(db, manager), "learning.record_edit", {"draft_before": after, "draft_after": after, "workflow_key": "reply",
                                                                        "entity_kind": "draft", "entity_id": f"d-{_u()}"})
    assert acc.data["lessons"] == [] and acc.data["outcome"]["outcome"] == "accepted"
    with pytest.raises(ValidationFailed):
        await dispatch(ctx_for(db, manager), "learning.record_edit", {"draft_before": "a", "draft_after": "b", "outcome": "bogus"})


# ── HTTP surface ─────────────────────────────────────────────────────────────
async def test_knowledge_api_roles_and_teach(client, db, owner, manager, mechanic):
    login(client, owner)
    r = await client.post("/api/teach", json={"kind": "policy", "text": "Never promise a delivery date before the carrier confirms.", "title": f"No ETA promises {_u()}"})
    assert r.status_code == 200, r.text
    item = r.json()["data"]["item"]
    assert item["kind"] == "policy" and item["status"] == "proposed" and item["source_kind"] == "teach"
    r = await client.post("/api/teach", json={"kind": "text", "title": f"Quote request {_u()}", "text": "1. Open the Montway form\n2. Enter the stock number\nNever enter the buyer's phone number.\nIf the form has no kei preset, escalate to Dylan.",
                                              "workflow_key": "quote.request"})
    assert r.status_code == 200, r.text
    proc = r.json()["data"]
    assert proc["version"]["stage"] == "proposed" and len(proc["version"]["spec"]["normal_path"]) == 2 and proc["version"]["spec"]["hard_constraints"]
    r = await client.post("/api/teach", json={"kind": "bogus", "text": "x"})
    assert r.status_code == 422
    r = await client.post(f"/api/knowledge/{item['id']}/approve", json={"expected_version": item["version"]})
    assert r.status_code == 200 and r.json()["data"]["item"]["status"] == "approved"
    r = await client.get("/api/knowledge", params={"status": "approved", "kind": "policy"})
    assert r.status_code == 200 and any(i["id"] == item["id"] for i in r.json()["items"]) and "total" in r.json()
    r = await client.get(f"/api/knowledge/{item['id']}")
    assert r.status_code == 200 and r.json()["id"] == item["id"]
    r = await client.get("/api/knowledge/corpus")
    assert r.status_code == 200 and "counts" in r.json() and "excluded" in r.json()
    r = await client.get("/api/knowledge/corpus/manifests")
    assert r.status_code == 200 and "items" in r.json()
    # manager: may read general policies, cannot approve, cannot see the corpus admin surface
    login(client, manager)
    r = await client.get("/api/knowledge", params={"kind": "policy"})
    assert r.status_code == 200 and any(i["id"] == item["id"] for i in r.json()["items"])
    r = await client.post(f"/api/knowledge/{item['id']}/retire", json={})
    assert r.status_code == 403
    r = await client.get("/api/knowledge/corpus")
    assert r.status_code == 403
    r = await client.post("/api/teach", json={"kind": "policy", "text": "managers cannot teach policy"})
    assert r.status_code == 403
    # mechanic: general policy visible, exceptions and owner-only items never
    login(client, mechanic)
    c = await create_contact(db, owner, name=f"Private {_u()}")
    exc = (await dispatch(ctx_for(db, owner), "knowledge.propose", {"kind": "exception", "title": "secret concession", "content": "10% off",
                                                                     "scope": {"contact_id": c["id"]}})).data["item"]
    await dispatch(ctx_for(db, owner), "knowledge.approve", {"item_id": exc["id"]})
    r = await client.get("/api/knowledge")
    assert r.status_code == 200
    ids = {i["id"] for i in r.json()["items"]}
    assert item["id"] in ids and exc["id"] not in ids
    r = await client.get(f"/api/knowledge/{exc['id']}")
    assert r.status_code == 404
    r = await client.get("/api/knowledge/search", params={"q": "secret concession"})
    assert r.status_code == 200 and all(k["id"] != exc["id"] for k in r.json()["approved_knowledge"])
