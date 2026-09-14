/* Employee task page: title, vehicle, due, Instructions, Required evidence; Complete task (evidence sheet) / I'm blocked.
   Offline: keep a local draft ("Waiting to upload"), retry; task stays incomplete. Owner verifies → vehicle may move.
   TODO(screen builder): GET /api/tasks/{id} ; POST /api/tasks/{id}/complete {evidence[]} ; POST /api/tasks/{id}/block {reason} ;
   POST /api/tasks/{id}/verify (owner) ; POST /api/uploads (evidence photos). */
import { useParams } from "react-router-dom";
import { useAuth } from "../../lib/auth";
import { Button } from "../../ui";
import { Scaffold } from "../scaffold";

export default function TaskDetail() {
  const { id = "" } = useParams();
  const { user } = useAuth();
  const canAct = user?.role !== "owner";
  return (
    <Scaffold
      title={`Task ${id}`}
      crumbs={[{ label: user?.role === "mechanic" ? "My tasks" : "Tasks", to: "/tasks" }, { label: id }]}
      probe={`/api/tasks/${encodeURIComponent(id)}`}
      emptyTitle="Task not found"
      emptyBody={`Nothing recorded for ${id} yet.`}
      actions={
        <>
          <Button variant="primary" disabled disabledReason={canAct ? "Evidence upload connects with the task data." : "Only the person assigned completes a task; you verify it."}>Complete task</Button>
          <Button variant="glass" disabled disabledReason="Blockers connect with the task data.">I'm blocked</Button>
        </>
      }
    />
  );
}
