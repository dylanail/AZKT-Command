/* Notification bell. One source for the bell, Home and the mobile sheet:
   GET /api/notifications (backend/app/services/notifications_feed.py) → {high, today, later, badge, status}.
   Badge = high + today; red when anything is high, otherwise the "wait" blue.
   Acknowledge / Snooze / Dismiss POST to /api/notifications/{id}/{action} — only stored notifications
   can be acted on there, so task and approval rows say so instead of offering a button that would 404.
   Reads poll every 60 s while the tab is visible and refetch on focus. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { useCommand } from "../lib/useCommand";
import { entityHref } from "../lib/links";
import { useIsMobile } from "../lib/viewport";
import { BellIcon, Button, Chip, IconButton, Input, Loading, Sheet, When } from "../ui";
import "../styles/notify.css";

export type NotifTier = "high" | "today" | "later";

/** One row of the feed (notifications_feed._item). */
export interface NotificationItem {
  id: string;
  kind: string;
  urgency: NotifTier;
  title: string;
  body: string;
  group_key: string;
  entity_kind: string | null;
  entity_id: string | null;
  deep_link: string | null;
  when: string | null;
  /** "notification" rows are stored and can be acknowledged/snoozed/dismissed; others are derived. */
  source: string;
  state: string;
  occurrences: number;
  /* aliases kept for callers written against the earlier placeholder shape */
  tier: NotifTier;
  at: string | null;
  href: string | null;
}

export interface NotificationStatus {
  all_clear: boolean;
  stale_connections: string[];
  message: string;
  sources_complete: boolean;
}
export interface NotificationFeed {
  high: NotificationItem[];
  today: NotificationItem[];
  later: NotificationItem[];
  badge: { count: number; color: string; high: number; today: number };
  status: NotificationStatus;
  generated_at: string | null;
}

interface RawFeed {
  high?: unknown[]; today?: unknown[]; later?: unknown[];
  badge?: Partial<NotificationFeed["badge"]>;
  status?: Partial<NotificationStatus>;
  generated_at?: string | null;
}

const TIERS: NotifTier[] = ["high", "today", "later"];
const GROUPS: { tier: NotifTier; label: string; color: string }[] = [
  { tier: "high", label: "Needs attention", color: "var(--blocked)" },
  { tier: "today", label: "Today", color: "var(--wait)" },
  { tier: "later", label: "Later", color: "var(--t3)" },
];

/** Plain-language name for the feed's `kind`. Unknown kinds show their own words, tidied. */
const KIND_LABELS: Record<string, string> = {
  task_blocked: "Blocked", task_overdue: "Overdue", task_due: "Task", task_waiting: "Waiting",
  approval: "Approval", connection_issue: "Connection", deposit_confirmed: "Deposit", case_update: "Case",
};
function kindLabel(kind: string): string {
  if (KIND_LABELS[kind]) return KIND_LABELS[kind];
  const t = (kind || "").replace(/[_.-]+/g, " ").trim();
  return t ? t.charAt(0).toUpperCase() + t.slice(1) : "Update";
}

function one(raw: unknown, tier: NotifTier): NotificationItem | null {
  const x = raw as Record<string, unknown> | null;
  if (!x || typeof x.id !== "string") return null;
  const urgency = TIERS.includes(x.urgency as NotifTier) ? (x.urgency as NotifTier) : tier;
  const when = typeof x.when === "string" ? x.when : null;
  const deep = typeof x.deep_link === "string" && x.deep_link ? x.deep_link : null;
  return {
    id: x.id,
    kind: typeof x.kind === "string" ? x.kind : "update",
    urgency,
    title: typeof x.title === "string" && x.title ? x.title : "Notification",
    body: typeof x.body === "string" ? x.body : "",
    group_key: typeof x.group_key === "string" ? x.group_key : x.id,
    entity_kind: typeof x.entity_kind === "string" ? x.entity_kind : null,
    entity_id: typeof x.entity_id === "string" ? x.entity_id : null,
    deep_link: deep,
    when,
    source: typeof x.source === "string" ? x.source : "derived",
    state: typeof x.state === "string" ? x.state : "unread",
    occurrences: typeof x.occurrences === "number" && x.occurrences > 0 ? x.occurrences : 1,
    tier: urgency,
    at: when,
    href: deep,
  };
}

function normalise(raw: RawFeed | null): NotificationFeed | null {
  if (!raw || typeof raw !== "object") return null;
  const bucket = (list: unknown[] | undefined, tier: NotifTier) =>
    (Array.isArray(list) ? list : []).map((r) => one(r, tier)).filter((i): i is NotificationItem => i !== null);
  const high = bucket(raw.high, "high");
  const today = bucket(raw.today, "today");
  const later = bucket(raw.later, "later");
  const b = raw.badge || {};
  const s = raw.status || {};
  return {
    high, today, later,
    badge: {
      count: typeof b.count === "number" ? b.count : high.length + today.length,
      color: typeof b.color === "string" ? b.color : high.length ? "red" : "neutral",
      high: typeof b.high === "number" ? b.high : high.length,
      today: typeof b.today === "number" ? b.today : today.length,
    },
    status: {
      all_clear: s.all_clear === true,
      stale_connections: Array.isArray(s.stale_connections) ? s.stale_connections.filter((v): v is string => typeof v === "string") : [],
      message: typeof s.message === "string" ? s.message : "",
      sources_complete: s.sources_complete !== false,
    },
    generated_at: typeof raw.generated_at === "string" ? raw.generated_at : null,
  };
}

/** The server builds absolute links from PUBLIC_ORIGIN; keep same-origin ones inside the app. */
export function routeFor(n: NotificationItem): string | null {
  const raw = n.deep_link;
  if (raw) {
    if (raw.startsWith("/")) return raw;
    try {
      const u = new URL(raw);
      if (u.origin === window.location.origin) return `${u.pathname}${u.search}${u.hash}`;
    } catch { /* not a URL we can use; fall through to the record route */ }
  }
  return entityHref(n.entity_kind, n.entity_id);
}

/**
 * The feed. Polls every `pollMs` while the tab is visible and refetches on focus.
 * `available` is false only when the endpoint answers 404/501 — then the sheet says so rather than
 * claiming everything is clear.
 */
export function useNotifications(pollMs = 60000) {
  const [feed, setFeed] = useState<NotificationFeed | null>(null);
  const [available, setAvailable] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const inflight = useRef(false);

  const load = useCallback(async () => {
    if (inflight.current) return;
    inflight.current = true;
    try {
      const r = await api.get<RawFeed | null>("/api/notifications", { tolerate: [404, 501] });
      if (r === null) { setAvailable(false); setFeed(null); }
      else { setAvailable(true); setFeed(normalise(r)); }
      setError(null);
    } catch (e) {
      setError(e); // keep the last good list; the bell must never break the shell
    } finally {
      inflight.current = false;
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void load(); }, pollMs);
    const onWake = () => { if (document.visibilityState === "visible") void load(); };
    window.addEventListener("focus", onWake);
    document.addEventListener("visibilitychange", onWake);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", onWake);
      document.removeEventListener("visibilitychange", onWake);
    };
  }, [load, pollMs]);

  const items = useMemo(() => (feed ? [...feed.high, ...feed.today, ...feed.later] : []), [feed]);
  const high = feed?.badge.high ?? 0;
  const today = feed?.badge.today ?? 0;
  return { feed, items, status: feed?.status || null, available, loading, error, reload: load, high, today, count: feed?.badge.count ?? high + today };
}

/* ---------- snooze helpers ---------- */
/** Phoenix does not observe DST, so it is UTC−7 all year. */
const PHOENIX_OFFSET_MS = 7 * 3600 * 1000;
export function minutesUntilTomorrow8Phoenix(now: Date = new Date()): number {
  const wall = new Date(now.getTime() - PHOENIX_OFFSET_MS);
  wall.setUTCDate(wall.getUTCDate() + 1);
  wall.setUTCHours(8, 0, 0, 0);
  const target = wall.getTime() + PHOENIX_OFFSET_MS;
  return Math.max(1, Math.round((target - now.getTime()) / 60000));
}
const MAX_SNOOZE_MINUTES = 60 * 24 * 14; // notifications_feed.NotificationSnoozeIn

/* ---------- one row ---------- */
function Row({ n, color, onOpen, onActed }: { n: NotificationItem; color: string; onOpen: (n: NotificationItem) => void; onActed: () => void }) {
  const { run, busy } = useCommand();
  const [mode, setMode] = useState<"idle" | "snooze">("idle");
  const [pickAt, setPickAt] = useState("");
  const [pickError, setPickError] = useState<string | null>(null);
  const route = routeFor(n);
  // Only stored notifications exist behind /api/notifications/{id}/… — a task or approval row is derived
  // from the record itself, so the buttons say where to act instead of failing.
  const actionable = n.source === "notification";
  const why = n.source === "approval"
    ? "An approval clears when you make the decision. Open it to review."
    : "This comes straight from the record. Open it and act there.";

  const act = async (action: "acknowledge" | "snooze" | "dismiss", body: Record<string, unknown> = {}, success?: string) => {
    const r = await run(`${n.id}:${action}`, `/api/notifications/${encodeURIComponent(n.id)}/${action}`, body, { success });
    if (r?.status === "ok") { setMode("idle"); onActed(); }
  };

  const snoozeMinutes = async (minutes: number, said: string) => {
    await act("snooze", { minutes }, `${said} The problem itself is untouched.`);
  };

  const snoozePicked = async () => {
    setPickError(null);
    if (!pickAt) { setPickError("Pick a date and time first."); return; }
    const target = new Date(pickAt);
    if (Number.isNaN(target.getTime())) { setPickError("That date and time can't be read."); return; }
    const minutes = Math.round((target.getTime() - Date.now()) / 60000);
    if (minutes < 1) { setPickError("Pick a time in the future."); return; }
    if (minutes > MAX_SNOOZE_MINUTES) { setPickError("Snooze can't go further than 14 days."); return; }
    await snoozeMinutes(minutes, `Snoozed until ${target.toLocaleString()}.`);
  };

  return (
    <li className="nf-item">
      <span className="nf-item__dot" style={{ background: color }} aria-hidden="true" />
      <div className="nf-item__main">
        <button type="button" className="nf-open" onClick={() => onOpen(n)} disabled={!route}
          title={route ? undefined : "No record is linked to this one."}>
          {n.title}
        </button>
        {n.body ? <span className="nf-item__body">{n.body}</span> : null}
        <span className="nf-item__meta">
          <Chip size="sm" tone="soft">{kindLabel(n.kind)}</Chip>
          {n.when ? <When iso={n.when} relative style={{ color }} /> : <span>Time not recorded</span>}
          {n.occurrences > 1 ? <span>· {n.occurrences} times</span> : null}
          {n.state === "snoozed" ? <span>· snoozed</span> : null}
        </span>
        {mode === "snooze" ? (
          <div className="nf-item__acts">
            <Button size="sm" variant="soft" loading={busy(`${n.id}:snooze`)} onClick={() => snoozeMinutes(60, "Snoozed for an hour.")}>1 hour</Button>
            <Button size="sm" variant="soft" loading={busy(`${n.id}:snooze`)} onClick={() => snoozeMinutes(minutesUntilTomorrow8Phoenix(), "Snoozed until tomorrow 8:00 AZ.")}>Tomorrow 8:00 AZ</Button>
            <Input type="datetime-local" aria-label="Snooze until" title="Uses this device's clock." value={pickAt} onChange={(e) => setPickAt(e.target.value)} style={{ width: 200, minWidth: 0 }} />
            <Button size="sm" variant="primary" loading={busy(`${n.id}:snooze`)} onClick={snoozePicked} disabled={!pickAt} disabledReason="Pick a date and time first.">Snooze</Button>
            <Button size="sm" variant="ghost" onClick={() => { setMode("idle"); setPickError(null); }}>Back</Button>
            {pickError ? <span className="fs12" style={{ color: "var(--blocked)" }} role="alert">{pickError}</span> : null}
          </div>
        ) : (
          <div className="nf-item__acts">
            <Button size="sm" variant="soft" disabled={!actionable} disabledReason={why} loading={busy(`${n.id}:acknowledge`)}
              onClick={() => act("acknowledge", {}, "Acknowledged. The problem itself is untouched.")}>Acknowledge</Button>
            <Button size="sm" variant="soft" disabled={!actionable} disabledReason={why} onClick={() => setMode("snooze")}>Snooze</Button>
            <Button size="sm" variant="ghost" disabled={!actionable} disabledReason={why} loading={busy(`${n.id}:dismiss`)}
              onClick={() => act("dismiss", {}, "Dismissed. The record keeps its own state.")}>Dismiss</Button>
          </div>
        )}
      </div>
    </li>
  );
}

/* ---------- bell ---------- */
export function NotificationBell({ mobile }: { mobile?: boolean }) {
  const { feed, status, available, loading, high, count, reload } = useNotifications();
  const [open, setOpen] = useState(false);
  const isMobile = useIsMobile();
  const nav = useNavigate();
  const useSheet = mobile ?? isMobile;
  const popRef = useRef<HTMLDivElement>(null);
  const bellRef = useRef<HTMLButtonElement>(null);

  const label = useMemo(() => {
    if (!available) return "Notifications · not connected yet";
    if (count === 0) return "Notifications · nothing due";
    return high ? `Notifications · ${high} needing attention, ${Math.max(0, count - high)} due today` : `Notifications · ${count} due today`;
  }, [available, count, high]);

  useEffect(() => {
    if (!open || useSheet) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { setOpen(false); bellRef.current?.focus(); } };
    document.addEventListener("keydown", onKey);
    const t = window.setTimeout(() => popRef.current?.focus({ preventScroll: true }), 0);
    return () => { document.removeEventListener("keydown", onKey); window.clearTimeout(t); };
  }, [open, useSheet]);

  // Deep links bypass navigation: straight to the record (spec §2.1).
  const go = (n: NotificationItem) => {
    const route = routeFor(n);
    if (!route) return;
    setOpen(false);
    nav(route);
  };

  const statusLine = !available ? (
    <span>Notifications aren't connected yet. Nothing here is a claim that you're clear.</span>
  ) : status ? (
    <>
      <span>
        {status.all_clear
          ? <>All clear as of {feed?.generated_at ? <When iso={feed.generated_at} format="time" /> : "now"}.</>
          : status.message || "Some items need attention."}
      </span>
      {status.stale_connections.length ? <Link to="/settings/connections" onClick={() => setOpen(false)}>Check connections</Link> : null}
    </>
  ) : (
    <span>Loading what needs attention…</span>
  );

  const body = (
    <>
      <div className={["nf-status", status && !status.all_clear ? "nf-status--attn" : ""].filter(Boolean).join(" ")} role="status">
        {statusLine}
      </div>
      <div className="notif__list">
        {loading && !feed ? <div style={{ padding: 16 }}><Loading rows={2} label="Loading notifications" /></div> : null}
        {GROUPS.map((g) => {
          const rows = feed ? feed[g.tier] : [];
          if (!rows.length) return null;
          return (
            <section key={g.tier} aria-labelledby={`nf-group-${g.tier}`}>
              <h3 id={`nf-group-${g.tier}`} className="nf-group" style={{ color: g.color, margin: 0 }}>{g.label} · {rows.length}</h3>
              <ul className="nf-list">
                {rows.map((n) => <Row key={`${g.tier}:${n.id}`} n={n} color={g.color} onOpen={go} onActed={reload} />)}
              </ul>
            </section>
          );
        })}
        {feed && !feed.high.length && !feed.today.length && !feed.later.length ? (
          <div className="empty">
            <span className="empty__title">{feed.status.all_clear ? "You're clear." : "Nothing in this list."}</span>
            <span className="empty__body">{feed.status.all_clear ? "Nothing due, nothing waiting." : feed.status.message}</span>
          </div>
        ) : null}
        {!available && !loading ? (
          <div className="empty">
            <span className="empty__title">Notifications aren't connected yet.</span>
            <span className="empty__body">The bell fills in once the server serves the feed. Tasks and approvals still work.</span>
          </div>
        ) : null}
      </div>
    </>
  );

  const footer = (
    <div className="notif__foot">
      <Link to="/tasks" onClick={() => setOpen(false)} className="fs13">All tasks</Link>
      <Link to="/settings/reminders" onClick={() => setOpen(false)} className="fs13">Reminder settings</Link>
    </div>
  );

  return (
    <>
      <IconButton
        ref={bellRef}
        label={label}
        title={label}
        active={open}
        badge={count || null}
        badgeTone={high ? "blocked" : "wait"}
        variant={useSheet ? "plain" : "glass"}
        size={useSheet ? "sm" : "md"}
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="dialog"
        aria-expanded={open}
      >
        <BellIcon size={useSheet ? 18 : 16} />
      </IconButton>
      {open && !useSheet ? (
        <>
          <div className="notif-scrim" onMouseDown={() => setOpen(false)} />
          <div ref={popRef} role="dialog" aria-label="Notifications" tabIndex={-1} className="notif-pop">
            <div className="notif__head">
              <span style={{ fontWeight: 600 }}>Notifications</span>
              <Button size="xs" variant="ghost" onClick={() => void reload()}>Refresh</Button>
            </div>
            {body}
            {footer}
          </div>
        </>
      ) : null}
      {useSheet ? (
        <Sheet open={open} onClose={() => setOpen(false)} title="Notifications" footer={footer}>
          {body}
        </Sheet>
      ) : null}
    </>
  );
}
