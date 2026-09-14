/* One row of a "Needs your decision" list: title, meta line (kind · consequence · record · deadline ·
   who asked), state when not pending, and a Review button that opens the exact approval. */
import type { ReactNode } from "react";
import { entityLabel, humanize, shortId } from "../../lib/links";
import { Button, HealthLabel, ListRow, Money, When } from "../../ui";
import { actorName, kindLabel, statusView, type ApprovalRowData } from "./types";
import { openApproval } from "./useApprovalReview";

export interface ApprovalRowProps {
  approval: ApprovalRowData;
  /** Override what Review does (default: openApproval → dialog on desktop, page on phones). */
  onReview?: (id: string) => void;
  /** Hide the state label (lists that are already filtered to pending). */
  hideState?: boolean;
}

function consequenceText(a: ApprovalRowData): ReactNode {
  const c = a.consequence || {};
  const bits: ReactNode[] = [];
  if (c.amount !== undefined && c.amount !== null && c.amount !== "") bits.push(<Money key="amt" amount={c.amount as number | string} currency={typeof c.currency === "string" ? c.currency : "USD"} />);
  else if (typeof c.scope === "string") bits.push(<span key="scope">{humanize(c.scope)}</span>);
  const to = (a.targets || {}).recipients;
  if (Array.isArray(to) && to.length) bits.push(<span key="to">to {to.length === 1 ? String(to[0]) : `${to.length} recipients`}</span>);
  return bits.length ? bits.map((b, i) => <span key={i}>{i ? " · " : ""}{b}</span>) : null;
}

export function ApprovalRow({ approval: a, onReview, hideState }: ApprovalRowProps) {
  const sv = statusView(a.status);
  const pending = a.status === "pending";
  const cons = consequenceText(a);
  const review = () => (onReview ? onReview(a.id) : openApproval(a.id));
  return (
    <ListRow
      title={a.title}
      tags={!hideState && !pending ? (sv.health ? <HealthLabel health={sv.health} label={sv.label} /> : <span className="fs13 t3">{sv.label}</span>) : undefined}
      meta={
        <span className="row-wrap" style={{ gap: 6 }}>
          <span>{kindLabel(a.kind)}</span>
          {cons ? <><span aria-hidden="true">·</span><span>{cons}</span></> : null}
          {a.entity_kind ? <><span aria-hidden="true">·</span><span>{entityLabel(a.entity_kind)} {shortId(a.entity_id)}</span></> : null}
          {pending && a.expires_at ? <><span aria-hidden="true">·</span><span>expires <When iso={a.expires_at} relative /></span></> : null}
          <span aria-hidden="true">·</span><span>asked by {actorName(a.requested_by)}</span>
        </span>
      }
      right={<Button size="sm" variant={pending ? "primary" : "soft"} onClick={review}>{pending ? "Review" : "Open"}</Button>}
    />
  );
}
