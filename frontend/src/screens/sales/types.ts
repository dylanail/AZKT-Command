/* Sales shapes. Mirrors backend/app/services/sales.py (lead_card, serialize_opportunity) and routers/sales.py. */
import type { TaskView } from "../tasks/types";

export type Pipeline = "vehicle" | "irq";
export type Stage = "new" | "conversation" | "awaiting_deposit" | "deposit_paid" | "lost";

export const PIPELINES: { id: Pipeline; label: string }[] = [
  { id: "vehicle", label: "Vehicle Sales" },
  { id: "irq", label: "IRQ" },
];
export const PIPELINE_LABEL: Record<string, string> = { vehicle: "Vehicle Sales", irq: "IRQ" };
export const BOARD_STAGES: { id: Stage; label: string }[] = [
  { id: "new", label: "New Lead" },
  { id: "conversation", label: "In Conversation" },
  { id: "awaiting_deposit", label: "Awaiting Deposit" },
  { id: "deposit_paid", label: "Deposit Paid" },
];
export const STAGE_LABEL: Record<string, string> = {
  new: "New Lead", conversation: "In Conversation", awaiting_deposit: "Awaiting Deposit", deposit_paid: "Deposit Paid", lost: "Lost / Not moving forward",
};
export const HAND_SETTABLE: Stage[] = ["new", "conversation", "awaiting_deposit", "lost"];
export const PAID_HANDOFF_NOTE = "Deposit Paid hands off to the vehicle's Sale tab: reservation, delivery, aftercare.";

export interface TaskBrief {
  id: string;
  title: string;
  type: string;
  status: string;
  owner_user_id?: string | null;
  due_at?: string | null;
  timezone?: string | null;
  local_due?: string | null;
  reminder_kind?: string | null;
  reminder_custom_minutes?: number | null;
  overdue?: boolean;
}

export interface LeadCard {
  id: string;
  version: number;
  pipeline: Pipeline | string;
  stage: Stage | string;
  stage_label?: string;
  contact_id?: string | null;
  name: string;
  company?: string | null;
  subject: string;
  vehicle_id?: string | null;
  stock_no?: string | null;
  import_request_id?: string | null;
  age: string;
  created_at?: string | null;
  stage_age?: string;
  owner_user_id?: string | null;
  owner?: { id: string; name: string } | null;
  next_task?: TaskBrief | null;
  next_action_label?: string;
  overdue?: boolean;
  source?: string | null;
  lost_reason?: string | null;
  converted_kind?: string | null;
  converted_id?: string | null;
}

export interface BoardColumn { stage: Stage | string; label: string; items: LeadCard[]; count: number; }
export interface BoardResp {
  pipeline: Pipeline | string;
  pipelines?: string[];
  stages?: string[];
  columns: BoardColumn[];
  lost: LeadCard[];
  total: number;
  as_of?: string;
}

export interface Opportunity {
  id: string;
  version: number;
  contact_id?: string | null;
  pipeline: Pipeline | string;
  stage: Stage | string;
  stage_label?: string;
  vehicle_id?: string | null;
  import_request_id?: string | null;
  enquiry?: string | null;
  budget_amount?: string | number | null;
  budget_currency?: string | null;
  source?: string | null;
  source_ref?: string | null;
  owner_user_id?: string | null;
  notes?: string | null;
  next_action?: string | null;
  lost_reason?: string | null;
  lost_at?: string | null;
  deposit_confirmed_at?: string | null;
  converted_kind?: string | null;
  converted_id?: string | null;
  converted_at?: string | null;
  conversation_ids?: string[];
  stage_history?: Array<{ from?: string | null; stage: string; at?: string; by_name?: string | null; note?: string | null }>;
  stage_changed_at?: string | null;
  reopened_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface ContactBrief {
  id: string;
  name: string;
  company?: string | null;
  primary_email?: string | null;
  primary_phone?: string | null;
  status?: string;
  identities?: Array<{ id: string; kind: string; value_raw?: string; value?: string; is_primary?: boolean; verified?: boolean }>;
}

export interface OpportunityDetail {
  opportunity: Opportunity;
  card: LeadCard;
  contact: ContactBrief | null;
  vehicle: { id: string; title?: string | null; stock_no?: string | null; commercial_state?: string | null; allocation?: string | null; hero_asset_id?: string | null; photo?: string | null } | null;
  import_request: { id: string; title?: string; status?: string; deposit_status?: string; agreement_status?: string; paused?: boolean } | null;
  conversion: { kind: string; id: string; at?: string | null; source_ref?: string | null; path?: string } | null;
  deposit: { state: string; label: string; invoices?: unknown[]; confirmed_at?: string | null };
  tasks: { open: TaskView[]; recent_closed: TaskView[]; next: TaskView | null };
  stages: Array<{ stage: string; label: string; hand_settable: boolean }>;
}

export function subjectKey(pipeline: string): string {
  return pipeline === "irq" ? "Looking for" : "Vehicle";
}
export function pipelineLabel(p: string | null | undefined): string {
  return PIPELINE_LABEL[p || ""] || p || "";
}
export function stageLabel(s: string | null | undefined): string {
  return STAGE_LABEL[s || ""] || s || "";
}
/** Age text from the card, with " ago" for prose. */
export function ageText(card: Pick<LeadCard, "age">): string {
  return card.age && card.age !== "Not recorded" ? `${card.age} ago` : "age not recorded";
}
