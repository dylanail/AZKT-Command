/* Data helpers for Home "Today", the tasks summary chips and the mobile reminder banner.
   GET /api/tasks/summary?tz= → counts; GET /api/tasks?view=&bucket= → due rows. Reused by Home later. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, command } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { TZ } from "../../lib/format";
import { defaultTz, isSnoozed, unwrapTask, withinNextHours, type TaskListResp, type TaskView } from "./types";

export interface TasksSummary {
  overdue: number;
  today: number;
  unassigned: number;
  blocked: number;
  awaiting_verification: number;
  as_of?: string;
  timezone?: string;
}

interface State<T> { data: T | null; loading: boolean; error: unknown; }

/** Counts for chips / Home. Refreshes when the tab becomes visible and every `refreshMs` (default 60s). */
export function useTasksSummary(opts: { refreshMs?: number; enabled?: boolean } = {}) {
  const { user } = useAuth();
  const enabled = opts.enabled ?? true;
  const tz = defaultTz(user?.timezone);
  const [state, setState] = useState<State<TasksSummary>>({ data: null, loading: enabled, error: null });
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!enabled) return;
    const ctrl = new AbortController();
    api.get<TasksSummary | null>(`/api/tasks/summary?tz=${encodeURIComponent(tz)}`, { signal: ctrl.signal, tolerate: [404, 501] })
      .then((d) => { if (!ctrl.signal.aborted) setState({ data: d, loading: false, error: null }); })
      .catch((e) => { if (!ctrl.signal.aborted) setState((s) => ({ data: s.data, loading: false, error: e })); });
    return () => ctrl.abort();
  }, [enabled, tz, tick]);

  useEffect(() => {
    if (!enabled) return;
    const ms = opts.refreshMs ?? 60_000;
    const h = window.setInterval(() => { if (document.visibilityState === "visible") reload(); }, ms);
    const onVis = () => { if (document.visibilityState === "visible") reload(); };
    document.addEventListener("visibilitychange", onVis);
    return () => { window.clearInterval(h); document.removeEventListener("visibilitychange", onVis); };
  }, [enabled, opts.refreshMs, reload]);

  return { summary: state.data, loading: state.loading, error: state.error, reload, tz };
}

export interface DueTask extends TaskView { kind: "overdue" | "due"; }

/**
 * Tasks that need a nudge right now: overdue, plus anything due within `withinMinutes` (default 15).
 * Snoozed tasks stay quiet until their snooze ends. Drives the reminder banner and Home "Today".
 */
export function useDueTasks(opts: { view?: "my" | "all"; withinMinutes?: number; enabled?: boolean; refreshMs?: number } = {}) {
  const enabled = opts.enabled ?? true;
  const view = opts.view ?? "my";
  const within = opts.withinMinutes ?? 15;
  const [rows, setRows] = useState<TaskView[]>([]);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState<unknown>(null);
  const [tick, setTick] = useState(0);
  const dismissed = useRef(new Set<string>());
  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!enabled) return;
    const ctrl = new AbortController();
    const q = (bucket: string) => api.get<TaskListResp | null>(`/api/tasks?view=${view}&bucket=${bucket}&limit=100`, { signal: ctrl.signal, tolerate: [404, 501] });
    Promise.all([q("overdue"), q("upcoming")])
      .then(([over, up]) => {
        if (ctrl.signal.aborted) return;
        const seen = new Set<string>();
        const out: TaskView[] = [];
        for (const t of over?.items || []) if (!seen.has(t.id)) { seen.add(t.id); out.push(t); }
        for (const t of up?.items || []) if (!seen.has(t.id) && withinNextHours(t, within / 60)) { seen.add(t.id); out.push(t); }
        setRows(out);
        setLoading(false);
        setError(null);
      })
      .catch((e) => { if (!ctrl.signal.aborted) { setError(e); setLoading(false); } });
    return () => ctrl.abort();
  }, [enabled, view, within, tick]);

  useEffect(() => {
    if (!enabled) return;
    const h = window.setInterval(() => { if (document.visibilityState === "visible") reload(); }, opts.refreshMs ?? 60_000);
    return () => window.clearInterval(h);
  }, [enabled, opts.refreshMs, reload]);

  const now = Date.now();
  const tasks = useMemo<DueTask[]>(() => rows
    .filter((t) => !dismissed.current.has(t.id) && !isSnoozed(t))
    .map((t) => ({ ...t, kind: t.due_at && new Date(t.due_at).getTime() < now ? "overdue" : "due" })), [rows, now]);

  const dismiss = useCallback((id: string) => { dismissed.current.add(id); setRows((xs) => xs.filter((x) => x.id !== id)); }, []);
  const complete = useCallback(async (id: string) => {
    const res = await command<{ task?: TaskView }>(`/api/tasks/${encodeURIComponent(id)}/complete`, {});
    setRows((xs) => xs.filter((x) => x.id !== id));
    return unwrapTask(res);
  }, []);
  const snooze = useCallback(async (id: string, minutes = 10) => {
    const res = await command<{ task?: TaskView }>(`/api/tasks/${encodeURIComponent(id)}/snooze`, { minutes });
    const t = unwrapTask(res);
    setRows((xs) => xs.map((x) => (x.id === id ? { ...x, ...(t || {}), snoozed_until: t?.snoozed_until || new Date(Date.now() + minutes * 60000).toISOString() } : x)));
    return t;
  }, []);

  return { tasks, loading, error, reload, dismiss, complete, snooze };
}

/** Today's window in a zone, for Home "Today" queries (start inclusive, end exclusive, ISO). */
export function todayRange(tz: string = TZ.phoenix, now: Date = new Date()): { from: string; to: string } {
  const p = new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(now);
  const [y, m, d] = p.split("-").map(Number);
  const next = new Date(Date.UTC(y, m - 1, d + 1));
  const pad = (n: number) => String(n).padStart(2, "0");
  return { from: `${y}-${pad(m)}-${pad(d)}`, to: `${next.getUTCFullYear()}-${pad(next.getUTCMonth() + 1)}-${pad(next.getUTCDate())}` };
}
