/* One vehicle row: photo thumb (or "No photo yet"), title, stock · frame · allocation, situation,
   next action · owner · time, exception, health label with colour AND text (never colour alone).
   Desktop uses the 5-column grid from the prototype's VEHICLES list; phones use a stacked row. */
import { useState } from "react";
import { Link } from "react-router-dom";
import { HealthLabel, When } from "../../../ui";
import { allocationLabel, situationOf, stateLabel, thumbUrl, vehicleHealth, type VehicleListItem } from "../types";

export function VehicleThumb({ assetId, size = "md", label = "No photo yet" }: { assetId?: string | null; size?: "sm" | "md" | "lg" | "xl"; label?: string }) {
  const [bad, setBad] = useState(false);
  const cls = size === "sm" ? "vh-thumb--sm" : size === "lg" ? "vh-thumb--md" : size === "xl" ? "vh-thumb--lg" : "";
  const url = thumbUrl(assetId);
  if (!url || bad) {
    return <span className={["vh-thumb--ph", cls].filter(Boolean).join(" ")} role="img" aria-label={label}>{size === "sm" ? "" : label}</span>;
  }
  return <img className={["vh-thumb", cls].filter(Boolean).join(" ")} src={url} alt="" loading="lazy" onError={() => setBad(true)} />;
}

export interface HealthRowProps {
  vehicle: VehicleListItem;
  /** Phone layout: one stacked row instead of the 5-column grid. */
  compact?: boolean;
  /** Mechanic view hides allocation (commercial) wording and shows the location instead. */
  hideAllocation?: boolean;
  ownerName?: string;
}

/** "Next action · owner · time" — every piece stays truthful when the record is empty. */
export function nextActionLine(v: VehicleListItem, ownerName?: string): { title: string; meta: string } {
  return {
    title: v.next_action || "No next action recorded",
    meta: [ownerName || (v.next_action_owner_id ? "Assigned" : "Unassigned")].filter(Boolean).join(" · "),
  };
}

export function HealthRow({ vehicle: v, compact = false, hideAllocation = false, ownerName }: HealthRowProps) {
  const h = vehicleHealth(v);
  const to = `/vehicles/${encodeURIComponent(v.id)}`;
  const next = nextActionLine(v, ownerName);
  const stage = stateLabel(v.states?.recon);
  const idLine = [v.stock_no || "No stock number", v.frame_no_raw || "Frame not recorded",
    hideAllocation ? v.location || "Location not recorded" : allocationLabel(v.allocation)].join(" · ");

  if (compact) {
    return (
      <Link to={to} className="vh-mrow">
        <VehicleThumb assetId={v.photo?.asset_id} />
        <span className="vh-mrow__main">
          <span className="vh-title">{v.title || "Vehicle"}</span>
          <span className="vh-mrow__sub">{idLine}</span>
          <span className="vh-mrow__sub">{situationOf(v)}</span>
          <span className="row-wrap" style={{ gap: 8 }}>
            <HealthLabel health={h.health} label={h.label} />
            {v.exception ? <span className="fs13 t2 truncate" style={{ maxWidth: 220 }}>{v.exception}</span> : null}
          </span>
          <span className="vh-mrow__sub">{next.title}</span>
        </span>
        <span className="list-row__chev" aria-hidden="true">›</span>
      </Link>
    );
  }

  return (
    <Link to={to} className="vh-row">
      <span className="vh-id">
        <VehicleThumb assetId={v.photo?.asset_id} />
        <span className="vh-col">
          <span className="vh-title">{v.title || "Vehicle"}</span>
          <span className="vh-col__sub truncate">{idLine}</span>
        </span>
      </span>
      <span className="truncate t2">{hideAllocation ? v.location || "Not recorded" : allocationLabel(v.allocation)}</span>
      <span className="vh-col">
        <span className="fs12 t4">{stage}</span>
        <span className="t2 truncate">{situationOf(v)}</span>
      </span>
      <span className="vh-col">
        <span className="truncate">{next.title}</span>
        <span className="vh-col__sub truncate">
          {next.meta}
          {" · "}
          {v.next_action_due_at ? <When iso={v.next_action_due_at} /> : "No time set"}
        </span>
      </span>
      <span className="vh-col">
        <HealthLabel health={h.health} label={h.label} />
        {v.exception ? <span className="fs12 t3 truncate">{v.exception}</span> : null}
      </span>
    </Link>
  );
}

export default HealthRow;
