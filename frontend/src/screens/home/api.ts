/* Home reads. Every path here exists in backend/app/routers/home.py — nothing is invented.
   GET /api/home?period=&start=&end=&tz=&horizon_days=   the whole page, one request
   GET /api/home/metrics/drilldown?metric=&period=&start=&end=&tz=   contributing records for one metric
   Writes: none. Home is read-only (the router is GET-only on purpose). */
import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../../lib/api";
import { TZ } from "../../lib/format";
import type { DrilldownResp, FinanceFilters, HomeResp, PeriodKind } from "./types";

export interface PeriodQuery { period: PeriodKind; start?: string | null; end?: string | null }

function periodParams(p: PeriodQuery, tz: string): URLSearchParams {
  const q = new URLSearchParams();
  q.set("period", p.period);
  if (p.period === "custom") {
    if (p.start) q.set("start", p.start);
    if (p.end) q.set("end", p.end);
  }
  q.set("tz", tz);
  return q;
}

export function homePath(p: PeriodQuery, tz: string, horizonDays: number): string {
  const q = periodParams(p, tz);
  q.set("horizon_days", String(horizonDays));
  return `/api/home?${q.toString()}`;
}

export function drilldownPath(metric: string, p: PeriodQuery, tz: string): string {
  const q = periodParams(p, tz);
  q.set("metric", metric);
  return `/api/home/metrics/drilldown?${q.toString()}`;
}

export function fetchHome(p: PeriodQuery, tz: string, horizonDays: number, signal?: AbortSignal): Promise<HomeResp> {
  return api.get<HomeResp>(homePath(p, tz, horizonDays), { signal });
}

export function fetchDrilldown(metric: string, p: PeriodQuery, tz: string, signal?: AbortSignal): Promise<DrilldownResp> {
  return api.get<DrilldownResp>(drilldownPath(metric, p, tz), { signal });
}

/** A custom period needs both ends; until then the request would be a 422, so we don't send it. */
export function periodReady(p: PeriodQuery): boolean {
  return p.period !== "custom" || (!!p.start && !!p.end);
}

/* ── Finance deep links ──────────────────────────────────────────────────────
   The drill-down hands back the API path Finance reads for the identical cohort. Map it to the Finance
   tab that renders that path and carry the period, so Costs / Profit open the same records (K01). */
const FINANCE_TAB: Record<string, string> = {
  "/api/finance/sold-cohort": "sold",
  "/api/finance/unsold-inventory": "vehicles",
  "/api/finance/vehicle-costs": "vehicles",
  "/api/finance/payments": "receivables",
  "/api/finance/needs-matching": "matching",
};

export function financeHref(f: FinanceFilters | null | undefined): string {
  if (!f || !f.path) return "/finance";
  const tab = FINANCE_TAB[f.path];
  if (!tab) return "/finance";
  const q = new URLSearchParams({ tab });
  if (f.period) q.set("period", f.period);
  if (f.from) q.set("from", f.from.slice(0, 10));
  if (f.to) q.set("to", f.to.slice(0, 10));
  return `/finance?${q.toString()}`;
}

/* ── people names ────────────────────────────────────────────────────────────
   Attention groups, today's work and in-progress rows carry owner ids only. GET /api/team names them;
   a role without access gets 403 and the rows keep an honest "Assigned" / "Unassigned" instead. */
export interface Person { id: string; display_name: string; handle?: string; role?: string; status?: string }

interface TeamCache { at: number; people: Person[] }
let teamCache: TeamCache | null = null;
let teamInflight: Promise<TeamCache> | null = null;
const TEAM_TTL = 60_000;

async function loadTeam(): Promise<TeamCache> {
  if (teamCache && Date.now() - teamCache.at < TEAM_TTL) return teamCache;
  if (teamInflight) return teamInflight;
  teamInflight = (async () => {
    try {
      const r = await api.get<{ items?: Person[] } | null>("/api/team", { tolerate: [403, 404, 501] });
      const people = (r?.items || [])
        .filter((p) => p && typeof p.id === "string")
        .map((p) => ({ ...p, display_name: p.display_name || p.handle || p.id }));
      teamCache = { at: Date.now(), people };
    } catch (e) {
      if (!(e instanceof ApiError)) throw e;
      teamCache = { at: Date.now(), people: [] };
    } finally {
      teamInflight = null;
    }
    return teamCache as TeamCache;
  })();
  return teamInflight;
}

/** nameOf(id): "You" · the person's name · "Assigned" when the name is not visible to this role. */
export function usePeopleNames(enabled = true, meId?: string | null) {
  const [people, setPeople] = useState<Person[]>(() => teamCache?.people || []);
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    void loadTeam().then((c) => { if (alive) setPeople(c.people); });
    return () => { alive = false; };
  }, [enabled]);
  return useCallback((id: string | null | undefined): string => {
    if (!id) return "Unassigned";
    if (meId && id === meId) return "You";
    const p = people.find((x) => x.id === id);
    return p ? p.display_name : "Assigned";
  }, [people, meId]);
}
