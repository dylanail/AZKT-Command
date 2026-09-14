/* Activity: append-only business events, newest first. GET /api/activity?kind=&entity_kind=&exceptions_only=&since=&q=
   for the compact list; expanding a row fetches GET /api/activity/{id} (receipt, sources, run/mission/correlation
   ids, policy version). Money the server scrubbed shows as "hidden". Filters live in the URL. */
import { useEffect, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ApiError, api } from "../../lib/api";
import { useQuery } from "../../lib/useQuery";
import { entityHref, entityLabel, humanize, shortId } from "../../lib/links";
import { Avatar, Button, Chip, EmptyState, ErrorState, Field, GlassPanel, Input, KeyValues, Loading, NotRecorded, PageHeader, SearchIcon, Select, When } from "../../ui";
import { JsonDetail, isEmptyValue } from "../shared/JsonDetail";
import { actorName, type ActorRef } from "../approvals/types";

interface ActivityBrief {
  id: string; at: string | null; actor: ActorRef; what: string; entity_kind: string | null; entity_id: string | null;
  kind: string; state: string | null; exception: boolean; visibility: string;
  has_receipt: boolean; has_sources: boolean; has_details: boolean; mission_id: string | null; run_id: string | null;
}
interface ActivityDetail extends ActivityBrief {
  receipt: Record<string, unknown>; details: Record<string, unknown>; sources: unknown[]; command_name: string | null;
  correlation_id: string | null; policy_version: string | null; version: number; created_at: string | null;
}
interface ListResp { items: ActivityBrief[]; total: number; limit: number; offset: number; money_hidden: boolean; scoped: boolean }

const KINDS = ["automation", "approval", "message", "task", "vehicle", "fact", "intake", "payment", "publication", "shipment", "bid", "contact", "access", "connection", "permission", "system"];
const ENTITY_KINDS = ["vehicle", "task", "contact", "approval", "shipment", "import_request", "candidate", "listing", "opportunity", "user", "invitation", "connection", "setting"];
const SINCE: Record<string, () => string | null> = {
  today: () => { const d = new Date(); d.setHours(0, 0, 0, 0); return d.toISOString(); },
  "7d": () => new Date(Date.now() - 7 * 86400000).toISOString(),
  "30d": () => new Date(Date.now() - 30 * 86400000).toISOString(),
  all: () => null,
};
const PAGE = 50;

function stateTone(state: string | null, exception: boolean): string {
  if (exception) return "var(--risk)";
  if (!state) return "var(--t3)";
  if (["confirmed", "verified", "active", "connected", "granted", "approved", "done", "completed"].includes(state)) return "var(--ok)";
  if (["failed", "blocked", "invalidated", "revoked", "disabled", "expired", "declined", "disconnected"].includes(state)) return "var(--blocked)";
  if (["queued", "pending", "executing", "paused", "unknown"].includes(state)) return "var(--wait)";
  return "var(--t3)";
}

function ActivityRow({ e, moneyHidden }: { e: ActivityBrief; moneyHidden: boolean }) {
  const [open, setOpen] = useState(false);
  const detail = useQuery<ActivityDetail | null>(async (signal) => (open ? api.get<ActivityDetail>(`/api/activity/${encodeURIComponent(e.id)}`, { signal }) : null), [open, e.id]);
  const href = entityHref(e.entity_kind, e.entity_id);
  const who = actorName(e.actor);
  const entity = e.entity_kind ? `${entityLabel(e.entity_kind)}${e.entity_id ? ` ${shortId(e.entity_id)}` : ""}` : null;
  const d = detail.data;
  return (
    <div className="act-row">
      <button type="button" className="act-row__head" aria-expanded={open} aria-controls={`act-${e.id}`} onClick={() => setOpen((o) => !o)}>
        <span className="act-row__time"><When iso={e.at} format="datetime" /></span>
        <Avatar name={e.actor?.kind === "user" ? who : e.actor?.kind === "external" ? "External" : "AZKT"} size="sm" />
        <span className="stack-sm" style={{ gap: 1, minWidth: 0 }}>
          <span className={["act-row__what", e.exception ? "act-row__what--exc" : ""].join(" ")}>{e.what}</span>
          <span className="act-row__meta">{who}{entity ? ` · ${entity}` : ""}{e.kind ? ` · ${humanize(e.kind)}` : ""}</span>
        </span>
        <span className="act-row__state" style={{ color: stateTone(e.state, e.exception) }}>{e.exception ? "Exception" : e.state ? humanize(e.state) : ""}</span>
      </button>
      {open ? (
        <div id={`act-${e.id}`} className="act-row__body">
          {detail.loading ? <Loading label="Loading details" rows={2} /> : detail.error ? (
            <ErrorState error={detail.error} onRetry={detail.reload} title={detail.error instanceof ApiError && detail.error.isNotFound ? "Details aren't visible to you" : undefined} />
          ) : d ? (
            <>
              <div className="eyebrow">Sources and technical details</div>
              <KeyValues items={[
                ["Receipt", isEmptyValue(d.receipt) ? <NotRecorded text={e.has_receipt ? "Hidden" : "No external receipt — internal change"} /> : <JsonDetail value={d.receipt} />],
                ["Sources", isEmptyValue(d.sources) ? <NotRecorded text="None attached" /> : <JsonDetail value={d.sources} />],
                ["Details", isEmptyValue(d.details) ? <NotRecorded text="None" /> : <JsonDetail value={d.details} />],
                ["Run", d.run_id || d.mission_id || d.correlation_id ? <span className="tnum">{[d.run_id ? `run ${d.run_id}` : null, d.mission_id ? `mission ${d.mission_id}` : null, d.correlation_id ? `correlation ${d.correlation_id}` : null].filter(Boolean).join(" · ")}</span> : <NotRecorded text="No run — done directly" />],
                ["Policy", d.policy_version ? `policy ${d.policy_version}${d.command_name ? ` · ${d.command_name}` : ""}` : d.command_name || <NotRecorded />],
                ["Entry", <span className="tnum">{d.id} · v{d.version}{d.visibility && d.visibility !== "all" ? ` · ${d.visibility} only` : ""}</span>],
              ]} />
              {moneyHidden && (d.receipt?.money_hidden || d.details?.money_hidden) ? <span className="fs12 t4">Money values are hidden for your role.</span> : null}
              <div className="row-wrap">
                {href ? <Button size="sm" variant="soft" to={href}>Open {entityLabel(e.entity_kind)}</Button> : <Button size="sm" variant="soft" disabled disabledReason={e.entity_kind ? "This record has no page yet." : "This entry is not about one record."}>Open record</Button>}
                {d.correlation_id ? <Link to={`/activity?correlation_id=${encodeURIComponent(d.correlation_id)}`} className="fs13">Everything in this run</Link> : null}
              </div>
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export default function Activity() {
  const [params, setParams] = useSearchParams();
  const kind = params.get("kind") || "";
  const entityKind = params.get("entity_kind") || "";
  const exc = params.get("exc") === "1";
  const since = params.get("since") || "7d";
  const q = params.get("q") || "";
  const correlation = params.get("correlation_id") || "";
  const offset = Math.max(0, Number(params.get("offset") || 0) || 0);
  const [draft, setDraft] = useState(q);
  useEffect(() => setDraft(q), [q]);

  const set = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) { if (v === null || v === "") next.delete(k); else next.set(k, v); }
    if (!("offset" in patch)) next.delete("offset");
    setParams(next, { replace: true });
  };
  const sinceIso = (SINCE[since] || SINCE["7d"])();
  const qs = new URLSearchParams();
  if (kind) qs.set("kind", kind);
  if (entityKind) qs.set("entity_kind", entityKind);
  if (exc) qs.set("exceptions_only", "true");
  if (sinceIso && !correlation) qs.set("since", sinceIso);
  if (q) qs.set("q", q);
  if (correlation) qs.set("correlation_id", correlation);
  qs.set("limit", String(PAGE));
  qs.set("offset", String(offset));
  const url = `/api/activity?${qs.toString()}`;
  const list = useQuery<ListResp>((signal) => api.get<ListResp>(url, { signal }), [url]);
  const denied = list.error instanceof ApiError && list.error.isDenied;
  const items = list.data?.items || [];
  const total = list.data?.total ?? 0;
  const anyFilter = !!(kind || entityKind || exc || q || correlation || since !== "7d");

  const submit = (e: FormEvent) => { e.preventDefault(); set({ q: draft.trim() || null }); };

  return (
    <div className="page" style={{ maxWidth: 1040 }}>
      <PageHeader title="Activity" subtitle="Everything AZKT and your team did, newest first. Expand a row for the receipt.">
        <form className="row-wrap" onSubmit={submit} style={{ gap: 8 }}>
          <div className="row" style={{ flex: "1 1 220px", minWidth: 0 }}>
            <Input pill value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="Search what happened…" aria-label="Search activity" style={{ height: 36 }} />
            <Button type="submit" size="md" variant="soft" iconLeft={<SearchIcon />}>Search</Button>
          </div>
          <Select aria-label="Kind" value={kind} onChange={(e) => set({ kind: e.target.value })} style={{ width: "auto", height: 36, fontSize: 13 }}>
            <option value="">Any kind</option>
            {KINDS.map((k) => <option key={k} value={k}>{humanize(k)}</option>)}
          </Select>
          <Select aria-label="Record type" value={entityKind} onChange={(e) => set({ entity_kind: e.target.value })} style={{ width: "auto", height: 36, fontSize: 13 }}>
            <option value="">Any record</option>
            {ENTITY_KINDS.map((k) => <option key={k} value={k}>{humanize(k)}</option>)}
          </Select>
          <Select aria-label="Since" value={since} onChange={(e) => set({ since: e.target.value })} style={{ width: "auto", height: 36, fontSize: 13 }} disabled={!!correlation} title={correlation ? "Showing one run; time filter is off." : undefined}>
            <option value="today">Today</option>
            <option value="7d">Last 7 days</option>
            <option value="30d">Last 30 days</option>
            <option value="all">All time</option>
          </Select>
          <Chip size="sm" tone={exc ? "risk" : "neutral"} selected={exc} onClick={() => set({ exc: exc ? null : "1" })}>Exceptions only</Chip>
          {correlation ? <Chip size="sm" tone="soft" onRemove={() => set({ correlation_id: null })} removeLabel="Clear run filter">Run {shortId(correlation)}</Chip> : null}
          {anyFilter ? <button type="button" className="linklike fs13" onClick={() => setParams(new URLSearchParams(), { replace: true })}>Clear filters</button> : null}
        </form>
      </PageHeader>

      {list.data?.scoped ? <div className="set-foot">Showing activity on your assigned vehicles and tasks, plus your own actions.</div> : null}

      <GlassPanel clip>
        {denied ? (
          <EmptyState title="Activity isn't available to your role" body="Ask the owner if you need to see the log." />
        ) : list.loading && !list.data ? <Loading label="Loading activity" rows={4} /> : list.error ? <ErrorState error={list.error} onRetry={list.reload} /> : !items.length ? (
          <EmptyState title={anyFilter ? "Nothing matches these filters" : "Nothing recorded yet"} body={anyFilter ? "Widen the time range or clear a filter." : "Every automated action writes one row here with its receipt."} action={anyFilter ? <Button size="sm" variant="soft" onClick={() => setParams(new URLSearchParams(), { replace: true })}>Clear filters</Button> : undefined} />
        ) : (
          <div role="list" aria-label="Activity">
            {items.map((e) => <div role="listitem" key={e.id}><ActivityRow e={e} moneyHidden={!!list.data?.money_hidden} /></div>)}
          </div>
        )}
      </GlassPanel>

      {total > PAGE ? (
        <div className="row-wrap" style={{ justifyContent: "space-between" }}>
          <span className="fs13 t3 tnum">{offset + 1}–{Math.min(offset + PAGE, total)} of {total}</span>
          <div className="row">
            <Button size="sm" variant="soft" disabled={offset === 0} disabledReason="This is the newest page." onClick={() => set({ offset: String(Math.max(0, offset - PAGE)) })}>Newer</Button>
            <Button size="sm" variant="soft" disabled={offset + PAGE >= total} disabledReason="No older entries." onClick={() => set({ offset: String(offset + PAGE) })}>Older</Button>
          </div>
        </div>
      ) : null}
      {list.data?.money_hidden ? <div className="set-foot">Money values are hidden for your role; the log still shows what happened.</div> : null}
    </div>
  );
}
