/* Shared approvals queue (route /approvals, owner). GET /api/approvals?status=&kind=&limit=&offset=.
   Filters live in the URL so returning to the list restores them. Rows open the exact review. */
import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { ApiError, api } from "../../lib/api";
import { useQuery } from "../../lib/useQuery";
import { Button, Chip, EmptyState, ErrorState, GlassPanel, ListGroup, Loading, PageHeader, SegmentedControl } from "../../ui";
import { ApprovalRow } from "./ApprovalRow";
import { KIND_LABELS, type ApprovalListResp, type ApprovalSummary } from "./types";

type View = "pending" | "inflight" | "attention" | "done" | "all";
const VIEW_STATUS: Record<View, string> = {
  pending: "pending",
  inflight: "approved,queued,executing",
  attention: "failed,result_unknown,handed_off",
  done: "confirmed,declined,expired,invalidated,canceled",
  all: "all",
};
const PAGE = 50;

export default function ApprovalsList() {
  const [params, setParams] = useSearchParams();
  const view = (["pending", "inflight", "attention", "done", "all"].includes(params.get("view") || "") ? params.get("view") : "pending") as View;
  const kind = params.get("kind") || "";
  const offset = Math.max(0, Number(params.get("offset") || 0) || 0);
  const set = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) { if (v === null || v === "") next.delete(k); else next.set(k, v); }
    setParams(next, { replace: true });
  };
  const url = useMemo(() => `/api/approvals?status=${encodeURIComponent(VIEW_STATUS[view])}${kind ? `&kind=${encodeURIComponent(kind)}` : ""}&limit=${PAGE}&offset=${offset}`, [view, kind, offset]);
  const q = useQuery<ApprovalListResp>((signal) => api.get<ApprovalListResp>(url, { signal }), [url]);
  const summary = useQuery<ApprovalSummary | null>((signal) => api.get<ApprovalSummary | null>("/api/approvals/summary", { signal, tolerate: [403, 404] }), []);
  const denied = q.error instanceof ApiError && q.error.isDenied;
  const items = q.data?.items || [];
  const total = q.data?.total ?? 0;
  const kinds = Object.keys(KIND_LABELS);

  return (
    <div className="page">
      <PageHeader title="Approvals" subtitle="Every consequential action waits here with its exact payload. Approved does not mean executed — each row shows the provider's answer.">
        <div className="row-wrap" style={{ justifyContent: "space-between" }}>
          <SegmentedControl<View>
            label="Approval state"
            size="sm"
            value={view}
            onChange={(v) => set({ view: v, offset: null })}
            options={[
              { value: "pending", label: "Pending", count: summary.data?.pending },
              { value: "inflight", label: "In flight", count: summary.data?.executing },
              { value: "attention", label: "Needs attention", count: summary.data ? summary.data.failed + summary.data.unknown + (summary.data.by_status?.handed_off || 0) : undefined },
              { value: "done", label: "Done" },
              { value: "all", label: "All" },
            ]}
          />
          <div className="row-wrap" style={{ gap: 6, overflowX: "auto", flexWrap: "nowrap", maxWidth: "100%" }} role="group" aria-label="Kind">
            <Chip size="sm" selected={!kind} onClick={() => set({ kind: null, offset: null })}>Any kind</Chip>
            {kinds.map((k) => <Chip key={k} size="sm" selected={kind === k} onClick={() => set({ kind: k, offset: null })}>{KIND_LABELS[k]}</Chip>)}
          </div>
        </div>
      </PageHeader>

      {denied ? (
        <GlassPanel clip><EmptyState title="Only the owner reviews approvals" body="Your drafts and requests appear here once the owner opens them." /></GlassPanel>
      ) : q.loading && !q.data ? (
        <GlassPanel clip><Loading label="Loading approvals" rows={3} /></GlassPanel>
      ) : q.error ? (
        <GlassPanel clip><ErrorState error={q.error} onRetry={q.reload} /></GlassPanel>
      ) : !items.length ? (
        <GlassPanel clip>
          <EmptyState
            title={view === "pending" ? "Nothing waiting for a decision" : view === "attention" ? "Nothing failed, unknown or handed off" : "Nothing here"}
            body={view === "pending" ? "Drafts, orders and publications land here with the exact payload, recipient and checks." : view === "attention" ? "Failed sends, unknown provider results and anything handed to a person would show here with their receipts." : "Change the state filter to see more."}
          />
        </GlassPanel>
      ) : (
        <>
          <ListGroup aria-label="Approvals">
            {items.map((a) => <ApprovalRow key={a.id} approval={a} hideState={view === "pending"} />)}
          </ListGroup>
          {total > PAGE ? (
            <div className="row-wrap" style={{ justifyContent: "space-between" }}>
              <span className="fs13 t3 tnum">{offset + 1}–{Math.min(offset + PAGE, total)} of {total}</span>
              <div className="row">
                <Button size="sm" variant="soft" disabled={offset === 0} disabledReason="This is the first page." onClick={() => set({ offset: String(Math.max(0, offset - PAGE)) })}>Newer</Button>
                <Button size="sm" variant="soft" disabled={offset + PAGE >= total} disabledReason="No older approvals." onClick={() => set({ offset: String(offset + PAGE) })}>Older</Button>
              </div>
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}
