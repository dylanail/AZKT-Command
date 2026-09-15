/* 5. Today — calls, meetings and timed follow-ups across both sales pipelines and operations, in Phoenix
   time (spec §2.2). The reporting period never filters this list; the server says so and we keep it true. */
import { Chip, GlassPanel, HealthLabel, When } from "../../../ui";
import { humanize } from "../../../lib/links";
import type { TodayItem } from "../types";
import { Explain, RecordLink } from "./parts";

function hrefFor(item: TodayItem): string | null {
  if (item.kind === "task") return `/tasks/${item.id}`;
  if (item.vehicle_id) return `/vehicles/${item.vehicle_id}`;
  if (item.opportunity_id) return `/sales?lead=${item.opportunity_id}`;
  if (item.contact_id) return `/contacts/${item.contact_id}`;
  return null;
}

function typeLabel(item: TodayItem): string {
  if (item.kind === "delivery") return "Delivery";
  return humanize(item.type || "Task");
}

export function TodayRow({ item, vehicleName, ownerName }: {
  item: TodayItem;
  vehicleName: (id: string | null | undefined) => string | null;
  ownerName: (id: string | null | undefined) => string;
}) {
  const href = hrefFor(item);
  const late = !!item.at && new Date(item.at).getTime() < Date.now();
  const vehicle = vehicleName(item.vehicle_id);

  return (
    <div className="hm-row hm-row--today">
      <div className="hm-row__when tnum">
        {item.at ? <When iso={item.at} format="time" /> : <span className="t4">No time recorded</span>}
        <span className="fs12 t4">{typeLabel(item)}</span>
      </div>
      <div className="hm-row__main">
        <div className="hm-row__title">
          <RecordLink href={href}>{item.title}</RecordLink>
          <Chip size="sm" tone="soft">{item.pipeline === "sales" ? "Sales" : "Operations"}</Chip>
          {item.status === "blocked" ? <HealthLabel health="blocked" label="Blocked" /> : null}
          {late && item.status !== "blocked" ? <HealthLabel health="risk" label="Time has passed" /> : null}
        </div>
        <Explain>
          {ownerName(item.owner_user_id)}
          {vehicle ? ` · ${vehicle}` : ""}
          {item.priority && item.priority !== "normal" ? ` · ${humanize(item.priority)} priority` : ""}
        </Explain>
      </div>
    </div>
  );
}

export function TodayList({ items, vehicleName, ownerName }: {
  items: TodayItem[];
  vehicleName: (id: string | null | undefined) => string | null;
  ownerName: (id: string | null | undefined) => string;
}) {
  return (
    <GlassPanel clip>
      {items.map((it) => <TodayRow key={`${it.kind}:${it.id}`} item={it} vehicleName={vehicleName} ownerName={ownerName} />)}
    </GlassPanel>
  );
}
