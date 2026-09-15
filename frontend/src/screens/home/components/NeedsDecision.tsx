/* 3. Needs your decision — pending approvals with the action, the record, the consequence, the deadline
   and Review (spec §2.2). Never filtered by the reporting period. Review opens the exact approval:
   the shared dialog on desktop, /approvals/:id on a phone — the same record Inbox and notifications open. */
import { Button, Chip, GlassPanel, HealthLabel, When } from "../../../ui";
import { entityHref, entityLabel, humanize, shortId } from "../../../lib/links";
import { openApproval } from "../../approvals/useApprovalReview";
import { kindLabel } from "../../approvals/types";
import type { DecisionItem, MoneyV } from "../types";
import { Amt, Explain, RecordLink } from "./parts";

/** The consequence block carries {amount, currency} when the role may see money, or money_hidden:true. */
function consequenceMoney(c: DecisionItem["consequence"]): MoneyV | null {
  if (typeof c.amount === "string" && typeof c.currency === "string") return { amount: c.amount, currency: c.currency };
  return null;
}

/** Everything in the consequence that is not money, said plainly ("recipients: 2 · channel: email"). */
function consequenceText(c: DecisionItem["consequence"]): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(c)) {
    if (k === "amount" || k === "currency" || k === "money_hidden") continue;
    if (v === null || v === undefined || v === "") continue;
    if (typeof v === "object") {
      const n = Array.isArray(v) ? v.length : Object.keys(v as object).length;
      if (n) parts.push(`${humanize(k).toLowerCase()}: ${n}`);
      continue;
    }
    parts.push(`${humanize(k).toLowerCase()}: ${String(v)}`);
  }
  return parts.join(" · ");
}

export function DecisionRow({ item, mobile }: { item: DecisionItem; mobile: boolean }) {
  const href = entityHref(item.related_record.kind, item.related_record.id);
  const money = consequenceMoney(item.consequence);
  const hiddenMoney = item.consequence.money_hidden === true;
  const extra = consequenceText(item.consequence);

  return (
    <div className="hm-row hm-row--decision">
      <div className="hm-row__main">
        <div className="hm-row__title">
          <span className="truncate">{item.title || kindLabel(item.kind)}</span>
          <Chip size="sm" tone="soft">{kindLabel(item.kind)}</Chip>
          {item.expired ? <HealthLabel health="blocked" label="Deadline passed" /> : null}
        </div>
        <Explain>
          {humanize(item.action)}
          {item.related_record.kind ? (
            <>
              {" · "}
              <RecordLink href={href}>
                {entityLabel(item.related_record.kind)} {shortId(item.related_record.id, 8)}
              </RecordLink>
            </>
          ) : null}
        </Explain>
        <div className="hm-row__facts fs13 tnum">
          <span>
            {hiddenMoney ? <>Amount <Amt m={null} hidden /></> : money ? <>Costs <Amt m={money} bold /></> : null}
            {!hiddenMoney && !money && extra ? extra : null}
          </span>
          {(hiddenMoney || money) && extra ? <span className="t3">{extra}</span> : null}
          <span className="t3">
            {item.deadline ? <>Decide by <When iso={item.deadline} format="long" /></> : "No deadline recorded"}
          </span>
          {item.requested_by ? <span className="t3">Asked by {item.requested_by}</span> : null}
        </div>
      </div>
      <div className="hm-row__right">
        <Button
          size={mobile ? "xl" : "md"}
          variant="primary"
          onClick={() => openApproval(item.approval_id)}
        >
          Review
        </Button>
      </div>
    </div>
  );
}

export function DecisionList({ items, mobile }: { items: DecisionItem[]; mobile: boolean }) {
  return (
    <GlassPanel clip>
      {items.map((it) => <DecisionRow key={`${it.approval_id}:${it.version}`} item={it} mobile={mobile} />)}
    </GlassPanel>
  );
}
