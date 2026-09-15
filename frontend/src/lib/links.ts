/* Deep links for records referenced by activity rows and approvals (entity_kind + entity_id). */
const ROUTES: Record<string, (id: string) => string> = {
  vehicle: (id) => `/vehicles/${id}`,
  task: (id) => `/tasks/${id}`,
  contact: (id) => `/contacts/${id}`,
  approval: (id) => `/approvals/${id}`,
  shipment: (id) => `/shipments/${id}`,
  shipment_quote: (id) => `/shipments/${id}`,
  import_request: (id) => `/requests/${id}`,
  candidate: (id) => `/candidates/${id}`,
  listing: (id) => `/listings/${id}`,
  opportunity: (id) => `/sales?lead=${id}`,
  thread: (id) => `/inbox/${id}`,
  conversation: (id) => `/inbox/${id}`,
  message: (id) => `/inbox/${id}`,
  user: () => "/settings/team",
  invitation: () => "/settings/team",
  connection: () => "/settings/connections",
  setting: () => "/settings/automation",
  shop_gate_rule: () => "/settings/automation",
  workflow_control: () => "/settings/automation",
  permission: () => "/settings/team",
};

export function entityHref(kind: string | null | undefined, id: string | null | undefined): string | null {
  if (!kind) return null;
  const fn = ROUTES[kind];
  if (!fn) return null;
  if (!id && !["user", "invitation", "connection", "setting", "shop_gate_rule", "workflow_control", "permission"].includes(kind)) return null;
  return fn(id || "");
}

export function entityLabel(kind: string | null | undefined): string {
  if (!kind) return "record";
  return kind.replace(/_/g, " ");
}

export function shortId(id: string | null | undefined, n = 8): string {
  if (!id) return "";
  return id.length > n + 2 ? `${id.slice(0, n)}…` : id;
}

/** "send_message" → "Send message". */
export function humanize(s: string | null | undefined): string {
  if (!s) return "";
  const t = s.replace(/[_.-]+/g, " ").trim();
  return t.charAt(0).toUpperCase() + t.slice(1);
}
