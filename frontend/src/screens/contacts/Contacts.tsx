/* Contacts: buyers (= leads), vendors, exporters, carriers with relationship state, promises and consent.
   TODO(screen builder): GET /api/contacts?tab=&q= ; POST /api/contacts. */
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Tabs } from "../../ui";
import { Scaffold } from "../scaffold";

type Tab = "all" | "buyers" | "vendors" | "exporters" | "carriers";

export default function Contacts() {
  const [params] = useSearchParams();
  const q = params.get("q") || "";
  const [tab, setTab] = useState<Tab>("all");
  return (
    <Scaffold
      title="Contacts"
      subtitle={q ? `Search: "${q}"` : "Buyers, vendors, exporters and carriers, with what each was promised."}
      probe={`/api/contacts?tab=${tab}${q ? `&q=${encodeURIComponent(q)}` : ""}`}
      emptyTitle={q ? "No contacts match" : "No contacts yet"}
      toolbar={<Tabs<Tab> label="Contact type" value={tab} onChange={setTab} tabs={[{ id: "all", label: "All" }, { id: "buyers", label: "Buyers" }, { id: "vendors", label: "Vendors" }, { id: "exporters", label: "Exporters" }, { id: "carriers", label: "Carriers" }]} />}
    />
  );
}
