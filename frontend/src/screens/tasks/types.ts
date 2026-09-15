/* Task shapes and pure helpers shared by Tasks, TaskDetail, Sales (lead pane) and Contacts.
   Mirrors backend/app/services/tasks.py serialize_task + routers/tasks.py task_view. */
import { TZ, formatWhen, type TzName } from "../../lib/format";
import type { CommandResult } from "../../lib/api";

export type TaskType = "call" | "meeting" | "follow_up" | "operational";
export type TaskStatus = "open" | "in_progress" | "blocked" | "waiting" | "awaiting_verification" | "completed" | "cancelled";
export type ReminderKind = "at" | "15m" | "1h" | "1d" | "custom";

export interface EvidenceRequirement { kind: string; min?: number; label?: string; }
export interface EvidenceEntry {
  asset_ids?: string[];
  note?: string | null;
  reading?: Record<string, unknown> | null;
  by?: string | null;
  at?: string | null;
}

export interface TaskView {
  id: string;
  version: number;
  title: string;
  type: TaskType | string;
  status: TaskStatus | string;
  priority?: string | null;
  owner_user_id?: string | null;
  assigned_by?: string | null;
  contact_id?: string | null;
  opportunity_id?: string | null;
  vehicle_id?: string | null;
  case_id?: string | null;
  shipment_id?: string | null;
  import_request_id?: string | null;
  notes?: string | null;
  instructions?: string | null;
  due_at?: string | null;
  start_at?: string | null;
  end_at?: string | null;
  timezone?: string | null;
  reminder_kind?: ReminderKind | string | null;
  reminder_custom_minutes?: number | null;
  schedule_revision?: number | null;
  snoozed_until?: string | null;
  snoozed_until_local?: string | null;
  next_check_at?: string | null;
  next_check_label?: string | null;
  evidence_required?: Array<EvidenceRequirement | string> | null;
  evidence?: EvidenceEntry[] | null;
  block_reason?: string | null;
  verification_status?: string | null;
  verified_by?: string | null;
  verified_at?: string | null;
  rejection_reason?: string | null;
  completed_at?: string | null;
  completed_by?: string | null;
  cancelled_at?: string | null;
  cancel_reason?: string | null;
  source_kind?: string | null;
  gate_requirement?: string | null;
  is_suggestion?: boolean;
  overdue?: boolean;
  created_at?: string | null;
  updated_at?: string | null;
  /* task_view additions */
  local_due?: string | null;
  zone_due?: string | null;
  tokyo_due?: string | null;
  buckets?: string[];
  /* schedule endpoint */
  day?: string | null;
}

export interface TaskRelated {
  vehicle?: { id: string; title?: string | null; stock_no?: string | null; location?: string | null; hero_asset_id?: string | null; photo?: string | null } | null;
  contact?: { id: string; name?: string | null; company?: string | null } | null;
  opportunity?: { id: string; pipeline?: string; stage?: string; stage_label?: string } | null;
}

export interface TaskListResp { items: TaskView[]; total: number; view?: string; bucket?: string | null; }

/* ---------- secondary views (routers/tasks.py list_cases / list_promises) ---------- */
/** GET /api/tasks/cases?status=open|all&limit= — Case rows (models/tasks.py Case). */
export interface CaseView {
  id: string;
  title: string;
  /** shipping_quote | shipment | recon | sale | dispute | listing_cleanup | sourcing | reply | other */
  kind: string;
  /** open | waiting | blocked | needs_owner | resolved | cancelled */
  status: string;
  owner_role: string | null;
  owner_user_id: string | null;
  vehicle_id: string | null;
  contact_id: string | null;
  opportunity_id: string | null;
  shipment_id: string | null;
  import_request_id: string | null;
  conversation_id: string | null;
  summary: string | null;
  waiting_on: string | null;
  next_action: string | null;
  next_check_at: string | null;
  overdue_check: boolean;
  resolved_at: string | null;
  version: number;
}
export interface CaseListResp { items: CaseView[]; total: number; as_of: string }

/** GET /api/tasks/promises?status=open|all&limit= — Commitment rows (models/tasks.py Commitment). */
export interface PromiseView {
  id: string;
  text: string;
  /** proposed | open | met | missed | withdrawn */
  status: string;
  contact_id: string | null;
  /** null when the role cannot see contacts — never a stand-in name. */
  contact_name: string | null;
  vehicle_id: string | null;
  opportunity_id: string | null;
  made_by: string | null;
  made_at: string | null;
  due_at: string | null;
  overdue: boolean;
  source_kind: string | null;
  source_id: string | null;
  version: number;
}
export interface PromiseListResp { items: PromiseView[]; total: number; as_of: string }

export const CASE_KIND_LABEL: Record<string, string> = {
  shipping_quote: "Shipping quote", shipment: "Shipment", recon: "Recon", sale: "Sale", dispute: "Dispute",
  listing_cleanup: "Listing cleanup", sourcing: "Sourcing", reply: "Reply", other: "Case",
};
export const CASE_STATUS_LABEL: Record<string, string> = {
  open: "Open", waiting: "Waiting", blocked: "Blocked", needs_owner: "Needs the owner",
  resolved: "Resolved", cancelled: "Cancelled",
};
export const PROMISE_STATUS_LABEL: Record<string, string> = {
  proposed: "Proposed", open: "Open", met: "Kept", missed: "Missed", withdrawn: "Withdrawn",
};

/** Colour for a case row; null keeps it neutral. */
export function caseHealth(c: Pick<CaseView, "status" | "overdue_check">): { health: "blocked" | "risk" | "wait" | "ok"; label: string } | null {
  if (c.status === "blocked") return { health: "blocked", label: "Blocked" };
  if (c.status === "needs_owner") return { health: "risk", label: "Needs the owner" };
  if (c.status === "resolved") return { health: "ok", label: "Resolved" };
  if (c.status === "waiting") return { health: "wait", label: "Waiting" };
  if (c.overdue_check) return { health: "risk", label: "Check overdue" };
  return null;
}

export function promiseHealth(p: Pick<PromiseView, "status" | "overdue">): { health: "blocked" | "risk" | "wait" | "ok"; label: string } | null {
  if (p.status === "met") return { health: "ok", label: "Kept" };
  if (p.status === "missed") return { health: "blocked", label: "Missed" };
  if (p.status === "withdrawn") return null;
  if (p.overdue) return { health: "risk", label: "Past due" };
  if (p.status === "proposed") return { health: "wait", label: "Proposed" };
  return null;
}

export const TYPE_LABEL: Record<string, string> = { call: "Call", meeting: "Meeting", follow_up: "Follow-up", operational: "Task" };
export const STATUS_LABEL: Record<string, string> = {
  open: "Open", in_progress: "In progress", blocked: "Blocked", waiting: "Waiting",
  awaiting_verification: "Awaiting verification", completed: "Done", cancelled: "Cancelled",
};
export const REMINDER_OPTIONS: { value: ReminderKind; label: string; short: string }[] = [
  { value: "at", label: "At the time", short: "at the time" },
  { value: "15m", label: "15 min before", short: "15 min before" },
  { value: "1h", label: "1 hour before", short: "1 hour before" },
  { value: "1d", label: "1 day before", short: "1 day before" },
  { value: "custom", label: "Custom", short: "custom" },
];
export const ACTIVE_STATUSES = ["open", "in_progress", "blocked", "waiting", "awaiting_verification"];

export function typeLabel(t: Pick<TaskView, "type">): string {
  return TYPE_LABEL[t.type] || "Task";
}
export function isActive(t: Pick<TaskView, "status">): boolean {
  return ACTIVE_STATUSES.includes(t.status);
}
export function isClosed(t: Pick<TaskView, "status">): boolean {
  return t.status === "completed" || t.status === "cancelled";
}
export function isOverdue(t: Pick<TaskView, "due_at" | "status" | "overdue">, now: Date = new Date()): boolean {
  if (t.overdue !== undefined && t.overdue !== null) return !!t.overdue && !isClosed(t);
  if (!t.due_at || isClosed(t) || t.status === "awaiting_verification") return false;
  const d = new Date(t.due_at);
  return !Number.isNaN(d.getTime()) && d.getTime() < now.getTime();
}
export function isSnoozed(t: Pick<TaskView, "snoozed_until">, now: Date = new Date()): boolean {
  if (!t.snoozed_until) return false;
  const d = new Date(t.snoozed_until);
  return !Number.isNaN(d.getTime()) && d.getTime() > now.getTime();
}
/** Tokyo shown alongside Phoenix when the task was scheduled in Japan. */
export function showsTokyo(t: Pick<TaskView, "timezone">): boolean {
  return t.timezone === TZ.tokyo;
}
export function reminderOffsetMinutes(kind: string | null | undefined, custom?: number | null): number | null {
  switch (kind) {
    case "at": return 0;
    case "15m": return 15;
    case "1h": return 60;
    case "1d": return 24 * 60;
    case "custom": return custom && custom > 0 ? custom : null;
    default: return null;
  }
}
export function reminderLabel(t: Pick<TaskView, "reminder_kind" | "reminder_custom_minutes" | "snoozed_until" | "due_at">): string {
  if (isSnoozed(t)) return `Snoozed until ${formatWhen(t.snoozed_until, { style: "time" })}`;
  if (!t.reminder_kind) return t.due_at ? "No reminder" : "Not scheduled";
  if (t.reminder_kind === "custom") return `Reminder ${t.reminder_custom_minutes ?? "?"} min before`;
  if (t.reminder_kind === "at") return "Reminder at the time";
  const o = REMINDER_OPTIONS.find((r) => r.value === t.reminder_kind);
  return o ? `Reminder ${o.short}` : "Reminder set";
}
/** When the reminder will fire, for previews. */
export function reminderFireAt(dueIso: string | Date | null | undefined, kind: string | null | undefined, custom?: number | null): Date | null {
  if (!dueIso) return null;
  const off = reminderOffsetMinutes(kind, custom);
  if (off === null) return null;
  const d = dueIso instanceof Date ? dueIso : new Date(dueIso);
  if (Number.isNaN(d.getTime())) return null;
  return new Date(d.getTime() - off * 60000);
}

export function evidenceRequirements(t: Pick<TaskView, "evidence_required">): { kind: string; min: number; label: string }[] {
  return (t.evidence_required || []).map((r) => {
    const kind = typeof r === "string" ? r : String(r.kind || "note");
    const min = typeof r === "string" ? 1 : Math.max(1, Number(r.min ?? 1) || 1);
    const label = typeof r === "string" ? EVIDENCE_LABEL[kind] || kind : r.label || EVIDENCE_LABEL[kind] || kind;
    return { kind, min, label };
  });
}
export const EVIDENCE_LABEL: Record<string, string> = { photo: "Photo", note: "Note", reading: "Reading", receipt: "Receipt" };
export function evidenceHave(t: Pick<TaskView, "evidence">, kind: string): number {
  let have = 0;
  for (const e of t.evidence || []) {
    if (kind === "photo" || kind === "receipt") have += (e.asset_ids || []).length;
    else if (kind === "note" && e.note) have += 1;
    else if (kind === "reading" && e.reading && Object.keys(e.reading).length) have += 1;
  }
  return have;
}
/** Same rule as services/tasks.py _evidence_satisfied. */
export function missingEvidence(t: Pick<TaskView, "evidence" | "evidence_required">): string[] {
  return evidenceRequirements(t)
    .map((r) => ({ ...r, have: evidenceHave(t, r.kind) }))
    .filter((r) => r.have < r.min)
    .map((r) => `${r.label.toLowerCase()} (${r.have}/${r.min})`);
}

/** Pull the task out of a command envelope ({task}) or a bare task. */
export function unwrapTask(res: CommandResult<unknown> | null | undefined): TaskView | null {
  const d = res?.data as { task?: TaskView } | TaskView | null | undefined;
  if (!d || typeof d !== "object") return null;
  if ("task" in d && d.task && typeof d.task === "object") return d.task as TaskView;
  if ("id" in d && "title" in d) return d as TaskView;
  return null;
}

/* ---------- local date/time in a zone ---------- */
function partsIn(d: Date, tz: string): Record<string, number> {
  const p = new Intl.DateTimeFormat("en-US", {
    timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).formatToParts(d);
  const out: Record<string, number> = {};
  for (const x of p) if (x.type !== "literal") out[x.type] = Number(x.value);
  if (out.hour === 24) out.hour = 0;
  return out;
}
function tzOffsetMs(d: Date, tz: string): number {
  const q = partsIn(d, tz);
  return Date.UTC(q.year, q.month - 1, q.day, q.hour, q.minute, q.second) - d.getTime();
}
/** "YYYY-MM-DD" and "HH:MM" for an instant in a zone (for date/time inputs). */
export function localParts(iso: string | Date | null | undefined, tz: TzName = TZ.phoenix): { date: string; time: string } {
  const d = iso instanceof Date ? iso : iso ? new Date(iso) : null;
  if (!d || Number.isNaN(d.getTime())) return { date: "", time: "" };
  const q = partsIn(d, tz);
  const pad = (n: number) => String(n).padStart(2, "0");
  return { date: `${q.year}-${pad(q.month)}-${pad(q.day)}`, time: `${pad(q.hour)}:${pad(q.minute)}` };
}
/** Wall-clock date + time in `tz` → UTC instant (DST-safe, no fixed offsets). */
export function zonedToUtc(date: string, time: string, tz: TzName = TZ.phoenix): Date | null {
  const [y, m, d] = date.split("-").map(Number);
  const [hh, mm] = (time || "09:00").split(":").map(Number);
  if (!y || !m || !d || Number.isNaN(hh)) return null;
  const guess = Date.UTC(y, m - 1, d, hh, mm || 0, 0);
  let inst = guess - tzOffsetMs(new Date(guess), tz);
  const off2 = tzOffsetMs(new Date(inst), tz);
  if (guess - off2 !== inst) inst = guess - off2;
  return new Date(inst);
}
/** Default scheduling zone: the signed-in person's zone when it is one we display, else Phoenix. */
export function defaultTz(userTz: string | null | undefined): TzName {
  return userTz === TZ.tokyo ? TZ.tokyo : TZ.phoenix;
}
/** A sensible default slot: next full hour, at least 30 minutes out. */
export function defaultSlot(tz: TzName = TZ.phoenix): { date: string; time: string } {
  const d = new Date(Date.now() + 30 * 60000);
  d.setMinutes(0, 0, 0);
  d.setTime(d.getTime() + 60 * 60000);
  return localParts(d, tz);
}
export function withinNextHours(t: Pick<TaskView, "due_at">, hours: number, now: Date = new Date()): boolean {
  if (!t.due_at) return false;
  const d = new Date(t.due_at).getTime();
  return d >= now.getTime() && d < now.getTime() + hours * 3600000;
}
