import { useEffect, useRef, useState } from "react";

export function StatusDot({ status }: { status?: string }) {
  const cls = status === "ok" ? "ok" : status === "degraded" ? "degraded" : "bad";
  return <span className={`dot ${cls}`} />;
}

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const run = async () => {
    setLoading(true);
    try {
      setData(await fn());
      setErr(null);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, err, loading, reload: run };
}

// Pull-to-refresh: simple touch-driven, mobile-only feel.
export function PullToRefresh({
  onRefresh,
  children,
}: {
  onRefresh: () => Promise<void> | void;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [pull, setPull] = useState(0);
  const start = useRef<number | null>(null);

  return (
    <div
      ref={ref}
      className="scroll"
      onTouchStart={(e) => {
        if (ref.current && ref.current.scrollTop <= 0)
          start.current = e.touches[0].clientY;
      }}
      onTouchMove={(e) => {
        if (start.current == null) return;
        const d = e.touches[0].clientY - start.current;
        if (d > 0) setPull(Math.min(d, 70));
      }}
      onTouchEnd={async () => {
        if (pull > 50) await onRefresh();
        setPull(0);
        start.current = null;
      }}
    >
      <div className={`ptr ${pull > 10 ? "show" : ""}`}>
        {pull > 50 ? "Release to refresh" : "Pull to refresh"}
      </div>
      {children}
    </div>
  );
}

export function money(n: number | undefined) {
  return "$" + (n ?? 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
}
