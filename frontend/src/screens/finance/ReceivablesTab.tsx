/* Receivables — GET /api/finance/receivables. What customers owe: kind, who, due, allocated, remaining, state.
   The owner can record a payment that arrived outside a provider, from verified evidence
   (POST /api/finance/payments/record-manual-confirmed). Confirming is not allocating. */
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../lib/api";
import { useQuery } from "../../lib/useQuery";
import { humanize } from "../../lib/links";
import { Button, Chip, EmptyState, ErrorState, GlassPanel, Loading, Notice, Table, Tr, When } from "../../ui";
import { Amt, MoneyMap } from "./parts";
import { ConfirmPaymentDialog } from "./dialogs";
import type { useRefs } from "./useRefs";
import { INVOICE_KIND_LABELS, invoiceStatusTone, type ReceivablesResp } from "./types";

export function ReceivablesTab({ canWrite, refs }: { canWrite: boolean; refs: ReturnType<typeof useRefs> }) {
  const q = useQuery<ReceivablesResp>((signal) => api.get<ReceivablesResp>("/api/finance/receivables", { signal }), []);
  const [recording, setRecording] = useState(false);

  const items = q.data?.items || [];
  const now = Date.now();

  return (
    <div className="stack">
      <div className="between">
        <div className="stack-sm" style={{ gap: 2 }}>
          <span className="eyebrow">Outstanding</span>
          <span className="fs14"><MoneyMap map={q.data?.outstanding} empty="Nothing outstanding" /></span>
        </div>
        <Button size="md" variant="primary" disabled={!canWrite} disabledReason="Only the owner records a confirmed payment." onClick={() => setRecording(true)}>
          Record a payment…
        </Button>
      </div>

      <GlassPanel clip>
        {q.loading ? <Loading label="Loading receivables" rows={3} /> : q.error ? <ErrorState error={q.error} onRetry={q.reload} /> : !items.length ? (
          <EmptyState title="Nothing owed to you" body="Deposits, balances, reservation fees and shipping obligations appear here while they are open." />
        ) : (
          <Table minWidth={860} aria-label="Receivables">
            <thead>
              <tr>
                <th>Obligation</th><th>Who</th><th style={{ textAlign: "right" }}>Due</th>
                <th style={{ textAlign: "right" }}>Applied</th><th style={{ textAlign: "right" }}>Remaining</th>
                <th>State</th><th>By when</th>
              </tr>
            </thead>
            <tbody>
              {items.map((i) => {
                const overdue = !!i.due_at && new Date(i.due_at).getTime() < now && i.status !== "paid";
                return (
                  <Tr key={i.id}>
                    <td>
                      <span className="stack-sm" style={{ gap: 1 }}>
                        <span>{INVOICE_KIND_LABELS[i.kind] || humanize(i.kind)}</span>
                        <span className="fs12 t3">
                          {i.vehicle_id ? <Link to={`/vehicles/${i.vehicle_id}?tab=money`}>{refs.vehicleName(i.vehicle_id)}</Link> : `Obligation ${i.id.slice(0, 8)}`}
                        </span>
                      </span>
                    </td>
                    <td>{i.contact_id ? <Link to={`/contacts/${i.contact_id}`}>{refs.contactName(i.contact_id)}</Link> : <span className="t3">Not recorded</span>}</td>
                    <td style={{ textAlign: "right" }}><Amt m={i.amount_due} /></td>
                    <td style={{ textAlign: "right" }}><Amt m={i.amount_allocated} /></td>
                    <td style={{ textAlign: "right" }}>
                      <Amt m={i.remaining} bold />
                      {i.overpaid_by ? <div className="fs12" style={{ color: "var(--risk)" }}>Overpaid by <Amt m={i.overpaid_by} /></div> : null}
                    </td>
                    <td>
                      <span className="row-wrap" style={{ gap: 4 }}>
                        <Chip size="sm" tone={invoiceStatusTone(i.status)}>{humanize(i.status)}</Chip>
                        {i.exceptions?.length ? <Chip size="sm" tone="blocked" count={i.exceptions.length}>exception</Chip> : null}
                        {i.reopened_at ? <Chip size="sm" tone="risk">Reopened</Chip> : null}
                      </span>
                    </td>
                    <td className="t3 fs13">
                      {i.due_at ? <span style={overdue ? { color: "var(--blocked)" } : undefined}><When iso={i.due_at} format="date" />{overdue ? " · overdue" : ""}</span> : "No date agreed"}
                    </td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        )}
      </GlassPanel>

      {items.some((i) => i.status === "overpaid") ? (
        <Notice tone="risk" lead="Overpaid obligation">The extra money stays on record as an overpayment. It is not counted as revenue and does not satisfy anything else.</Notice>
      ) : null}
      <div className="set-foot">
        An obligation is satisfied only by confirmed, correctly applied payments. A customer saying they paid, or an email whose
        subject says "paid", never moves an obligation on its own.
      </div>

      {recording ? <ConfirmPaymentDialog payment={null} canWrite={canWrite} onClose={() => setRecording(false)} onDone={q.reload} /> : null}
    </div>
  );
}
