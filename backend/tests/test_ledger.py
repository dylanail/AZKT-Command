"""Ledger mapping/import acceptance (E01): sorted/inserted rows keep identity; formula-driven value change is flagged as a
new evidence revision; similar rows are not dropped; no sheet writes; unsupported source stays honest."""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from backend.app.adapters import sheets_ledger
from backend.app.core.errors import Blocked, Denied, Unsupported
from backend.app.domain.commands import dispatch
from backend.app.models.finance import CostEvidence, CostItem, LedgerRow
from backend.app.services import ledger as ledger_svc
from backend.tests.conftest import ctx_for, login, run_worker_once

HEADERS = ["Date", "Vendor", "Description", "Amount", "Currency", "Stock", "Invoice", "Category", "Paid"]


def _u() -> str:
    return uuid.uuid4().hex[:8]


@pytest_asyncio.fixture(autouse=True, scope="module")
async def _drain_outbox_after_module():
    yield
    sheets_ledger.set_source(None)
    for _ in range(50):
        out = await run_worker_once()
        if not out.get("events") and not out.get("jobs"):
            break


@pytest.fixture
def source():
    src = sheets_ledger.FixtureLedgerSource()
    sheets_ledger.set_source(src)
    yield src
    sheets_ledger.set_source(None)


def rows_for(tag: str) -> list[list]:
    return [
        ["2026-08-01", f"Kei Parts {tag}", "brake pads", "120.00", "USD", "", f"INV-{tag}-1", "parts", "yes"],
        ["2026-08-02", f"Port {tag}", "customs clearance", "300.00", "USD", "", "", "import", ""],
        ["2026-08-03", f"Yard {tag}", "storage week 1", "50.00", "USD", "", "", "storage", ""],
        ["2026-08-03", f"Yard {tag}", "storage week 1", "50.00", "USD", "", "", "storage", ""],     # similar-looking, real expense
    ]


async def cmd(db, user, name, payload):
    return await dispatch(ctx_for(db, user), name, payload)


async def activate(db, owner, sheet_id: str, tab: str = "Ledger", **update) -> dict:
    m = (await cmd(db, owner, "ledger.propose_mapping", {"sheet_id": sheet_id, "tab": tab})).data["mapping"]
    if update:
        m = (await cmd(db, owner, "ledger.update_mapping", {"mapping_id": m["id"], **update})).data["mapping"]
    m = (await cmd(db, owner, "ledger.preview", {"mapping_id": m["id"]})).data["mapping"]
    assert m["status"] == "previewed"
    return (await cmd(db, owner, "ledger.activate_mapping", {"mapping_id": m["id"], "expected_version": m["version"]})).data["mapping"]


async def test_E01_rows_sorted_inserted_and_formula_changes_keep_identity(db, owner, manager, source):
    tag = _u()
    sheet = f"sheet-{tag}"
    formulas = {(1, "Amount"): "=C2*1.1"}
    source.add_sheet(sheet, "AZKT Ledger", {"Ledger": {"tab_id": "101", "headers": HEADERS, "rows": rows_for(tag), "formulas": formulas}})
    proposed = (await cmd(db, owner, "ledger.propose_mapping", {"sheet_id": sheet})).data
    m = proposed["mapping"]
    assert m["columns"] == {"date": "Date", "vendor": "Vendor", "description": "Description", "amount": "Amount", "currency": "Currency",
                            "vehicle_ref": "Stock", "invoice_no": "Invoice", "category": "Category", "paid_flag": "Paid"}
    assert m["id_strategy"] == "fingerprint" and m["status"] == "draft" and m["detected"]["formula_columns"] == ["Amount"] and not proposed["missing_fields"]
    with pytest.raises(Blocked):   # preview before activation
        await cmd(db, owner, "ledger.activate_mapping", {"mapping_id": m["id"]})
    pv = (await cmd(db, owner, "ledger.preview", {"mapping_id": m["id"]})).data["preview"]
    assert pv["sampled"] == 4 and pv["rows_total"] == 4 and pv["samples"][1]["formulas"] == {"Amount": "=C2*1.1"}
    assert any("similar row" in e["exception"] for e in pv["exceptions"]) and pv["settlement"]["note"]
    with pytest.raises(Denied):    # activation is an owner decision
        await cmd(db, manager, "ledger.activate_mapping", {"mapping_id": m["id"]})
    act = (await cmd(db, owner, "ledger.activate_mapping", {"mapping_id": m["id"]})).data["mapping"]
    assert act["status"] == "active"
    first = (await cmd(db, owner, "ledger.import_rows", {"mapping_id": m["id"]})).data["summary"]
    assert first["inserted"] == 4 and first["changed"] == 0 and len(first["evidence_ids"]) == 4
    items = (await db.execute(select(CostItem).where(CostItem.vendor_name.like(f"%{tag}")))).scalars().all()
    assert len(items) == 4                                                                    # both "similar" storage rows are real expenses
    paid = next(i for i in items if i.invoice_ref == f"INV-{tag}-1")
    assert paid.amount_paid == 0 and paid.status == "invoiced"                                # "Paid" column is not verified settlement
    # sort + insert: identities stay, row numbers move, no duplicates
    rows = list(reversed(rows_for(tag)))
    rows.insert(0, ["2026-08-04", f"Shop {tag}", "labor", "200.00", "USD", "", "", "labor", ""])
    source.set_rows(sheet, "Ledger", rows, formulas={(3, "Amount"): "=C2*1.1"})   # the formula stays on the Port row
    second = (await cmd(db, owner, "ledger.import_rows", {"mapping_id": m["id"]})).data["summary"]
    assert second["inserted"] == 1 and second["moved"] + second["unchanged"] == 4 and second["changed"] == 0
    lrows = (await db.execute(select(LedgerRow).where(LedgerRow.mapping_id == m["id"]))).scalars().all()
    assert len(lrows) == 5 and len((await db.execute(select(CostItem).where(CostItem.vendor_name.like(f"%{tag}")))).scalars().all()) == 5
    # formula-driven value change: same identity, new revision + new evidence revision; conflict is visible, nothing averaged
    rows[3][3] = "330.00"
    source.set_rows(sheet, "Ledger", rows, formulas={(3, "Amount"): "=C2*1.1"})
    third = (await cmd(db, owner, "ledger.import_rows", {"mapping_id": m["id"]})).data["summary"]
    assert third["changed"] == 1 and third["inserted"] == 0 and third["changed_rows"][0]["formula_driven"] is True
    lrows = (await db.execute(select(LedgerRow).where(LedgerRow.mapping_id == m["id"]))).scalars().all()
    assert len(lrows) == 5
    port = next(r for r in lrows if r.values["Vendor"] == f"Port {tag}")
    assert port.status == "changed" and port.revision == 2 and port.history[0]["values"]["Amount"] == "300.00" and port.values["Amount"] == "330.00"
    evs = (await db.execute(select(CostEvidence).where(CostEvidence.ledger_row_id == port.id).order_by(CostEvidence.revision))).scalars().all()
    assert [e.revision for e in evs] == [1, 2] and evs[0].is_current is False and evs[1].supersedes_id == evs[0].id
    assert evs[1].match_state == "conflict" and evs[1].discrepancy["observed"] == "330.00" and evs[1].cost_item_id == evs[0].cost_item_id
    port_item = await db.get(CostItem, evs[1].cost_item_id)
    await db.refresh(port_item)
    assert port_item.amount_invoiced == 300                                                 # unconfirmed change never rewrites the cost
    assert len((await db.execute(select(CostItem).where(CostItem.vendor_name.like(f"%{tag}")))).scalars().all()) == 5
    # re-import without changes is a no-op; a row that disappears is flagged missing, never deleted
    fourth = (await cmd(db, owner, "ledger.import_rows", {"mapping_id": m["id"]})).data["summary"]
    assert fourth["inserted"] == fourth["changed"] == 0 and fourth["unchanged"] == 5
    source.set_rows(sheet, "Ledger", rows[1:], formulas={(2, "Amount"): "=C2*1.1"})
    fifth = (await cmd(db, owner, "ledger.import_rows", {"mapping_id": m["id"]})).data["summary"]
    assert fifth["missing"] == 1 and len((await db.execute(select(LedgerRow).where(LedgerRow.mapping_id == m["id"]))).scalars().all()) == 5
    # no sheet writes anywhere: the fixture only saw reads and its rows are exactly what the test set
    assert source.sheets[sheet]["tabs"]["Ledger"]["rows"] == rows[1:] and source.reads >= 6


async def test_E01_immutable_id_column_and_verified_settlement(db, owner, source):
    tag = _u()
    sheet = f"sheet-{tag}"
    headers = ["ID", "Date", "Payee", "Memo", "Total USD", "Stock", "Settled"]
    rows = [["L-1", "08/01/2026", f"Parts {tag}", "pads", "$120.00", "", "yes"], ["L-2", "08/02/2026", f"Port {tag}", "customs", "$300.00", "", ""]]
    source.add_sheet(sheet, "Ledger 2", {"Ledger": {"tab_id": "202", "headers": headers, "rows": rows}})
    m = await activate(db, owner, sheet, settlement_column="Settled", settlement_verified=True)
    assert m["id_strategy"] == "column:ID" and m["settlement_column"] == "Settled" and m["settlement_verified"] is True
    assert m["columns"]["vendor"] == "Payee" and m["columns"]["amount"] == "Total USD" and m["columns"]["description"] == "Memo"
    s1 = (await cmd(db, owner, "ledger.import_rows", {"mapping_id": m["id"]})).data["summary"]
    assert s1["inserted"] == 2
    paid = (await db.execute(select(CostItem).where(CostItem.vendor_name == f"Parts {tag}"))).scalar_one()
    assert paid.status == "paid" and paid.amount_paid == 120 and paid.amount_invoiced == 120       # verified settlement column → paid observation
    unpaid = (await db.execute(select(CostItem).where(CostItem.vendor_name == f"Port {tag}"))).scalar_one()
    assert unpaid.status == "invoiced" and unpaid.amount_paid == 0
    # sorted rows + a corrected memo keep the ledger id identity
    source.set_rows(sheet, "Ledger", [["L-2", "08/02/2026", f"Port {tag}", "customs (corrected)", "$300.00", "", ""], rows[0]])
    s2 = (await cmd(db, owner, "ledger.import_rows", {"mapping_id": m["id"]})).data["summary"]
    assert s2["changed"] == 1 and s2["moved"] == 1 and s2["inserted"] == 0
    lrows = (await db.execute(select(LedgerRow).where(LedgerRow.mapping_id == m["id"]))).scalars().all()
    assert {r.external_row_id for r in lrows} == {"L-1", "L-2"} and len(lrows) == 2


async def test_ledger_source_unsupported_until_connected(db, owner):
    sheets_ledger.set_source(None)
    with pytest.raises(Unsupported):
        await cmd(db, owner, "ledger.propose_mapping", {"sheet_id": "real-sheet"})


async def test_ledger_router_lists_mappings_and_rows(client, db, owner, source):
    tag = _u()
    sheet = f"sheet-{tag}"
    source.add_sheet(sheet, "Ledger 3", {"Ledger": {"tab_id": "303", "headers": HEADERS, "rows": rows_for(tag)[:2]}})
    login(client, owner)
    p = await client.post("/api/finance/ledger/propose-mapping", json={"sheet_id": sheet})
    assert p.status_code == 200 and p.json()["status"] == "ok"
    mid = p.json()["data"]["mapping"]["id"]
    assert (await client.post("/api/finance/ledger/preview", json={"mapping_id": mid})).status_code == 200
    assert (await client.post("/api/finance/ledger/activate-mapping", json={"mapping_id": mid})).json()["data"]["mapping"]["status"] == "active"
    imp = await client.post("/api/finance/ledger/import-rows", json={"mapping_id": mid})
    assert imp.json()["data"]["summary"]["inserted"] == 2
    lst = await client.get(f"/api/finance/ledger/rows?mapping_id={mid}")
    assert lst.status_code == 200 and lst.json()["total"] == 2
    got = await client.get(f"/api/finance/ledger/mappings/{mid}")
    assert got.json()["mapping"]["sheet_link"].endswith("gid=303")


async def test_settled_row_change_restates_paid_without_double_counting(db, owner, source):
    """A verified-settlement row that changes is the SAME money: the later revision restates the paid amount by its
    delta instead of recording a second payment (invariants 6/15 — one non-duplicated basis)."""
    tag = _u()
    sheet = f"sheet-{tag}"
    headers = ["ID", "Date", "Payee", "Memo", "Total USD", "Stock", "Settled"]
    source.add_sheet(sheet, "Ledger 4", {"Ledger": {"tab_id": "404", "headers": headers,
                                                    "rows": [["L-1", "08/01/2026", f"Parts {tag}", "pads", "$120.00", "", "yes"]]}})
    m = await activate(db, owner, sheet, settlement_column="Settled", settlement_verified=True)
    await cmd(db, owner, "ledger.import_rows", {"mapping_id": m["id"]})
    it = (await db.execute(select(CostItem).where(CostItem.vendor_name == f"Parts {tag}"))).scalar_one()
    assert it.amount_paid == 120 and it.status == "paid"
    # the same settled row is edited in the sheet (memo corrected): identity kept, money not duplicated
    source.set_rows(sheet, "Ledger", [["L-1", "08/01/2026", f"Parts {tag}", "pads (corrected)", "$120.00", "", "yes"]])
    assert (await cmd(db, owner, "ledger.import_rows", {"mapping_id": m["id"]})).data["summary"]["changed"] == 1
    await db.refresh(it)
    assert it.amount_paid == 120 and len([o for o in it.observations if o["kind"] == "paid"]) == 1
    # a corrected settled amount restates the paid total by the delta and keeps the correction visible
    source.set_rows(sheet, "Ledger", [["L-1", "08/01/2026", f"Parts {tag}", "pads (corrected)", "$100.00", "", "yes"]])
    await cmd(db, owner, "ledger.import_rows", {"mapping_id": m["id"]})
    await db.refresh(it)
    assert it.amount_paid == 100
    paid_obs = [o for o in it.observations if o["kind"] == "paid"]
    assert [o["amount"] for o in paid_obs] == ["120.00", "-20.00"] and "restated" in paid_obs[-1]["note"]
    assert it.amount_invoiced == 120 and it.status in ("partially_paid", "invoiced")   # unconfirmed change never rewrites the cost
