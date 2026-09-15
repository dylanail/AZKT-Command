/* 7. In progress — what AZKT or a person is handling and the next checkpoint (spec §2.2). Only recorded
   missions and cases: there is no fabricated "live thinking" here.
   8. Completed — recent outcomes, collapsed, linked to Activity. */
import type { ReactNode } from "react";
import { Chip, Expander, GlassPanel, When } from "../../../ui";
import { entityHref, humanize } from "../../../lib/links";
import type { CompletedItem, ProgressItem } from "../types";
import { Explain, RecordLink } from "./parts";

function handler(item: ProgressItem, ownerName: (id: string | null | undefined) => string): string {
  if (item.owner_user_id) return ownerName(item.owner_user_id);
  if (item.kind === "mission") return item.role ? `AZKT ${humanize(item.role)}` : "AZKT";
  return item.role ? humanize(item.role) : "No owner recorded";
}

export function ProgressRow({ item, ownerName }: {
  item: ProgressItem;
  ownerName: (id: string | null | undefined) => string;
}) {
  const href = item.vehicle_id ? `/vehicles/${item.vehicle_id}` : null;
  return (
    <div className="hm-row">
      <div className="hm-row__main">
        <div className="hm-row__title">
          <span className="truncate">{item.title}</span>
          <Chip size="sm" tone="soft">{item.kind === "mission" ? "AZKT" : "Case"}</Chip>
          <Chip size="sm" tone="wait">{humanize(item.status)}</Chip>
        </div>
        <Explain>
          {handler(item, ownerName)}
          {item.waiting_on ? ` · waiting on ${humanize(item.waiting_on)}` : ""}
          {item.next_action ? ` · next: ${item.next_action}` : ""}
        </Explain>
        <div className="hm-row__facts fs13 tnum">
          <span className="t3">
            {item.next_check_at
              ? <>Next checkpoint <When iso={item.next_check_at} format="long" /></>
              : "No checkpoint recorded"}
          </span>
          {href ? <RecordLink href={href}>Open the vehicle</RecordLink> : null}
        </div>
      </div>
    </div>
  );
}

export function ProgressList({ items, ownerName }: {
  items: ProgressItem[];
  ownerName: (id: string | null | undefined) => string;
}) {
  return (
    <GlassPanel clip>
      {items.map((it) => <ProgressRow key={`${it.kind}:${it.id}`} item={it} ownerName={ownerName} />)}
    </GlassPanel>
  );
}

export function CompletedList({ items }: { items: CompletedItem[] }) {
  return (
    <GlassPanel clip>
      {items.map((it) => {
        const href = entityHref(it.entity_kind, it.entity_id) || it.activity_path;
        return (
          <div className="hm-row hm-row--completed" key={it.id}>
            <div className="hm-row__when tnum">
              {it.at ? <When iso={it.at} format="short" /> : <span className="t4">No time recorded</span>}
            </div>
            <div className="hm-row__main">
              <div className="hm-row__title">
                <RecordLink href={href}>{it.what}</RecordLink>
              </div>
              <Explain>
                {it.actor || "AZKT"}
                {it.state ? ` · ${humanize(it.state)}` : ""}
              </Explain>
            </div>
          </div>
        );
      })}
    </GlassPanel>
  );
}

/** The collapsed wrapper Home uses so completed work never competes with what still needs a decision. */
export function CompletedExpander({ count, children }: { count: number; children: ReactNode }) {
  return (
    <Expander title={<span className="hm-expander-title">Completed recently {count ? <span className="count tnum">{count}</span> : null}</span>}>
      {children}
    </Expander>
  );
}
