/* Shapes from backend/app/routers/shipping.py and services/shipping.py serializers.
   Leg, quote and booking amounts arrive as null with money_hidden:true for actors without costs.read. */
import type { DualTime } from "../requests/types";

export type { DualTime };

export const SHIPMENT_STATUSES = ["planned", "in_transit", "at_port", "released", "domestic", "received", "complete", "exception"] as const;
export type ShipmentStatus = (typeof SHIPMENT_STATUSES)[number];
export const SHIPMENT_LABEL: Record<string, string> = {
  planned: "Planned", in_transit: "In transit", at_port: "At port", released: "Released",
  domestic: "Domestic leg", received: "Received", complete: "Complete", exception: "Exception",
};
export function shipmentHealth(status: string): "blocked" | "risk" | "ok" | "wait" | null {
  if (status === "exception") return "blocked";
  if (status === "complete" || status === "received") return "ok";
  if (status === "at_port") return "risk";
  return "wait";
}

export const LEG_KINDS = ["export", "ocean", "port", "domestic"] as const;
export const LEG_KIND_LABEL: Record<string, string> = {
  export: "Export (Japan)", ocean: "Ocean", port: "Port / customs", domestic: "Domestic delivery",
};
export const LEG_STATUSES = ["planned", "quoted", "booked", "in_progress", "complete", "cancelled"] as const;
export const LEG_STATUS_LABEL: Record<string, string> = {
  planned: "Planned", quoted: "Quoted", booked: "Booked", in_progress: "In progress", complete: "Complete", cancelled: "Cancelled",
};

export const MILESTONE_KINDS = ["vessel_departed", "vessel_arrival", "discharge", "release", "carrier_booked", "pickup", "received"] as const;
export const MILESTONE_LABEL: Record<string, string> = {
  vessel_departed: "Vessel departed", vessel_arrival: "Vessel arrival", discharge: "Discharge",
  release: "Release", carrier_booked: "Carrier booked", pickup: "Pickup", received: "Received",
};
export const MILESTONE_STATUSES = ["planned", "estimated", "completed"] as const;
export const MILESTONE_SOURCE_KINDS = ["manual", "document", "message", "carrier", "port", "provider", "exporter", "customs"] as const;

/** The quote case's status chain, in the order it actually runs. */
export const QUOTE_CHAIN = ["draft", "pending_approval", "requested", "received", "clarifying", "forwarded", "booked"] as const;
export const QUOTE_LABEL: Record<string, string> = {
  draft: "Draft",
  needs_information: "Needs information",
  pending_approval: "Waiting for approval",
  requested: "Requested · waiting for the vendor",
  received: "Reply received",
  clarifying: "Clarifying with the vendor",
  forwarded: "Forwarded to the customer",
  booked: "Booked",
  declined: "Declined",
  expired: "Expired",
};

export interface Shipment {
  id: string;
  version: number;
  ref: string | null;
  status: string;
  vehicle_ids: string[];
  container_no: string | null;
  vessel: string | null;
  voyage: string | null;
  route_from: string | null;
  route_to: string | null;
  exporter_contact_id: string | null;
  eta: DualTime;
  eta_source: string | null;
  storage_deadline: DualTime;
  storage_deadline_source: string | null;
  storage_deadline_source_ref: string | null;
  storage_deadline_note: string | null;
  case_id: string | null;
  notes: string;
  exception_summary: string | null;
  extra: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
  /* list projection */
  latest_milestone?: Milestone | null;
  vehicle_count?: number;
}

export interface VehicleMember {
  id: string; title: string | null; stock_no: string | null;
  logistics_state?: string | null; health?: string | null; photo?: string | null; hero_asset_id?: string | null;
}

export interface Leg {
  id: string;
  version: number;
  shipment_id: string;
  kind: string;
  status: string;
  vehicle_id: string | null;
  carrier_contact_id: string | null;
  carrier_name: string | null;
  driver_contact: string | null;
  booking_ref: string | null;
  booked_at: string | null;
  appointment: DualTime;
  pickup_at: string | null;
  delivered_at: string | null;
  amount: string | null;
  currency: string | null;
  quote_id: string | null;
  approval_id: string | null;
  evidence: unknown[];
  conditions: string;
  route_from: string | null;
  route_to: string | null;
  notes: string;
  cancelled_at: string | null;
  money_hidden?: boolean;
}

export interface Milestone {
  id?: string;
  version?: number;
  shipment_id?: string;
  vehicle_id: string | null;
  kind: string;
  status: string;
  at: DualTime;
  source_kind: string | null;
  source_ref: string | null;
  note?: string | null;
  applies_to?: string;
  exception?: boolean;
  supersedes_id?: string | null;
  is_current?: boolean;
  recorded_at?: string | null;
  scope?: string | null;
}

export interface QuoteComparable {
  quote_id: string; vendor_name: string | null; amount: string | null; currency: string | null;
  received_at: string | null; status: string; service: string | null; operability: string | null;
  excluded_because?: string[];
}
export interface QuoteComparison {
  quote_amount: string | null;
  currency: string | null;
  comparables: QuoteComparable[];
  excluded: QuoteComparable[];
  comparable_count: number;
  evidence: "weak" | "adequate" | string;
  weakness: string[];
  range: { min: string | null; max: string | null } | null;
  position: "above" | "below" | "within" | null;
  recommendation: string;
  threshold: null;
  compared_at: string | null;
}

export interface Quote {
  id: string;
  version: number;
  shipment_id: string | null;
  vehicle_id: string | null;
  case_id: string | null;
  leg_id: string | null;
  buyer_contact_id: string | null;
  vendor_contact_id: string | null;
  vendor_name: string | null;
  status: string;
  route_from: string | null;
  route_to: string | null;
  route_key: string | null;
  service: string | null;
  operability: string | null;
  dimensions: Record<string, unknown>;
  size_class: string | null;
  timing_window: Record<string, unknown>;
  needs_information: Array<{ field: string; reason: string }>;
  request_payload: Record<string, unknown>;
  request_payload_hash: string | null;
  recipients: string[];
  channel: string | null;
  request_approval_id: string | null;
  request_action_id: string | null;
  request_action_state: string | null;
  request_receipt: Record<string, unknown>;
  requested_at: string | null;
  received_at: string | null;
  reply_message_id: string | null;
  amount: string | null;
  currency: string | null;
  binding: string;
  scope: string;
  inclusions: string[];
  exclusions: string[];
  timing: string | null;
  expires_at: string | null;
  reply_extracted: Record<string, unknown>;
  clarification_task_id: string | null;
  comparison: QuoteComparison | Record<string, never>;
  forward_approval_id: string | null;
  forward_action_id: string | null;
  forwarded_at: string | null;
  forward_payload: Record<string, unknown>;
  booking_approval_id: string | null;
  booking_action_id: string | null;
  booked_at: string | null;
  booking: Record<string, unknown>;
  next_check_at: string | null;
  check_count: number;
  extra: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
  money_hidden?: boolean;
}

export interface CaseRef {
  id: string; status: string; waiting_on?: string | null; next_action?: string | null;
  next_check_at: string | null; summary?: string | null;
}
export interface TaskRef { id: string; title: string; status: string; due_at: string | null }

export interface ShipmentDetailResp {
  shipment: Shipment;
  vehicles: VehicleMember[];
  legs: Leg[];
  milestones: { current: Milestone[]; history: Milestone[]; effective: { container: Record<string, Milestone>; per_vehicle: Record<string, Record<string, Milestone>> } };
  release_evidence: { storage_deadline: DualTime; source: string | null; source_ref: string | null; note: string | null };
  quotes: Quote[];
  case: CaseRef | null;
  tasks: TaskRef[];
}
export interface ShipmentListResp { items: Shipment[]; total: number }

/** The data a quote request would share: the recorded facts, narrowed to the chosen fields. */
export const QUOTE_FIELDS = ["vehicle", "dimensions", "operability", "route_from", "route_to", "timing"] as const;
export type QuoteField = (typeof QUOTE_FIELDS)[number];
export const QUOTE_FIELD_LABEL: Record<string, string> = {
  vehicle: "Vehicle (title, year, size class)",
  dimensions: "Dimensions and weight",
  operability: "Running or inoperable",
  route_from: "Origin",
  route_to: "Destination",
  timing: "Requested timing window",
  buyer: "Buyer",
};

export function sharedData(q: Quote, fields: string[]): Record<string, unknown> {
  const facts = q.request_payload || {};
  const out: Record<string, unknown> = {};
  for (const f of fields) {
    const v = (facts as Record<string, unknown>)[f];
    if (v === undefined || v === null || v === "unknown") continue;
    if (typeof v === "object" && !Array.isArray(v) && Object.keys(v as object).length === 0) continue;
    out[f] = v;
  }
  return out;
}

export function quoteStageIndex(status: string): number {
  const i = (QUOTE_CHAIN as readonly string[]).indexOf(status);
  if (i >= 0) return i;
  if (status === "needs_information") return 0;
  return -1;
}

/** ETA and storage deadlines only mean something with their source (spec §8.3). */
export function sourceLabel(source: string | null | undefined, kind = "estimate"): string {
  return source ? `${kind} · ${source}` : `${kind} · source not recorded`;
}
