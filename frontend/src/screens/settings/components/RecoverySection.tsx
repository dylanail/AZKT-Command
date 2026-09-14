/* Settings › Recovery. System health from GET /api/health (owner): worker heartbeat, queue, outbox, external actions
   with unknown/failed results, overdue reminders, connections, storage. Plus passkeys and Sign out for everyone. */
import { api } from "../../../lib/api";
import { useAuth } from "../../../lib/auth";
import { useQuery } from "../../../lib/useQuery";
import { can } from "../../../lib/perms";
import { humanize } from "../../../lib/links";
import { Button, ErrorState, GlassPanel, HealthLabel, Loading, Notice, When } from "../../../ui";
import { PasskeysBlock } from "./PasskeysBlock";
import { freshnessHealth, type HealthResp } from "./types";

function lag(s: number): string {
  if (!s) return "no backlog";
  if (s < 90) return `${s}s behind`;
  if (s < 5400) return `${Math.round(s / 60)} min behind`;
  return `${Math.round(s / 3600)} h behind`;
}

export function SystemHealth({ withModel = false }: { withModel?: boolean }) {
  const q = useQuery<HealthResp>((signal) => api.get<HealthResp>("/api/health", { signal }), []);
  if (q.loading) return <GlassPanel clip><Loading label="Checking system health" rows={3} /></GlassPanel>;
  if (q.error) return <GlassPanel clip><ErrorState error={q.error} onRetry={q.reload} /></GlassPanel>;
  const h = q.data;
  if (!h) return null;
  const jobs = h.jobs?.by_state || {};
  const queued = (jobs.queued || 0) + (jobs.running || 0);
  const failedJobs = jobs.failed || 0;
  return (
    <div className="stack">
      <div className="between">
        <div className="eyebrow">System <span style={{ textTransform: "none", letterSpacing: 0 }}>· {h.environment} · as of <When iso={h.as_of} format="time" /></span></div>
        <Button size="xs" variant="ghost" onClick={q.reload}>Refresh</Button>
      </div>
      {!h.ok ? <Notice tone="risk" lead={!h.worker.ok ? "Worker not heartbeating" : "Unknown external results"} role="alert">{!h.worker.ok ? "Reminders, syncs and queued actions do not run until the worker is back." : `${h.external_actions.unknown} external ${h.external_actions.unknown === 1 ? "action" : "actions"} may have reached a provider without a receipt; they are reconciled by reference, never retried blindly.`}</Notice> : null}
      <div className="kpis">
        <div className={`kpi kpi--${h.worker.ok ? "ok" : "blocked"}`}><span className="kpi__label">Worker</span><span className="kpi__value">{h.worker.ok ? "Alive" : "Silent"}</span><span className="kpi__sub">{h.worker.last_heartbeat_at ? <>heartbeat <When iso={h.worker.last_heartbeat_at} relative /></> : "no heartbeat recorded"}</span></div>
        <div className={`kpi kpi--${failedJobs ? "risk" : queued ? "wait" : "ok"}`}><span className="kpi__label">Queue</span><span className="kpi__value">{queued}</span><span className="kpi__sub">{lag(h.jobs?.lag_seconds || 0)}{failedJobs ? ` · ${failedJobs} failed` : ""}{jobs.done ? ` · ${jobs.done} done` : ""}</span></div>
        <div className={`kpi kpi--${h.outbox.pending ? "wait" : "ok"}`}><span className="kpi__label">Outbox</span><span className="kpi__value">{h.outbox.pending}</span><span className="kpi__sub">{lag(h.outbox.lag_seconds)}</span></div>
        <div className={`kpi kpi--${h.external_actions.unknown ? "risk" : h.external_actions.failed ? "blocked" : "ok"}`}><span className="kpi__label">External actions</span><span className="kpi__value">{h.external_actions.unknown} unknown</span><span className="kpi__sub">{h.external_actions.failed} failed</span></div>
        <div className={`kpi kpi--${h.reminders.overdue_deliveries ? "risk" : "ok"}`}><span className="kpi__label">Reminders</span><span className="kpi__value">{h.reminders.overdue_deliveries}</span><span className="kpi__sub">overdue deliveries</span></div>
        <div className="kpi"><span className="kpi__label">Storage</span><span className="kpi__value" style={{ fontSize: 16 }}>{humanize(h.storage.backend)}</span><span className="kpi__sub">database {h.database.ok ? "ok" : "failing"}</span></div>
        {withModel && h.model ? (
          <div className={`kpi kpi--${h.model.over_cap ? "blocked" : h.model.api_key_present === false || h.model.available === false ? "risk" : "ok"}`}><span className="kpi__label">Model</span><span className="kpi__value" style={{ fontSize: 16 }}>{h.model.over_cap ? "Over budget" : h.model.api_key_present === false || h.model.available === false ? "No API key" : "Ready"}</span><span className="kpi__sub">{h.model.day_usd !== undefined ? `${h.model.day_usd} USD today` : "deterministic lists still work"}</span></div>
        ) : null}
      </div>
      <GlassPanel clip>
        {(h.connections || []).map((c) => (
          <div key={c.provider} className="set-row">
            <div className="set-row__main"><span className="set-row__title">{c.label}</span>{c.freshness.last_success_at ? <span className="set-row__meta">last sync <When iso={c.freshness.last_success_at} relative /></span> : null}</div>
            <div className="set-row__right"><HealthLabel health={freshnessHealth(c.freshness.state)} label={c.freshness.label} dot /></div>
          </div>
        ))}
      </GlassPanel>
      <div className="set-foot">A restart replays nothing: waiting cases, reminders, source cursors and unknown actions are persisted and picked up by the worker. The runbook in the handoff covers backups and restore.</div>
    </div>
  );
}

export function RecoverySection() {
  const { user } = useAuth();
  return (
    <div className="stack-lg">
      {can(user, "settings") ? <SystemHealth /> : null}
      <PasskeysBlock />
    </div>
  );
}
