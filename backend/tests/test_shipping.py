"""Shipping acceptance: shipments/legs/milestones (K04 shape, container-wide vs per-vehicle exceptions, sourced
deadlines) and the adaptive Montway quote case (G06 scope, G07 durable waiting + ambiguous reply, G08 weak comparison
and separate forward/booking approvals)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from backend.app.core.errors import Blocked, Denied
from backend.app.domain.commands import CommandContext, dispatch
from backend.app.models.contacts import Contact
from backend.app.models.runtime import Approval, ExternalAction, Permission
from backend.app.models.shipping import ShipmentLeg, ShipmentQuote
from backend.app.models.tasks import Case, Task
from backend.app.models.vehicles import Vehicle, VehicleMilestone
from backend.tests.conftest import actor_of, ctx_for, login, make_user, run_worker_once

NOW = datetime.now(timezone.utc)
MONTWAY = "quotes@montway.example"


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


async def make_contact(db, name: str, role: str = "buyer") -> Contact:
    c = Contact(name=name, roles=[role], status="active", search_text=name.lower(), primary_email=f"{uid()}@example.com",
                extra={"delivery_address": "Mesa, AZ 85201"})
    db.add(c)
    await db.commit()
    return c


async def make_vehicle(db, owner, *, buyer: Contact | None = None, dims: bool = True, operability: str | None = "running") -> Vehicle:
    extra = {}
    if dims:
        extra.update({"length_mm": 3395, "width_mm": 1475, "height_mm": 1800, "weight_kg": 800})
    if operability:
        extra["operability"] = operability
    res = await dispatch(ctx_for(db, owner), "vehicles.create", {"title": f"2019 Daihatsu Hijet {uid()}", "make": "Daihatsu", "model": "Hijet",
                                                                  "model_year": 2019, "frame_no_raw": f"S510P-{uid()}", "logistics_state": "on_vessel",
                                                                  "source_kind": "document", "source_ref": "auction-sheet", "extra": extra,
                                                                  "create_missing_task": False})
    vid = res.data["vehicle"]["id"]
    if buyer is not None:
        await dispatch(ctx_for(db, owner), "vehicles.update", {"vehicle_id": vid, "buyer_contact_id": buyer.id, "allocation": "reserved"})
    return await db.get(Vehicle, vid)


async def make_shipment(db, owner, vehicles: list[Vehicle], **kw) -> dict:
    res = await dispatch(ctx_for(db, owner), "shipments.create", {"ref": f"SHP-{uid()}", "vehicle_ids": [v.id for v in vehicles],
                                                                   "container_no": "TCLU1234567", "vessel": "Morning Star", "voyage": "042E",
                                                                   "route_from": "Yokohama", "route_to": "Long Beach", **kw})
    return res.data["shipment"]


async def _manager(db):
    from backend.app.models import User
    u = (await db.execute(select(User).where(User.handle == "luis"))).scalar_one_or_none()
    return u or await make_user(db, "luis", "manager", display_name="Luis")


async def _logistics(db):
    from backend.app.models import User
    u = (await db.execute(select(User).where(User.handle == "lena"))).scalar_one_or_none()
    return u or await make_user(db, "lena", "logistics", display_name="Lena")


async def approve(db, owner, approval_id: str) -> dict:
    return (await dispatch(ctx_for(db, owner), "approvals.approve", {"approval_id": approval_id})).data


async def started_case(db, owner, buyer: Contact, vehicle: Vehicle, *, service: str | None = "open") -> ShipmentQuote:
    res = await dispatch(ctx_for(db, owner), "quotes.start_case", {
        "vehicle_id": vehicle.id, "origin": "Port of Long Beach, CA", "destination": "Mesa, AZ 85201", "service": service,
        "vendor_name": "Montway", "timing": {"earliest": (NOW + timedelta(days=7)).isoformat(), "source": "customer message m-1"}})
    return await db.get(ShipmentQuote, res.data["quote"]["id"])


async def requested(db, owner, q: ShipmentQuote, recipients: list[str] | None = None) -> ShipmentQuote:
    res = await dispatch(ctx_for(db, owner), "quotes.request", {"quote_id": q.id, "recipients": recipients or [MONTWAY], "channel": "email",
                                                                  "message": "Please quote the kei truck below (nonbinding)."})
    if res.status == "needs_review":  # exact approval (no standing permission covers the recipients)
        out = await approve(db, owner, res.approval_id)
        assert out["executed"] is True
    else:
        assert res.status == "ok"
    await db.refresh(q)
    return q


# ── K04 shape / milestones ──────────────────────────────────────────────────
async def test_K04_milestones_planned_estimated_completed_with_source_and_exceptions(db, owner):
    v1, v2 = await make_vehicle(db, owner), await make_vehicle(db, owner)
    s = await make_shipment(db, owner, [v1, v2], eta_at=(NOW + timedelta(days=10)).isoformat(), eta_source="carrier notice CN-1")
    sid = s["id"]
    # same ref again -> the existing shipment (retry-safe creation)
    again = await dispatch(ctx_for(db, owner), "shipments.create", {"ref": s["ref"], "vehicle_ids": [v1.id]})
    assert again.data["created"] is False and again.data["shipment"]["id"] == sid
    # planned (no date) / estimated (sourced date) / completed (sourced past date)
    res = await dispatch(ctx_for(db, owner), "shipments.record_milestone", {"shipment_id": sid, "kind": "vessel_arrival", "status": "planned"})
    assert res.data["milestone"]["status"] == "planned" and res.data["milestone"]["at"] == {"utc": None, "phoenix": "Not recorded", "tokyo": "Not recorded"}
    with pytest.raises(Blocked):  # an estimate needs a time; a sourced one needs its reference
        await dispatch(ctx_for(db, owner), "shipments.record_milestone", {"shipment_id": sid, "kind": "vessel_arrival", "status": "estimated", "source_kind": "carrier"})
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "shipments.record_milestone", {"shipment_id": sid, "kind": "vessel_arrival", "status": "estimated",
                                                                           "at": (NOW + timedelta(days=9)).isoformat(), "source_kind": "carrier"})
    res = await dispatch(ctx_for(db, owner), "shipments.record_milestone", {"shipment_id": sid, "kind": "vessel_arrival", "status": "estimated",
                                                                             "at": (NOW + timedelta(days=9)).isoformat(), "source_kind": "carrier", "source_ref": "CN-2"})
    m = res.data["milestone"]
    assert m["status"] == "estimated" and m["source_kind"] == "carrier" and m["source_ref"] == "CN-2" and m["at"]["utc"] and m["at"]["phoenix"].endswith("AZ")
    assert m["supersedes_id"] and m["applies_to"] == "container"
    arrived = NOW - timedelta(days=1)
    res = await dispatch(ctx_for(db, owner), "shipments.record_milestone", {"shipment_id": sid, "kind": "vessel_arrival", "status": "completed",
                                                                             "at": arrived.isoformat(), "source_kind": "port", "source_ref": "port-notice-9"})
    assert res.data["affected_vehicle_ids"] == [v1.id, v2.id] and res.data["milestone"]["status"] == "completed"
    # bridged to the vehicle timeline through the vehicles domain command (one current milestone per vehicle)
    assert all(res.data["bridged"][vid]["bridged"] for vid in (v1.id, v2.id))
    for v in (v1, v2):
        rows = (await db.execute(select(VehicleMilestone).where(VehicleMilestone.vehicle_id == v.id, VehicleMilestone.kind == "arrived_port",
                                                                VehicleMilestone.is_current.is_(True)))).scalars().all()
        assert len(rows) == 1 and rows[0].status == "completed" and rows[0].source_ref == "port-notice-9" and rows[0].shipment_id == sid
        await db.refresh(v)
        assert v.logistics_state == "at_port"
    # per-vehicle exception survives a later container-wide notice
    res = await dispatch(ctx_for(db, owner), "shipments.record_milestone", {"shipment_id": sid, "kind": "release", "status": "completed", "vehicle_id": v1.id,
                                                                             "at": (NOW - timedelta(hours=5)).isoformat(), "source_kind": "customs", "source_ref": "entry-1"})
    assert res.data["milestone"]["exception"] is True and res.data["affected_vehicle_ids"] == [v1.id]
    res = await dispatch(ctx_for(db, owner), "shipments.record_milestone", {"shipment_id": sid, "kind": "release", "status": "estimated",
                                                                             "at": (NOW + timedelta(days=2)).isoformat(), "source_kind": "port", "source_ref": "hold-3"})
    assert res.data["affected_vehicle_ids"] == [v2.id] and res.data["preserved_exceptions"] == [v1.id]
    eff = res.data["effective"]["per_vehicle"]
    assert eff[v1.id]["release"]["status"] == "completed" and eff[v1.id]["release"]["scope"] == "vehicle"
    assert eff[v2.id]["release"]["status"] == "estimated" and eff[v2.id]["release"]["scope"] == "container"
    assert eff[v2.id]["received"] == {"kind": "received", "status": "not_recorded", "at": {"utc": None, "phoenix": "Not recorded", "tokyo": "Not recorded"},
                                      "source_kind": None, "source_ref": None, "scope": None}
    # repeated identical notice is idempotent (no new row)
    rep = await dispatch(ctx_for(db, owner), "shipments.record_milestone", {"shipment_id": sid, "kind": "release", "status": "estimated",
                                                                             "at": (NOW + timedelta(days=2)).isoformat(), "source_kind": "port", "source_ref": "hold-3"})
    assert rep.data["created"] is False and rep.data["idempotent"] is True
    # storage deadline requires source evidence
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "shipments.set_storage_deadline", {"shipment_id": sid, "at": (NOW + timedelta(days=4)).isoformat(),
                                                                               "source_kind": "manual", "source_ref": "guess"})
    res = await dispatch(ctx_for(db, owner), "shipments.set_storage_deadline", {"shipment_id": sid, "at": (NOW + timedelta(days=4)).isoformat(),
                                                                                 "source_kind": "port", "source_ref": "free-time-notice-2"})
    assert res.data["shipment"]["storage_deadline"]["phoenix"].endswith("AZ") and res.data["shipment"]["storage_deadline_source_ref"] == "free-time-notice-2"
    # legs: add / update; booked only through quotes.book
    leg = (await dispatch(ctx_for(db, owner), "shipments.add_leg", {"shipment_id": sid, "kind": "domestic", "vehicle_id": v1.id})).data["leg"]
    with pytest.raises(Exception):
        await dispatch(ctx_for(db, owner), "shipments.update_leg", {"leg_id": leg["id"], "status": "booked"})
    with pytest.raises(Blocked):
        await dispatch(ctx_for(db, owner), "shipments.update_leg", {"leg_id": leg["id"], "status": "complete"})
    res = await dispatch(ctx_for(db, owner), "shipments.update_leg", {"leg_id": leg["id"], "status": "complete", "evidence": [{"kind": "photo", "asset_id": "a1"}],
                                                                       "delivered_at": (NOW - timedelta(hours=1)).isoformat()})
    assert res.data["leg"]["status"] == "complete"


# ── G06 ─────────────────────────────────────────────────────────────────────
async def test_G06_manual_route_keeps_scope_and_new_recipient_needs_new_approval(db, owner):
    buyer = await make_contact(db, "Jane Journey")
    v = await make_vehicle(db, owner, buyer=buyer)
    q = await started_case(db, owner, buyer, v)
    assert q.status == "draft" and q.needs_information == [] and q.request_payload["vehicle"]["title"] == v.title
    assert q.request_payload["dimensions"]["length_mm"]["value"] == 3395 and q.operability == "running" and q.size_class == "kei_truck"
    case = await db.get(Case, q.case_id)
    assert case.kind == "shipping_quote" and case.vehicle_id == v.id
    # approved standing permission: Montway's manual email route only
    db.add(Permission(subject_kind="workflow", action_pattern="quotes.request", recipients=[MONTWAY], status="active",
                      fields=["vehicle", "dimensions", "operability", "route_from", "route_to", "timing"],
                      authorized_by=owner.id, description="Montway manual quote route"))
    await db.commit()
    res = await dispatch(ctx_for(db, owner), "quotes.request", {"quote_id": q.id, "recipients": [MONTWAY], "channel": "email",
                                                                  "message": "Web form has no kei-truck preset; quoting by email."})
    assert res.status == "ok" and any("standing permission" in r for r in res.decision["reasons"])
    shared = res.data["shared"]
    # exactly the recorded vehicle and data, no substitute model, nothing invented
    assert shared["vehicle"]["title"] == v.title and shared["vehicle"]["frame_no"] == v.frame_no_raw and set(shared) <= {"vehicle", "dimensions", "operability", "route_from", "route_to", "timing"}
    assert "buyer" not in shared and res.data["sent"] is False
    await db.refresh(q)
    assert q.status == "requested" and q.recipients == [MONTWAY] and q.next_check_at is not None
    act = await db.get(ExternalAction, q.request_action_id)
    assert act.provider == "email" and act.state == "intent" and act.payload["data"]["vehicle"]["title"] == v.title
    # a fallback that adds a recipient is outside the permission: needs a new approval, nothing changes until then
    res = await dispatch(ctx_for(db, owner), "quotes.request", {"quote_id": q.id, "recipients": [MONTWAY, "dispatch@othercarrier.example"], "channel": "email",
                                                                  "message": "Web form has no kei-truck preset; quoting by email."})
    assert res.status == "needs_review" and res.approval_id
    await db.refresh(q)
    assert q.recipients == [MONTWAY] and q.request_action_id == act.id
    a = await db.get(Approval, res.approval_id)
    assert a.targets["recipients"] == [MONTWAY, "dispatch@othercarrier.example"] and a.consequence["fields_shared"]
    # sharing extra data (the buyer) with the permitted recipient is outside the permission's data scope: structured block
    with pytest.raises(Blocked) as e:
        await dispatch(ctx_for(db, owner), "quotes.request", {"quote_id": q.id, "recipients": [MONTWAY], "channel": "email",
                                                                "fields": ["vehicle", "dimensions", "operability", "route_from", "route_to", "timing", "buyer"]})
    assert e.value.detail["fields_outside_scope"] == ["buyer"] and e.value.detail["decision"] == "Needs review"
    # ... and with a recipient the permission does not cover, the same extra data goes to exact review as a new approval
    res = await dispatch(ctx_for(db, owner), "quotes.request", {"quote_id": q.id, "recipients": [MONTWAY, "dispatch@othercarrier.example"], "channel": "email",
                                                                  "fields": ["vehicle", "dimensions", "operability", "route_from", "route_to", "timing", "buyer"]})
    assert res.status == "needs_review" and res.approval_id != a.id
    # data that is not recorded can never be shared
    v2 = await make_vehicle(db, owner, buyer=buyer, dims=False, operability=None)
    q2 = await started_case(db, owner, buyer, v2, service=None)
    assert q2.status == "needs_information" and {n["field"] for n in q2.needs_information} >= {"length_mm", "operability", "service"}
    with pytest.raises(Blocked) as e:
        await dispatch(ctx_for(db, owner), "quotes.request", {"quote_id": q2.id, "recipients": [MONTWAY]})
    assert "dimensions" in e.value.detail["missing"]
    # the approved scope change executes with the new recipient set and a new intent; still no booking
    out = await approve(db, owner, a.id)
    assert out["executed"] is True
    await db.refresh(q)
    assert q.recipients == [MONTWAY, "dispatch@othercarrier.example"] and q.request_action_id != act.id and q.status == "requested"
    assert (await db.execute(select(ShipmentLeg).where(ShipmentLeg.status == "booked", ShipmentLeg.vehicle_id == v.id))).scalars().all() == []
    # a manager cannot use the owner's exact approval path for a vendor send without permission scope: consequential needs review
    res = await dispatch(ctx_for(db, await _manager(db)), "quotes.request", {"quote_id": q.id, "recipients": ["x@vendor.example"]})
    assert res.status == "needs_review"
    # revoke the standing permission so later cases go through exact approval again
    perm = (await db.execute(select(Permission).where(Permission.action_pattern == "quotes.request", Permission.status == "active"))).scalars().first()
    perm.status = "revoked"
    perm.revoked_at = NOW
    await db.commit()


# ── G07 ─────────────────────────────────────────────────────────────────────
async def test_G07_case_waits_across_worker_restart_and_ambiguous_reply_clarifies(db, owner):
    buyer = await make_contact(db, "Kim Keeps")
    v = await make_vehicle(db, owner, buyer=buyer)
    q = await requested(db, owner, await started_case(db, owner, buyer, v))
    assert q.status == "requested" and q.next_check_at > NOW + timedelta(days=1)
    case = await db.get(Case, q.case_id)
    await db.refresh(case)
    assert case.status == "waiting" and case.waiting_on == "vendor reply" and case.next_check_at == q.next_check_at
    # the worker runs (as after a restart): the intent executes into a manual sending task, never claims delivery; the case keeps waiting
    await run_worker_once()
    await db.refresh(q)
    await db.refresh(case)
    act = await db.get(ExternalAction, q.request_action_id)
    await db.refresh(act)
    assert act.state == "handed_off" and act.receipt["sent"] is False and act.receipt["state"] == "manual_send_required"
    send_task = await db.get(Task, act.receipt["task_id"])
    assert send_task is not None and v.title in send_task.instructions and MONTWAY in send_task.notes
    assert q.status == "requested" and q.next_check_at is not None and case.status == "waiting" and case.next_check_at is not None
    # an ambiguous reply covering two vehicles -> clarifying with a task, no price accepted
    res = await dispatch(ctx_for(db, owner), "quotes.record_reply", {
        "vehicle_id": v.id, "from_addr": MONTWAY, "message_id": "montway-reply-1", "body": "Hijet $1,150 / Carry $1,250",
        "extracted": {"amount": "1150", "currency": "USD", "vehicle_refs": ["Hijet", "Carry"]}})
    assert res.data["clarifying"] is True
    await db.refresh(q)
    assert q.status == "clarifying" and q.amount is None and q.clarification_task_id
    task = await db.get(Task, q.clarification_task_id)
    assert "which vehicle" in task.title.lower() and task.status == "open"
    await db.refresh(case)
    assert case.status == "waiting" and case.waiting_on == "vendor clarification"
    # replaying the same message changes nothing; the clarified reply resumes the case
    rep = await dispatch(ctx_for(db, owner), "quotes.record_reply", {"quote_id": q.id, "message_id": "montway-reply-1", "extracted": {}})
    assert rep.data["idempotent"] is True
    res = await dispatch(ctx_for(db, owner), "quotes.record_reply", {
        "quote_id": q.id, "from_addr": MONTWAY, "message_id": "montway-reply-2",
        "extracted": {"amount": "1150", "currency": "USD", "scope": "door to door, open carrier", "inclusions": ["insurance"],
                      "exclusions": ["storage fees"], "timing": "5-7 days", "expires_at": (NOW + timedelta(days=14)).isoformat(), "vehicle_refs": ["Hijet"]}})
    assert res.data["clarifying"] is False
    await db.refresh(q)
    await db.refresh(case)
    assert q.status == "received" and q.amount == Decimal("1150.00") and q.currency == "USD" and q.inclusions == ["insurance"] and q.next_check_at is None
    assert case.status == "open" and "Compare" in case.next_action


# ── G08 ─────────────────────────────────────────────────────────────────────
async def _received_quote(db, owner, buyer: Contact, v: Vehicle, amount: str, *, route_to: str = "Mesa, AZ 85201") -> ShipmentQuote:
    res = await dispatch(ctx_for(db, owner), "quotes.start_case", {"vehicle_id": v.id, "origin": "Port of Long Beach, CA", "destination": route_to,
                                                                    "service": "open", "vendor_name": "Montway",
                                                                    "timing": {"earliest": (NOW + timedelta(days=7)).isoformat(), "source": "m-1"}})
    q = await db.get(ShipmentQuote, res.data["quote"]["id"])
    q = await requested(db, owner, q)
    await dispatch(ctx_for(db, owner), "quotes.record_reply", {"quote_id": q.id, "message_id": f"reply-{uid()}",
                                                                "extracted": {"amount": amount, "currency": "USD", "scope": "door to door",
                                                                              "expires_at": (NOW + timedelta(days=14)).isoformat()}})
    await db.refresh(q)
    assert q.status == "received"
    return q


async def test_G08_weak_comparison_labelled_forward_does_not_book_booking_needs_own_approval(db, owner):
    buyer = await make_contact(db, "Lou Ledger")
    v = await make_vehicle(db, owner, buyer=buyer)
    q = await _received_quote(db, owner, buyer, v, "1150", route_to="Flagstaff, AZ 86001")  # a route with no quote history
    res = await dispatch(ctx_for(db, owner), "quotes.compare", {"quote_id": q.id})
    cmp_ = res.data["comparison"]
    assert cmp_["comparable_count"] == 0 and cmp_["evidence"] == "weak" and "no comparable quotes on record" in cmp_["weakness"]
    assert "second quote" in cmp_["recommendation"].lower() and cmp_["threshold"] is None and cmp_["range"] is None
    # a quote on another route is not a comparable, and is listed with the reason it was excluded
    other = await _received_quote(db, owner, buyer, await make_vehicle(db, owner, buyer=buyer), "900", route_to="Tucson, AZ")
    res = await dispatch(ctx_for(db, owner), "quotes.compare", {"quote_id": q.id})
    assert res.data["comparison"]["comparable_count"] == 0
    assert any(x["quote_id"] == other.id and "different route" in x["excluded_because"] for x in res.data["comparison"]["excluded"])
    # forwarding needs its own approval and never books
    res = await dispatch(ctx_for(db, owner), "quotes.forward_to_customer", {"quote_id": q.id, "to": [buyer.primary_email],
                                                                              "body": "Montway quoted $1,150 door to door (nonbinding)."})
    assert res.status == "needs_review"
    fwd = await db.get(Approval, res.approval_id)
    assert fwd.kind == "send_message" and fwd.command_name == "quotes.forward_to_customer"
    out = await approve(db, owner, fwd.id)
    assert out["executed"] is True and out["result"]["data"]["booked"] is False
    await db.refresh(q)
    assert q.status == "forwarded" and q.booked_at is None and q.booking == {} and q.forward_action_id
    assert (await db.execute(select(ShipmentLeg).where(ShipmentLeg.vehicle_id == v.id, ShipmentLeg.status == "booked"))).scalars().all() == []
    # the forward approval cannot authorize a booking
    book = {"quote_id": q.id, "carrier_name": "Montway", "route_from": "Port of Long Beach, CA", "route_to": "Mesa, AZ 85201", "vehicle_id": v.id,
            "amount": "1150", "currency": "USD", "conditions": "open carrier, door to door, insurance included"}
    book["route_to"] = "Flagstaff, AZ 86001"
    await db.refresh(fwd)
    with pytest.raises(Denied):
        await dispatch(CommandContext(db=db, actor=actor_of(owner), approval=fwd), "quotes.book", book)
    # booking is its own exact approval; a mismatched amount is invalidated at execution time
    res = await dispatch(ctx_for(db, owner), "quotes.book", {**book, "amount": "1100"})
    assert res.status == "needs_review"
    out = await approve(db, owner, res.approval_id)
    assert out["executed"] is False and "does not match the quoted" in out["approval"]["invalidated_reason"]
    await db.refresh(q)
    assert q.status == "forwarded"
    res = await dispatch(ctx_for(db, owner), "quotes.book", book)
    assert res.status == "needs_review"
    ba = await db.get(Approval, res.approval_id)
    assert ba.kind == "booking" and ba.consequence["amount"] == "1150" and ba.consequence["moves_money"] is True
    out = await approve(db, owner, ba.id)
    assert out["executed"] is True and out["result"]["data"]["booked"] is True
    await db.refresh(q)
    assert q.status == "booked" and q.booking["carrier_name"] == "Montway" and q.booking["amount"] == "1150.00" and q.booking_approval_id == ba.id
    # no shipment linked yet: the booking is recorded on the quote; a leg appears once the vehicle is on a shipment
    await run_worker_once()
    act = await db.get(ExternalAction, q.booking_action_id)
    await db.refresh(act)
    assert act.receipt["sent"] is False


async def test_shipping_routes_and_money_visibility(client, db, owner):
    buyer = await make_contact(db, "Mia Money")
    v = await make_vehicle(db, owner, buyer=buyer)
    s = await make_shipment(db, owner, [v])
    await dispatch(ctx_for(db, owner), "shipments.record_milestone", {"shipment_id": s["id"], "kind": "vessel_arrival", "status": "estimated",
                                                                       "at": (NOW + timedelta(days=3)).isoformat(), "source_kind": "carrier", "source_ref": "CN-9"})
    leg = (await dispatch(ctx_for(db, owner), "shipments.add_leg", {"shipment_id": s["id"], "kind": "domestic", "vehicle_id": v.id})).data["leg"]
    l = await db.get(ShipmentLeg, leg["id"])
    l.amount, l.currency = Decimal("1150.00"), "USD"
    await db.commit()
    q = await _received_quote(db, owner, buyer, v, "1150")
    login(client, owner)
    res = await client.get("/api/shipments", params={"vehicle_id": v.id})
    assert res.status_code == 200 and res.json()["total"] == 1 and res.json()["items"][0]["latest_milestone"]["status"] == "estimated"
    res = await client.get(f"/api/shipments/{s['id']}")
    body = res.json()
    eff = body["milestones"]["effective"]["per_vehicle"][v.id]
    assert eff["vessel_arrival"]["status"] == "estimated" and eff["vessel_arrival"]["at"]["tokyo"].endswith("JST") and eff["received"]["status"] == "not_recorded"
    assert body["legs"][0]["amount"] == "1150.00" and body["vehicles"][0]["id"] == v.id
    res = await client.get(f"/api/shipping/quotes/{q.id}")
    body = res.json()
    assert body["quote"]["amount"] == "1150.00" and body["decisions"] == {"forwarded": False, "booked": False,
                                                                           "note": "Forwarding and booking are separate approvals; neither authorizes the other"}
    assert body["actions"]["request"]["state"] in ("intent", "confirmed", "handed_off") and body["case"]["status"] == "open"
    res = await client.post("/api/shipping/quotes/start", json={"vehicle_id": v.id, "destination": "Mesa, AZ", "service": "open"})
    assert res.status_code == 200 and res.json()["status"] == "ok"
    res = await client.post(f"/api/shipments/{s['id']}/record-milestone", json={"kind": "discharge", "status": "planned"})
    assert res.status_code == 200 and res.json()["data"]["milestone"]["status"] == "planned"
    res = await client.post(f"/api/shipments/{s['id']}/set-storage-deadline", json={"at": (NOW + timedelta(days=4)).isoformat(), "source_kind": "manual", "source_ref": "x"})
    assert res.status_code == 409 and res.json()["error"] == "blocked"
    # a logistics person (no costs.read) sees the shipment but not the money
    login(client, await _logistics(db))
    res = await client.get(f"/api/shipments/{s['id']}")
    assert res.status_code == 200 and res.json()["legs"][0]["amount"] is None and res.json()["legs"][0]["money_hidden"] is True
    res = await client.get(f"/api/shipping/quotes/{q.id}")
    assert res.json()["quote"]["amount"] is None and res.json()["quote"]["money_hidden"] is True
    # a mechanic (no shipping.read) is refused
    login(client, await make_user(db, f"mech{uid()}", "mechanic"))
    assert (await client.get("/api/shipments")).status_code == 403
