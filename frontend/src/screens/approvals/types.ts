/* Shapes from backend/app/routers/approvals.py (_row / _detail) and services/approvals.py (_brief). */
import type { Health } from "../../ui";

export type ApprovalStatus = "pending" | "approved" | "queued" | "executing" | "confirmed" | "handed_off" | "failed" | "result_unknown" | "declined" | "expired" | "invalidated" | "canceled";
export const APPROVAL_STATES: ApprovalStatus[] = ["pending", "approved", "queued", "executing", "confirmed", "handed_off", "failed", "result_unknown", "declined", "expired", "invalidated", "canceled"];
export const IN_FLIGHT: ReadonlySet<string> = new Set(["approved", "queued", "executing"]);

export interface ActorRef { kind?: string; user_id?: string | null; display_name?: string | null; role?: string | null; client_name?: string | null; agent_role?: string | null }
export interface ApprovalCheck { key: string; ok: boolean; label: string }
export interface ExternalActionRef { id: string; state: string; provider: string | null; provider_ref: string | null; attempts: number; error: string | null; receipt: Record<string, unknown>; executed_at: string | null }

export interface ApprovalRowData {
  id: string; kind: string; title: string; status: ApprovalStatus | string; version: number; command_name: string;
  consequence: Record<string, unknown>; targets: Record<string, unknown>;
  entity_kind: string | null; entity_id: string | null; requested_by: ActorRef;
  expires_at: string | null; created_at: string | null; decided_at: string | null; review_path: string | null;
  mission_id: string | null; invalidated_reason: string | null; superseded_by_id: string | null; supersedes_id: string | null;
}
export interface ApprovalDetail extends ApprovalRowData {
  payload: Record<string, unknown>; payload_hash: string; checks: ApprovalCheck[]; sources: unknown[];
  receipt: Record<string, unknown> | null; result: Record<string, unknown> | null; authorized_by: string | null;
  external_action_id: string | null; policy_version: string | null;
  action_class: string | null; description: string | null; recommendation: string | null;
  conditions: Record<string, unknown>; record_versions: Record<string, unknown>; executor: string | null; decision_note: string | null;
  run_id: string | null; invalidation: { invalidated: boolean; reason: string | null; superseded_by_id: string | null; supersedes_id: string | null };
  external_action: ExternalActionRef | null; can_decide: boolean; previous_version?: ApprovalRowData;
}
export interface ApprovalListResp { items: ApprovalRowData[]; total: number; limit: number; offset: number }
export interface ApprovalSummary { pending: number; executing: number; unknown: number; failed: number; by_status: Record<string, number> }

export const KIND_LABELS: Record<string, string> = {
  send_message: "Send message", publish: "Publish", bid: "Bid", parts_order: "Parts order", booking: "Booking",
  payment: "Payment", price_change: "Price change", permission: "Permission", quote_request: "Quote request",
  translation_request: "Translation request", fact_overwrite: "Fact change", other: "Action",
};
export function kindLabel(kind: string | null | undefined): string {
  if (!kind) return "Action";
  return KIND_LABELS[kind] || kind.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

/** Truthful state text + colour. `health` null = neutral grey. */
export function statusView(status: string): { label: string; health: Health | null } {
  switch (status) {
    case "pending": return { label: "Needs your decision", health: "wait" };
    case "approved": return { label: "Approved · awaiting execution", health: "wait" };
    case "queued": return { label: "Approved · queued", health: "wait" };
    case "executing": return { label: "Executing", health: "wait" };
    case "confirmed": return { label: "Confirmed", health: "ok" };
    case "handed_off": return { label: "Handed off · a person finishes it", health: "wait" };
    case "failed": return { label: "Failed", health: "blocked" };
    case "result_unknown": return { label: "Result unknown", health: "risk" };
    case "declined": return { label: "Declined", health: null };
    case "expired": return { label: "Expired", health: "risk" };
    case "invalidated": return { label: "Details changed — review again", health: "risk" };
    case "canceled": return { label: "Canceled", health: null };
    default: return { label: status.replace(/_/g, " "), health: null };
  }
}

/** Primary verb for the approve button, by kind. */
export function approveVerb(kind: string | null | undefined): string {
  switch (kind) {
    case "send_message": return "Approve & send";
    case "publish": return "Approve & publish";
    case "bid": return "Approve & bid";
    case "parts_order": return "Approve & order";
    case "booking": return "Approve & book";
    case "payment": return "Approve & pay";
    default: return "Approve & execute";
  }
}

const BODY_FIELDS = ["body", "text", "message", "content", "note", "reply"];
/** The payload field that carries the human-readable message, if any. */
export function bodyFieldOf(payload: Record<string, unknown> | null | undefined): string | null {
  if (!payload) return null;
  for (const k of BODY_FIELDS) if (typeof payload[k] === "string" && (payload[k] as string).length) return k;
  return null;
}

export function actorName(a: ActorRef | null | undefined): string {
  if (!a) return "Not recorded";
  if (a.display_name) return a.display_name;
  if (a.kind === "agent") return a.agent_role ? `AZKT · ${a.agent_role}` : "AZKT";
  if (a.kind === "external") return a.client_name || "External agent";
  if (a.kind === "system") return "AZKT";
  return a.user_id ? `Person ${a.user_id.slice(0, 8)}` : "Not recorded";
}
