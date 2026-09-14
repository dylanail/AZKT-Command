/* Reminder banner: due/overdue tasks with Open · Done · Snooze, as in the prototype's mobile banner.
   Same source as the bell (tasks API); GET links never mark done — every action is a POST. */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { describeError } from "../../lib/api";
import { TZ } from "../../lib/format";
import { Button, When, useToast } from "../../ui";
import { useDueTasks, type DueTask } from "./useTasksSummary";
import { showsTokyo, typeLabel } from "./types";
import "./tasks.css";

export interface ReminderBannerProps {
  view?: "my" | "all";
  /** Most items shown at once (default 3). */
  max?: number;
  /** Where "Open" goes; default the task page. */
  hrefFor?: (t: DueTask) => string;
  className?: string;
}

export function ReminderBanner({ view = "my", max = 3, hrefFor, className = "" }: ReminderBannerProps) {
  const due = useDueTasks({ view });
  const { toast } = useToast();
  const nav = useNavigate();
  const [busy, setBusy] = useState<string | null>(null);
  if (!due.tasks.length) return null;
  const act = async (t: DueTask, fn: () => Promise<unknown>, okMsg: string) => {
    setBusy(t.id);
    try { await fn(); toast({ message: okMsg, tone: "ok" }); }
    catch (e) { toast({ message: describeError(e), tone: "blocked" }); }
    finally { setBusy(null); }
  };
  return (
    <div className={["tk-banner", className].filter(Boolean).join(" ")} aria-label="Reminders">
      {due.tasks.slice(0, max).map((t) => {
        const href = hrefFor ? hrefFor(t) : t.opportunity_id ? `/sales?lead=${encodeURIComponent(t.opportunity_id)}` : `/tasks/${encodeURIComponent(t.id)}`;
        return (
          <div key={t.id} role="alert" className="tk-alert">
            <span className="tk-alert__dot" style={{ background: t.kind === "overdue" ? "var(--blocked)" : "var(--amber)" }} aria-hidden="true" />
            <div className="tk-alert__body">
              <span className="tk-alert__title">{t.kind === "overdue" ? "Overdue" : "Due now"} · {typeLabel(t)}</span>
              <span className="tk-alert__text">{t.title}{t.due_at ? <> · <When iso={t.due_at} tz={TZ.phoenix} withTokyo={showsTokyo(t)} /></> : null}</span>
              <div className="tk-alert__actions">
                <Button size="xs" variant="primary" onClick={() => nav(href)}>{t.opportunity_id ? "Open lead" : "Open"}</Button>
                <Button size="xs" variant="soft" loading={busy === t.id} onClick={() => act(t, () => due.complete(t.id), "Marked done")}>Done</Button>
                <Button size="xs" variant="soft" loading={busy === t.id} onClick={() => act(t, () => due.snooze(t.id, 10), "Snoozed 10 min · the time itself hasn't changed")}>Snooze 10 min</Button>
              </div>
            </div>
            <button type="button" className="toast__x" aria-label="Dismiss" onClick={() => due.dismiss(t.id)}>×</button>
          </div>
        );
      })}
      {due.tasks.length > max ? <span className="fs12 t3" style={{ padding: "0 6px" }}>{due.tasks.length - max} more waiting in Tasks.</span> : null}
    </div>
  );
}

export default ReminderBanner;
