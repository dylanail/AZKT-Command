/* Shared task dialogs/sheets and the command helper. Every write goes through POST /api/tasks[/{id}/{action}]
   and the CommandResult envelope; blocked/needs_review outcomes are explained, never swallowed. */
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, command, describeError, type CommandResult } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { can } from "../../lib/perms";
import { TZ, formatWhen, tzLabel, type TzName } from "../../lib/format";
import { useIsMobile } from "../../lib/viewport";
import { Button, Chip, Field, Input, ResponsiveDialog, Select, Textarea, useToast, EmptyState, Loading, Notice } from "../../ui";
import { usePeople } from "./usePeople";
import { fetchVehicleOptions, vehicleLabel, type VehicleOption } from "./useNames";
import {
  REMINDER_OPTIONS, TYPE_LABEL, defaultSlot, defaultTz, localParts, reminderFireAt, unwrapTask, zonedToUtc,
  type ReminderKind, type TaskView,
} from "./types";

/* ---------- command helper ---------- */
export interface TaskCommandOutcome {
  task: TaskView | null;
  result: CommandResult<{ task?: TaskView }> | null;
  error: unknown;
}
export function useTaskCommand() {
  const { toast } = useToast();
  const nav = useNavigate();
  return useCallback(async (path: string, body: Record<string, unknown> = {}, opts: { okMessage?: string; quiet?: boolean } = {}): Promise<TaskCommandOutcome> => {
    try {
      const res = await command<{ task?: TaskView }>(path, body);
      if (res.status === "needs_review") {
        toast({
          title: "Sent for approval", tone: "wait",
          message: res.decision?.reasons?.[0] || "The owner reviews this before it takes effect.",
          actions: res.approval_id ? [{ label: "Review", primary: true, onClick: () => nav(`/approvals/${res.approval_id}`) }] : undefined,
        });
        return { task: null, result: res, error: null };
      }
      if (res.status === "blocked") {
        toast({ message: res.decision?.reasons?.join("; ") || "A check is blocking this action.", tone: "blocked" });
        return { task: null, result: res, error: null };
      }
      if (opts.okMessage && !opts.quiet) toast({ message: opts.okMessage, tone: "ok" });
      return { task: unwrapTask(res), result: res, error: null };
    } catch (e) {
      if (!opts.quiet) toast({ message: describeError(e), tone: e instanceof ApiError && e.isBusinessGate ? "risk" : "blocked" });
      return { task: null, result: null, error: e };
    }
  }, [toast, nav]);
}
export const taskPath = (id: string, action: string) => `/api/tasks/${encodeURIComponent(id)}/${action}`;

/* ---------- schedule fields (date · time · zone · reminder) ---------- */
export interface ScheduleValue {
  date: string;
  time: string;
  tz: TzName;
  reminder: ReminderKind | "";
  customMinutes: number;
}
export function scheduleFromTask(t: TaskView | null | undefined, userTz?: string | null): ScheduleValue {
  const tz = (t?.timezone === TZ.tokyo ? TZ.tokyo : t?.timezone === TZ.phoenix ? TZ.phoenix : defaultTz(userTz)) as TzName;
  const parts = t?.due_at ? localParts(t.due_at, tz) : defaultSlot(tz);
  return { date: parts.date, time: parts.time, tz, reminder: (t?.reminder_kind as ReminderKind) || "", customMinutes: t?.reminder_custom_minutes || 30 };
}
export function scheduleInstant(v: ScheduleValue): Date | null {
  if (!v.date) return null;
  return zonedToUtc(v.date, v.time || "09:00", v.tz);
}
export function schedulePayload(v: ScheduleValue): Record<string, unknown> {
  const inst = scheduleInstant(v);
  const out: Record<string, unknown> = { timezone: v.tz };
  if (inst) out.due_at = inst.toISOString();
  if (v.reminder) {
    out.reminder_kind = v.reminder;
    if (v.reminder === "custom") out.reminder_custom_minutes = Math.max(1, v.customMinutes || 1);
  }
  return out;
}

export function ScheduleFields({ value, onChange, required = true }: { value: ScheduleValue; onChange: (v: ScheduleValue) => void; required?: boolean }) {
  const inst = scheduleInstant(value);
  const fire = value.reminder ? reminderFireAt(inst, value.reminder, value.customMinutes) : null;
  return (
    <div className="stack" style={{ gap: 12 }}>
      <div className="tk-grid2">
        <Field label="Date" required={required}>
          <Input type="date" value={value.date} onChange={(e) => onChange({ ...value, date: e.target.value })} />
        </Field>
        <Field label="Time" required={required}>
          <Input type="time" value={value.time} onChange={(e) => onChange({ ...value, time: e.target.value })} />
        </Field>
      </div>
      <Field label="Time zone" hint={value.tz === TZ.tokyo ? "Shown as Tokyo and Phoenix time." : "Shown as Phoenix time."}>
        <Select value={value.tz} onChange={(e) => onChange({ ...value, tz: e.target.value as TzName })}>
          <option value={TZ.phoenix}>Phoenix (AZ)</option>
          <option value={TZ.tokyo}>Tokyo (JST)</option>
        </Select>
      </Field>
      <div className="field">
        <span className="field__label"><span>Remind me</span></span>
        <div className="tk-reminds" role="group" aria-label="Reminder">
          <Chip size="sm" tone={value.reminder === "" ? "act" : "neutral"} selected={value.reminder === ""} onClick={() => onChange({ ...value, reminder: "" })}>No reminder</Chip>
          {REMINDER_OPTIONS.map((o) => (
            <Chip key={o.value} size="sm" tone={value.reminder === o.value ? "act" : "neutral"} selected={value.reminder === o.value} onClick={() => onChange({ ...value, reminder: o.value })}>{o.label}</Chip>
          ))}
        </div>
        {value.reminder === "custom" ? (
          <label className="row" style={{ gap: 8, color: "var(--t3)", fontSize: 13 }}>
            <input className="input" type="number" min={1} style={{ width: 88, height: 36 }} value={value.customMinutes} onChange={(e) => onChange({ ...value, customMinutes: Math.max(1, parseInt(e.target.value, 10) || 1) })} aria-label="Minutes before" />
            minutes before
          </label>
        ) : null}
        <span className="field__hint">
          {inst ? <>Due {formatWhen(inst, { tz: value.tz, style: "long" })}{value.tz === TZ.tokyo ? ` · ${formatWhen(inst, { tz: TZ.phoenix, style: "long" })}` : ""}</> : "Pick a date and time."}
          {fire ? <> · Reminder {formatWhen(fire, { tz: value.tz, style: "short" })}</> : null}
        </span>
      </div>
    </div>
  );
}

/* ---------- Reschedule ---------- */
export function RescheduleSheet({ task, open, onClose, onSaved }: { task: TaskView | null; open: boolean; onClose: () => void; onSaved: (t: TaskView | null) => void }) {
  const { user } = useAuth();
  const mobile = useIsMobile();
  const run = useTaskCommand();
  const [v, setV] = useState<ScheduleValue>(() => scheduleFromTask(task, user?.timezone));
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) setV(scheduleFromTask(task, user?.timezone)); }, [open, task, user?.timezone]);
  if (!task) return null;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!v.date) return;
    setBusy(true);
    const body: Record<string, unknown> = { ...schedulePayload(v), expected_version: task.version };
    if (!v.reminder) body.clear_reminder = true;
    const out = await run(taskPath(task.id, "reschedule"), body, { okMessage: "Rescheduled. Old reminders are cancelled." });
    setBusy(false);
    if (!out.error && out.result?.status === "ok") { onSaved(out.task); onClose(); }
  };
  return (
    <ResponsiveDialog mobile={mobile} open={open} onClose={onClose} title="Reschedule" description={task.title}
      footer={<><Button type="submit" form="tk-reschedule" variant="primary" loading={busy} disabled={!v.date} disabledReason="Pick a date first.">Save</Button><Button variant="ghost" onClick={onClose}>Cancel</Button></>}>
      <form id="tk-reschedule" onSubmit={submit}><ScheduleFields value={v} onChange={setV} /></form>
    </ResponsiveDialog>
  );
}

/* ---------- Assign ---------- */
export function AssignSheet({ task, open, onClose, onSaved }: { task: TaskView | null; open: boolean; onClose: () => void; onSaved: (t: TaskView | null) => void }) {
  const mobile = useIsMobile();
  const run = useTaskCommand();
  const { people, denied, loading } = usePeople(open);
  const [pick, setPick] = useState<string | null>(task?.owner_user_id || null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) setPick(task?.owner_user_id || null); }, [open, task]);
  if (!task) return null;
  const submit = async () => {
    setBusy(true);
    const out = await run(taskPath(task.id, "assign"), { owner_user_id: pick, expected_version: task.version }, { okMessage: pick ? "Assigned. It shows in their My tasks right away." : "Unassigned." });
    setBusy(false);
    if (!out.error && out.result?.status === "ok") { onSaved(out.task); onClose(); }
  };
  return (
    <ResponsiveDialog mobile={mobile} open={open} onClose={onClose} title="Assign task" description={task.title}
      footer={<><Button variant="primary" loading={busy} onClick={submit} disabled={denied} disabledReason="Your role can't see the team list.">Assign</Button><Button variant="ghost" onClick={onClose}>Cancel</Button><span className="fs12 t4">Shows in their My tasks right away. The owner verifies when it's done.</span></>}>
      {loading ? <Loading rows={2} /> : denied ? (
        <Notice tone="risk" lead="Team list unavailable">Your role can't list people, so this task can't be reassigned from here.</Notice>
      ) : (
        <div role="radiogroup" aria-label="Who" className="tk-people">
          <button type="button" role="radio" aria-checked={pick === null} className="tk-person" onClick={() => setPick(null)}>
            <span className="tk-person__mark" aria-hidden="true">{pick === null ? "●" : "○"}</span>
            <span><span>Unassigned</span><span className="t4 fs12"> · no owner (shows as an exception)</span></span>
          </button>
          {people.map((p) => (
            <button key={p.id} type="button" role="radio" aria-checked={pick === p.id} className="tk-person" onClick={() => setPick(p.id)}>
              <span className="tk-person__mark" aria-hidden="true">{pick === p.id ? "●" : "○"}</span>
              <span><span>{p.display_name}</span><span className="t4 fs12"> · {p.role}</span></span>
            </button>
          ))}
          {!people.length ? <EmptyState title="No one to assign yet" body="Invite people from Team first." /> : null}
        </div>
      )}
    </ResponsiveDialog>
  );
}

/* ---------- Cancel / Reject (reason dialogs) ---------- */
export function ReasonDialog({ open, onClose, title, description, label, placeholder, confirmLabel, required = false, tone = "primary", onConfirm }: {
  open: boolean; onClose: () => void; title: string; description?: string; label: string; placeholder?: string;
  confirmLabel: string; required?: boolean; tone?: "primary" | "danger"; onConfirm: (reason: string) => Promise<boolean | void>;
}) {
  const mobile = useIsMobile();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { if (open) setReason(""); }, [open]);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (required && !reason.trim()) return;
    setBusy(true);
    const ok = await onConfirm(reason.trim());
    setBusy(false);
    if (ok !== false) onClose();
  };
  return (
    <ResponsiveDialog mobile={mobile} open={open} onClose={onClose} title={title} description={description} size="sm" initialFocusRef={ref}
      footer={<><Button type="submit" form="tk-reason" variant={tone} loading={busy} disabled={required && !reason.trim()} disabledReason="Add a reason first.">{confirmLabel}</Button><Button variant="ghost" onClick={onClose}>Back</Button></>}>
      <form id="tk-reason" onSubmit={submit}>
        <Field label={label} required={required}>
          <Textarea ref={ref} value={reason} onChange={(e) => setReason(e.target.value)} placeholder={placeholder} rows={3} />
        </Field>
      </form>
    </ResponsiveDialog>
  );
}

/* ---------- Snooze options ---------- */
export const SNOOZE_OPTIONS: { minutes: number; label: string }[] = [
  { minutes: 10, label: "10 min" }, { minutes: 60, label: "1 hour" }, { minutes: 24 * 60, label: "1 day" },
];

/* ---------- New task ---------- */
export interface NewTaskDefaults {
  title?: string;
  type?: string;
  vehicle_id?: string | null;
  contact_id?: string | null;
  opportunity_id?: string | null;
  owner_user_id?: string | null;
}
export function NewTaskDialog({ open, onClose, onCreated, defaults }: { open: boolean; onClose: () => void; onCreated: (t: TaskView | null) => void; defaults?: NewTaskDefaults }) {
  const { user } = useAuth();
  const mobile = useIsMobile();
  const run = useTaskCommand();
  const { people, denied } = usePeople(open && can(user, "tasks.assign"));
  const [title, setTitle] = useState("");
  const [type, setType] = useState("operational");
  const [sched, setSched] = useState<ScheduleValue>(() => ({ ...scheduleFromTask(null, user?.timezone), date: "" }));
  const [owner, setOwner] = useState<string>("");
  const [vehicleId, setVehicleId] = useState("");
  const [contactId, setContactId] = useState("");
  const [oppId, setOppId] = useState("");
  const [notes, setNotes] = useState("");
  const [evidencePhoto, setEvidencePhoto] = useState(false);
  const [vehicles, setVehicles] = useState<VehicleOption[] | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const titleRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setTitle(defaults?.title || "");
    setType(defaults?.type || "operational");
    setSched({ ...scheduleFromTask(null, user?.timezone), date: "" });
    setOwner(defaults?.owner_user_id || (can(user, "tasks.assign") ? "" : user?.id || ""));
    setVehicleId(defaults?.vehicle_id || "");
    setContactId(defaults?.contact_id || "");
    setOppId(defaults?.opportunity_id || "");
    setNotes("");
    setEvidencePhoto(false);
    setErr(null);
    if (vehicles === undefined) fetchVehicleOptions().then(setVehicles).catch(() => setVehicles(null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;
    if (sched.reminder && !sched.date) { setErr("A reminder needs a due date and time."); return; }
    setBusy(true);
    setErr(null);
    const body: Record<string, unknown> = {
      title: title.trim(), type, notes, timezone: sched.tz,
      ...(sched.date ? schedulePayload(sched) : {}),
      ...(owner ? { owner_user_id: owner } : {}),
      ...(vehicleId.trim() ? { vehicle_id: vehicleId.trim() } : {}),
      ...(contactId.trim() ? { contact_id: contactId.trim() } : {}),
      ...(oppId.trim() ? { opportunity_id: oppId.trim() } : {}),
      ...(evidencePhoto ? { evidence_required: [{ kind: "photo", min: 1 }] } : {}),
    };
    const out = await run("/api/tasks", body, { okMessage: `Added "${title.trim()}"` });
    setBusy(false);
    if (out.error) { setErr(describeError(out.error)); return; }
    if (out.result?.status === "ok") { onCreated(out.task); onClose(); }
  };
  const canAssign = can(user, "tasks.assign") && !denied;
  return (
    <ResponsiveDialog mobile={mobile} open={open} onClose={onClose} title="New task" initialFocusRef={titleRef}
      footer={<><Button type="submit" form="tk-new" variant="primary" loading={busy} disabled={!title.trim()} disabledReason="Give the task a title.">Add task</Button><Button variant="ghost" onClick={onClose}>Cancel</Button></>}>
      <form id="tk-new" className="stack" onSubmit={submit}>
        {err ? <Notice tone="blocked" lead="Not saved" role="alert">{err}</Notice> : null}
        <Field label="What needs doing" required>
          <Input ref={titleRef} value={title} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Replace rear brake pads" maxLength={200} />
        </Field>
        <div className="tk-grid2">
          <Field label="Type">
            <Select value={type} onChange={(e) => setType(e.target.value)}>
              {Object.entries(TYPE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </Select>
          </Field>
          <Field label="Owner" hint={!canAssign ? "Only owner/manager assign; this task is yours." : undefined}>
            {canAssign ? (
              <Select value={owner} onChange={(e) => setOwner(e.target.value)}>
                <option value="">Unassigned</option>
                {user ? <option value={user.id}>{user.display_name} (you)</option> : null}
                {people.filter((p) => p.id !== user?.id).map((p) => <option key={p.id} value={p.id}>{p.display_name} · {p.role}</option>)}
              </Select>
            ) : <Input value={user?.display_name || "You"} disabled />}
          </Field>
        </div>
        <ScheduleFields value={sched} onChange={setSched} required={false} />
        <Field label="Vehicle" hint={vehicles === null ? "Vehicle list not available yet — paste a vehicle id." : "Optional."}>
          {vehicles && vehicles.length ? (
            <Select value={vehicleId} onChange={(e) => setVehicleId(e.target.value)}>
              <option value="">None</option>
              {vehicles.map((v) => <option key={v.id} value={v.id}>{vehicleLabel(v)}</option>)}
            </Select>
          ) : <Input value={vehicleId} onChange={(e) => setVehicleId(e.target.value)} placeholder="Vehicle id (optional)" />}
        </Field>
        <div className="tk-grid2">
          <Field label="Contact id" hint="Optional.">
            <Input value={contactId} onChange={(e) => setContactId(e.target.value)} placeholder="Contact id" />
          </Field>
          <Field label="Lead id" hint="Optional.">
            <Input value={oppId} onChange={(e) => setOppId(e.target.value)} placeholder="Opportunity id" />
          </Field>
        </div>
        <Field label="Proof when done">
          <div>
            <Chip size="sm" tone={evidencePhoto ? "act" : "neutral"} selected={evidencePhoto} onClick={() => setEvidencePhoto((v) => !v)}>{evidencePhoto ? "Photo required" : "No photo required"}</Chip>
          </div>
        </Field>
        <Field label="Notes">
          <Textarea value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Notes (optional)" rows={3} />
        </Field>
      </form>
    </ResponsiveDialog>
  );
}

/** Label for a person + zone line, e.g. "Marco · AZ". */
export function zoneSuffix(tz: string | null | undefined): string {
  return tz ? tzLabel(tz) : "";
}

export function useTaskDialogs() {
  const [reschedule, setReschedule] = useState<TaskView | null>(null);
  const [assign, setAssign] = useState<TaskView | null>(null);
  const [cancel, setCancel] = useState<TaskView | null>(null);
  const [reject, setReject] = useState<TaskView | null>(null);
  return useMemo(() => ({ reschedule, setReschedule, assign, setAssign, cancel, setCancel, reject, setReject }), [reschedule, assign, cancel, reject]);
}
