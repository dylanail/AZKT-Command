/* Permission keys mirror backend/app/domain/actors.py PERM_KEYS and policy.py ROLE_DEFAULTS.
   The server is the authority; the client only uses these to hide/disable controls with a reason. */

export type Role = "owner" | "manager" | "mechanic" | "sales" | "logistics" | "books";
export type Scope = "all" | "assigned";

export const PERM_KEYS = [
  "vehicles.read", "vehicles.write", "vehicles.all",
  "tasks.read", "tasks.write", "tasks.assign", "tasks.verify",
  "contacts.read", "contacts.write",
  "sales.read", "sales.write",
  "requests.read", "requests.write",
  "shipping.read", "shipping.write",
  "inbox.read", "inbox.draft", "inbox.send",
  "listings.read", "listings.draft", "listings.publish",
  "parts.request", "parts.order",
  "documents.read", "documents.write",
  "costs.read", "finance.status", "finance.write",
  "activity.read", "agents.chat", "intake",
  "approve", "team", "settings", "connections",
  "knowledge.write", "permissions",
] as const;
export type Perm = (typeof PERM_KEYS)[number];

/** Owner-locked keys cannot be granted to non-owners (policy.py OWNER_LOCKED). */
export const OWNER_LOCKED: ReadonlySet<string> = new Set([
  "tasks.verify", "team", "approve", "permissions", "settings", "connections",
  "inbox.send", "listings.publish", "parts.order", "finance.write",
]);

export const ROLE_LABELS: Record<Role, string> = {
  owner: "Owner",
  manager: "Manager",
  mechanic: "Mechanic",
  sales: "Sales",
  logistics: "Logistics",
  books: "Bookkeeper",
};

export const ROLE_DESCRIPTIONS: Record<Role, string> = {
  owner: "Everything, including Money.",
  manager: "Runs the people you assign to them. Sees everything, including Money. Cannot add people or verify work.",
  mechanic: "Tasks and vehicles in the shop. No prices, no customer messages.",
  sales: "Buyers, listings and the inbox. No costs or margins.",
  logistics: "Shipments, ports and documents. No costs.",
  books: "Finance and invoices. Vehicles read-only.",
};

export interface PermHolder {
  role: Role | string;
  perms?: Record<string, boolean> | null;
}

/** Pure check. Owner always passes; otherwise the effective perms map decides. */
export function can(user: PermHolder | null | undefined, perm: Perm | string): boolean {
  if (!user) return false;
  if (user.role === "owner") return true;
  return !!(user.perms && user.perms[perm]);
}

export function canAny(user: PermHolder | null | undefined, perms: (Perm | string)[]): boolean {
  return perms.some((p) => can(user, p));
}

/** Employees see the reduced shell (My tasks · Vehicles · More). */
export function isEmployeeRole(role: string | undefined | null): boolean {
  return role === "mechanic" || role === "logistics";
}

/** Plain-language reason for a disabled control. */
export function whyNot(perm: Perm | string): string {
  const map: Record<string, string> = {
    "tasks.verify": "Only the owner verifies finished work.",
    team: "Only the owner adds or removes people.",
    approve: "Only the owner approves.",
    settings: "Only the owner changes settings.",
    connections: "Only the owner manages connections.",
    "inbox.send": "Sending needs owner approval.",
    "listings.publish": "Publishing needs owner approval.",
    "parts.order": "Orders above your cap need owner approval.",
    "finance.write": "Only the owner writes to Finance.",
    "costs.read": "Costs and margins are hidden for your role.",
  };
  return map[perm] || "Your role can't do this.";
}

/** Role presets mirrored from policy.py ROLE_DEFAULTS. Used only to preview effective switches in the
    Add/Edit person dialog; the server recomputes on save. */
const MANAGER: Record<Perm, boolean> = {
  "vehicles.read": true, "vehicles.write": true, "vehicles.all": true,
  "tasks.read": true, "tasks.write": true, "tasks.assign": true, "tasks.verify": false,
  "contacts.read": true, "contacts.write": true,
  "sales.read": true, "sales.write": true,
  "requests.read": true, "requests.write": true,
  "shipping.read": true, "shipping.write": true,
  "inbox.read": true, "inbox.draft": true, "inbox.send": false,
  "listings.read": true, "listings.draft": true, "listings.publish": false,
  "parts.request": true, "parts.order": false,
  "documents.read": true, "documents.write": true,
  "costs.read": false, "finance.status": true, "finance.write": false,
  "activity.read": true, "agents.chat": true, intake: true,
  approve: false, team: false, settings: false, connections: false,
  "knowledge.write": false, permissions: false,
};
const MECHANIC: Record<Perm, boolean> = {
  "vehicles.read": true, "vehicles.write": false, "vehicles.all": false,
  "tasks.read": true, "tasks.write": true, "tasks.assign": false, "tasks.verify": false,
  "contacts.read": false, "contacts.write": false,
  "sales.read": false, "sales.write": false,
  "requests.read": false, "requests.write": false,
  "shipping.read": false, "shipping.write": false,
  "inbox.read": false, "inbox.draft": false, "inbox.send": false,
  "listings.read": false, "listings.draft": false, "listings.publish": false,
  "parts.request": true, "parts.order": false,
  "documents.read": false, "documents.write": false,
  "costs.read": false, "finance.status": false, "finance.write": false,
  "activity.read": false, "agents.chat": true, intake: true,
  approve: false, team: false, settings: false, connections: false,
  "knowledge.write": false, permissions: false,
};
export const ROLE_DEFAULTS: Record<Role, Record<Perm, boolean>> = {
  owner: Object.fromEntries(PERM_KEYS.map((k) => [k, true])) as Record<Perm, boolean>,
  manager: MANAGER,
  mechanic: MECHANIC,
  sales: { ...MANAGER, "tasks.assign": false, "shipping.write": false, "requests.write": true, "listings.draft": true },
  logistics: { ...MECHANIC, "vehicles.all": true, "shipping.read": true, "shipping.write": true, "contacts.read": true, "documents.read": true, "documents.write": true, "activity.read": true },
  books: { ...MECHANIC, "vehicles.all": true, "costs.read": true, "finance.status": true, "finance.write": true, "documents.read": true, "activity.read": true, "tasks.write": false, "parts.request": false },
};

/** Role defaults + per-person overrides, with owner-locked keys ignored for non-owners (policy.effective_perms). */
export function effectivePerms(role: Role | string, overrides: Record<string, boolean> | null | undefined): Record<Perm, boolean> {
  const base = { ...(ROLE_DEFAULTS[role as Role] || ROLE_DEFAULTS.mechanic) };
  for (const [k, v] of Object.entries(overrides || {})) {
    if (OWNER_LOCKED.has(k) && role !== "owner") continue;
    if (k in base) base[k as Perm] = !!v;
  }
  return base;
}

/** Plain-language labels for the permission switches. */
export const PERM_LABELS: Record<Perm, string> = {
  "vehicles.read": "See vehicles",
  "vehicles.write": "Edit vehicles",
  "vehicles.all": "All vehicles, not only assigned",
  "tasks.read": "See tasks",
  "tasks.write": "Work on tasks",
  "tasks.assign": "Assign tasks to others",
  "tasks.verify": "Verify finished work",
  "contacts.read": "See contacts",
  "contacts.write": "Edit contacts",
  "sales.read": "See sales leads",
  "sales.write": "Work sales leads",
  "requests.read": "See import requests",
  "requests.write": "Work import requests",
  "shipping.read": "See shipments",
  "shipping.write": "Update shipments",
  "inbox.read": "Read the inbox",
  "inbox.draft": "Draft replies",
  "inbox.send": "Send customer messages",
  "listings.read": "See listings",
  "listings.draft": "Draft listings",
  "listings.publish": "Publish listings",
  "parts.request": "Request parts",
  "parts.order": "Order parts",
  "documents.read": "See documents",
  "documents.write": "Add documents",
  "costs.read": "See costs and margins",
  "finance.status": "See payment status",
  "finance.write": "Write to Finance",
  "activity.read": "See Activity",
  "agents.chat": "Talk to AZKT agents",
  intake: "Photo and voice intake",
  approve: "Approve actions",
  team: "Add or remove people",
  settings: "Change settings",
  connections: "Manage connections",
  "knowledge.write": "Teach procedures",
  permissions: "Change permissions",
};

export const PERM_GROUPS: { label: string; keys: Perm[] }[] = [
  { label: "Vehicles & tasks", keys: ["vehicles.read", "vehicles.write", "vehicles.all", "tasks.read", "tasks.write", "tasks.assign", "tasks.verify"] },
  { label: "People & sales", keys: ["contacts.read", "contacts.write", "sales.read", "sales.write", "requests.read", "requests.write"] },
  { label: "Messages & listings", keys: ["inbox.read", "inbox.draft", "inbox.send", "listings.read", "listings.draft", "listings.publish"] },
  { label: "Shop & shipping", keys: ["parts.request", "parts.order", "shipping.read", "shipping.write", "documents.read", "documents.write", "intake"] },
  { label: "Money", keys: ["costs.read", "finance.status", "finance.write"] },
  { label: "AZKT & admin", keys: ["activity.read", "agents.chat", "knowledge.write", "approve", "team", "permissions", "settings", "connections"] },
];
