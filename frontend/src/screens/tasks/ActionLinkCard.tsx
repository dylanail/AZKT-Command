/* Reminder emails carry secondary links to /tasks/<id>?action=done|snooze|reschedule|cancel
   (backend/app/services/email_templates.py review_link). A GET must never change anything
   (spec §5.4, C06), so arriving here only pre-selects the action: this card asks, and the POST
   happens when the person presses Confirm. The link is removed from the URL afterwards so a refresh
   or a Back does not re-offer it. The server decides who may act; its reason is shown as written. */
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useCommand } from "../../lib/useCommand";
import { Button, Chip, Field, GlassPanel, Input, Textarea } from "../../ui";
import { ScheduleFields, SNOOZE_OPTIONS, scheduleFromTask, schedulePayload, taskPath, type ScheduleValue } from "./TaskSheets";
import { STATUS_LABEL, isClosed, type TaskView } from "./types";
import "../../styles/notify.css";

type Action = "done" | "snooze" | "reschedule" | "cancel";
const ACTIONS: Action[] = ["done", "snooze", "reschedule", "cancel"];
const COMMAND: Record<Action, string> = { done: "complete", snooze: "snooze", reschedule: "reschedule", cancel: "cancel" };
const TITLES: Record<Action, string> = {
  done: "Mark this task done?",
  snooze: "Snooze this reminder?",
  reschedule: "Move this task to a new time?",
  cancel: "Cancel this task?",
};
const BLURBS: Record<Action, string> = {
  done: "You opened this from a reminder. Nothing has happened yet.",
  snooze: "The reminder moves; the task's own time stays where it is.",
  reschedule: "Reminders already scheduled for the old time are cancelled.",
  cancel: "The task closes and its scheduled reminders are cancelled.",
};

export function ActionLinkCard({ task, onDone }: { task: TaskView; onDone: () => void }) {
  const [params, setParams] = useSearchParams();
  const { run, busy } = useCommand();
  const raw = params.get("action");
  const [sched, setSched] = useState<ScheduleValue>(() => scheduleFromTask(task, task.timezone));
  const [minutes, setMinutes] = useState(60);
  const [reason, setReason] = useState("");
  const [blocked, setBlocked] = useState<string | null>(null);

  if (!raw) return null;
  const action = ACTIONS.includes(raw as Action) ? (raw as Action) : null;

  const clear = () => {
    const next = new URLSearchParams(params);
    next.delete("action");
    setParams(next, { replace: true });
  };

  if (!action) {
    return (
      <GlassPanel clip className="nf-action nf-action--unknown">
        <div className="nf-action__body">
          <span className="nf-action__title">Unknown link</span>
          <span className="fs13 t3">
            That link asked AZKT to do “{raw}”, which isn&apos;t something this page can do. Nothing has changed.
            The task is below, with its buttons.
          </span>
          <div className="nf-action__foot"><Button variant="soft" onClick={clear}>Close this</Button></div>
        </div>
      </GlassPanel>
    );
  }

  if (isClosed(task)) {
    return (
      <GlassPanel clip className="nf-action nf-action--unknown">
        <div className="nf-action__body">
          <span className="nf-action__title">Nothing to do here</span>
          <span className="fs13 t3">
            This task is already {(STATUS_LABEL[task.status] || task.status).toLowerCase()}, so the link from the reminder
            no longer applies. Nothing has changed.
          </span>
          <div className="nf-action__foot"><Button variant="soft" onClick={clear}>Close this</Button></div>
        </div>
      </GlassPanel>
    );
  }

  const key = `action-link:${action}`;
  const body = (): Record<string, unknown> => {
    switch (action) {
      case "snooze": return { minutes, expected_version: task.version };
      case "reschedule": return { ...schedulePayload(sched), expected_version: task.version };
      case "cancel": return { reason: reason.trim() || undefined, expected_version: task.version };
      default: return { expected_version: task.version };
    }
  };
  const success: Record<Action, string> = {
    done: "Marked done.",
    snooze: `Reminder snoozed ${minutes} minutes. The task's time is unchanged.`,
    reschedule: "Moved. Old reminders are cancelled.",
    cancel: "Cancelled. Scheduled reminders are cancelled too.",
  };

  const confirm = async () => {
    setBlocked(null);
    const r = await run(key, taskPath(task.id, COMMAND[action]), body(), {
      success: success[action],
      onError: (m) => setBlocked(m),
    });
    if (!r) return;
    if (r.status === "blocked") { setBlocked(r.decision.reasons.join(" · ") || "A check is blocking this."); return; }
    clear();
    onDone();
  };

  const confirmLabel: Record<Action, string> = { done: "Mark it done", snooze: "Snooze", reschedule: "Save the new time", cancel: "Cancel the task" };
  const needsDate = action === "reschedule" && !sched.date;

  return (
    <GlassPanel clip className="nf-action">
      <div className="nf-action__body" role="region" aria-label="Action from your reminder">
        <span className="nf-action__title">{TITLES[action]}</span>
        <span className="fs13 t3">
          {BLURBS[action]} {task.title} · currently {(STATUS_LABEL[task.status] || task.status).toLowerCase()} · version {task.version}.
        </span>

        {action === "snooze" ? (
          <div className="nf-action__picks" role="group" aria-label="How long">
            {SNOOZE_OPTIONS.map((o) => (
              <Chip key={o.minutes} size="sm" tone={minutes === o.minutes ? "act" : "neutral"} selected={minutes === o.minutes} onClick={() => setMinutes(o.minutes)}>{o.label}</Chip>
            ))}
            <label className="row fs13 t3" style={{ gap: 8 }}>
              <Input type="number" min={1} max={10080} aria-label="Minutes" value={minutes}
                onChange={(e) => setMinutes(Math.min(10080, Math.max(1, parseInt(e.target.value, 10) || 1)))}
                style={{ width: 96, minWidth: 0 }} />
              minutes
            </label>
          </div>
        ) : null}

        {action === "reschedule" ? <ScheduleFields value={sched} onChange={setSched} /> : null}

        {action === "cancel" ? (
          <Field label="Why (optional)">
            <Textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2} placeholder="Useful later when someone asks what happened." />
          </Field>
        ) : null}

        {action === "done" && (task.evidence_required || []).length ? (
          <span className="fs13" style={{ color: "var(--risk)" }}>
            This task needs proof before it can be completed. Use “Complete task” below to add it — confirming here will be refused.
          </span>
        ) : null}

        {blocked ? <span className="fs13" style={{ color: "var(--blocked)" }} role="alert">{blocked}</span> : null}

        <div className="nf-action__foot">
          <Button variant={action === "cancel" ? "danger" : "primary"} size="xl" loading={busy(key)} onClick={confirm}
            disabled={needsDate} disabledReason="Pick a date first.">{confirmLabel[action]}</Button>
          <Button variant="ghost" size="xl" onClick={clear}>Not now</Button>
        </div>
      </div>
    </GlassPanel>
  );
}
