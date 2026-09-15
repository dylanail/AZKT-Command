"""Sourcing acceptance: import request lifecycle gates, candidate evaluation (G01), shared candidates across buyers
(G02), translation completeness/revision (G03), bid packets and revalidation (G04), win/loss/manual purchase (G05)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from backend.app.core.errors import Blocked, Conflict, Denied
from backend.app.domain.commands import CommandContext, dispatch
from backend.app.models.contacts import Contact
from backend.app.models.runtime import Approval
from backend.app.models.sourcing import Bid, Candidate, CandidateMatch, ImportRequest, Translation
from backend.app.models.tasks import Case, Task
from backend.app.models.vehicles import Vehicle, VehicleMilestone
from backend.app.services import requirements as reqs
from backend.tests.conftest import actor_of, ctx_for, login, run_worker_once

NOW = datetime.now(timezone.utc)
MUST_MANUAL = {"key": "manual_transmission", "tier": "must", "text": "Manual transmission", "field": "transmission", "op": "eq", "value": "manual"}
MUST_AC = {"key": "ac", "tier": "must", "text": "Air conditioning", "field": "ac", "op": "truthy"}
PREFER_YEAR = {"key": "year_2018_plus", "tier": "prefer", "text": "2018 or newer", "field": "year", "op": "gte", "value": 2018}
PREFER_KM = {"key": "low_km", "tier": "prefer", "text": "Under 60,000 km", "field": "mileage_km", "op": "lte", "value": 60000}
AVOID_RUST = {"key": "no_rust", "tier": "avoid", "text": "No rust", "field": "rust", "op": "truthy"}


@pytest_asyncio.fixture(autouse=True, scope="module")
async def _drain_outbox_after_module():
    """These scenarios emit many domain events; drain the shared outbox/jobs on teardown so later test modules
    (which assume an idle worker) start from a clean queue."""
    yield
    for _ in range(50):
        out = await run_worker_once()
        if not out.get("events") and not out.get("jobs"):
            break


def uid() -> str:
    return uuid.uuid4().hex[:8]


def raw_candidate(*, lot: str | None = None, deadline: datetime | None = None, auction_at: datetime | None = None, **specs) -> dict:
    lot = lot or f"L{uid()}"
    auction_at = auction_at or (NOW + timedelta(days=5))
    deadline = deadline or (auction_at - timedelta(hours=12))
    return {"auction_house": "USS Tokyo", "lot_no": lot, "auction_at": auction_at.isoformat(), "deadline_at": deadline.isoformat(),
            "deadline_source": "uss:bid_deadline", "url": f"https://auction.example/{lot}", "title": f"Daihatsu Hijet {lot}",
            "frame_no": f"S510P-{uid()}", "specs": specs}


def complete_doc(lot: str) -> str:
    return "\n".join(["Auction sheet", f"USS Tokyo lot {lot}", "Grade: 4", "Condition", "Minor scratches on the bed.",
                      "Translator notes", "No accident history noted.", "Translation complete"])


def incomplete_doc(lot: str) -> str:
    return "\n".join(["Auction sheet", f"USS Tokyo lot {lot}", "Grade: 4", "Condition", "Minor scratches."])


async def make_contact(db, name: str) -> Contact:
    c = Contact(name=name, roles=["buyer"], status="active", search_text=name.lower(), primary_email=f"{uid()}@example.com")
    db.add(c)
    await db.commit()
    return c


async def active_request(db, owner, contact: Contact, requirements: list[dict], *, title: str = "Kei truck",
                         budget: tuple[str, str] | None = ("12000", "USD")) -> ImportRequest:
    """Inquiry -> agreement signed (evidence) -> deposit rule -> deposit confirmed from payment evidence -> Active Search."""
    payload = {"contact_id": contact.id, "title": title, "requirements": requirements, "source_ref": f"msg-{uid()}"}
    if budget:
        payload.update({"budget_amount": budget[0], "budget_currency": budget[1]})
    res = await dispatch(ctx_for(db, owner), "import_requests.create", payload)
    rid = res.data["request"]["id"]
    assert res.data["request"]["status"] == "inquiry"
    await dispatch(ctx_for(db, owner), "import_requests.set_agreement", {"request_id": rid, "agreement_id": f"agr-{uid()}",
                                                                          "status": "signed", "source_ref": f"docusign-{uid()}"})
    await dispatch(ctx_for(db, owner), "import_requests.set_deposit_rule", {"request_id": rid, "amount": "1000", "currency": "USD",
                                                                             "source_ref": "agreement clause 3"})
    res = await dispatch(ctx_for(db, owner), "import_requests.confirm_deposit", {"request_id": rid, "payment_id": f"pay-{uid()}",
                                                                                  "amount": "1000", "currency": "USD", "source_ref": f"square-{uid()}"})
    assert res.data["request"]["status"] == "active_search", res.data["gate"]
    return await db.get(ImportRequest, rid)


async def ingest(db, owner, *raws: dict, match: bool = False) -> list[Candidate]:
    res = await dispatch(ctx_for(db, owner), "candidates.ingest", {"candidates": list(raws), "match": match})
    return [await db.get(Candidate, i) for i in res.data["candidate_ids"]]


async def match_for(db, owner, request: ImportRequest, candidate: Candidate) -> CandidateMatch:
    await dispatch(ctx_for(db, owner), "candidates.match", {"candidate_id": candidate.id, "request_id": request.id, "force": True})
    m = (await db.execute(select(CandidateMatch).where(CandidateMatch.candidate_id == candidate.id,
                                                        CandidateMatch.import_request_id == request.id))).scalar_one()
    await db.refresh(m)
    return m


async def approve(db, owner, approval_id: str) -> dict:
    res = await dispatch(ctx_for(db, owner), "approvals.approve", {"approval_id": approval_id})
    return res.data


async def complete_translation(db, owner, candidate: Candidate, *, findings: dict | None = None) -> Translation:
    res = await dispatch(ctx_for(db, owner), "translations.request", {"candidate_id": candidate.id})
    assert res.status == "needs_review"
    out = await approve(db, owner, res.approval_id)
    tid = out["result"]["data"]["translation"]["id"]
    await dispatch(ctx_for(db, owner), "translations.record_detected", {"translation_id": tid, "doc_ref": f"gdoc-{candidate.lot_no}",
                                                                         "doc_revision": "r1", "text": complete_doc(candidate.lot_no)})
    res = await dispatch(ctx_for(db, owner), "translations.mark_complete", {
        "translation_id": tid, "findings": findings or {},
        "excerpts": [{"section": "Condition", "ja": "荷台に小傷", "en": "Minor scratches on the bed."}]})
    assert res.data["translation"]["status"] == "complete"
    return await db.get(Translation, tid)


async def prepared_bid(db, owner, candidate: Candidate, request: ImportRequest, max_amount: str = "450000") -> Bid:
    res = await dispatch(ctx_for(db, owner), "bids.prepare", {
        "candidate_id": candidate.id, "import_request_id": request.id, "max_amount": max_amount, "currency": "JPY",
        "fee_basis": "auction fee + exporter fee per agreement", "fx_estimate": {"rate": "0.0067", "source": "xe.com", "date": "2026-09-14"},
        "disclosures": ["Grade 4, minor bed scratches"]})
    return await db.get(Bid, res.data["bid"]["id"])


async def pending_bid(db, owner, candidate: Candidate, request: ImportRequest, max_amount: str = "450000") -> tuple[Bid, Approval]:
    b = await prepared_bid(db, owner, candidate, request, max_amount)
    res = await dispatch(ctx_for(db, owner), "bids.submit_for_approval", {"bid_id": b.id})
    assert res.data["status"] == "needs_review", res.data
    await db.refresh(b)
    return b, await db.get(Approval, res.data["approval_id"])


# ── lifecycle gates ─────────────────────────────────────────────────────────
async def test_lifecycle_gates_block_active_search_until_evidence(db, owner):
    contact = await make_contact(db, "Gate Buyer")
    res = await dispatch(ctx_for(db, owner), "import_requests.create", {"contact_id": contact.id, "title": "Gated", "requirements": [MUST_MANUAL],
                                                                         "source_ref": f"src-{uid()}"})
    r = res.data["request"]
    gate = r["active_search_gate"]
    assert gate["decision"] == "Blocked"
    keys = {g["key"]: g for g in gate["gates"]}
    assert keys["agreement"]["ok"] is False and keys["deposit"]["ok"] is False and "not configured" in keys["deposit"]["reason"]
    assert keys["requirements"]["ok"] is True
    # same source_ref: idempotent creation, no second row
    again = await dispatch(ctx_for(db, owner), "import_requests.create", {"contact_id": contact.id, "title": "Gated", "requirements": [MUST_MANUAL],
                                                                           "source_ref": r["source_ref"]})
    assert again.data["created"] is False and again.data["request"]["id"] == r["id"]
    # signed without evidence is refused; signed with evidence moves to deposit_pending
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "import_requests.set_agreement", {"request_id": r["id"], "status": "signed"})
    res = await dispatch(ctx_for(db, owner), "import_requests.set_agreement", {"request_id": r["id"], "status": "signed", "source_ref": "sig-1"})
    assert res.data["request"]["status"] == "deposit_pending"
    # deposit without a configured rule stays blocked with an explicit reason (no invented amount)
    with pytest.raises(Blocked) as e:
        await dispatch(ctx_for(db, owner), "import_requests.confirm_deposit", {"request_id": r["id"], "payment_id": "p1", "amount": "500",
                                                                                "currency": "USD", "source_ref": "sq-1"})
    assert "not configured" in e.value.message
    # the deposit rule is owner-only
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, await _manager(db)), "import_requests.set_deposit_rule", {"request_id": r["id"], "amount": "1000", "currency": "USD"})
    # setting the rule alone never opens the gate: the deposit is still only "pending", so the request stays deposit_pending
    res = await dispatch(ctx_for(db, owner), "import_requests.set_deposit_rule", {"request_id": r["id"], "amount": "1000", "currency": "USD"})
    assert res.data["request"]["status"] == "deposit_pending" and res.data["request"]["deposit_status"] == "pending"
    assert res.data["gate"]["decision"] == "Blocked"
    assert {g["key"]: g["ok"] for g in res.data["gate"]["gates"]}["deposit"] is False
    # a partial payment is a recorded fact, so it is persisted and answered with a structured Blocked decision
    # (raising would roll the evidence back); the gate stays shut and the remaining balance is explicit.
    res = await dispatch(ctx_for(db, owner), "import_requests.confirm_deposit", {"request_id": r["id"], "payment_id": "p1", "amount": "400",
                                                                                  "currency": "USD", "source_ref": "sq-1"})
    assert res.data["confirmed"] is False and res.data["partial"] is True and res.data["decision"] == "Blocked"
    assert res.data["remaining"] == "600.00" and res.data["request"]["deposit_status"] == "partial"
    assert res.data["request"]["status"] == "deposit_pending" and res.data["gate"]["decision"] == "Blocked"
    # currency mismatches are never converted by assumption
    with pytest.raises(Blocked) as e:
        await dispatch(ctx_for(db, owner), "import_requests.confirm_deposit", {"request_id": r["id"], "payment_id": "p1b", "amount": "600",
                                                                                "currency": "JPY", "source_ref": "sq-1b"})
    assert "does not match" in e.value.message
    res = await dispatch(ctx_for(db, owner), "import_requests.confirm_deposit", {"request_id": r["id"], "payment_id": "p2", "amount": "600",
                                                                                  "currency": "USD", "source_ref": "sq-2"})
    assert res.data["request"]["status"] == "active_search" and res.data["request"]["deposit_status"] == "confirmed"
    assert res.data["request"]["deposit_evidence"]["amount"] == "1000.00" and len(res.data["request"]["deposit_evidence"]["payments"]) == 2
    # replay of the same payment is idempotent; a different payment conflicts
    rep = await dispatch(ctx_for(db, owner), "import_requests.confirm_deposit", {"request_id": r["id"], "payment_id": "p2", "amount": "600",
                                                                                  "currency": "USD", "source_ref": "sq-2"})
    assert rep.data["idempotent"] is True and rep.data["changed"] is False
    with pytest.raises(Conflict):
        await dispatch(ctx_for(db, owner), "import_requests.confirm_deposit", {"request_id": r["id"], "payment_id": "p3", "amount": "1000",
                                                                                "currency": "USD", "source_ref": "sq-3"})
    # must-have change needs buyer evidence and bumps the version
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "import_requests.revise_requirements", {"request_id": r["id"], "requirements": [MUST_AC]})
    res = await dispatch(ctx_for(db, owner), "import_requests.revise_requirements", {"request_id": r["id"], "requirements": [MUST_AC],
                                                                                      "source_ref": "buyer-email-9"})
    assert res.data["request"]["requirements_version"] == 2 and res.data["must_changed"] is True


async def _manager(db):
    from backend.tests.conftest import make_user
    from backend.app.models import User
    u = (await db.execute(select(User).where(User.handle == "luis"))).scalar_one_or_none()
    return u or await make_user(db, "luis", "manager", display_name="Luis")


# ── G01 ─────────────────────────────────────────────────────────────────────
async def test_G01_mandatory_fail_blocks_and_unknown_blocks_bid_readiness(db, owner):
    contact = await make_contact(db, "G01 Buyer")
    r = await active_request(db, owner, contact, [MUST_MANUAL, MUST_AC, PREFER_YEAR, PREFER_KM, AVOID_RUST])
    a, b, c = await ingest(
        db, owner,
        raw_candidate(transmission="automatic", ac=True, year=2021, mileage_km=20000, rust=False),  # best prefs, fails a must
        raw_candidate(transmission="manual", year=2019, mileage_km=50000, rust=False),                # A/C unknown
        raw_candidate(transmission="manual", ac=True, year=2015, mileage_km=90000, rust=False))       # passes musts, weak prefs
    ma, mb, mc = [await match_for(db, owner, r, x) for x in (a, b, c)]
    assert ma.mandatory_fail is True and ma.status == "rejected" and ma.score is None and ma.bid_ready is False
    assert "Fails must-have: manual_transmission" in ma.rejected_reason
    assert mb.mandatory_fail is False and mb.mandatory_unknown is True and mb.bid_ready is False
    assert mb.extra["needs_confirmation"] == ["ac"]
    assert {o["key"]: o["result"] for o in mb.outcomes}["ac"] == "unknown"
    assert mc.mandatory_fail is False and mc.mandatory_unknown is False and mc.bid_ready is True
    # score cannot override a gate: A has the best preference score but is not rankable; B ranks after C despite better prefs
    assert (ma.extra["preference_score"] or 0) > (mc.extra["preference_score"] or 0)
    ranked = reqs.rank([{"id": m.id, "mandatory_fail": m.mandatory_fail, "mandatory_unknown": m.mandatory_unknown, "score": m.score} for m in (ma, mb, mc)])
    assert [x["id"] for x in ranked] == [mc.id, mb.id]
    await db.refresh(r)
    assert any(e["candidate_id"] == a.id and e["kind"] == "mandatory_fail" for e in r.exclusions)
    # bids on the failed / unknown candidates are blocked with structured reasons; the unknown one asks for confirmation
    bb = await prepared_bid(db, owner, b, r)
    with pytest.raises(Blocked) as e:
        await dispatch(ctx_for(db, owner), "bids.submit_for_approval", {"bid_id": bb.id})
    assert any("unknown must-have needs confirmation: ac" in x for x in e.value.detail["reasons"])
    ba = await prepared_bid(db, owner, a, r)
    with pytest.raises(Blocked) as e:
        await dispatch(ctx_for(db, owner), "bids.submit_for_approval", {"bid_id": ba.id})
    assert any("fails must-have" in x for x in e.value.detail["reasons"])
    # re-matching keeps the rejection and its reason (never re-presented as new)
    ma2 = await match_for(db, owner, r, a)
    assert ma2.id == ma.id and ma2.status == "rejected"
    # candidate identity: re-ingest updates the snapshot in place, never duplicates
    again = await dispatch(ctx_for(db, owner), "candidates.ingest", {"candidates": [raw_candidate(lot=b.lot_no, auction_at=b.auction_at,
                                                                                                   deadline=b.deadline_at, transmission="manual",
                                                                                                   ac=True, year=2019, mileage_km=50000)], "match": False})
    assert again.data["created"] == 0 and again.data["updated"] == 1
    rows = (await db.execute(select(Candidate).where(Candidate.lot_no == b.lot_no))).scalars().all()
    assert len(rows) == 1 and rows[0].snapshot_version == 2
    mb2 = await match_for(db, owner, r, b)
    assert mb2.mandatory_unknown is False and mb2.bid_ready is True


async def test_auction_sources_return_typed_unsupported_or_fixture(db, owner):
    res = await dispatch(ctx_for(db, owner), "candidates.ingest", {"source": {"kind": "http"}, "match": False})
    assert res.data["status"] == "unsupported" and res.data["reason"]["kind"] == "not_configured"
    res = await dispatch(ctx_for(db, owner), "candidates.ingest", {"source": {"kind": "fixture", "items": [raw_candidate(transmission="manual", ac=True)]},
                                                                    "match": False})
    assert res.data["status"] == "ok" and res.data["created"] == 1 and res.data["source"]["source"] == "fixture"


# ── G02 ─────────────────────────────────────────────────────────────────────
async def test_G02_one_candidate_two_buyers_one_translation_private_matches_no_duplicate_bid(db, owner):
    alice, bob = await make_contact(db, "Alice Aardvark"), await make_contact(db, "Bob Bandicoot")
    ra = await active_request(db, owner, alice, [MUST_MANUAL, MUST_AC], title="Alice hijet", budget=("13500", "USD"))
    rb = await active_request(db, owner, bob, [MUST_MANUAL, MUST_AC], title="Bob hijet", budget=("9800", "USD"))
    (c,) = await ingest(db, owner, raw_candidate(transmission="manual", ac=True, year=2019, mileage_km=40000))
    ma, mb = await match_for(db, owner, ra, c), await match_for(db, owner, rb, c)
    assert ma.id != mb.id and ma.bid_ready and mb.bid_ready
    # two overlapping translation requests -> one translation for the candidate (exact approval each time; manual task, never "sent")
    r1 = await dispatch(ctx_for(db, owner), "translations.request", {"candidate_id": c.id})
    r2 = await dispatch(ctx_for(db, owner, kind="agent"), "translations.request", {"candidate_id": c.id})
    assert r1.status == "needs_review" and r2.status == "needs_review" and r1.approval_id == r2.approval_id  # same pending approval
    out = await approve(db, owner, r1.approval_id)
    t = out["result"]["data"]["translation"]
    assert out["result"]["data"]["created"] is True and t["request_channel"] == "manual" and t["status"] == "manual_task"
    task = await db.get(Task, t["manual_task_id"])
    assert task is not None and c.lot_no in task.instructions
    r3 = await dispatch(ctx_for(db, owner), "translations.request", {"candidate_id": c.id, "note": "second run"})
    out3 = await approve(db, owner, r3.approval_id)
    assert out3["result"]["data"]["deduplicated"] is True
    rows = (await db.execute(select(Translation).where(Translation.candidate_id == c.id))).scalars().all()
    assert len(rows) == 1
    tid = rows[0].id
    await dispatch(ctx_for(db, owner), "translations.record_detected", {"translation_id": tid, "doc_ref": "gdoc-shared", "doc_revision": "5",
                                                                         "text": complete_doc(c.lot_no)})
    await dispatch(ctx_for(db, owner), "translations.mark_complete", {"translation_id": tid, "excerpts": [{"section": "Grade", "ja": "4点", "en": "Grade 4"}]})
    # independent private buyer drafts: no other buyer's identity, budget or request appears
    da = (await dispatch(ctx_for(db, owner), "candidates.prepare_buyer_message", {"match_id": ma.id})).data["draft"]
    dbob = (await dispatch(ctx_for(db, owner), "candidates.prepare_buyer_message", {"match_id": mb.id})).data["draft"]
    for draft, other, budget, other_req in ((da, "Bob", "9800", rb.id), (dbob, "Alice", "13500", ra.id)):
        assert other not in draft["body"] and budget not in draft["body"] and other_req not in draft["body"]
        assert draft["scope"]["import_request_id"] != other_req
    assert "Grade 4" in da["body"] and c.lot_no in da["body"]
    # no duplicate competing bids for the same lot from two linked requests (invariant 8)
    ba, appr = await pending_bid(db, owner, c, ra)
    assert ba.status == "pending_approval" and appr.status == "pending"
    bb = await prepared_bid(db, owner, c, rb)
    with pytest.raises(Blocked) as e:
        await dispatch(ctx_for(db, owner), "bids.submit_for_approval", {"bid_id": bb.id})
    assert any("another linked request already has an active bid" in x for x in e.value.detail["reasons"])
    # the shared candidate view never leaks identities either: each match only names its own request
    from backend.app.services.sourcing import serialize_match
    assert rb.id not in str(serialize_match(ma)) and ra.id not in str(serialize_match(mb))


# ── G03 ─────────────────────────────────────────────────────────────────────
async def test_G03_incomplete_doc_stays_incomplete_and_revision_invalidates_dependents(db, owner):
    a, b = await make_contact(db, "Cara Corrections"), await make_contact(db, "Dan Delivered")
    ra = await active_request(db, owner, a, [MUST_MANUAL, MUST_AC], title="Cara")
    rb = await active_request(db, owner, b, [MUST_MANUAL, MUST_AC], title="Dan")
    (c,) = await ingest(db, owner, raw_candidate(transmission="manual", ac=True, year=2020, mileage_km=30000))
    ma, mb = await match_for(db, owner, ra, c), await match_for(db, owner, rb, c)
    res = await dispatch(ctx_for(db, owner), "translations.request", {"candidate_id": c.id})
    tid = (await approve(db, owner, res.approval_id))["result"]["data"]["translation"]["id"]
    # edited but incomplete: missing sections keep it incomplete; matches show translation_incomplete
    res = await dispatch(ctx_for(db, owner), "translations.record_detected", {"translation_id": tid, "doc_ref": "gdoc-1", "doc_revision": "3",
                                                                               "text": incomplete_doc(c.lot_no), "modified_at": NOW.isoformat()})
    t = res.data["translation"]
    assert t["status"] == "incomplete" and "Translator notes" in t["completeness"]["missing"] and "Translation complete" in t["completeness"]["missing"]
    await db.refresh(ma)
    assert ma.status == "translation_incomplete"
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "translations.mark_complete", {"translation_id": tid})
    # the same revision with only a newer timestamp is not new information, let alone completion
    res = await dispatch(ctx_for(db, owner), "translations.record_detected", {"translation_id": tid, "doc_ref": "gdoc-1", "doc_revision": "3",
                                                                               "text": incomplete_doc(c.lot_no), "modified_at": (NOW + timedelta(hours=2)).isoformat()})
    assert res.data["changed"] is False and res.data["translation"]["status"] == "incomplete"
    # complete text for the wrong lot is an identity mismatch
    res = await dispatch(ctx_for(db, owner), "translations.record_detected", {"translation_id": tid, "doc_ref": "gdoc-1", "doc_revision": "4",
                                                                               "text": complete_doc("L-OTHER")})
    assert res.data["translation"]["status"] == "incomplete" and res.data["translation"]["identity_check"]["ok"] is False
    # corrected doc via the fixture adapter -> detected -> complete with excerpts + revision
    res = await dispatch(ctx_for(db, owner), "translations.record_detected", {
        "translation_id": tid, "doc_ref": "gdoc-1", "doc_revision": "5",
        "doc": {"kind": "fixture", "docs": {"gdoc-1": {"revision": "5", "text": complete_doc(c.lot_no)}}}})
    assert res.data["translation"]["status"] == "detected"
    res = await dispatch(ctx_for(db, owner), "translations.mark_complete", {"translation_id": tid, "findings": {"grade": "4"},
                                                                             "excerpts": [{"section": "Condition", "ja": "荷台に小傷", "en": "Minor scratches"}]})
    assert res.data["translation"]["status"] == "complete" and res.data["translation"]["excerpts"][0]["doc_revision"] == "5"
    await db.refresh(c)
    assert c.specs["grade"] == "4" and c.extra["spec_sources"]["grade"].startswith("translation:")
    # drafts: Cara's unsent, Dan's sent; a bid pending approval
    await dispatch(ctx_for(db, owner), "candidates.prepare_buyer_message", {"match_id": ma.id})
    await dispatch(ctx_for(db, owner), "candidates.prepare_buyer_message", {"match_id": mb.id})
    await dispatch(ctx_for(db, owner), "candidates.mark_buyer_message_sent", {"match_id": mb.id, "message_ref": "gmail-77"})
    bid, appr = await pending_bid(db, owner, c, ra)
    # the corrected translation invalidates the unsent draft + pending bid approval, opens a correction case for the sent one
    res = await dispatch(ctx_for(db, owner), "translations.revise", {"translation_id": tid, "doc_revision": "6",
                                                                      "text": complete_doc(c.lot_no) + "\nGrade: 3.5", "reason": "grade corrected to 3.5"})
    assert res.data["drafts_invalidated"] == 1 and res.data["bids_invalidated"] == 1 and len(res.data["correction_cases"]) == 1
    await db.refresh(appr)
    await db.refresh(bid)
    assert appr.status == "invalidated" and "translation revised" in appr.invalidated_reason and bid.status == "invalidated"
    await db.refresh(ma)
    await db.refresh(mb)
    assert ma.extra["buyer_draft"]["invalidated"] is True and mb.extra["correction_case_id"]
    case = await db.get(Case, mb.extra["correction_case_id"])
    assert case.kind == "reply" and case.import_request_id == rb.id
    t = await db.get(Translation, tid)
    await db.refresh(t)
    assert t.revision_no == 1 and t.revision_history[0]["revision_no"] == 0 and t.revision_history[0]["excerpts"][0]["en"] == "Minor scratches"
    await db.refresh(c)
    assert "grade" not in c.specs  # superseded translation facts are dropped until re-confirmed


# ── G04 ─────────────────────────────────────────────────────────────────────
async def test_G04_expired_deadline_blocks_and_changes_invalidate_with_dual_time(db, owner):
    contact = await make_contact(db, "Eve Eager")
    r = await active_request(db, owner, contact, [MUST_MANUAL, MUST_AC])
    past = NOW - timedelta(days=2)
    (expired,) = await ingest(db, owner, raw_candidate(auction_at=past + timedelta(hours=6), deadline=past, transmission="manual", ac=True))
    await match_for(db, owner, r, expired)
    await complete_translation(db, owner, expired)
    await dispatch(ctx_for(db, owner), "candidates.record_interest", {"match_id": (await match_for(db, owner, r, expired)).id, "note": "buyer keen"})
    b = await prepared_bid(db, owner, expired, r)
    assert b.packet["deadline"]["tokyo"].endswith("JST") and b.packet["deadline"]["phoenix"].endswith("AZ")
    with pytest.raises(Blocked) as e:
        await dispatch(ctx_for(db, owner), "bids.submit_for_approval", {"bid_id": b.id})
    reason = next(x for x in e.value.detail["reasons"] if "deadline passed" in x)
    assert "JST" in reason and "AZ" in reason
    # live candidate: interest is not authorization; the packet needs the owner's exact approval
    (c,) = await ingest(db, owner, raw_candidate(transmission="manual", ac=True, year=2019, mileage_km=40000))
    await match_for(db, owner, r, c)
    await complete_translation(db, owner, c)
    bid, a1 = await pending_bid(db, owner, c, r, "500000")
    assert bid.packet["max"] == {"amount": "500000", "currency": "JPY"} and bid.packet["fx_estimate"]["usd"] == "3350.00"
    d = bid.packet["deadline"]
    assert d["utc"] and d["tokyo"] and d["phoenix"] and d["source"] == "uss:bid_deadline"
    # a changed max invalidates the pending approval and needs a fresh one
    b2 = await prepared_bid(db, owner, c, r, "600000")
    await db.refresh(a1)
    assert b2.id == bid.id and b2.status == "draft" and a1.status == "invalidated"
    res = await dispatch(ctx_for(db, owner), "bids.submit_for_approval", {"bid_id": b2.id})
    a2 = await db.get(Approval, res.data["approval_id"])
    # a changed deadline in the source snapshot invalidates the approved packet
    await dispatch(ctx_for(db, owner), "candidates.ingest", {"candidates": [raw_candidate(lot=c.lot_no, auction_at=c.auction_at,
                                                                                            deadline=c.deadline_at + timedelta(days=1),
                                                                                            transmission="manual", ac=True, year=2019, mileage_km=40000)],
                                                              "match": False})
    await db.refresh(a2)
    await db.refresh(b2)
    assert a2.status == "invalidated" and b2.status == "invalidated" and "deadline changed" in a2.invalidated_reason
    stale = await match_for(db, owner, r, c)  # the snapshot change also flagged the match; re-evaluation clears it
    assert stale.stale is False
    # revalidate immediately before execution: a lot/deadline change after approval request blocks execution
    b3, a3 = await pending_bid(db, owner, c, r, "600000")
    c.deadline_at = c.deadline_at + timedelta(hours=3)
    await db.commit()
    out = await approve(db, owner, a3.id)
    assert out["executed"] is False and out["approval"]["status"] == "invalidated"
    assert any("deadline changed" in x for x in out["approval"]["invalidated_reason"].split("; "))
    # an untouched packet executes: approved is not submitted; placement is a manual task with the exact packet
    b4, a4 = await pending_bid(db, owner, c, r, "600000")
    out = await approve(db, owner, a4.id)
    assert out["executed"] is True and out["result"]["data"]["submitted"] is False and out["result"]["data"]["channel"] == "manual_task"
    await db.refresh(b4)
    assert b4.status == "approved" and b4.submission_task_id
    task = await db.get(Task, b4.submission_task_id)
    assert "600000 JPY" in task.instructions and b4.packet_hash in task.instructions


# ── G05 ─────────────────────────────────────────────────────────────────────
async def _approved_bid(db, owner, request: ImportRequest, candidate: Candidate) -> Bid:
    await match_for(db, owner, request, candidate)
    await complete_translation(db, owner, candidate)
    b, a = await pending_bid(db, owner, candidate, request)
    await approve(db, owner, a.id)
    await db.refresh(b)
    assert b.status == "approved"
    return b


async def test_G05_won_lost_replay_idempotent_and_manual_purchase_never_invents_bid(db, owner):
    contact = await make_contact(db, "Finn Fulfilled")
    r = await active_request(db, owner, contact, [MUST_MANUAL, MUST_AC], title="Finn")
    (c,) = await ingest(db, owner, raw_candidate(transmission="manual", ac=True, year=2019, mileage_km=40000, make="Daihatsu", model="Hijet"))
    b = await _approved_bid(db, owner, r, c)
    won = {"bid_id": b.id, "result": "won", "evidence": {"source_ref": "exporter-mail-1", "amount": "480000", "currency": "JPY",
                                                          "at": (NOW - timedelta(hours=3)).isoformat()}}
    res = await dispatch(ctx_for(db, owner), "bids.record_result", won)
    vid = res.data["vehicle_id"]
    assert res.data["changed"] is True and res.data["request_status"] == "purchased" and vid
    v = await db.get(Vehicle, vid)
    assert v.origin_candidate_id == c.id and v.stock_no and v.stock_no.startswith("STK-") and v.buyer_contact_id == contact.id
    assert v.purchase_evidence["bid_id"] == b.id and v.purchase_amount == Decimal("480000")
    ms = (await db.execute(select(VehicleMilestone).where(VehicleMilestone.vehicle_id == vid, VehicleMilestone.kind == "purchased"))).scalars().all()
    assert len(ms) == 1 and ms[0].status == "completed" and ms[0].source_ref == "exporter-mail-1"
    # replaying the win creates nothing new; a contradicting result conflicts
    rep = await dispatch(ctx_for(db, owner), "bids.record_result", won)
    assert rep.data["idempotent"] is True and rep.data["vehicle_id"] == vid
    assert len((await db.execute(select(Vehicle).where(Vehicle.origin_candidate_id == c.id))).scalars().all()) == 1
    with pytest.raises(Conflict):
        await dispatch(ctx_for(db, owner), "bids.record_result", {**won, "result": "lost"})
    await db.refresh(r)
    assert r.purchased_vehicle_id == vid and r.status == "purchased" and r.purchase_evidence["manual"] is False
    # lost: the request keeps searching and remembers the exclusion
    contact2 = await make_contact(db, "Gia Gone")
    r2 = await active_request(db, owner, contact2, [MUST_MANUAL, MUST_AC], title="Gia")
    (c2,) = await ingest(db, owner, raw_candidate(transmission="manual", ac=True, year=2018, mileage_km=50000))
    b2 = await _approved_bid(db, owner, r2, c2)
    lost = {"bid_id": b2.id, "result": "lost", "evidence": {"source_ref": "exporter-mail-2"}}
    res = await dispatch(ctx_for(db, owner), "bids.record_result", lost)
    await db.refresh(r2)
    assert res.data["request_status"] == "active_search" and any(e["candidate_id"] == c2.id and e["kind"] == "lost" for e in r2.exclusions)
    rep = await dispatch(ctx_for(db, owner), "bids.record_result", lost)
    assert rep.data["idempotent"] is True and rep.data["vehicle_id"] is None
    assert (await dispatch(ctx_for(db, owner), "candidates.match", {"candidate_id": c2.id, "request_id": r2.id, "force": True})).data["evaluated"] == 0
    # already-purchased truck recorded manually: purchase evidence, no bid AZKT performed
    contact3 = await make_contact(db, "Hal Handled")
    r3 = await active_request(db, owner, contact3, [MUST_MANUAL], title="Hal")
    res = await dispatch(ctx_for(db, owner), "import_requests.record_purchase", {
        "request_id": r3.id, "vehicle": {"make": "Suzuki", "model": "Carry", "model_year": 2017, "frame_no_raw": f"DA16T-{uid()}"},
        "evidence": {"source_ref": "invoice-44", "amount": "5200", "currency": "USD", "purchased_at": (NOW - timedelta(days=1)).isoformat()}})
    await db.refresh(r3)
    assert res.data["recorded"] is True and res.data["bid_id"] is None
    assert r3.status == "purchased" and r3.purchase_evidence["manual"] is True and r3.purchase_evidence["bid_id"] is None
    assert (await db.execute(select(Bid).where(Bid.import_request_id == r3.id))).scalars().all() == []
    v3 = await db.get(Vehicle, res.data["vehicle_id"])
    assert v3.origin_candidate_id is None and v3.purchase_evidence["source_ref"] == "invoice-44"
    rep = await dispatch(ctx_for(db, owner), "import_requests.record_purchase", {"request_id": r3.id, "vehicle_id": v3.id,
                                                                                  "evidence": {"source_ref": "invoice-44"}})
    assert rep.data["idempotent"] is True
    with pytest.raises(Conflict):
        await dispatch(ctx_for(db, owner), "import_requests.record_purchase", {"request_id": r3.id, "vehicle_id": vid,
                                                                                "evidence": {"source_ref": "invoice-45"}})
    with pytest.raises(Denied):
        await dispatch(ctx_for(db, await _manager(db)), "import_requests.record_purchase", {"request_id": r3.id, "vehicle_id": v3.id,
                                                                                            "evidence": {"source_ref": "invoice-44"}})


# ── API surface ─────────────────────────────────────────────────────────────
async def test_import_request_and_candidate_routes(client, db, owner):
    contact = await make_contact(db, "Ivy Interface")
    r = await active_request(db, owner, contact, [MUST_MANUAL, MUST_AC, PREFER_YEAR], title="Ivy route")
    (c,) = await ingest(db, owner, raw_candidate(transmission="manual", year=2019, mileage_km=40000))
    m = await match_for(db, owner, r, c)
    b = await prepared_bid(db, owner, c, r)
    login(client, owner)
    res = await client.get("/api/import-requests", params={"status": "active_search"})
    assert res.status_code == 200 and res.json()["total"] >= 1 and "items" in res.json()
    row = next(x for x in res.json()["items"] if x["id"] == r.id)
    assert row["candidate_counts"]["candidates"] >= 1 and row["paused"] is False and row["lifecycle"][0] == "inquiry"
    res = await client.get(f"/api/import-requests/{r.id}")
    body = res.json()
    assert body["request"]["requirement_tiers"]["must"] and body["request"]["gates"]["decision"] == "Allowed"
    cand = next(x for x in body["candidates"] if x["id"] == m.id)
    assert {ch["key"]: ch["result"] for ch in cand["checks"]}["ac"] == "unknown" and cand["decision"] == "Needs review"
    assert cand["candidate"]["deadline_at"]["tokyo"] and cand["candidate"]["deadline_at"]["phoenix"]
    assert body["bids"][0]["max_amount"] == "450000"
    res = await client.get(f"/api/candidates/{c.id}")
    body = res.json()
    assert body["candidate"]["auction_at"]["tokyo"].endswith("JST") and body["candidate"]["auction_at"]["phoenix"].endswith("AZ")
    assert body["checks"][0]["request"]["id"] == r.id and body["bids"][0]["gate"]["decision"] == "Blocked"
    res = await client.post(f"/api/import-requests/{r.id}/pause", json={"reason": "buyer travelling"})
    assert res.status_code == 200 and res.json()["status"] == "ok" and res.json()["data"]["request"]["paused"] is True
    res = await client.post("/api/candidates/ingest", json={"candidates": [raw_candidate(transmission="manual", ac=True)], "match": False})
    assert res.status_code == 200 and res.json()["data"]["created"] == 1
    # money hidden for a manager (no costs.read): bid max / budget are stripped, deadline still shown
    mgr = await _manager(db)
    login(client, mgr)
    res = await client.get(f"/api/import-requests/{r.id}")
    body = res.json()
    assert body["request"]["money_hidden"] is True and body["request"]["budget_amount"] is None
    assert body["bids"][0]["max_amount"] is None and body["bids"][0]["deadline_at"]["tokyo"]
    res = await client.get("/api/candidates", params={"request_id": r.id})
    assert res.status_code == 200 and any(x["id"] == c.id for x in res.json()["items"])
