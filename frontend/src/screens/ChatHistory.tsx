import { useMemo, useState } from "react";
import { api } from "../api";
import { PullToRefresh, useAsync } from "../ui";

type Msg = {
  agent: string;
  role: string;
  content: string;
  at: string | null;
  source: "dashboard" | "transcript";
  session: string | null;
};

export default function ChatHistory() {
  const agents = useAsync<any[]>(() => api.get("/api/agents"));
  const [agent, setAgent] = useState("all");
  const [source, setSource] = useState("all");
  const msgs = useAsync<Msg[]>(
    () => api.get(`/api/chat/history?agent=${agent}&source=${source}&limit=300`),
    [agent, source]
  );

  const agentKeys = useMemo(
    () => ["all", ...((agents.data ?? []).map((a) => a.key))],
    [agents.data]
  );

  return (
    <>
      <div className="topbar"><span>All chat history</span></div>
      <div style={{ padding: "10px 12px 0", display: "flex", gap: 8, flexWrap: "wrap" }}>
        <select value={agent} onChange={(e) => setAgent(e.target.value)}>
          {agentKeys.map((k) => (
            <option key={k} value={k}>{k === "all" ? "All agents" : k}</option>
          ))}
        </select>
        <select value={source} onChange={(e) => setSource(e.target.value)}>
          <option value="all">All sources</option>
          <option value="dashboard">Dashboard</option>
          <option value="transcript">Agent / Telegram</option>
        </select>
      </div>
      <PullToRefresh onRefresh={msgs.reload}>
        {msgs.loading && <div className="muted">Loading…</div>}
        {!msgs.loading && (msgs.data ?? []).length === 0 && (
          <div className="muted">No messages yet.</div>
        )}
        {(msgs.data ?? []).map((m, i) => (
          <div key={i} className="card" style={{ marginBottom: 8 }}>
            <div className="spread" style={{ fontSize: 12 }}>
              <span className="tag" style={{ textTransform: "capitalize" }}>
                {m.agent} · {m.role}
              </span>
              <span className="muted">
                {m.source === "transcript" ? "agent/telegram" : "dashboard"}
                {m.at ? ` · ${new Date(m.at).toLocaleString()}` : ""}
              </span>
            </div>
            <div className={`msg ${m.role}`} style={{ marginTop: 6, whiteSpace: "pre-wrap" }}>
              {m.content || "…"}
            </div>
          </div>
        ))}
      </PullToRefresh>
    </>
  );
}
