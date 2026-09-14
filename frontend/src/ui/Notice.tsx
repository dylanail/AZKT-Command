import type { ReactNode } from "react";

export interface NoticeProps {
  tone?: "blocked" | "risk" | "ok" | "wait" | "neutral" | "amber";
  /** Leading, coloured phrase ("Awaiting verification"). */
  lead?: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
  role?: "status" | "alert";
  className?: string;
}
/** Inline state banner on detail pages. */
export function Notice({ tone = "neutral", lead, children, action, role = "status", className = "" }: NoticeProps) {
  return (
    <div className={["notice", tone !== "neutral" ? `notice--${tone}` : "", className].filter(Boolean).join(" ")} role={role}>
      <span>
        {lead ? <span className="notice__lead">{lead}</span> : null}
        {lead && children ? " · " : null}
        {children}
      </span>
      {action}
    </div>
  );
}
