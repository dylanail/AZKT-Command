import { Link } from "react-router-dom";
import { api } from "../api";
import { PullToRefresh, StatusDot, useAsync } from "../ui";

export default function Agents() {
  const a = useAsync<any[]>(() => api.get("/api/agents"));
  return (
    <>
      <div className="topbar"><span>Agents</span></div>
      <PullToRefresh onRefresh={a.reload}>
        {a.loading && <div className="muted">Loading…</div>}
        {(a.data ?? []).map((ag) => {
          const h = ag.health || {};
          return (
            <Link key={ag.key} to={`/agents/${ag.key}`}>
              <div className="card">
                <div className="spread">
                  <div className="row">
                    <StatusDot status={h.status} />
                    <strong style={{ textTransform: "capitalize" }}>{ag.key}</strong>
                  </div>
                  <span className="tag">{ag.configured ? `:${ag.port}` : "unconfigured"}</span>
                </div>
                <div className="muted" style={{ marginTop: 6, fontSize: 13 }}>
                  {h.timer?.managed === false
                    ? "DM orchestrator (no timer)"
                    : h.timer?.active
                    ? "Timer active"
                    : "Timer stopped / degraded"}
                  {h.model ? ` · ${h.model}` : ""}
                </div>
              </div>
            </Link>
          );
        })}
      </PullToRefresh>
    </>
  );
}
