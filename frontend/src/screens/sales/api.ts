/* Sales writes: one helper per command so the board, list, lead pane and mobile sheet share the same
   outcome handling. Deposit Paid is never set here — the API blocks it and we relay its reason. */
import { ApiError, command, describeError } from "../../lib/api";
import type { Opportunity, Stage } from "./types";

export type MoveOutcome =
  | { kind: "ok"; opportunity: Opportunity | null; moved: boolean }
  | { kind: "needs_review"; approval_id: string | null; reasons: string[] }
  | { kind: "blocked"; message: string; action?: string; detail: Record<string, unknown> }
  | { kind: "error"; message: string; error: unknown };

const opp = (id: string, action: string) => `/api/sales/opportunities/${encodeURIComponent(id)}/${action}`;

async function runOpp(path: string, body: Record<string, unknown>): Promise<MoveOutcome> {
  try {
    const res = await command<{ opportunity?: Opportunity; moved?: boolean }>(path, body);
    if (res.status === "needs_review") return { kind: "needs_review", approval_id: res.approval_id, reasons: res.decision?.reasons || [] };
    if (res.status === "blocked") return { kind: "blocked", message: res.decision?.reasons?.join("; ") || "Blocked", detail: {} };
    return { kind: "ok", opportunity: res.data?.opportunity ?? null, moved: res.data?.moved ?? true };
  } catch (e) {
    if (e instanceof ApiError && e.code === "blocked") {
      return { kind: "blocked", message: e.message, action: typeof e.detail.action === "string" ? e.detail.action : undefined, detail: e.detail };
    }
    return { kind: "error", message: describeError(e), error: e };
  }
}

/** Move between hand-settable stages. Lost needs a reason; Deposit Paid comes back as blocked with the API's reason. */
export function moveStage(id: string, stage: Stage, opts: { reason?: string; note?: string; expected_version?: number } = {}): Promise<MoveOutcome> {
  if (stage === "lost") return runOpp(opp(id, "mark-lost"), { reason: opts.reason || "", expected_version: opts.expected_version });
  return runOpp(opp(id, "move-stage"), { stage, note: opts.note, expected_version: opts.expected_version });
}
export function reopenLead(id: string, opts: { stage?: Stage; note?: string; expected_version?: number } = {}): Promise<MoveOutcome> {
  return runOpp(opp(id, "reopen"), { stage: opts.stage, note: opts.note, expected_version: opts.expected_version });
}
export function updateLead(id: string, patch: Record<string, unknown>): Promise<MoveOutcome> {
  return runOpp(opp(id, "update"), patch);
}
export function isDepositGate(o: MoveOutcome): boolean {
  return o.kind === "blocked" && (o.action === "record_payment" || /deposit paid/i.test(o.message));
}
