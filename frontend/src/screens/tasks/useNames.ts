/* Resolve the records a task points at (contact / opportunity / vehicle) to display names.
   Task rows carry ids only; one list call per kind fills the labels (403/404 tolerated → ids stay). */
import { useEffect, useMemo, useState } from "react";
import { api } from "../../lib/api";

/** Any row that points at a person, a lead or a vehicle by id: tasks, cases and promises all do. */
export interface NameRefs {
  contact_id?: string | null;
  opportunity_id?: string | null;
  vehicle_id?: string | null;
}

export interface VehicleOption { id: string; title?: string | null; stock_no?: string | null; }
interface ContactRow { id: string; name?: string | null; company?: string | null; }
interface OppRow { id: string; card?: { name?: string; subject?: string; pipeline?: string } | null; enquiry?: string | null; pipeline?: string; }

interface Names {
  contacts: Record<string, string>;
  opportunities: Record<string, { name: string; subject: string; pipeline: string }>;
  vehicles: Record<string, string>;
}
const empty: Names = { contacts: {}, opportunities: {}, vehicles: {} };

function listOf<T>(raw: unknown): T[] {
  if (Array.isArray(raw)) return raw as T[];
  const o = raw as { items?: T[] } | null;
  return o && Array.isArray(o.items) ? o.items : [];
}

/** Vehicles list, tolerant of the endpoint not existing yet (→ []). */
export async function fetchVehicleOptions(signal?: AbortSignal): Promise<VehicleOption[] | null> {
  const r = await api.get<unknown>("/api/vehicles?limit=500", { signal, tolerate: [403, 404, 501] });
  if (r === null) return null;
  return listOf<VehicleOption & { model_year?: number; make?: string; model?: string }>(r)
    .filter((v) => v && typeof v.id === "string")
    .map((v) => ({ id: v.id, stock_no: v.stock_no ?? null, title: v.title || [v.model_year, v.make, v.model].filter(Boolean).join(" ") || null }));
}

export function vehicleLabel(v: VehicleOption | null | undefined, fallbackId?: string | null): string {
  if (!v) return fallbackId ? `Vehicle ${fallbackId.slice(0, 8)}` : "Vehicle";
  const t = v.title || "";
  if (t && v.stock_no && !t.includes(v.stock_no)) return `${t} · ${v.stock_no}`;
  return t || v.stock_no || `Vehicle ${v.id.slice(0, 8)}`;
}

export function useNames(tasks: NameRefs[] | null | undefined, opts: { contacts?: boolean; opportunities?: boolean; vehicles?: boolean } = {}) {
  const want = { contacts: true, opportunities: true, vehicles: true, ...opts };
  const [names, setNames] = useState<Names>(empty);
  const needs = useMemo(() => {
    const c = new Set<string>(), o = new Set<string>(), v = new Set<string>();
    for (const t of tasks || []) {
      if (t.contact_id) c.add(t.contact_id);
      if (t.opportunity_id) o.add(t.opportunity_id);
      if (t.vehicle_id) v.add(t.vehicle_id);
    }
    return { c: c.size, o: o.size, v: v.size };
  }, [tasks]);

  useEffect(() => {
    let alive = true;
    const ctrl = new AbortController();
    const run = async () => {
      const next: Names = { contacts: {}, opportunities: {}, vehicles: {} };
      const jobs: Promise<void>[] = [];
      if (want.contacts && needs.c) jobs.push((async () => {
        const r = await api.get<unknown>("/api/contacts?tab=all&limit=500", { signal: ctrl.signal, tolerate: [403, 404] }).catch(() => null);
        for (const c of listOf<ContactRow>(r)) if (c?.id) next.contacts[c.id] = c.name || c.company || c.id.slice(0, 8);
      })());
      if (want.opportunities && needs.o) jobs.push((async () => {
        const r = await api.get<unknown>("/api/sales/opportunities?include_lost=true&limit=500", { signal: ctrl.signal, tolerate: [403, 404] }).catch(() => null);
        for (const o of listOf<OppRow>(r)) if (o?.id) next.opportunities[o.id] = { name: o.card?.name || "Lead", subject: o.card?.subject || o.enquiry || "", pipeline: o.card?.pipeline || o.pipeline || "" };
      })());
      if (want.vehicles && needs.v) jobs.push((async () => {
        const vs = await fetchVehicleOptions(ctrl.signal).catch(() => null);
        for (const v of vs || []) next.vehicles[v.id] = vehicleLabel(v);
      })());
      await Promise.all(jobs);
      if (alive) setNames(next);
    };
    void run();
    return () => { alive = false; ctrl.abort(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [needs.c, needs.o, needs.v]);

  return {
    contactName: (id: string | null | undefined) => (id ? names.contacts[id] || `Contact ${id.slice(0, 8)}` : ""),
    leadName: (id: string | null | undefined) => (id ? names.opportunities[id]?.name || "Lead" : ""),
    leadSubject: (id: string | null | undefined) => (id ? names.opportunities[id]?.subject || "" : ""),
    leadPipeline: (id: string | null | undefined) => (id ? names.opportunities[id]?.pipeline || "" : ""),
    vehicleName: (id: string | null | undefined) => (id ? names.vehicles[id] || `Vehicle ${id.slice(0, 8)}` : ""),
  };
}
