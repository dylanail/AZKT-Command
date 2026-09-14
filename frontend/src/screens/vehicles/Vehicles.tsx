/* Vehicles: list by default; named views All / Sourcing / Shipping / Shop / Sales; Shop has a Board toggle
   with 4 fixed columns (Needs inspection → In recon → Finalization → Ready for sale) and a Move stage button.
   TODO(screen builder): GET /api/vehicles?view=&q=&attention=&docs= ; POST /api/vehicles (New: auction URL/stock);
   POST /api/vehicles/{id}/move-stage (gate check → tasks). Mechanic sees only vehicles with their tasks. */
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useAuth } from "../../lib/auth";
import { isEmployeeRole } from "../../lib/perms";
import { Chip, SegmentedControl, Tabs } from "../../ui";
import { Scaffold } from "../scaffold";

type View = "all" | "sourcing" | "shipping" | "shop" | "sales";

export default function Vehicles() {
  const { user } = useAuth();
  const [params] = useSearchParams();
  const q = params.get("q") || "";
  const [view, setView] = useState<View>("all");
  const [layout, setLayout] = useState<"list" | "board">("list");
  const employee = isEmployeeRole(user?.role);
  const probe = employee ? "/api/vehicles?mine=1" : `/api/vehicles?view=${view}${q ? `&q=${encodeURIComponent(q)}` : ""}`;

  return (
    <Scaffold
      title={employee ? "Vehicles you work on" : "Vehicles"}
      subtitle={q ? `Search: "${q}"` : employee ? "Only vehicles with your tasks. No prices or customer details." : "List by default. Boards are a per-view option."}
      probe={probe}
      emptyTitle={q ? "No vehicles match" : "No vehicles yet"}
      emptyBody={q ? "Try a stock number, model or year." : "Add one from New › Vehicle with an auction URL or stock number."}
      toolbar={employee ? undefined : (
        <div className="row-wrap" style={{ justifyContent: "space-between" }}>
          <Tabs<View>
            label="Vehicle views"
            value={view}
            onChange={setView}
            tabs={[{ id: "all", label: "All" }, { id: "sourcing", label: "Sourcing" }, { id: "shipping", label: "Shipping" }, { id: "shop", label: "Shop" }, { id: "sales", label: "Sales" }]}
          />
          <div className="row-wrap">
            <Chip size="sm" onClick={() => undefined} disabled disabledReason="Filters connect with the vehicle list.">Needs attention</Chip>
            <Chip size="sm" onClick={() => undefined} disabled disabledReason="Filters connect with the vehicle list.">Document issues</Chip>
            {view === "shop" ? (
              <SegmentedControl label="Layout" size="sm" value={layout} onChange={setLayout} options={[{ value: "list", label: "List" }, { value: "board", label: "Board" }]} />
            ) : null}
          </div>
        </div>
      )}
    />
  );
}
