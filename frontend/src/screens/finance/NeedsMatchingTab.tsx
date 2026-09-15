/* Needs matching — GET /api/finance/needs-matching.
   Four kinds of unfinished business: cost evidence to review, payments only reported, payment allocations
   proposed against obligations, and fuzzy cost allocations across vehicles. Nothing here is decided for you. */
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../lib/api";
import { useQuery } from "../../lib/useQuery";
import { useCommand } from "../../lib/useCommand";
import { humanize } from "../../lib/links";
import { Button, Chip, EmptyState, ErrorState, Expander, GlassPanel, KeyValues, Loading, Notice, Section, When } from "../../ui";
import { Amt, DiscrepancyBlock, Reasons, SourceLink } from "./parts";
import { ConfirmAllocationDialog, ConfirmPaymentDialog, EvidenceReviewDialog, ProposeAllocationDialog, type EvidenceMode } from "./dialogs";
import { useRefs } from "./useRefs";
import {
  ALLOCATION_FLAG_LABELS, CATEGORY_LABELS, EVIDENCE_KIND_LABELS, INVOICE_KIND_LABELS, MATCH_STATE_LABELS, matchStateTone,
  type CostAllocation, type Evidence, type Invoice, type NeedsMatchingResp, type Payment, type PaymentAllocation,
} from "./types";

interface Props { canWrite: boolean; isOwner: boolean; refs: ReturnType<typeof useRefs> }

function ExtractedFields({ e }: { e: Evidence }) {
  const extra = Object.entries(e.extracted || {}).filter(([, v]) => v !== null && v !== "" && typeof v !== "object");
  return (
    <KeyValues items={[
      ["Vendor", e.vendor || "Not recorded"],
      ["Invoice / order", e.invoice_no || e.order_no || "Not recorded"],
      ["Amount", <Amt key="a" m={e.amount} />],
      ["Date on the document", e.occurred_at ? <When key="d" iso={e.occurred_at} format="date" /> : "Not recorded"],
      ["Vehicle text on the document", e.vehicle_ref_text || "Not recorded"],
      ["Confidence", e.confidence ? humanize(e.confidence) : "Not scored"],
      ["Revision", `${e.revision}${e.supersedes_id ? ` · replaces ${e.supersedes_id.slice(0, 8)}` : ""}`],
      ...extra.map(([k, v]) => [humanize(k), String(v)] as [string, string]),
    ]} />
  );
}

function EvidenceCard({ e, canWrite, refs, onReview }: Props & { e: Evidence; onReview: (e: Evidence, m: EvidenceMode) => void }) {
  const conflict = e.match_state === "conflict";
  const noTarget = !e.cost_item_id && !e.proposed_cost_item_id && (e.candidates || []).length !== 1;
  const confirmReason = !canWrite ? "Only the owner writes to Finance."
    : noTarget ? "No single cost item to confirm — use Correct to choose one."
    : undefined;
  return (
    <div className="fin-row">
      <div className="fin-row__head">
        <div className="fin-row__main">
          <div className="fin-row__title">
            <span className="truncate">{e.vendor || "Vendor not recorded"}</span>
            <Chip size="sm" tone={matchStateTone(e.match_state)}>{MATCH_STATE_LABELS[e.match_state] || humanize(e.match_state)}</Chip>
            {e.is_credit ? <Chip size="sm" tone="wait">Credit</Chip> : null}
            {e.is_settlement ? <Chip size="sm" tone="ok">Verified settlement</Chip> : null}
          </div>
          <div className="fin-row__meta">
            {EVIDENCE_KIND_LABELS[e.kind] || humanize(e.kind)}
            {e.invoice_no || e.order_no ? ` · ${e.invoice_no || e.order_no}` : ""}
            {e.occurred_at ? <> · <When iso={e.occurred_at} format="date" /></> : null}
            {" · "}<SourceLink href={e.source_link} ref_={e.source_ref} />
          </div>
        </div>
        <div className="fin-row__amount"><Amt m={e.amount} bold /></div>
      </div>

      <div className="fin-row__chips">
        <Chip size="sm" tone={e.proposed_vehicle_id ? "act" : "risk"}>
          {e.proposed_vehicle_id ? refs.vehicleName(e.proposed_vehicle_id) : "No vehicle proposed"}
        </Chip>
        <Chip size="sm" tone={e.proposed_category ? "soft" : "risk"}>
          {e.proposed_category ? (CATEGORY_LABELS[e.proposed_category] || humanize(e.proposed_category)) : "No category proposed"}
        </Chip>
        {e.proposed_cost_item_id || e.cost_item_id ? (
          <Chip size="sm" tone="soft">Cost item {(e.cost_item_id || e.proposed_cost_item_id || "").slice(0, 8)}</Chip>
        ) : null}
        {(e.candidates || []).length > 1 ? <Chip size="sm" tone="risk" count={e.candidates.length}>possible items</Chip> : null}
      </div>

      {conflict ? <DiscrepancyBlock d={e.discrepancy} /> : null}
      <Reasons items={e.match_reasons} />

      <div className="fin-row__actions">
        <Button size="sm" variant="primary" disabled={!!confirmReason} disabledReason={confirmReason} onClick={() => onReview(e, "confirm")}>Confirm</Button>
        <Button size="sm" variant="soft" disabled={!canWrite} disabledReason={canWrite ? undefined : "Only the owner writes to Finance."} onClick={() => onReview(e, "correct")}>Correct…</Button>
        <Button size="sm" variant="ghost" disabled={!canWrite} disabledReason={canWrite ? undefined : "Only the owner writes to Finance."} onClick={() => onReview(e, "leave")}>Leave unmatched</Button>
        {e.proposed_vehicle_id ? <Button size="sm" variant="ghost" to={`/vehicles/${e.proposed_vehicle_id}?tab=money`}>Vehicle</Button> : null}
      </div>

      <Expander title="Extracted fields and source">
        <div className="stack-sm">
          <ExtractedFields e={e} />
          {e.review_note ? <div className="fs13 t3">Note: {e.review_note}</div> : null}
          <div className="fs12 t4">Evidence {e.id.slice(0, 8)} · v{e.version} · recorded <When iso={e.created_at} format="datetime" />
            {e.ledger_row_id ? ` · ledger row ${e.ledger_row_id.slice(0, 8)}` : ""}</div>
        </div>
      </Expander>
    </div>
  );
}

function ReportedPaymentCard({ p, canWrite, onConfirm, onAllocate }: { p: Payment; canWrite: boolean; onConfirm: (p: Payment) => void; onAllocate: (p: Payment) => void }) {
  return (
    <div className="fin-row">
      <div className="fin-row__head">
        <div className="fin-row__main">
          <div className="fin-row__title">
            <span className="truncate">{p.payer_name || p.payer_email || "Payer not recorded"}</span>
            <Chip size="sm" tone="risk">{p.status_label}</Chip>
            {p.is_payout ? <Chip size="sm" tone="wait">Payout, not customer money</Chip> : null}
          </div>
          <div className="fin-row__meta">
            {humanize(p.provider)}{p.source_kind ? ` · ${humanize(p.source_kind)}` : ""}
            {p.occurred_at ? <> · <When iso={p.occurred_at} format="datetime" /></> : null}
          </div>
        </div>
        <div className="fin-row__amount"><Amt m={p.amount} bold /></div>
      </div>
      {p.report_flags?.length ? (
        <div className="fin-row__chips">
          {p.report_flags.map((f) => (
            <Chip key={f} size="sm" tone={f === "needs_confirmation" ? "amber" : "risk"}
              title={f === "sender_not_provider" ? "The sending domain is not the provider's." : f === "subject_claims_paid_is_not_evidence" ? "A subject line saying 'paid' is not evidence." : undefined}>
              {humanize(f)}
            </Chip>
          ))}
        </div>
      ) : null}
      <Notice tone="wait" lead="Reported, not confirmed">
        Nothing downstream moves on a claim. Confirm it from a bank line or receipt first; applying it to an obligation is a separate step.
      </Notice>
      <div className="fin-row__actions">
        <Button size="sm" variant="primary" disabled={!canWrite} disabledReason={canWrite ? undefined : "Only the owner confirms a payment."} onClick={() => onConfirm(p)}>Confirm from evidence…</Button>
        <Button size="sm" variant="soft" disabled disabledReason="Confirm the payment before applying it to an obligation." onClick={() => onAllocate(p)}>Apply to an obligation…</Button>
      </div>
      <Expander title="Claim details">
        <KeyValues items={[
          ["Claimed by", humanize(String(p.report_source?.claimed_by ?? "unknown"))],
          ["Sender", String(p.report_source?.sender ?? "Not recorded")],
          ["Subject", String(p.report_source?.subject ?? "Not recorded")],
          ["Provider named", String(p.report_source?.provider_hint ?? "Not recorded")],
          ["Reference", p.source_ref || "Not recorded"],
          ["Payment id", p.id.slice(0, 8)],
        ]} />
      </Expander>
    </div>
  );
}

function PaymentAllocationCard({
  a, payment, invoice, siblings, isOwner, onConfirm,
}: { a: PaymentAllocation; payment: Payment | null; invoice: Invoice | null; siblings: PaymentAllocation[]; isOwner: boolean; onConfirm: (a: PaymentAllocation) => void }) {
  const ambiguous = a.ambiguous || a.flags.includes("ambiguous");
  return (
    <div className="fin-row">
      <div className="fin-row__head">
        <div className="fin-row__main">
          <div className="fin-row__title">
            <span className="truncate">{invoice ? (INVOICE_KIND_LABELS[invoice.kind] || humanize(invoice.kind)) : "Obligation"}</span>
            <Chip size="sm" tone="wait">Proposed allocation</Chip>
            {ambiguous ? <Chip size="sm" tone="risk">Two obligations could be this payment</Chip> : null}
          </div>
          <div className="fin-row__meta">
            Payment {payment ? `${payment.provider} · ${payment.payer_name || "payer not recorded"}` : a.payment_id.slice(0, 8)}
            {" · obligation "}{a.invoice_id.slice(0, 8)}
          </div>
        </div>
        <div className="fin-row__amount"><Amt m={a.amount} bold /></div>
      </div>
      <div className="fin-row__chips">
        {a.flags.map((f) => <Chip key={f} size="sm" tone={f === "partial" ? "wait" : "risk"}>{ALLOCATION_FLAG_LABELS[f] || humanize(f)}</Chip>)}
        {invoice ? <Chip size="sm" tone="soft"><Amt m={invoice.remaining} /> remaining</Chip> : null}
      </div>
      {ambiguous ? (
        <Notice tone="risk" lead="Pick one" role="alert">
          {siblings.length} open obligations match this payment equally well. AZKT proposed all of them and will not choose.
        </Notice>
      ) : null}
      <div className="fin-row__actions">
        <Button size="sm" variant="primary" disabled={!isOwner} disabledReason={isOwner ? undefined : "Only the owner confirms an allocation."} onClick={() => onConfirm(a)}>Confirm allocation…</Button>
        {invoice?.vehicle_id ? <Button size="sm" variant="ghost" to={`/vehicles/${invoice.vehicle_id}?tab=money`}>Vehicle</Button> : null}
      </div>
    </div>
  );
}

function CostAllocationCard({ a, refs, isOwner, onConfirm, busy }: { a: CostAllocation; refs: Props["refs"]; isOwner: boolean; onConfirm: (a: CostAllocation) => void; busy: boolean }) {
  return (
    <div className="fin-row">
      <div className="fin-row__head">
        <div className="fin-row__main">
          <div className="fin-row__title">
            <Link className="truncate" to={`/vehicles/${a.vehicle_id}?tab=money`}>{refs.vehicleName(a.vehicle_id)}</Link>
            <Chip size="sm" tone="wait">Proposed split</Chip>
            <Chip size="sm" tone="soft">{humanize(a.basis)} basis</Chip>
          </div>
          <div className="fin-row__meta">Cost item {a.cost_item_id.slice(0, 8)}{a.weight ? ` · weight ${a.weight}` : ""}</div>
        </div>
        <div className="fin-row__amount"><Amt m={a.net_amount || a.amount} bold /></div>
      </div>
      <Reasons items={a.review_reasons} title="Why this needs review" />
      <div className="fin-row__actions">
        <Button size="sm" variant="primary" loading={busy} disabled={!isOwner} disabledReason={isOwner ? undefined : "Only the owner confirms an allocation."} onClick={() => onConfirm(a)}>
          Confirm every split on this item
        </Button>
        <Button size="sm" variant="ghost" to={`/vehicles/${a.vehicle_id}?tab=money`}>Vehicle</Button>
      </div>
    </div>
  );
}

export function NeedsMatchingTab({ canWrite, isOwner, refs }: Props) {
  const q = useQuery<NeedsMatchingResp>((signal) => api.get<NeedsMatchingResp>("/api/finance/needs-matching", { signal }), []);
  const invoicesQ = useQuery<{ items: Invoice[] }>((signal) => api.get<{ items: Invoice[] }>("/api/finance/invoices", { signal, tolerate: [404] }), []);
  const paymentsQ = useQuery<{ items: Payment[] }>((signal) => api.get<{ items: Payment[] }>("/api/finance/payments?limit=200", { signal, tolerate: [404] }), []);
  const { run, busy } = useCommand();

  const [review, setReview] = useState<{ e: Evidence; mode: EvidenceMode } | null>(null);
  const [confirmPay, setConfirmPay] = useState<Payment | null>(null);
  const [allocatePay, setAllocatePay] = useState<Payment | null>(null);
  const [confirmAlloc, setConfirmAlloc] = useState<PaymentAllocation | null>(null);

  const invoiceById = useMemo(() => {
    const m = new Map<string, Invoice>();
    for (const i of invoicesQ.data?.items || []) m.set(i.id, i);
    return m;
  }, [invoicesQ.data]);
  const paymentById = useMemo(() => {
    const m = new Map<string, Payment>();
    for (const p of paymentsQ.data?.items || []) m.set(p.id, p);
    return m;
  }, [paymentsQ.data]);

  /** Confirmed money that has not been applied to any obligation yet (E07: applying stays explicit). */
  const unapplied = useMemo(() => (paymentsQ.data?.items || []).filter((p) => {
    if (p.is_payout) return false;
    if (p.status !== "completed" && p.status !== "partially_refunded") return false;
    const left = Number(p.unallocated_amount?.amount ?? "0");
    return Number.isFinite(left) && left > 0;
  }), [paymentsQ.data]);

  const items = q.data?.items || [];
  const evidence = items.filter((i): i is Evidence & { item_kind: "cost_evidence" } => i.item_kind === "cost_evidence");
  const reported = items.filter((i): i is Payment & { item_kind: "payment_reported" } => i.item_kind === "payment_reported");
  const payAllocs = items.filter((i): i is PaymentAllocation & { item_kind: "payment_allocation" } => i.item_kind === "payment_allocation");
  const costAllocs = items.filter((i): i is CostAllocation & { item_kind: "cost_allocation" } => i.item_kind === "cost_allocation");

  const reload = () => { q.reload(); invoicesQ.reload(); paymentsQ.reload(); };

  const confirmCostAllocs = async (a: CostAllocation) => {
    const r = await run(`ca:${a.cost_item_id}`, "/api/finance/costs/confirm-allocations", { cost_item_id: a.cost_item_id },
      { success: "Splits confirmed. They balance exactly to the item's amount." });
    if (r?.status === "ok") reload();
  };

  if (q.loading) return <GlassPanel clip><Loading label="Loading what needs matching" rows={4} /></GlassPanel>;
  if (q.error) return <GlassPanel clip><ErrorState error={q.error} onRetry={q.reload} /></GlassPanel>;
  if (!items.length && !unapplied.length) {
    return (
      <GlassPanel clip>
        <EmptyState title="Everything is matched"
          body="New ledger rows, invoices and receipts land here with a proposed match. Nothing is reconciled behind your back." />
      </GlassPanel>
    );
  }

  return (
    <div className="stack-lg">
      {evidence.length ? (
        <Section title="Cost evidence" count={evidence.length}>
          <GlassPanel clip>
            {evidence.map((e) => (
              <EvidenceCard key={e.id} e={e} canWrite={canWrite} isOwner={isOwner} refs={refs}
                onReview={(ev, mode) => setReview({ e: ev, mode })} />
            ))}
          </GlassPanel>
        </Section>
      ) : null}

      {reported.length ? (
        <Section title="Payments reported" count={reported.length}>
          <GlassPanel clip>
            {reported.map((p) => <ReportedPaymentCard key={p.id} p={p} canWrite={canWrite} onConfirm={setConfirmPay} onAllocate={setAllocatePay} />)}
          </GlassPanel>
        </Section>
      ) : null}

      {unapplied.length ? (
        <Section title="Confirmed money not yet applied" count={unapplied.length}>
          <GlassPanel clip>
            {unapplied.map((p) => (
              <div key={p.id} className="fin-row">
                <div className="fin-row__head">
                  <div className="fin-row__main">
                    <div className="fin-row__title">
                      <span className="truncate">{p.payer_name || p.payer_email || "Payer not recorded"}</span>
                      <Chip size="sm" tone="ok">{p.status_label}</Chip>
                    </div>
                    <div className="fin-row__meta">
                      {humanize(p.provider)}{p.method ? ` · ${humanize(p.method)}` : ""}
                      {p.occurred_at ? <> · <When iso={p.occurred_at} format="datetime" /></> : null}
                      {p.evidence_ref ? ` · ${p.evidence_ref}` : ""}
                    </div>
                  </div>
                  <div className="fin-row__amount"><Amt m={p.unallocated_amount} bold /></div>
                </div>
                <div className="fin-row__chips">
                  <Chip size="sm" tone="soft">Received <Amt m={p.amount} /></Chip>
                  <Chip size="sm" tone="soft">Applied <Amt m={p.allocated_amount} /></Chip>
                  {Number(p.refunded_amount?.amount ?? "0") > 0 ? <Chip size="sm" tone="risk">Refunded <Amt m={p.refunded_amount} /></Chip> : null}
                </div>
                <div className="fin-row__actions">
                  <Button size="sm" variant="primary" disabled={!canWrite} disabledReason={canWrite ? undefined : "Only the owner writes to Finance."} onClick={() => setAllocatePay(p)}>Apply to an obligation…</Button>
                </div>
              </div>
            ))}
          </GlassPanel>
        </Section>
      ) : null}

      {payAllocs.length ? (
        <Section title="Payments waiting to be applied" count={payAllocs.length}>
          <GlassPanel clip>
            {payAllocs.map((a) => (
              <PaymentAllocationCard key={a.id} a={a}
                payment={paymentById.get(a.payment_id) || null}
                invoice={invoiceById.get(a.invoice_id) || null}
                siblings={a.candidate_group ? payAllocs.filter((x) => x.candidate_group === a.candidate_group) : [a]}
                isOwner={isOwner} onConfirm={setConfirmAlloc} />
            ))}
          </GlassPanel>
        </Section>
      ) : null}

      {costAllocs.length ? (
        <Section title="Cost splits waiting for review" count={costAllocs.length}>
          <GlassPanel clip>
            {costAllocs.map((a) => (
              <CostAllocationCard key={a.id} a={a} refs={refs} isOwner={isOwner} onConfirm={confirmCostAllocs} busy={busy(`ca:${a.cost_item_id}`)} />
            ))}
          </GlassPanel>
        </Section>
      ) : null}

      <div className="set-foot">
        A quote, an emailed invoice, a ledger row and a receipt for the same purchase are one expense with several observations —
        they are never added together. Confirming a payment is not the same as applying it to an obligation.
      </div>

      {review ? (
        <EvidenceReviewDialog evidence={review.e} mode={review.mode} vehicles={refs.vehicles} canWrite={canWrite}
          onClose={() => setReview(null)} onDone={reload} />
      ) : null}
      {confirmPay ? <ConfirmPaymentDialog payment={confirmPay} canWrite={canWrite} onClose={() => setConfirmPay(null)} onDone={reload} /> : null}
      {allocatePay ? <ProposeAllocationDialog payment={allocatePay} canWrite={canWrite} onClose={() => setAllocatePay(null)} onDone={reload} /> : null}
      {confirmAlloc ? (
        <ConfirmAllocationDialog allocation={confirmAlloc} invoice={invoiceById.get(confirmAlloc.invoice_id) || null}
          siblings={confirmAlloc.candidate_group ? payAllocs.filter((x) => x.candidate_group === confirmAlloc.candidate_group) : [confirmAlloc]}
          isOwner={isOwner} onClose={() => setConfirmAlloc(null)} onDone={reload} />
      ) : null}
    </div>
  );
}
