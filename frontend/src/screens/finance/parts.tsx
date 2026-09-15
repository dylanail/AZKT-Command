/* Small display pieces shared by the Finance tabs. Every amount goes through <Amt> so a null money
   value reads "Not recorded" instead of 0, and currency always travels with the number. */
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Chip, Money, NotRecorded } from "../../ui";
import { humanize } from "../../lib/links";
import { CATEGORY_LABELS, type CategoryTotal, type Discrepancy, type MoneyV, type Tone } from "./types";

/** One money value with its currency. Null/absent reads as "Not recorded" — never as zero. */
export function Amt({ m, bold, title }: { m: MoneyV | null | undefined; bold?: boolean; title?: string }) {
  if (!m || m.amount === null || m.amount === undefined) return <NotRecorded />;
  return <Money amount={m.amount} currency={m.currency} title={title} style={bold ? { fontWeight: 600 } : undefined} />;
}

/** "1,200.00 USD · 118,000 JPY" for the per-currency totals the tabs return. */
export function MoneyMap({ map, empty = "Nothing outstanding" }: { map: Record<string, string> | null | undefined; empty?: string }) {
  const pairs = Object.entries(map || {});
  if (!pairs.length) return <span className="t3">{empty}</span>;
  return (
    <span className="row-wrap" style={{ gap: 6 }}>
      {pairs.map(([cur, amount], i) => (
        <span key={cur}>
          {i ? <span className="t4"> · </span> : null}
          <Money amount={amount} currency={cur} />
        </span>
      ))}
    </span>
  );
}

export interface KpiSpec { label: ReactNode; value: ReactNode; sub?: ReactNode; tone?: "blocked" | "risk" | "ok" | "wait" }

export function Kpis({ items }: { items: KpiSpec[] }) {
  return (
    <div className="kpis">
      {items.map((k, i) => (
        <div key={i} className={["kpi", k.tone ? `kpi--${k.tone}` : ""].filter(Boolean).join(" ")}>
          <span className="kpi__label">{k.label}</span>
          <span className="kpi__value">{k.value}</span>
          {k.sub ? <span className="kpi__sub">{k.sub}</span> : null}
        </div>
      ))}
    </div>
  );
}

/** Why AZKT proposed this match, in its own words. Never summarised away. */
export function Reasons({ items, title = "Why this match" }: { items: string[] | null | undefined; title?: string }) {
  if (!items || !items.length) return null;
  return (
    <div className="stack-sm" style={{ gap: 4 }}>
      <span className="eyebrow">{title}</span>
      <ul className="fs13 t2" style={{ margin: 0, paddingLeft: 18, lineHeight: 1.55 }}>
        {items.map((r, i) => <li key={i}>{r}</li>)}
      </ul>
    </div>
  );
}

/** A material disagreement between the evidence and the recorded item. Shown, never averaged away. */
export function DiscrepancyBlock({ d }: { d: Discrepancy | null | undefined }) {
  if (!d || !Object.keys(d).length) return null;
  const field = typeof d.field === "string" ? d.field : "value";
  return (
    <div className="fin-conflict" role="note">
      <span className="fin-conflict__title">Conflict on {humanize(field).toLowerCase()} — not averaged</span>
      <dl className="apv-facts fs13" style={{ margin: 0 }}>
        <dt>Evidence says</dt>
        <dd>{String(d.evidence ?? "—")}{d.currency && field === "amount" ? ` ${d.currency}` : ""}</dd>
        <dt>Recorded item</dt>
        <dd>{String(d.item ?? "—")}{d.currency && field === "amount" ? ` ${d.currency}` : ""}</dd>
        {d.difference !== undefined ? (<><dt>Difference</dt><dd>{String(d.difference)}{d.currency ? ` ${d.currency}` : ""}</dd></>) : null}
      </dl>
      <span className="fs12 t3">Confirming asks you which value is right; nothing is picked for you.</span>
    </div>
  );
}

/** Opens the exact source the evidence came from (ledger cell, message, file). */
export function SourceLink({ href, label = "Open source", ref_ }: { href: string | null | undefined; label?: string; ref_?: string | null }) {
  if (!href) return ref_ ? <span className="fs12 t4 truncate" title={ref_}>{ref_}</span> : null;
  if (href.startsWith("/")) return <Link className="fs13" to={href}>{label}</Link>;
  return <a className="fs13" href={href} target="_blank" rel="noreferrer noopener">{label}</a>;
}

export function CategoryChips({ by }: { by: Record<string, CategoryTotal> | null | undefined }) {
  const entries = Object.entries(by || {});
  if (!entries.length) return <span className="t3 fs13">No cost lines</span>;
  return (
    <div className="row-wrap" style={{ gap: 6 }}>
      {entries.map(([cat, t]) => (
        <Chip key={cat} size="sm" tone={t.fx_missing || t.estimated ? "amber" : "soft"}
          title={`${t.lines} line(s)${t.estimated ? ` · ${t.estimated} estimated` : ""}${t.fx_missing ? ` · ${t.fx_missing} without an FX conversion` : ""}`}>
          {CATEGORY_LABELS[cat] || humanize(cat)} <Amt m={t.usd} />
        </Chip>
      ))}
    </div>
  );
}

/** "Estimated · 2 estimated lines · 1 line without FX" — completeness said out loud. */
export function CompletenessLine({ label, parts }: { label: string; parts: Array<string | null | undefined> }) {
  const shown = parts.filter((p): p is string => !!p);
  return (
    <span className="fs12 t3">
      <strong style={{ fontWeight: 600, color: label === "Recorded" ? "var(--ok)" : "var(--risk)" }}>{label}</strong>
      {shown.length ? ` · ${shown.join(" · ")}` : ""}
    </span>
  );
}

export function StateChip({ tone, children, title }: { tone: Tone; children: ReactNode; title?: string }) {
  return <Chip size="sm" tone={tone} title={title}>{children}</Chip>;
}

/** Restatements (late corrections) shown as notes rather than silent edits. */
export function RestatementNotes({ items, title = "Restatements" }: { items: Record<string, unknown>[] | null | undefined; title?: string }) {
  if (!items || !items.length) return null;
  return (
    <div className="stack-sm" style={{ gap: 4 }}>
      <span className="eyebrow">{title} · {items.length}</span>
      <ul className="fs13 t2" style={{ margin: 0, paddingLeft: 18, lineHeight: 1.55 }}>
        {items.map((r, i) => {
          const field = typeof r.field === "string" ? r.field : null;
          const from = r.from ?? r.old_value ?? r.previous;
          const to = r.to ?? r.new_value ?? r.value;
          const note = typeof r.note === "string" ? r.note : typeof r.reason === "string" ? r.reason : null;
          const at = typeof r.at === "string" ? r.at : null;
          return (
            <li key={i}>
              {field ? <>{humanize(field)}: </> : null}
              {from !== undefined ? <>{String(from)} → </> : null}
              {to !== undefined ? <>{String(to)}</> : null}
              {note ? <span className="t3"> — {note}</span> : null}
              {at ? <span className="t4"> ({at.slice(0, 10)})</span> : null}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
