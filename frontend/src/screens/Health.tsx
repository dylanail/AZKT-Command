import { api } from "../api";
import { PullToRefresh, StatusDot, useAsync } from "../ui";

export default function Health() {
  const h = useAsync<any>(() => api.get("/api/health/overview"));
  const a = useAsync<any[]>(() => api.get("/api/agents"));
  const reload = async () => { await Promise.all([h.reload(), a.reload()]); };
  const d = h.data;
  return (
    <>
      <div className="topbar"><span>System health</span></div>
      <PullToRefresh onRefresh={reload}>
        <div className="card">
          <h3>Infrastructure</h3>
          <div className="spread" style={{ padding: "4px 0" }}>
            <span>Postgres</span><StatusDot status={d?.postgres?.ok ? "ok" : "bad"} />
          </div>
          {d?.droplet && (
            <>
              <div className="spread" style={{ padding: "4px 0" }}>
                <span>CPU</span><span className="muted">{d.droplet.cpu_percent}%</span>
              </div>
              <div className="spread" style={{ padding: "4px 0" }}>
                <span>Memory</span><span className="muted">{d.droplet.mem_percent}%</span>
              </div>
              <div className="spread" style={{ padding: "4px 0" }}>
                <span>Disk</span>
                <span className="muted">
                  {d.droplet.disk_used_percent}% · {d.droplet.disk_free_gb}GB free
                </span>
              </div>
            </>
          )}
        </div>

        <div className="card">
          <h3>Notion sync</h3>
          <div className="spread">
            <span>Last sync</span>
            <span className="muted">
              {d?.notion_last_sync ? new Date(d.notion_last_sync).toLocaleString() : "never"}
            </span>
          </div>
          {d?.notion_schema_gaps?.length > 0 && (
            <div className="pill-warn" style={{ marginTop: 10 }}>
              Unmapped: {d.notion_schema_gaps.join(", ")}
            </div>
          )}
        </div>

        <div className="card">
          <h3>Agents — last run</h3>
          {(a.data ?? []).map((ag) => (
            <div key={ag.key} className="spread" style={{ padding: "6px 0" }}>
              <span className="row">
                <StatusDot status={ag.health?.status} />
                <span style={{ textTransform: "capitalize" }}>{ag.key}</span>
              </span>
              <span className="muted" style={{ fontSize: 12 }}>
                {ag.health?.timer?.managed === false
                  ? "no timer"
                  : ag.health?.timer?.active
                  ? "active"
                  : "STOPPED"}
              </span>
            </div>
          ))}
        </div>
      </PullToRefresh>
    </>
  );
}
