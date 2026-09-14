/* Sales: two pipelines (Import requests, Vehicle sales), stages New lead → In conversation → Awaiting deposit →
   Deposit paid; Lost parked separately. Board/Tasks views. Lead opens in the inspector (mode "lead").
   TODO(screen builder): GET /api/leads?pipe=&stage= ; POST /api/leads ; POST /api/leads/{id}/stage ;
   GET/POST /api/sales-tasks (call/meeting/follow-up with reminder offsets); registerLeadPane() for the inspector. */
import { useState } from "react";
import { SegmentedControl, Tabs } from "../../ui";
import { Scaffold } from "../scaffold";

type Pipe = "irq" | "veh";
type View = "board" | "tasks";

export default function Sales() {
  const [pipe, setPipe] = useState<Pipe>("irq");
  const [view, setView] = useState<View>("board");
  return (
    <Scaffold
      title="Sales"
      subtitle="Calls, meetings and follow-ups are timed tasks, not stages."
      probe={`/api/leads?pipe=${pipe}`}
      emptyTitle="No leads yet"
      emptyBody="Add one from New › Lead. Deposit paid hands off to the vehicle's Sale tab."
      wide
      toolbar={
        <div className="row-wrap" style={{ justifyContent: "space-between" }}>
          <SegmentedControl<Pipe> label="Pipeline" value={pipe} onChange={setPipe} options={[{ value: "irq", label: "Import requests" }, { value: "veh", label: "Vehicle sales" }]} />
          <Tabs<View> label="Sales view" value={view} onChange={setView} tabs={[{ id: "board", label: "Board" }, { id: "tasks", label: "Tasks" }]} />
        </div>
      }
    />
  );
}
