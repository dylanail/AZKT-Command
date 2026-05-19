import { Link } from "react-router-dom";
import { api } from "../api";
import { PullToRefresh, StatusDot, useAsync } from "../ui";

export default function Home() {
  const w = useAsync<any>(() => api.get("/api/vehicles/widgets/home"));
  const h = useAsync<any>(() => api.get("/api/health/overview"));
  const ap = useAsync<any[]>(() => api.get("/api/approvals"));

  const reload = async () => {
    await Promise.all([w.reload(), h.reload(), ap.reload()]);
  };

  return (
    <>
      <div className="topbar">
        <span>Today</span>
        <Link to="/health"><StatusDot status={h.data?.postgres?.ok ? "ok" : "bad"} /></Link>
      </div>
      <PullToRefresh onRefresh={reload}>
        {ap.data && ap.data.length > 0 && (
          <Link to="/approvals">
            <div className="card" style={{ borderColor: "var(--amber)" }}>
              <div className="spread">
                <strong>{ap.data.length} awaiting your decision</strong>
                <span className="tag">Review →</span>
              </div>
            </div>
          </Link>
        )}

        <div className="card">
          <h3>Won — awaiting decision</h3>
          {(w.data?.won_awaiting_decision ?? []).length === 0 && (
            <div className="muted">Nothing waiting.</div>
          )}
          {(w.data?.won_awaiting_decision ?? []).map((v: any) => (
            <Link key={v.id} to="/pipeline">
              <div className="spread" style={{ padding: "8px 0" }}>
                <span>{v.title || "Untitled"}</span>
                <span className="tag">Won</span>
              </div>
            </Link>
          ))}
        </div>

        <div className="grid2">
          <div className="card">
            <h3>New IRQs · 24h</h3>
            <div style={{ fontSize: 28, fontWeight: 800 }}>
              {(w.data?.new_irqs_24h ?? []).length}
            </div>
          </div>
          <div className="card">
            <h3>Stale IRQs · 7d+</h3>
            <div style={{ fontSize: 28, fontWeight: 800, color: "var(--amber)" }}>
              {(w.data?.stale_irqs_7d ?? []).length}
            </div>
          </div>
        </div>

        <div className="card">
          <h3>Listings · days on market</h3>
          {(w.data?.listings_days_on_market ?? []).length === 0 && (
            <div className="muted">No active listings.</div>
          )}
          {(w.data?.listings_days_on_market ?? []).map((v: any) => (
            <div key={v.id} className="spread" style={{ padding: "6px 0" }}>
              <span>{v.title}</span>
              <span className="tag">{v.days_on_market}d</span>
            </div>
          ))}
        </div>

        {h.data?.notion_schema_gaps?.length > 0 && (
          <Link to="/settings">
            <div className="pill-warn">
              {h.data.notion_schema_gaps.length} Notion mappings unset — sync
              partially paused. Tap to configure.
            </div>
          </Link>
        )}
      </PullToRefresh>
    </>
  );
}
