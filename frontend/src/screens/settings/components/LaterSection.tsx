/* External agents · Procedures · Knowledge: shells for sections that land in a later stage.
   Each probes its endpoint (404 → truthful "Not available yet") and keeps the intended controls disabled with reasons. */
import { api } from "../../../lib/api";
import { useQuery } from "../../../lib/useQuery";
import { Button, EmptyState, ErrorState, GlassPanel, ListGroup, ListRow, Loading } from "../../../ui";
import { JsonDetail } from "../../shared/JsonDetail";

interface Spec { endpoint: string; title: string; blurb: string; emptyTitle: string; controls: { label: string; reason: string; primary?: boolean }[] }
const SPECS: Record<"external-agents" | "procedures" | "knowledge", Spec> = {
  "external-agents": {
    endpoint: "/api/external-clients",
    title: "External agents",
    blurb: "Other agents connect through the Manager with a scoped client key. They can only do what the client scope and your grants both allow; every action is recorded and, when consequential, waits for your approval.",
    emptyTitle: "No external agent clients yet",
    controls: [{ label: "Add client", reason: "External agent clients arrive in a later stage.", primary: true }, { label: "Rotate key", reason: "No client to rotate yet." }],
  },
  procedures: {
    endpoint: "/api/procedures",
    title: "Procedures / Teach",
    blurb: "Teach AZKT how you do things: a step list with the sources it may use and the checks it must pass. Versioned; a change re-checks anything pending.",
    emptyTitle: "No procedures recorded",
    controls: [{ label: "New procedure", reason: "Teach arrives in a later stage.", primary: true }, { label: "Import from a demonstration", reason: "Learning from demonstrations arrives with Teach." }],
  },
  knowledge: {
    endpoint: "/api/knowledge",
    title: "Knowledge",
    blurb: "Corpus coverage, retrieval sources and corrections. Answers cite what they used; gaps show here instead of being papered over.",
    emptyTitle: "Knowledge index not available",
    controls: [{ label: "Re-index", reason: "The knowledge service arrives in a later stage.", primary: true }, { label: "Export corpus", reason: "Corpus export arrives with the knowledge service." }],
  },
};

export function LaterSection({ id }: { id: keyof typeof SPECS }) {
  const spec = SPECS[id];
  const q = useQuery<unknown>((signal) => api.get<unknown>(spec.endpoint, { signal, tolerate: [404, 501] }), [spec.endpoint]);
  const items = Array.isArray(q.data) ? q.data : (q.data && typeof q.data === "object" && Array.isArray((q.data as { items?: unknown[] }).items)) ? (q.data as { items: unknown[] }).items : null;
  return (
    <div className="stack">
      <div className="fs14 t2" style={{ maxWidth: 640 }}>{spec.blurb}</div>
      <div className="row-wrap">
        {spec.controls.map((c) => <Button key={c.label} variant={c.primary ? "primary" : "soft"} size="md" disabled disabledReason={c.reason}>{c.label}</Button>)}
      </div>
      {q.loading ? <GlassPanel clip><Loading rows={2} /></GlassPanel> : q.error ? <GlassPanel clip><ErrorState error={q.error} onRetry={q.reload} /></GlassPanel> : q.data === null ? (
        <GlassPanel clip><EmptyState title="Not available yet" body={<span><code>{spec.endpoint}</code> is not answering. This section fills in when that service lands; nothing is hidden here.</span>} /></GlassPanel>
      ) : items && items.length === 0 ? (
        <GlassPanel clip><EmptyState title={spec.emptyTitle} /></GlassPanel>
      ) : items ? (
        <ListGroup aria-label={spec.title}>
          {items.map((it, i) => {
            const o = (it && typeof it === "object" ? it : {}) as Record<string, unknown>;
            const name = String(o.name || o.label || o.title || o.id || `Item ${i + 1}`);
            const meta = [o.status, o.scope, o.version !== undefined ? `v${String(o.version)}` : null].filter(Boolean).map(String).join(" · ");
            return <ListRow key={String(o.id || i)} title={name} meta={meta || undefined} />;
          })}
        </ListGroup>
      ) : (
        <GlassPanel padded><JsonDetail value={q.data} /></GlassPanel>
      )}
    </div>
  );
}
