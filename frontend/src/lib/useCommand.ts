/* Runs a POST command and explains the CommandResult envelope through toasts:
   ok → success toast · needs_review → "Sent for review" with a Review action · blocked → the reasons.
   ApiError (409 conflict, 422 validation, 403 denied…) becomes a plain-language toast and is rethrown
   so callers can react (e.g. reload after a version conflict). */
import { useCallback, useState } from "react";
import { ApiError, command, describeError, type CommandResult } from "./api";
import { useToast } from "../ui/Toast";

/* The approval dialog host registers itself here so a "Sent for review" toast can open the exact review
   without lib/ importing screens/. Falls back to a full-page navigation. */
let reviewOpener: ((id: string) => void) | null = null;
export function registerReviewOpener(fn: ((id: string) => void) | null) { reviewOpener = fn; }
export function openReview(id: string) {
  if (reviewOpener) reviewOpener(id);
  else window.location.assign(`/approvals/${id}`);
}

export interface RunOptions<T> {
  /** Toast shown on status ok (omit for silent). */
  success?: string;
  /** Called when the command produced an approval instead of acting. */
  onNeedsReview?: (approvalId: string, result: CommandResult<T>) => void;
  /** Called with the plain-language message when the command threw. */
  onError?: (message: string, error: unknown) => void;
  /** Reuse an Idempotency-Key when retrying the exact same command. */
  idempotencyKey?: string;
}

export function useCommand() {
  const { toast } = useToast();
  const [busyKeys, setBusyKeys] = useState<Set<string>>(new Set());

  const run = useCallback(async <T = unknown>(key: string, path: string, body: unknown, opts: RunOptions<T> = {}): Promise<CommandResult<T> | null> => {
    setBusyKeys((s) => new Set(s).add(key));
    try {
      const r = await command<T>(path, body, { idempotencyKey: opts.idempotencyKey });
      if (r.status === "ok") {
        if (opts.success) toast({ message: opts.success, tone: "ok" });
      } else if (r.status === "needs_review") {
        const id = r.approval_id;
        toast({
          title: "Sent for review",
          message: r.decision.reasons.join(" · ") || "This needs an approval before it runs.",
          tone: "wait",
          actions: id ? [{ label: "Review", primary: true, onClick: () => openReview(id) }] : undefined,
        });
        if (id) opts.onNeedsReview?.(id, r);
      } else {
        toast({ title: "Blocked", message: r.decision.reasons.join(" · ") || "A check is blocking this action.", tone: "blocked", duration: 8000 });
      }
      return r;
    } catch (e) {
      const msg = e instanceof ApiError && e.code === "conflict" ? `${describeError(e)} Reload and try again.` : describeError(e);
      toast({ message: msg, tone: e instanceof ApiError && e.isBusinessGate ? "risk" : "blocked", duration: 6000 });
      opts.onError?.(msg, e);
      return null;
    } finally {
      setBusyKeys((s) => { const n = new Set(s); n.delete(key); return n; });
    }
  }, [toast]);

  const busy = useCallback((key: string) => busyKeys.has(key), [busyKeys]);
  return { run, busy, anyBusy: busyKeys.size > 0 };
}
