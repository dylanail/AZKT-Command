/* Shapes copied from the backend serializers so the screen never guesses a key:
   backend/app/services/inbox.py  → serialize_conversation, serialize_message, list_threads, thread_detail, coverage
   backend/app/services/reply.py  → serialize_draft, gather_facts, the pydantic command inputs
   backend/app/services/reply_checks.py → _check()
   backend/app/routers/inbox.py   → the ACTIONS map. */

/* ── filters ───────────────────────────────────────────────────────────── */
export type ThreadFilter = "needs_reply" | "drafts" | "taken_over" | "unmatched" | "all";
export const THREAD_FILTERS: ThreadFilter[] = ["needs_reply", "drafts", "taken_over", "unmatched", "all"];
export const FILTER_LABELS: Record<ThreadFilter, string> = {
  needs_reply: "Needs reply",
  drafts: "Drafts",
  taken_over: "Taken over",
  unmatched: "Unmatched",
  all: "All",
};
export const FILTER_EMPTY: Record<ThreadFilter, string> = {
  needs_reply: "Nothing is waiting on a reply.",
  drafts: "No drafts are open. Prepare one from a thread that needs a reply.",
  taken_over: "Nobody has taken a thread over. AZKT is drafting normally.",
  unmatched: "Every thread is matched to a person AZKT knows.",
  all: "No threads have arrived yet.",
};
export function isThreadFilter(v: string | null): v is ThreadFilter {
  return !!v && (THREAD_FILTERS as string[]).includes(v);
}

/* ── conversation (serialize_conversation) ─────────────────────────────── */
export interface RecordLink {
  kind: string;
  id: string;
  match?: string | null;
  evidence?: { confirmed_by?: string | null; at?: string | null; reason?: string | null } | null;
}

/** Conversation.state — backend STATES tuple. */
export type ThreadState =
  | "needs_reply" | "drafting" | "blocked" | "awaiting_approval" | "replied"
  | "taken_over" | "unmatched" | "archived" | "no_reply_needed";

/** Conversation.classification — backend CLASSIFICATIONS tuple. */
export type Classification =
  | "customer" | "supplier" | "logistics" | "payment" | "newsletter" | "spam"
  | "automated" | "bounce" | "personal" | "unmatched";
export const CLASSIFICATIONS: Classification[] = [
  "customer", "supplier", "logistics", "payment", "newsletter", "spam", "automated", "bounce", "personal", "unmatched",
];
export const CLASSIFICATION_LABELS: Record<string, string> = {
  customer: "Customer", supplier: "Supplier", logistics: "Logistics", payment: "Payment",
  newsletter: "Newsletter", spam: "Suspected spam", automated: "Automated notice",
  bounce: "Bounce", personal: "Personal", unmatched: "Not matched yet",
};

export const STATE_LABELS: Record<string, string> = {
  needs_reply: "Needs reply",
  drafting: "Draft ready",
  blocked: "Draft blocked",
  awaiting_approval: "Waiting for approval",
  replied: "Replied",
  taken_over: "Taken over",
  unmatched: "Not matched",
  archived: "Archived",
  no_reply_needed: "No reply needed",
};

export interface Conversation {
  id: string;
  version: number;
  connection_id: string | null;
  account: string | null;
  channel: string | null;
  provider_thread_id: string | null;
  subject: string | null;
  participants: string[];
  contact_id: string | null;
  contact_match: string | null;
  match_reasons: string[];
  links: RecordLink[];
  classification: string | null;
  classification_reasons: string[];
  classification_source: string | null;
  state: ThreadState | string;
  prior_state: string | null;
  no_reply_reason: string | null;
  triage_reason: string | null;
  sensitivity: string | null;
  language: string | null;
  spam_reason: string | null;
  case_id: string | null;
  takeover_by: string | null;
  takeover_at: string | null;
  last_inbound_at: string | null;
  last_outbound_at: string | null;
  send_decision_version: number | null;
  /** message count (list + detail only) */
  messages: number | null;
  /** draft count (list + detail only) */
  drafts: number | null;
  created_at: string | null;
  updated_at: string | null;
  /** list only: the opening of the newest message, already stripped of quoted text (inbox._snippets). */
  snippet?: string;
  last_message?: LastMessage;
}

/** list_threads → item["last_message"]: the newest message in the thread. */
export interface LastMessage {
  snippet: string;
  direction: "in" | "out" | string | null;
  at: string | null;
}

export interface ThreadListResp {
  items: Conversation[];
  /** The real count for this filter under the caller's scope, not the size of the page. */
  total: number;
  filter: string;
  account: string | null;
  limit?: number;
  offset?: number;
}

/** GET /api/inbox/counts → {filter: n}, over exactly the scope and filters the list uses. */
export type ThreadCounts = Partial<Record<ThreadFilter, number>>;

/* ── message (serialize_message) ───────────────────────────────────────── */
export interface Attachment {
  filename: string;
  mime: string;
  size: number;
  attachment_id?: string | null;
}

export interface Message {
  id: string;
  version: number;
  conversation_id: string;
  connection_id: string | null;
  provider_message_id: string | null;
  provider_thread_id: string | null;
  direction: "in" | "out" | string;
  from: string | null;
  to: string[];
  cc: string[];
  sent_at: string | null;
  subject: string | null;
  snippet: string | null;
  classification: string | null;
  is_automated: boolean | null;
  suppression: string | null;
  attachments: Attachment[];
  extracted: Record<string, unknown>;
  admitted: boolean | null;
  admission_rule: string | null;
  excluded_reason: string | null;
  quarantined_at: string | null;
  personal_allowlisted: boolean | null;
  attribution: string | null;
  sent_by: string | null;
  draft_id: string | null;
  receipt: Record<string, unknown>;
  rfc_message_id: string | null;
  headers: Record<string, unknown>;
  body_text: string | null;
  body_new_text: string | null;
}

/* ── draft (serialize_draft) ───────────────────────────────────────────── */
export interface Check {
  key: string;
  ok: boolean;
  label: string;
  remediation: string;
  blocking: boolean;
  detail: Record<string, unknown>;
}

export interface PlanItem {
  index: number;
  question: string;
  sentence_index?: number;
  kind?: string;
  deadline?: string | null;
  needs_attachment?: boolean;
  topics: string[];
  facts_needed: string[];
  answered: boolean;
  source: Record<string, unknown> | null;
}

export interface DraftFacts {
  as_of?: string;
  account?: { connection_id: string | null; identity: string | null; provider: string | null; freshness?: Freshness };
  contact?: { id: string | null; name: string | null; status: string | null; emails: string[]; opted_out: boolean };
  participants?: string[];
  vehicles?: Array<Record<string, unknown>>;
  prices?: Array<{ amount: string; currency: string; source: string; label: string }> | null;
  shipment?: Record<string, unknown>;
  coverage_gaps?: CoverageGap[];
  language?: string;
  sensitivity?: string | null;
  /** true when the role cannot read costs — prices are stripped server-side. */
  money_hidden?: boolean;
}

/** reply.send_state: the ExternalAction owns the truth about the provider call, the draft is the fallback. */
export type SendState =
  | "not_submitted" | "awaiting_approval" | "approved" | "sending" | "sent"
  | "handed_off" | "failed" | "result_unknown";
export interface DraftSend {
  state: SendState | string;
  external_action_id: string | null;
  receipt: { provider_ref?: string | null };
  at: string | null;
}

/** Draft.status — draft | blocked | pending_approval | approved | sending | sent | invalidated | superseded | declined */
export type DraftStatus =
  | "draft" | "blocked" | "pending_approval" | "approved" | "sending" | "sent"
  | "invalidated" | "superseded" | "declined";

export interface Draft {
  id: string;
  version: number;
  conversation_id: string;
  draft_version: number;
  status: DraftStatus | string;
  to: string[];
  cc: string[];
  subject: string | null;
  body: string | null;
  attachments: unknown[];
  answer_plan: PlanItem[];
  sources: Array<Record<string, unknown>>;
  blocked_reason: string | null;
  approval_id: string | null;
  provider_draft_id: string | null;
  content_hash: string | null;
  invalidated_reason: string | null;
  sent_message_id: string | null;
  our_message_id: string | null;
  external_action_id: string | null;
  supersedes_id: string | null;
  edit_history: unknown[];
  created_by_role: string | null;
  generator: string | null;
  facts: DraftFacts;
  commitments: unknown[];
  send_decision_version: number | null;
  based_on_inbound_id: string | null;
  receipt: Record<string, unknown>;
  sent_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  /** Where the send actually is (reply.send_state). Absent only on a response older than that field. */
  send?: DraftSend | null;
  checks: Check[];
  failing_checks: Check[];
}

export interface DraftVersionsResp {
  items: Draft[];
  total: number;
  conversation_id: string;
  current: Draft;
}

/* ── thread detail (inbox_svc.thread_detail) ───────────────────────────── */
export interface Freshness {
  state: "ok" | "warn" | "degraded" | "expired" | "disconnected" | string;
  label: string;
  last_success_at: string | null;
}

export interface ContextContact {
  id: string;
  name: string | null;
  status: string | null;
  consent: Record<string, unknown> | null;
}

export interface ContextVehicle {
  id: string;
  stock_no: string | null;
  title: string | null;
  commercial_state: string | null;
  allocation: string | null;
  match?: string | null;
  evidence?: Record<string, unknown> | null;
}

export interface ThreadConnection {
  provider: string;
  label: string | null;
  status?: string | null;
  freshness: Freshness;
  account_identity?: string | null;
}

export interface ThreadDetail {
  conversation: Conversation;
  messages: Message[];
  drafts: Draft[];
  context: { contact: ContextContact | null; vehicles: ContextVehicle[]; links: RecordLink[] };
  takeover: { state: boolean; by: string | null; at: string | null; paused: boolean };
  connection: ThreadConnection | null;
}

/* ── coverage (inbox_svc.coverage) ─────────────────────────────────────── */
export interface CoverageGap {
  from: string | null;
  to: string | null;
  kind: string | null;
  detail: string | null;
  at: string | null;
  resolved_at: string | null;
  resolved_by?: string | null;
  note?: string | null;
}

export interface CoverageAccount {
  provider: string;
  label: string;
  connected: boolean;
  status: string;
  freshness: Freshness;
  coverage: { from: string | null; to: string | null };
  gaps: CoverageGap[];
  /** owner-only fields (non-owners get the scrubbed account) */
  account_identity?: string | null;
  gap_history?: CoverageGap[];
  excluded?: { total?: number; by_reason?: Record<string, number>; last_at?: string };
  watch_expires_at?: string | null;
  catch_up?: { pending?: boolean; started_at?: string; reason?: string; processed?: number; completed_at?: string };
  capabilities?: Record<string, unknown>;
  failure?: Record<string, unknown>;
}

export interface CoverageResp {
  accounts: CoverageAccount[];
  all_clear_possible: boolean;
  stale: string[];
}

/* ── record links a thread can carry (inbox.link_record LinkIn.kind) ───── */
export type LinkKind = "vehicle" | "opportunity" | "import_request" | "shipment" | "sale" | "invoice";
export const LINKABLE_KINDS: LinkKind[] = ["vehicle", "opportunity", "import_request", "sale", "shipment", "invoice"];
export const LINK_KIND_LABELS: Record<string, string> = {
  vehicle: "Vehicle",
  opportunity: "Sales lead",
  import_request: "Import request",
  shipment: "Shipment",
  sale: "Sale",
  invoice: "Invoice",
  contact: "Person",
};

/* ── small helpers ─────────────────────────────────────────────────────── */
export function threadTitle(c: Conversation | null | undefined): string {
  if (!c) return "Thread";
  const s = (c.subject || "").trim();
  return s || "(no subject)";
}

/** Who the thread is with: the first participant that is not our own account. */
export function threadWho(c: Conversation | null | undefined): string {
  if (!c) return "";
  const account = (c.account || "").toLowerCase();
  const other = (c.participants || []).find((p) => p && p.toLowerCase() !== account);
  return other || c.participants?.[0] || c.account || "";
}

export function accountLabel(provider: string | null | undefined, account: string | null | undefined): string {
  if (provider === "gmail_personal") return "Personal email";
  if (provider === "gmail_business") return "Business email";
  return account || "Manual entry";
}

/** The draft the reply editor should show: the newest one that is still live. */
export function liveDraft(drafts: Draft[] | undefined): Draft | null {
  if (!drafts || !drafts.length) return null;
  const dead = new Set(["superseded", "invalidated", "declined"]);
  const live = drafts.find((d) => !dead.has(d.status));
  return live || drafts[0];
}

export function blockingFailures(d: Draft | null): Check[] {
  if (!d) return [];
  if (d.failing_checks?.length) return d.failing_checks;
  return (d.checks || []).filter((c) => c.blocking && !c.ok);
}

/* Truthful send state for a submitted draft (spec §4.3 step 8). The server decides it — the draft's own
   status only says why a reply that was never submitted cannot be. */
const SEND_STATES = new Set(["not_submitted", "awaiting_approval", "approved", "sending", "sent", "handed_off", "failed", "result_unknown"]);
/** Why a draft that has not been submitted is not ready: none = it is ready to submit. */
export type DraftSituation = "none" | "blocked" | "declined" | "stale";

export function sendStateOf(d: Draft | null): SendState {
  if (!d) return "not_submitted";
  const s = d.send?.state;
  if (typeof s === "string" && SEND_STATES.has(s)) return s as SendState;
  // Fallback for a response that predates the send block; the same mapping the server uses.
  if (d.status === "sent") return "sent";
  if (d.status === "sending") return "sending";
  if (d.status === "pending_approval") return "awaiting_approval";
  if (d.status === "approved") return "approved";
  return "not_submitted";
}

/** What the draft itself says when nothing has been submitted. */
export function draftSituation(d: Draft | null): DraftSituation {
  if (!d) return "none";
  if (d.status === "declined") return "declined";
  if (d.status === "invalidated" || d.status === "superseded") return "stale";
  if (d.status === "blocked") return "blocked";
  return "none";
}

/** True while the reply is with the owner, the provider, or a person finishing it by hand. */
export function sendInFlight(state: SendState): boolean {
  return state === "awaiting_approval" || state === "approved" || state === "sending" || state === "result_unknown";
}

/** The mailbox reference AZKT actually recorded, if any. */
export function sendReceiptRef(d: Draft | null): string | null {
  const ref = d?.send?.receipt?.provider_ref;
  if (typeof ref === "string" && ref) return ref;
  const legacy = (d?.receipt || {})["message_id"];
  return typeof legacy === "string" && legacy ? legacy : null;
}

export function bytes(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n) || n <= 0) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}
