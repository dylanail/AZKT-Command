import { useCallback, useEffect, useRef, useState, type DependencyList } from "react";

export interface QueryState<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  reload: () => void;
}

/**
 * Minimal data hook for screens: runs `fn` on mount and when deps change, cancels stale results,
 * exposes reload for ErrorState. Screens pass `tolerate:[404]` in the fetch while endpoints land.
 */
export function useQuery<T>(fn: (signal: AbortSignal) => Promise<T>, deps: DependencyList = []): QueryState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const ctrl = useRef<AbortController | null>(null);

  useEffect(() => {
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    setLoading(true);
    setError(null);
    fn(c.signal)
      .then((d) => { if (!c.signal.aborted) { setData(d); setLoading(false); } })
      .catch((e) => { if (!c.signal.aborted) { setError(e); setLoading(false); } });
    return () => c.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload };
}
