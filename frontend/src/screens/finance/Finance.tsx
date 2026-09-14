/* Finance (owner writes; manager read-only): bank/Stripe feeds as read-only lines with proposed matches;
   owner confirms matches; vehicle cost = landed + recon lines.
   TODO(screen builder): GET /api/finance/lines?tab=recon|recv|pay|cost ; POST /api/finance/lines/{id}/match {vehicle_id} ;
   GET /api/finance/summary. Hide amounts when !can("costs.read") (Money hidden). */
import { useState } from "react";
import { useCan } from "../../lib/auth";
import { Tabs } from "../../ui";
import { Scaffold } from "../scaffold";

type Tab = "recon" | "recv" | "pay" | "cost";

export default function Finance() {
  const can = useCan();
  const [tab, setTab] = useState<Tab>("recon");
  return (
    <Scaffold
      title="Finance"
      subtitle={can("finance.write") ? "Confirm matches; feeds are read-only." : "Read-only for your role."}
      probe={`/api/finance/lines?tab=${tab}`}
      emptyTitle="No lines yet"
      emptyBody="Bank and Stripe feeds appear here once connected."
      toolbar={<Tabs<Tab> label="Finance tabs" value={tab} onChange={setTab} tabs={[{ id: "recon", label: "To reconcile" }, { id: "recv", label: "Received" }, { id: "pay", label: "To pay" }, { id: "cost", label: "Vehicle costs" }]} />}
      wide
    />
  );
}
