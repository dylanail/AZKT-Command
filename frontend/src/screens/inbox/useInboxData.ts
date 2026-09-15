/* Data hooks for the inbox screen: the paged thread list, the per-filter counts that sit on the
   segmented control, and a contact-id → name map so rows can show a person rather than an address. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../lib/api";
import { useQuery } from "../../lib/useQuery";
import { countsPath, threadsPath } from "./api";
import { type Conversation, type ThreadCounts, type ThreadFilter, type ThreadListResp } from "./types";

export const PAGE_SIZE = 50;

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
  /** Everything in this filter under the caller's scope, not just the rows loaded so far. */
  total: number | null;
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
  const [total, setTotal] = useState<number | null>(null);
  const [localTick, setLocalTick] = useState(0);
  const ctrl = useRef<AbortController | null>(null);

  // First page (and every reset).
  useEffect(() => {
    ctrl.current?.abort();
    if (!enabled) { setPages([]); setTotal(null); setLoading(false); return; }
    const c = new AbortController();
    ctrl.current = c;
    setLoading(true);
    setError(null);
    api.get<ThreadListResp>(threadsPath({ filter, account: account || null, q: q || null, limit: PAGE_SIZE, offset: 0 }), { signal: c.signal })
      .then((r) => {
        if (c.signal.aborted) return;
        const items = r.items || [];
        setPages([items]);
        setTotal(typeof r.total === "number" ? r.total : null);
        setHasMore(typeof r.total === "number" ? items.length < r.total : items.length >= PAGE_SIZE);
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
        if (typeof r.total === "number") { setTotal(r.total); setHasMore(offset + items.length < r.total); }
        else setHasMore(items.length >= PAGE_SIZE);
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

  return { items, total, loading, loadingMore, error, hasMore, reload: () => setLocalTick((t) => t + 1), loadMore };
}

/** Counts for the segmented control: one request, counted server-side over the caller's own scope. */
export function useFilterCounts(account: string, q: string, tick: number, enabled = true): ThreadCounts {
  const [counts, setCounts] = useState<ThreadCounts>({});
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    const c = new AbortController();
    api.get<Record<string, number> | null>(countsPath({ account: account || null, q: q || null }), { signal: c.signal, tolerate: [403, 404] })
      .then((r) => {
        if (!alive) return;
        const next: ThreadCounts = {};
        for (const [f, n] of Object.entries(r || {})) if (typeof n === "number") next[f as ThreadFilter] = n;
        setCounts(next);
      })
      .catch(() => { if (alive) setCounts({}); });   // a count that didn't load stays absent, never a guessed 0
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
