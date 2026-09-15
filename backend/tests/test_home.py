"""The Home aggregate API (spec §2.2, §2.4).

K04 vehicle timeline: actual vs estimated milestones, an unrecorded inspection, correct stage age, an open
shipment on a sold truck, and turnaround endpoints excluded with a count.
K05 role safety and stale/computing states on Home, with no model dependency anywhere on this path.
H11 a stale connection never produces a false all-clear while the deterministic lists keep working.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from backend.app.core.time import PHOENIX
from backend.app.domain.commands import dispatch
from backend.app.models.comms import Connection
from backend.app.models.contacts import Contact
from backend.app.models.vehicles import MILESTONE_KINDS, Vehicle
from backend.app.services import connections as conns
from backend.app.services import home as home_svc
from backend.app.services import timeline as tl
from backend.tests.conftest import actor_of, ctx_for, login, run_worker_once

NOW = datetime.now(timezone.utc)
REQUIRED = ("gmail_business", "square", "sheets", "drive")


@pytest_asyncio.fixture(autouse=True, scope="module")
async def _drain_outbox_after_module():
    yield
    for _ in range(50):
        out = await run_worker_once()
        if not out.get("events") and not out.get("jobs"):
            break


def _u() -> str:
    return uuid.uuid4().hex[:8]


async def cmd(db, user, name: str, payload: dict, **kw):
    return await dispatch(ctx_for(db, user, **kw), name, payload)


async def contact(db) -> Contact:
    name = f"Buyer {_u()}"
    c = Contact(name=name, roles=["buyer"], status="active", search_text=name.lower(), primary_email=f"{_u()}@example.com")
    db.add(c)
    await db.commit()
    return c


async def vehicle(db, **kw) -> Vehicle:
    stock = kw.pop("stock_no", f"HOM-{_u()[:5].upper()}")
    v = Vehicle(stock_no=stock, title=stock, make="Suzuki", model="Carry", model_year=2003, **kw)
    db.add(v)
    await db.commit()
    await db.refresh(v)
    return v


# ── K04 ──────────────────────────────────────────────────────────────────────
@pytest_asyncio.fixture
async def en_route_sale(db, owner):
    """Purchased and received are actual; the inspection was never recorded; delivery is an estimate that was
    revised once; the truck is sold while its shipment is still open."""
    c = await contact(db)
    v = await vehicle(db, acquired_at=NOW - timedelta(days=60), logistics_state="purchased")
    await cmd(db, owner, "vehicles.record_milestone", {"vehicle_id": v.id, "kind": "purchased", "status": "completed",
                                                       "at": (NOW - timedelta(days=60)).isoformat(),
                                                       "source_kind": "owner_reported", "source_ref": f"auction-{_u()}"})
    ship = (await cmd(db, owner, "shipments.create", {"vehicle_ids": [v.id], "vessel": "MV Kei", "voyage": "V1",
                                                      "route_from": "Yokohama", "route_to": "Los Angeles",
                                                      "eta_at": (NOW + timedelta(days=4)).isoformat(),
                                                      "eta_source": "carrier notice"})).data["shipment"]
    await cmd(db, owner, "shipments.record_milestone", {"shipment_id": ship["id"], "kind": "vessel_departed",
                                                        "status": "completed", "at": (NOW - timedelta(days=20)).isoformat(),
                                                        "source_kind": "exporter", "source_ref": f"notice-{_u()}"})
    # a revised (superseded) arrival estimate: the first notice is kept in history, not overwritten silently
    await cmd(db, owner, "shipments.record_milestone", {"shipment_id": ship["id"], "kind": "vessel_arrival",
                                                        "status": "estimated", "at": (NOW + timedelta(days=9)).isoformat(),
                                                        "source_kind": "carrier", "source_ref": f"eta1-{_u()}"})
    await cmd(db, owner, "shipments.record_milestone", {"shipment_id": ship["id"], "kind": "vessel_arrival",
                                                        "status": "estimated", "at": (NOW + timedelta(days=4)).isoformat(),
                                                        "source_kind": "carrier", "source_ref": f"eta2-{_u()}"})
    await cmd(db, owner, "vehicles.record_milestone", {"vehicle_id": v.id, "kind": "delivered", "status": "estimated",
                                                       "at": (NOW + timedelta(days=5)).isoformat(),
                                                       "source_kind": "manual", "note": "internal target"})
    s = (await cmd(db, owner, "sales.reserve", {"vehicle_id": v.id, "buyer_contact_id": c.id})).data["sale"]
    await cmd(db, owner, "sales.agree", {"sale_id": s["id"], "price": "9000.00", "currency": "USD"})
    sale = (await cmd(db, owner, "sales.mark_completed", {"sale_id": s["id"], "completed_at": NOW.isoformat(),
                                                          "source_ref": f"bos-{_u()}"})).data["sale"]
    await cmd(db, owner, "tasks.create", {"title": "Collect the export certificate", "vehicle_id": v.id,
                                          "type": "operational", "due_at": (NOW + timedelta(days=2)).isoformat()})
    blocked = (await cmd(db, owner, "tasks.create", {"title": "Book domestic carrier", "vehicle_id": v.id,
                                                     "type": "operational"})).data["task"]
    await cmd(db, owner, "tasks.report_blocker", {"task_id": blocked["id"], "reason": "no release date yet"})
    return {"vehicle_id": v.id, "shipment_id": ship["id"], "sale_id": sale["id"]}


async def test_K04_timeline_shows_actual_versus_estimated_and_never_invents_a_date(db, owner, en_route_sale):
    out = await tl.timeline(db, actor_of(owner), vehicle_ids=[en_route_sale["vehicle_id"]], horizon_days=7)
    item = out["items"][0]
    kinds = {m["kind"]: m for m in item["milestones"]}
    assert list(kinds) == list(MILESTONE_KINDS)

    purchased = kinds["purchased"]
    assert purchased["status"] == "completed" and purchased["actual"] is True
    assert purchased["source"]["kind"] == "owner_reported" and purchased["source"]["ref"]
    assert purchased["at"] and purchased["recorded"] is True

    on_vessel = kinds["on_vessel"]                      # bridged from the shipment notice, with its source
    assert on_vessel["status"] == "completed" and on_vessel["source"]["kind"] == "exporter"
    assert on_vessel["origin"] == "shipment" and on_vessel["source"]["shipment_id"] == en_route_sale["shipment_id"]

    inspected = kinds["inspected"]                      # never recorded: honest, not guessed
    assert inspected["status"] == "not_recorded" and inspected["at"] is None
    assert inspected["at_label"] == "Not recorded" and inspected["recorded"] is False

    delivered = kinds["delivered"]
    assert delivered["status"] == "estimated" and delivered["actual"] is False and delivered["at"]

    arrived = kinds["arrived_port"]                     # an estimate that has not happened is not an actual
    assert arrived["status"] == "not_recorded"
    assert arrived["shipment_expectation"]["status"] == "estimated"
    assert arrived["shipment_expectation"]["at"]

    assert item["days_since_acquisition"] == 60
    assert item["time_in_current_stage"]["days"] is not None
    assert item["current_stage"]["state"] == item["states"]["recon"]
    assert "no invented" in " ".join(out["notes"])
    assert all(m["at"] is None for m in item["milestones"] if m["status"] == "not_recorded")
    assert not any("percent" in str(m) for m in item["milestones"])


async def test_K04_sold_while_en_route_keeps_the_open_shipment_and_blocked_work(db, owner, en_route_sale):
    out = await tl.timeline(db, actor_of(owner), vehicle_ids=[en_route_sale["vehicle_id"]], horizon_days=7)
    item = out["items"][0]
    assert item["states"]["commercial"] == "sold"
    assert item["states"]["logistics"] == "on_vessel"          # independent dimensions
    assert item["shipment_open_while_sold"] is True
    assert item["open_shipments"] and item["open_shipments"][0]["id"] == en_route_sale["shipment_id"]
    assert item["open_shipments"][0]["eta_source"] == "carrier notice"
    titles = {b.get("title") for b in item["blocked_work"]}
    assert "Book domestic carrier" in titles
    assert any(b.get("block_reason") == "no release date yet" for b in item["blocked_work"])
    # upcoming events inside the horizon are sourced or labelled internal targets — never invented
    bases = {u["basis"] for u in item["upcoming"]}
    assert bases and bases <= {"sourced", "internal target", "estimated milestone", "planned milestone",
                               "estimated shipment notice", "planned shipment notice"}
    assert any(u["kind"] == "shipment_eta" and u["basis"] == "sourced" for u in item["upcoming"])
    assert any(u["kind"] == "task" and u["basis"] == "internal target" for u in item["upcoming"])
    assert all(u["at"] <= (NOW + timedelta(days=8)).isoformat() for u in item["upcoming"])
    # blocked_only and vehicle filters need no second timeline store
    only = await tl.timeline(db, actor_of(owner), blocked_only=True, horizon_days=7)
    assert en_route_sale["vehicle_id"] in {i["vehicle_id"] for i in only["items"]}


async def test_K04_missing_turnaround_endpoints_are_excluded_with_a_count(db, owner, en_route_sale):
    out = await tl.timeline(db, actor_of(owner), vehicle_ids=[en_route_sale["vehicle_id"]], horizon_days=30)
    item = out["items"][0]
    assert item["turnaround"]["acquisition_to_sale"]["days"] == 60
    assert item["turnaround"]["received_to_ready"]["days"] is None
    assert item["turnaround"]["received_to_ready"]["excluded"] is True
    assert set(item["turnaround"]["received_to_ready"]["missing_endpoints"]) == {"received_at", "ready_at"}
    assert out["turnaround_excluded"]["received_to_ready"] >= 1
    assert out["turnaround_excluded"]["acquisition_to_sale"] == 0


async def test_K04_historical_period_view_does_not_hide_current_work(db, owner, en_route_sale):
    out = await tl.timeline(db, actor_of(owner), vehicle_ids=[en_route_sale["vehicle_id"]], horizon_days=7,
                            period_from=NOW - timedelta(days=90), period_to=NOW - timedelta(days=10))
    item = out["items"][0]
    done = {m["kind"] for m in item["period_view"]["completed"]}
    assert "purchased" in done and "sold" not in done        # sold happened after the window
    assert item["blocked_work"] and item["next_task"]         # current urgent work is never filtered away
    assert item["upcoming"]


# ── K05 ──────────────────────────────────────────────────────────────────────
async def test_K05_owner_sees_metrics_and_a_mechanic_cannot_query_financial_aggregates(client, db, owner, mechanic):
    login(client, owner)
    r = await client.get("/api/home", params={"period": "month", "tz": PHOENIX})
    assert r.status_code == 200
    page = r.json()
    assert page["sections"] == ["status", "business_overview", "needs_decision", "needs_attention", "today",
                                "vehicle_timeline", "in_progress", "completed"]
    bo = page["business_overview"]
    assert bo["available"] is True and bo["money_hidden"] is False
    assert bo["period"]["timezone"] == PHOENIX and bo["currency"] == "USD" and bo["as_of"]
    assert "stale" in bo and bo["completeness"]["label"] in ("Recorded", "Estimated")
    assert [v["label"] for v in bo["summary_row"]] == ["Vehicles sold", "Vehicle costs (sold cohort)",
                                                       bo["summary_row"][2]["label"], "Days to sale"]
    assert page["model_used"] is False and page["status"]["model_used"] is False

    login(client, mechanic)
    r = await client.get("/api/home")
    assert r.status_code == 200
    page = r.json()
    assert page["business_overview"]["available"] is False
    assert "costs.read" in page["business_overview"]["reason"]
    assert page["business_overview"].get("values") is None
    assert page["needs_decision"]["available"] is False and page["needs_decision"]["items"] == []
    assert '"amount"' not in str(page["business_overview"])
    assert (await client.get("/api/home/metrics")).status_code == 403
    assert (await client.get("/api/home/metrics/drilldown", params={"metric": "gross_profit"})).status_code == 403


async def test_K05_manager_sees_counts_and_timing_but_no_amounts(client, db, manager):
    login(client, manager)
    r = await client.get("/api/home/metrics", params={"period": "month"})
    assert r.status_code == 200
    m = r.json()
    assert m["money_hidden"] is True and m["visible"] == "counts and timing only"
    assert m["values"]["gross_profit"]["value"] is None and m["values"]["net_vehicle_sales_value"]["value"] is None
    assert isinstance(m["values"]["vehicles_sold"]["count"], int)
    assert "days_to_sale" in m["values"] and "median_days" in m["values"]["days_to_sale"]
    dd = await client.get("/api/home/metrics/drilldown", params={"metric": "days_to_sale", "period": "month"})
    assert dd.status_code == 200 and dd.json()["money_hidden"] is True


async def test_K05_drilldown_opens_finance_with_the_same_cohort(client, db, owner):
    login(client, owner)
    r = await client.get("/api/home/metrics/drilldown", params={"metric": "vehicle_costs_sold_cohort",
                                                                "period": "custom", "start": "2019-05-01",
                                                                "end": "2019-05-31"})
    assert r.status_code == 200
    dd = r.json()
    fin = await client.get("/api/finance/sold-cohort", params={"from": "2019-05-01", "to": "2019-05-31"})
    assert fin.status_code == 200
    assert sorted(s["vehicle_id"] for s in fin.json()["sales"]) == dd["vehicle_ids"]
    assert dd["finance_filters"]["path"] == "/api/finance/sold-cohort"


async def test_K05_timeline_endpoint_applies_record_scope(client, db, owner, mechanic):
    login(client, mechanic)
    r = await client.get("/api/home/timeline", params={"horizon_days": 7})
    assert r.status_code == 200
    assert r.json()["items"] == []                         # nothing is assigned to this mechanic
    login(client, owner)
    r = await client.get("/api/home/timeline", params={"horizon_days": 30, "compact": True, "limit": 5})
    assert r.status_code == 200 and r.json()["horizon_days"] == 30
    bad = await client.get("/api/home/timeline", params={"view": "nope"})
    assert bad.status_code == 422


# ── H11 ──────────────────────────────────────────────────────────────────────
@pytest_asyncio.fixture
async def connection_rows(db):
    """Take control of the four required connections, then restore them."""
    saved = []
    rows = {}
    for provider in REQUIRED:
        row = await conns.get(db, provider, create=True)
        saved.append((row, row.status, row.last_success_at, row.watch_expires_at))
        rows[provider] = row
    await db.commit()
    yield rows
    for row, status, last, watch in saved:
        row.status, row.last_success_at, row.watch_expires_at = status, last, watch
    await db.commit()


async def test_H11_a_stale_connection_never_produces_an_all_clear(client, db, owner, connection_rows):
    now = datetime.now(timezone.utc)
    for row in connection_rows.values():
        row.status, row.last_success_at, row.watch_expires_at = "connected", now, None
    await db.commit()
    login(client, owner)
    fresh = (await client.get("/api/home")).json()["status"]
    assert fresh["all_clear_possible"] is True and not fresh["stale_connections"]

    connection_rows["gmail_business"].last_success_at = now - timedelta(hours=4)
    await db.commit()
    page = (await client.get("/api/home")).json()
    st = page["status"]
    assert st["all_clear_possible"] is False and st["all_clear"] is False
    assert conns.PROVIDER_LABELS["gmail_business"] in st["stale_connections"]
    assert "needs attention" in st["summary"]
    assert "No urgent items found" not in st["summary"] or "needs attention" in st["summary"]
    # the deterministic, source-backed lists still work while a source is behind
    assert page["needs_decision"]["available"] is True
    assert page["today"]["available"] is True
    assert page["in_progress"]["available"] is True
    stale_group = [g for g in page["needs_attention"]["groups"] if g["problem"] == "connections_stale"]
    assert stale_group and any(i["provider"] == "gmail_business" for i in stale_group[0]["items"])
    assert stale_group[0]["next_action"]
    assert page["model_used"] is False


async def test_H11_summary_counts_stay_deterministic_with_an_open_approval(client, db, owner, connection_rows):
    """K03 tail: an urgent approval stays visible whatever reporting period is selected."""
    a, b = await contact(db), await contact(db)
    res = await cmd(db, owner, "contacts.merge_propose", {"survivor_id": a.id, "merged_id": b.id, "reason": "duplicate"})
    assert res.status == "needs_review"
    approval_id = res.approval_id
    login(client, owner)
    for params in ({"period": "month"}, {"period": "7d"},
                   {"period": "custom", "start": "2019-05-01", "end": "2019-05-31"}):
        page = (await client.get("/api/home", params=params)).json()
        ids = {r["approval_id"] for r in page["needs_decision"]["items"]}
        assert approval_id in ids
        row = next(r for r in page["needs_decision"]["items"] if r["approval_id"] == approval_id)
        assert row["action"] == "contacts.merge_propose" and row["review_path"] == f"/approvals/{approval_id}"
        assert row["deadline"] and row["kind"] == "contact_merge"
        assert row["targets"]["contacts"] == [a.id, b.id] and "related_record" in row
        assert page["needs_decision"]["total"] == len(page["needs_decision"]["items"])
    assert "never filtered by the reporting period" in page["needs_decision"]["note"]


async def test_home_groups_duplicate_alerts_about_one_problem(db, owner, en_route_sale):
    out = await home_svc.needs_attention(db, actor_of(owner), now=datetime.now(timezone.utc), tz=PHOENIX)
    problems = [g["problem"] for g in out["groups"]]
    assert len(problems) == len(set(problems))                 # one row per underlying problem
    blocked = [g for g in out["groups"] if g["problem"].startswith("blocked_work:")]
    assert blocked and all(g["count"] == len(g["items"]) for g in blocked)
    assert all(g["next_action"] for g in out["groups"])
    assert out["items_total"] >= out["total"]
