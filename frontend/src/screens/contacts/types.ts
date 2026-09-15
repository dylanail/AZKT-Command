/* Contact shapes. Mirrors backend/app/services/contacts.py serialize_contact / serialize_identity / serialize_merge
   and routers/contacts.py get_contact. */
import type { TaskView } from "../tasks/types";

export type ContactStatus = "active" | "provisional" | "merged" | "archived";

export interface Identity {
  id: string;
  kind: string;
  value_raw?: string;
  value_norm?: string;
  value?: string;
  label?: string;
  verified?: boolean;
  is_primary?: boolean;
  source?: string | null;
  country?: string | null;
  created_at?: string | null;
}

export interface Contact {
  id: string;
  version: number;
  name: string;
  company?: string | null;
  roles: string[];
  primary_email?: string | null;
  primary_phone?: string | null;
  status: ContactStatus | string;
  verified?: boolean;
  consent?: Record<string, unknown>;
  source?: string | null;
  source_ref?: string | null;
  notes?: string | null;
  merged_into_id?: string | null;
  aliases?: Array<{ name?: string | null; company?: string | null }>;
  provisional_reason?: string | null;
  extra?: Record<string, unknown>;
  archived_at?: string | null;
  merged_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  legacy_customer_id?: string | null;
  identities?: Identity[];
}

export interface ContactListResp { items: Contact[]; total: number; tab?: string; }

export interface MergeRecord {
  id: string;
  survivor_id: string;
  merged_id: string;
  reason?: string | null;
  merged_by?: string | null;
  approval_id?: string | null;
  snapshot?: unknown;
  reverted_at?: string | null;
  reverted_by?: string | null;
  created_at?: string | null;
}

export interface ContactDetailResp {
  contact: Contact;
  opportunities: Array<{ id: string; pipeline: string; stage: string; stage_label?: string; enquiry?: string | null; vehicle_id?: string | null; created_at?: string | null; lost_reason?: string | null }>;
  import_requests: Array<{ id: string; title?: string | null; status?: string | null; deposit_status?: string | null; opportunity_id?: string | null }>;
  vehicles: Array<{ id: string; title?: string | null; stock_no?: string | null; commercial_state?: string | null; allocation?: string | null }>;
  tasks: TaskView[];
  promises: Array<{ id: string; text: string; status?: string | null; due_at?: string | null; made_at?: string | null; made_by?: string | null; vehicle_id?: string | null; opportunity_id?: string | null }>;
  consent: Record<string, unknown>;
  conversations: Array<{ id: string; subject?: string | null; channel?: string | null; state?: string | null; contact_match?: string | null; last_inbound_at?: string | null }>;
  merge_history: MergeRecord[];
  merged_into: { id: string } | null;
}

export const ROLE_OPTIONS: { id: string; label: string }[] = [
  { id: "buyer", label: "Buyer" }, { id: "vendor", label: "Vendor" }, { id: "exporter", label: "Exporter" }, { id: "importer", label: "Importer" },
  { id: "carrier", label: "Carrier" }, { id: "dispatcher", label: "Dispatcher" }, { id: "port", label: "Port" }, { id: "other", label: "Other" },
];
export const ROLE_LABEL: Record<string, string> = Object.fromEntries(ROLE_OPTIONS.map((r) => [r.id, r.label]));
export const IDENTITY_KINDS: { id: string; label: string }[] = [
  { id: "email", label: "Email" }, { id: "phone", label: "Phone" }, { id: "telegram", label: "Telegram" }, { id: "instagram", label: "Instagram" },
  { id: "provider", label: "Provider ref" }, { id: "other", label: "Other" },
];
export const IDENTITY_LABEL: Record<string, string> = Object.fromEntries(IDENTITY_KINDS.map((k) => [k.id, k.label]));

export type ContactTab = "buyers" | "vendors" | "exporters" | "carriers" | "all";
export const TABS: { id: ContactTab; label: string }[] = [
  { id: "buyers", label: "Buyers" }, { id: "vendors", label: "Vendors" }, { id: "exporters", label: "Exporters" }, { id: "carriers", label: "Carriers" }, { id: "all", label: "All" },
];

export function identityValue(i: Identity): string {
  return i.value_raw || i.value || i.value_norm || "";
}
export function primaryIdentity(c: Contact): string {
  if (c.primary_email) return c.primary_email;
  if (c.primary_phone) return c.primary_phone;
  const p = (c.identities || []).find((i) => i.is_primary) || (c.identities || [])[0];
  return p ? identityValue(p) : "";
}
export function statusBadge(c: Pick<Contact, "status">): { tone: "wait" | "soft" | "risk" | "neutral"; label: string } | null {
  if (c.status === "provisional") return { tone: "wait", label: "Provisional" };
  if (c.status === "merged") return { tone: "soft", label: "Merged" };
  if (c.status === "archived") return { tone: "risk", label: "Archived" };
  return null;
}
