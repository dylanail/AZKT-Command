import type { HTMLAttributes } from "react";

export type Health = "blocked" | "risk" | "ok" | "wait";

/** The four health colours are always paired with a text label. */
export const HEALTH_LABELS: Record<Health, string> = {
  blocked: "Blocked",
  risk: "Needs attention",
  ok: "On track",
  wait: "Waiting",
};

export function healthColor(h: Health): string {
  return `var(--${h})`;
}

export interface HealthLabelProps extends HTMLAttributes<HTMLSpanElement> {
  health: Health;
  /** Override the default label (e.g. "Overdue", "Awaiting verification"). Always rendered — never colour alone. */
  label?: string;
  size?: "sm" | "md";
  dot?: boolean;
}

export function HealthLabel({ health, label, size = "sm", dot = false, className = "", ...rest }: HealthLabelProps) {
  const cls = ["health", `health--${health}`, size === "md" ? "health--md" : "", className].filter(Boolean).join(" ");
  return (
    <span className={cls} {...rest}>
      {dot ? <span className="health__dot" aria-hidden="true" /> : null}
      {label || HEALTH_LABELS[health]}
    </span>
  );
}

export type DotTone = Health | "amber" | "muted";
export interface StatusDotProps extends HTMLAttributes<HTMLSpanElement> {
  tone: DotTone;
  pulse?: boolean;
  size?: "sm" | "md";
  /** Screen-reader text; pass when the dot is the only indicator. */
  label?: string;
}
export function StatusDot({ tone, pulse = false, size = "md", label, className = "", ...rest }: StatusDotProps) {
  const cls = ["sdot", `sdot--${tone}`, pulse ? "sdot--pulse" : "", size === "sm" ? "sdot--sm" : "", className].filter(Boolean).join(" ");
  return (
    <span className={cls} role={label ? "img" : undefined} aria-label={label} aria-hidden={label ? undefined : true} {...rest}>
      <span className="sdot__core" />
      {pulse ? <span className="sdot__ring" /> : null}
    </span>
  );
}
