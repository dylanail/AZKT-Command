/* Shapes copied from the backend, not invented:
     backend/app/routers/agent.py        chat / threads / status / coverage / missions / runs
     backend/app/agent/manager.py        handle_message() reply keys, thread(), status()
     backend/app/agent/runtime.py        brief() / run_brief() / updates_since()
     backend/app/agent/coverage.py       for_actor()
   Anything the server may omit is optional here; the screen renders "Not recorded" rather than a guess. */

export const AGENT_ROLES = ["manager", "customer_sales", "sourcing", "logistics", "shop", "listings", "finance"] as const;
export type AgentRole = (typeof AGENT_ROLES)[number];

export function isAgentRole(v: string | undefined | null): v is AgentRole {
  return !!v && (AGENT_ROLES as readonly string[]).includes(v);
}

/** Plain-English role cards. Condensed from backend/app/agent/prompts.py ROLE_DESCRIPTIONS. */
export const ROLE_META: Record<AgentRole, { label: string; blurb: string }> = {
  manager: { label: "Manager", blurb: "Priorities, next checks and anything across the business. Reaches every record you can." },
  customer_sales: { label: "Customer & sales", blurb: "Buyer questions, qualifying, reply drafts, sales tasks and aftercare." },
  sourcing: { label: "Sourcing", blurb: "Auction finds, requirement matching, translation and bid packets." },
  logistics: { label: "Logistics", blurb: "Shipping legs, port and storage deadlines, quotes and carriers." },
  shop: { label: "Shop", blurb: "Inspection and recon, parts, assignments, photos and readiness evidence." },
  listings: { label: "Listings", blurb: "Vehicle packages, the website, media and availability." },
  finance: { label: "Finance", blurb: "Ledger and parts matching, payment state, obligations and agreements." },
};

/* ---------- chat ---------- */

/** One record a turn touched: {kind, id, version} (domain/commands.py CommandContext.changed). */
export interface ChangedRef {
  kind?: string | null;
  id?: string | null;
  version?: number | null;
  label?: string | null;
}

/** agent/tools.py _approval_brief. */
export interface ApprovalBrief {
  id?: string | null;
  title?: string | null;
  kind?: string | null;
  status?: string | null;
  version?: number | null;
  expires_at?: string | null;
  review_path?: string | null;
}

/** manager.py needed_input: the question plus what was checked and what is missing. */
export interface NeededInput {
  question?: string | null;
  candidates?: Array<Record<string, unknown>> | null;
  reasons?: string[] | null;
  task?: unknown;
  checked?: unknown;
  missing?: unknown;
}

export interface TurnBlock {
  type: string;
  id?: string | null;
  kind?: string | null;
  label?: string | null;
  status?: string | null;
  asset_id?: string | null;
}

/** The non-streaming POST /api/agent/chat body, and the SSE `done` payload. */
export interface ChatReply {
  text: string;
  status: string;
  mission_id: string | null;
  run_id: string | null;
  cursor: number;
  changed: ChangedRef[];
  approvals: ApprovalBrief[];
  needed_input?: NeededInput | null;
  run_status?: string | null;
  used_model?: boolean | null;
  error?: string | null;
  wrote?: boolean;
  fast_path?: string | null;
  thread_key?: string;
  role?: string;
  context?: Record<string, unknown>;
  reasons?: string[];
  blocks?: TurnBlock[];
  intake_id?: string | null;
  replayed?: boolean;
}

export interface ChatRequest {
  message: string;
  role: AgentRole;
  context: Record<string, unknown>;
  attachments: string[];
  request_id: string;
}

/** GET /api/agent/threads/{role} — web and Telegram turns share one thread. */
export interface ThreadTurn {
  id: string;
  role: string;
  content: string;
  blocks: TurnBlock[];
  channel: string;
  mission_id: string | null;
  run_id: string | null;
  context: Record<string, unknown>;
  state: string;
  at: string | null;
}
export interface ThreadResp {
  thread_key: string;
  role: string;
  turns: ThreadTurn[];
  count: number;
}

/* ---------- status ---------- */
export interface StatusDoing { mission_id: string; outcome: string; status: string; started_at: string | null }
export interface StatusWaiting { mission_id: string; status: string; waiting_on: string | null; next_check_at: string | null }
export interface RoleStatus {
  role: string;
  health: string;
  doing: StatusDoing[];
  waiting: StatusWaiting[];
  counts: { running: number; waiting: number };
}
export interface AgentStatus {
  model: { available: boolean; budget_configured: boolean; over_cap: boolean; day_usd?: number | null; daily_cap_usd?: number | null };
  deterministic_paths: string[];
  roles: RoleStatus[];
  as_of: string;
}

/* ---------- coverage (owner) ---------- */
export interface CoverageCommand {
  command: string;
  tool: string | null;
  action_class: string;
  perm: string | null;
  ui_action: string[];
  covered: boolean;
  excluded_reason?: string | null;
  description: string;
  available?: boolean;
}
export interface CoverageReadTool { tool: string; name: string; perm: string | null; kind: string; description: string; available?: boolean }
export interface CoverageResp {
  policy_version: string;
  commands: CoverageCommand[];
  read_tools: CoverageReadTool[];
  counts: { commands: number; write_tools: number; read_tools: number; ui_commands: number; excluded: number; available_commands?: number };
  ui_commands: string[];
  gaps: string[];
  excluded: Array<{ command: string; reason: string }>;
  complete: boolean;
  actor?: { kind: string; role: string; scope: string; client_id: string | null; client_scopes: string[] };
}

/* ---------- missions and runs ---------- */
export interface MissionUpdate { seq: number; at?: string; state?: string; text?: string; [k: string]: unknown }
export interface MissionBrief {
  id: string;
  outcome: string;
  status: string;
  role: string;
  trigger?: string;
  channel?: string;
  entity_refs?: Array<{ kind?: string; id?: string; label?: string | null }>;
  cursor: number;
  waiting_on?: string | null;
  next_check_at?: string | null;
  result?: Record<string, unknown>;
  created_at?: string | null;
  finished_at?: string | null;
  paused_reason?: string | null;
  version?: number;
}
export interface RunBrief {
  id: string;
  mission_id: string;
  status: string;
  steps_used: number;
  budget_steps: number;
  started_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
  used_model?: boolean;
  model?: string | null;
  result?: Record<string, unknown>;
}

export const TERMINAL_MISSION = new Set(["succeeded", "failed", "cancelled"]);
export const TERMINAL_RUN = new Set(["succeeded", "failed", "cancelled"]);
/** Run states the server treats as "stop streaming": it is resting, not broken. */
export const RESTING_RUN = new Set(["needs_information", "waiting_approval", "waiting_external", "waiting_until"]);

/* ---------- local view models ---------- */
export type ToolTrailStatus = "running" | "ok" | "needs_review" | "blocked" | "error" | string;
export interface ToolTrailItem { id: string; tool: string; status: ToolTrailStatus }

/** One rendered turn: a stored thread turn, or a live one being streamed. */
export interface ChatMessage {
  key: string;
  who: "me" | "agent";
  text: string;
  channel: string;
  at: string | null;
  state: string;
  streaming?: boolean;
  /** Set when the stream or the POST failed; rendered as an honest sentence, never a fake reply. */
  failure?: string | null;
  trail?: ToolTrailItem[];
  approvals?: ApprovalBrief[];
  changed?: ChangedRef[];
  neededInput?: NeededInput | null;
  missionId?: string | null;
  runId?: string | null;
  cursor?: number;
  context?: Record<string, unknown>;
  usedModel?: boolean | null;
  reasons?: string[];
  attachments?: string[];
}

/** Health words for the role cards. Exactly what /api/agent/status reports, never a guess. */
export function healthWords(health: string): { tone: "ok" | "wait" | "risk" | "blocked"; label: string } {
  switch (health) {
    case "ok": return { tone: "ok", label: "Ready" };
    case "over_budget": return { tone: "wait", label: "AI is off (budget)" };
    case "unavailable": return { tone: "wait", label: "Model unavailable" };
    case "setup_blocked": return { tone: "wait", label: "Needs setup" };
    default: return { tone: "wait", label: health.replace(/_/g, " ") };
  }
}

/** The status line under a role card. Built only from what the API returned. */
export function statusLine(s: RoleStatus | undefined): string {
  if (!s) return "No status reported.";
  const running = s.counts?.running ?? 0;
  const waiting = s.counts?.waiting ?? 0;
  if (running && waiting) return `${running} running · ${waiting} waiting`;
  if (running) return `${running} running`;
  if (waiting) return `${waiting} waiting`;
  return "Nothing running";
}
