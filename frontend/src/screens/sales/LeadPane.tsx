/* Lead pane — the inspector's "Lead" mode on desktop and the lead sheet on phones.
   Name · pipeline/age · stage chips (current disabled) · deposit line · contact / subject / budget / source / owner ·
   notes · tasks (+ Call / + Meeting / + Follow-up with reminder) · links · Mark lost / Reopen. */
import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, describeError } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { can, whyNot } from "../../lib/perms";
import { useQuery } from "../../lib/useQuery";
import { TZ } from "../../lib/format";
import { useIsMobile } from "../../lib/viewport";
import { Button, Chip, EmptyState, ErrorState, Field, Input, Loading, Money, Notice, ResponsiveDialog, SegmentedControl, Select, Textarea, When, useToast } from "../../ui";
import { ReasonDialog, RescheduleSheet, ScheduleFields, schedulePayload, scheduleFromTask, taskPath, useTaskCommand, type ScheduleValue } from "../tasks/TaskSheets";
import { usePeople } from "../tasks/usePeople";
import { isActive, isOverdue, reminderLabel, showsTokyo, typeLabel, STATUS_LABEL, type TaskView } from "../tasks/types";
import { isDepositGate, moveStage, reopenLead, updateLead, type MoveOutcome } from "./api";
import { BOARD_STAGES, ageText, pipelineLabel, stageLabel, subjectKey, type OpportunityDetail, type Stage } from "./types";
import "./sales.css";

/* ---------- Deposit gate: the API's reason + Record payment ---------- */
export function DepositGateDialog({ message, onClose }: { message: string | null; onClose: () => void }) {
  const mobile = useIsMobile();
  return (
    <ResponsiveDialog mobile={mobile} open={!!message} onClose={onClose} title="Deposit Paid comes from payment evidence" size="sm"
      footer={<><Button variant="primary" to="/finance?tab=matching">Record payment</Button><Button variant="ghost" onClick={onClose}>Not now</Button></>}>
      <Notice tone="blocked" lead="Blocked" role="alert">{message}</Notice>
      <p className="fs13 t3">Match the payment in Finance › Needs matching. The lead moves to Deposit Paid on its own and hands off to the vehicle's Sale tab or the import request.</p>
    </ResponsiveDialog>
  );
}

/** Shared outcome handling for stage moves (board, list, pane). Returns true when the move applied. */
export function useMoveOutcome() {
  const { toast } = useToast();
  const nav = useNavigate();
  const [gate, setGate] = useState<string | null>(null);
  const handle = useCallback((o: MoveOutcome, okMessage?: string): boolean => {
    if (o.kind === "ok") { if (okMessage) toast({ message: okMessage, tone: "ok" }); return true; }
    if (o.kind === "needs_review") {
      toast({ title: "Sent for approval", message: o.reasons[0] || "The owner reviews this first.", tone: "wait", actions: o.approval_id ? [{ label: "Review", primary: true, onClick: () => nav(`/approvals/${o.approval_id}`) }] : undefined });
      return false;
    }
    if (o.kind === "blocked") {
      if (isDepositGate(o)) setGate(o.message); else toast({ message: o.message, tone: "blocked" });
      return false;
    }
    toast({ message: o.message, tone: "blocked" });
    return false;
  }, [toast, nav]);
  return { gate, setGate, handle };
}

const TASK_TYPES: { value: "call" | "meeting" | "follow_up"; label: string }[] = [
  { value: "call", label: "Call" }, { value: "meeting", label: "Meeting" }, { value: "follow_up", label: "Follow-up" },
];

export interface LeadPaneProps {
  leadId: string;
  /** Called after any write so the board/list can refresh. */
  onChanged?: () => void;
  /** Start with the task form open for this type (from "No next step · add one"). */
  initialTaskType?: "call" | "meeting" | "follow_up" | null;
}

export function LeadPane({ leadId, onChanged, initialTaskType = null }: LeadPaneProps) {
  const { user } = useAuth();
  const runTask = useTaskCommand();
  const { gate, setGate, handle } = useMoveOutcome();
  const [tick, setTick] = useState(0);
  const q = useQuery<OpportunityDetail | null>((signal) => api.get<OpportunityDetail | null>(`/api/sales/opportunities/${encodeURIComponent(leadId)}`, { signal, tolerate: [404] }), [leadId, tick]);
  const reload = useCallback(() => { setTick((t) => t + 1); onChanged?.(); }, [onChanged]);
  const write = can(user, "sales.write");
  const taskWrite = can(user, "tasks.write");
  const people = usePeople(write);

  const [notes, setNotes] = useState("");
  const [notesDirty, setNotesDirty] = useState(false);
  const [savingNotes, setSavingNotes] = useState(false);
  const [lostOpen, setLostOpen] = useState(false);
  const [busyStage, setBusyStage] = useState<string | null>(null);
  const [tf, setTf] = useState<{ type: "call" | "meeting" | "follow_up"; sched: ScheduleValue; notes: string } | null>(null);
  const [tfBusy, setTfBusy] = useState(false);
  const [reschedule, setReschedule] = useState<TaskView | null>(null);
  const [cancelTask, setCancelTask] = useState<TaskView | null>(null);

  useEffect(() => {
    if (q.data) { setNotes(q.data.opportunity.notes || ""); setNotesDirty(false); }
  }, [q.data]);
  useEffect(() => {
    if (initialTaskType && q.data && !tf) setTf({ type: initialTaskType, sched: scheduleFromTask(null, user?.timezone), notes: "" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialTaskType, q.data]);

  const d = q.data;
  const o = d?.opportunity;
  const card = d?.card;
  const stage = (o?.stage || "new") as Stage;
  const isLost = stage === "lost";
  const isPaid = stage === "deposit_paid";
  const allTasks = useMemo(() => [...(d?.tasks.open || []), ...(d?.tasks.recent_closed || [])], [d]);

  const move = async (target: Stage) => {
    if (!o || target === stage) return;
    if (target === "lost") { setLostOpen(true); return; }
    setBusyStage(target);
    const out = await moveStage(o.id, target, { expected_version: o.version });
    setBusyStage(null);
    if (handle(out, `${card?.name || "Lead"} → ${stageLabel(target)}`)) reload();
  };
  const markLost = async (reason: string) => {
    if (!o) return false;
    const out = await moveStage(o.id, "lost", { reason, expected_version: o.version });
    const ok = handle(out, `${card?.name || "Lead"} parked as lost`);
    if (ok) reload();
    return ok;
  };
  const reopen = async () => {
    if (!o) return;
    setBusyStage("reopen");
    const out = await reopenLead(o.id, { stage: "new", expected_version: o.version });
    setBusyStage(null);
    if (handle(out, `${card?.name || "Lead"} reopened as New Lead`)) reload();
  };
  const saveNotes = async () => {
    if (!o) return;
    setSavingNotes(true);
    const out = await updateLead(o.id, { notes, expected_version: o.version });
    setSavingNotes(false);
    if (handle(out, "Notes saved")) { setNotesDirty(false); reload(); }
  };
  const setOwner = async (ownerId: string) => {
    if (!o) return;
    const out = await updateLead(o.id, ownerId ? { owner_user_id: ownerId, expected_version: o.version } : { unassign: true, expected_version: o.version });
    if (handle(out, ownerId ? "Owner updated" : "Owner cleared")) reload();
  };

  const openTaskForm = (type: "call" | "meeting" | "follow_up") => setTf({ type, sched: scheduleFromTask(null, user?.timezone), notes: "" });
  const saveTask = async (e: FormEvent) => {
    e.preventDefault();
    if (!tf || !o || !tf.sched.date) return;
    setTfBusy(true);
    const body: Record<string, unknown> = {
      title: `${TASK_TYPES.find((t) => t.value === tf.type)?.label} · ${card?.name || "lead"}`,
      type: tf.type, opportunity_id: o.id, contact_id: o.contact_id || undefined, vehicle_id: o.vehicle_id || undefined,
      owner_user_id: o.owner_user_id || user?.id || undefined, notes: tf.notes, ...schedulePayload(tf.sched),
    };
    const out = await runTask("/api/tasks", body, { okMessage: `${TASK_TYPES.find((t) => t.value === tf.type)?.label} added` });
    setTfBusy(false);
    if (out.result?.status === "ok") { setTf(null); reload(); }
  };
  const completeTask = async (t: TaskView) => {
    const out = await runTask(taskPath(t.id, "complete"), { expected_version: t.version }, { okMessage: `${typeLabel(t)} done` });
    if (out.result?.status === "ok") reload();
  };

  if (q.loading) return <div className="lp"><Loading label="Loading lead" rows={3} /></div>;
  if (q.error) return <div className="lp"><ErrorState error={q.error} onRetry={q.reload} /></div>;
  if (!d || !o || !card) return <div className="lp"><EmptyState title="Lead not found" body="It may have been merged or archived." /></div>;

  const contactLine = d.contact ? [d.contact.name, d.contact.primary_phone || d.contact.primary_email].filter(Boolean).join(" · ") : card.name;
  const budgetHidden = o.budget_amount === null || o.budget_amount === undefined;
  /* Ask AZKT about this lead — the Agents screen pins ?context=kind:id&label=. */
  const askHref = `/agents?context=${encodeURIComponent(`opportunity:${o.id}`)}&label=${encodeURIComponent(card.name || "this lead")}`;

  return (
    <div className="lp">
      <div>
        <div className="lp__name">{card.name}{card.company ? <span className="t3" style={{ fontWeight: 400 }}> · {card.company}</span> : null}</div>
        <div className="lp__sub">{pipelineLabel(o.pipeline)} · added {ageText(card)}{card.source ? ` · ${card.source}` : ""}</div>
      </div>

      <div>
        <div className="lp__label"><span>Stage</span><span className="t4" style={{ textTransform: "none", letterSpacing: 0 }}>{card.stage_age ? `in stage ${card.stage_age}` : ""}</span></div>
        <div className="lp__stages" role="group" aria-label="Stage">
          {[...BOARD_STAGES, { id: "lost" as Stage, label: "Lost" }].map((s) => {
            const on = s.id === stage;
            const disabledReason = on ? undefined : !write ? whyNot("sales.write") : isPaid ? "Deposit Paid is evidence-backed; refunds or disputes are handled in Finance." : undefined;
            return (
              <Chip key={s.id} size="sm" tone={on ? "act" : "neutral"} selected={on} disabled={on || !!disabledReason} disabledReason={disabledReason} aria-current={on ? "step" : undefined}
                onClick={() => void move(s.id)} title={s.id === "deposit_paid" && !on ? "Set from matched payment evidence" : undefined}>
                {busyStage === s.id ? "…" : s.label}
              </Chip>
            );
          })}
        </div>
        <div className="stack-sm" style={{ marginTop: 10 }}>
          {!isPaid && !isLost ? (
            <Button size="sm" variant="glass" onClick={() => void move("deposit_paid")} loading={busyStage === "deposit_paid"} disabled={!write} disabledReason={whyNot("sales.write")}>Mark deposit paid</Button>
          ) : null}
          {isPaid ? (
            <span className="fs13" style={{ color: "var(--ok)" }}>
              Deposit paid{o.deposit_confirmed_at ? <> · <When iso={o.deposit_confirmed_at} format="date" /></> : null} ·{" "}
              {d.conversion?.path ? <Link to={d.conversion.path}>continue in {d.conversion.kind === "sale" ? "the vehicle's Sale tab" : "the import request"}</Link> : o.vehicle_id ? <Link to={`/vehicles/${encodeURIComponent(o.vehicle_id)}?tab=sale`}>continue in the vehicle's Sale tab</Link> : "handed off"}
            </span>
          ) : null}
          {isLost ? <span className="fs13 t3">Lost{o.lost_at ? <> · <When iso={o.lost_at} format="date" /></> : null}{o.lost_reason ? ` · ${o.lost_reason}` : ""}</span> : null}
          <Button size="sm" variant="soft" to={askHref}>Ask about this lead</Button>
          <span className="fs12" style={{ color: d.deposit.state === "confirmed" ? "var(--ok)" : d.deposit.state === "partial" ? "var(--risk)" : d.deposit.state === "awaiting" ? "var(--wait)" : "var(--t4)" }}>{d.deposit.label}</span>
        </div>
      </div>

      <dl className="kv">
        <dt>Contact</dt><dd>{d.contact ? <Link to={`/contacts/${encodeURIComponent(d.contact.id)}`} className="wrap">{contactLine}</Link> : contactLine}</dd>
        <dt>{subjectKey(o.pipeline)}</dt><dd>{o.vehicle_id ? <Link to={`/vehicles/${encodeURIComponent(o.vehicle_id)}`} className="wrap">{card.subject}</Link> : card.subject}</dd>
        {!budgetHidden ? <><dt>Budget</dt><dd><Money amount={o.budget_amount} currency={o.budget_currency || "USD"} /></dd></> : null}
        <dt>Source</dt><dd>{o.source || <span className="not-recorded">Not recorded</span>}</dd>
        <dt>Owner</dt>
        <dd>
          {write && people.people.length ? (
            <select className="select" style={{ height: 34, fontSize: 13 }} value={o.owner_user_id || ""} onChange={(e) => void setOwner(e.target.value)} aria-label="Lead owner">
              <option value="">Unassigned</option>
              {people.people.map((p) => <option key={p.id} value={p.id}>{p.display_name}</option>)}
            </select>
          ) : (card.owner?.name || people.nameOf(o.owner_user_id))}
        </dd>
        {d.import_request ? <><dt>Import request</dt><dd><Link to={`/requests/${encodeURIComponent(d.import_request.id)}`} className="wrap">{d.import_request.title || d.import_request.id.slice(0, 8)}{d.import_request.status ? ` · ${d.import_request.status}` : ""}</Link></dd></> : null}
      </dl>

      <div>
        <div className="lp__label"><span>Notes</span>{notesDirty ? <Button size="xs" variant="primary" loading={savingNotes} onClick={saveNotes}>Save</Button> : null}</div>
        <Textarea value={notes} onChange={(e) => { setNotes(e.target.value); setNotesDirty(true); }} placeholder="What they want, what you promised…" rows={3} disabled={!write} title={!write ? whyNot("sales.write") : undefined} />
      </div>

      <div className="stack-sm">
        <div className="lp__label" style={{ marginBottom: 0 }}>
          <span>Tasks <span className="t4">· {d.tasks.open.length}</span></span>
          <span className="lp__adds" style={{ textTransform: "none", letterSpacing: 0 }}>
            {TASK_TYPES.map((t) => <Button key={t.value} size="xs" variant="soft" onClick={() => openTaskForm(t.value)} disabled={!taskWrite || isLost} disabledReason={isLost ? "Reopen the lead first." : whyNot("tasks.write")}>+ {t.label}</Button>)}
          </span>
        </div>
        {tf ? (
          <form className="lp__form" onSubmit={saveTask}>
            <div style={{ fontWeight: 500 }}>New {TASK_TYPES.find((t) => t.value === tf.type)?.label.toLowerCase()}</div>
            <SegmentedControl label="Type" size="sm" block value={tf.type} onChange={(v) => setTf({ ...tf, type: v })} options={TASK_TYPES} />
            <ScheduleFields value={tf.sched} onChange={(sched) => setTf({ ...tf, sched })} />
            <Field label="Notes">
              <Input value={tf.notes} onChange={(e) => setTf({ ...tf, notes: e.target.value })} placeholder="Notes (optional)" />
            </Field>
            <div className="row-wrap">
              <Button type="submit" size="sm" variant="primary" loading={tfBusy} disabled={!tf.sched.date} disabledReason="Pick a date first.">Add {TASK_TYPES.find((t) => t.value === tf.type)?.label.toLowerCase()}</Button>
              <Button size="sm" variant="ghost" onClick={() => setTf(null)}>Cancel</Button>
            </div>
          </form>
        ) : null}
        {allTasks.length === 0 && !tf ? <span className="fs13 t4">No tasks yet. Add a call, meeting or follow-up so this lead has a next step.</span> : null}
        {allTasks.map((t) => {
          const active = isActive(t);
          const over = isOverdue(t);
          return (
            <div key={t.id} className={["lp__task", over ? "lp__task--overdue" : ""].filter(Boolean).join(" ")}>
              <div className="lp__task-top">
                <span style={{ fontWeight: 500 }}>{typeLabel(t)}{t.type === "operational" ? ` · ${t.title}` : ""}</span>
                <span className={["lp__task-when", over ? "lp__task-when--overdue" : ""].filter(Boolean).join(" ")}>{over ? "Overdue · " : ""}{t.due_at ? <When iso={t.due_at} tz={TZ.phoenix} withTokyo={showsTokyo(t)} /> : "Not scheduled"}</span>
              </div>
              {t.notes ? <div className="fs13 t2">{t.notes}</div> : null}
              <div className="lp__task-foot">
                <span className="fs12 t4">{reminderLabel(t)}</span>
                {active ? (
                  <span className="row" style={{ gap: 4 }}>
                    <Button size="xs" variant="primary" onClick={() => void completeTask(t)} disabled={!taskWrite || t.status === "awaiting_verification"} disabledReason={t.status === "awaiting_verification" ? "Waiting for the owner to verify." : whyNot("tasks.write")}>Done</Button>
                    <Button size="xs" variant="soft" onClick={() => setReschedule(t)} disabled={!taskWrite} disabledReason={whyNot("tasks.write")}>Reschedule</Button>
                    <Button size="xs" variant="ghost" onClick={() => setCancelTask(t)} disabled={!taskWrite} disabledReason={whyNot("tasks.write")}>Cancel</Button>
                  </span>
                ) : (
                  <span className="fs12" style={{ color: t.status === "completed" ? "var(--ok)" : "var(--t4)" }}>{STATUS_LABEL[t.status] || t.status}</span>
                )}
              </div>
            </div>
          );
        })}
      </div>

      <div className="lp__links">
        {d.contact ? <Link to={`/contacts/${encodeURIComponent(d.contact.id)}`}>Open contact</Link> : null}
        {o.vehicle_id ? <Link to={`/vehicles/${encodeURIComponent(o.vehicle_id)}`}>Open vehicle{d.vehicle?.stock_no ? ` · ${d.vehicle.stock_no}` : ""}</Link> : null}
        {(o.conversation_ids || []).length ? <Link to={`/inbox/${encodeURIComponent((o.conversation_ids as string[])[0])}`}>Open thread</Link> : null}
        {!d.contact && !o.vehicle_id ? <span className="t4">No linked records yet.</span> : null}
      </div>

      {isLost ? (
        <button type="button" className="linklike fs13" style={{ color: "var(--t3)", alignSelf: "flex-start" }} onClick={reopen} disabled={!write} title={!write ? whyNot("sales.write") : undefined}>Reopen as New Lead</button>
      ) : !isPaid ? (
        <button type="button" className="linklike fs13" style={{ color: "var(--t3)", alignSelf: "flex-start" }} onClick={() => setLostOpen(true)} disabled={!write} title={!write ? whyNot("sales.write") : undefined}>Mark lost / not moving forward</button>
      ) : null}

      <DepositGateDialog message={gate} onClose={() => setGate(null)} />
      <ReasonDialog open={lostOpen} onClose={() => setLostOpen(false)} title="Mark lost / not moving forward" description={card.name} label="Why" placeholder="e.g. bought elsewhere, no reply for 3 weeks" confirmLabel="Mark lost" required tone="danger" onConfirm={markLost} />
      <RescheduleSheet task={reschedule} open={!!reschedule} onClose={() => setReschedule(null)} onSaved={() => reload()} />
      <ReasonDialog open={!!cancelTask} onClose={() => setCancelTask(null)} title="Cancel task" description={cancelTask?.title} label="Why (optional)" confirmLabel="Cancel task" tone="danger"
        onConfirm={async (reason) => { const t = cancelTask; if (!t) return false; const out = await runTask(taskPath(t.id, "cancel"), { reason: reason || undefined, expected_version: t.version }, { okMessage: "Cancelled · reminders removed" }); if (out.result?.status === "ok") reload(); return !out.error; }} />
    </div>
  );
}

export default LeadPane;
