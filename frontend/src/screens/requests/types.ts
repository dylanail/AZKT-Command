/* Shapes from backend/app/routers/requests.py, routers/candidates.py and services/sourcing.py serializers.
   Money fields arrive as null with money_hidden:true for actors without costs.read. */

/* ---------- lifecycle ---------- */
export const LIFECYCLE = ["inquiry", "qualification", "deposit_pending", "active_search", "purchased", "delivered", "closed"] as const;
export type Lifecycle = (typeof LIFECYCLE)[number];

export const LIFECYCLE_LABEL: Record<string, string> = {
  inquiry: "Inquiry",
  qualification: "Qualification",
  deposit_pending: "Deposit pending",
  active_search: "Active search",
  purchased: "Purchased",
  delivered: "Delivered",
  closed: "Closed",
};

/** Board columns in the fixed spec order; the last column holds both end states. */
export const BOARD_COLUMNS: { key: string; label: string; statuses: Lifecycle[] }[] = [
  { key: "inquiry", label: "Inquiry", statuses: ["inquiry"] },
  { key: "qualification", label: "Qualification", statuses: ["qualification"] },
  { key: "deposit_pending", label: "Deposit pending", statuses: ["deposit_pending"] },
  { key: "active_search", label: "Active search", statuses: ["active_search"] },
  { key: "purchased", label: "Purchased", statuses: ["purchased"] },
  { key: "done", label: "Delivered / Closed", statuses: ["delivered", "closed"] },
];

export const OPEN_STATUSES: Lifecycle[] = ["inquiry", "qualification", "deposit_pending", "active_search"];

/* ---------- time ---------- */
/** services/sourcing.dual_time: one instant, rendered in UTC + Tokyo + Phoenix with its source. */
export interface DualTime {
  utc: string | null;
  tokyo: string;
  phoenix: string;
  /** services/shipping._dual returns the short form (utc + both zones) with no iso/source. */
  tokyo_iso?: string | null;
  phoenix_iso?: string | null;
  source?: string | null;
}

/* ---------- requirements & gates ---------- */
export type Tier = "must" | "prefer" | "avoid";
export const TIERS: Tier[] = ["must", "prefer", "avoid"];
export const TIER_LABEL: Record<Tier, string> = { must: "Must have", prefer: "Prefer", avoid: "Avoid" };
export const OPS = ["eq", "ne", "in", "not_in", "gte", "lte", "gt", "lt", "contains", "truthy", "falsy"] as const;
export const OP_LABEL: Record<string, string> = {
  eq: "is", ne: "is not", in: "is one of", not_in: "is none of", gte: "at least", lte: "at most",
  gt: "more than", lt: "less than", contains: "contains", truthy: "is present", falsy: "is absent",
};

export interface Requirement {
  key: string;
  tier: Tier;
  text: string;
  field?: string | null;
  op?: string | null;
  value?: unknown;
  note?: string | null;
  source_ref?: string | null;
}
export interface RequirementTiersMap { must: Requirement[]; prefer: Requirement[]; avoid: Requirement[] }

export interface Gate { key: string; ok: boolean; status: string; reason: string | null }
export interface GateDecision { decision: "Allowed" | "Blocked" | string; gates: Gate[]; reasons: (string | null)[] }

export const GATE_LABEL: Record<string, string> = {
  agreement: "Search agreement signed, with evidence",
  deposit: "Deposit confirmed against the recorded rule",
  requirements: "Requirements usable for matching",
};

/* ---------- import request ---------- */
export interface ContactBrief { id: string; name: string; company: string | null; status?: string | null }
export interface VehicleBrief {
  id: string; title: string | null; stock_no: string | null; logistics_state?: string | null;
  recon_state?: string | null; commercial_state?: string | null; health?: string | null;
  photo?: string | null; hero_asset_id?: string | null;
}
export interface Exclusion {
  candidate_id: string; kind: string; reason: string; at: string | null; requirements_version?: number | null;
}
export interface DepositRule { amount?: string | null; currency?: string | null; source_ref?: string | null; set_at?: string | null }
export interface EvidenceBag { [k: string]: unknown }

export interface ImportRequest {
  id: string;
  version: number;
  contact_id: string;
  opportunity_id: string | null;
  title: string;
  status: Lifecycle | string;
  lifecycle: string[];
  paused: boolean;
  paused_reason: string | null;
  paused_at: string | null;
  requirements: Requirement[];
  requirement_tiers: RequirementTiersMap;
  requirements_version: number;
  requirements_history: Array<Record<string, unknown>>;
  budget_amount: string | null;
  budget_currency: string | null;
  agreement_id: string | null;
  agreement_status: "none" | "sent" | "signed" | string;
  agreement_evidence: EvidenceBag;
  deposit_rule: DepositRule;
  deposit_status: "unset" | "pending" | "partial" | "confirmed" | "refunded" | string;
  deposit_evidence: EvidenceBag;
  deposit_confirmed_at: string | null;
  deposit_invoice_id: string | null;
  active_search_gate: GateDecision;
  gates?: GateDecision;
  purchased_vehicle_id: string | null;
  purchase_evidence: EvidenceBag;
  purchased_at: string | null;
  delivered_at: string | null;
  closed_at: string | null;
  close_reason: string | null;
  exclusions: Exclusion[];
  notes: string;
  next_check_at: string | null;
  lifecycle_history: Array<Record<string, unknown>>;
  created_at: string | null;
  updated_at: string | null;
  money_hidden?: boolean;
  /* list projection only */
  contact?: ContactBrief | null;
  candidate_counts?: { candidates: number; bid_ready: number; rejected: number };
  lifecycle_index?: number | null;
}

export interface RequestListResp { items: ImportRequest[]; total: number; lifecycle: string[] }

/* ---------- candidates ---------- */
export interface Candidate {
  id: string;
  version: number;
  provider: string | null;
  auction_house: string | null;
  lot_no: string | null;
  identity: string;
  auction_at: DualTime;
  deadline_at: DualTime;
  deadline_passed: boolean;
  source_url: string | null;
  title: string | null;
  frame_raw: string | null;
  specs: Record<string, unknown>;
  spec_sources: Record<string, string>;
  snapshot: Record<string, unknown>;
  snapshot_version: number | null;
  snapshot_hash: string | null;
  images: string[];
  status: string;
  discovered_at: string | null;
  last_seen_at: string | null;
  ingest_count: number | null;
  vehicle_id: string | null;
  result: Record<string, unknown>;
  match_summary?: { requests: number; bid_ready: number; rejected: number };
}

export type CheckResult = "pass" | "fail" | "unknown";
export interface CheckEvidence {
  field?: string | null; op?: string | null; expected?: unknown; observed?: unknown; source?: string | null; reason?: string | null;
}
export interface Check { key: string; tier: Tier; text: string | null; result: CheckResult; evidence: CheckEvidence }

export interface BuyerDraft {
  id: string; version: number; body: string; created_at: string | null; sent_at: string | null;
  invalidated?: boolean; invalidated_reason?: string | null; message_ref?: string | null; sources?: string[];
  translation_revision_no?: number | null; requirements_version?: number | null;
}

export interface CandidateMatch {
  id: string;
  version: number;
  candidate_id: string;
  import_request_id: string;
  requirements_version: number;
  outcomes: Check[];
  score: number | null;
  preference_score: number | null;
  mandatory_fail: boolean;
  mandatory_unknown: boolean;
  bid_ready: boolean;
  status: string;
  rejected_reason: string | null;
  needs_confirmation: string[];
  stale: boolean;
  stale_reason: string | null;
  evaluated_at: string | null;
  translation_id: string | null;
  translation_revision_no: number | null;
  bid_id: string | null;
  buyer_draft_id: string | null;
  buyer_message_sent_at: string | null;
  interest?: Record<string, unknown> | null;
  buyer_draft?: BuyerDraft | null;
  checks: Check[];
  decision: "Allowed" | "Needs review" | "Blocked" | string;
  /* request-detail projection */
  candidate?: Candidate | null;
  /* candidate-detail projection: this request only, never another buyer's terms */
  request?: { id: string; title: string | null; status: string | null; requirements_version: number | null };
}

/* ---------- translations ---------- */
export interface TranslationExcerpt { section?: string; ja?: string; en?: string; [k: string]: unknown }
export interface Translation {
  id: string;
  version: number;
  candidate_id: string;
  status: "draft" | "pending_approval" | "requested" | "detected" | "incomplete" | "complete" | "revised" | "invalidated" | string;
  request_channel: string | null;
  request_action_id: string | null;
  request_action_state: string | null;
  request_receipt: Record<string, unknown>;
  manual_task_id: string | null;
  requested_at: string | null;
  request_content: string | null;
  approval_id: string | null;
  doc_provider: string | null;
  doc_ref: string | null;
  doc_revision: string | null;
  doc_modified_at: string | null;
  revision_no: number | null;
  required_sections: string[];
  completeness: Record<string, unknown>;
  identity_check: Record<string, unknown>;
  excerpts: TranslationExcerpt[];
  findings: Record<string, unknown>;
  completed_at: string | null;
  last_checked_at: string | null;
  revision_history: Array<Record<string, unknown>>;
}

export const TRANSLATION_LABEL: Record<string, string> = {
  draft: "Draft", pending_approval: "Waiting for approval", requested: "Requested", detected: "Document detected",
  incomplete: "Incomplete", complete: "Complete", revised: "Revised", invalidated: "Invalidated",
};

/* ---------- bids ---------- */
export interface BidPacket {
  auction?: Record<string, unknown>;
  deadline?: DualTime;
  max?: { amount?: string; currency?: string };
  fee_basis?: string;
  fx_estimate?: { rate?: string; source?: string; date?: string; usd?: string; status?: string; note?: string };
  buyer_requirements?: Record<string, unknown>;
  translation?: Record<string, unknown>;
  agreement?: Record<string, unknown>;
  deposit?: Record<string, unknown>;
  disclosures?: string[];
  note?: string | null;
}
export interface Bid {
  id: string;
  version: number;
  candidate_id: string;
  import_request_id: string | null;
  max_amount: string | null;
  currency: string | null;
  fee_basis: string | null;
  fx_estimate: Record<string, unknown> | null;
  deadline_at: DualTime;
  auction_house: string | null;
  lot_no: string | null;
  auction_at: DualTime;
  packet: BidPacket | null;
  packet_hash: string | null;
  approval_id: string | null;
  status: "draft" | "pending_approval" | "approved" | "submitted" | "won" | "lost" | "invalidated" | "cancelled" | "expired" | string;
  submission_channel: string | null;
  submission_task_id: string | null;
  submitted_at: string | null;
  result: Record<string, unknown>;
  result_at: string | null;
  invalidated_reason: string | null;
  translation_revision_no: number | null;
  requirements_version: number | null;
  money_hidden?: boolean;
  gate?: { decision: string; reasons: string[] };
}

export const BID_LABEL: Record<string, string> = {
  draft: "Draft packet", pending_approval: "Waiting for owner approval", approved: "Approved · awaiting submission",
  submitted: "Submitted to the auction", won: "Won", lost: "Lost", invalidated: "Details changed — prepare again",
  cancelled: "Cancelled", expired: "Expired",
};

export interface ApprovalRef {
  id: string; status: string; title: string; version: number; entity_id: string;
  invalidated_reason: string | null; review_path: string | null; expires_at?: string | null;
}

export interface ActivityRow {
  id: string; at: string; what: string; kind: string | null; state: string | null;
  entity_kind: string | null; entity_id: string | null; exception: boolean;
  actor?: Record<string, unknown> | null; sources?: unknown;
}

export interface TaskRef { id: string; title: string; status: string; due_at: string | null; owner_user_id?: string | null }

export interface RequestDetailResp {
  request: ImportRequest;
  contact: ContactBrief | null;
  purchased_vehicle: VehicleBrief | null;
  opportunity: Record<string, unknown> | null;
  candidates: CandidateMatch[];
  candidate_ranking: CandidateMatch[];
  translations: Translation[];
  bids: Bid[];
  bid_approvals: ApprovalRef[];
  tasks: TaskRef[];
  activity: ActivityRow[];
  inbox?: { conversations: unknown[]; note?: string };
}

export interface CandidateDetailResp {
  candidate: Candidate;
  checks: CandidateMatch[];
  translations: Translation[];
  current_translation: Translation | null;
  bids: Bid[];
  bid_approvals: ApprovalRef[];
  vehicle_id: string | null;
}

/* ---------- helpers ---------- */
export function lifecycleLabel(status: string | null | undefined): string {
  if (!status) return "Not recorded";
  return LIFECYCLE_LABEL[status] || status.replace(/_/g, " ");
}

export function lifecycleHealth(r: ImportRequest): { health: "blocked" | "risk" | "ok" | "wait"; label: string } | null {
  if (r.paused) return { health: "risk", label: "Paused" };
  if (r.status === "closed") return null;
  if (r.status === "active_search") return { health: "ok", label: "Active search" };
  if (r.status === "purchased" || r.status === "delivered") return { health: "ok", label: lifecycleLabel(r.status) };
  const gate = r.gates || r.active_search_gate;
  if (gate && gate.decision === "Blocked") return { health: "wait", label: "Waiting on the gate" };
  return null;
}

export function agreementLabel(r: ImportRequest): string {
  if (r.agreement_status === "signed") return "Signed";
  if (r.agreement_status === "sent") return "Sent · awaiting signature";
  return "Not sent";
}

/** True when the owner has recorded a deposit rule. The amount itself may be stripped for a role
    without finance visibility, so presence is decided by the rule object, never by the amount. */
export function depositRuleSet(r: ImportRequest): boolean {
  return Object.keys(r.deposit_rule || {}).length > 0 || r.deposit_status !== "unset";
}

export function depositLabel(r: ImportRequest): string {
  switch (r.deposit_status) {
    case "confirmed": return "Confirmed";
    case "partial": return "Partly paid";
    case "pending": return "Pending";
    case "refunded": return "Refunded";
    default: return "Rule not set";
  }
}

export function checkTone(result: CheckResult): "ok" | "blocked" | "risk" {
  return result === "pass" ? "ok" : result === "fail" ? "blocked" : "risk";
}
export const CHECK_LABEL: Record<CheckResult, string> = { pass: "Pass", fail: "Fail", unknown: "Unknown" };

/** Plain-language reason a candidate cannot go to a bid for this request. */
export function bidReadyReason(m: CandidateMatch): string | null {
  if (m.mandatory_fail) return `Fails a must-have: ${(m.checks || []).filter((c) => c.tier === "must" && c.result === "fail").map((c) => c.text || c.key).join(", ") || "recorded facts do not meet it"}`;
  if (m.mandatory_unknown) return `Needs confirmation: ${(m.needs_confirmation || []).join(", ") || "a must-have is still unknown"}`;
  if (m.stale) return m.stale_reason || "Needs re-evaluation against the current requirements";
  if (!m.bid_ready) return "Not bid ready yet";
  return null;
}

export function candidateTitle(c: Candidate | null | undefined): string {
  if (!c) return "Candidate";
  return c.title || [c.auction_house, c.lot_no ? `lot ${c.lot_no}` : null].filter(Boolean).join(" · ") || "Candidate";
}

export function auctionIdentity(c: Candidate | null | undefined): string {
  if (!c) return "Not recorded";
  const bits = [c.auction_house, c.lot_no ? `lot ${c.lot_no}` : null, c.provider].filter(Boolean);
  return bits.length ? bits.join(" · ") : "Not recorded";
}
