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
