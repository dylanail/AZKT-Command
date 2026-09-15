/* Data hooks for the inbox screen: the paged thread list, the per-filter counts that sit on the
   segmented control, and a contact-id → name map so rows can show a person rather than an address. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../lib/api";
import { useQuery } from "../../lib/useQuery";
import { threadsPath } from "./api";
import { THREAD_FILTERS, type Conversation, type ThreadFilter, type ThreadListResp } from "./types";

export const PAGE_SIZE = 50;
/** The list endpoint answers with a page, not a grand total, so counts are capped and shown as "200+". */
export const COUNT_LIMIT = 200;

/** Debounce a fast-changing value (the search box) before it reaches the API. */
export function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const h = window.setTimeout(() => setV(value), ms);
    return () => window.clearTimeout(h);
  }, [value, ms]);
  return v;
}

export interface ThreadsState {
  items: Conversation[];
  loading: boolean;
  loadingMore: boolean;
  error: unknown;
  hasMore: boolean;
  reload: () => void;
  loadMore: () => void;
}

/** Paged list. Changing filter/account/query resets to the first page. */
export function useThreads(filter: ThreadFilter, account: string, q: string, tick: number, enabled = true): ThreadsState {
  const [pages, setPages] = useState<Conversation[][]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [hasMore, setHasMore] = useState(false);
  const [localTick, setLocalTick] = useState(0);
  const ctrl = useRef<AbortController | null>(null);

  // First page (and every reset).
  useEffect(() => {
    ctrl.current?.abort();
    if (!enabled) { setPages([]); setLoading(false); return; }
    const c = new AbortController();
    ctrl.current = c;
    setLoading(true);
    setError(null);
    api.get<ThreadListResp>(threadsPath({ filter, account: account || null, q: q || null, limit: PAGE_SIZE, offset: 0 }), { signal: c.signal })
      .then((r) => {
        if (c.signal.aborted) return;
        setPages([r.items || []]);
        setHasMore((r.items || []).length >= PAGE_SIZE);
        setLoading(false);
      })
      .catch((e) => { if (!c.signal.aborted) { setError(e); setLoading(false); } });
    return () => c.abort();
  }, [filter, account, q, tick, localTick, enabled]);

  const loadMore = useCallback(() => {
    const offset = pages.reduce((n, p) => n + p.length, 0);
    setLoadingMore(true);
    api.get<ThreadListResp>(threadsPath({ filter, account: account || null, q: q || null, limit: PAGE_SIZE, offset }))
      .then((r) => {
        const items = r.items || [];
        setPages((p) => [...p, items]);
        setHasMore(items.length >= PAGE_SIZE);
      })
      .catch(() => setHasMore(false))
      .finally(() => setLoadingMore(false));
  }, [pages, filter, account, q]);

  const items = useMemo(() => {
    const seen = new Set<string>();
    const out: Conversation[] = [];
    for (const page of pages) for (const it of page) if (!seen.has(it.id)) { seen.add(it.id); out.push(it); }
    return out;
  }, [pages]);

  return { items, loading, loadingMore, error, hasMore, reload: () => setLocalTick((t) => t + 1), loadMore };
}

/** Counts for the segmented control: one bounded page per filter, refreshed with the same scope. */
export function useFilterCounts(account: string, q: string, tick: number, enabled = true): Partial<Record<ThreadFilter, number>> {
  const [counts, setCounts] = useState<Partial<Record<ThreadFilter, number>>>({});
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    const c = new AbortController();
    Promise.all(THREAD_FILTERS.map(async (f) => {
      try {
        const r = await api.get<ThreadListResp>(threadsPath({ filter: f, account: account || null, q: q || null, limit: COUNT_LIMIT }), { signal: c.signal });
        return [f, (r.items || []).length] as const;
      } catch {
        return [f, undefined] as const;
      }
    })).then((rows) => {
      if (!alive) return;
      const next: Partial<Record<ThreadFilter, number>> = {};
      for (const [f, n] of rows) if (typeof n === "number") next[f] = n;
      setCounts(next);
    });
    return () => { alive = false; c.abort(); };
  }, [account, q, tick, enabled]);
  return counts;
}

/** contact_id → name, so a row can name a person instead of an address. Tolerates a role without contacts.read. */
export function useContactNames(enabled: boolean): Record<string, string> {
  const q = useQuery<{ items?: Array<{ id: string; name?: string | null }> } | null>(
    (signal) => (enabled
      ? api.get<{ items?: Array<{ id: string; name?: string | null }> } | null>("/api/contacts?limit=500", { signal, tolerate: [403, 404] })
      : Promise.resolve(null)),
    [enabled],
  );
  return useMemo(() => {
    const out: Record<string, string> = {};
    for (const c of q.data?.items || []) if (c.id && c.name) out[c.id] = c.name;
    return out;
  }, [q.data]);
}
