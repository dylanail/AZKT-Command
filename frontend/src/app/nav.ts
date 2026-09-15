/* Role-aware navigation tables. Counts are filled by the shell when endpoints exist (provisional). */
import type { Role } from "../lib/perms";
import { isEmployeeRole } from "../lib/perms";

export interface NavItem {
  key: string;
  label: string;
  /** Mobile label when shorter. */
  mLabel?: string;
  to: string;
  /** Masked PNG icon (mobile nav) from /gen; falls back to an SVG path in Icons.NAV_PATHS. */
  img?: string;
  imgSize?: number;
  icon?: string;
  /** Match nested routes too (default true). */
  end?: boolean;
  /** Show a risk dot (e.g. a connection needs attention). */
  dot?: boolean;
}

const HOME: NavItem = { key: "home", label: "Home", to: "/", img: "/gen/icon-home.png", imgSize: 26, icon: "home", end: true };
const VEHICLES: NavItem = { key: "vehicles", label: "Vehicles", to: "/vehicles", img: "/gen/icon-vehicles.png", imgSize: 34, icon: "vehicles" };
const SALES: NavItem = { key: "sales", label: "Sales", to: "/sales", icon: "sales" };
const REQUESTS: NavItem = { key: "requests", label: "Import requests", mLabel: "Requests", to: "/requests" };
const TASKS: NavItem = { key: "tasks", label: "Tasks", to: "/tasks", icon: "tasks" };
const MY_TASKS: NavItem = { key: "tasks", label: "My tasks", to: "/tasks", icon: "tasks" };
const INBOX: NavItem = { key: "inbox", label: "Inbox", to: "/inbox" };
const CONTACTS: NavItem = { key: "contacts", label: "Contacts", to: "/contacts", icon: "team" };
const SHIPMENTS: NavItem = { key: "shipments", label: "Shipments", to: "/shipments" };
const AGENTS: NavItem = { key: "agents", label: "Agents", to: "/agents", img: "/gen/icon-agents-thin.png", imgSize: 26, icon: "agents" };
const ACTIVITY: NavItem = { key: "activity", label: "Activity", to: "/activity" };
const FINANCE: NavItem = { key: "finance", label: "Finance", to: "/finance" };
const TEAM: NavItem = { key: "team", label: "Team", to: "/settings/team", icon: "team" };
const PEOPLE: NavItem = { key: "team", label: "People", to: "/settings/team", icon: "team" };
const SETTINGS: NavItem = { key: "settings", label: "Settings", to: "/settings", end: true };
const MORE: NavItem = { key: "more", label: "More", to: "/more", icon: "more" };

export interface NavSet {
  primary: NavItem[];
  utility: NavItem[];
  mobile: NavItem[];
  /** Items listed on the mobile "More" page. */
  more: NavItem[];
}

export function navFor(role: Role | string | undefined): NavSet {
  if (isEmployeeRole(role)) {
    // Logistics lives in shipments; mechanics never see them (shipping.read is off for the role).
    const primary = role === "logistics" ? [MY_TASKS, SHIPMENTS, VEHICLES] : [MY_TASKS, VEHICLES];
    return {
      primary,
      utility: [MORE],
      mobile: primary.length > 3 ? [MY_TASKS, VEHICLES, MORE] : [...primary, MORE],
      more: role === "logistics" ? [SHIPMENTS] : [],
    };
  }
  const isOwner = role === "owner";
  const utility = isOwner ? [AGENTS, ACTIVITY, FINANCE, TEAM, SETTINGS] : [AGENTS, ACTIVITY, FINANCE, PEOPLE, SETTINGS];
  return {
    primary: [HOME, VEHICLES, SALES, REQUESTS, SHIPMENTS, TASKS, INBOX, CONTACTS],
    utility,
    mobile: [HOME, VEHICLES, SALES, MORE],
    more: [REQUESTS, SHIPMENTS, TASKS, INBOX, CONTACTS, AGENTS, ACTIVITY, FINANCE, isOwner ? TEAM : PEOPLE, SETTINGS],
  };
}

/** Routes a role may open. Everything else lands on /denied. */
export function routeAllowed(role: Role | string | undefined, pathname: string): boolean {
  if (!role) return false;
  if (isEmployeeRole(role)) {
    if (role === "logistics" && /^\/(shipments|contacts)(\/|$)/.test(pathname)) return true;
    return /^\/(tasks|vehicles|more|approvals)(\/|$)/.test(pathname) || pathname === "/";
  }
  if (role === "owner") return true;
  // manager and other office roles: everything except owner-only settings sections
  // (Recovery stays open: it holds everyone's passkeys and sign-out; the health block inside is owner-only).
  if (/^\/settings\/usage(\/|$)/.test(pathname)) return false;
  return true;
}
