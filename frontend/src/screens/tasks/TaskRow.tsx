/* One task row: type · title / what it's about · when (overdue obvious) · reminder · owner, with
   Done / Snooze / Reschedule / Assign / Cancel / Verify actions. Used by Tasks, Contacts and the Sales task view. */
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../../lib/auth";
import { can, whyNot } from "../../lib/perms";
import { TZ } from "../../lib/format";
import { Button, Chip, HealthLabel, Menu, When, type MenuItem } from "../../ui";
import { SNOOZE_OPTIONS, taskPath, useTaskCommand, type useTaskDialogs } from "./TaskSheets";
import { isActive, isClosed, isOverdue, isSnoozed, missingEvidence, reminderLabel, showsTokyo, typeLabel, STATUS_LABEL, type TaskView } from "./types";

export interface TaskAbout { label: string; to?: string; extra?: string; }

export interface TaskRowProps {
  task: TaskView;
  /** What the task is about (lead / vehicle / contact), resolved by the caller. */
  about?: TaskAbout | null;
  ownerName?: string;
  showOwner?: boolean;
  dialogs: ReturnType<typeof useTaskDialogs>;
  /** Called with the updated task after any successful action (null when the row should be reloaded). */
  onChanged: (t: TaskView | null) => void;
  /** Hide the title link (when the row already sits on the task page). */
  compact?: boolean;
  /** Always show Tokyo time next to Phoenix (the "Tokyo time" toggle). */
  tokyo?: boolean;
}

export function taskHealth(t: TaskView): { health: "blocked" | "risk" | "ok" | "wait"; label: string } | null {
  if (t.status === "blocked") return { health: "blocked", label: "Blocked" };
  if (t.status === "awaiting_verification") return { health: "wait", label: "Awaiting verification" };
  if (t.status === "waiting") return { health: "wait", label: "Waiting" };
  if (t.status === "completed") return { health: "ok", label: t.verification_status === "verified" ? "Verified" : "Done" };
  if (t.status === "cancelled") return null;
  if (t.verification_status === "rejected") return { health: "risk", label: "Evidence rejected" };
  if (isActive(t) && !t.owner_user_id) return { health: "risk", label: "Unassigned" };
  return null;
}

export function TaskRow({ task: t, about, ownerName, showOwner = true, dialogs, onChanged, compact = false, tokyo = false }: TaskRowProps) {
  const { user } = useAuth();
  const nav = useNavigate();
  const run = useTaskCommand();
  const overdue = isOverdue(t);
  const closed = isClosed(t);
  const write = can(user, "tasks.write");
  const verify = can(user, "tasks.verify");
  const assign = can(user, "tasks.assign");
  const needsEvidence = missingEvidence(t).length > 0;
  const health = taskHealth(t);

  const done = async () => {
    if (needsEvidence) { nav(`/tasks/${encodeURIComponent(t.id)}`); return; }
    const out = await run(taskPath(t.id, "complete"), { expected_version: t.version }, { okMessage: t.evidence_required?.length ? "Saved · Awaiting verification" : `Done: ${t.title}` });
    if (out.result?.status === "ok") onChanged(out.task);
  };
  const snooze = async (minutes: number) => {
    const out = await run(taskPath(t.id, "snooze"), { minutes, expected_version: t.version }, { okMessage: "Reminder snoozed · the time itself hasn't changed" });
    if (out.result?.status === "ok") onChanged(out.task);
  };
  const doVerify = async () => {
    const out = await run(taskPath(t.id, "verify"), { expected_version: t.version }, { okMessage: `${t.title} verified` });
    if (out.result?.status === "ok") onChanged(out.task);
  };
  const reopen = async () => {
    const out = await run(taskPath(t.id, "reopen"), { expected_version: t.version }, { okMessage: "Reopened" });
    if (out.result?.status === "ok") onChanged(out.task);
  };

  const doneReason = !write ? whyNot("tasks.write")
    : t.status === "awaiting_verification" ? "Waiting for the owner to verify."
    : closed ? `Already ${STATUS_LABEL[t.status]?.toLowerCase() || t.status}.` : undefined;

  const more: MenuItem[] = [];
  if (!closed) {
    for (const o of SNOOZE_OPTIONS) more.push({ label: `Snooze ${o.label}`, meta: "reminder only", onSelect: () => void snooze(o.minutes), disabled: !write || !t.due_at, disabledReason: !t.due_at ? "No time to snooze." : whyNot("tasks.write") });
    more.push({ label: "Reschedule", sepBefore: true, onSelect: () => dialogs.setReschedule(t), disabled: !write, disabledReason: whyNot("tasks.write") });
    more.push({ label: t.owner_user_id ? "Reassign" : "Assign", onSelect: () => dialogs.setAssign(t), disabled: !assign, disabledReason: "Only owner/manager assign tasks." });
    if (t.status === "awaiting_verification") {
      more.push({ label: "Verify", sepBefore: true, onSelect: () => void doVerify(), disabled: !verify, disabledReason: whyNot("tasks.verify") });
      more.push({ label: "Reject evidence", onSelect: () => dialogs.setReject(t), disabled: !verify, disabledReason: whyNot("tasks.verify") });
    }
    more.push({ label: "Cancel task", sepBefore: true, onSelect: () => dialogs.setCancel(t), disabled: !write, disabledReason: whyNot("tasks.write") });
  } else {
    more.push({ label: "Reopen", onSelect: () => void reopen(), disabled: !write, disabledReason: whyNot("tasks.write") });
  }
  if (!compact) more.push({ label: "Open task page", to: `/tasks/${encodeURIComponent(t.id)}`, sepBefore: true });

  return (
    <div className={["tk-row", overdue ? "tk-row--overdue" : ""].filter(Boolean).join(" ")}>
      <div className="tk-row__main">
        <div className="tk-row__title">
          <Chip size="sm" tone="soft">{typeLabel(t)}</Chip>
          {compact ? <span>{t.title}</span> : <Link to={`/tasks/${encodeURIComponent(t.id)}`}>{t.title}</Link>}
          {health ? <HealthLabel health={health.health} label={health.label} /> : null}
          {isSnoozed(t) ? <span className="fs12 t4">snoozed</span> : null}
        </div>
        <div className="tk-row__meta">
          {about ? (about.to ? <Link to={about.to}>{about.label}</Link> : <span>{about.label}</span>) : null}
          {about?.extra ? <span>· {about.extra}</span> : null}
          {t.notes && compact === false ? <span className="truncate" style={{ maxWidth: 360 }}>· {t.notes}</span> : null}
          {t.block_reason && t.status === "blocked" ? <span style={{ color: "var(--blocked)" }}>· {t.block_reason}</span> : null}
        </div>
      </div>
      <div className="tk-row__side">
        <span className={["tk-row__when", overdue ? "tk-row__when--overdue" : ""].filter(Boolean).join(" ")}>
          {overdue ? "Overdue · " : ""}
          {t.due_at ? <When iso={t.due_at} tz={TZ.phoenix} withTokyo={tokyo || showsTokyo(t)} /> : <span className="t4">Not scheduled</span>}
        </span>
        <span className="fs12 t4">
          {reminderLabel(t)}
          {showOwner ? <> · {ownerName ?? (t.owner_user_id ? "Assigned" : "Unassigned")}</> : null}
        </span>
        <div className="tk-row__actions">
          {!closed ? (
            t.status === "awaiting_verification" && verify ? (
              <Button size="xs" variant="primary" onClick={doVerify}>Verify</Button>
            ) : (
              <Button size="xs" variant="primary" onClick={done} disabled={!!doneReason} disabledReason={doneReason}>{needsEvidence ? "Open to complete" : "Done"}</Button>
            )
          ) : null}
          <Menu label="More actions" align="right" items={more} trigger={<Button size="xs" variant="soft">More</Button>} />
        </div>
      </div>
    </div>
  );
}
