import { useState } from "react";
import { api } from "../api";
import { PullToRefresh, money, useAsync } from "../ui";

type Row = { agent_key: string; model: string; input_tokens: number; output_tokens: number; cost_usd: number };

function Period({ title, rows }: { title: string; rows: Row[] }) {
  const total = rows.reduce((s, r) => s + r.cost_usd, 0);
  const max = Math.max(1, ...rows.map((r) => r.cost_usd));
  const byAgent: Record<string, Row[]> = {};
  rows.forEach((r) => (byAgent[r.agent_key] ||= []).push(r));
  return (
    <div className="card">
      <div className="spread">
        <h3 style={{ margin: 0 }}>{title}</h3>
        <strong>{money(total)}</strong>
      </div>
      {Object.entries(byAgent).map(([ag, rs]) => (
        <div key={ag} style={{ marginTop: 10 }}>
          <div className="spread" style={{ fontSize: 13 }}>
            <span style={{ textTransform: "capitalize" }}>{ag}</span>
            <span className="muted">{money(rs.reduce((s, r) => s + r.cost_usd, 0))}</span>
          </div>
          {rs.map((r) => (
            <div key={r.model} style={{ marginTop: 6 }}>
              <div className="spread muted" style={{ fontSize: 12 }}>
                <span>{r.model}</span>
                <span>{money(r.cost_usd)}</span>
              </div>
              <div className="bar"><span style={{ width: `${(r.cost_usd / max) * 100}%` }} /></div>
            </div>
          ))}
        </div>
      ))}
      {rows.length === 0 && <div className="muted" style={{ marginTop: 8 }}>No usage logged yet.</div>}
    </div>
  );
}

export default function Costs() {
  const c = useAsync<any>(() => api.get("/api/costs/summary"));
  const [busy, setBusy] = useState(false);
  const refresh = async () => {
    setBusy(true);
    try {
      await api.post("/api/costs/refresh");
      await c.reload();
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <div className="topbar">
        <span>Costs</span>
        <button style={{ padding: "6px 12px", minHeight: 0 }} disabled={busy} onClick={refresh}>
          {busy ? "…" : "Ingest"}
        </button>
      </div>
      <PullToRefresh onRefresh={c.reload}>
        {c.data && (
          <>
            <Period title="Today" rows={c.data.today} />
            <Period title="This week" rows={c.data.week} />
            <Period title="This month" rows={c.data.month} />
            <Period title="All time" rows={c.data.all_time} />
          </>
        )}
      </PullToRefresh>
    </>
  );
}
