"""Live Google Sheets ledger source (spec §6.1, §12.3).

`services/ledger.py` reads through the `LedgerSource` protocol declared in `adapters/sheets_ledger.py`
(`get_sheet_meta` / `read_rows`). That module owns the protocol and the fixture implementation; this
module adds the **live** implementation and the factory that installs it, so finance never has to know
how Google is reached.

Read-only by construction:

* only `spreadsheets.readonly` / `drive.metadata.readonly` scopes are requested, and
  `assert_read_only()` refuses to run against a connection that was granted a write scope;
* values are read twice per range — `FORMATTED_VALUE` for the stored value a person sees and
  `FORMULA` for the formula behind it — and never written back;
* the source revision is the Drive file `version` (row numbers are never an identity, §6.1/E01).

Factory (documented for the finance domain):

    from ..adapters import sheets
    await sheets.install_live_source(db)      # registers the live source with sheets_ledger.set_source
    sheets.uninstall_source()                 # back to the fixture / unconnected source

`FakeSheets` is the in-memory implementation used by tests: it is a `FixtureLedgerSource` plus the
Drive-side "list candidate sheets by name" call that the setup screen needs.
"""
from __future__ import annotations

from typing import Any

from ..core.errors import ProviderError, Unsupported
from .sheets_ledger import FixtureLedgerSource, LedgerSource, SheetMeta, SheetRow, TabMeta, set_source

SHEETS_URL = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"
READ_SCOPES = ("https://www.googleapis.com/auth/spreadsheets.readonly",
               "https://www.googleapis.com/auth/drive.metadata.readonly",
               "https://www.googleapis.com/auth/drive.readonly",
               "openid", "email", "profile")
WRITE_SCOPES = ("https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive")


def assert_read_only(scopes: list[str] | None) -> None:
    """The ledger integration is read-only authority (spec §6.1): a write scope is a setup error."""
    bad = [s for s in (scopes or []) if s in WRITE_SCOPES]
    if bad:
        raise Unsupported(f"the ledger source never requests write scopes (granted: {bad}); "
                          "reconnect Google with read-only access")


def requested_scopes() -> list[str]:
    return [s for s in READ_SCOPES if s.startswith("https://")]


def _a1(tab: str, rng: str | None) -> str:
    quoted = "'" + tab.replace("'", "''") + "'"
    return f"{quoted}!{rng}" if rng else quoted


class SheetsLedgerSource:
    """Live implementation of finance's `LedgerSource`. Values AND formulas; never writes."""

    kind = "live"

    def __init__(self, api, *, sample_rows: int = 5):
        self.api = api
        self.sample_rows = sample_rows
        assert_read_only(list((api.conn.granted_scopes or []) if getattr(api, "conn", None) else []))

    async def _revision(self, sheet_id: str) -> str:
        try:
            meta = await self.api.request("GET", f"{DRIVE_FILES_URL}/{sheet_id}",
                                          params={"fields": "version,modifiedTime", "supportsAllDrives": "true"})
        except ProviderError:
            return ""
        return str(meta.get("version") or meta.get("modifiedTime") or "")

    async def _values(self, sheet_id: str, a1: str, render: str) -> list[list]:
        body = await self.api.request("GET", f"{SHEETS_URL}/{sheet_id}/values/{a1}",
                                      params={"valueRenderOption": render, "dateTimeRenderOption": "FORMATTED_STRING"})
        return list(body.get("values") or [])

    async def get_sheet_meta(self, sheet_id: str) -> SheetMeta:
        body = await self.api.request("GET", f"{SHEETS_URL}/{sheet_id}", params={
            "fields": "properties(title),sheets(properties(sheetId,title,gridProperties(rowCount)))"})
        title = ((body.get("properties") or {}).get("title")) or sheet_id
        tabs: list[TabMeta] = []
        for s in body.get("sheets") or []:
            props = s.get("properties") or {}
            tab_title = str(props.get("title") or "")
            tab_id = str(props.get("sheetId") if props.get("sheetId") is not None else tab_title)
            values = await self._values(sheet_id, _a1(tab_title, f"A1:ZZ{self.sample_rows + 1}"), "FORMATTED_VALUE")
            formulas = await self._values(sheet_id, _a1(tab_title, f"A1:ZZ{self.sample_rows + 1}"), "FORMULA")
            headers = [str(h) for h in (values[0] if values else [])]
            samples = []
            formula_columns: list[str] = []
            for i, row in enumerate(values[1:] if values else []):
                samples.append({h: (row[j] if j < len(row) else None) for j, h in enumerate(headers)})
                frow = formulas[i + 1] if len(formulas) > i + 1 else []
                for j, h in enumerate(headers):
                    cell = frow[j] if j < len(frow) else None
                    if isinstance(cell, str) and cell.startswith("=") and h not in formula_columns:
                        formula_columns.append(h)
            row_count = int(((props.get("gridProperties") or {}).get("rowCount")) or 0)
            tabs.append(TabMeta(tab_id=tab_id, title=tab_title, headers=headers,
                                row_count=max(row_count - 1, 0), sample_rows=samples,
                                formula_columns=formula_columns))
        return SheetMeta(sheet_id=sheet_id, title=title, revision=await self._revision(sheet_id), tabs=tabs,
                         permissions={"role": "reader", "writable": False,
                                      "note": "AZKT reads the ledger; writeback is a separate approved feature (§6.4)"})

    async def read_rows(self, sheet_id: str, tab: str, range: str | None = None) -> tuple[list[SheetRow], str]:
        a1 = _a1(tab, range)
        values = await self._values(sheet_id, a1, "FORMATTED_VALUE")
        formulas = await self._values(sheet_id, a1, "FORMULA")
        if not values:
            return [], await self._revision(sheet_id)
        headers = [str(h) for h in values[0]]
        offset = 2
        if range:
            digits = "".join(ch for ch in range.split(":")[0] if ch.isdigit())
            if digits:
                offset = int(digits) + 1
        rows: list[SheetRow] = []
        for i, row in enumerate(values[1:]):
            frow = formulas[i + 1] if len(formulas) > i + 1 else []
            vals = {h: (row[j] if j < len(row) else None) for j, h in enumerate(headers)}
            fs = {h: (frow[j] if j < len(frow) and isinstance(frow[j], str) and str(frow[j]).startswith("=") else None)
                  for j, h in enumerate(headers)}
            rows.append(SheetRow(row_number=offset + i, values=vals, formulas=fs))
        return rows, await self._revision(sheet_id)

    async def list_candidate_sheets(self, name: str | None = None, *, page_size: int = 50) -> list[dict]:
        q = [f"mimeType = '{SPREADSHEET_MIME}'", "trashed = false"]
        if name:
            q.append("name contains '%s'" % name.replace("'", "\\'"))
        body = await self.api.request("GET", DRIVE_FILES_URL, params={
            "q": " and ".join(q), "pageSize": page_size, "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "fields": "files(id,name,modifiedTime,owners(emailAddress),webViewLink,version)"})
        return list(body.get("files") or [])

    # explicitly unsupported: read-only authority (spec §6.1, §6.4)
    async def write_rows(self, *_a, **_kw):
        raise Unsupported("ledger writeback is a separate owner-approved feature with a change preview (§6.4)")


class FakeSheets(FixtureLedgerSource):
    """Fixture ledger source plus the Drive-side sheet picker used by the setup screen."""

    kind = "fake"

    def __init__(self) -> None:
        super().__init__()
        self.offline = False

    async def list_candidate_sheets(self, name: str | None = None, *, page_size: int = 50) -> list[dict]:
        if self.offline:
            raise ProviderError("google auth expired", kind="auth_expired")
        out = []
        for sheet_id, s in self.sheets.items():
            if name and name.lower() not in s["title"].lower():
                continue
            out.append({"id": sheet_id, "name": s["title"], "modifiedTime": "2026-09-14T00:00:00Z",
                        "owners": [{"emailAddress": "dylxnxil@gmail.com"}], "version": s["revision"],
                        "webViewLink": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"})
        return out[:page_size]

    async def write_rows(self, *_a, **_kw):
        raise Unsupported("ledger writeback is a separate owner-approved feature (§6.4)")


# ── factory ──────────────────────────────────────────────────────────────────
def make_source(db, conn) -> LedgerSource:
    """Build the live source for a connected Google account (raises `Unsupported` when not connected)."""
    if conn is None or conn.status == "disconnected":
        raise Unsupported("the ledger sheet is not connected; connect Google under Settings → Connections",
                          setup_blocked="sheets.oauth")
    from ..core.config import settings
    if settings.ENV == "test":
        raise Unsupported("live Sheets calls are disabled in tests; install a fixture source")
    from .google_oauth import GoogleApi
    return SheetsLedgerSource(GoogleApi(db, conn))


async def install_live_source(db, conn=None) -> LedgerSource:
    """Register the live Sheets source with `sheets_ledger.set_source` so `services/ledger.py` uses it."""
    if conn is None:
        from ..services import connections as conn_svc
        conn = await conn_svc.get(db, "sheets")
    src = make_source(db, conn)
    set_source(src)
    return src


def install_source(src: Any) -> None:
    set_source(src)


def uninstall_source() -> None:
    set_source(None)
