/* Tasks: My | All (scope-aware); Overdue / Next 24 hours / Upcoming / Unassigned / Blocked / Waiting /
   Awaiting verification / Completed from GET /api/tasks?view=&bucket=; summary chips from GET /api/tasks/summary;
   schedule view from GET /api/tasks/schedule grouped by local day (Tokyo alongside Phoenix when jp).
   One task can sit in several buckets without duplicating rows.
   Four views live side by side in the URL: Tasks (default) · Cases (?secondary=cases) ·
   Promises (?secondary=promises) · Schedule (?layout=schedule, kept as it was). */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useAuth } from "../../lib/auth";
import { api } from "../../lib/api";
import { can, isEmployeeRole, whyNot } from "../../lib/perms";
import { useQuery } from "../../lib/useQuery";
import { TZ, formatWhen, pluralize } from "../../lib/format";
import { Button, Chip, EmptyState, ErrorState, GlassPanel, Loading, PageHeader, SegmentedControl, PlusIcon } from "../../ui";
import { NewTaskDialog, ReasonDialog, RescheduleSheet, AssignSheet, taskPath, useTaskCommand, useTaskDialogs } from "./TaskSheets";
import { TaskRow, type TaskAbout } from "./TaskRow";
import { useNames } from "./useNames";
import { usePeople } from "./usePeople";
import { useTasksSummary, todayRange, type TasksSummary } from "./useTasksSummary";
import { CasesView, PromisesView } from "./SecondaryViews";
import { defaultTz, isOverdue, withinNextHours, type CaseListResp, type PromiseListResp, type TaskListResp, type TaskView } from "./types";
import "./tasks.css";

/** The four things the Tasks screen can show; "schedule" is the old ?layout=schedule. */
type Secondary = "tasks" | "cases" | "promises" | "schedule";

type BucketId = "overdue" | "next24" | "upcoming" | "unassigned" | "blocked" | "waiting" | "awaiting_verification" | "completed";
type ApiBucket = "overdue" | "upcoming" | "unassigned" | "blocked" | "waiting" | "awaiting_verification" | "completed";
interface BucketDef { id: BucketId; label: string; api: ApiBucket; summary?: keyof TasksSummary; empty: string; }

const BUCKETS: BucketDef[] = [
  { id: "overdue", label: "Overdue", api: "overdue", summary: "overdue", empty: "Nothing overdue" },
  { id: "next24", label: "Next 24 hours", api: "upcoming", summary: "today", empty: "Nothing due in the next day" },
  { id: "upcoming", label: "Upcoming", api: "upcoming", empty: "Nothing scheduled" },
  { id: "unassigned", label: "Unassigned", api: "unassigned", summary: "unassigned", empty: "Everything has an owner" },
  { id: "blocked", label: "Blocked", api: "blocked", summary: "blocked", empty: "Nothing is blocked" },
  { id: "waiting", label: "Waiting", api: "waiting", empty: "Nothing waiting on someone else" },
  { id: "awaiting_verification", label: "Awaiting verification", api: "awaiting_verification", summary: "awaiting_verification", empty: "Nothing to verify" },
  { id: "completed", label: "Completed", api: "completed", empty: "Nothing finished yet" },
];
const isBucket = (s: string | null): s is BucketId => !!s && BUCKETS.some((b) => b.id === s);

interface ListData { main: TaskView[]; overdue: TaskView[]; missing: boolean; }
interface ScheduleResp { items: TaskView[]; total: number; from: string; to: string; timezone: string; }

function addDays(iso: string, n: number): string {
  const [y, m, d] = iso.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d + n));
  const pad = (x: number) => String(x).padStart(2, "0");
  return `${dt.getUTCFullYear()}-${pad(dt.getUTCMonth() + 1)}-${pad(dt.getUTCDate())}`;
}
function dayLabel(iso: string, tz: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  const anchor = new Date(Date.UTC(y, m - 1, d, 12));
  const today = todayRange(tz).from;
  const base = new Intl.DateTimeFormat("en-US", { timeZone: "UTC", weekday: "short", month: "short", day: "numeric" }).format(anchor);
  return iso === today ? `Today · ${base}` : iso === addDays(today, 1) ? `Tomorrow · ${base}` : base;
}

export default function Tasks() {
  const { user } = useAuth();
  const [params, setParams] = useSearchParams();
  const q = (params.get("q") || "").trim().toLowerCase();
  const employee = isEmployeeRole(user?.role);
  const canAll = user?.scope === "all" && !employee;
  const view: "my" | "all" = params.get("view") === "my" ? "my" : params.get("view") === "all" && canAll ? "all" : canAll ? "all" : "my";
  const rawSecondary = params.get("secondary");
  const secondary: Secondary = rawSecondary === "cases" ? "cases"
    : rawSecondary === "promises" ? "promises"
    : params.get("layout") === "schedule" ? "schedule" : "tasks";
  // Cases and promises share one "include closed" switch; it only applies while one of them is open.
  const closedStatus: "open" | "all" = params.get("status") === "all" ? "all" : "open";
  const bucket: BucketId = isBucket(params.get("bucket")) ? (params.get("bucket") as BucketId) : "upcoming";
  const jp = params.get("jp") === "1";
  const tz = defaultTz(user?.timezone);
  const from = params.get("from") || todayRange(tz).from;
  const to = addDays(from, 7);
  const [tick, setTick] = useState(0);
  const reloadAll = useCallback(() => setTick((t) => t + 1), []);
  const setParam = useCallback((k: string, v: string | null) => {
    const p = new URLSearchParams(params);
    if (v) p.set(k, v); else p.delete(k);
    setParams(p, { replace: true });
  }, [params, setParams]);
  const setSecondary = useCallback((v: Secondary) => {
    const p = new URLSearchParams(params);
    p.delete("secondary");
    p.delete("layout");
    if (v === "cases" || v === "promises") p.set("secondary", v);
    else {
      p.delete("status");                       // "include closed" belongs to those two views only
      if (v === "schedule") p.set("layout", "schedule");
    }
    setParams(p, { replace: true });
  }, [params, setParams]);

  const summary = useTasksSummary();
  const summaryReload = summary.reload;
  useEffect(() => { if (tick > 0) summaryReload(); /* refresh counts after actions */ }, [tick, summaryReload]);
  const def = BUCKETS.find((b) => b.id === bucket) as BucketDef;

  const list = useQuery<ListData>(async (signal) => {
    if (secondary !== "tasks") return { main: [], overdue: [], missing: false };
    const get = (b: ApiBucket) => api.get<TaskListResp | null>(`/api/tasks?view=${view}&bucket=${b}&limit=200${jp ? "&jp=1" : ""}`, { signal, tolerate: [404, 501] });
    const [main, over] = await Promise.all([get(def.api), bucket === "upcoming" ? get("overdue") : Promise.resolve(null)]);
    return { main: main?.items || [], overdue: over?.items || [], missing: main === null };
  }, [secondary, view, bucket, jp, tick]);

  const sched = useQuery<ScheduleResp | null>(async (signal) => {
    if (secondary !== "schedule") return null;
    return api.get<ScheduleResp | null>(`/api/tasks/schedule?from=${from}&to=${to}&view=${view}&tz=${encodeURIComponent(tz)}${jp ? "&jp=1" : ""}`, { signal, tolerate: [404, 501] });
  }, [secondary, view, from, to, tz, jp, tick]);

  // Cases and promises load whatever the open view is, so their counts on the switch are real numbers.
  const cases = useQuery<CaseListResp | null>(
    (signal) => api.get<CaseListResp | null>(`/api/tasks/cases?status=${closedStatus}&limit=200`, { signal, tolerate: [403, 404, 501] }),
    [closedStatus, tick],
  );
  const promises = useQuery<PromiseListResp | null>(
    (signal) => api.get<PromiseListResp | null>(`/api/tasks/promises?status=${closedStatus}&limit=200`, { signal, tolerate: [403, 404, 501] }),
    [closedStatus, tick],
  );

  const allRows = useMemo(
    () => [...(list.data?.main || []), ...(list.data?.overdue || []), ...(sched.data?.items || []), ...(cases.data?.items || []), ...(promises.data?.items || [])],
    [list.data, sched.data, cases.data, promises.data],
  );
  const names = useNames(allRows, { contacts: can(user, "contacts.read"), opportunities: can(user, "sales.read"), vehicles: true });
  const people = usePeople(!employee);
  const dialogs = useTaskDialogs();
  const run = useTaskCommand();
  const [newOpen, setNewOpen] = useState(params.get("new") === "1");
  useEffect(() => { if (params.get("new") === "1") setNewOpen(true); }, [params]);

  const matchesQ = (t: TaskView) => !q || `${t.title} ${t.notes || ""} ${names.leadName(t.opportunity_id)} ${names.vehicleName(t.vehicle_id)} ${names.contactName(t.contact_id)}`.toLowerCase().includes(q);
  const aboutOf = (t: TaskView): TaskAbout | null => {
    if (t.opportunity_id) return { label: names.leadName(t.opportunity_id), to: `/sales?lead=${encodeURIComponent(t.opportunity_id)}`, extra: names.leadSubject(t.opportunity_id) || undefined };
    if (t.vehicle_id) return { label: names.vehicleName(t.vehicle_id), to: `/vehicles/${encodeURIComponent(t.vehicle_id)}` };
    if (t.contact_id) return { label: names.contactName(t.contact_id), to: `/contacts/${encodeURIComponent(t.contact_id)}` };
    return null;
  };
  const onChanged = () => reloadAll();

  // Sections for the list view.
  const sections = useMemo(() => {
    const d = list.data;
    if (!d) return [] as { key: string; label: string; rows: TaskView[]; empty: string }[];
    const main = d.main.filter(matchesQ);
    if (bucket === "upcoming") {
      const over = d.overdue.filter(matchesQ);
      return [
        { key: "over", label: "Overdue", rows: over, empty: "Nothing overdue" },
        { key: "next24", label: "Next 24 hours", rows: main.filter((t) => withinNextHours(t, 24)), empty: "Nothing due in the next day" },
        { key: "later", label: "Upcoming", rows: main.filter((t) => !withinNextHours(t, 24)), empty: "Nothing scheduled further out" },
      ];
    }
    if (bucket === "next24") return [{ key: "next24", label: "Next 24 hours", rows: main.filter((t) => withinNextHours(t, 24)), empty: def.empty }];
    return [{ key: bucket, label: def.label, rows: main, empty: def.empty }];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [list.data, bucket, q, names]);

  const s = summary.summary;
  const title = employee ? "My tasks" : view === "my" ? "My tasks" : "Tasks";
  const subtitle = secondary === "cases"
    ? "Work that is waiting on someone else, each with what it waits on and when it is checked again."
    : secondary === "promises"
    ? "What has been promised to people, and when each one is due."
    : q ? `Search: "${q}"` : s
    ? [s.overdue ? `${pluralize(s.overdue, "overdue")}` : null, `${s.today} due today`, s.blocked ? `${s.blocked} blocked` : null, s.awaiting_verification ? `${s.awaiting_verification} awaiting verification` : null].filter(Boolean).join(" · ")
    : employee ? "Complete with evidence; the owner verifies." : "Shop tasks, sales calls and verification in one list. Verification stays with the owner.";

  const visibleBuckets = BUCKETS.filter((b) => !(b.id === "unassigned" && user?.scope === "assigned"));

  return (
    <div className="page page-wide">
      <PageHeader
        title={title}
        subtitle={subtitle}
        actions={
          <Button variant="primary" iconLeft={<PlusIcon />} onClick={() => setNewOpen(true)} disabled={!can(user, "tasks.write")} disabledReason={whyNot("tasks.write")}>New task</Button>
        }
      >
        <div className="tk-toolbar">
          <div className="row-wrap">
            {canAll ? (
              <SegmentedControl<"my" | "all"> label="Whose tasks" size="sm" value={view} onChange={(v) => setParam("view", v)} options={[{ value: "my", label: "My" }, { value: "all", label: "All" }]} />
            ) : null}
            <SegmentedControl<Secondary>
              label="What to show"
              size="sm"
              value={secondary}
              onChange={setSecondary}
              options={[
                { value: "tasks", label: "Tasks" },
                { value: "cases", label: "Cases", count: cases.data?.total },
                { value: "promises", label: "Promises", count: promises.data?.total },
                { value: "schedule", label: "Schedule" },
              ]}
            />
          </div>
          <div className="row-wrap">
            {secondary === "cases" || secondary === "promises" ? (
              <Chip size="sm" tone={closedStatus === "all" ? "act" : "neutral"} selected={closedStatus === "all"}
                onClick={() => setParam("status", closedStatus === "all" ? null : "all")}
                title="Also show the ones that are finished or cancelled">Include closed</Chip>
            ) : null}
            <Chip size="sm" tone={jp ? "act" : "neutral"} selected={jp} onClick={() => setParam("jp", jp ? null : "1")} title="Also show Tokyo time">Tokyo time</Chip>
          </div>
        </div>
        {secondary === "tasks" ? (
          <div className="tk-chips" role="group" aria-label="Task buckets">
            {visibleBuckets.map((b) => (
              <Chip key={b.id} size="sm" tone={bucket === b.id ? "act" : "neutral"} selected={bucket === b.id} onClick={() => setParam("bucket", b.id === "upcoming" ? null : b.id)} count={b.summary && s ? s[b.summary] as number : undefined}>{b.label}</Chip>
            ))}
          </div>
        ) : secondary === "schedule" ? (
          <div className="tk-toolbar">
            <div className="row-wrap">
              <Button size="sm" variant="soft" onClick={() => setParam("from", addDays(from, -7))}>‹ Previous week</Button>
              <Button size="sm" variant="soft" onClick={() => setParam("from", null)}>This week</Button>
              <Button size="sm" variant="soft" onClick={() => setParam("from", addDays(from, 7))}>Next week ›</Button>
            </div>
            <span className="fs13 t3 tnum">{formatWhen(new Date(`${from}T12:00:00Z`), { tz: "UTC", style: "date" })} – {formatWhen(new Date(`${addDays(from, 6)}T12:00:00Z`), { tz: "UTC", style: "date" })} · days in {tz === TZ.tokyo ? "Tokyo" : "Phoenix"} time</span>
          </div>
        ) : null}
      </PageHeader>

      {summary.error && !s ? <ErrorState error={summary.error} onRetry={summary.reload} title="Couldn't load the summary" /> : null}

      {secondary === "cases" ? (
        <CasesView data={cases.data} loading={cases.loading} error={cases.error} reload={cases.reload}
          names={names} tokyo={jp} showClosed={closedStatus === "all"} />
      ) : secondary === "promises" ? (
        <PromisesView data={promises.data} loading={promises.loading} error={promises.error} reload={promises.reload}
          names={names} tokyo={jp} showClosed={closedStatus === "all"} />
      ) : secondary === "tasks" ? (
        list.loading ? <GlassPanel clip><Loading label="Loading tasks" rows={4} /></GlassPanel>
        : list.error ? <ErrorState error={list.error} onRetry={list.reload} />
        : list.data?.missing ? <GlassPanel clip><EmptyState title="Tasks aren't connected yet" body="This list fills in once the tasks API is live." /></GlassPanel>
        : (
          <div className="stack">
            {sections.map((sec) => (
              <section key={sec.key} className="tk-group">
                <div className="section-title"><h2>{sec.label} <span className="count">{sec.rows.length}</span></h2></div>
                <GlassPanel>
                  {sec.rows.length === 0 ? <EmptyState title={q ? "No tasks match" : sec.empty} /> : sec.rows.map((t) => (
                    <TaskRow key={t.id} task={t} about={aboutOf(t)} ownerName={people.nameOf(t.owner_user_id)} showOwner={!employee} tokyo={jp} dialogs={dialogs} onChanged={onChanged} />
                  ))}
                </GlassPanel>
              </section>
            ))}
          </div>
        )
      ) : (
        sched.loading ? <GlassPanel clip><Loading label="Loading schedule" rows={4} /></GlassPanel>
        : sched.error ? <ErrorState error={sched.error} onRetry={sched.reload} />
        : sched.data === null ? <GlassPanel clip><EmptyState title="Schedule isn't connected yet" body="Scheduled tasks group by day here once the tasks API is live." /></GlassPanel>
        : (
          <ScheduleView data={sched.data} tz={tz} jp={jp} matches={matchesQ} aboutOf={aboutOf} ownerName={people.nameOf} showOwner={!employee} dialogs={dialogs} onChanged={onChanged} />
        )
      )}

      <NewTaskDialog open={newOpen} onClose={() => { setNewOpen(false); if (params.get("new")) setParam("new", null); }} onCreated={() => reloadAll()} />
      <RescheduleSheet task={dialogs.reschedule} open={!!dialogs.reschedule} onClose={() => dialogs.setReschedule(null)} onSaved={() => reloadAll()} />
      <AssignSheet task={dialogs.assign} open={!!dialogs.assign} onClose={() => dialogs.setAssign(null)} onSaved={() => reloadAll()} />
      <ReasonDialog open={!!dialogs.cancel} onClose={() => dialogs.setCancel(null)} title="Cancel task" description={dialogs.cancel?.title} label="Why (optional)" placeholder="e.g. no longer needed" confirmLabel="Cancel task" tone="danger"
        onConfirm={async (reason) => { const t = dialogs.cancel; if (!t) return false; const out = await run(taskPath(t.id, "cancel"), { reason: reason || undefined, expected_version: t.version }, { okMessage: "Cancelled · reminders removed" }); if (out.result?.status === "ok") reloadAll(); return !out.error; }} />
      <ReasonDialog open={!!dialogs.reject} onClose={() => dialogs.setReject(null)} title="Reject evidence" description={dialogs.reject?.title} label="What's missing or wrong" placeholder="e.g. photo doesn't show the installed part" confirmLabel="Reject and reopen" required tone="danger"
        onConfirm={async (reason) => { const t = dialogs.reject; if (!t) return false; const out = await run(taskPath(t.id, "reject"), { reason, expected_version: t.version }, { okMessage: "Reopened with your reason" }); if (out.result?.status === "ok") reloadAll(); return !out.error; }} />
    </div>
  );
}

function ScheduleView({ data, tz, jp, matches, aboutOf, ownerName, showOwner, dialogs, onChanged }: {
  data: ScheduleResp; tz: string; jp: boolean; matches: (t: TaskView) => boolean; aboutOf: (t: TaskView) => TaskAbout | null;
  ownerName: (id: string | null | undefined) => string; showOwner: boolean; dialogs: ReturnType<typeof useTaskDialogs>; onChanged: () => void;
}) {
  const days = useMemo(() => {
    const map = new Map<string, TaskView[]>();
    for (const t of data.items.filter(matches)) {
      const key = t.day || "undated";
      if (!map.has(key)) map.set(key, []);
      (map.get(key) as TaskView[]).push(t);
    }
    return Array.from(map.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [data, matches]);
  if (!days.length) return <GlassPanel clip><EmptyState title="Nothing scheduled this week" body="Calls, meetings and dated tasks appear by day." /></GlassPanel>;
  return (
    <div className="stack">
      {days.map(([day, rows]) => (
        <section key={day} className="tk-day">
          <div className="tk-day__head">
            <h2>{day === "undated" ? "No date" : dayLabel(day, tz)} <span className="count">{rows.length}</span></h2>
            {jp && day !== "undated" && (rows[0]?.due_at || rows[0]?.start_at) ? <span className="tk-day__tokyo">Tokyo · {formatWhen(rows[0].start_at || rows[0].due_at, { tz: TZ.tokyo, style: "long" })}</span> : null}
          </div>
          <GlassPanel>
            {rows.map((t) => (
              <TaskRow key={t.id} task={t} about={aboutOf(t)} ownerName={ownerName(t.owner_user_id)} showOwner={showOwner} tokyo={jp} dialogs={dialogs} onChanged={onChanged} />
            ))}
          </GlassPanel>
          {rows.some((t) => isOverdue(t)) ? <span className="fs12" style={{ color: "var(--blocked)", padding: "0 4px" }}>Some of these are overdue.</span> : null}
        </section>
      ))}
    </div>
  );
}
