/* One instant in both zones. Auction and deadline instants always show Japan and Arizona time with
   the source of the time (spec §8.2 step 1). Never invents a time: null stays "Not recorded". */
import { TZ } from "../../../lib/format";
import { When } from "../../../ui";
import type { DualTime as DualTimeValue } from "../types";

export interface DualTimeProps {
  value: DualTimeValue | string | null | undefined;
  /** Stack the two zones instead of joining with "·". */
  stacked?: boolean;
  /** Show the recorded source after the times ("· port notice"). */
  showSource?: boolean;
  format?: "short" | "datetime" | "long";
  className?: string;
  /** Text used when nothing is recorded. */
  empty?: string;
}

function isoOf(value: DualTimeValue | string | null | undefined): string | null {
  if (!value) return null;
  if (typeof value === "string") return value;
  return value.utc || value.tokyo_iso || value.phoenix_iso || null;
}

export function DualTime({ value, stacked = false, showSource = false, format = "datetime", className = "", empty = "Not recorded" }: DualTimeProps) {
  const iso = isoOf(value);
  const source = typeof value === "object" && value ? value.source : null;
  if (!iso) {
    return (
      <span className={["dual", className].filter(Boolean).join(" ")}>
        <span className="not-recorded">{empty}</span>
        {showSource && source ? <span className="dual__src">· {source}</span> : null}
      </span>
    );
  }
  return (
    <span className={["dual", stacked ? "dual--stacked" : "", className].filter(Boolean).join(" ")}>
      <When iso={iso} tz={TZ.tokyo} format={format} />
      {stacked ? null : <span aria-hidden="true" className="dual__dot">·</span>}
      <When iso={iso} tz={TZ.phoenix} format={format} />
      {showSource ? <span className="dual__src">· {source ? `source: ${source}` : "source not recorded"}</span> : null}
    </span>
  );
}

/** "in 3 days" style urgency next to a deadline, with the absolute times in the title. */
export function deadlineTone(value: DualTimeValue | string | null | undefined, passed?: boolean): "blocked" | "risk" | "ok" | null {
  const iso = isoOf(value);
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  if (passed || t <= Date.now()) return "blocked";
  if (t - Date.now() < 24 * 3600 * 1000) return "risk";
  return "ok";
}

export default DualTime;
