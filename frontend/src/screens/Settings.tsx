import { useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../auth";
import { api } from "../api";
import { useAsync } from "../ui";

function IntegrationForm({ name, connected }: { name: string; connected: boolean }) {
  const [open, setOpen] = useState(false);
  const [fields, setFields] = useState("");
  const save = async () => {
    let parsed: unknown = {};
    try { parsed = JSON.parse(fields || "{}"); } catch { alert("Invalid JSON"); return; }
    await api.put(`/api/integrations/${name}`, parsed);
    setOpen(false);
  };
  return (
    <div className="card">
      <div className="spread">
        <strong style={{ textTransform: "capitalize" }}>{name}</strong>
        <span className="tag">{connected ? "Connected" : "Not connected"}</span>
      </div>
      {!connected && (
        <div className="muted" style={{ fontSize: 13, marginTop: 6 }}>
          Not connected — add credentials to enable later.
        </div>
      )}
      {open ? (
        <>
          <textarea rows={4} placeholder='{"api_key":"…"}' value={fields}
            onChange={(e) => setFields(e.target.value)} style={{ marginTop: 10 }} />
          <div className="btn-row" style={{ marginTop: 8 }}>
            <button onClick={() => setOpen(false)}>Cancel</button>
            <button className="btn-green" onClick={save}>Save</button>
          </div>
        </>
      ) : (
        <button style={{ marginTop: 10, width: "100%" }} onClick={() => setOpen(true)}>
          Add credentials
        </button>
      )}
    </div>
  );
}

export default function Settings() {
  const { logout } = useAuth();
  const integ = useAsync<any>(() => api.get("/api/integrations"));
  const costs = useAsync<any>(() => api.get("/api/costs/summary"));
  const health = useAsync<any>(() => api.get("/api/health/overview"));

  return (
    <>
      <div className="topbar"><span>Settings</span></div>
      <div className="scroll">
        <div className="card">
          <h3>Quick links</h3>
          <Link to="/customers"><div style={{ padding: "8px 0" }}>Customers →</div></Link>
          <Link to="/health"><div style={{ padding: "8px 0" }}>System health →</div></Link>
        </div>

        <div className="card">
          <h3>Notion mapping</h3>
          {health.data?.notion_schema_gaps?.length ? (
            <div className="pill-warn">
              {health.data.notion_schema_gaps.length} unset. Edit
              <code> config/notion_schema.json </code> on the droplet — exact
              property names, no guessing.
            </div>
          ) : (
            <div className="muted">All Notion mappings set.</div>
          )}
        </div>

        <h3 style={{ margin: "8px 4px", color: "var(--muted)" }}>Integrations</h3>
        {["gohighlevel", "twilio", "square"].map((n) => (
          <IntegrationForm key={n} name={n} connected={!!integ.data?.[n]?.connected} />
        ))}

        <div className="card">
          <h3>Model pricing (read-only)</h3>
          {costs.data?.pricing &&
            Object.entries(costs.data.pricing.per_mtok).map(([m, r]: any) => (
              <div key={m} className="spread muted" style={{ fontSize: 13, padding: "4px 0" }}>
                <span>{m}</span>
                <span>${r.input}/${r.output} per Mtok</span>
              </div>
            ))}
          <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
            Edit <code>config/pricing.json</code> on the droplet.
          </div>
        </div>

        <button className="btn-red" style={{ width: "100%" }} onClick={logout}>
          Sign out
        </button>
      </div>
    </>
  );
}
