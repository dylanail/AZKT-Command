/* "Working on it": the durable mission behind the last reply. The progress stream is only a view of it —
   losing the connection says "Reconnecting…", never "failed", and the run carries on either way. */
import { humanize } from "../../../lib/links";
import { useCommand } from "../../../lib/useCommand";
import { Button, Chip, GlassPanel, When } from "../../../ui";
import { cancelRunPath } from "../api";
import { useRunEvents, type RunConnection } from "../useRunEvents";
import { RESTING_RUN, TERMINAL_MISSION, TERMINAL_RUN } from "../types";

const CONNECTION_WORDS: Record<RunConnection, string> = {
  idle: "",
  connecting: "Opening the progress feed…",
  live: "Live progress",
  reconnecting: "Reconnecting…",
  finished: "Finished",
  lost: "Progress feed paused",
};

const RUN_WORDS: Record<string, string> = {
  queued: "Queued",
  running: "Running",
  succeeded: "Finished",
  failed: "Stopped",
  cancelled: "Cancelled",
  needs_information: "Waiting for your answer",
  waiting_approval: "Waiting for your review",
  waiting_external: "Waiting on someone else",
  waiting_until: "Waiting for a scheduled check",
};

export interface MissionPanelProps {
  runId: string | null;
  missionId: string | null;
  startCursor: number;
  onChanged?: () => void;
}

export function MissionPanel({ runId, missionId, startCursor, onChanged }: MissionPanelProps) {
  const s = useRunEvents(runId, startCursor);
  const { run, mission, updates, connection, note } = s;
  const { run: command, busy } = useCommand();

  if (!runId && !missionId) return null;

  const status = run?.status || mission?.status || "running";
  const finished = (!!run && TERMINAL_RUN.has(run.status)) || (!!mission && TERMINAL_MISSION.has(mission.status));
  const resting = !!run && RESTING_RUN.has(run.status);
  const cancellable = !!runId && !finished && !resting;

  const cancel = async () => {
    if (!runId) return;
    const r = await command("cancel-run", cancelRunPath(runId), { reason: "cancelled from the Agents screen" }, {
      success: "Stopped. Anything already saved stays saved.",
    });
    if (r && r.status === "ok") { s.retry(); onChanged?.(); }
  };

  return (
    <GlassPanel padded className="ag-mission" aria-label="Work in progress">
      <div className="between">
        <div className="row-wrap">
          <span className="ag-mission__title">{finished ? "Finished" : "Working on it"}</span>
          <Chip size="sm" tone={finished ? "soft" : resting ? "wait" : "act"}>{RUN_WORDS[status] || humanize(status)}</Chip>
          {run ? <span className="fs12 t4 tnum">step {run.steps_used}{run.budget_steps ? ` of ${run.budget_steps}` : ""}</span> : null}
        </div>
        <div className="row-wrap">
          {connection === "lost" ? <Button size="sm" variant="soft" onClick={s.retry}>Reconnect</Button> : null}
          <Button
            size="md"
            variant="soft"
            loading={busy("cancel-run")}
            disabled={!cancellable}
            disabledReason={finished ? "This work has already finished." : resting ? "It is waiting on you, not running." : "Nothing to cancel."}
            onClick={() => void cancel()}
          >
            Cancel
          </Button>
        </div>
      </div>

      {mission?.outcome ? <div className="fs14 t2">{mission.outcome}</div> : null}

      <div className="fs12 t4">
        {CONNECTION_WORDS[connection]}
        {note ? ` · ${note}` : ""}
        {connection === "reconnecting" ? " The work keeps running while this reconnects." : ""}
      </div>

      {updates.length ? (
        <ol className="ag-updates">
          {updates.slice(-8).map((u) => (
            <li key={u.seq}>
              <span className="ag-updates__state">{humanize(String(u.state || "update"))}</span>
              <span className="grow">{String(u.text || "")}</span>
              {u.at ? <When iso={String(u.at)} relative className="fs12 t4 nowrap" /> : null}
            </li>
          ))}
        </ol>
      ) : (
        <div className="fs13 t3">No progress recorded yet.</div>
      )}

      {mission?.waiting_on ? (
        <div className="fs13 t3">
          Waiting on {humanize(mission.waiting_on)}
          {mission.next_check_at ? <> · next check <When iso={mission.next_check_at} relative /></> : null}
        </div>
      ) : null}

      {run?.error ? <div className="fs13" style={{ color: "var(--risk)" }}>{run.error}</div> : null}
    </GlassPanel>
  );
}
