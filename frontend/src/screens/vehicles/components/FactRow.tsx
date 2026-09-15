/* One Overview fact: label · value · status text · Source button.
   Source opens the single right-hand inspector in source-details mode with the fact's provenance
   (observed / effective / record version / fact status / source), and the a/b pair when two sources disagree. */
import { Button } from "../../../ui";
import { formatWhen } from "../../../lib/format";
import { useInspector, type SourceDetail, type SourceRow } from "../../../app/Inspector";
import { FACT_STATUS_LABELS, factLabel, factTone, factValue, type FactData } from "../types";

const toneVar: Record<string, string> = { ok: "var(--ok)", risk: "var(--risk)", wait: "var(--wait)", neutral: "var(--t3)" };

function when(iso: string | null | undefined): string {
  return iso ? formatWhen(iso, { style: "long" }) : "Not recorded";
}

/** "Confirmed · auction sheet · Aug 30" — status, source and when, in plain language. */
export function factStatusLine(f: FactData): string {
  const status = FACT_STATUS_LABELS[f.status] || f.status;
  const src = f.source_kind ? f.source_kind.replace(/_/g, " ") : null;
  const at = f.observed_at ? formatWhen(f.observed_at, { style: "datetime" }) : null;
  return [status, src, at].filter(Boolean).join(" · ");
}

export function factSourceDetail(f: FactData, conflictWith?: FactData | null): SourceDetail {
  const rows: SourceRow[] = [
    { k: "Observed", v: when(f.observed_at) },
    { k: "Effective", v: when(f.effective_at) },
    { k: "Record version", v: `v${f.version}` },
    { k: "Fact status", v: FACT_STATUS_LABELS[f.status] || f.status },
    { k: "Source", v: f.source_kind ? f.source_kind.replace(/_/g, " ") : "Not recorded" },
  ];
  if (f.source_ref) rows.push({ k: "Reference", v: f.source_ref });
  if (f.actor) rows.push({ k: "Recorded by", v: f.actor });
  if (f.confidence_method) rows.push({ k: "How", v: f.confidence_method });
  if (f.critical) rows.push({ k: "Critical field", v: "Owner confirmation required" });
  const tone = factTone(f.status);
  const detail: SourceDetail = {
    k: factLabel(f.key),
    v: factValue(f),
    status: factStatusLine(f),
    statusTone: tone === "neutral" ? undefined : tone,
    rows,
  };
  if (conflictWith) {
    detail.conflict = {
      a: factValue(f),
      aSrc: [f.source_kind?.replace(/_/g, " "), when(f.observed_at)].filter(Boolean).join(" · ") || "Source not recorded",
      b: factValue(conflictWith),
      bSrc: [conflictWith.source_kind?.replace(/_/g, " "), when(conflictWith.observed_at)].filter(Boolean).join(" · ") || "Source not recorded",
    };
  }
  return detail;
}

export interface FactRowProps {
  fact: FactData;
  /** All current facts, used to resolve `conflict_with_id` into the other side of the disagreement. */
  all?: FactData[];
  /** Owner confirmation of a critical fact; omitted → the inspector's Verify is disabled with a reason. */
  onVerify?: (f: FactData) => void;
  onHistory?: (f: FactData) => void;
}

export function FactRow({ fact: f, all, onVerify, onHistory }: FactRowProps) {
  const insp = useInspector();
  const other = f.conflict_with_id ? (all || []).find((x) => x.id === f.conflict_with_id) || null : null;
  const tone = factTone(f.status);
  const open = () => {
    const detail = factSourceDetail(f, other);
    if (onVerify) detail.onVerify = () => onVerify(f);
    if (onHistory) detail.onHistory = () => onHistory(f);
    insp.openSource(detail);
  };
  return (
    <div className="vh-fact">
      <span className="vh-fact__k">{factLabel(f.key)}</span>
      <span className="vh-fact__v">
        <span>{factValue(f)}</span>
        <span className="vh-fact__status" style={{ color: toneVar[tone] }}>{factStatusLine(f)}</span>
      </span>
      <Button size="xs" variant="soft" onClick={open} aria-label={`Source of ${factLabel(f.key)}`}>Source</Button>
    </div>
  );
}

export default FactRow;
