/* Finance rows carry ids; these look up the labels. One list call per kind, 403/404 tolerated so a
   missing name degrades to a short id instead of breaking the tab. */
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../lib/api";
import type { CostItem, Invoice } from "./types";

export interface VehicleRef { id: string; stock_no: string | null; title: string | null }
export interface ContactRef { id: string; name: string }

interface VehicleRow { id?: string; stock_no?: string | null; title?: string | null; model_year?: number; make?: string; model?: string }
interface ContactRow { id?: string; name?: string | null; company?: string | null }

function listOf<T>(raw: unknown): T[] {
  if (Array.isArray(raw)) return raw as T[];
  const o = raw as { items?: T[] } | null;
  return o && Array.isArray(o.items) ? o.items : [];
}

export function vehicleLabel(v: VehicleRef | null | undefined, fallbackId?: string | null): string {
  if (!v) return fallbackId ? `Vehicle ${fallbackId.slice(0, 8)}` : "No vehicle";
  const t = v.title || "";
  if (t && v.stock_no && !t.includes(v.stock_no)) return `${t} · ${v.stock_no}`;
  return t || v.stock_no || `Vehicle ${v.id.slice(0, 8)}`;
}

/** Vehicles and contacts for labels and pickers. Loaded once per mount of the Finance screen. */
export function useRefs(enabled = true) {
  const [vehicles, setVehicles] = useState<VehicleRef[]>([]);
  const [contacts, setContacts] = useState<ContactRef[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (!enabled) return;
    const ctrl = new AbortController();
    let alive = true;
    void (async () => {
      const [vRaw, cRaw] = await Promise.all([
        api.get<unknown>("/api/vehicles?limit=500", { signal: ctrl.signal, tolerate: [403, 404, 501] }).catch(() => null),
        api.get<unknown>("/api/contacts?tab=all&limit=500", { signal: ctrl.signal, tolerate: [403, 404, 501] }).catch(() => null),
      ]);
      if (!alive) return;
      setVehicles(listOf<VehicleRow>(vRaw).filter((v) => typeof v.id === "string").map((v) => ({
        id: v.id as string,
        stock_no: v.stock_no ?? null,
        title: v.title || [v.model_year, v.make, v.model].filter(Boolean).join(" ") || null,
      })));
      setContacts(listOf<ContactRow>(cRaw).filter((c) => typeof c.id === "string").map((c) => ({
        id: c.id as string, name: c.name || c.company || `Contact ${(c.id as string).slice(0, 8)}`,
      })));
      setLoaded(true);
    })();
    return () => { alive = false; ctrl.abort(); };
  }, [enabled]);

  const vehicleById = useMemo(() => {
    const m = new Map<string, VehicleRef>();
    for (const v of vehicles) m.set(v.id, v);
    return m;
  }, [vehicles]);
  const contactById = useMemo(() => {
    const m = new Map<string, string>();
    for (const c of contacts) m.set(c.id, c.name);
    return m;
  }, [contacts]);

  const vehicleName = useCallback(
    (id: string | null | undefined) => (id ? vehicleLabel(vehicleById.get(id), id) : "No vehicle"),
    [vehicleById],
  );
  const contactName = useCallback(
    (id: string | null | undefined) => (id ? contactById.get(id) || `Contact ${id.slice(0, 8)}` : "No contact recorded"),
    [contactById],
  );

  return { vehicles, contacts, vehicleName, contactName, loaded };
}

/** Cost items for the "Correct" picker. Fetched on first use, filtered in the browser. */
export function useCostItems(enabled: boolean) {
  const [items, setItems] = useState<CostItem[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!enabled || items || loading) return;
    const ctrl = new AbortController();
    setLoading(true);
    api.get<{ items: CostItem[] }>("/api/finance/costs?limit=500", { signal: ctrl.signal })
      .then((r) => { setItems(r?.items || []); setLoading(false); })
      // Set items on failure too, so the effect's guard stops it retrying on every render.
      .catch((e) => { if (!ctrl.signal.aborted) { setError(e); setItems([]); setLoading(false); } });
    return () => ctrl.abort();
  }, [enabled, items, loading]);

  return { items, error, loading };
}

/** Open obligations for the allocation picker. */
export function useOpenInvoices(enabled: boolean) {
  const [items, setItems] = useState<Invoice[] | null>(null);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (!enabled || items || loading) return;
    const ctrl = new AbortController();
    setLoading(true);
    api.get<{ items: Invoice[] }>("/api/finance/invoices", { signal: ctrl.signal })
      .then((r) => { setItems((r?.items || []).filter((i) => i.status === "open" || i.status === "partially_paid")); setLoading(false); })
      .catch(() => { if (!ctrl.signal.aborted) { setItems([]); setLoading(false); } });
    return () => ctrl.abort();
  }, [enabled, items, loading]);
  return { items, loading };
}
