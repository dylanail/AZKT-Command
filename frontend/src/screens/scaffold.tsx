/* Shared scaffold for stage-one screens: title, optional probe of the endpoint the screen
   will use, and a truthful empty state. Screen builders replace the body next stage. */
import type { ReactNode } from "react";
import { api } from "../lib/api";
import { useQuery } from "../lib/useQuery";
import { EmptyState, ErrorState, GlassPanel, Loading, PageHeader, type Crumb } from "../ui";

export interface ScaffoldProps {
  title: ReactNode;
  subtitle?: ReactNode;
  crumbs?: Crumb[];
  actions?: ReactNode;
  /** GET endpoint to probe (404/501 tolerated → empty state). */
  probe?: string;
  emptyTitle?: ReactNode;
  emptyBody?: ReactNode;
  /** Rendered above the list panel (tabs, filters). */
  toolbar?: ReactNode;
  /** Rendered when data arrives (count etc). */
  children?: (data: unknown) => ReactNode;
  wide?: boolean;
}

export function useProbe(path: string | undefined) {
  return useQuery<unknown>(async (signal) => (path ? api.get<unknown>(path, { signal, tolerate: [404, 501] }) : null), [path]);
}

export function countOf(data: unknown): number | null {
  if (Array.isArray(data)) return data.length;
  if (data && typeof data === "object") {
    const o = data as Record<string, unknown>;
    for (const k of ["items", "results", "rows", "data"]) if (Array.isArray(o[k])) return (o[k] as unknown[]).length;
    if (typeof o.count === "number") return o.count;
  }
  return null;
}

export function Scaffold({ title, subtitle, crumbs, actions, probe, emptyTitle = "Nothing waiting", emptyBody, toolbar, children, wide }: ScaffoldProps) {
  const q = useProbe(probe);
  const n = countOf(q.data);
  return (
    <div className={["page", wide ? "page-wide" : ""].filter(Boolean).join(" ")}>
      <PageHeader title={title} subtitle={subtitle} crumbs={crumbs} actions={actions}>{toolbar}</PageHeader>
      <GlassPanel clip>
        {q.loading ? <Loading label={`Loading ${typeof title === "string" ? title.toLowerCase() : ""}`} /> :
          q.error ? <ErrorState error={q.error} onRetry={q.reload} /> :
          q.data === null || n === 0 ? <EmptyState title={emptyTitle} body={emptyBody ?? (q.data === null ? "This list fills in once its data source is connected." : undefined)} /> :
          children ? children(q.data) : <EmptyState title={`${n ?? ""} loaded`} body="The screen builder renders these next stage." />}
      </GlassPanel>
    </div>
  );
}
