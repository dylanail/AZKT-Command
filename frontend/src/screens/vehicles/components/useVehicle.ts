/* Vehicle detail data + the commands the detail screen runs.
   GET /api/vehicles/{id} (card, facts, milestones, tasks, recon, parts, files, sale, money)
   GET /api/activity?entity_kind=vehicle&entity_id=… (recent activity; 403 tolerated → hidden)
   GET /api/finance/vehicles/{id}/money (403/404 tolerated → hidden / not available)
   POST /api/shop/vehicles/{id}/move · /api/vehicles/{id}/{action} · /api/shop/... (CommandResult envelope). */
import { useCallback, useMemo, useState } from "react";
import { ApiError, api, command, describeError, type CommandResult } from "../../../lib/api";
import { useQuery } from "../../../lib/useQuery";
import { useToast } from "../../../ui";
import type { GatePreview, VehicleDetailResp, VehicleMoney } from "../types";

export interface ActivityRow {
  id: string;
  at?: string | null;
  what?: string;
  state?: string | null;
  exception?: boolean;
  entity_kind?: string | null;
  entity_id?: string | null;
  actor?: { display_name?: string | null; kind?: string | null; agent_role?: string | null } | null;
}

/** Money is a separate read so a 403 hides only the tab, never the page. */
export type MoneyState =
  | { kind: "loading" }
  | { kind: "hidden" }
  | { kind: "unavailable" }
  | { kind: "ready"; money: VehicleMoney };

export function useVehicle(id: string) {
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);

  const detail = useQuery<VehicleDetailResp | null>(
    (signal) => api.get<VehicleDetailResp | null>(`/api/vehicles/${encodeURIComponent(id)}`, { signal, tolerate: [404] }),
    [id, tick],
  );
  const activity = useQuery<{ items: ActivityRow[] } | null>(
    (signal) => api.get<{ items: ActivityRow[] } | null>(
      `/api/activity?entity_kind=vehicle&entity_id=${encodeURIComponent(id)}&limit=20`,
      { signal, tolerate: [403, 404, 501] },
    ),
    [id, tick],
  );

  return { detail, activity, reload, tick };
}

/** Owner / costs.read money read. Never throws: the caller renders "hidden" or "not available". */
export function useVehicleMoney(id: string, enabled: boolean, tick = 0): MoneyState {
  const q = useQuery<VehicleMoney | null | "denied">(
    async (signal) => {
      if (!enabled) return "denied";
      try {
        return await api.get<VehicleMoney | null>(`/api/finance/vehicles/${encodeURIComponent(id)}/money`, { signal, tolerate: [404, 501] });
      } catch (e) {
        if (e instanceof ApiError && (e.status === 403 || e.status === 401)) return "denied";
        throw e;
      }
    },
    [id, enabled, tick],
  );
  if (!enabled) return { kind: "hidden" };
  if (q.loading) return { kind: "loading" };
  if (q.data === "denied") return { kind: "hidden" };
  if (q.error || q.data === null) return { kind: "unavailable" };
  return { kind: "ready", money: q.data };
}

/** Read-only gate preview for a proposed stage move (no side effects). */
export async function fetchGates(vehicleId: string, toState: string, signal?: AbortSignal): Promise<GatePreview | null> {
  return api.get<GatePreview | null>(
    `/api/shop/vehicles/${encodeURIComponent(vehicleId)}/gates?to_state=${encodeURIComponent(toState)}`,
    { signal, tolerate: [404, 501] },
  );
}

export interface CommandOutcome<T> {
  result: CommandResult<T> | null;
  data: T | null;
  error: unknown;
  ok: boolean;
}

/**
 * POST a vehicle/shop command and explain the envelope: ok → optional toast, needs_review → "Sent for
 * approval", blocked → the reasons. Never swallows a blocked outcome; the caller may also inspect `data`
 * (e.g. the gate list from a blocked move) and show its own dialog with `quiet`.
 */
export function useVehicleCommand() {
  const { toast } = useToast();
  const [busyKey, setBusyKey] = useState<string | null>(null);

  const run = useCallback(async <T = unknown>(
    key: string,
    path: string,
    body: Record<string, unknown> = {},
    opts: { okMessage?: string; quiet?: boolean; quietBlocked?: boolean } = {},
  ): Promise<CommandOutcome<T>> => {
    setBusyKey(key);
    try {
      const res = await command<T>(path, body);
      if (res.status === "needs_review") {
        toast({
          title: "Sent for approval",
          tone: "wait",
          message: res.decision?.reasons?.[0] || "The owner reviews this before it takes effect.",
        });
        return { result: res, data: res.data, error: null, ok: false };
      }
      if (res.status === "blocked") {
        if (!opts.quietBlocked) {
          toast({ title: "Blocked", message: res.decision?.reasons?.join(" · ") || "A check is blocking this action.", tone: "blocked", duration: 8000 });
        }
        return { result: res, data: res.data, error: null, ok: false };
      }
      if (opts.okMessage && !opts.quiet) toast({ message: opts.okMessage, tone: "ok" });
      return { result: res, data: res.data, error: null, ok: true };
    } catch (e) {
      if (!opts.quiet) {
        const msg = e instanceof ApiError && e.code === "conflict" ? `${describeError(e)} Reload and try again.` : describeError(e);
        toast({ message: msg, tone: e instanceof ApiError && e.isBusinessGate ? "risk" : "blocked", duration: 6000 });
      }
      return { result: null, data: null, error: e, ok: false };
    } finally {
      setBusyKey(null);
    }
  }, [toast]);

  return useMemo(() => ({ run, busyKey, busy: (k: string) => busyKey === k }), [run, busyKey]);
}

export const vehiclePath = (id: string, action: string) => `/api/vehicles/${encodeURIComponent(id)}/${action}`;
export const movePath = (id: string) => `/api/shop/vehicles/${encodeURIComponent(id)}/move`;
export const issuePath = (issueId: string, action: string) => `/api/shop/issues/${encodeURIComponent(issueId)}/${action}`;
export const partPath = (partId: string, action: string) => `/api/shop/parts/${encodeURIComponent(partId)}/${action}`;
export const partsPath = (vehicleId: string) => `/api/shop/vehicles/${encodeURIComponent(vehicleId)}/parts`;
export const workOrderPath = (vehicleId: string) => `/api/shop/vehicles/${encodeURIComponent(vehicleId)}/work-orders`;
