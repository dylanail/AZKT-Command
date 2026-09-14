/* Notification bell. Reads GET /api/notifications?state=unread (404 tolerated while the backend lands).
   Badge = high + today; red if any high, else the "wait" blue, as in the prototype. */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { useIsMobile } from "../lib/viewport";
import { BellIcon, IconButton, Sheet, When } from "../ui";

export type NotifTier = "high" | "today" | "later";
export interface NotificationItem {
  id: string;
  title: string;
  body?: string;
  tier: NotifTier;
  /** ISO time the item refers to (due/overdue moment). */
  at?: string | null;
  href?: string | null;
  kind?: string;
}

interface Envelope { items?: NotificationItem[]; notifications?: NotificationItem[]; }

function normalise(raw: unknown): NotificationItem[] {
  const list = Array.isArray(raw) ? raw : ((raw as Envelope)?.items || (raw as Envelope)?.notifications || []);
  return (list as Partial<NotificationItem>[]).filter((x) => x && typeof x.id === "string").map((x) => ({
    id: x.id as string,
    title: x.title || "Notification",
    body: x.body || "",
    tier: (x.tier === "high" || x.tier === "today" || x.tier === "later") ? x.tier : "later",
    at: x.at ?? null,
    href: x.href ?? null,
    kind: x.kind,
  }));
}

const GROUPS: { tier: NotifTier; label: string; color: string }[] = [
  { tier: "high", label: "Overdue", color: "var(--blocked)" },
  { tier: "today", label: "Today", color: "var(--wait)" },
  { tier: "later", label: "Later", color: "var(--t3)" },
];

/* Badge fallback while /api/notifications is not served: GET /api/approvals/summary (owner only; 403/404 tolerated)
   turns pending approvals into one "today" item that opens the approvals queue. */
interface ApprovalSummary { pending?: number; unknown?: number; failed?: number }
async function approvalFallback(): Promise<NotificationItem[]> {
  const s = await api.get<ApprovalSummary | null>("/api/approvals/summary", { tolerate: [403, 404, 501] });
  if (!s) return [];
  const out: NotificationItem[] = [];
  const pending = s.pending || 0;
  const bad = (s.unknown || 0) + (s.failed || 0);
  if (pending) out.push({ id: "approvals-pending", title: `${pending} ${pending === 1 ? "approval needs" : "approvals need"} your decision`, body: "Exact payloads waiting for you.", tier: "today", href: "/approvals", kind: "approval" });
  if (bad) out.push({ id: "approvals-attention", title: `${bad} ${bad === 1 ? "action" : "actions"} failed or unknown`, body: "Provider results that need a look.", tier: "high", href: "/approvals?view=attention", kind: "approval" });
  return out;
}

export function useNotifications(pollMs = 60000) {
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [available, setAvailable] = useState(true);
  const load = useCallback(async () => {
    try {
      const r = await api.get<unknown>("/api/notifications?state=unread", { tolerate: [404, 501] });
      if (r === null) {
        const fb = await approvalFallback().catch(() => [] as NotificationItem[]);
        setAvailable(fb.length > 0);
        setItems(fb);
        return;
      }
      setAvailable(true);
      setItems(normalise(r));
    } catch {
      /* keep the last list; the bell must never break the shell */
    }
  }, []);
  useEffect(() => {
    void load();
    const h = window.setInterval(() => { if (document.visibilityState === "visible") void load(); }, pollMs);
    return () => window.clearInterval(h);
  }, [load, pollMs]);
  const high = items.filter((i) => i.tier === "high").length;
  const today = items.filter((i) => i.tier === "today").length;
  return { items, available, reload: load, high, today, count: high + today };
}

export function NotificationBell({ mobile }: { mobile?: boolean }) {
  const { items, available, high, count } = useNotifications();
  const [open, setOpen] = useState(false);
  const isMobile = useIsMobile();
  const nav = useNavigate();
  const useSheet = mobile ?? isMobile;

  const label = useMemo(() => {
    if (!available) return "Notifications";
    if (count === 0) return "Notifications · nothing due";
    return high ? `${high} overdue, ${count - high} due today` : `${count} due today`;
  }, [available, count, high]);

  useEffect(() => {
    if (!open || useSheet) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, useSheet]);

  const go = (n: NotificationItem) => { setOpen(false); if (n.href) nav(n.href); };

  const list = (
    <>
      {GROUPS.map((g) => {
        const rows = items.filter((i) => i.tier === g.tier);
        if (!rows.length) return null;
        return (
          <div key={g.tier} style={{ display: "contents" }}>
            <div className="notif__group" style={{ color: g.color }}>{g.label}</div>
            {rows.map((n) => (
              <button key={n.id} type="button" className="notif__row" onClick={() => go(n)}>
                <span className="notif__dot" style={{ background: g.color }} aria-hidden="true" />
                <span className="stack-sm" style={{ gap: 1 }}>
                  <span style={{ fontWeight: 500 }}>{n.title}</span>
                  {n.body ? <span className="fs13 t3">{n.body}</span> : null}
                  {n.at ? <When iso={n.at} relative className="fs12" style={{ color: g.color }} /> : null}
                </span>
              </button>
            ))}
          </div>
        );
      })}
      {items.length === 0 ? (
        <div className="empty"><span className="empty__title">{available ? "You're clear." : "Notifications aren't connected yet."}</span><span className="empty__body">{available ? "Nothing due, nothing waiting." : "The bell will fill in once the backend reports reminders."}</span></div>
      ) : null}
    </>
  );

  const footer = (
    <div className="notif__foot">
      <Link to="/tasks" onClick={() => setOpen(false)} className="fs13">All tasks</Link>
      <Link to="/" onClick={() => setOpen(false)} className="fs13">Needs attention</Link>
    </div>
  );

  return (
    <>
      <IconButton
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
          <div role="dialog" aria-label="Notifications" className="notif-pop">
            <div className="notif__head"><span style={{ fontWeight: 600 }}>Notifications</span><span className="fs13 t3">{label}</span></div>
            <div className="notif__list">{list}</div>
            {footer}
          </div>
        </>
      ) : null}
      {useSheet ? (
        <Sheet open={open} onClose={() => setOpen(false)} title="Notifications" footer={footer}>
          <div className="notif__list">{list}</div>
        </Sheet>
      ) : null}
    </>
  );
}
