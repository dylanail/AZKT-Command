/* Ledger — the Google Sheet AZKT reads, never writes.
   GET  /api/finance/ledger/mappings · /api/finance/ledger/rows?mapping_id=
   POST /api/finance/ledger/propose-mapping · update-mapping · preview · activate-mapping · import-rows
   A mapping goes draft → previewed → active. Editing a mapping throws its preview away; only a previewed
   mapping can be activated, and only the owner activates it. */
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { api } from "../../lib/api";
import { useQuery } from "../../lib/useQuery";
import { useCommand } from "../../lib/useCommand";
import { humanize } from "../../lib/links";
import {
  Button, Chip, EmptyState, ErrorState, Expander, Field, GlassPanel, Input, KeyValues, Loading, Notice, Section, Select, Switch,
  Table, Tr, When,
} from "../../ui";
import {
  LEDGER_FIELDS, LEDGER_FIELD_LABELS, LEDGER_MAPPING_STATUS_LABELS, LEDGER_REQUIRED_FIELDS, LEDGER_ROW_STATUS_LABELS,
  ledgerRowTone, matchStateTone, MATCH_STATE_LABELS,
  type LedgerMapping, type LedgerMappingsResp, type LedgerRowsResp,
} from "./types";

const ROW_FILTERS = ["all", "new", "matched", "ambiguous", "changed", "moved", "missing"] as const;
type RowFilter = (typeof ROW_FILTERS)[number];

function mappingStatusTone(status: string) {
  if (status === "active") return "ok" as const;
  if (status === "previewed") return "wait" as const;
  if (status === "superseded") return "soft" as const;
  return "amber" as const;
}

function ProposeMappingForm({ canWrite, onDone }: { canWrite: boolean; onDone: () => void }) {
  const { run, busy } = useCommand();
  const [sheetId, setSheetId] = useState("");
  const [tab, setTab] = useState("");
  const [currency, setCurrency] = useState("USD");
  const blocked = !canWrite ? "Only the owner connects a ledger." : !sheetId.trim() ? "Paste the sheet id first." : null;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (blocked) return;
    const r = await run("ledger:propose", "/api/finance/ledger/propose-mapping", {
      sheet_id: sheetId.trim(), tab: tab.trim() || null, currency_default: currency.trim().toUpperCase() || "USD",
    }, { success: "Mapping proposed. Check the columns, then preview before activating." });
    if (r?.status === "ok") { setSheetId(""); setTab(""); onDone(); }
  };

  return (
    <form className="stack" onSubmit={submit}>
      <div className="form-grid">
        <Field label="Google Sheet id" required hint="The long id in the sheet's address.">
          <Input value={sheetId} onChange={(e) => setSheetId(e.target.value)} placeholder="1AbC…" />
        </Field>
        <Field label="Tab" hint="Blank uses the first tab."><Input value={tab} onChange={(e) => setTab(e.target.value)} placeholder="e.g. Expenses" /></Field>
        <Field label="Default currency"><Input value={currency} maxLength={3} onChange={(e) => setCurrency(e.target.value.toUpperCase())} /></Field>
      </div>
      <div className="row-wrap">
        <Button type="submit" variant="primary" size="md" loading={busy("ledger:propose")} disabled={!!blocked} disabledReason={blocked || undefined}>
          Inspect the sheet
        </Button>
        <span className="fs12 t3">AZKT reads the sheet. It never writes cells, reformats it or adds tracking columns.</span>
      </div>
    </form>
  );
}

function ColumnEditor({ m, canWrite, onDone }: { m: LedgerMapping; canWrite: boolean; onDone: () => void }) {
  const { run, busy } = useCommand();
  const headers = useMemo(() => {
    const h = (m.detected as { headers?: unknown }).headers;
    return Array.isArray(h) ? h.map(String) : [];
  }, [m.detected]);
  const [columns, setColumns] = useState<Record<string, string>>({ ...(m.columns || {}) });
  const [currency, setCurrency] = useState(m.currency_default || "USD");
  const [settlement, setSettlement] = useState(m.settlement_column || "");
  const [verified, setVerified] = useState(!!m.settlement_verified);
  const [headerRow, setHeaderRow] = useState(String(m.header_row ?? 1));

  useEffect(() => {
    setColumns({ ...(m.columns || {}) });
    setCurrency(m.currency_default || "USD");
    setSettlement(m.settlement_column || "");
    setVerified(!!m.settlement_verified);
    setHeaderRow(String(m.header_row ?? 1));
  }, [m.id, m.version, m.columns, m.currency_default, m.settlement_column, m.settlement_verified, m.header_row]);

  const editable = m.status === "draft" || m.status === "previewed";
  const missing = LEDGER_REQUIRED_FIELDS.filter((f) => !columns[f]);
  const dirty = JSON.stringify(columns) !== JSON.stringify(m.columns || {})
    || currency !== (m.currency_default || "USD")
    || settlement !== (m.settlement_column || "")
    || verified !== !!m.settlement_verified
    || headerRow !== String(m.header_row ?? 1);

  const saveReason = !canWrite ? "Only the owner writes to Finance."
    : !editable ? "An active mapping is versioned, not edited — propose a new one."
    : !dirty ? "Nothing changed yet."
    : null;

  const save = async () => {
    if (saveReason) return;
    const r = await run(`ledger:update:${m.id}`, "/api/finance/ledger/update-mapping", {
      mapping_id: m.id, expected_version: m.version,
      columns: Object.fromEntries(LEDGER_FIELDS.map((f) => [f, columns[f] || ""])),
      currency_default: currency.trim().toUpperCase() || "USD",
      settlement_column: settlement || "",
      settlement_verified: verified,
      header_row: Number(headerRow) || 1,
    }, { success: "Mapping saved. Its previous preview no longer applies — preview it again." });
    if (r?.status === "ok") onDone();
  };

  return (
    <div className="stack">
      <div className="form-grid">
        {LEDGER_FIELDS.map((f) => (
          <Field key={f} label={LEDGER_FIELD_LABELS[f]} required={LEDGER_REQUIRED_FIELDS.includes(f)}
            hint={f === "row_id" ? "An immutable id column beats a fingerprint. Row number is never an identity." : undefined}>
            {headers.length ? (
              <Select value={columns[f] || ""} disabled={!editable || !canWrite} onChange={(e) => setColumns((c) => ({ ...c, [f]: e.target.value }))}>
                <option value="">Not mapped</option>
                {headers.map((h) => <option key={h} value={h}>{h}</option>)}
              </Select>
            ) : (
              <Input value={columns[f] || ""} disabled={!editable || !canWrite} onChange={(e) => setColumns((c) => ({ ...c, [f]: e.target.value }))} placeholder="column header" />
            )}
          </Field>
        ))}
        <Field label="Default currency"><Input value={currency} maxLength={3} disabled={!editable || !canWrite} onChange={(e) => setCurrency(e.target.value.toUpperCase())} /></Field>
        <Field label="Header row"><Input inputMode="numeric" value={headerRow} disabled={!editable || !canWrite} onChange={(e) => setHeaderRow(e.target.value)} /></Field>
        <Field label="Settlement column" hint="A column that genuinely records the money leaving your account.">
          {headers.length ? (
            <Select value={settlement} disabled={!editable || !canWrite} onChange={(e) => setSettlement(e.target.value)}>
              <option value="">None</option>
              {headers.map((h) => <option key={h} value={h}>{h}</option>)}
            </Select>
          ) : <Input value={settlement} disabled={!editable || !canWrite} onChange={(e) => setSettlement(e.target.value)} />}
        </Field>
      </div>

      <Switch checked={verified} onChange={setVerified} disabled={!editable || !canWrite || !settlement}
        disabledReason={!settlement ? "Pick a settlement column first." : !editable ? "An active mapping is versioned, not edited." : "Only the owner writes to Finance."}
        label="This column is verified settlement evidence"
        meta="Off means ledger rows stay cost observations, never proof that money moved" />

      {missing.length ? (
        <Notice tone="risk" lead="Incomplete mapping">Map {missing.map((f) => LEDGER_FIELD_LABELS[f].toLowerCase()).join(", ")} before previewing.</Notice>
      ) : null}

      <div className="row-wrap">
        <Button variant="primary" size="md" loading={busy(`ledger:update:${m.id}`)} disabled={!!saveReason} disabledReason={saveReason || undefined} onClick={save}>Save mapping</Button>
        <Button variant="ghost" size="md" disabled={!dirty} disabledReason="Nothing to discard." onClick={() => { setColumns({ ...(m.columns || {}) }); setCurrency(m.currency_default || "USD"); setSettlement(m.settlement_column || ""); setVerified(!!m.settlement_verified); setHeaderRow(String(m.header_row ?? 1)); }}>Discard</Button>
      </div>
    </div>
  );
}

function PreviewResults({ m }: { m: LedgerMapping }) {
  const p = m.preview || {};
  const samples = p.samples || [];
  const exceptions = p.exceptions || [];
  if (!p.at) {
    return <EmptyState title="Not previewed yet" body="Preview runs the mapping over the sheet without writing anything, and shows what would match." align="left" />;
  }
  return (
    <div className="stack">
      <div className="fs13 t3">
        {p.rows_total ?? 0} rows in the sheet · {p.sampled ?? samples.length} sampled · revision {p.source_revision || "unknown"} ·
        previewed <When iso={p.at} format="datetime" />
      </div>
      {p.settlement?.note ? <Notice tone="wait" lead="Settlement">{p.settlement.note}</Notice> : null}
      {exceptions.length ? (
        <Notice tone="risk" lead={`${exceptions.length} exception${exceptions.length === 1 ? "" : "s"}`}>
          {exceptions.slice(0, 4).map((x) => `row ${x.row_number ?? "?"}: ${String(x.exception)}`).join(" · ")}
          {exceptions.length > 4 ? ` · and ${exceptions.length - 4} more` : ""}
        </Notice>
      ) : null}
      {samples.length ? (
        <Table minWidth={860} aria-label="Preview rows">
          <thead><tr><th>Row</th><th>Date</th><th>Vendor</th><th style={{ textAlign: "right" }}>Amount</th><th>Would match</th></tr></thead>
          <tbody>
            {samples.map((s) => {
              const parsed = s.parsed || {};
              const amount = parsed.amount as string | null;
              const currency = (parsed.currency as string) || m.currency_default || "USD";
              return (
                <tr key={s.fingerprint}>
                  <td className="t3 fs13">{s.row_number ?? "?"}{s.already_imported ? <div className="fs12 t4">already imported</div> : null}</td>
                  <td className="t3 fs13">{(parsed.date as string) || "Not parsed"}</td>
                  <td>{(parsed.vendor as string) || <span className="t3">Not parsed</span>}</td>
                  <td style={{ textAlign: "right" }}>{amount !== null && amount !== undefined ? `${amount} ${currency}` : <span className="t3">Not parsed</span>}</td>
                  <td>
                    {s.match ? (
                      <span className="stack-sm" style={{ gap: 2 }}>
                        <Chip size="sm" tone={matchStateTone(s.match.state)}>{MATCH_STATE_LABELS[s.match.state] || humanize(s.match.state)}</Chip>
                        <span className="fs12 t3">{s.match.reasons.join(" · ")}</span>
                      </span>
                    ) : <span className="t3 fs13">No amount to match on</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      ) : <EmptyState title="No sample rows came back" align="left" />}
    </div>
  );
}

function ImportedRows({ mappingId }: { mappingId: string }) {
  const [filter, setFilter] = useState<RowFilter>("all");
  const q = useQuery<LedgerRowsResp>((signal) => api.get<LedgerRowsResp>(
    `/api/finance/ledger/rows?mapping_id=${encodeURIComponent(mappingId)}${filter === "all" ? "" : `&status=${filter}`}`,
    { signal, tolerate: [404] },
  ), [mappingId, filter]);

  return (
    <div className="stack">
      <div className="row-wrap">
        {ROW_FILTERS.map((f) => (
          <Chip key={f} size="sm" selected={filter === f} tone={filter === f ? "act" : "soft"} onClick={() => setFilter(f)}>
            {f === "all" ? "All" : LEDGER_ROW_STATUS_LABELS[f] || humanize(f)}
          </Chip>
        ))}
      </div>
      <GlassPanel clip>
        {q.loading ? <Loading label="Loading imported rows" rows={3} />
          : q.error ? <ErrorState error={q.error} onRetry={q.reload} />
          : !(q.data?.items || []).length ? <EmptyState title="No rows with that state" body="Sync reads the sheet and lands every real expense here — similar-looking rows stay distinct." />
          : (
            <Table minWidth={900} aria-label="Imported ledger rows">
              <thead><tr><th>Row</th><th>Date</th><th>Vendor</th><th style={{ textAlign: "right" }}>Amount</th><th>State</th><th>Identity</th></tr></thead>
              <tbody>
                {(q.data?.items || []).map((r) => {
                  const p = r.parsed || {};
                  const exceptions = (r.exceptions || []) as unknown[];
                  return (
                    <Tr key={r.id}>
                      <td className="t3 fs13">
                        {r.row_number_seen ?? "?"}
                        {r.previous_row_number && r.previous_row_number !== r.row_number_seen ? <div className="fs12 t4">was row {r.previous_row_number}</div> : null}
                      </td>
                      <td className="t3 fs13">{(p.date as string) || "Not parsed"}</td>
                      <td>{(p.vendor as string) || <span className="t3">Not parsed</span>}</td>
                      <td style={{ textAlign: "right" }}>{p.amount !== null && p.amount !== undefined ? `${String(p.amount)} ${String(p.currency || "")}` : <span className="t3">Not parsed</span>}</td>
                      <td>
                        <span className="row-wrap" style={{ gap: 4 }}>
                          <Chip size="sm" tone={ledgerRowTone(r.status)}>{LEDGER_ROW_STATUS_LABELS[r.status] || humanize(r.status)}</Chip>
                          {r.revision > 1 ? <Chip size="sm" tone="soft">rev {r.revision}</Chip> : null}
                          {exceptions.length ? <Chip size="sm" tone="blocked" count={exceptions.length} title={exceptions.map(String).join(" · ")}>exception</Chip> : null}
                          {Object.keys(r.formulas || {}).length ? <Chip size="sm" tone="soft" title="A formula drives at least one mapped value.">formula</Chip> : null}
                        </span>
                      </td>
                      <td className="fs12 t4">
                        {r.external_row_id ? `id ${r.external_row_id}` : `fingerprint ${r.row_fingerprint.slice(0, 12)}`}
                        {r.cost_evidence_id ? <div>evidence {r.cost_evidence_id.slice(0, 8)}</div> : null}
                      </td>
                    </Tr>
                  );
                })}
              </tbody>
            </Table>
          )}
      </GlassPanel>
    </div>
  );
}

export function LedgerTab({ canWrite, isOwner }: { canWrite: boolean; isOwner: boolean }) {
  const q = useQuery<LedgerMappingsResp>((signal) => api.get<LedgerMappingsResp>("/api/finance/ledger/mappings", { signal, tolerate: [404] }), []);
  const { run, busy } = useCommand();
  const [selected, setSelected] = useState<string | null>(null);

  const mappings = q.data?.items || [];
  const current = mappings.find((m) => m.id === selected)
    || mappings.find((m) => m.status === "active")
    || mappings[0]
    || null;

  const preview = async (m: LedgerMapping) => {
    const r = await run(`ledger:preview:${m.id}`, "/api/finance/ledger/preview", { mapping_id: m.id, limit: 25, expected_version: m.version },
      { success: "Preview ready. Nothing was written to costs." });
    if (r?.status === "ok") q.reload();
  };
  const activate = async (m: LedgerMapping) => {
    const r = await run(`ledger:activate:${m.id}`, "/api/finance/ledger/activate-mapping", { mapping_id: m.id, expected_version: m.version },
      { success: "Mapping active. It is now the read-only authority for imports." });
    if (r?.status === "ok") q.reload();
  };
  const sync = async (m: LedgerMapping) => {
    const r = await run(`ledger:sync:${m.id}`, "/api/finance/ledger/import-rows", { mapping_id: m.id },
      { success: "Sync queued. New and changed rows land on Needs matching." });
    if (r?.status === "ok") q.reload();
  };

  if (q.loading) return <GlassPanel clip><Loading label="Loading ledger mappings" rows={3} /></GlassPanel>;
  if (q.error) return <GlassPanel clip><ErrorState error={q.error} onRetry={q.reload} /></GlassPanel>;

  if (!mappings.length) {
    return (
      <div className="stack">
        <GlassPanel padded>
          <div className="stack">
            <div className="stack-sm" style={{ gap: 2 }}>
              <h2 style={{ margin: 0, fontSize: 15 }}>Connect the ledger</h2>
              <span className="fs13 t3">Pick the exact sheet and tab. AZKT reads stored values and the formulas behind them; it never writes.</span>
            </div>
            <ProposeMappingForm canWrite={canWrite} onDone={q.reload} />
          </div>
        </GlassPanel>
        <div className="set-foot">
          Row number is never an identity — rows get sorted and inserted. AZKT prefers an immutable id column and otherwise
          fingerprints the row, so a formula-driven change becomes a new revision of the same expense instead of a second one.
        </div>
      </div>
    );
  }

  const m = current as LedgerMapping;
  const missing = LEDGER_REQUIRED_FIELDS.filter((f) => !(m.columns || {})[f]);
  const previewReason = !canWrite ? "Only the owner writes to Finance."
    : missing.length ? `Map ${missing.join(", ")} first.`
    : m.status === "superseded" ? "This mapping was replaced by a newer version."
    : undefined;
  const activateReason = !isOwner ? "Only the owner activates a mapping."
    : m.status === "active" ? "Already active."
    : m.status !== "previewed" ? "Preview the mapping before activating it."
    : undefined;
  const syncReason = !canWrite ? "Only the owner writes to Finance."
    : m.status !== "active" ? "Only an active mapping imports rows."
    : undefined;

  return (
    <div className="stack-lg">
      <Section title="Mapping" count={mappings.length}>
        <GlassPanel padded>
          <div className="stack">
            <div className="between">
              <div className="stack-sm" style={{ gap: 2 }}>
                <span className="row-wrap">
                  <strong>{m.sheet_title || "Sheet"}</strong>
                  <span className="t3">/ {m.tab_title || m.tab_id || "first tab"}</span>
                  <Chip size="sm" tone={mappingStatusTone(m.status)}>{LEDGER_MAPPING_STATUS_LABELS[m.status] || humanize(m.status)}</Chip>
                  <Chip size="sm" tone="soft">v{m.mapping_version ?? 1}</Chip>
                  <Chip size="sm" tone={m.id_strategy?.startsWith("column:") ? "ok" : "amber"}
                    title={m.id_strategy?.startsWith("column:") ? "A stable id column identifies each row." : "No id column — rows are fingerprinted."}>
                    {m.id_strategy?.startsWith("column:") ? `id column ${m.id_strategy.slice(7)}` : "fingerprint identity"}
                  </Chip>
                </span>
                <span className="fs12 t3">
                  <a href={m.sheet_link} target="_blank" rel="noreferrer noopener">Open the sheet</a>
                  {m.last_import_at ? <> · last sync <When iso={m.last_import_at} relative /></> : " · never synced"}
                  {m.source_revision ? ` · revision ${m.source_revision}` : ""}
                </span>
              </div>
              {mappings.length > 1 ? (
                <Select aria-label="Mapping version" value={m.id} onChange={(e) => setSelected(e.target.value)} style={{ width: "auto", minWidth: 200 }}>
                  {mappings.map((x) => (
                    <option key={x.id} value={x.id}>v{x.mapping_version ?? 1} · {LEDGER_MAPPING_STATUS_LABELS[x.status] || x.status} · {x.tab_title || x.tab_id}</option>
                  ))}
                </Select>
              ) : null}
            </div>

            {m.status === "active" && !m.settlement_verified ? (
              <Notice tone="wait" lead="Ledger rows are observations">
                No verified settlement column, so an imported row records what a cost is — not that the money left your account.
              </Notice>
            ) : null}

            <div className="row-wrap">
              <Button size="md" variant="soft" loading={busy(`ledger:preview:${m.id}`)} disabled={!!previewReason} disabledReason={previewReason} onClick={() => preview(m)}>Preview</Button>
              <Button size="md" variant="primary" loading={busy(`ledger:activate:${m.id}`)} disabled={!!activateReason} disabledReason={activateReason} onClick={() => activate(m)}>Activate</Button>
              <Button size="md" variant="glass" loading={busy(`ledger:sync:${m.id}`)} disabled={!!syncReason} disabledReason={syncReason} onClick={() => sync(m)}>Sync now</Button>
            </div>

            {m.last_import ? (
              <Expander title="Last sync result">
                <KeyValues items={Object.entries(m.last_import)
                  .filter(([k, v]) => k !== "evidence_ids" && k !== "changed_rows" && typeof v !== "object")
                  .map(([k, v]) => [humanize(k), String(v)] as [string, string])} />
              </Expander>
            ) : null}

            <Expander title="Column mapping" defaultOpen={m.status !== "active"}>
              <ColumnEditor m={m} canWrite={canWrite} onDone={q.reload} />
            </Expander>
          </div>
        </GlassPanel>
      </Section>

      <Section title="Preview">
        <GlassPanel padded><PreviewResults m={m} /></GlassPanel>
      </Section>

      <Section title="Imported rows">
        <ImportedRows mappingId={m.id} />
      </Section>

      <Section title="Connect another sheet or tab">
        <GlassPanel padded><ProposeMappingForm canWrite={canWrite} onDone={q.reload} /></GlassPanel>
      </Section>

      <div className="set-foot">
        Read-only is the authority here: AZKT never rewrites formulas, reformats the sheet or inserts tracking columns.
        A row that disappears from a snapshot is kept and marked, not deleted.
      </div>
    </div>
  );
}
