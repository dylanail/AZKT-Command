import { api } from "../api";
import { PullToRefresh, useAsync } from "../ui";

export default function Approvals() {
  const a = useAsync<any[]>(() => api.get("/api/approvals"));
  const decide = async (id: string, d: "approve" | "reject") => {
    await api.post(`/api/approvals/${id}/${d}`);
    a.reload();
  };
  return (
    <>
      <div className="topbar"><span>Approval queue</span></div>
      <PullToRefresh onRefresh={a.reload}>
        {(a.data ?? []).length === 0 && (
          <div className="card muted">Nothing needs your decision. 🎉</div>
        )}
        {(a.data ?? []).map((c) => (
          <div key={c.id} className="card">
            <div className="spread">
              <strong>{c.title}</strong>
              <span className="tag" style={{ textTransform: "capitalize" }}>{c.agent_key}</span>
            </div>
            {c.context?.why && (
              <p style={{ margin: "8px 0", fontSize: 14 }}>{c.context.why}</p>
            )}
            <div style={{ fontSize: 13 }} className="muted">
              {c.context?.if_approve && <div>✓ {c.context.if_approve}</div>}
              {c.context?.if_reject && <div>✗ {c.context.if_reject}</div>}
            </div>
            <div className="btn-row" style={{ marginTop: 12 }}>
              <button className="btn-red" onClick={() => decide(c.id, "reject")}>Reject</button>
              <button className="btn-green" onClick={() => decide(c.id, "approve")}>Approve</button>
            </div>
          </div>
        ))}
      </PullToRefresh>
    </>
  );
}
