/* Milestones in the K04 shape: planned / estimated / completed, the sourced time (or "Not recorded"),
   and where that time came from. A container-wide notice applies to every member vehicle; a per-vehicle
   row is an exception that survives later container notices, and is labelled as one. */
import { Chip, EmptyState, HealthLabel } from "../../../ui";
import { DualTime } from "../../requests/components/DualTime";
import { MILESTONE_LABEL, type Milestone, type VehicleMember } from "../types";

function statusTone(status: string): "ok" | "wait" | "risk" | null {
  if (status === "completed") return "ok";
  if (status === "estimated") return "wait";
  if (status === "planned") return null;
  return "risk";
}

export interface MilestoneListProps {
  rows: Milestone[];
  vehicles: VehicleMember[];
  /** Dim rows that were superseded by a later notice. */
  superseded?: boolean;
  emptyTitle?: string;
  emptyBody?: string;
}

export function MilestoneList({ rows, vehicles, superseded = false, emptyTitle = "No milestones recorded", emptyBody = "Departure, arrival, discharge, release, booking, pickup and receipt are recorded with their source." }: MilestoneListProps) {
  if (!rows.length) return <EmptyState align="left" title={emptyTitle} body={emptyBody} />;
  const titleOf = (vid: string | null) => (vid ? vehicles.find((v) => v.id === vid)?.title || vid : null);
  return (
    <ul className="ms-list">
      {rows.map((m, i) => {
        const tone = statusTone(m.status);
        return (
          <li key={m.id || `${m.kind}-${i}`} className={["ms-row", superseded ? "ms-row--superseded" : ""].filter(Boolean).join(" ")}>
            <div className="ms-row__main">
              <span className="ms-row__kind">
                {MILESTONE_LABEL[m.kind] || m.kind.replace(/_/g, " ")}
                {tone ? <HealthLabel health={tone} label={m.status === "completed" ? "Completed" : m.status === "estimated" ? "Estimated" : m.status} /> : <Chip size="sm" tone="soft">Planned</Chip>}
                {m.vehicle_id ? <Chip size="sm" tone="risk" title="A per-vehicle exception; later container notices do not overwrite it">Exception · {titleOf(m.vehicle_id)}</Chip> : <Chip size="sm" tone="soft">Whole container</Chip>}
              </span>
              <span className="ms-row__src">
                {m.source_kind ? `source: ${m.source_kind}` : "source not recorded"}
                {m.source_ref ? ` · ${m.source_ref}` : m.source_kind && m.source_kind !== "manual" ? " · no reference recorded" : ""}
                {m.note ? ` · ${m.note}` : ""}
              </span>
            </div>
            <div className="ms-row__when"><DualTime value={m.at} stacked /></div>
          </li>
        );
      })}
    </ul>
  );
}

export default MilestoneList;
