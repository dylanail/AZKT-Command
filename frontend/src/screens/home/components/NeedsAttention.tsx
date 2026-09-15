/* 4. Needs attention — blockers, missing documents, overdue promises and tasks, money to match, unmatched
   messages, failed publications and stale connections (spec §2.2). The API already groups every alert about
   the same underlying problem, so a group is rendered as one row and never flattened back into duplicates.
   The count opens the filtered queue the API named. */
import { Chip, Expander, GlassPanel, HealthLabel, When } from "../../../ui";
import { entityHref, humanize, shortId } from "../../../lib/links";
import type { AttentionGroup, AttentionItem } from "../types";
import { ATTENTION_KIND_LABELS, severityHealth } from "../types";
import { Explain, QueueLink, RecordLink } from "./parts";

/** One line per grouped item, using whichever of its fields the server filled in. */
function itemLine(it: AttentionItem): { text: string; href: string | null } {
  const text =
    it.title
    || it.text
    || (it.type ? `${humanize(it.type)}${it.status ? ` — ${humanize(it.status)}` : ""}` : "")
    || (it.label ? `${it.label}${it.state ? ` — ${humanize(it.state)}` : ""}` : "")
    || (it.channel ? `${humanize(it.channel)}${it.state ? ` — ${humanize(it.state)}` : ""}` : "")
    || (it.id ? `${humanize(it.kind || "item")} ${shortId(it.id, 8)}` : humanize(it.kind || "item"));
  const href = entityHref(it.kind || null, (it.id as string) || null)
    || (it.vehicle_id ? `/vehicles/${it.vehicle_id}` : null);
  return { text, href };
}

export function AttentionRow({
  group, mobile, ownerName,
}: {
  group: AttentionGroup;
  mobile: boolean;
  ownerName: (id: string | null | undefined) => string;
}) {
  const health = severityHealth(group.severity);
  const overdue = !!group.due_at && new Date(group.due_at).getTime() < Date.now();
  const kindLabel = ATTENTION_KIND_LABELS[group.kind] || humanize(group.kind);

  return (
    <div className="hm-row">
      <div className="hm-row__main">
        <div className="hm-row__title">
          <span className="truncate">{group.title}</span>
          <Chip size="sm" tone="soft">{kindLabel}</Chip>
          <HealthLabel health={health} label={group.severity === "high" ? "Needs you now" : "Needs attention"} />
        </div>
        {group.detail ? <Explain>{group.detail}</Explain> : null}
        <div className="hm-row__facts fs13 tnum">
          <span>{group.next_action || "No next action recorded"}</span>
          <span className="t3">Owner: {ownerName(group.owner_user_id)}</span>
          <span className={overdue ? "hm-overdue" : "t3"}>
            {group.due_at ? <>{overdue ? "Was due " : "Due "}<When iso={group.due_at} format="long" /></> : "No due date recorded"}
          </span>
        </div>
        {group.items.length > 1 ? (
          <Expander title={`Show the ${group.items.length} items in this group`}>
            <ul className="hm-items">
              {group.items.slice(0, 25).map((it, i) => {
                const line = itemLine(it);
                return (
                  <li key={`${it.id || i}`}>
                    <RecordLink href={line.href}>{line.text}</RecordLink>
                    {it.reason ? <span className="t3"> — {it.reason}</span> : null}
                    {it.due_at ? <span className="t3"> · <When iso={it.due_at} format="date" /></span> : null}
                  </li>
                );
              })}
              {group.items.length > 25 ? <li className="t4">Showing the first 25 of {group.items.length}.</li> : null}
            </ul>
          </Expander>
        ) : null}
      </div>
      <div className="hm-row__right">
        <QueueLink to={group.link} label={group.count > 1 ? `Open ${group.count}` : "Open"} mobile={mobile} />
      </div>
    </div>
  );
}

export function AttentionList({
  groups, mobile, ownerName,
}: {
  groups: AttentionGroup[];
  mobile: boolean;
  ownerName: (id: string | null | undefined) => string;
}) {
  return (
    <GlassPanel clip>
      {groups.map((g) => <AttentionRow key={g.problem} group={g} mobile={mobile} ownerName={ownerName} />)}
    </GlassPanel>
  );
}
