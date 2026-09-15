/* Milestones / timeline (spec §2.4). Planned, estimated and completed are distinguished in words;
   a milestone with no date reads "Not recorded" rather than being hidden or guessed. */
import { NotRecorded, When } from "../../../ui";
import { MILESTONE_LABELS, MILESTONE_ORDER, MILESTONE_STATUS_LABELS, type MilestoneData } from "../types";

const statusTone: Record<string, string> = {
  completed: "var(--ok)", estimated: "var(--wait)", planned: "var(--t3)",
};

export interface MilestoneListProps {
  milestones: MilestoneData[];
  /** Also show milestones that exist in the canonical order but have no record yet. */
  showMissing?: boolean;
  /** Restrict the "missing" rows to the ones that matter for this vehicle. */
  expected?: string[];
}

export function MilestoneList({ milestones, showMissing = false, expected }: MilestoneListProps) {
  const byKind = new Map<string, MilestoneData>();
  for (const m of milestones) if (m.is_current !== false) byKind.set(m.kind, m);

  const kinds = showMissing
    ? MILESTONE_ORDER.filter((k) => byKind.has(k) || (expected ? expected.includes(k) : true))
    : MILESTONE_ORDER.filter((k) => byKind.has(k));

  if (!kinds.length) {
    return <span className="not-recorded">No milestones recorded yet.</span>;
  }

  return (
    <div>
      {kinds.map((kind) => {
        const m = byKind.get(kind);
        const label = MILESTONE_LABELS[kind] || kind.replace(/_/g, " ");
        return (
          <div key={kind} className="vh-ms">
            <span className="vh-ms__kind">{label}</span>
            <span className="vh-ms__when">
              {m?.at ? <When iso={m.at} format="long" /> : <NotRecorded />}
              {m?.note ? <span className="fs12 t3"> · {m.note}</span> : null}
              {m?.source_kind ? <span className="fs12 t4"> · {m.source_kind.replace(/_/g, " ")}</span> : null}
            </span>
            <span className="vh-ms__status" style={{ color: m ? statusTone[m.status] || "var(--t3)" : "var(--t4)" }}>
              {m ? MILESTONE_STATUS_LABELS[m.status] || m.status : "Not recorded"}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export default MilestoneList;
