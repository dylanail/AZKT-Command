/* One shipping-quote case: draft → waiting for approval → requested → reply received → clarifying →
   forwarded to the customer → booked. Forwarding and booking are separate approvals, and neither
   authorizes the other, so Book stays disabled with its reason until the quote has been forwarded. */
import { TZ } from "../../../lib/format";
import { Button, Chip, Expander, HealthLabel, Money, Notice, When } from "../../../ui";
import { JsonDetail } from "../../shared/JsonDetail";
import { QUOTE_CHAIN, QUOTE_LABEL, quoteStageIndex, type Quote, type QuoteComparison } from "../types";

export interface QuoteCaseProps {
  q: Quote;
  costs: boolean;
  writeReason?: string;
  busy: (key: string) => boolean;
  onRequest: () => void;
  onRecordReply: () => void;
  onCompare: () => void;
  onForward: () => void;
  onBook: () => void;
  onDecline: () => void;
  onOpenApproval: (id: string) => void;
}

function chainTone(status: string): "ok" | "wait" | "risk" | "blocked" | null {
  if (status === "booked") return "ok";
  if (status === "declined" || status === "expired") return "blocked";
  if (status === "needs_information") return "risk";
  return "wait";
}

export function QuoteCase({ q, costs, writeReason, busy, onRequest, onRecordReply, onCompare, onForward, onBook, onDecline, onOpenApproval }: QuoteCaseProps) {
  const stage = quoteStageIndex(q.status);
  const comparison = (q.comparison && Object.keys(q.comparison).length ? (q.comparison as QuoteComparison) : null);
  const tone = chainTone(q.status);
  const expired = !!q.expires_at && new Date(q.expires_at).getTime() < Date.now();
  const closed = ["booked", "declined", "expired"].includes(q.status);

  const requestReason = writeReason
    || (closed ? `This case is ${QUOTE_LABEL[q.status]?.toLowerCase() || q.status}.` : undefined)
    || (q.needs_information?.length ? "Missing facts below have to be recorded first — AZKT will not invent them." : undefined);
  const replyReason = writeReason || (["draft", "needs_information", "pending_approval"].includes(q.status) ? "Request the quote before recording a reply." : closed ? "This case is closed." : undefined);
  const compareReason = writeReason || (q.amount === null && !q.money_hidden ? "There is no recorded price to compare." : !["received", "clarifying", "forwarded"].includes(q.status) ? "Compare once a priced reply is recorded." : undefined);
  const forwardReason = writeReason || (q.status !== "received" && q.status !== "forwarded" ? "Only a received quote can be forwarded." : expired ? "The vendor quote has expired — ask for a fresh one." : undefined);
  const bookReason = writeReason
    || (q.status === "booked" ? "Already booked."
      : q.status !== "forwarded" ? "Forward the quote to the customer first — booking is a separate approval and a forward never authorizes it."
      : expired ? "The vendor quote has expired."
      : !costs && q.money_hidden ? "Booking commits an amount, and amounts are hidden for your role." : undefined);

  return (
    <article className="qc">
      <div className="qc__head">
        <div className="qc__who">
          <div className="qc__title">
            <span>{q.vendor_name || "Vendor not recorded"}</span>
            {tone ? <HealthLabel health={tone} label={QUOTE_LABEL[q.status] || q.status} /> : <Chip size="sm" tone="soft">{QUOTE_LABEL[q.status] || q.status}</Chip>}
            {q.binding === "binding" ? <Chip size="sm" tone="wait">Binding</Chip> : <Chip size="sm" tone="soft">Nonbinding</Chip>}
          </div>
          <div className="qc__meta">
            {q.route_from || q.route_to ? `${q.route_from || "origin not recorded"} → ${q.route_to || "destination not recorded"}` : "Route not recorded"}
            {q.service && q.service !== "unknown" ? ` · ${q.service}` : " · service not specified"}
            {q.operability && q.operability !== "unknown" ? ` · ${q.operability}` : " · operability unknown"}
          </div>
        </div>
        <div className="qc__who" style={{ alignItems: "flex-end" }}>
          <span className="fs13">{q.amount !== null || q.money_hidden ? <Money amount={q.amount} currency={q.currency || "USD"} hidden={!costs} /> : <span className="not-recorded">No price recorded</span>}</span>
          <span className="fs12 t4">
            {q.next_check_at ? <>next check <When iso={q.next_check_at} tz={TZ.phoenix} format="datetime" /></>
              : q.requested_at ? <>requested <When iso={q.requested_at} tz={TZ.phoenix} format="datetime" /></>
              : "not requested"}
          </span>
        </div>
      </div>

      <div className="qc__chain" role="list" aria-label="Quote case progress">
        {QUOTE_CHAIN.map((s, i) => (
          <span key={s} role="listitem" className={["qc__step", stage > i ? "qc__step--done" : "", stage === i ? "qc__step--now" : ""].filter(Boolean).join(" ")}>
            {QUOTE_LABEL[s] || s}
          </span>
        ))}
      </div>

      {q.needs_information?.length ? (
        <Notice tone="risk" lead="Missing facts">
          <ul className="qc-needs">
            {q.needs_information.map((n, i) => <li key={i}>{n.field}: {n.reason}</li>)}
          </ul>
        </Notice>
      ) : null}
      {expired && !closed ? <Notice tone="risk" lead="Vendor quote expired">It expired <When iso={q.expires_at} tz={TZ.phoenix} format="datetime" />. Forwarding and booking are blocked until a fresh quote is recorded.</Notice> : null}

      {q.received_at ? (
        <Expander title="What the vendor actually said" defaultOpen={q.status === "received" || q.status === "clarifying"}>
          <dl className="shp-facts">
            <dt>Scope</dt><dd>{q.scope || <span className="not-recorded">Not recorded</span>}</dd>
            <dt>Includes</dt><dd>{q.inclusions?.length ? q.inclusions.join(", ") : <span className="not-recorded">Not recorded</span>}</dd>
            <dt>Excludes</dt><dd>{q.exclusions?.length ? q.exclusions.join(", ") : <span className="not-recorded">Not recorded</span>}</dd>
            <dt>Timing</dt><dd>{q.timing || <span className="not-recorded">Not recorded</span>}</dd>
            <dt>Expires</dt><dd>{q.expires_at ? <When iso={q.expires_at} tz={TZ.phoenix} format="long" /> : <span className="not-recorded">Not recorded</span>}</dd>
            <dt>Reply</dt><dd>{q.reply_message_id || <span className="not-recorded">Not recorded</span>}</dd>
          </dl>
        </Expander>
      ) : null}

      {comparison ? (
        <div className="qc-compare">
          <Notice tone={comparison.evidence === "weak" ? "risk" : "ok"} lead={comparison.evidence === "weak" ? "Weak evidence" : "Comparable evidence"}>
            {comparison.weakness?.length ? comparison.weakness.join(" · ") : `${comparison.comparable_count} comparable quote${comparison.comparable_count === 1 ? "" : "s"} on record`}
            {". "}{comparison.recommendation}
          </Notice>
          <span className="fs13 t3">
            {comparison.range && costs ? <>Range <Money amount={comparison.range.min} currency={comparison.currency || "USD"} /> – <Money amount={comparison.range.max} currency={comparison.currency || "USD"} />{comparison.position ? ` · this quote sits ${comparison.position}` : ""}</> : null}
            {!costs ? "Amounts are hidden for your role; the weakness label and the recommendation are not." : null}
          </span>
          <Expander title={`Comparables (${comparison.comparables?.length || 0}) and what was excluded (${comparison.excluded?.length || 0})`}>
            <JsonDetail value={{ comparables: comparison.comparables, excluded: comparison.excluded, compared_at: comparison.compared_at }} />
            <span className="fs12 t4">No price threshold is invented: only genuinely comparable route, date window, operability, size and service count.</span>
          </Expander>
        </div>
      ) : null}

      <div className="qc__actions">
        <Button size="sm" variant="primary" onClick={onRequest} loading={busy(`quote-request:${q.id}`)} disabled={!!requestReason} disabledReason={requestReason}>
          {q.status === "requested" || q.status === "clarifying" ? "Request again…" : "Request quote…"}
        </Button>
        <Button size="sm" variant="soft" onClick={onRecordReply} loading={busy(`quote-reply:${q.id}`)} disabled={!!replyReason} disabledReason={replyReason}>Record reply…</Button>
        <Button size="sm" variant="soft" onClick={onCompare} loading={busy(`quote-compare:${q.id}`)} disabled={!!compareReason} disabledReason={compareReason}>Compare</Button>
        <Button size="sm" variant="soft" onClick={onForward} loading={busy(`quote-forward:${q.id}`)} disabled={!!forwardReason} disabledReason={forwardReason}>Forward to customer…</Button>
        <Button size="sm" variant="glass" onClick={onBook} loading={busy(`quote-book:${q.id}`)} disabled={!!bookReason} disabledReason={bookReason}>Book…</Button>
        <Button size="sm" variant="ghost" onClick={onDecline} disabled={!!writeReason || q.status === "booked"} disabledReason={writeReason || "Cancel the booking with the carrier first."}>Decline…</Button>
      </div>

      <div className="row-wrap">
        {q.request_approval_id ? <Button size="xs" variant="soft" onClick={() => onOpenApproval(q.request_approval_id as string)}>Review the request approval</Button> : null}
        {q.forward_approval_id ? <Button size="xs" variant="soft" onClick={() => onOpenApproval(q.forward_approval_id as string)}>Review the forward approval</Button> : null}
        {q.booking_approval_id ? <Button size="xs" variant="soft" onClick={() => onOpenApproval(q.booking_approval_id as string)}>Review the booking approval</Button> : null}
        {q.clarification_task_id ? <Button size="xs" variant="ghost" to={`/tasks/${encodeURIComponent(q.clarification_task_id)}`}>Open the clarification task</Button> : null}
        {q.request_action_state ? <span className="fs12 t4">send: {q.request_action_state}</span> : null}
        {q.recipients?.length ? <span className="fs12 t4">to {q.recipients.join(", ")} ({q.channel || "channel not recorded"})</span> : null}
      </div>

      {Object.keys(q.booking || {}).length ? (
        <Expander title="The booking exactly as approved"><JsonDetail value={q.booking} /></Expander>
      ) : null}
    </article>
  );
}

export default QuoteCase;
