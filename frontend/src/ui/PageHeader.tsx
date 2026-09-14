import type { ReactNode } from "react";
import { Link } from "react-router-dom";

export interface Crumb { label: ReactNode; to?: string; }
export interface PageHeaderProps {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  crumbs?: Crumb[];
  /** Renders below the title row (tabs, filters). */
  children?: ReactNode;
  className?: string;
  /** Use h2 when the page already has an h1 (e.g. nested detail). */
  level?: 1 | 2;
}
export function PageHeader({ title, subtitle, actions, crumbs, children, className = "", level = 1 }: PageHeaderProps) {
  const H = level === 1 ? "h1" : "h2";
  return (
    <div className={["stack", className].filter(Boolean).join(" ")} style={{ gap: 12 }}>
      {crumbs?.length ? (
        <nav className="crumbs" aria-label="Breadcrumb">
          {crumbs.map((c, i) => (
            <span key={i} className="row" style={{ gap: 6 }}>
              {c.to ? <Link to={c.to}>{c.label}</Link> : <span className="truncate" style={{ color: "var(--t2)" }}>{c.label}</span>}
              {i < crumbs.length - 1 ? <span aria-hidden="true">›</span> : null}
            </span>
          ))}
        </nav>
      ) : null}
      <div className="page-head">
        <div className="grow">
          <H>{title}</H>
          {subtitle ? <div className="page-head__sub">{subtitle}</div> : null}
        </div>
        {actions ? <div className="page-head__actions">{actions}</div> : null}
      </div>
      {children}
    </div>
  );
}

/** Section heading with count and optional link, as on Home. */
export function Section({ title, count, link, children }: { title: ReactNode; count?: number | string; link?: ReactNode; children: ReactNode }) {
  return (
    <section>
      <div className="section-title">
        <h2>{title} {count !== undefined ? <span className="count">{count}</span> : null}</h2>
        {link}
      </div>
      {children}
    </section>
  );
}
