/* Application shell. Desktop: sidebar / toolbar / main / optional inspector grid.
   Mobile (<768px): full-page views with a floating pill bottom nav. */
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { useTheme } from "../lib/theme";
import { useIsMobile } from "../lib/viewport";
import { ROLE_LABELS, isEmployeeRole, type Role } from "../lib/perms";
import { initialOf } from "../lib/format";
import { api } from "../lib/api";
import { navFor, type NavItem } from "./nav";
import { InspectorPanel, useInspector } from "./Inspector";
import { NotificationBell } from "./Notifications";
import { Button, Dialog, IconButton, Input, Menu, NavPathIcon, SearchIcon, SegmentedControl, StatusDot, ThemeIcon, ChevronDown, When } from "../ui";

/* ---------- sync status (GET /api/health, tolerated 404) ---------- */
interface HealthResp { ok?: boolean; last_sync?: string | null; synced_at?: string | null; connections?: { bad?: number } | number; }
function useSyncStatus() {
  const [state, setState] = useState<{ ok: boolean | null; at: string | null; bad: number }>({ ok: null, at: null, bad: 0 });
  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const r = await api.get<HealthResp | null>("/api/health", { tolerate: [404, 501, 503] });
        if (!alive) return;
        if (!r) { setState({ ok: null, at: null, bad: 0 }); return; }
        const bad = typeof r.connections === "number" ? r.connections : (r.connections?.bad ?? 0);
        setState({ ok: r.ok !== false, at: r.last_sync ?? r.synced_at ?? null, bad });
      } catch { if (alive) setState((s) => ({ ...s, ok: false })); }
    };
    void load();
    const h = window.setInterval(() => { if (document.visibilityState === "visible") void load(); }, 60000);
    return () => { alive = false; window.clearInterval(h); };
  }, []);
  return state;
}

/* ---------- search ---------- */
type SearchKind = "vehicles" | "requests" | "contacts" | "tasks";
function SearchDialog({ open, onClose, employee }: { open: boolean; onClose: () => void; employee: boolean }) {
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<SearchKind>("vehicles");
  const nav = useNavigate();
  const ref = useRef<HTMLInputElement>(null);
  const kinds = employee
    ? [{ value: "tasks" as const, label: "My tasks" }, { value: "vehicles" as const, label: "Vehicles" }]
    : [{ value: "vehicles" as const, label: "Vehicles" }, { value: "requests" as const, label: "Requests" }, { value: "contacts" as const, label: "Contacts" }, { value: "tasks" as const, label: "Tasks" }];
  useEffect(() => { if (employee) setKind("tasks"); }, [employee]);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const s = q.trim();
    if (!s) return;
    onClose();
    nav(`/${kind}?q=${encodeURIComponent(s)}`);
    setQ("");
  };
  return (
    <Dialog open={open} onClose={onClose} title="Search" size="md" align="top" initialFocusRef={ref}>
      <form className="stack" onSubmit={submit}>
        <SegmentedControl label="Search in" options={kinds} value={kind} onChange={setKind} size="sm" />
        <Input ref={ref} value={q} onChange={(e) => setQ(e.target.value)} placeholder={employee ? "Task, vehicle or stock number…" : "Vehicle, stock number, name, request…"} aria-label="Search terms" />
        <div className="row-wrap">
          <Button type="submit" variant="primary" disabled={!q.trim()} disabledReason="Type something to search for.">Search</Button>
          <span className="fs12 t4">Opens the {kinds.find((k) => k.value === kind)?.label.toLowerCase()} list filtered by your words.</span>
        </div>
      </form>
    </Dialog>
  );
}

/* ---------- account menu ---------- */
function AccountMenu({ mobile }: { mobile?: boolean }) {
  const { user, logout } = useAuth();
  const { theme, toggleTheme, glass, toggleGlass } = useTheme();
  const nav = useNavigate();
  const role = (user?.role || "mechanic") as Role;
  const trigger = mobile
    ? <IconButton label="Account" size="sm" variant="plain"><span className="toolbar__initial">{initialOf(user?.display_name)}</span></IconButton>
    : <IconButton label="Account"><span className="toolbar__initial">{initialOf(user?.display_name)}</span></IconButton>;
  return (
    <Menu
      align="right"
      label="Account"
      heading={<span><b style={{ fontWeight: 500, color: "var(--text)" }}>{user?.display_name}</b> · {ROLE_LABELS[role] || role}</span>}
      trigger={trigger}
      items={[
        { label: theme === "dark" ? "Light mode" : "Dark mode", meta: "Appearance", onSelect: toggleTheme },
        { label: glass === "on" ? "Reduce transparency" : "Restore glass", meta: glass === "on" ? "Opaque panels" : "Blur on", onSelect: toggleGlass },
        { label: "Account & passkeys", to: isEmployeeRole(role) ? "/more/account" : "/settings/recovery", sepBefore: true },
        { label: "Sign out", sepBefore: true, onSelect: () => { void logout().then(() => nav("/login", { replace: true })); } },
      ]}
    />
  );
}

/* ---------- desktop sidebar ---------- */
function Sidebar({ items, utility, badDot }: { items: NavItem[]; utility: NavItem[]; badDot: boolean }) {
  const { user } = useAuth();
  const role = (user?.role || "mechanic") as Role;
  const scope = user?.scope === "assigned" ? "Only your tasks and vehicles" : role === "owner" ? "Everything, including Money" : "Everything you're allowed";
  return (
    <nav className="sidebar" aria-label="Primary">
      <div className="sidebar__logo"><img src="/gen/azkt-logo.png" alt="Arizona Kei Trucks" width={156} height={40} /></div>
      {items.map((n) => (
        <NavLink key={n.key} to={n.to} end={n.end} className="navitem">
          <span className="navitem__label">{n.label}</span>
        </NavLink>
      ))}
      {utility.length ? <div className="sidebar__sep" role="separator" /> : null}
      {utility.map((n) => (
        <NavLink key={n.key} to={n.to} end={n.end} className="navitem navitem--util">
          <span className="navitem__label">{n.label}</span>
          {n.key === "settings" && badDot ? <span className="navitem__dot" title="One connection needs attention" /> : null}
        </NavLink>
      ))}
      <div className="sidebar__me">
        <strong>{user?.display_name} · {ROLE_LABELS[role] || role}</strong>
        <span>{scope}</span>
      </div>
    </nav>
  );
}

/* ---------- desktop toolbar ---------- */
function Toolbar({ employee, onSearch, sync }: { employee: boolean; onSearch: () => void; sync: ReturnType<typeof useSyncStatus> }) {
  const { theme, toggleTheme } = useTheme();
  const insp = useInspector();
  const askOn = insp.open && insp.mode === "ask";
  const newItems = [
    { label: "Vehicle", meta: "Auction URL or stock", to: "/vehicles?new=1" },
    { label: "Lead", meta: "Sales", to: "/sales?new=1" },
    { label: "Import request", to: "/requests?new=1" },
    { label: "Task", to: "/tasks?new=1" },
    { label: "Person", meta: "Invite", to: "/settings/team?new=1", sepBefore: true },
  ];
  return (
    <div className="toolbar">
      <button type="button" className="toolbar__search" onClick={onSearch} aria-label="Search">
        <SearchIcon />
        <span>{employee ? "Search your tasks and vehicles…" : "Search vehicles, requests, contacts, tasks…"}</span>
      </button>
      {!employee ? (
        <Menu label="New" items={newItems} trigger={<Button className="toolbar__new">New <span className="t4 toolbar__new-caret"><ChevronDown /></span></Button>} />
      ) : null}
      <span className="toolbar__sync">
        <StatusDot tone={sync.ok === false ? "risk" : sync.ok === null ? "muted" : "ok"} size="sm" />
        {sync.ok === null ? "Sync status unknown" : sync.at ? <>Synced <When iso={sync.at} format="time" /></> : sync.ok ? "Synced" : "Sync problem"}
      </span>
      <NotificationBell />
      <IconButton label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"} title="Toggle light/dark" onClick={toggleTheme}><ThemeIcon /></IconButton>
      {!employee ? (
        <Button className="toolbar__ask" aria-pressed={askOn} onClick={insp.toggleAsk} title="Ask AZKT">
          <StatusDot tone="amber" size="sm" />
          <span className="toolbar__ask-label">Ask AZKT</span>
        </Button>
      ) : null}
      <AccountMenu />
    </div>
  );
}

/* ---------- mobile ---------- */
function MobileTop() {
  const { user } = useAuth();
  const role = (user?.role || "mechanic") as Role;
  return (
    <header className="mtop">
      <NavLink to={isEmployeeRole(role) ? "/tasks" : "/"} className="mtop__logo" aria-label="AZKT home"><img src="/gen/azkt-logo.png" alt="Arizona Kei Trucks" width={92} height={24} /></NavLink>
      <span className="mtop__right">
        <NotificationBell mobile />
        <span className="mtop__role">{ROLE_LABELS[role] || role}</span>
        <AccountMenu mobile />
      </span>
    </header>
  );
}

function MobileNav({ items }: { items: NavItem[] }) {
  return (
    <nav className="mnav" aria-label="Primary">
      {items.map((n) => (
        <NavLink key={n.key} to={n.to} end={n.end} className="mnav__item">
          <span className="mnav__icon">
            {n.img ? (
              <span className="mnav__mask" aria-hidden="true" style={{ width: n.imgSize || 26, height: n.imgSize || 26, ["--icon" as string]: `url(${n.img})` }} />
            ) : (
              <NavPathIcon name={n.icon || "more"} />
            )}
          </span>
          <span className="mnav__label">{n.mLabel || n.label}</span>
        </NavLink>
      ))}
    </nav>
  );
}

/* ---------- shell ---------- */
export default function Shell() {
  const { user } = useAuth();
  const isMobile = useIsMobile();
  const insp = useInspector();
  const loc = useLocation();
  const [searchOpen, setSearchOpen] = useState(false);
  const sync = useSyncStatus();
  const role = user?.role;
  const employee = isEmployeeRole(role);
  const nav = useMemo(() => navFor(role), [role]);

  // Keyboard: "/" focuses search (desktop), Esc closes the inspector.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
      if (e.key === "/" && !typing && !isMobile) { e.preventDefault(); setSearchOpen(true); }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [isMobile]);

  // Scroll to top on route change (page content, not the inspector).
  useEffect(() => { window.scrollTo({ top: 0 }); }, [loc.pathname]);

  if (isMobile) {
    return (
      <div className="mshell">
        <MobileTop />
        <main className="mmain" id="main"><Outlet /></main>
        <MobileNav items={nav.mobile} />
        <SearchDialog open={searchOpen} onClose={() => setSearchOpen(false)} employee={employee} />
      </div>
    );
  }
  const inspectorOpen = insp.open && !employee;
  return (
    <div className="shell" data-inspector={inspectorOpen ? "open" : "closed"}>
      <Sidebar items={nav.primary} utility={nav.utility} badDot={sync.bad > 0} />
      <Toolbar employee={employee} onSearch={() => setSearchOpen(true)} sync={sync} />
      <main className="main" id="main"><Outlet /></main>
      {inspectorOpen ? <InspectorPanel /> : null}
      <SearchDialog open={searchOpen} onClose={() => setSearchOpen(false)} employee={employee} />
    </div>
  );
}
