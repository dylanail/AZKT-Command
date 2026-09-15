"""Ledger mapping and import (spec §6.1, E01). Read-only authority: nothing here writes to a sheet.

Identity: prefer an immutable ledger id column (`column:<name>`); otherwise a fingerprint of
(date|vendor|amount|currency|description normalized) with an occurrence ordinal so similar-looking real expenses are
never dropped. A `stable_key` (date|vendor|description) lets a formula-driven value change keep the SAME identity:
the row becomes `changed` with a new evidence revision instead of a duplicate expense. Row number is never an identity.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, Field
from sqlalchemy import select

from ..adapters import sheets_ledger
from ..core.errors import Blocked, Conflict, NotFound, ValidationFailed
from ..core.ids import sha256_hex
from ..core.money import quantize
from ..domain.commands import CommandContext, command, dispatch
from ..models.finance import COST_CATEGORIES, LedgerMapping, LedgerRow
from . import finance as fin

MAPPABLE_FIELDS = ("date", "vendor", "description", "amount", "currency", "vehicle_ref", "invoice_no", "category", "paid_flag", "row_id")
FIELD_HINTS: dict[str, tuple[str, ...]] = {
    "row_id": ("id", "row id", "txn id", "transaction id", "ledger id", "entry id", "uid", "ref id"),
    "date": ("date", "txn date", "transaction date", "paid on", "when"),
    "vendor": ("vendor", "payee", "supplier", "paid to", "merchant", "shop", "who"),
    "description": ("description", "memo", "item", "details", "notes", "what", "line"),
    "currency": ("currency", "ccy", "cur"),
    "paid_flag": ("paid?", "paid", "settled", "cleared", "paid flag", "payment status"),
    "amount": ("amount", "total", "cost", "price", "usd", "amount usd", "total usd", "jpy", "amount jpy"),
    "vehicle_ref": ("vehicle", "stock", "stock no", "stk", "truck", "frame", "frame no", "unit", "vin", "chassis"),
    "invoice_no": ("invoice", "invoice no", "inv", "inv no", "reference", "ref", "receipt", "receipt no", "order", "order no", "po"),
    "category": ("category", "type", "class", "bucket", "expense type"),
}
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d %b %Y", "%b %d, %Y", "%b %d %Y", "%Y/%m/%d", "%d-%b-%Y", "%B %d, %Y")
TRUTHY = {"y", "yes", "true", "paid", "x", "1", "settled", "cleared", "done"}
CATEGORY_ALIASES = {"purchase": "purchase", "auction": "purchase", "vehicle": "purchase", "import": "import", "customs": "import",
                    "duty": "import", "shipping": "transport", "freight": "transport", "transport": "transport", "trucking": "transport",
                    "recon": "recon", "repair": "recon", "parts": "parts", "part": "parts", "labor": "labor", "labour": "labor",
                    "selling": "selling", "listing": "selling", "ads": "selling", "storage": "storage", "yard": "storage"}


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9?]+", " ", (h or "").lower()).strip()


def detect_columns(headers: list[str]) -> dict[str, str]:
    """Header → field heuristics: exact hint match first, then substring; each header used once."""
    normalized = {h: _norm_header(h) for h in headers}
    taken: set[str] = set()
    out: dict[str, str] = {}
    for field, hints in FIELD_HINTS.items():
        for h, n in normalized.items():
            if h not in taken and n in hints:
                out[field], taken = h, taken | {h}
                break
    for field, hints in FIELD_HINTS.items():
        if field in out:
            continue
        for h, n in normalized.items():
            if h in taken:
                continue
            if any(re.search(rf"\b{re.escape(hint)}\b", n) for hint in hints):
                out[field], taken = h, taken | {h}
                break
    return out


def parse_amount_cell(raw) -> tuple[Decimal | None, str | None, str | None]:
    """→ (amount, detected currency, error)."""
    if raw is None or raw == "":
        return None, None, "amount missing"
    if isinstance(raw, (int, float, Decimal)):
        return Decimal(str(raw)), None, None
    s = str(raw).strip()
    cur = None
    if "¥" in s or "JPY" in s.upper():
        cur = "JPY"
    elif "$" in s or "USD" in s.upper():
        cur = "USD"
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    s2 = re.sub(r"[^\d.]", "", s)
    if not s2:
        return None, cur, f"amount not numeric: {raw!r}"
    try:
        d = Decimal(s2)
    except InvalidOperation:
        return None, cur, f"amount not numeric: {raw!r}"
    return (-d if neg else d), cur, None


def parse_date_cell(raw) -> tuple[str | None, str | None]:
    if raw is None or raw == "":
        return None, "date missing"
    if isinstance(raw, datetime):
        return raw.date().isoformat(), None
    if isinstance(raw, date):
        return raw.isoformat(), None
    s = str(raw).strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date().isoformat(), None
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat(), None
    except ValueError:
        return None, f"date not recognized: {raw!r}"


def normalize_row(mapping: LedgerMapping, row: sheets_ledger.SheetRow) -> dict:
    cols = mapping.columns or {}
    v = row.values

    def get(field):
        h = cols.get(field)
        return v.get(h) if h else None

    exceptions: list[str] = []
    d, err = parse_date_cell(get("date"))
    if err:
        exceptions.append(err)
    amount, detected_cur, err = parse_amount_cell(get("amount"))
    if err:
        exceptions.append(err)
    cur = (str(get("currency") or "").strip().upper() or detected_cur or mapping.currency_default or "USD")
    if amount is not None:
        amount = quantize(amount, cur)
    vendor = (str(get("vendor") or "").strip() or None)
    if not vendor:
        exceptions.append("vendor missing")
    desc = str(get("description") or "").strip() or None
    cat_raw = str(get("category") or "").strip().lower()
    category = CATEGORY_ALIASES.get(cat_raw) or (cat_raw if cat_raw in COST_CATEGORIES else None)
    paid_raw = get("paid_flag")
    paid = None
    if paid_raw is not None and str(paid_raw).strip() != "":
        paid = str(paid_raw).strip().lower() in TRUTHY
    settlement = bool(paid) and bool(mapping.settlement_column) and bool(mapping.settlement_verified) \
        and cols.get("paid_flag") == mapping.settlement_column
    ext = str(get("row_id")).strip() if get("row_id") not in (None, "") else None
    return {"date": d, "vendor": vendor, "description": desc, "amount": str(amount) if amount is not None else None, "currency": cur,
            "vehicle_ref": (str(get("vehicle_ref")).strip() or None) if get("vehicle_ref") not in (None, "") else None,
            "invoice_no": (str(get("invoice_no")).strip() or None) if get("invoice_no") not in (None, "") else None,
            "category": category, "paid_flag": paid, "is_settlement": settlement, "external_id": ext,
            "is_credit": amount is not None and amount < 0, "exceptions": exceptions}


def fingerprint_of(parsed: dict) -> str:
    return sha256_hex("|".join([parsed.get("date") or "", fin.norm_text(parsed.get("vendor")), parsed.get("amount") or "",
                                parsed.get("currency") or "", fin.norm_text(parsed.get("description"))]))


def stable_key_of(parsed: dict) -> str:
    return sha256_hex("|".join([parsed.get("date") or "", fin.norm_text(parsed.get("vendor")), fin.norm_text(parsed.get("description"))]))


def identify(mapping: LedgerMapping, rows: list[sheets_ledger.SheetRow]) -> list[dict]:
    """Assign identities to a snapshot: fingerprint / stable key with occurrence ordinals (similar rows are distinct)."""
    seen_fp: dict[str, int] = {}
    seen_sk: dict[str, int] = {}
    out = []
    for r in rows:
        parsed = normalize_row(mapping, r)
        if mapping.id_strategy.startswith("column:") and parsed.get("external_id"):
            fp = f"id:{parsed['external_id']}"
            sk = fp
        else:
            base_fp, base_sk = fingerprint_of(parsed), stable_key_of(parsed)
            seen_fp[base_fp] = seen_fp.get(base_fp, 0) + 1
            seen_sk[base_sk] = seen_sk.get(base_sk, 0) + 1
            fp = f"{base_fp}#{seen_fp[base_fp]}"
            sk = f"{base_sk}#{seen_sk[base_sk]}"
            if seen_fp[base_fp] > 1:
                parsed["exceptions"] = parsed["exceptions"] + [f"similar row #{seen_fp[base_fp]} (kept as a distinct expense; review)"]
        out.append({"row": r, "parsed": parsed, "fingerprint": fp, "stable_key": sk,
                    "value_hash": sheets_ledger.raw_row_hash(r.values, r.formulas)})
    return out


def serialize_mapping(m: LedgerMapping) -> dict:
    return {"id": m.id, "version": m.version, "connection_id": m.connection_id, "sheet_id": m.sheet_id, "sheet_title": m.sheet_title,
            "tab_id": m.tab_id, "tab_title": m.tab_title, "header_row": m.header_row, "columns": m.columns or {},
            "id_strategy": m.id_strategy, "currency_default": m.currency_default, "mapping_version": m.mapping_version, "status": m.status,
            "preview": m.preview or {}, "activated_at": fin.iso(m.activated_at), "activated_by": m.activated_by,
            "settlement_column": m.settlement_column, "settlement_verified": m.settlement_verified, "detected": m.detected or {},
            "source_revision": m.source_revision, "last_import_at": fin.iso(m.last_import_at), "last_import": m.last_import or {},
            "sheet_link": f"https://docs.google.com/spreadsheets/d/{m.sheet_id}/edit#gid={m.tab_id or 0}", "created_at": fin.iso(m.created_at)}


def serialize_ledger_row(r: LedgerRow) -> dict:
    return {"id": r.id, "version": r.version, "mapping_id": r.mapping_id, "row_fingerprint": r.row_fingerprint, "stable_key": r.stable_key,
            "external_row_id": r.external_row_id, "row_number_seen": r.row_number_seen, "previous_row_number": r.previous_row_number,
            "values": r.values or {}, "formulas": {k: v for k, v in (r.formulas or {}).items() if v}, "parsed": r.parsed or {},
            "source_revision": r.source_revision, "retrieved_at": fin.iso(r.retrieved_at), "last_seen_at": fin.iso(r.last_seen_at),
            "status": r.status, "cost_evidence_id": r.cost_evidence_id, "revision": r.revision, "history": r.history or [],
            "exceptions": r.exceptions or [], "value_hash": r.value_hash}


async def _mapping(ctx: CommandContext, mapping_id: str, expected_version: int | None = None) -> LedgerMapping:
    m = (await ctx.db.execute(select(LedgerMapping).where(LedgerMapping.id == mapping_id).with_for_update())).scalar_one_or_none()
    if m is None:
        raise NotFound("ledger mapping not found")
    if expected_version is not None and m.version != expected_version:
        raise Conflict("mapping changed since you loaded it", current_version=m.version)
    return m


# ── commands ─────────────────────────────────────────────────────────────────
class ProposeMappingIn(BaseModel):
    sheet_id: str
    tab: str | None = None
    connection_id: str | None = None
    columns: dict | None = None            # explicit overrides {field: header}
    currency_default: str = "USD"


@command("ledger.propose_mapping", input=ProposeMappingIn, perm="finance.write", action_class="internal",
         description="Inspect the sheet (tabs/columns/formulas/sample rows) and propose a versioned column mapping + id strategy.")
async def ledger_propose_mapping(ctx: CommandContext, inp: ProposeMappingIn) -> dict:
    src = sheets_ledger.get_source()
    meta = await src.get_sheet_meta(inp.sheet_id)
    if not meta.tabs:
        raise Blocked("sheet has no tabs")
    tab = next((t for t in meta.tabs if inp.tab in (t.title, t.tab_id)), None) if inp.tab else meta.tabs[0]
    if tab is None:
        raise NotFound(f"tab {inp.tab!r} not found", tabs=[t.title for t in meta.tabs])
    detected = detect_columns(tab.headers)
    columns = {**detected, **{k: v for k, v in (inp.columns or {}).items() if v}}
    bad = [k for k in columns if k not in MAPPABLE_FIELDS] + [v for v in columns.values() if v not in tab.headers]
    if bad:
        raise ValidationFailed("unknown field or header in column overrides", bad=bad, headers=tab.headers)
    id_strategy = "fingerprint"
    if columns.get("row_id"):
        rows, _ = await src.read_rows(inp.sheet_id, tab.title)
        ids = [r.values.get(columns["row_id"]) for r in rows]
        ids_present = [i for i in ids if i not in (None, "")]
        if ids_present and len(set(map(str, ids_present))) == len(ids_present) == len(ids):
            id_strategy = f"column:{columns['row_id']}"
        else:
            columns.pop("row_id", None)
    prior = (await ctx.db.execute(select(LedgerMapping).where(LedgerMapping.sheet_id == inp.sheet_id, LedgerMapping.tab_id == tab.tab_id)
                                  .order_by(LedgerMapping.mapping_version.desc()))).scalars().first()
    m = LedgerMapping(connection_id=inp.connection_id, sheet_id=inp.sheet_id, sheet_title=meta.title, tab_id=tab.tab_id, tab_title=tab.title,
                      header_row=1, columns=columns, id_strategy=id_strategy, currency_default=inp.currency_default.upper(),
                      mapping_version=(prior.mapping_version + 1) if prior else 1, status="draft", preview={},
                      detected={"headers": tab.headers, "sample_rows": tab.sample_rows, "formula_columns": tab.formula_columns,
                                "row_count": tab.row_count, "permissions": meta.permissions, "detected": detected,
                                "tabs": [{"tab_id": t.tab_id, "title": t.title, "row_count": t.row_count} for t in meta.tabs]},
                      source_revision=meta.revision, created_by=ctx.actor.user_id, updated_by=ctx.actor.user_id)
    ctx.db.add(m)
    await ctx.db.flush()
    ctx.changed.append({"kind": "ledger_mapping", "id": m.id, "version": m.version})
    missing = [f for f in ("date", "vendor", "amount") if f not in columns]
    ctx.record(f"Ledger mapping proposed v{m.mapping_version}: {meta.title} / {tab.title} ({id_strategy})", entity_kind="ledger_mapping",
               entity_id=m.id, kind="connection", state="draft", visibility="finance", details={"columns": columns, "missing": missing})
    return {"mapping": serialize_mapping(m), "missing_fields": missing, "created": True}


class UpdateMappingIn(BaseModel):
    mapping_id: str
    columns: dict | None = None
    id_strategy: str | None = None
    currency_default: str | None = None
    settlement_column: str | None = None
    settlement_verified: bool | None = None
    header_row: int | None = None
    expected_version: int | None = None


@command("ledger.update_mapping", input=UpdateMappingIn, perm="finance.write", action_class="internal",
         description="Edit a draft/previewed mapping (columns, id strategy, verified settlement column). Active mappings are versioned, not edited.")
async def ledger_update_mapping(ctx: CommandContext, inp: UpdateMappingIn) -> dict:
    m = await _mapping(ctx, inp.mapping_id, inp.expected_version)
    if m.status not in ("draft", "previewed"):
        raise Blocked(f"mapping is {m.status}; propose a new version instead")
    headers = (m.detected or {}).get("headers") or []
    if inp.columns is not None:
        bad = [k for k in inp.columns if k not in MAPPABLE_FIELDS] + [v for v in inp.columns.values() if v and headers and v not in headers]
        if bad:
            raise ValidationFailed("unknown field or header", bad=bad, headers=headers)
        m.columns = {**(m.columns or {}), **{k: v for k, v in inp.columns.items() if v}}
        for k, v in inp.columns.items():
            if not v:
                m.columns.pop(k, None)
    if inp.id_strategy is not None:
        if inp.id_strategy != "fingerprint" and not inp.id_strategy.startswith("column:"):
            raise ValidationFailed("id_strategy must be 'fingerprint' or 'column:<header>'")
        if inp.id_strategy.startswith("column:"):
            m.columns = {**(m.columns or {}), "row_id": inp.id_strategy.split(":", 1)[1]}
        m.id_strategy = inp.id_strategy
    if inp.currency_default:
        m.currency_default = inp.currency_default.upper()
    if inp.settlement_column is not None:
        if headers and inp.settlement_column and inp.settlement_column not in headers:
            raise ValidationFailed("settlement column not in the sheet headers", headers=headers)
        m.settlement_column = inp.settlement_column or None
        m.columns = {**(m.columns or {}), "paid_flag": inp.settlement_column} if inp.settlement_column else m.columns
    if inp.settlement_verified is not None:
        m.settlement_verified = bool(inp.settlement_verified) and bool(m.settlement_column)
    if inp.header_row is not None:
        m.header_row = inp.header_row
    m.status = "draft"   # edits invalidate a previous preview
    m.preview = {}
    ctx.touch(m, "ledger_mapping")
    ctx.record(f"Ledger mapping edited v{m.mapping_version}", entity_kind="ledger_mapping", entity_id=m.id, kind="connection", state="draft",
               visibility="finance", details={"columns": m.columns, "id_strategy": m.id_strategy, "settlement": m.settlement_column})
    return {"mapping": serialize_mapping(m)}


class PreviewIn(BaseModel):
    mapping_id: str
    limit: int = Field(default=25, ge=1, le=500)
    expected_version: int | None = None


@command("ledger.preview", input=PreviewIn, perm="finance.write", action_class="internal",
         description="Dry-run the mapping: sample parsed rows, matching outcomes and exceptions before activation. Writes nothing to costs.")
async def ledger_preview(ctx: CommandContext, inp: PreviewIn) -> dict:
    m = await _mapping(ctx, inp.mapping_id, inp.expected_version)
    if m.status == "superseded":
        raise Blocked("mapping is superseded")
    missing = [f for f in ("date", "vendor", "amount") if f not in (m.columns or {})]
    if missing:
        raise Blocked("mapping is incomplete", missing=missing)
    src = sheets_ledger.get_source()
    rows, revision = await src.read_rows(m.sheet_id, m.tab_title or m.tab_id)
    ident = identify(m, rows)
    samples, exceptions = [], []
    for it in ident[: inp.limit]:
        p = it["parsed"]
        match = None
        if p["amount"] is not None:
            match = await fin.match_evidence(ctx.db, {"vendor": p["vendor"], "invoice_no": p["invoice_no"], "amount": p["amount"],
                                                      "currency": p["currency"], "occurred_at": datetime.fromisoformat(p["date"]).replace(tzinfo=timezone.utc) if p["date"] else None,
                                                      "vehicle_ref_text": p["vehicle_ref"], "is_credit": p["is_credit"]})
        existing = (await ctx.db.execute(select(LedgerRow.id, LedgerRow.status).where(LedgerRow.mapping_id == m.id,
                                                                                      LedgerRow.row_fingerprint == it["fingerprint"]))).first()
        samples.append({"row_number": it["row"].row_number, "fingerprint": it["fingerprint"][:16], "parsed": p, "already_imported": bool(existing),
                        "match": {"state": match["state"], "reasons": match["reasons"], "cost_item_id": match["cost_item_id"]} if match else None,
                        "formulas": {k: v for k, v in it["row"].formulas.items() if v}})
        for e in p["exceptions"]:
            exceptions.append({"row_number": it["row"].row_number, "exception": e})
    m.preview = {"at": ctx.now.isoformat(), "source_revision": revision, "rows_total": len(rows), "sampled": len(samples), "samples": samples,
                 "exceptions": exceptions, "id_strategy": m.id_strategy,
                 "settlement": {"column": m.settlement_column, "verified": m.settlement_verified,
                                "note": None if m.settlement_verified else "ledger rows are cost observations, not bank settlement evidence"}}
    m.source_revision = revision
    if m.status == "draft":
        m.status = "previewed"
    ctx.touch(m, "ledger_mapping")
    ctx.record(f"Ledger mapping previewed: {len(samples)} sample rows, {len(exceptions)} exceptions", entity_kind="ledger_mapping", entity_id=m.id,
               kind="connection", state="previewed", visibility="finance")
    return {"mapping": serialize_mapping(m), "preview": m.preview}


class MappingRefIn(BaseModel):
    mapping_id: str
    expected_version: int | None = None


@command("ledger.activate_mapping", input=MappingRefIn, perm="finance.write", action_class="owner_only", approval_kind="ledger_mapping",
         summary=lambda p: f"Activate ledger mapping {p.mapping_id[:8]}",
         description="Owner activates a previewed mapping as the read-only import authority (supersedes the previous active version).")
async def ledger_activate_mapping(ctx: CommandContext, inp: MappingRefIn) -> dict:
    m = await _mapping(ctx, inp.mapping_id, inp.expected_version)
    if m.status == "active":
        return {"mapping": serialize_mapping(m), "activated": False}
    if m.status != "previewed":
        raise Blocked("preview the mapping before activating it", status=m.status)
    others = (await ctx.db.execute(select(LedgerMapping).where(LedgerMapping.sheet_id == m.sheet_id, LedgerMapping.tab_id == m.tab_id,
                                                               LedgerMapping.status == "active", LedgerMapping.id != m.id))).scalars().all()
    for o in others:
        o.status = "superseded"
        o.bump(ctx.actor.user_id)
    m.status, m.activated_at, m.activated_by = "active", ctx.now, ctx.actor.user_id
    ctx.touch(m, "ledger_mapping")
    ctx.record(f"Ledger mapping v{m.mapping_version} activated (read-only import authority)", entity_kind="ledger_mapping", entity_id=m.id,
               kind="connection", state="active", visibility="finance", details={"superseded": [o.id for o in others]})
    ctx.emit("ledger.evidence_changed", aggregate_type="ledger_mapping", aggregate_id=m.id, payload={"change": "mapping_activated"})
    return {"mapping": serialize_mapping(m), "activated": True, "superseded": [o.id for o in others]}


class ImportIn(BaseModel):
    mapping_id: str
    range: str | None = None
    dry_run: bool = False


@command("ledger.import_rows", input=ImportIn, perm="finance.write", action_class="internal",
         description="Import rows through the active mapping. Idempotent by identity: sorted/inserted rows keep their identity, "
                     "a formula-driven value change becomes `changed` with a new evidence revision, similar rows are kept distinct.")
async def ledger_import_rows(ctx: CommandContext, inp: ImportIn) -> dict:
    m = await _mapping(ctx, inp.mapping_id)
    if m.status != "active":
        raise Blocked("mapping is not active", status=m.status)
    src = sheets_ledger.get_source()
    rows, revision = await src.read_rows(m.sheet_id, m.tab_title or m.tab_id, inp.range)
    ident = identify(m, rows)
    existing = {r.row_fingerprint: r for r in (await ctx.db.execute(select(LedgerRow).where(LedgerRow.mapping_id == m.id))).scalars().all()}
    by_stable: dict[str, list[LedgerRow]] = {}
    for r in existing.values():
        if r.stable_key:
            by_stable.setdefault(r.stable_key, []).append(r)
    seen_ids: set[str] = set()
    counts = {"inserted": 0, "unchanged": 0, "moved": 0, "changed": 0, "ambiguous": 0, "missing": 0, "exceptions": 0}
    evidence_ids: list[str] = []
    changed_rows: list[dict] = []
    for it in ident:
        r, p, fp = it["row"], it["parsed"], it["fingerprint"]
        row = existing.get(fp)
        outcome = None
        if row is None:
            # not by fingerprint: a value change keeps the identity through the stable key (formula-driven change)
            cands = [x for x in by_stable.get(it["stable_key"], []) if x.id not in seen_ids and x.row_fingerprint != fp]
            if len(cands) == 1:
                row = cands[0]
                outcome = "changed"
            elif len(cands) > 1:
                outcome = "ambiguous"
        elif row.value_hash != it["value_hash"]:
            outcome = "changed"
        elif row.row_number_seen != r.row_number:
            outcome = "moved"
        else:
            outcome = "unchanged"
        if inp.dry_run:
            counts[outcome if outcome in counts else "inserted"] += 1
            continue
        if row is None:
            row = LedgerRow(mapping_id=m.id, row_fingerprint=fp, stable_key=it["stable_key"], external_row_id=p.get("external_id"),
                            row_number_seen=r.row_number, raw={"values": r.values, "formulas": r.formulas}, values=r.values, formulas=r.formulas,
                            source_revision=revision, retrieved_at=ctx.now, last_seen_at=ctx.now, status="new", value_hash=it["value_hash"],
                            revision=1, parsed=p, exceptions=p["exceptions"], history=[], created_by=ctx.actor.user_id)
            if outcome == "ambiguous":
                row.status = "ambiguous"
                row.exceptions = list(row.exceptions) + ["several earlier rows could be this changed row; review"]
                counts["ambiguous"] += 1
            else:
                counts["inserted"] += 1
            ctx.db.add(row)
            await ctx.db.flush()
            existing[fp] = row
            by_stable.setdefault(row.stable_key, []).append(row)
        elif outcome == "changed":
            row.history = list(row.history or []) + [{"revision": row.revision, "values": row.values, "formulas": row.formulas,
                                                      "source_revision": row.source_revision, "row_number": row.row_number_seen,
                                                      "at": fin.iso(row.retrieved_at)}]
            row.revision = (row.revision or 1) + 1
            row.previous_row_number = row.row_number_seen
            row.row_number_seen = r.row_number
            row.values, row.formulas, row.raw = r.values, r.formulas, {"values": r.values, "formulas": r.formulas}
            row.value_hash, row.parsed, row.exceptions = it["value_hash"], p, p["exceptions"]
            row.row_fingerprint, row.stable_key = fp, it["stable_key"]
            row.source_revision, row.retrieved_at, row.last_seen_at = revision, ctx.now, ctx.now
            row.status = "changed"
            row.bump(ctx.actor.user_id)
            counts["changed"] += 1
            changed_rows.append({"ledger_row_id": row.id, "revision": row.revision, "row_number": r.row_number,
                                 "formula_driven": any(r.formulas.get(h) for h in (m.columns or {}).values())})
        elif outcome == "moved":
            row.previous_row_number, row.row_number_seen = row.row_number_seen, r.row_number
            row.last_seen_at, row.source_revision = ctx.now, revision
            if row.status in ("new", "matched", "moved"):
                row.status = "moved"
            counts["moved"] += 1
        else:
            row.last_seen_at = ctx.now
            counts["unchanged"] += 1
        seen_ids.add(row.id)
        if outcome in ("changed",) or row.cost_evidence_id is None:
            if p["amount"] is None or not p["vendor"]:
                counts["exceptions"] += 1
                continue
            res = await dispatch(ctx.child(), "costs.record_evidence", {
                "kind": "ledger_row", "source_ref": f"ledger:{m.sheet_id}:{m.tab_id}:{row.id}", "source_hash": it["value_hash"],
                "extracted": {**p, "row_number": r.row_number, "formulas": {k: v for k, v in r.formulas.items() if v}, "source_revision": revision},
                "amount": str(abs(Decimal(p["amount"]))), "currency": p["currency"], "vendor": p["vendor"], "invoice_no": p["invoice_no"],
                "occurred_at": f"{p['date']}T00:00:00+00:00" if p["date"] else None, "vehicle_ref_text": p["vehicle_ref"],
                "category": p["category"], "is_credit": p["is_credit"], "is_settlement": p["is_settlement"], "ledger_row_id": row.id,
                "create_if_unmatched": not p["is_credit"]}, commit=False)
            ev = res.data["evidence"]
            row.cost_evidence_id = ev["id"]
            evidence_ids.append(ev["id"])
            if outcome != "changed":
                row.status = {"matched": "matched", "conflict": "ambiguous", "ambiguous": "ambiguous", "proposed": "ambiguous",
                              "unmatched": "new"}.get(ev["match_state"], row.status)
    if not inp.dry_run and inp.range is None:
        for r0 in existing.values():
            if r0.id not in seen_ids and r0.status != "missing":
                r0.status = "missing"
                r0.exceptions = list(r0.exceptions or []) + [f"not present in snapshot {revision}; kept for review, not deleted"]
                counts["missing"] += 1
    summary = {**counts, "rows_in_snapshot": len(rows), "source_revision": revision, "evidence_ids": evidence_ids, "changed_rows": changed_rows,
               "dry_run": inp.dry_run, "at": ctx.now.isoformat()}
    if not inp.dry_run:
        m.last_import_at, m.last_import, m.source_revision = ctx.now, summary, revision
        ctx.touch(m, "ledger_mapping")
        ctx.record(f"Ledger import: {counts['inserted']} new, {counts['changed']} changed, {counts['moved']} moved, {counts['unchanged']} unchanged, "
                   f"{counts['ambiguous']} ambiguous, {counts['missing']} missing (revision {revision})", entity_kind="ledger_mapping",
                   entity_id=m.id, kind="connection", state="imported", visibility="finance", exception=bool(counts["ambiguous"] or counts["missing"]),
                   details={k: v for k, v in summary.items() if k != "evidence_ids"})
        ctx.emit("ledger.evidence_changed", aggregate_type="ledger_mapping", aggregate_id=m.id, aggregate_version=m.version,
                 payload={"change": "imported", **{k: v for k, v in counts.items()}, "source_revision": revision})
    return {"summary": summary, "mapping": serialize_mapping(m)}
