/* Settings › Drive importer. Picks the importer's root folder, scans it, reviews the folder → vehicle
   matches and copies permitted originals into AZKT.

   GET  /api/drive/folders?q=          → {connected, setup_blocked?, candidates[], selected}
   POST /api/drive/root                → {folder_id} | {name}            (owner · connections)
   GET  /api/drive/status              → {connected, root, counts, cursor, freshness, failure, duplicate_names}
   GET  /api/drive/files?folder_id=    → {items[]}
   GET  /api/drive/matches             → {items[] with candidates}
   POST /api/drive/scan                → {full?, limit?}
   POST /api/drive/import              → {file_ids[] | folder_id, include_documents}
   POST /api/drive/matches/{file_id}/{confirm|correct|leave} → {vehicle_id?, note?}
   Shapes copied from backend/app/services/drive_assets.py (serialize_drive_file, coverage, proposed_matches). */
import { useState } from "react";
import { api } from "../../../lib/api";
import { useAuth } from "../../../lib/auth";
import { can, whyNot } from "../../../lib/perms";
import { useCommand } from "../../../lib/useCommand";
import { useQuery } from "../../../lib/useQuery";
import {
  Button, Chip, EmptyState, ErrorState, Expander, Field, GlassPanel, HealthLabel, Input, KeyValues, Loading,
  Notice, NotRecorded, When, useToast,
} from "../../../ui";
import { freshnessHealth } from "./types";

/* ---- shapes ---- */
interface FolderCandidate { id: string; name: string | null; link: string | null; owner: string | null; modified_time: string | null; duplicate_name: boolean }
interface FoldersResp { connected: boolean; setup_blocked?: string; query?: string; candidates: FolderCandidate[]; selected?: string | null }
interface DriveFile {
  id: string; file_id: string; name: string; mime_type: string; is_folder: boolean; parent_id: string | null;
  path: string | null; owner_email: string | null; modified_time: string | null; size_bytes: number | null;
  web_link: string | null; in_root: boolean; removed: boolean; retrieval_eligible: boolean;
  ineligible_reason: string | null; vehicle_id: string | null; match_state: string | null;
  match_evidence: { reasons?: string[]; candidates?: MatchCandidate[]; corroboration?: string[] };
  confirmed_by: string | null; asset_id: string | null; classification: string | null;
  import_status: string | null; import_error: string | null; imported_at: string | null;
}
interface MatchCandidate { vehicle_id: string; score?: number; reasons?: string[]; title?: string | null; stock_no?: string | null }
interface MatchRow extends DriveFile { candidates: MatchCandidate[] }
interface DriveStatus {
  connected: boolean;
  root: { folder_id: string | null; name: string | null; path: string | null; link: string | null; owner: string | null } | null;
  duplicate_names?: Array<{ id: string; name: string | null; link: string | null }>;
  counts: Partial<Record<"files" | "folders" | "pending" | "imported" | "failed" | "blocked" | "left_root" | "removed", number>>;
  cursor?: { page_token: string | null; at: string | null; mode: string | null };
  freshness: { state: string; label: string; last_success_at: string | null };
  failure?: { kind?: string; message?: string; at?: string };
}
interface VehicleHit { id: string; title: string; stock_no?: string | null }
interface ImportResult { imported?: number; deduplicated?: number; skipped?: number; failed?: number; blocked?: number }
interface ScanResult { mode?: string; indexed?: number; changed?: number; removed?: number; left_root?: number; pending_import?: number;
  matches?: { summary?: Partial<Record<"matched" | "proposed" | "ambiguous" | "unmatched" | "rechecked" | "kept_confirmed", number>> } }

const MATCH_STATE: Record<string, { label: string; tone: "ok" | "wait" | "risk" | "amber" | "soft" }> = {
  matched: { label: "Matched on evidence", tone: "ok" },
  confirmed: { label: "Confirmed by you", tone: "ok" },
  corrected: { label: "Corrected by you", tone: "ok" },
  proposed: { label: "Proposed — needs a look", tone: "wait" },
  ambiguous: { label: "More than one truck fits", tone: "risk" },
  unmatched: { label: "No match", tone: "soft" },
  left: { label: "Left unmatched", tone: "soft" },
};
function confidenceText(score: number | undefined): string {
  if (score === undefined || score === null) return "no score recorded";
  return `${Math.round(score * 100)}% confidence`;
}

export function DriveSection() {
  const { user } = useAuth();
  const manage = can(user, "connections");
  const canMatch = can(user, "vehicles.write");
  const canImport = can(user, "documents.write");
  const { run, busy } = useCommand();
  const { toast } = useToast();
  const [tick, setTick] = useState(0);
  const reload = () => setTick((t) => t + 1);

  const [folderQuery, setFolderQuery] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [assignFor, setAssignFor] = useState<string | null>(null);
  const [vehicleTerm, setVehicleTerm] = useState("");

  const status = useQuery<DriveStatus | null>(
    (signal) => api.get<DriveStatus | null>("/api/drive/status", { signal, tolerate: [403, 404] }), [tick]);
  const folders = useQuery<FoldersResp | null>(
    (signal) => (pickerOpen
      ? api.get<FoldersResp | null>(`/api/drive/folders${folderQuery.trim() ? `?q=${encodeURIComponent(folderQuery.trim())}` : ""}`, { signal, tolerate: [403] })
      : Promise.resolve(null)),
    [pickerOpen, folderQuery, tick]);
  const matches = useQuery<{ items: MatchRow[] } | null>(
    (signal) => api.get<{ items: MatchRow[] } | null>("/api/drive/matches", { signal, tolerate: [403, 404] }), [tick]);
  const files = useQuery<{ items: DriveFile[] } | null>(
    (signal) => api.get<{ items: DriveFile[] } | null>("/api/drive/files?limit=200", { signal, tolerate: [403, 404] }), [tick]);
  const vehicles = useQuery<{ items: VehicleHit[] } | null>(
    (signal) => (assignFor
      ? api.get<{ items: VehicleHit[] } | null>(`/api/vehicles?limit=8${vehicleTerm.trim() ? `&q=${encodeURIComponent(vehicleTerm.trim())}` : ""}`, { signal, tolerate: [403] })
      : Promise.resolve(null)),
    [assignFor, vehicleTerm]);

  if (!manage) {
    return (
      <GlassPanel padded>
        <EmptyState align="left" title="Owner only" body={whyNot("connections")} />
      </GlassPanel>
    );
  }

  const st = status.data;
  const root = st?.root || null;
  const counts = st?.counts || {};
  const setupBlocked = folders.data?.setup_blocked
    || (st && !st.connected ? "Google Drive isn't connected. Connect it under Settings → Connections." : null);
  const pending = (files.data?.items || []).filter((f) => !f.is_folder && !f.removed && f.import_status === "pending");
  const blocked = (files.data?.items || []).filter((f) => !f.is_folder && !f.retrieval_eligible);
  const failed = (files.data?.items || []).filter((f) => f.import_status === "failed");

  const toggle = (fileId: string) => setSelected((s) => {
    const n = new Set(s);
    if (n.has(fileId)) n.delete(fileId); else n.add(fileId);
    return n;
  });

  const doRoot = async (folderId: string) => {
    const r = await run("root", "/api/drive/root", { folder_id: folderId }, { success: "Importer folder set. Scan it to build the index." });
    if (r) { setPickerOpen(false); reload(); }
  };
  const doScan = async (full: boolean) => {
    const r = await run<ScanResult>("scan", "/api/drive/scan", { full });
    if (r?.status === "ok") {
      const d = r.data || {};
      const m = d.matches?.summary || {};
      const needsYou = (m.proposed || 0) + (m.ambiguous || 0);
      toast({
        tone: "ok",
        title: full ? "Full re-scan finished" : "Scan finished",
        message: `${d.indexed ?? 0} file${d.indexed === 1 ? "" : "s"} seen · ${d.pending_import ?? 0} waiting to import`
          + (needsYou ? ` · ${needsYou} folder${needsYou === 1 ? "" : "s"} need${needsYou === 1 ? "s" : ""} your decision` : "")
          + (d.left_root ? ` · ${d.left_root} left the folder` : ""),
        duration: 7000,
      });
    }
    if (r) reload();
  };
  /* The server says exactly what happened to each file, so the toast repeats its counts rather than
     claiming every selected file arrived. */
  const doImport = async (fileIds: string[]) => {
    const r = await run<ImportResult>("import", "/api/drive/import", { file_ids: fileIds, include_documents: true });
    if (r?.status === "ok") {
      const d = r.data || {};
      const parts = [
        d.imported ? `${d.imported} copied in` : null,
        d.deduplicated ? `${d.deduplicated} already had identical bytes` : null,
        d.skipped ? `${d.skipped} skipped` : null,
        d.blocked ? `${d.blocked} not allowed` : null,
        d.failed ? `${d.failed} failed` : null,
      ].filter(Boolean);
      toast({
        tone: d.failed || d.blocked ? "risk" : "ok",
        title: d.imported ? "Files imported" : "Nothing new was copied",
        message: parts.length ? `${parts.join(" · ")}. Originals stay in Drive.` : "Every file was already in AZKT.",
        duration: 7000,
      });
    }
    if (r) { setSelected(new Set()); reload(); }
  };
  const doDecide = async (fileId: string, decision: "confirm" | "correct" | "leave", vehicleId?: string) => {
    const r = await run(`match-${fileId}`, `/api/drive/matches/${encodeURIComponent(fileId)}/${decision}`,
      vehicleId ? { vehicle_id: vehicleId } : {},
      { success: decision === "leave" ? "Left unmatched" : decision === "correct" ? "Folder pointed at that truck" : "Match confirmed" });
    if (r) { setAssignFor(null); setVehicleTerm(""); reload(); }
  };

  const rootReason = setupBlocked || undefined;
  const scanReason = setupBlocked || (!root?.folder_id ? "Pick the importer folder first." : undefined);
  const importReason = !canImport ? "Your role can't add documents."
    : setupBlocked || (!root?.folder_id ? "Pick the importer folder first." : !pending.length ? "Nothing is waiting to be imported." : undefined);

  return (
    <div className="stack">
      {setupBlocked ? (
        <Notice tone="risk" lead="Drive isn't connected" action={<Button size="sm" variant="soft" to="/settings/connections">Connections</Button>}>
          {setupBlocked} AZKT only ever reads the importer's Drive — it never writes there or changes sharing.
        </Notice>
      ) : null}
      {st?.failure?.message ? (
        <Notice tone="blocked" lead="Last Drive call failed">{st.failure.kind ? `${st.failure.kind.replace(/_/g, " ")}: ` : ""}{st.failure.message}</Notice>
      ) : null}
      {counts.left_root ? (
        <Notice tone="risk" lead="Files left the folder">
          {counts.left_root} file{counts.left_root === 1 ? " has" : "s have"} moved outside the chosen folder, so AZKT can no longer read {counts.left_root === 1 ? "it" : "them"}. Already imported evidence is kept.
        </Notice>
      ) : null}

      {/* ---- root folder ---- */}
      <GlassPanel padded>
        {status.loading ? <Loading rows={3} label="Loading the importer folder" />
          : status.error ? <ErrorState error={status.error} onRetry={status.reload} />
            : status.data === null ? <EmptyState align="left" title="Not available for your role" body={whyNot("connections")} />
              : (
                <div className="stack-sm">
                  <div className="set-row" style={{ borderBottom: 0, paddingTop: 0 }}>
                    <div className="set-row__main">
                      <span className="set-row__title">
                        <span>{root?.name || "No importer folder chosen"}</span>
                        {st?.freshness ? <HealthLabel health={freshnessHealth(st.freshness.state)} label={st.freshness.label} dot /> : null}
                      </span>
                      <span className="set-row__meta">
                        {root?.path || "Pick the folder the importer shares with you — AZKT reads only inside it."}
                        {root?.owner ? ` · owned by ${root.owner}` : ""}
                      </span>
                      {root?.link ? <a className="fs13" href={root.link} target="_blank" rel="noreferrer noopener">Open in Drive</a> : null}
                    </div>
                    <div className="set-row__right">
                      <Button size="sm" variant={root?.folder_id ? "soft" : "primary"} disabled={!!rootReason} disabledReason={rootReason}
                        onClick={() => setPickerOpen((o) => !o)}>
                        {root?.folder_id ? "Change folder" : "Choose folder"}
                      </Button>
                    </div>
                  </div>

                  {st?.duplicate_names?.length ? (
                    <span className="fs12" style={{ color: "var(--amber-t)" }}>
                      {st.duplicate_names.length} other folder{st.duplicate_names.length === 1 ? " has" : "s have"} the same name. AZKT uses the exact folder you picked, never the name.
                    </span>
                  ) : null}

                  {pickerOpen ? (
                    <div className="stack-sm">
                      <Field label="Search folders in the connected Drive" hint="Duplicate names are shown together so you pick the exact one.">
                        <Input value={folderQuery} onChange={(e) => setFolderQuery(e.target.value)} placeholder="Dylan Nail Shipments" />
                      </Field>
                      {folders.loading ? <Loading rows={2} label="Searching Drive" />
                        : folders.error ? <ErrorState error={folders.error} onRetry={folders.reload} />
                          : !folders.data?.candidates?.length ? (
                            <EmptyState align="left" title="No folder matched" body="Try a different name. AZKT only sees folders this Google account can read." />
                          ) : (
                            <div className="opts">
                              {folders.data.candidates.map((c) => (
                                <button key={c.id} type="button" className="opt" role="radio"
                                  aria-checked={c.id === (folders.data?.selected || root?.folder_id)}
                                  disabled={busy("root")} onClick={() => doRoot(c.id)}>
                                  <span className="opt__mark" aria-hidden="true" />
                                  <span style={{ minWidth: 0 }}>
                                    <span className="opt__label">{c.name || c.id}</span>
                                    <span className="opt__desc">
                                      {c.owner || "owner not recorded"}
                                      {c.modified_time ? <> · changed <When iso={c.modified_time} relative /></> : null}
                                      {c.duplicate_name ? " · another folder has this name" : ""}
                                    </span>
                                  </span>
                                </button>
                              ))}
                            </div>
                          )}
                    </div>
                  ) : null}
                </div>
              )}
      </GlassPanel>

      {/* ---- status + scan ---- */}
      <GlassPanel padded>
        <div className="stack-sm">
          <div className="kpis">
            <div className="kpi"><span className="kpi__label">Files seen</span><span className="kpi__value">{counts.files ?? 0}</span><span className="kpi__sub">{counts.folders ?? 0} folders</span></div>
            <div className="kpi"><span className="kpi__label">Waiting to import</span><span className="kpi__value">{counts.pending ?? 0}</span><span className="kpi__sub">copied only when you ask</span></div>
            <div className="kpi kpi--ok"><span className="kpi__label">In AZKT</span><span className="kpi__value">{counts.imported ?? 0}</span><span className="kpi__sub">originals kept in Drive</span></div>
            <div className={`kpi ${counts.failed ? "kpi--blocked" : ""}`}><span className="kpi__label">Failed</span><span className="kpi__value">{counts.failed ?? 0}</span><span className="kpi__sub">retryable</span></div>
            <div className={`kpi ${counts.blocked ? "kpi--risk" : ""}`}><span className="kpi__label">Not allowed</span><span className="kpi__value">{counts.blocked ?? 0}</span><span className="kpi__sub">access or type</span></div>
          </div>
          <KeyValues items={[
            ["Last scan", st?.cursor?.at ? <><When iso={st.cursor.at} format="long" />{st.cursor.mode ? ` · ${st.cursor.mode}` : ""}</>
              : st?.freshness?.last_success_at ? <When iso={st.freshness.last_success_at} format="long" /> : <NotRecorded text="Never scanned" />],
            ["Removed from Drive", counts.removed ?? 0],
          ]} />
          <div className="row-wrap">
            <Button variant="primary" loading={busy("scan")} disabled={!!scanReason} disabledReason={scanReason} onClick={() => doScan(false)}>Scan now</Button>
            <Button variant="soft" loading={busy("scan")} disabled={!!scanReason} disabledReason={scanReason} onClick={() => doScan(true)}>Full re-scan</Button>
            <span className="fs12 t4">A scan reads names, types, checksums and folder structure. It copies nothing.</span>
          </div>
        </div>
      </GlassPanel>

      {/* ---- matches ---- */}
      <GlassPanel clip>
        <div style={{ padding: "14px 16px 6px" }}>
          <h3 style={{ margin: 0, fontSize: 15 }}>Folders waiting on you</h3>
          <div className="fs13 t3" style={{ marginTop: 2 }}>
            A folder is linked to a truck only on a unique stock or frame reference. A name, model or year alone is a proposal.
          </div>
        </div>
        {matches.loading ? <div style={{ padding: 16 }}><Loading rows={3} label="Loading matches" /></div>
          : matches.error ? <div style={{ padding: 16 }}><ErrorState error={matches.error} onRetry={matches.reload} /></div>
            : matches.data === null ? <div style={{ padding: 16 }}><span className="not-recorded">Matches aren't available for your role.</span></div>
              : !matches.data.items.length ? (
                <div style={{ padding: "0 16px 16px" }}>
                  <EmptyState align="left" title="Nothing waiting" body="Every scanned folder is either linked on evidence or already reviewed." />
                </div>
              ) : matches.data.items.map((m) => {
                const state = MATCH_STATE[m.match_state || ""] || { label: m.match_state || "unknown", tone: "soft" as const };
                const reasons = m.match_evidence?.reasons || [];
                const best = m.candidates?.[0];
                const matchBusy = busy(`match-${m.file_id}`);
                return (
                  <div key={m.file_id} className="set-row">
                    <div className="set-row__main">
                      <span className="set-row__title">
                        <span>{m.name}</span>
                        <Chip size="sm" tone={state.tone}>{state.label}</Chip>
                      </span>
                      <span className="set-row__meta">{m.path || "path not recorded"}</span>
                      {m.candidates?.length ? (
                        <div className="stack-sm" style={{ gap: 4, marginTop: 4 }}>
                          {m.candidates.slice(0, 3).map((c) => (
                            <span key={c.vehicle_id} className="fs13">
                              {c.title || c.vehicle_id}{c.stock_no ? ` · ${c.stock_no}` : ""}
                              <span className="t3"> · {confidenceText(c.score)}</span>
                              {c.reasons?.length ? <span className="t4"> · {c.reasons.slice(0, 2).join("; ")}</span> : null}
                            </span>
                          ))}
                        </div>
                      ) : <span className="fs13 t3">No truck matched this folder's contents.</span>}
                      {reasons.length ? <span className="fs12 t4">{reasons.slice(0, 3).join(" · ")}</span> : null}

                      {assignFor === m.file_id ? (
                        <div className="stack-sm" style={{ marginTop: 8 }}>
                          <Field label="Which truck does this folder belong to?">
                            <Input value={vehicleTerm} onChange={(e) => setVehicleTerm(e.target.value)} placeholder="Stock number, frame number or model" />
                          </Field>
                          {vehicles.loading ? <Loading rows={2} label="Searching vehicles" />
                            : !vehicles.data?.items?.length ? <span className="not-recorded">No vehicle matched that search.</span>
                              : (
                                <div className="opts">
                                  {vehicles.data.items.map((vh) => (
                                    <button key={vh.id} type="button" className="opt" role="radio" aria-checked={false}
                                      disabled={matchBusy} onClick={() => doDecide(m.file_id, "correct", vh.id)}>
                                      <span className="opt__mark" aria-hidden="true" />
                                      <span style={{ minWidth: 0 }}>
                                        <span className="opt__label">{vh.title}</span>
                                        <span className="opt__desc">{vh.stock_no || "no stock number recorded"}</span>
                                      </span>
                                    </button>
                                  ))}
                                </div>
                              )}
                          <div><Button size="sm" variant="ghost" onClick={() => { setAssignFor(null); setVehicleTerm(""); }}>Cancel</Button></div>
                        </div>
                      ) : null}
                    </div>
                    <div className="set-row__right">
                      <Button size="sm" variant="primary" loading={matchBusy}
                        disabled={!canMatch || !best?.vehicle_id}
                        disabledReason={!canMatch ? "Your role can't link folders to vehicles." : "There is no proposed truck to confirm — pick one instead."}
                        onClick={() => doDecide(m.file_id, "confirm", best?.vehicle_id)}>
                        Confirm
                      </Button>
                      <Button size="sm" variant="soft" disabled={!canMatch} disabledReason="Your role can't link folders to vehicles."
                        onClick={() => { setAssignFor(assignFor === m.file_id ? null : m.file_id); setVehicleTerm(""); }}>
                        Pick a truck
                      </Button>
                      <Button size="sm" variant="ghost" loading={matchBusy} disabled={!canMatch} disabledReason="Your role can't link folders to vehicles."
                        onClick={() => doDecide(m.file_id, "leave")}>
                        Leave
                      </Button>
                    </div>
                  </div>
                );
              })}
      </GlassPanel>

      {/* ---- files waiting to import ---- */}
      <GlassPanel clip>
        <div style={{ padding: "14px 16px 6px" }}>
          <div className="between" style={{ gap: 10, flexWrap: "wrap" }}>
            <h3 style={{ margin: 0, fontSize: 15 }}>Waiting to import <span className="t3" style={{ fontWeight: 400 }}>{pending.length}</span></h3>
            <div className="row-wrap">
              <Button size="sm" variant="soft" loading={busy("import")} disabled={!!importReason || !selected.size}
                disabledReason={importReason || "Tick the files you want first."}
                onClick={() => doImport([...selected])}>
                Import selected
              </Button>
              <Button size="sm" variant="primary" loading={busy("import")} disabled={!!importReason} disabledReason={importReason}
                onClick={() => doImport([])}>
                Import everything waiting
              </Button>
            </div>
          </div>
          <div className="fs13 t3" style={{ marginTop: 2 }}>
            Originals stay in Drive. A copy keeps the source link and checksum; identical files are deduplicated.
            Invoices, IDs and shipping papers are stored as documents and never enter a public photo set.
          </div>
        </div>
        {files.loading ? <div style={{ padding: 16 }}><Loading rows={3} label="Loading files" /></div>
          : files.error ? <div style={{ padding: 16 }}><ErrorState error={files.error} onRetry={files.reload} /></div>
            : !pending.length ? (
              <div style={{ padding: "0 16px 16px" }}>
                <EmptyState align="left" title="Nothing waiting" body={root?.folder_id ? "Every readable file in the folder has been imported or skipped." : "Pick the importer folder and scan it first."} />
              </div>
            ) : pending.slice(0, 50).map((f) => (
              <label key={f.file_id} className="set-row" style={{ cursor: "pointer" }}>
                <div className="set-row__main">
                  <span className="set-row__title">
                    <input type="checkbox" style={{ width: 20, height: 20, flex: "none" }} checked={selected.has(f.file_id)} onChange={() => toggle(f.file_id)} aria-label={`Import ${f.name}`} />
                    <span className="truncate">{f.name}</span>
                    {f.classification ? <Chip size="sm" tone="soft">{f.classification.replace(/_/g, " ")}</Chip> : null}
                  </span>
                  <span className="set-row__meta">
                    {f.path || "path not recorded"}
                    {f.modified_time ? <> · changed <When iso={f.modified_time} relative /></> : null}
                    {f.size_bytes ? ` · ${Math.round(f.size_bytes / 1024)} KB` : ""}
                  </span>
                </div>
              </label>
            ))}
      </GlassPanel>

      {blocked.length || failed.length ? (
        <Expander title={`Files AZKT could not take (${blocked.length + failed.length})`}>
          <div className="stack-sm">
            {blocked.slice(0, 20).map((f) => (
              <span key={f.file_id} className="fs13">{f.name} — <span className="t3">{f.ineligible_reason || "retrieval isn't permitted for this file"}</span></span>
            ))}
            {failed.slice(0, 20).map((f) => (
              <span key={f.file_id} className="fs13">{f.name} — <span style={{ color: "var(--blocked)" }}>{f.import_error || "import failed"}</span></span>
            ))}
            <span className="set-foot" style={{ padding: 0 }}>Failures stay retryable. Nothing is invented in their place — a missing photo stays "No photo yet".</span>
          </div>
        </Expander>
      ) : null}
    </div>
  );
}
