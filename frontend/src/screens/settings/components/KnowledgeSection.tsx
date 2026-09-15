/* Settings › Knowledge (spec §9.1–9.5, G11, G13).
   GET  /api/knowledge?status=&kind=            — proposed / approved / retired / rejected items with their scope
   POST /api/knowledge/{id}/approve|reject|retire — owner only; approval activates knowledge, never a permission
   GET  /api/knowledge/corpus · /corpus/manifests — what the corpus is built from, and what it excludes
   POST /api/knowledge/corpus/reindex           — re-chunk one source (a tombstoned one needs explicit readmission)
   GET  /api/knowledge/search?q=                — the same ACL-safe envelope the agents get, labelled and redacted
   GET  /api/proposals · POST /api/proposals/{id}/decide — promotion proposals and the exact permission they ask for */
import { useRef, useState, type FormEvent } from "react";
import "../../../styles/learning.css";
import { api } from "../../../lib/api";
import { useAuth } from "../../../lib/auth";
import { useQuery } from "../../../lib/useQuery";
import { useCommand } from "../../../lib/useCommand";
import { useIsMobile } from "../../../lib/viewport";
import { can } from "../../../lib/perms";
import { humanize } from "../../../lib/links";
import {
  Button, Chip, EmptyState, ErrorState, Expander, Field, GlassPanel, Input, KeyValues, Loading, Notice, ResponsiveDialog,
  SegmentedControl, Section, Select, Textarea, When,
} from "../../../ui";
import { JsonDetail } from "../../shared/JsonDetail";

const KINDS = ["policy", "style", "template", "example", "lesson", "exception", "procedure_note"] as const;
const KIND_LABELS: Record<string, string> = {
  policy: "Policy", style: "Style", template: "Template", example: "Example",
  lesson: "Lesson", exception: "Buyer exception", procedure_note: "Procedure note",
};
const STATUSES = ["proposed", "approved", "retired", "rejected"] as const;
type Status = (typeof STATUSES)[number] | "all";

const SOURCE_KINDS = ["message", "document", "knowledge", "procedure", "vehicle_fact", "task_note", "note", "example", "transcript", "web_page", "attachment"];

interface KnowledgeItem {
  id: string; version: number; kind: string; title: string; content: string;
  scope: { contact_id?: string; vehicle_id?: string; workflow?: string };
  status: string; classification: string | null; usable_as: string[]; requires_owner: boolean;
  evidence: unknown[]; visibility: string; effective_from: string | null; expires_at: string | null;
  source_kind: string | null; source_ref: string | null; proposed_by: string | null;
  approved_by: string | null; approved_at: string | null; retired_at: string | null; retired_reason: string | null;
  rejected_at: string | null; rejected_reason: string | null; diff_summary: Record<string, unknown>;
  tests: unknown[]; created_at: string | null; updated_at: string | null; is_general: boolean;
}
interface KnowledgeResp { items: KnowledgeItem[]; total: number; kinds: string[]; statuses: string[] }

interface Manifest {
  id: string; label: string; status: string; sources: Array<Record<string, unknown>>;
  coverage: { from: string | null; to: string | null };
  counts: Record<string, unknown>; excluded: Record<string, unknown>;
  embedding: { model: string | null; dims: number | null };
  chunker_version: string | null; parser_version: string | null;
  retrieval_settings: Record<string, unknown>; failures: unknown[];
  activated_at: string | null; superseded_by_id: string | null; last_indexed_at: string | null; created_at: string | null;
}
interface Coverage {
  manifest: Manifest | null; defined: boolean; claim: string;
  counts: { chunks: number; by_source_kind: Record<string, number>; by_trust: Record<string, number>; embedded: number; knowledge_items: Record<string, number> };
  excluded: Record<string, unknown>; failures: unknown[];
  coverage: { from: string | null; to: string | null; observed_from: string | null; observed_to: string | null };
  last_indexed_at: string | null;
  embedding: { configured: boolean; model: string | null; dims: number | null };
}

interface Hit {
  chunk_id: string; source_kind: string; source_id: string; text: string; kind: string | null;
  happened_at: string | null; speaker: string | null; other_customer: boolean; score: number;
  identity_redacted: boolean; deal_terms_stripped: boolean;
  trust: string; is_historical: boolean; label: string; authority: string;
}
interface Retrieval {
  query: string;
  current_facts: Array<Record<string, unknown>>;
  approved_knowledge: Array<KnowledgeItem & { trust: string; label: string; authority: string }>;
  historical_examples: Hit[];
  sources: Array<Record<string, unknown>>;
  resolved: Record<string, unknown>;
  acl: Record<string, unknown>;
  as_of: string;
  labels: Record<string, string>;
}

interface Proposal {
  id: string; version: number; workflow_key: string; action_class: string; status: string;
  evidence: {
    cases?: number; days?: number; accepted?: number; accepted_pct?: number; critical_errors?: number;
    counts?: Record<string, number>; window_from?: string | null; window_to?: string | null;
    median_review_seconds?: number | null;
    samples?: { accepted?: Array<Record<string, unknown>>; edited?: Array<Record<string, unknown>>; declined_or_critical?: Array<Record<string, unknown>> };
  };
  thresholds: Record<string, unknown>;
  proposed_permission: Record<string, unknown>;
  exclusions: string[];
  decided_by: string | null; decided_at: string | null; decision_note: string | null;
  permission_id: string | null; last_asked_at: string | null; asked_count: number;
  regression: Record<string, unknown>; paused_at: string | null; paused_reason: string | null;
  window: { from: string | null; to: string | null }; created_at: string | null; decision: string;
}
interface ProposalsResp { items: Proposal[]; total: number }

function scopeChips(item: KnowledgeItem) {
  const s = item.scope || {};
  const out: Array<{ label: string; title: string }> = [];
  if (s.contact_id) out.push({ label: `Buyer ${s.contact_id.slice(0, 8)}`, title: "Scoped to one contact. It never becomes general policy." });
  if (s.vehicle_id) out.push({ label: `Vehicle ${s.vehicle_id.slice(0, 8)}`, title: "Scoped to one vehicle." });
  if (s.workflow) out.push({ label: `Workflow ${s.workflow}`, title: "Applies to one kind of work." });
  if (!out.length) out.push({ label: "General", title: "Applies everywhere. General policy needs the owner's acceptance." });
  return out;
}

function statusTone(status: string) {
  if (status === "approved") return "ok" as const;
  if (status === "proposed") return "amber" as const;
  if (status === "rejected") return "blocked" as const;
  return "soft" as const;
}

/* ── items ───────────────────────────────────────────────────────────── */

function ItemsBlock({ isOwner, canWrite }: { isOwner: boolean; canWrite: boolean }) {
  const { run, busy } = useCommand();
  const [status, setStatus] = useState<Status>("proposed");
  const [kind, setKind] = useState("");
  const [reasonFor, setReasonFor] = useState<{ item: KnowledgeItem; action: "reject" | "retire" } | null>(null);
  const [reason, setReason] = useState("");
  const isMobile = useIsMobile();

  const qs = [
    status === "all" ? "" : `status=${status}`,
    kind ? `kind=${kind}` : "",
    "limit=200",
  ].filter(Boolean).join("&");
  const q = useQuery<KnowledgeResp>((signal) => api.get<KnowledgeResp>(`/api/knowledge?${qs}`, { signal, tolerate: [404, 501] }), [qs]);
  const items = q.data?.items || [];

  const act = async (item: KnowledgeItem, action: "approve" | "reject" | "retire", why?: string) => {
    const r = await run(`kn:${action}:${item.id}`, `/api/knowledge/${encodeURIComponent(item.id)}/${action}`,
      { expected_version: item.version, reason: why || null },
      {
        success: action === "approve" ? "Approved. It is live knowledge now — no permission changed."
          : action === "reject" ? "Declined. The proposal stays on record."
          : "Retired. It stays auditable but no longer answers as current policy.",
      });
    if (r?.status === "ok") { setReasonFor(null); setReason(""); q.reload(); }
  };

  return (
    <div className="stack">
      <div className="row-wrap" style={{ justifyContent: "space-between" }}>
        <SegmentedControl<Status>
          label="Status"
          value={status}
          onChange={setStatus}
          options={[{ value: "proposed", label: "Proposed" }, { value: "approved", label: "Approved" }, { value: "retired", label: "Retired" }, { value: "rejected", label: "Declined" }, { value: "all", label: "All" }]}
        />
        <Select aria-label="Filter by kind" value={kind} onChange={(e) => setKind(e.target.value)} style={{ width: "auto", minWidth: 170 }}>
          <option value="">Every kind</option>
          {KINDS.map((k) => <option key={k} value={k}>{KIND_LABELS[k]}</option>)}
        </Select>
      </div>

      <GlassPanel clip>
        {q.loading ? <Loading label="Loading knowledge" rows={3} />
          : q.error ? <ErrorState error={q.error} onRetry={q.reload} />
          : q.data === null ? <EmptyState title="Not available yet" body={<span><code>/api/knowledge</code> is not answering. Nothing is hidden here.</span>} />
          : !items.length ? <EmptyState title="Nothing here" body="Corrections, policies and style notes land here as proposals. Nothing counts as knowledge until you approve it." />
          : items.map((item) => (
            <div key={item.id} className="lrn-row">
              <div className="lrn-row__head">
                <div className="lrn-row__main">
                  <div className="lrn-row__title">
                    <span className="truncate">{item.title}</span>
                    <Chip size="sm" tone={statusTone(item.status)}>{humanize(item.status)}</Chip>
                    <Chip size="sm" tone={item.kind === "policy" ? "act" : "soft"}>{KIND_LABELS[item.kind] || humanize(item.kind)}</Chip>
                    {scopeChips(item).map((s) => <Chip key={s.label} size="sm" tone={s.label === "General" ? "amber" : "soft"} title={s.title}>{s.label}</Chip>)}
                    {item.visibility !== "all" ? <Chip size="sm" tone="wait" title="Only roles with that visibility ever retrieve it.">{humanize(item.visibility)} only</Chip> : null}
                  </div>
                  <div className="lrn-row__meta">{item.content}</div>
                  <div className="lrn-row__meta">
                    {item.usable_as?.length ? `Usable as ${item.usable_as.join(", ")}` : "Not usable yet"}
                    {item.classification ? ` · ${humanize(item.classification)}` : ""}
                    {item.source_kind ? ` · from ${humanize(item.source_kind)}` : ""}
                    {item.effective_from ? <> · from <When iso={item.effective_from} format="date" /></> : null}
                    {item.expires_at ? <> · until <When iso={item.expires_at} format="date" /></> : null}
                    {item.approved_at ? <> · approved <When iso={item.approved_at} relative /></> : null}
                    {item.retired_reason ? ` · retired: ${item.retired_reason}` : ""}
                    {item.rejected_reason ? ` · declined: ${item.rejected_reason}` : ""}
                  </div>
                </div>
              </div>

              {item.kind === "policy" && item.status === "proposed" ? (
                <Notice tone="wait" lead="New general policy">This changes how AZKT answers everywhere. Approving activates knowledge only — it grants no permission.</Notice>
              ) : null}
              {item.classification === "factual" && !item.evidence?.length && item.status === "proposed" ? (
                <Notice tone="risk" lead="A fact without evidence">Factual lessons need evidence before they can be approved.</Notice>
              ) : null}

              {item.evidence?.length || Object.keys(item.diff_summary || {}).length ? (
                <Expander title="Evidence and what changed">
                  <div className="stack-sm">
                    {item.evidence?.length ? <JsonDetail value={item.evidence} /> : null}
                    {Object.keys(item.diff_summary || {}).length ? <JsonDetail value={item.diff_summary} /> : null}
                  </div>
                </Expander>
              ) : null}

              <div className="lrn-row__actions">
                <Button size="sm" variant="primary" loading={busy(`kn:approve:${item.id}`)}
                  disabled={!isOwner || item.status !== "proposed"}
                  disabledReason={!isOwner ? "Only the owner approves knowledge." : `Already ${humanize(item.status).toLowerCase()}.`}
                  onClick={() => act(item, "approve")}>Approve</Button>
                <Button size="sm" variant="ghost"
                  disabled={!isOwner || item.status !== "proposed"}
                  disabledReason={!isOwner ? "Only the owner declines a proposal." : `Already ${humanize(item.status).toLowerCase()}.`}
                  onClick={() => { setReasonFor({ item, action: "reject" }); setReason(""); }}>Decline…</Button>
                <Button size="sm" variant="ghost"
                  disabled={!isOwner || item.status !== "approved"}
                  disabledReason={!isOwner ? "Only the owner retires knowledge." : "Only approved knowledge can be retired."}
                  onClick={() => { setReasonFor({ item, action: "retire" }); setReason(""); }}>Retire…</Button>
                {!canWrite ? <span className="fs12 t3">Your role can read knowledge but not change it.</span> : null}
              </div>
            </div>
          ))}
      </GlassPanel>

      <ResponsiveDialog
        mobile={isMobile} open={!!reasonFor} onClose={() => setReasonFor(null)} size="sm"
        title={reasonFor?.action === "retire" ? "Retire this knowledge?" : "Decline this proposal?"}
        footer={<>
          <Button type="submit" form="kn-reason" variant={reasonFor?.action === "retire" ? "danger" : "primary"}
            loading={!!reasonFor && busy(`kn:${reasonFor.action}:${reasonFor.item.id}`)}
            disabled={!reason.trim()} disabledReason="A reason is required — the record keeps it.">
            {reasonFor?.action === "retire" ? "Retire" : "Decline"}
          </Button>
          <Button variant="ghost" onClick={() => setReasonFor(null)}>Cancel</Button>
        </>}
      >
        <form id="kn-reason" className="stack" onSubmit={(e: FormEvent) => { e.preventDefault(); if (reasonFor && reason.trim()) void act(reasonFor.item, reasonFor.action, reason.trim()); }}>
          <div className="fs14 t2">
            {reasonFor?.action === "retire"
              ? "It stays auditable and readable in history, but stops answering as current policy."
              : "The proposal stays on record with your reason. Nothing is deleted."}
          </div>
          <Field label="Reason" required><Input value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
        </form>
      </ResponsiveDialog>
    </div>
  );
}

/* ── corpus ──────────────────────────────────────────────────────────── */

function ReindexDialog({ canWrite, onClose, onDone }: { canWrite: boolean; onClose: () => void; onDone: () => void }) {
  const isMobile = useIsMobile();
  const { run, busy } = useCommand();
  const [sourceKind, setSourceKind] = useState("message");
  const [sourceId, setSourceId] = useState("");
  const [text, setText] = useState("");
  const [visibility, setVisibility] = useState("all");
  const [readmit, setReadmit] = useState(false);

  const blocked = !canWrite ? "Only roles that can teach may re-index."
    : !sourceId.trim() ? "Which source? Give its id."
    : !text.trim() ? "Re-indexing needs the corrected text for that source."
    : null;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (blocked) return;
    const r = await run("corpus:reindex", "/api/knowledge/corpus/reindex", {
      source_kind: sourceKind, source_id: sourceId.trim(), text: text.trim(), visibility, readmit,
    }, { success: "Source re-chunked. Its old chunks were replaced, not duplicated." });
    if (r?.status === "ok") { onDone(); onClose(); }
  };

  return (
    <ResponsiveDialog
      mobile={isMobile} open onClose={onClose} size="md" align="top" title="Re-index one source"
      footer={<><Button type="submit" form="reindex" variant="primary" loading={busy("corpus:reindex")} disabled={!!blocked} disabledReason={blocked || undefined}>Re-index</Button><Button variant="ghost" onClick={onClose}>Cancel</Button></>}
    >
      <form id="reindex" className="stack" onSubmit={submit}>
        <div className="fs13 t2">
          Re-indexing replaces one source's chunks — after a parser upgrade or a correction. A source that was tombstoned
          because access was revoked stays out unless you re-admit it on purpose.
        </div>
        <div className="form-grid">
          <Field label="Source kind">
            <Select value={sourceKind} onChange={(e) => setSourceKind(e.target.value)}>
              {SOURCE_KINDS.map((k) => <option key={k} value={k}>{humanize(k)}</option>)}
            </Select>
          </Field>
          <Field label="Source id" required><Input value={sourceId} onChange={(e) => setSourceId(e.target.value)} placeholder="message / document id" /></Field>
          <Field label="Who may retrieve it">
            <Select value={visibility} onChange={(e) => setVisibility(e.target.value)}>
              <option value="all">Everyone with the record</option>
              <option value="owner">Owner only</option>
              <option value="finance">Finance only</option>
            </Select>
          </Field>
        </div>
        <Field label="Corrected text" required hint="The text to chunk and embed for this source.">
          <Textarea rows={6} value={text} onChange={(e) => setText(e.target.value)} />
        </Field>
        <label className="row" style={{ gap: 8 }}>
          <input type="checkbox" checked={readmit} onChange={(e) => setReadmit(e.target.checked)} />
          <span className="fs13">Re-admit it if it was tombstoned (access was revoked or it was quarantined)</span>
        </label>
      </form>
    </ResponsiveDialog>
  );
}

function CorpusBlock({ canWrite }: { canWrite: boolean }) {
  const cov = useQuery<Coverage>((signal) => api.get<Coverage>("/api/knowledge/corpus", { signal, tolerate: [403, 404, 501] }), []);
  const mans = useQuery<{ items: Manifest[]; total: number }>((signal) => api.get<{ items: Manifest[]; total: number }>("/api/knowledge/corpus/manifests", { signal, tolerate: [403, 404, 501] }), []);
  const [reindex, setReindex] = useState(false);

  if (cov.loading) return <GlassPanel clip><Loading label="Loading corpus coverage" rows={3} /></GlassPanel>;
  if (cov.error) return <GlassPanel clip><ErrorState error={cov.error} onRetry={cov.reload} /></GlassPanel>;
  if (cov.data === null) {
    return <GlassPanel clip><EmptyState title="Coverage not available" body={<span><code>/api/knowledge/corpus</code> is not answering, or your role cannot see it.</span>} /></GlassPanel>;
  }
  const d = cov.data;
  if (!d) return null;
  const m = d.manifest;
  const failures = (d.failures || []) as unknown[];

  return (
    <div className="stack">
      {!d.defined ? (
        <Notice tone="risk" lead="Coverage is undefined" role="alert">
          {d.claim} — without a manifest AZKT cannot claim its knowledge is complete, and it won't.
        </Notice>
      ) : null}

      <div className="kpis">
        <div className="kpi"><span className="kpi__label">Chunks indexed</span><span className="kpi__value">{d.counts.chunks}</span><span className="kpi__sub">{d.counts.embedded} embedded</span></div>
        <div className="kpi"><span className="kpi__label">Knowledge items</span><span className="kpi__value">{Object.values(d.counts.knowledge_items || {}).reduce((a, b) => a + b, 0)}</span><span className="kpi__sub">{Object.entries(d.counts.knowledge_items || {}).map(([k, v]) => `${v} ${k}`).join(" · ") || "none"}</span></div>
        <div className={["kpi", failures.length ? "kpi--risk" : ""].filter(Boolean).join(" ")}><span className="kpi__label">Failures</span><span className="kpi__value">{failures.length}</span><span className="kpi__sub">{failures.length ? "shown below, not hidden" : "none recorded"}</span></div>
        <div className="kpi"><span className="kpi__label">Last indexed</span><span className="kpi__value" style={{ fontSize: 15 }}>{d.last_indexed_at ? <When iso={d.last_indexed_at} format="datetime" /> : "Never"}</span><span className="kpi__sub">{d.embedding.configured ? `embeddings on · ${d.embedding.model || "model not recorded"}` : "embeddings not configured"}</span></div>
      </div>

      <GlassPanel clip>
        <div className="set-row">
          <div className="set-row__main">
            <span className="set-row__title">Coverage window</span>
            <span className="set-row__meta">
              {d.coverage.from || d.coverage.to
                ? <>Declared {d.coverage.from?.slice(0, 10) || "?"} → {d.coverage.to?.slice(0, 10) || "?"}</>
                : "No declared window"}
              {d.coverage.observed_from ? ` · actually holding ${d.coverage.observed_from.slice(0, 10)} → ${d.coverage.observed_to?.slice(0, 10) || "?"}` : " · nothing indexed yet"}
            </span>
          </div>
        </div>
        <div className="set-row">
          <div className="set-row__main">
            <span className="set-row__title">Sources in the manifest</span>
            <span className="set-row__meta">
              {m?.sources?.length
                ? m.sources.map((s, i) => `${String(s.kind ?? "source")}${s.account ? ` · ${String(s.account)}` : ""}${s.scope ? ` · ${String(s.scope)}` : ""}`).join(" | ")
                : "No sources recorded — personal email stays out unless it is explicitly allow-listed."}
            </span>
          </div>
        </div>
        <div className="set-row">
          <div className="set-row__main">
            <span className="set-row__title">Excluded on purpose</span>
            <span className="set-row__meta">
              {Object.entries(d.excluded || {}).length
                ? Object.entries(d.excluded).map(([k, v]) => `${humanize(k)}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`).join(" · ")
                : "Nothing excluded"}
            </span>
          </div>
        </div>
        <div className="set-row">
          <div className="set-row__main">
            <span className="set-row__title">By trust</span>
            <span className="set-row__meta">
              {Object.entries(d.counts.by_trust || {}).map(([k, v]) => `${v} ${humanize(k).toLowerCase()}`).join(" · ") || "Nothing indexed"}
              {" · "}external messages stay evidence, never instructions.
            </span>
          </div>
        </div>
        <div className="set-row">
          <div className="set-row__main">
            <span className="set-row__title">Versions</span>
            <span className="set-row__meta">
              Embedding {m?.embedding?.model || d.embedding.model || "not recorded"}
              {(m?.embedding?.dims || d.embedding.dims) ? ` · ${m?.embedding?.dims || d.embedding.dims} dims` : ""}
              {m?.chunker_version ? ` · chunker ${m.chunker_version}` : ""}
              {m?.parser_version ? ` · parser ${m.parser_version}` : ""}
            </span>
          </div>
          <div className="set-row__right">
            <Button size="sm" variant="soft" disabled={!canWrite} disabledReason="Only roles that can teach may re-index." onClick={() => setReindex(true)}>Re-index a source…</Button>
          </div>
        </div>
      </GlassPanel>

      {failures.length ? (
        <div className="stack-sm">
          <span className="eyebrow">Failures · {failures.length}</span>
          <GlassPanel padded><JsonDetail value={failures} /></GlassPanel>
        </div>
      ) : null}

      {(mans.data?.items || []).length > 1 ? (
        <Expander title={`Manifest history · ${mans.data?.items.length}`}>
          <div className="stack-sm">
            {(mans.data?.items || []).map((x) => (
              <div key={x.id} className="fs13 t2">
                <Chip size="sm" tone={x.status === "active" ? "ok" : "soft"}>{humanize(x.status)}</Chip>{" "}
                {x.label || x.id.slice(0, 8)} · {x.coverage.from?.slice(0, 10) || "?"} → {x.coverage.to?.slice(0, 10) || "?"} ·
                {x.embedding.model || "model not recorded"}{x.superseded_by_id ? " · superseded" : ""}
              </div>
            ))}
          </div>
        </Expander>
      ) : null}

      {reindex ? <ReindexDialog canWrite={canWrite} onClose={() => setReindex(false)} onDone={() => { cov.reload(); mans.reload(); }} /> : null}
    </div>
  );
}

/* ── search ──────────────────────────────────────────────────────────── */

function SearchBlock() {
  const [draft, setDraft] = useState("");
  const [q, setQ] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const res = useQuery<Retrieval | null>(
    async (signal) => (q.trim() ? api.get<Retrieval>(`/api/knowledge/search?q=${encodeURIComponent(q.trim())}&limit=8`, { signal }) : null),
    [q],
  );
  const d = res.data;
  const redacted = (d?.historical_examples || []).some((h) => h.identity_redacted || h.deal_terms_stripped || h.other_customer);

  return (
    <div className="stack">
      <form className="row-wrap" onSubmit={(e) => { e.preventDefault(); setQ(draft); }}>
        <Field label="Search what AZKT knows" className="grow" hint="You see exactly what your role is allowed to retrieve — the filter runs before the search, not after it.">
          <Input ref={inputRef} value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="e.g. deposit rule for a reserved truck" />
        </Field>
        <Button type="submit" variant="soft" size="md" disabled={!draft.trim()} disabledReason="Type something to search for.">Search</Button>
      </form>

      {res.loading ? <GlassPanel clip><Loading label="Searching" rows={2} /></GlassPanel>
        : res.error ? <GlassPanel clip><ErrorState error={res.error} onRetry={res.reload} /></GlassPanel>
        : !d ? null
        : (
          <div className="stack">
            {redacted ? (
              <Notice tone="wait" lead="Redacted">
                Another customer's name, contact details and deal terms are stripped out of examples. Tone survives; their private terms do not.
              </Notice>
            ) : null}

            <Section title="Current facts" count={d.current_facts.length}>
              <GlassPanel padded>
                {d.current_facts.length ? <JsonDetail value={d.current_facts} /> : <span className="fs13 t3">{d.labels.current_facts} — nothing resolved from this question.</span>}
              </GlassPanel>
            </Section>

            <Section title="Approved knowledge" count={d.approved_knowledge.length}>
              <GlassPanel clip>
                {d.approved_knowledge.length ? d.approved_knowledge.map((k) => (
                  <div key={k.id} className="lrn-row">
                    <div className="lrn-row__title">
                      <span className="truncate">{k.title}</span>
                      <Chip size="sm" tone="ok">Approved</Chip>
                      <Chip size="sm" tone="soft">{KIND_LABELS[k.kind] || humanize(k.kind)}</Chip>
                      <Chip size="sm" tone={k.authority === "policy" ? "act" : "soft"}>{k.authority}</Chip>
                      {scopeChips(k).map((s) => <Chip key={s.label} size="sm" tone="soft" title={s.title}>{s.label}</Chip>)}
                    </div>
                    <div className="lrn-row__meta">{k.content}</div>
                  </div>
                )) : <EmptyState title="No approved knowledge answers this" body="Rather than improvising, AZKT says it doesn't know." />}
              </GlassPanel>
            </Section>

            <Section title="Historical examples" count={d.historical_examples.length}>
              <div className="stack-sm">
                <span className="fs12 t3">{d.labels.historical_examples}</span>
                {d.historical_examples.length ? d.historical_examples.map((h) => (
                  <div key={h.chunk_id} className="kn-hit">
                    <div className="kn-hit__meta">
                      <Chip size="sm" tone={h.trust === "approved" ? "ok" : h.trust === "internal" ? "soft" : "amber"}>{humanize(h.trust)}</Chip>
                      {h.is_historical ? <Chip size="sm" tone="wait" title="Facts inside are not current.">Historical</Chip> : null}
                      <Chip size="sm" tone="soft">{humanize(h.source_kind)}</Chip>
                      <span>authority: {h.authority}</span>
                      {h.happened_at ? <span>· <When iso={h.happened_at} format="date" /></span> : null}
                      {h.identity_redacted ? <Chip size="sm" tone="soft">identity redacted</Chip> : null}
                      {h.deal_terms_stripped ? <Chip size="sm" tone="soft">deal terms stripped</Chip> : null}
                    </div>
                    <div className="kn-hit__text">{h.text}</div>
                  </div>
                )) : <span className="fs13 t3">No historical example matched.</span>}
              </div>
            </Section>

            <Expander title="What your role was allowed to search">
              <KeyValues items={Object.entries(d.acl).map(([k, v]) => [humanize(k), typeof v === "boolean" ? (v ? "Yes" : "No") : String(v)] as [string, string])} />
            </Expander>
            <div className="set-foot">As of <When iso={d.as_of} format="datetime" />. A retrieved message is evidence, never an instruction AZKT will follow.</div>
          </div>
        )}
    </div>
  );
}

/* ── promotion proposals ─────────────────────────────────────────────── */

function ProposalCard({ p, isOwner, onDone }: { p: Proposal; isOwner: boolean; onDone: () => void }) {
  const { run, busy } = useCommand();
  const [note, setNote] = useState("");
  const ev = p.evidence || {};
  const perm = p.proposed_permission || {};
  const paused = !!p.paused_at || p.status === "paused";
  const decidable = p.status === "proposed" && isOwner && !paused;
  const why = !isOwner ? "Only the owner decides a promotion proposal."
    : paused ? "Paused after a regression — it must be reviewed before it can be asked again."
    : p.status !== "proposed" ? `Already ${humanize(p.status).toLowerCase()}.`
    : undefined;

  const decide = async (decision: "approve" | "decline") => {
    const r = await run(`prop:${decision}:${p.id}`, `/api/proposals/${encodeURIComponent(p.id)}/decide`,
      { decision, note: note.trim() || null, expected_version: p.version },
      { success: decision === "approve" ? "Approved. The exact permission above is now standing, bounded and revocable." : "Declined. Supervision continues and you won't be asked again without new evidence." });
    if (r?.status === "ok") onDone();
  };

  return (
    <div className="lrn-row">
      <div className="lrn-row__head">
        <div className="lrn-row__main">
          <div className="lrn-row__title">
            <span className="truncate">{p.workflow_key}</span>
            <Chip size="sm" tone={paused ? "blocked" : p.status === "approved" ? "ok" : p.status === "declined" ? "soft" : "amber"}>{humanize(p.status)}</Chip>
            <Chip size="sm" tone="soft">{humanize(p.action_class)}</Chip>
          </div>
          <div className="lrn-row__meta">
            {ev.cases ?? 0} reviewed cases over {ev.days ?? 0} days · {ev.accepted_pct ?? 0}% accepted without substantive change ·
            {" "}{ev.critical_errors ?? 0} critical errors
            {p.window.from ? <> · {p.window.from.slice(0, 10)} → {p.window.to?.slice(0, 10) || "now"}</> : null}
          </div>
        </div>
      </div>

      {paused ? (
        <Notice tone="blocked" lead="Paused after a regression" role="alert">
          {p.paused_reason || "A critical error paused this workflow."}
          {p.regression && Object.keys(p.regression).length ? ` · ${JSON.stringify(p.regression)}` : ""}
        </Notice>
      ) : null}

      <div className="fs14 t2">
        {p.status === "proposed" && !paused
          ? `AZKT handled ${ev.cases ?? 0} of these; ${ev.accepted ?? 0} needed no substantive change. May it do this specific type automatically, inside the limits below?`
          : p.decision_note || ""}
      </div>

      <Expander title="The exact permission being asked for" defaultOpen={p.status === "proposed" && !paused}>
        <div className="stack-sm">
          <KeyValues items={[
            ["Action pattern", String(perm.action_pattern ?? "not recorded")],
            ["Recipients", Array.isArray(perm.recipients) && perm.recipients.length ? (perm.recipients as string[]).join(", ") : "only the reviewed set"],
            ["Domains", Array.isArray(perm.domains) && perm.domains.length ? (perm.domains as string[]).join(", ") : "none beyond the reviewed set"],
            ["Rate limit", perm.rate_limit ? JSON.stringify(perm.rate_limit) : "none"],
            ["Per-action limit", perm.per_action_limit ? String(perm.per_action_limit) : "not applicable"],
            ["Expires", perm.expires_at ? String(perm.expires_at).slice(0, 10) : "not recorded"],
            ["Monitoring", perm.monitoring ? JSON.stringify(perm.monitoring) : "not recorded"],
          ]} />
          <div className="spec-block">
            <span className="spec-block__title">Never automatic — always still reviewed</span>
            <ul>
              {(p.exclusions.length ? p.exclusions : (perm.excluded_cases as string[]) || []).map((x, i) => <li key={i}>{x}</li>)}
              {(Array.isArray(perm.still_requires_review) ? (perm.still_requires_review as string[]) : []).map((x, i) => <li key={`r${i}`}>{x}</li>)}
            </ul>
          </div>
        </div>
      </Expander>

      <Expander title="Review samples and outcome counts">
        <div className="stack-sm">
          <KeyValues items={Object.entries(ev.counts || {}).map(([k, v]) => [humanize(k), String(v)] as [string, string])} />
          {ev.samples ? <JsonDetail value={ev.samples} /> : <span className="fs13 t3">No samples recorded.</span>}
          {Object.keys(p.thresholds || {}).length ? (
            <>
              <span className="spec-block__title">Thresholds these were measured against</span>
              <KeyValues items={Object.entries(p.thresholds).map(([k, v]) => [humanize(k), String(v)] as [string, string])} />
            </>
          ) : null}
        </div>
      </Expander>

      {decidable ? <Field label="Note (recorded with your decision)"><Input value={note} onChange={(e) => setNote(e.target.value)} /></Field> : null}

      <div className="lrn-row__actions">
        <Button size="sm" variant="primary" loading={busy(`prop:approve:${p.id}`)} disabled={!decidable} disabledReason={why} onClick={() => decide("approve")}>Allow it, within these limits</Button>
        <Button size="sm" variant="ghost" loading={busy(`prop:decline:${p.id}`)} disabled={!decidable} disabledReason={why} onClick={() => decide("decline")}>Keep reviewing everything</Button>
        {p.asked_count > 1 ? <span className="fs12 t3">Asked {p.asked_count} times{p.last_asked_at ? " · last " : ""}{p.last_asked_at ? <When iso={p.last_asked_at} relative /> : null}</span> : null}
      </div>
    </div>
  );
}

function ProposalsBlock({ isOwner, canSee }: { isOwner: boolean; canSee: boolean }) {
  const q = useQuery<ProposalsResp>((signal) => api.get<ProposalsResp>("/api/proposals?limit=100", { signal, tolerate: [403, 404, 501] }), []);
  if (!canSee) {
    return <GlassPanel clip><EmptyState title="Owner only" body="Promotion proposals ask for a standing permission, so only the owner sees and decides them." /></GlassPanel>;
  }
  if (q.loading) return <GlassPanel clip><Loading label="Loading proposals" rows={2} /></GlassPanel>;
  if (q.error) return <GlassPanel clip><ErrorState error={q.error} onRetry={q.reload} /></GlassPanel>;
  if (q.data === null) return <GlassPanel clip><EmptyState title="Not available yet" body={<span><code>/api/proposals</code> is not answering.</span>} /></GlassPanel>;
  const items = q.data?.items || [];
  return (
    <div className="stack">
      <GlassPanel clip>
        {!items.length
          ? <EmptyState title="Nothing is asking to run on its own" body="AZKT only asks after enough reviewed evidence, and never more than once every two weeks without new evidence." />
          : items.map((p) => <ProposalCard key={p.id} p={p} isOwner={isOwner} onDone={q.reload} />)}
      </GlassPanel>
      <div className="set-foot">
        AZKT cannot enable itself: approving a proposal is the only thing that creates the permission, and the permission is
        exactly the one shown — bounded, expiring and revocable. A critical regression pauses the workflow and its proposals.
      </div>
    </div>
  );
}

/* ── section ─────────────────────────────────────────────────────────── */

export function KnowledgeSection() {
  const { user } = useAuth();
  const isOwner = user?.role === "owner";
  const canWrite = can(user, "knowledge.write");
  const canProposals = can(user, "permissions");

  return (
    <div className="stack-lg">
      <Section title="What AZKT knows">
        <ItemsBlock isOwner={!!isOwner} canWrite={canWrite} />
      </Section>

      <Section title="Corpus coverage">
        <CorpusBlock canWrite={canWrite} />
      </Section>

      <Section title="Search">
        <SearchBlock />
      </Section>

      <Section title="Asking to run on its own">
        <ProposalsBlock isOwner={!!isOwner} canSee={canProposals} />
      </Section>
    </div>
  );
}
