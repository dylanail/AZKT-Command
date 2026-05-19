import { Link } from "react-router-dom";
import { api } from "../api";
import { PullToRefresh, useAsync } from "../ui";

export default function Customers() {
  const c = useAsync<any[]>(() => api.get("/api/customers"));
  return (
    <>
      <div className="topbar"><span>Customers</span></div>
      <PullToRefresh onRefresh={c.reload}>
        {(c.data ?? []).map((cu) => (
          <Link key={cu.id} to={`/customers/${cu.id}`}>
            <div className="card">
              <strong>{cu.name || "Unnamed"}</strong>
              <div className="muted" style={{ fontSize: 13 }}>
                {cu.email || ""} {cu.phone ? `· ${cu.phone}` : ""}
              </div>
            </div>
          </Link>
        ))}
        {(c.data ?? []).length === 0 && <div className="card muted">No customers yet.</div>}
      </PullToRefresh>
    </>
  );
}
