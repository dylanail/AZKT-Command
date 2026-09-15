/* Settings › Usage (owner). Model budget from GET /api/health (settings perm) plus the legacy cost report if the
   server still exposes one (GET /api/legacy/costs, 404 tolerated). */
import { Link } from "react-router-dom";
import { api } from "../../../lib/api";
import { useQuery } from "../../../lib/useQuery";
import { Button, ErrorState, GlassPanel, Loading, Money, Notice } from "../../../ui";
import { JsonDetail } from "../../shared/JsonDetail";
import type { HealthResp } from "./types";

export function UsageSection() {
  const q = useQuery<HealthResp>((signal) => api.get<HealthResp>("/api/health", { signal }), []);
  const legacy = useQuery<unknown>((signal) => api.get<unknown>("/api/legacy/costs", { signal, tolerate: [404, 501] }), []);
  const m = q.data?.model;
  const capText = (cap?: string) => (cap && Number(cap) > 0 ? <Money amount={cap} currency="USD" /> : "no cap");
  return (
    <div className="stack-lg">
      <div className="stack">
        <div className="eyebrow">Model budget</div>
        {q.loading ? <GlassPanel clip><Loading rows={2} /></GlassPanel> : q.error ? <GlassPanel clip><ErrorState error={q.error} onRetry={q.reload} /></GlassPanel> : m ? (
          <>
            {m.over_cap ? <Notice tone="blocked" lead="Over budget" role="alert">Drafting and summaries are paused until the window resets or you raise the cap under Automation. Deterministic lists keep working.</Notice> : m.api_key_present === false ? <Notice tone="risk" lead="No model key">Set ANTHROPIC_API_KEY on the server. AZKT works without a model; drafts and summaries wait.</Notice> : null}
            <div className="kpis">
              <div className={`kpi kpi--${m.over_cap ? "blocked" : "ok"}`}><span className="kpi__label">Today</span><span className="kpi__value"><Money amount={m.day_usd ?? null} currency="USD" /></span><span className="kpi__sub">cap {capText(m.daily_cap_usd)}</span></div>
              <div className={`kpi kpi--${m.over_cap ? "blocked" : "ok"}`}><span className="kpi__label">This month</span><span className="kpi__value"><Money amount={m.month_usd ?? null} currency="USD" /></span><span className="kpi__sub">cap {capText(m.monthly_cap_usd)}</span></div>
              <div className="kpi"><span className="kpi__label">Key</span><span className="kpi__value" style={{ fontSize: 16 }}>{m.api_key_present ? "Present" : "Missing"}</span><span className="kpi__sub">{m.configured ? "budget configured" : "no budget set"}</span></div>
            </div>
            <div className="row-wrap"><Button size="sm" variant="soft" to="/settings/automation">Change caps</Button><span className="fs13 t3">Spend is metered per call by the model adapter; caps are enforced before a call is made.</span></div>
          </>
        ) : <GlassPanel clip><ErrorState error={new Error("The health endpoint did not include a model budget. Only the owner sees it.")} /></GlassPanel>}
      </div>
      <div className="stack">
        <div className="eyebrow">Legacy costs</div>
        {legacy.loading ? <GlassPanel clip><Loading rows={1} /></GlassPanel> : legacy.error ? <GlassPanel clip><ErrorState error={legacy.error} onRetry={legacy.reload} /></GlassPanel> : legacy.data === null ? (
          <div className="set-foot">The legacy cost report (<code>/api/legacy/costs</code>) is not served by this backend. Vehicle costs and ledger matching live in <Link to="/finance">Finance</Link>.</div>
        ) : <GlassPanel padded><JsonDetail value={legacy.data} /></GlassPanel>}
      </div>
    </div>
  );
}
