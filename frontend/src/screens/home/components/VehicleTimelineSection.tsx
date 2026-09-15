/* 6. Vehicle timeline — one compact line per vehicle: current stage, how long it has been there, the last
   recorded milestone, the next event and any blocked work (spec §2.4).
   Rules kept visible: a milestone that was never recorded reads "Not recorded", every date says where it came
   from (a sourced date or a clearly labelled internal target), and no ETA is ever invented. A sold truck still
   shows its open shipment. The horizon selector (7 / 30 days) lives in the URL. */
import { Chip, GlassPanel, HealthLabel, NotRecorded, SegmentedControl, When } from "../../../ui";
import type { Health } from "../../../ui";
import type { TimelineItem, TimelineSection } from "../types";
import { STAGE_DIMENSION_LABELS, stateLabel } from "../labels";
import { Explain, RecordLink } from "./parts";

/** The two offered horizons. Any 1–365 day value that arrives in the URL is honoured and shown as a third
    option rather than silently snapped to 7 — the server accepts the same range. */
export const HORIZONS = [7, 30] as const;
export type Horizon = number;

export function readHorizon(raw: string | null): Horizon {
  const n = Number(raw);
  if (!Number.isFinite(n) || !Number.isInteger(n) || n < 1 || n > 365) return 7;
  return n;
}

const HEALTHS: ReadonlySet<string> = new Set(["blocked", "risk", "ok", "wait"]);
function healthOf(h: string | null): Health | null {
  return h && HEALTHS.has(h) ? (h as Health) : null;
}

/** "sourced" / "internal target" reads plainly; anything else is the server's own basis, verbatim. */
function basisLabel(basis: string): string {
  if (basis === "sourced") return "sourced date";
  if (basis === "internal target") return "internal target, not a promise";
  return basis;
}

export function TimelineRow({ item }: { item: TimelineItem }) {
  const health = healthOf(item.health);
  const stage = `${STAGE_DIMENSION_LABELS[item.current_stage_dimension] || item.current_stage_dimension}: ${stateLabel(item.current_stage)}`;
  const name = item.title || item.stock_no || `Vehicle ${item.vehicle_id.slice(0, 8)}`;

  return (
    <div className="hm-row hm-row--timeline">
      <div className="hm-row__main">
        <div className="hm-row__title">
          <RecordLink href={`/vehicles/${item.vehicle_id}`}>{name}</RecordLink>
          {item.stock_no && item.title ? <span className="fs12 t4 tnum">{item.stock_no}</span> : null}
          <Chip size="sm" tone="soft">{stage}</Chip>
          {health ? <HealthLabel health={health} /> : null}
          {item.blocked_work ? <HealthLabel health="blocked" label={`${item.blocked_work} blocked`} /> : null}
        </div>
        <Explain>
          {item.days_in_stage === null
            ? "Time in this stage is not recorded"
            : `${item.days_in_stage} day${item.days_in_stage === 1 ? "" : "s"} in this stage`}
          {" · "}
          {item.days_since_acquisition === null
            ? "no recorded acquisition date"
            : `${item.days_since_acquisition} day${item.days_since_acquisition === 1 ? "" : "s"} since acquisition`}
        </Explain>
        <div className="hm-row__facts fs13 tnum">
          <span className="t3">
            Last milestone:{" "}
            {item.last_milestone
              ? <>{item.last_milestone.label} · <When iso={item.last_milestone.at} format="date" /></>
              : <NotRecorded />}
          </span>
          <span className="t3">
            Next:{" "}
            {item.next_event
              ? <>{item.next_event.label} · <When iso={item.next_event.at} format="long" /> <span className="t4">({basisLabel(item.next_event.basis)})</span></>
              : <span className="t4">nothing recorded inside this horizon</span>}
          </span>
          {item.next_action.title ? (
            <span className="t3">
              Next action: {item.next_action.title}
              {item.next_action.due_at ? <> · <When iso={item.next_action.due_at} format="date" /></> : " · no due date recorded"}
            </span>
          ) : null}
        </div>
        {item.shipment_open_while_sold ? (
          <span className="fs12 hm-overdue">Sold, and its shipment is still open.</span>
        ) : null}
      </div>
    </div>
  );
}

export function VehicleTimelineControls({
  horizon, onHorizon, section,
}: {
  horizon: Horizon;
  onHorizon: (h: Horizon) => void;
  section: TimelineSection | null | undefined;
}) {
  return (
    <div className="hm-period">
      <SegmentedControl<string>
        label="Future events horizon"
        size="sm"
        value={String(horizon)}
        onChange={(v) => onHorizon(readHorizon(v))}
        options={[
          ...HORIZONS.map((h) => ({ value: String(h), label: `Next ${h} days` })),
          ...(HORIZONS.includes(horizon as 7 | 30) ? [] : [{ value: String(horizon), label: `Next ${horizon} days` }]),
        ]}
      />
      {section && section.available !== false ? (
        <span className="fs12 t3 tnum">
          {section.returned} of {section.total} vehicle{section.total === 1 ? "" : "s"}
          {section.as_of ? <> · as of <When iso={section.as_of} format="datetime" /></> : null}
        </span>
      ) : null}
    </div>
  );
}

export function TimelineList({ items }: { items: TimelineItem[] }) {
  return (
    <GlassPanel clip>
      {items.map((it) => <TimelineRow key={it.vehicle_id} item={it} />)}
    </GlassPanel>
  );
}
