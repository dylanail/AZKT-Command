/* Vehicle shapes and pure helpers shared by Vehicles, VehicleDetail, Intake and components/.
   Mirrors backend/app/services/vehicles.py (serialize_vehicle / serialize_list_item / serialize_fact /
   serialize_milestone / serialize_issue / serialize_work_order / serialize_part / vehicle_detail),
   services/shop.py (board, evaluate_gates) and services/intake.py (serialize_intake / intake_status). */
import type { Health } from "../../ui";
import type { TaskView } from "../tasks/types";

/* ---------- states ---------- */
export const RECON_STATES = ["needs_inspection", "in_recon", "finalization", "ready_for_sale"] as const;
export type ReconState = (typeof RECON_STATES)[number];

export const STATE_LABELS: Record<string, string> = {
  candidate: "Candidate", purchased: "Purchased", export_pending: "Export pending", on_vessel: "On vessel",
  at_port: "At port", released: "Released", domestic_transit: "Domestic transit", received: "Received",
  not_applicable: "No shipment", needs_inspection: "Needs inspection", in_recon: "In recon",
  finalization: "Finalization", ready_for_sale: "Ready for sale", not_listed: "Not listed", listed: "Listed",
  reserved: "Reserved", sold: "Sold", delivered: "Delivered", pending: "Documents pending",
  complete: "Documents complete", conflicted: "Documents conflicted", missing: "Documents missing",
};
export function stateLabel(s: string | null | undefined): string {
  if (!s) return "Not recorded";
  return STATE_LABELS[s] || s.replace(/_/g, " ");
}
export const SHOP_STAGES: { id: ReconState; label: string }[] = RECON_STATES.map((s) => ({ id: s, label: STATE_LABELS[s] }));

export const VIEWS = ["all", "sourcing", "shipping", "shop", "sales"] as const;
export type VehicleView = (typeof VIEWS)[number];
export const VIEW_LABELS: Record<VehicleView, string> = {
  all: "All", sourcing: "Sourcing", shipping: "Shipping", shop: "Shop", sales: "Sales",
};

/* ---------- facts ---------- */
export type FactStatus = "reported" | "inferred" | "confirmed" | "estimated" | "conflicted" | "outdated" | "unknown" | string;

export const FACT_LABELS: Record<string, string> = {
  title: "Title", make: "Make", model: "Model", model_year: "Year", color: "Color", grade: "Grade",
  location: "Location", odometer_km: "Odometer", frame_no: "Frame number", title_status: "Title status",
  purchase_amount: "Purchase amount", acquired_at: "Acquired", landed_cost_usd: "Landed cost",
  asking_price: "Asking price",
};
export function factLabel(key: string): string {
  return FACT_LABELS[key] || key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

export const FACT_STATUS_LABELS: Record<string, string> = {
  reported: "Reported, not verified", inferred: "Inferred", confirmed: "Confirmed", estimated: "Estimated",
  conflicted: "Conflicted", outdated: "Outdated", unknown: "Unknown",
};
/** Colour tone for a fact's status. Always paired with the status text. */
export function factTone(status: FactStatus): "ok" | "risk" | "wait" | "neutral" {
  if (status === "confirmed") return "ok";
  if (status === "conflicted") return "risk";
  if (status === "outdated" || status === "unknown") return "wait";
  return "neutral";
}

export interface FactData {
  id: string;
  version: number;
  vehicle_id: string;
  key: string;
  value: string | null;
  value_num?: string | null;
  unit?: string | null;
  currency?: string | null;
  status: FactStatus;
  observed_at?: string | null;
  effective_at?: string | null;
  source_kind?: string | null;
  source_ref?: string | null;
  actor?: string | null;
  confidence_method?: string | null;
  supersedes_id?: string | null;
  conflict_with_id?: string | null;
  visibility?: string | null;
  is_current: boolean;
  critical: boolean;
  created_at?: string | null;
}

/** "84,210 km" / "5,263 USD" / the raw value. Never invents a value. */
export function factValue(f: FactData): string {
  if (f.value === null || f.value === undefined || f.value === "") return "Not recorded";
  const num = f.value_num ?? null;
  if (num !== null && f.currency) return `${formatNumber(num)} ${f.currency.toUpperCase()}`;
  if (num !== null && f.unit) return `${formatNumber(num)} ${f.unit}`;
  if (f.unit) return `${f.value} ${f.unit}`;
  return f.value;
}
function formatNumber(s: string): string {
  const n = Number(s);
  if (!Number.isFinite(n)) return s;
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(n);
}

/* ---------- milestones ---------- */
export const MILESTONE_LABELS: Record<string, string> = {
  purchased: "Purchased", export_cleared: "Export cleared", on_vessel: "On vessel", arrived_port: "Arrived at port",
  released: "Released", received: "Received", inspected: "Inspected", recon_started: "Recon started",
  ready: "Ready for sale", listed: "Listed", reserved: "Reserved", sold: "Sold", delivered: "Delivered",
};
export const MILESTONE_ORDER = [
  "purchased", "export_cleared", "on_vessel", "arrived_port", "released", "received",
  "inspected", "recon_started", "ready", "listed", "reserved", "sold", "delivered",
];
export const MILESTONE_STATUS_LABELS: Record<string, string> = {
  planned: "Planned", estimated: "Estimated", completed: "Completed",
};

export interface MilestoneData {
  id: string;
  version: number;
  vehicle_id: string;
  kind: string;
  status: string;
  at?: string | null;
  date_label?: string | null;
  source_kind?: string | null;
  source_ref?: string | null;
  actor_id?: string | null;
  note?: string | null;
  supersedes_id?: string | null;
  is_current: boolean;
  shipment_id?: string | null;
  created_at?: string | null;
}

/* ---------- recon / parts ---------- */
export interface IssueData {
  id: string;
  version: number;
  vehicle_id: string;
  title: string;
  detail?: string | null;
  severity: string;
  status: string;
  source_kind?: string | null;
  source_ref?: string | null;
  intake_observation_id?: string | null;
  asset_ids: string[];
  work_order_id?: string | null;
  task_id?: string | null;
  disclosure_required: boolean;
  resolved_at?: string | null;
  resolved_by?: string | null;
  resolution_note?: string | null;
  deferred_at?: string | null;
  deferred_reason?: string | null;
  created_at?: string | null;
}
export const ISSUE_STATUS_LABELS: Record<string, string> = {
  open: "Open", in_progress: "In progress", resolved: "Resolved", deferred: "Deferred",
};
export const SEVERITY_LABELS: Record<string, string> = {
  info: "Info", normal: "Normal", major: "Major", safety: "Safety",
};

export interface WorkOrderData {
  id: string;
  version: number;
  vehicle_id: string;
  ref?: string | null;
  title: string;
  status: string;
  assignee_user_id?: string | null;
  recon_issue_id?: string | null;
  cost_item_id?: string | null;
  notes?: string | null;
  created_at?: string | null;
}

export const PART_CHAIN = ["requested", "approved", "ordered", "arrived", "installed", "verified"] as const;
export type PartState = (typeof PART_CHAIN)[number] | "cancelled" | "returned";
export const PART_LABELS: Record<string, string> = {
  requested: "Requested", approved: "Approved", ordered: "Ordered", arrived: "Arrived",
  installed: "Installed", verified: "Verified", cancelled: "Cancelled", returned: "Returned",
};

export interface PartData {
  id: string;
  version: number;
  vehicle_id: string;
  work_order_id?: string | null;
  name: string;
  part_no?: string | null;
  vendor_contact_id?: string | null;
  quantity: number;
  state: PartState | string;
  payment_state?: string | null;
  requested_by?: string | null;
  order_ref?: string | null;
  ordered_at?: string | null;
  arrived_at?: string | null;
  installed_at?: string | null;
  verified_at?: string | null;
  verified_by?: string | null;
  paid_at?: string | null;
  cost_item_id?: string | null;
  approval_id?: string | null;
  evidence?: Array<{ kind?: string; asset_ids?: string[]; note?: string | null; by?: string | null; at?: string | null }> | null;
  notes?: string | null;
  history?: Array<{ from?: string; to?: string; at?: string; by?: string | null; note?: string | null }> | null;
  physical_label?: string;
  created_at?: string | null;
}

/* ---------- assets ---------- */
export interface AssetLinkBrief {
  id: string;
  role: string;
  slot?: string | null;
  position?: number | null;
  entity_kind?: string;
  entity_id?: string;
  confirmed_by?: string | null;
}
export interface AssetBrief {
  id: string;
  kind: string;
  content_type?: string | null;
  size_bytes?: number | null;
  width?: number | null;
  height?: number | null;
  captured_at?: string | null;
  uploaded_at?: string | null;
  classification?: string | null;
  sensitive: boolean;
  public_eligible: boolean;
  pre_arrival: boolean;
  status: string;
  original_name?: string | null;
  urls?: { original?: string; web?: string; thumb?: string } | null;
  transcript?: string | null;
  link?: AssetLinkBrief | null;
  error?: string | null;
}
export const CLASSIFICATION_LABELS: Record<string, string> = {
  listing_photo: "Listing photo", invoice: "Invoice", id_document: "ID document",
  shipping_paper: "Shipping paper", screenshot: "Screenshot", unrelated: "Unrelated",
  unknown: "Not classified", voice_note: "Voice note",
};
export const SLOT_LABELS: Record<string, string> = {
  front_34: "front 3/4", interior: "interior", rear: "rear", side: "side", bed: "bed", engine: "engine",
  dash: "dash", odometer: "odometer", frame: "frame plate", underbody: "underbody", damage: "damage",
};
export function slotLabel(slot: string | null | undefined): string {
  if (!slot) return "unslotted";
  return SLOT_LABELS[slot] || slot.replace(/_/g, " ");
}
export const DEFAULT_SLOTS = ["front_34", "interior", "rear", "side", "bed", "engine"];

/* ---------- vehicle core ---------- */
export interface VehicleStates {
  logistics: string;
  recon: string;
  commercial: string;
  documents: string;
}
export interface ConditionBullet {
  id: string;
  text: string;
  source: string;
  evidence?: string[];
  observation_id?: string | null;
  observation_ids?: string[];
  added_by?: string | null;
  added_at?: string | null;
  version?: number;
  removed?: boolean;
  history?: Array<Record<string, unknown>>;
}
export const CONDITION_SOURCE_LABELS: Record<string, string> = {
  owner_reported: "Owner reported", image_observed: "Seen in photos", proposed_check: "Proposed check",
  inspection: "Inspection", customer: "Customer reported",
};

export interface VehicleCore {
  id: string;
  version: number;
  stock_no?: string | null;
  title: string;
  title_raw?: string | null;
  frame_no_raw?: string | null;
  frame_no_norm?: string | null;
  make?: string | null;
  model?: string | null;
  model_year?: number | null;
  color?: string | null;
  grade?: string | null;
  odometer_km?: number | null;
  title_status?: string | null;
  intake_status?: string | null;
  missing_identity_fields: string[];
  allocation?: string | null;
  buyer_contact_id?: string | null;
  states: VehicleStates;
  state_labels?: Partial<Record<keyof VehicleStates, string>>;
  logistics_state: string;
  recon_state: string;
  commercial_state: string;
  documents_state: string;
  health: Health | string;
  health_reason?: string | null;
  situation?: string | null;
  exception?: string | null;
  next_action?: string | null;
  next_action_owner_id?: string | null;
  next_action_due_at?: string | null;
  location?: string | null;
  condition: ConditionBullet[];
  condition_version?: number;
  condition_history?: Array<Record<string, unknown>>;
  dates: Record<string, string | null>;
  purchase_amount?: string | null;
  purchase_currency?: string | null;
  landed_cost_usd?: number | null;
  sold_price_usd?: number | null;
  asking_price?: string | null;
  asking_currency?: string | null;
  price_approved_at?: string | null;
  origin_candidate_id?: string | null;
  active_sale_id?: string | null;
  hero_asset_id?: string | null;
  photo?: string | null;
  photo_requirements: string[];
  disclosures: string[];
  archived_at?: string | null;
  notes?: string | null;
  view: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface VehicleListItem {
  id: string;
  version: number;
  stock_no?: string | null;
  title: string;
  frame_no_raw?: string | null;
  make?: string | null;
  model?: string | null;
  model_year?: number | null;
  color?: string | null;
  photo?: { asset_id: string; thumb: string } | null;
  photo_status?: string | null;
  allocation?: string | null;
  situation?: string | null;
  states: VehicleStates;
  next_action?: string | null;
  next_action_owner_id?: string | null;
  next_action_due_at?: string | null;
  exception?: string | null;
  health: Health | string;
  health_reason?: string | null;
  intake_status?: string | null;
  missing_identity_fields: string[];
  location?: string | null;
  view: string;
  archived_at?: string | null;
  updated_at?: string | null;
}
export interface VehicleListResp {
  items: VehicleListItem[];
  total: number;
  view?: string;
  views?: string[];
  empty_state?: string | null;
}

/* ---------- detail ---------- */
export interface ShipmentBrief {
  id: string;
  ref?: string | null;
  status?: string | null;
  eta_at?: string | null;
  eta_source?: string | null;
  container_no?: string | null;
  vessel?: string | null;
}
export interface SaleTab {
  commercial_state: string;
  allocation?: string | null;
  buyer_contact_id?: string | null;
  asking_price?: string | null;
  asking_currency?: string | null;
  price_approved_at?: string | null;
  active_sale?: {
    id: string; status: string; buyer_contact_id?: string | null; reserved_at?: string | null;
    reservation_expires_at?: string | null; completed_at?: string | null; delivered_at?: string | null;
  } | null;
  opportunities: Array<{ id: string; contact_id?: string | null; pipeline?: string; stage?: string; owner_user_id?: string | null }>;
  disclosures: string[];
}
export interface MoneyTab {
  money_hidden: boolean;
  purchase_amount?: string | null;
  purchase_currency?: string | null;
  asking_price?: string | null;
  asking_currency?: string | null;
  cost_items?: Array<{ id: string; category: string; description?: string | null; currency: string; amount?: string | null; status: string; is_estimate_only: boolean }>;
  cost_total?: Record<string, string>;
  coverage?: string;
}
export interface VehicleDetailResp {
  vehicle: VehicleCore;
  tabs: {
    overview: {
      identity: { stock_no?: string | null; frame_no_raw?: string | null; title: string; intake_status?: string | null; missing_identity_fields: string[] };
      health: { health: string; reason?: string | null; next_action?: string | null; owner_id?: string | null; due_at?: string | null };
      condition: ConditionBullet[];
      condition_version?: number;
      facts: FactData[];
      milestones: MilestoneData[];
      states: VehicleStates;
      situation?: string | null;
    };
    work: {
      tasks: TaskView[];
      recon_issues: IssueData[];
      work_orders: WorkOrderData[];
      parts: PartData[];
      shipments: ShipmentBrief[];
    };
    files: {
      photos: AssetBrief[];
      documents: AssetBrief[];
      audio: AssetBrief[];
      hero_asset_id?: string | null;
      photo_status?: string | null;
      missing_slots: string[];
      required_slots: string[];
    };
    sale: SaleTab;
    money: MoneyTab;
  };
}

/* ---------- gates ---------- */
export interface GateItem {
  requirement: string;
  label: string;
  to_state?: string | null;
  ok: boolean;
  detail: string;
  items: string[];
  overridable: boolean;
  factual: boolean;
  param?: Record<string, unknown>;
  source?: string;
  overridden?: { by?: string | null; reason?: string } | null;
  override_refused?: string | null;
}
export interface GatePreview {
  vehicle_id: string;
  from: string;
  to: string;
  backward: boolean;
  gates: GateItem[];
  decision: string;
  reason_required: boolean;
}
export interface GateTaskRef {
  gate: string;
  task_id?: string | null;
  created?: boolean;
  title?: string | null;
}
export interface MoveResult {
  decision: string;
  moved: boolean;
  from: string;
  to: string;
  gates: GateItem[];
  tasks: GateTaskRef[];
  reasons: string[];
  vehicle?: VehicleCore;
}

/* ---------- board ---------- */
export interface BoardColumn {
  state: string;
  label: string;
  vehicles: VehicleListItem[];
  count: number;
}
export interface BoardResp {
  columns: BoardColumn[];
  total: number;
  states?: string[];
  empty_state?: string | null;
}

/* ---------- money (finance) ---------- */
export interface MoneyAmount { amount: string; currency: string }
export interface VehicleMoney {
  vehicle_id: string;
  stock_no?: string | null;
  currency_basis?: string;
  estimated_total?: MoneyAmount | null;
  committed_invoiced?: MoneyAmount | null;
  cash_paid?: MoneyAmount | null;
  remaining_payable?: MoneyAmount | null;
  unmatched_evidence?: { count: number; totals?: Record<string, string> } | null;
  approved_sale_price?: MoneyAmount | null;
  sale_price_label?: string | null;
  sale_id?: string | null;
  sale_status?: string | null;
  estimated_margin?: MoneyAmount | null;
  margin_label?: string | null;
  completeness?: {
    label: string; estimated_lines: number; fx_missing_lines: number; cash_fx_missing: number;
    unmatched_evidence: number; allocations_needing_review: number; notes: string[];
    components?: Record<string, number>;
  } | null;
  by_category?: Record<string, { usd?: MoneyAmount | null; lines: number; fx_missing: number; estimated: number }>;
  lines?: Array<Record<string, unknown>>;
  as_of?: string;
}

/* ---------- intake ---------- */
export type IntakeStatus =
  | "open" | "analyzing" | "analyzed" | "needs_choice" | "needs_info"
  | "applied" | "partially_applied" | "failed" | "abandoned" | "undone" | string;

export const INTAKE_STATUS_LABELS: Record<string, string> = {
  open: "Open", analyzing: "Analyzing", analyzed: "Ready to apply", needs_choice: "Choose the vehicle",
  needs_info: "Needs information", applied: "Saved to AZKT", partially_applied: "Partially saved",
  failed: "Failed", abandoned: "Abandoned", undone: "Undone",
};

export interface IntakeObservation {
  id: string;
  version: number;
  intake_id: string;
  revision: number;
  kind: string;          // condition | request | identifier | milestone | assignee | priority | note
  text: string;
  source: string;        // owner_text | owner_voice | image | proposed_check
  asset_id?: string | null;
  confidence?: string | null;  // stated | observed | uncertain
  field?: string | null;
  value?: string | null;
  applied_kind?: string | null;
  applied_id?: string | null;
  applied_command?: string | null;
  applied_version?: number | null;
  status: string;        // pending | applied | failed | skipped | rejected
  removed: boolean;
  error?: string | null;
  meta?: Record<string, unknown>;
  history?: Array<Record<string, unknown>>;
  created_at?: string | null;
}

export interface IntakeRecord {
  id: string;
  version: number;
  owner_user_id?: string | null;
  channel?: string;
  target_mode: string;      // new | existing | find
  vehicle_id?: string | null;
  candidate_vehicle_ids: string[];
  status: IntakeStatus;
  revision: number;
  text_notes?: string | null;
  transcript?: string | null;
  transcript_asset_ids: string[];
  asset_ids: string[];
  failed_asset_ids: string[];
  failed_uploads: Array<{ upload_id?: string; name?: string; error?: string }>;
  analysis: Record<string, unknown>;
  result: Record<string, unknown>;
  missing_fields: string[];
  last_error?: string | null;
  applied_at?: string | null;
  undone_at?: string | null;
  device_draft: Record<string, unknown>;
  choice: Record<string, unknown>;
  corrections: Array<Record<string, unknown>>;
  applied: Record<string, unknown>;
  complete: boolean;
  label: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface IntakeCandidate {
  vehicle_id: string;
  stock_no?: string | null;
  title?: string | null;
  frame_no_raw?: string | null;
  model_year?: number | null;
  color?: string | null;
  location?: string | null;
  recon_state?: string | null;
  logistics_state?: string | null;
  hero_asset_id?: string | null;
  score?: number | null;
  reasons?: string[];
}

export interface IntakeStatusResp {
  intake: IntakeRecord;
  items: Array<{
    asset_id?: string; upload_id?: string; name?: string | null; original_name?: string | null;
    status: string; saved_to_azkt: boolean; linked_to_vehicle?: boolean; error?: string | null;
    retryable?: boolean; urls?: { thumb?: string } | null; kind?: string;
  }>;
  observations: IntakeObservation[];
  vehicle?: {
    id: string; stock_no?: string | null; title: string; intake_status?: string | null;
    missing_identity_fields: string[]; condition: ConditionBullet[]; condition_version?: number; archived_at?: string | null;
  } | null;
  target: { mode: string; vehicle_id: string | null; label: string };
  saved_to_device: Record<string, unknown>;
  saved_to_azkt: { assets: number; failed: number; applied: boolean };
}

export interface IntakeApplyResult {
  vehicle_id: string | null;
  created: boolean;
  card_created_by_intake?: boolean;
  condition_bullets: Array<{ id?: string; text?: string; source?: string } | string>;
  tasks_created: Array<{ id?: string; title?: string | null; missing_identity?: boolean }>;
  tasks_updated: Array<{ id?: string; title?: string | null }>;
  issues_created: Array<{ id?: string; title?: string | null }>;
  photos_saved: number;
  photos_failed: Array<{ asset_id?: string; upload_id?: string; name?: string | null; error?: string; retryable?: boolean }>;
  missing: string[];
  needs_confirmation: Array<{ field?: string; value?: string; task_id?: string | null } | string>;
  facts: Array<Record<string, unknown>>;
  milestones: Array<Record<string, unknown>>;
  failed_observations: Array<{ observation_id?: string; kind?: string; text?: string; error?: string }>;
  revision?: number;
  stock_no?: string | null;
  title?: string | null;
  intake_status?: string | null;
  condition_version?: number;
  card_path?: string;
}

/* ---------- helpers ---------- */
/** The health label shown next to a vehicle: colour plus text, never colour alone. */
export function vehicleHealth(v: { health?: string | null; exception?: string | null; health_reason?: string | null }): { health: Health; label: string } {
  const h = (v.health || "ok") as Health;
  const labels: Record<Health, string> = { blocked: "Blocked", risk: "Needs attention", ok: "On track", wait: "Waiting" };
  return { health: (["blocked", "risk", "ok", "wait"].includes(h) ? h : "ok") as Health, label: labels[h] || "On track" };
}

export function identityLine(v: { stock_no?: string | null; frame_no_raw?: string | null; allocation?: string | null }): string {
  return [v.stock_no || "No stock number", v.frame_no_raw || "Frame not recorded", allocationLabel(v.allocation)].filter(Boolean).join(" · ");
}
export function allocationLabel(a: string | null | undefined): string {
  if (!a) return "Allocation not recorded";
  const map: Record<string, string> = { inventory: "Inventory", candidate: "Candidate", request: "Request", reserved: "Reserved", sold: "Sold" };
  return map[a] || a.replace(/_/g, " ");
}

/** Situation string from the card, falling back to the state labels. */
export function situationOf(v: { situation?: string | null; states?: VehicleStates }): string {
  if (v.situation) return v.situation;
  if (!v.states) return "Not recorded";
  const parts: string[] = [];
  if (v.states.logistics && !["received", "not_applicable"].includes(v.states.logistics)) parts.push(stateLabel(v.states.logistics));
  parts.push(stateLabel(v.states.recon));
  if (v.states.commercial && v.states.commercial !== "not_listed") parts.push(stateLabel(v.states.commercial));
  if (["conflicted", "missing"].includes(v.states.documents)) parts.push(stateLabel(v.states.documents));
  return parts.join(" · ") || "Not recorded";
}

export function hasDocumentIssue(v: { states?: VehicleStates }): boolean {
  return !!v.states && ["conflicted", "missing"].includes(v.states.documents);
}

export function thumbUrl(assetId: string | null | undefined): string | null {
  return assetId ? `/api/assets/${encodeURIComponent(assetId)}/thumb` : null;
}

/** Unwrap `data` from a CommandResult, tolerating bare payloads. */
export function dataOf<T>(res: { data?: unknown } | null | undefined): T | null {
  return (res?.data as T) ?? null;
}
