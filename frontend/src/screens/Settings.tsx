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

function Passkeys() {
  const { addPasskey } = useAuth();
  const keys = useAsync<any[]>(() => api.get("/auth/credentials"));
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  const add = async () => {
    setBusy(true);
    setMsg("");
    try {
      await addPasskey(name.trim() || "passkey");
      setName("");
      setMsg("New passkey added — follow the on-device prompt to finish (Face ID etc.).");
      await keys.reload();
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const rename = async (id: string, current: string) => {
    const label = window.prompt("Rename passkey", current);
    if (!label || !label.trim()) return;
    try {
      await api.post(`/auth/credentials/${id}/rename`, { label: label.trim() });
      await keys.reload();
    } catch (e) {
      setMsg((e as Error).message);
    }
  };

  const revoke = async (id: string, label: string) => {
    if (!window.confirm(`Revoke passkey "${label}"? That device can no longer sign in.`)) return;
    try {
      await api.del(`/auth/credentials/${id}`);
      await keys.reload();
    } catch (e) {
      setMsg((e as Error).message);
    }
  };

  const list = keys.data ?? [];
  return (
    <div className="card">
      <h3>Passkeys</h3>
      <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>
        Each device (phone Face ID, laptop, security key) is its own passkey.
        Name new ones so you can revoke a lost device later.
      </p>

      {list.map((k) => (
        <div key={k.id} className="spread" style={{ padding: "8px 0", borderTop: "1px solid var(--line, #1e2630)" }}>
          <div>
            <div style={{ fontSize: 14 }}>{k.label || "passkey"}</div>
            <div className="muted" style={{ fontSize: 11 }}>
              {k.created_at ? new Date(k.created_at).toLocaleDateString() : ""}
              {k.transports ? ` · ${k.transports}` : ""}
            </div>
          </div>
          <div className="btn-row">
            <button onClick={() => rename(k.id, k.label || "passkey")}>Rename</button>
            <button
              className="btn-red"
              disabled={list.length <= 1}
              title={list.length <= 1 ? "Add another passkey before revoking your only one" : ""}
              onClick={() => revoke(k.id, k.label || "passkey")}
            >
              Revoke
            </button>
          </div>
        </div>
      ))}

      <input
        placeholder="New passkey name (e.g. iPhone 16, MacBook)"
        value={name}
        onChange={(e) => setName(e.target.value)}
        style={{ marginTop: 12 }}
      />
      <button
        className="btn-green"
        style={{ width: "100%", marginTop: 8 }}
        disabled={busy}
        onClick={add}
      >
        {busy ? "Follow your browser prompt…" : "Add another passkey"}
      </button>
      {msg && <div className="muted" style={{ marginTop: 8, fontSize: 13 }}>{msg}</div>}
    </div>
  );
}

function NotionCard() {
  const status = useAsync<any>(() => api.get("/api/notion/status"));
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState<string>("");

  const introspect = async () => {
    setBusy(true);
    setOut("");
    try {
      const r = await api.post("/api/notion/introspect");
      const lines: string[] = [];
      for (const [db, info] of Object.entries<any>(r.databases || {})) {
        lines.push(`=== ${db} ===`);
        if (info.error) {
          lines.push(`  ${info.error} ${info.detail || ""}`);
        } else {
          for (const p of info.properties || [])
            lines.push(`  ${p.type}\t${p.name}`);
        }
      }
      setOut(lines.join("\n") + `\n\nDraft written: ${r.draft_written}`);
    } catch (e) {
      setOut((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const gaps: string[] = status.data?.schema_gaps ?? [];
  return (
    <div className="card">
      <h3>Notion sync</h3>
      {status.data && (
        <div className="muted" style={{ fontSize: 13 }}>
          Last sync: {status.data.last_sync_at
            ? new Date(status.data.last_sync_at).toLocaleString()
            : "never"} · polling every {status.data.env?.poll_seconds}s
        </div>
      )}
      {gaps.length ? (
        <div className="pill-warn" style={{ marginTop: 8 }}>
          {gaps.length} unmapped: {gaps.slice(0, 6).join(", ")}
          {gaps.length > 6 ? "…" : ""}
        </div>
      ) : (
        <div className="muted" style={{ marginTop: 8 }}>All Notion mappings set.</div>
      )}
      <p className="muted" style={{ fontSize: 13, marginTop: 10 }}>
        To wire it: set <code>NOTION_TOKEN</code> + the three DB ids in
        <code> .env</code>, run introspection to get exact property names,
        then transcribe them into <code>config/notion_schema.json</code>. The
        <strong> main</strong> agent can do all of this via the
        <code> /api/notion/*</code> endpoints.
      </p>
      <button
        style={{ width: "100%", marginTop: 4 }}
        disabled={busy}
        onClick={introspect}
      >
        {busy ? "Asking Notion…" : "Run introspection"}
      </button>
      {out && (
        <pre className="muted" style={{
          marginTop: 8, fontSize: 12, whiteSpace: "pre-wrap",
          maxHeight: 240, overflow: "auto",
        }}>{out}</pre>
      )}
    </div>
  );
}

export default function Settings() {
  const { logout } = useAuth();
  const integ = useAsync<any>(() => api.get("/api/integrations"));
  const costs = useAsync<any>(() => api.get("/api/costs/summary"));

  return (
    <>
      <div className="topbar"><span>Settings</span></div>
      <div className="scroll">
        <div className="card">
          <h3>Quick links</h3>
          <Link to="/chat"><div style={{ padding: "8px 0" }}>All chat history →</div></Link>
          <Link to="/customers"><div style={{ padding: "8px 0" }}>Customers →</div></Link>
          <Link to="/health"><div style={{ padding: "8px 0" }}>System health →</div></Link>
        </div>

        <NotionCard />

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

        <Passkeys />

        <button className="btn-red" style={{ width: "100%" }} onClick={logout}>
          Sign out
        </button>
      </div>
    </>
  );
}
