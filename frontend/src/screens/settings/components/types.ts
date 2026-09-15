/* Shapes from backend/app/services/team.py serializers, routers/connections.py, settings_store.py and health.py. */
import type { Health } from "../../../ui";
import type { Role, Scope } from "../../../lib/perms";
import type { NotificationPrefs } from "./notifyTypes";

export interface Person {
  id: string; version: number; handle: string; display_name: string; role: Role | string; status: "active" | "invited" | "disabled" | string;
  scope: Scope | string; manager_id: string | null; email: string | null; phone: string | null; timezone: string | null;
  last_seen_at: string | null; created_at: string | null; disabled_at: string | null;
  /* owner detail only */
  perms?: Record<string, boolean>; overrides?: Record<string, boolean>; grants?: Grant[];
  access_changed_at?: string | null; access_changed_by?: string | null; disabled_by?: string | null; disabled_reason?: string | null;
  invited_by?: string | null; invitation_id?: string | null;
}
export interface Grant { perm: string; granted: boolean; by: string | null; at: string; note: string | null }
export interface Invitation {
  id: string; version: number; display_name: string; email: string | null; phone: string | null; role: string; scope: string;
  manager_id: string | null; perms: Record<string, boolean>; status: "pending" | "accepted" | "revoked" | "expired" | string;
  invited_by: string | null; note: string; expires_at: string | null; created_at: string | null; accepted_at: string | null;
  accepted_user_id: string | null; revoked_at: string | null; revoked_by: string | null;
}
export interface TeamResp { items: Person[]; total: number; scope: "all" | "reports" }
export interface InvitationsResp { items: Invitation[]; total: number }
export interface InviteData { invitation: Invitation; token: string | null; accept_path: string | null; created: boolean; note?: string }

export function statusView(status: string): { label: string; health: Health | null } {
  switch (status) {
    case "active": return { label: "Active", health: "ok" };
    case "invited": return { label: "Invited", health: "wait" };
    case "disabled": return { label: "Disabled", health: "blocked" };
    case "pending": return { label: "Invite pending", health: "wait" };
    case "accepted": return { label: "Accepted", health: "ok" };
    case "revoked": return { label: "Revoked", health: null };
    case "expired": return { label: "Expired", health: "risk" };
    default: return { label: status, health: null };
  }
}
export function scopeText(scope: string | null | undefined, role?: string): string {
  if (role === "owner") return "Everything, including Money";
  return scope === "assigned" ? "Only vehicles assigned to them" : "All vehicles";
}
export function contactOf(p: { email?: string | null; phone?: string | null }): string {
  return p.email || p.phone || "No contact recorded";
}

/* ---- connections ---- */
export type FreshState = "ok" | "warn" | "degraded" | "expired" | "disconnected" | "unknown";
export interface Connection {
  provider: string; label: string; status: string;
  freshness: { state: FreshState | string; label: string; last_success_at: string | null };
  account_identity?: string | null; config?: Record<string, unknown>; granted_scopes?: string[]; requested_scopes?: string[];
  last_attempt_at?: string | null; last_success_at?: string | null; coverage?: { from: string | null; to: string | null };
  watch_expires_at?: string | null; failure?: { kind?: string; message?: string; at?: string } | Record<string, never>;
  dependent_workflows?: string[]; environment?: string; id?: string | null; connected_at?: string | null;
}
export interface ConnectionsResp { items: Connection[]; total: number; all_clear_possible: boolean; stale: string[]; google_oauth_configured: boolean; environment: string }

export function freshnessHealth(state: string): Health {
  switch (state) {
    case "ok": return "ok";
    case "warn":
    case "degraded": return "risk";
    case "expired": return "blocked";
    default: return "wait";
  }
}

/* ---- settings store ---- */
export interface SettingEntry<T = Record<string, unknown>> { key: string; value: T; overrides: Record<string, unknown>; version: number; defaults: T; updated_at: string | null; updated_by: string | null; recorded: boolean }
export type ChannelMode = "telegram_email" | "telegram_fallback_email" | "email_only";
export const CHANNEL_MODES: { value: ChannelMode; label: string }[] = [
  { value: "telegram_email", label: "Telegram + email" },
  { value: "telegram_fallback_email", label: "Telegram, email if delivery fails" },
  { value: "email_only", label: "Email only" },
];
export const REMINDER_KINDS: { key: string; label: string; hint: string; ownerDefault?: boolean }[] = [
  { key: "task_reminder", label: "Task reminders", hint: "At the offset you pick on each task" },
  { key: "overdue", label: "Overdue", hint: "Once, after a task's time passes" },
  { key: "digest", label: "Morning digest", hint: "Only on days with something due" },
  { key: "deposit_confirmed", label: "Deposit paid", hint: "Instant confirmation with receipt" },
  { key: "case_update", label: "Case updates", hint: "Waiting cases, blockers, replies" },
  { key: "connection_issue", label: "Connection issues", hint: "A source stopped syncing" },
];
export interface RemindersSettings {
  task_reminder: { enabled: boolean; default_offset: string };
  overdue: { enabled: boolean; delay_minutes: number };
  digest: { enabled: boolean; local_time: string; timezone: string; non_empty_only: boolean };
  deposit_confirmed: { enabled: boolean; owner_only: boolean };
  channels: Record<string, ChannelMode | string>;
  employee_reminders_enabled: boolean; late_grace_minutes: number; obsolete_after_hours: number;
}
export interface AutomationSettings {
  parts_cap: string | null;
  spend_caps: { per_action: string | null; daily: string | null; monthly: string | null; currency: string };
  model_daily_budget_usd: string | null; model_monthly_budget_usd: string | null; discretionary_ai_enabled: boolean;
}
export interface ReportingSettings { required_cost_categories: string[] }
/* backend/app/models/finance.py COST_CATEGORIES, in plain English. */
export const COST_CATEGORIES: { value: string; label: string }[] = [
  { value: "purchase", label: "Purchase" },
  { value: "import", label: "Import" },
  { value: "transport", label: "Transport" },
  { value: "recon", label: "Recon" },
  { value: "parts", label: "Parts" },
  { value: "labor", label: "Labour" },
  { value: "selling", label: "Selling" },
  { value: "storage", label: "Storage" },
  { value: "other", label: "Other" },
];
export interface Control { key: string; paused: boolean; reason: string | null; changed_by: string | null; changed_at: string | null }
export interface Outstanding {
  external_actions: { pending: number; executing: number; unknown: number; by_state: Record<string, number> };
  approvals: { pending: number; queued: number; result_unknown: number };
}
export interface PauseResp { items: Control[]; total: number; outstanding: Outstanding; any_paused: boolean }
export interface GateRule {
  id: string | null; version: number; to_state: string; requirement: string; label: string; param: Record<string, unknown>;
  overridable: boolean; active: boolean; factual: boolean; source: "persisted" | "default"; updated_at: string | null; updated_by: string | null;
}
export interface GateRulesResp { items: GateRule[]; total: number; requirements: string[]; factual: string[] }
export const RECON_STATES: { value: string; label: string }[] = [
  { value: "needs_inspection", label: "Needs inspection" },
  { value: "in_recon", label: "In recon" },
  { value: "finalization", label: "Finalization" },
  { value: "ready_for_sale", label: "Ready for sale" },
];
export const REQUIREMENT_LABELS: Record<string, string> = {
  photos_min: "Minimum photos", recon_verified: "Recon work verified by owner", disclosures_written: "Disclosures written",
  inspection_logged: "Inspection logged", docs_complete: "Documents complete",
};

/* ---- me / prefs ---- */
export interface MePrefs {
  timezone: string | null; reminder_email: string | null; reminder_email_verified: boolean; reminder_email_verified_at: string | null;
  notification_prefs: NotificationPrefs;
}
export interface MeResp {
  id: string; version: number; handle: string; display_name: string; email: string | null; phone: string | null; role: string; scope: string;
  status: string; manager_id: string | null; perms: Record<string, boolean>; grants: Grant[]; prefs: MePrefs;
  pairing: { telegram: { status: string; paired_at?: string | null; last_inbound_at?: string | null; delivery_failures?: number; note?: string } };
  last_seen_at: string | null; created_at: string | null;
}

/* ---- health ---- */
export interface HealthResp {
  ok: boolean; environment: string; as_of: string;
  database: { ok: boolean };
  worker: { ok: boolean; last_heartbeat_at: string | null; detail: Record<string, unknown> };
  jobs: { by_state: Record<string, number>; lag_seconds: number };
  outbox: { pending: number; lag_seconds: number };
  external_actions: { unknown: number; failed: number };
  reminders: { overdue_deliveries: number };
  connections: Array<{ provider: string; label: string; freshness: { state: string; label: string; last_success_at?: string | null } }>;
  model: { available?: boolean; configured?: boolean; over_cap?: boolean; day_usd?: string; month_usd?: string; daily_cap_usd?: string; monthly_cap_usd?: string; api_key_present?: boolean };
  storage: { backend: string };
}
