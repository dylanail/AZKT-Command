"""Ledger source interface (spec §6.1, §12.3). Read-only: values AND formulas, tab/column metadata, source revision.

`FixtureLedgerSource` is the in-memory implementation used by tests (and by the ledger preview in dev);
`GoogleSheetsLedgerSource` returns `unsupported` until a Google connection exists (stage 2). Nothing here ever
writes to a sheet."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..core.errors import Unsupported


@dataclass
class SheetRow:
    row_number: int                      # 1-based position in the tab at retrieval time (NOT an identity)
    values: dict[str, Any]               # header -> stored (computed) value
    formulas: dict[str, str | None]      # header -> formula text when the cell is formula-driven


@dataclass
class TabMeta:
    tab_id: str
    title: str
    headers: list[str]
    row_count: int
    sample_rows: list[dict] = field(default_factory=list)
    formula_columns: list[str] = field(default_factory=list)


@dataclass
class SheetMeta:
    sheet_id: str
    title: str
    revision: str
    tabs: list[TabMeta]
    permissions: dict = field(default_factory=dict)   # {role: reader|writer, writable: bool}


class LedgerSource(Protocol):
    async def get_sheet_meta(self, sheet_id: str) -> SheetMeta: ...

    async def read_rows(self, sheet_id: str, tab: str, range: str | None = None) -> tuple[list[SheetRow], str]: ...


def raw_row_hash(values: dict, formulas: dict) -> str:
    return hashlib.sha256(json.dumps({"v": values, "f": formulas}, sort_keys=True, default=str).encode()).hexdigest()


class FixtureLedgerSource:
    """Deterministic in-memory sheet. Tests mutate it (sort / insert / change a formula result) between imports."""

    def __init__(self) -> None:
        self.sheets: dict[str, dict] = {}
        self.reads: int = 0

    def add_sheet(self, sheet_id: str, title: str, tabs: dict[str, dict], revision: str = "1") -> None:
        """tabs: {tab_title: {"tab_id": str, "headers": [...], "rows": [[...]], "formulas": {(row_index, header): "=..."}}}"""
        self.sheets[sheet_id] = {"title": title, "revision": str(revision), "tabs": {}}
        for t, spec in tabs.items():
            self.sheets[sheet_id]["tabs"][t] = {"tab_id": spec.get("tab_id") or t, "headers": list(spec["headers"]),
                                                "rows": [list(r) for r in spec.get("rows", [])],
                                                "formulas": dict(spec.get("formulas", {}))}

    def set_rows(self, sheet_id: str, tab: str, rows: list[list], formulas: dict | None = None, revision: str | None = None) -> None:
        t = self.sheets[sheet_id]["tabs"][tab]
        t["rows"] = [list(r) for r in rows]
        if formulas is not None:
            t["formulas"] = dict(formulas)
        if revision is not None:
            self.sheets[sheet_id]["revision"] = str(revision)
        else:
            self.sheets[sheet_id]["revision"] = str(int(self.sheets[sheet_id]["revision"]) + 1)

    def _tab(self, sheet_id: str, tab: str) -> dict:
        s = self.sheets.get(sheet_id)
        if s is None:
            raise Unsupported(f"sheet {sheet_id} not available to this source")
        if tab not in s["tabs"]:
            by_id = {v["tab_id"]: k for k, v in s["tabs"].items()}
            if tab in by_id:
                tab = by_id[tab]
            else:
                raise Unsupported(f"tab {tab!r} not found in sheet {sheet_id}")
        return s["tabs"][tab]

    def _rows(self, t: dict) -> list[SheetRow]:
        out = []
        for i, row in enumerate(t["rows"]):
            values = {h: (row[j] if j < len(row) else None) for j, h in enumerate(t["headers"])}
            formulas = {h: t["formulas"].get((i, h)) for h in t["headers"]}
            out.append(SheetRow(row_number=i + 2, values=values, formulas=formulas))  # header is row 1
        return out

    async def get_sheet_meta(self, sheet_id: str) -> SheetMeta:
        self.reads += 1
        s = self.sheets.get(sheet_id)
        if s is None:
            raise Unsupported(f"sheet {sheet_id} not available to this source")
        tabs = []
        for title, t in s["tabs"].items():
            rows = self._rows(t)
            fcols = sorted({h for r in rows for h, f in r.formulas.items() if f})
            tabs.append(TabMeta(tab_id=t["tab_id"], title=title, headers=list(t["headers"]), row_count=len(rows),
                                sample_rows=[copy.deepcopy(r.values) for r in rows[:5]], formula_columns=fcols))
        return SheetMeta(sheet_id=sheet_id, title=s["title"], revision=s["revision"], tabs=tabs,
                         permissions={"role": "reader", "writable": False})

    async def read_rows(self, sheet_id: str, tab: str, range: str | None = None) -> tuple[list[SheetRow], str]:
        self.reads += 1
        s = self.sheets[sheet_id]
        rows = self._rows(self._tab(sheet_id, tab))
        if range:
            lo, hi = _parse_row_range(range)
            rows = [r for r in rows if (lo is None or r.row_number >= lo) and (hi is None or r.row_number <= hi)]
        return rows, s["revision"]


def _parse_row_range(rng: str) -> tuple[int | None, int | None]:
    """Accepts 'A2:Z100', '2:100' or '2-100' → (2, 100)."""
    import re
    m = re.findall(r"\d+", rng)
    if len(m) >= 2:
        return int(m[0]), int(m[1])
    if len(m) == 1:
        return int(m[0]), None
    return None, None


class GoogleSheetsLedgerSource:
    """Stage 2: reads through the Google connection (values + formulas via `valueRenderOption=FORMULA`).
    Until a connection exists every call is an honest `unsupported` (never fake rows)."""

    async def get_sheet_meta(self, sheet_id: str) -> SheetMeta:
        raise Unsupported("Google Sheets ledger source is not connected (stage 2); connect Google under Settings → Connections")

    async def read_rows(self, sheet_id: str, tab: str, range: str | None = None) -> tuple[list[SheetRow], str]:
        raise Unsupported("Google Sheets ledger source is not connected (stage 2)")


_SOURCE: LedgerSource | None = None


def get_source() -> LedgerSource:
    return _SOURCE or GoogleSheetsLedgerSource()


def set_source(src: LedgerSource | None) -> None:
    """Tests and the stage-2 connection registry install the active source here."""
    global _SOURCE
    _SOURCE = src
