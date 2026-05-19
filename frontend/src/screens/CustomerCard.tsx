import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { useAsync } from "../ui";

export default function CustomerCard() {
  const { id = "" } = useParams();
  const d = useAsync<any>(() => api.get(`/api/customers/${id}`), [id]);
  const c = d.data?.customer;
  return (
    <>
      <div className="topbar">
        <Link to="/customers">‹ Customers</Link>
        <span>Customer</span>
        <span />
      </div>
      <div className="scroll">
        {c && (
          <div className="card">
            <strong style={{ fontSize: 18 }}>{c.name}</strong>
            <div className="muted">{c.email} {c.phone ? `· ${c.phone}` : ""}</div>
          </div>
        )}
        <div className="card">
          <h3>IRQ history</h3>
          {(d.data?.irqs ?? []).map((i: any) => (
            <div key={i.id} className="spread" style={{ padding: "6px 0" }}>
              <span>{i.title}</span>
              <span className="tag">{i.status}</span>
            </div>
          ))}
          {(d.data?.irqs ?? []).length === 0 && <div className="muted">No IRQs.</div>}
        </div>
        <div className="card">
          <h3>Vehicles bought</h3>
          {(d.data?.vehicles ?? []).map((v: any) => (
            <div key={v.id} className="spread" style={{ padding: "6px 0" }}>
              <span>{v.title}</span>
              <span className="tag">{v.stage}</span>
            </div>
          ))}
          {(d.data?.vehicles ?? []).length === 0 && <div className="muted">None.</div>}
        </div>
      </div>
    </>
  );
}
