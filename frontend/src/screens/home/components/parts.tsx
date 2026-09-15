/* Small pieces every Home section shares: one honest money cell, the section shell (heading + count +
   link + anchor), and the three states a section can arrive in — not permitted, broken, or empty. */
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Button, EmptyState, ErrorState, GlassPanel, Loading, Money, NotRecorded } from "../../../ui";
import type { MoneyV, Unavailable } from "../types";

/** One amount with its currency. Null reads "Not recorded"; a role without costs.read reads "••••". */
export function Amt({ m, hidden, bold, title }: { m?: MoneyV | null; hidden?: boolean; bold?: boolean; title?: string }) {
  if (hidden) return <Money amount={null} currency="USD" hidden title={title || "Amounts are hidden for your role"} />;
  if (!m || m.amount === null || m.amount === undefined) return <NotRecorded />;
  return <Money amount={m.amount} currency={m.currency} title={title} style={bold ? { fontWeight: 600 } : undefined} />;
}

/** The one-line explanation under every metric and row: what it counts, over what period. */
export function Explain({ children }: { children: ReactNode }) {
  return <span className="hm-explain">{children}</span>;
}

export interface HomeSectionProps {
  /** Anchor id — the mobile jump bar scrolls here. */
  id: string;
  title: string;
  count?: number | null;
  countLabel?: string;
  link?: ReactNode;
  children: ReactNode;
}

export function HomeSection({ id, title, count, countLabel, link, children }: HomeSectionProps) {
  return (
    /* tabIndex -1 so the mobile jump bar can move focus, not just the scroll position. */
    <section id={id} className="hm-section" aria-labelledby={`${id}-h`} tabIndex={-1}>
      <div className="section-title">
        <h2 id={`${id}-h`}>
          {title}
          {count !== undefined && count !== null ? (
            <span className="count tnum"> {count}{countLabel ? ` ${countLabel}` : ""}</span>
          ) : null}
        </h2>
        {link}
      </div>
      {children}
    </section>
  );
}

export interface SectionBodyProps {
  section: Unavailable | null | undefined;
  /** First load only — a refresh keeps the previous rows on screen. */
  loading: boolean;
  /** Rows the section holds; 0 renders the empty state. */
  count: number;
  emptyTitle: string;
  emptyBody?: ReactNode;
  emptyAction?: ReactNode;
  onRetry?: () => void;
  loadingLabel?: string;
  loadingRows?: number;
  children: ReactNode;
}

/**
 * The four honest outcomes for one section, decided by the server:
 *  · still loading      → skeleton
 *  · available:false    → the server's reason, said calmly (a permission, not a failure)
 *  · available:false + degraded → this section broke; the rest of the page still renders
 *  · empty              → truthful empty copy
 */
export function SectionBody({
  section, loading, count, emptyTitle, emptyBody, emptyAction, onRetry,
  loadingLabel = "Loading", loadingRows = 2, children,
}: SectionBodyProps) {
  if (loading && !section) {
    return <GlassPanel clip><Loading label={loadingLabel} rows={loadingRows} /></GlassPanel>;
  }
  if (section && section.available === false) {
    if (section.degraded) {
      return (
        <GlassPanel clip>
          <ErrorState
            error={new Error(section.reason || "This section could not be loaded.")}
            title="Couldn't load this section"
            onRetry={onRetry}
          />
        </GlassPanel>
      );
    }
    return (
      <GlassPanel clip>
        <div className="hm-unavailable">{sentence(section.reason) || "This section isn't shown for your role."}</div>
      </GlassPanel>
    );
  }
  if (!count) {
    return <GlassPanel clip><EmptyState title={emptyTitle} body={emptyBody} action={emptyAction} /></GlassPanel>;
  }
  return <>{children}</>;
}

/** The server's reason, capitalised and full-stopped so it reads as a sentence to a person. */
export function sentence(reason: string | null | undefined): string {
  const r = (reason || "").trim();
  if (!r) return "";
  const s = r.charAt(0).toUpperCase() + r.slice(1);
  return /[.!?]$/.test(s) ? s : `${s}.`;
}

/** "Open the queue" affordance on a group — a real link when the API gave one, disabled with a reason otherwise. */
export function QueueLink({ to, label, mobile }: { to: string | null | undefined; label: string; mobile: boolean }) {
  if (!to) {
    return <Button size={mobile ? "xl" : "sm"} variant="soft" disabled disabledReason="This list has no filtered view yet.">{label}</Button>;
  }
  return <Button size={mobile ? "xl" : "sm"} variant="soft" to={to}>{label}</Button>;
}

/** A record reference rendered as a link when we know its route, plain text otherwise. */
export function RecordLink({ href, children }: { href: string | null; children: ReactNode }) {
  if (!href) return <span className="t3">{children}</span>;
  return <Link to={href} className="wrap">{children}</Link>;
}
