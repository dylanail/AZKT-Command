import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { StatusDot, useAsync } from "../ui";

export default function AgentDetail() {
  const { key = "" } = useParams();
  const detail = useAsync<any>(() => api.get(`/api/agents/${key}`), [key]);
  const msgs = useAsync<any[]>(() => api.get(`/api/agents/${key}/messages`), [key]);
  const [tab, setTab] = useState<"chat" | "prompt">("chat");
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [prompt, setPrompt] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (detail.data?.prompt?.content != null) setPrompt(detail.data.prompt.content);
  }, [detail.data]);
  useEffect(() => {
    endRef.current?.scrollIntoView();
  }, [msgs.data]);

  const h = detail.data?.health || {};
  const send = async () => {
    if (!input.trim()) return;
    setSending(true);
    try {
      await api.post(`/api/agents/${key}/chat`, { message: input });
      setInput("");
      await msgs.reload();
    } finally {
      setSending(false);
    }
  };
  const ctl = async (path: string) => {
    await api.post(`/api/agents/${key}${path}`);
    detail.reload();
  };
  const savePrompt = async () => {
    await api.put(`/api/agents/${key}/prompt`, { content: prompt });
    detail.reload();
  };
  const rollback = async (sha: string) => {
    await api.post(`/api/agents/${key}/prompt/rollback`, { sha });
    detail.reload();
  };

  return (
    <>
      <div className="topbar">
        <Link to="/agents">‹ Agents</Link>
        <span style={{ textTransform: "capitalize" }}>{key}</span>
        <StatusDot status={h.status} />
      </div>
      <div className="scroll">
        <div className="card">
          <div className="spread">
            <span className="muted">
              {h.timer?.managed === false
                ? "DM orchestrator"
                : h.timer?.active
                ? `Timer active${h.timer?.last_trigger_usec ? "" : ""}`
                : "Timer stopped"}
            </span>
            <span className="tag">{h.model}</span>
          </div>
          <div className="btn-row" style={{ marginTop: 12 }}>
            <button onClick={() => ctl("/run")}>Run once</button>
            {h.timer?.managed !== false &&
              (h.timer?.active ? (
                <button onClick={() => ctl("/schedule/pause")}>Pause</button>
              ) : (
                <button className="btn-green" onClick={() => ctl("/schedule/resume")}>
                  Resume
                </button>
              ))}
          </div>
          {h.timer && h.timer.degraded && (
            <div className="pill-warn" style={{ marginTop: 10 }}>
              Degraded — timer not running. (This is the silent-failure guard.)
            </div>
          )}
        </div>

        <div className="btn-row" style={{ marginBottom: 12 }}>
          <button className={tab === "chat" ? "btn-green" : ""} onClick={() => setTab("chat")}>
            Chat
          </button>
          <button className={tab === "prompt" ? "btn-green" : ""} onClick={() => setTab("prompt")}>
            System prompt
          </button>
        </div>

        {tab === "chat" ? (
          <>
            {(msgs.data ?? []).map((m, i) => (
              <div key={i} className={`msg ${m.role}`}>{m.content || "…"}</div>
            ))}
            <div ref={endRef} />
            <div className="composer">
              <textarea
                rows={2}
                placeholder={`Message ${key}…`}
                value={input}
                onChange={(e) => setInput(e.target.value)}
              />
              <button className="btn-green" disabled={sending} onClick={send}>
                {sending ? "…" : "Send"}
              </button>
            </div>
          </>
        ) : (
          <>
            <div className="card">
              <textarea
                rows={14}
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                style={{ fontFamily: "ui-monospace, monospace", fontSize: 13 }}
              />
              <button className="btn-green" style={{ width: "100%", marginTop: 10 }} onClick={savePrompt}>
                Save new version
              </button>
            </div>
            <div className="card">
              <h3>Version history</h3>
              {(detail.data?.prompt?.history ?? []).map((v: any) => (
                <div key={v.sha} className="spread" style={{ padding: "8px 0" }}>
                  <div>
                    <div style={{ fontSize: 13 }}>{v.message}</div>
                    <div className="muted" style={{ fontSize: 11 }}>
                      {new Date(v.ts * 1000).toLocaleString()} · {v.sha.slice(0, 7)}
                    </div>
                  </div>
                  <button onClick={() => rollback(v.sha)}>Roll back</button>
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </>
  );
}
