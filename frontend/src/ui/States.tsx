import type { CSSProperties, ReactNode } from "react";
import { Button } from "./Button";
import { RefreshIcon } from "./Icons";
import { ApiError, describeError } from "../lib/api";

export interface EmptyStateProps {
  /** Truthful, specific copy. Defaults to "Nothing waiting". */
  title?: ReactNode;
  body?: ReactNode;
  action?: ReactNode;
  align?: "center" | "left";
  className?: string;
}
export function EmptyState({ title = "Nothing waiting", body, action, align = "center", className = "" }: EmptyStateProps) {
  return (
    <div className={["empty", align === "left" ? "empty--left" : "", className].filter(Boolean).join(" ")}>
      <span className="empty__title">{title}</span>
      {body ? <span className="empty__body">{body}</span> : null}
      {action ? <div className="empty__action">{action}</div> : null}
    </div>
  );
}

/** Inline value placeholder — never invent a value. */
export function NotRecorded({ text = "Not recorded" }: { text?: string }) {
  return <span className="not-recorded">{text}</span>;
}

export interface SkeletonProps { width?: number | string; height?: number | string; radius?: number | string; style?: CSSProperties; className?: string; }
export function Skeleton({ width = "100%", height = 14, radius = 8, style, className = "" }: SkeletonProps) {
  return <span className={["skeleton", className].filter(Boolean).join(" ")} style={{ width, height, borderRadius: radius, ...style }} aria-hidden="true" />;
}

/** Loading block: a label for screen readers plus shimmer rows. */
export function Loading({ label = "Loading", rows = 3, className = "" }: { label?: string; rows?: number; className?: string }) {
  return (
    <div className={["loading", className].filter(Boolean).join(" ")} role="status" aria-live="polite" aria-busy="true">
      <span className="sr-only">{label}…</span>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="stack-sm" style={{ gap: 6 }}>
          <Skeleton width={`${55 + ((i * 17) % 35)}%`} height={14} />
          <Skeleton width={`${30 + ((i * 23) % 40)}%`} height={11} />
        </div>
      ))}
    </div>
  );
}

/** Full-page loading used by lazy routes. */
export function PageLoading({ title }: { title?: string }) {
  return (
    <div className="page" aria-busy="true">
      {title ? <h1>{title}</h1> : <Skeleton width={220} height={28} radius={10} />}
      <div className="glass glass--clip"><Loading rows={4} /></div>
    </div>
  );
}

export interface ErrorStateProps {
  error: unknown;
  onRetry?: () => void;
  title?: ReactNode;
  retryLabel?: string;
  className?: string;
}
export function ErrorState({ error, onRetry, title, retryLabel = "Try again", className = "" }: ErrorStateProps) {
  const msg = describeError(error);
  const meta = error instanceof ApiError ? [error.code, error.requestId ? `ref ${error.requestId}` : null].filter(Boolean).join(" · ") : null;
  return (
    <div className={["error-state", className].filter(Boolean).join(" ")} role="alert">
      <span className="error-state__title">{title || "Couldn't load this"}</span>
      <span className="error-state__msg">{msg}</span>
      {meta ? <span className="error-state__meta">{meta}</span> : null}
      {onRetry ? <div><Button size="sm" variant="soft" onClick={onRetry} iconLeft={<RefreshIcon />}>{retryLabel}</Button></div> : null}
    </div>
  );
}
