import { useState } from "react";
import { NavLink, Route, Routes, useNavigate } from "react-router-dom";
import { useAuth } from "./auth";
import { api } from "./api";
import Login from "./screens/Login";
import Home from "./screens/Home";
import Agents from "./screens/Agents";
import AgentDetail from "./screens/AgentDetail";
import Approvals from "./screens/Approvals";
import Pipeline from "./screens/Pipeline";
import Costs from "./screens/Costs";
import Customers from "./screens/Customers";
import CustomerCard from "./screens/CustomerCard";
import Health from "./screens/Health";
import Settings from "./screens/Settings";

function QuickAdd({ close }: { close: () => void }) {
  const [tab, setTab] = useState<"vehicle" | "irq">("vehicle");
  const [val, setVal] = useState("");
  const nav = useNavigate();
  const submit = async () => {
    if (!val.trim()) return;
    if (tab === "vehicle")
      await api.post("/api/vehicles", { title: val, auction_url: val });
    else await api.post("/api/irqs/quick-add", { title: val });
    close();
    nav(tab === "vehicle" ? "/pipeline" : "/");
  };
  return (
    <div className="center" style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.6)", zIndex: 50 }}>
      <div className="card" style={{ width: "92%", maxWidth: 440 }}>
        <div className="btn-row" style={{ marginBottom: 12 }}>
          <button className={tab === "vehicle" ? "btn-green" : ""} onClick={() => setTab("vehicle")}>
            Auction URL
          </button>
          <button className={tab === "irq" ? "btn-green" : ""} onClick={() => setTab("irq")}>
            Quick IRQ
          </button>
        </div>
        <textarea
          rows={3}
          placeholder={tab === "vehicle" ? "Paste auction URL…" : "Jot the IRQ…"}
          value={val}
          onChange={(e) => setVal(e.target.value)}
        />
        <div className="btn-row" style={{ marginTop: 12 }}>
          <button onClick={close}>Cancel</button>
          <button className="btn-green" onClick={submit}>Add</button>
        </div>
      </div>
    </div>
  );
}

const NAV = [
  { to: "/", ic: "▦", label: "Home" },
  { to: "/agents", ic: "◉", label: "Agents" },
  { to: "/approvals", ic: "✓", label: "Approve" },
  { to: "/pipeline", ic: "▤", label: "Pipeline" },
  { to: "/costs", ic: "$", label: "Costs" },
];

export default function App() {
  const { ready, authed } = useAuth();
  const [qa, setQa] = useState(false);

  if (!ready) return <div className="center">Loading…</div>;
  if (!authed) return <Login />;

  return (
    <div className="app">
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/agents" element={<Agents />} />
        <Route path="/agents/:key" element={<AgentDetail />} />
        <Route path="/approvals" element={<Approvals />} />
        <Route path="/pipeline" element={<Pipeline />} />
        <Route path="/costs" element={<Costs />} />
        <Route path="/customers" element={<Customers />} />
        <Route path="/customers/:id" element={<CustomerCard />} />
        <Route path="/health" element={<Health />} />
        <Route path="/settings" element={<Settings />} />
      </Routes>

      <button className="fab" onClick={() => setQa(true)} aria-label="Quick add">＋</button>
      {qa && <QuickAdd close={() => setQa(false)} />}

      <nav className="bottomnav">
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} end={n.to === "/"}
            className={({ isActive }) => (isActive ? "active" : "")}>
            <span className="ic">{n.ic}</span>
            {n.label}
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
