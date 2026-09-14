/* Tasks: all tasks across pipelines (owner/manager) or "My tasks" with Open / Blocked / Done filters (mechanic).
   TODO(screen builder): GET /api/tasks?mine=1&state= ; GET /api/tasks?q= ; POST /api/tasks (assign); reminders banner. */
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useAuth } from "../../lib/auth";
import { isEmployeeRole } from "../../lib/perms";
import { Chip } from "../../ui";
import { Scaffold } from "../scaffold";

type Filter = "open" | "blocked" | "done";

export default function Tasks() {
  const { user } = useAuth();
  const [params] = useSearchParams();
  const q = params.get("q") || "";
  const [filter, setFilter] = useState<Filter>("open");
  const employee = isEmployeeRole(user?.role);
  const emptyByFilter: Record<Filter, string> = { open: "No open tasks.", blocked: "Nothing is blocked.", done: "Nothing finished yet today." };
  return (
    <Scaffold
      title={employee ? "My tasks" : "Tasks"}
      subtitle={q ? `Search: "${q}"` : employee ? "Complete with evidence; Dylan verifies." : "Shop tasks and sales tasks across every pipeline."}
      probe={`/api/tasks?${employee ? "mine=1&" : ""}state=${filter}${q ? `&q=${encodeURIComponent(q)}` : ""}`}
      emptyTitle={q ? "No tasks match" : emptyByFilter[filter]}
      toolbar={
        <div className="row-wrap">
          {(["open", "blocked", "done"] as Filter[]).map((f) => (
            <Chip key={f} tone={filter === f ? "act" : "neutral"} selected={filter === f} onClick={() => setFilter(f)}>{f[0].toUpperCase() + f.slice(1)}</Chip>
          ))}
        </div>
      }
    />
  );
}
