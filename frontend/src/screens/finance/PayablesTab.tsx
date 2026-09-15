/* Payables — GET /api/finance/payables: cost items that are invoiced or partly paid, grouped by vendor.
   "Mark paid" records a paid observation (POST /api/finance/costs/observe kind=paid) — an observation with a
   reference, not a money movement. AZKT never pays anything. */
import { useMemo, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api } from "../../lib/api";
import { useQuery } from "../../lib/useQuery";
import { useCommand } from "../../lib/useCommand";
import { useIsMobile } from "../../lib/viewport";
import { humanize } from "../../lib/links";
import { Button, Chip, EmptyState, ErrorState, Field, GlassPanel, Input, Loading, Notice, ResponsiveDialog, Section, Table, Textarea, Tr, When } from "../../ui";
import { Amt, MoneyMap } from "./parts";
import type { useRefs } from "./useRefs";
import { CATEGORY_LABELS, type CostItem, type PayablesResp } from "./types";

function MarkPaidDialog({ item, canWrite, onClose, onDone }: { item: CostItem; canWrite: boolean; onClose: () => void; onDone: () => void }) {
  const isMobile = useIsMobile();
  const { run, busy } = useCommand();
  const firstRef = useRef<HTMLElement>(null);
  const [amount, setAmount] = useState(item.remaining?.amount || item.active_amount?.amount || "");
  const [sourceRef, setSourceRef] = useState("");
  const [note, setNote] = useState("");
  const key = `paid:${item.id}`;
  const blocked = !canWrite ? "Only the owner writes to Finance."
    : !amount.trim() ? "Say how much was paid."
    : !sourceRef.trim() ? "A reference is required — a bank line, a receipt number or a cheque number."
    : null;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (blocked) return;
    const r = await run(key, "/api/finance/costs/observe", {
      cost_item_id: item.id, kind: "paid", amount: amount.trim(), currency: item.currency,
      source_ref: sourceRef.trim(), note: note.trim() || null, expected_version: item.version,
    }, { success: "Paid observation recorded against this cost." });
    if (r?.status === "ok") { onDone(); onClose(); }
  };

  return (
    <ResponsiveDialog
      mobile={isMobile} open onClose={onClose} size="sm" title="Record that this was paid"
      eyebrow={item.vendor_name || CATEGORY_LABELS[item.category] || item.category}
      initialFocusRef={firstRef}
      footer={<><Button type="submit" form="mark-paid" variant="primary" loading={busy(key)} disabled={!!blocked} disabledReason={blocked || undefined}>Record payment</Button><Button variant="ghost" onClick={onClose}>Cancel</Button></>}
    >
      <form id="mark-paid" className="stack" onSubmit={submit}>
        <div className="fs13 t2">
          This records an observation that you settled this bill. AZKT does not move money and does not talk to your bank.
        </div>
        <dl className="apv-facts fs14" style={{ margin: 0 }}>
          <dt>Invoiced</dt><dd><Amt m={item.active_amount} /></dd>
          <dt>Already paid</dt><dd><Amt m={item.amount_paid} /></dd>
          <dt>Remaining</dt><dd><Amt m={item.remaining} bold /></dd>
        </dl>
        <Field label={`Amount paid (${item.currency})`} required>
          <Input ref={firstRef as React.RefObject<HTMLInputElement>} inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} />
        </Field>
        <Field label="Reference" required hint="Where this can be checked later.">
          <Input value={sourceRef} onChange={(e) => setSourceRef(e.target.value)} placeholder="e.g. Chase 09/14 ACH 8812" />
        </Field>
        <Field label="Note"><Textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)} /></Field>
      </form>
    </ResponsiveDialog>
  );
}

export function PayablesTab({ canWrite, refs }: { canWrite: boolean; refs: ReturnType<typeof useRefs> }) {
  const q = useQuery<PayablesResp>((signal) => api.get<PayablesResp>("/api/finance/payables", { signal }), []);
  const [paying, setPaying] = useState<CostItem | null>(null);

  const byVendor = useMemo(() => {
    const m = new Map<string, CostItem[]>();
    for (const c of q.data?.items || []) {
      const key = c.vendor_name || "Vendor not recorded";
      const list = m.get(key) || [];
      list.push(c);
      m.set(key, list);
    }
    return Array.from(m.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  }, [q.data]);

  if (q.loading) return <GlassPanel clip><Loading label="Loading payables" rows={3} /></GlassPanel>;
  if (q.error) return <GlassPanel clip><ErrorState error={q.error} onRetry={q.reload} /></GlassPanel>;
  if (!byVendor.length) {
    return (
      <GlassPanel clip>
        <EmptyState title="Nothing outstanding to a vendor" body="Invoiced costs with money still to pay appear here, grouped by who you owe." />
      </GlassPanel>
    );
  }

  return (
    <div className="stack-lg">
      <div className="stack-sm" style={{ gap: 2 }}>
        <span className="eyebrow">Outstanding to vendors</span>
        <span className="fs14"><MoneyMap map={q.data?.outstanding} empty="Nothing outstanding" /></span>
      </div>

      {byVendor.map(([vendor, rows]) => (
        <Section key={vendor} title={vendor} count={rows.length}>
          <GlassPanel clip>
            <Table minWidth={820} aria-label={`Payables for ${vendor}`}>
              <thead>
                <tr>
                  <th>Cost</th><th>Vehicle</th><th style={{ textAlign: "right" }}>Invoiced</th>
                  <th style={{ textAlign: "right" }}>Paid</th><th style={{ textAlign: "right" }}>Remaining</th>
                  <th>When</th><th><span className="sr-only">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((c) => (
                  <Tr key={c.id}>
                    <td>
                      <span className="stack-sm" style={{ gap: 1 }}>
                        <span>{c.description || CATEGORY_LABELS[c.category] || c.category}</span>
                        <span className="fs12 t3 row-wrap" style={{ gap: 4 }}>
                          <Chip size="sm" tone="soft">{CATEGORY_LABELS[c.category] || humanize(c.category)}</Chip>
                          <Chip size="sm" tone={c.status === "partially_paid" ? "wait" : "amber"}>{humanize(c.status)}</Chip>
                          {c.is_pass_through ? <Chip size="sm" tone="soft" title="Billed on to the customer; not a vehicle cost.">Pass-through</Chip> : null}
                          {c.shared ? <Chip size="sm" tone="soft">Split across vehicles</Chip> : null}
                          {c.invoice_ref ? <span className="nowrap">{c.invoice_ref}</span> : null}
                        </span>
                      </span>
                    </td>
                    <td>
                      {c.vehicle_id ? <Link to={`/vehicles/${c.vehicle_id}?tab=money`}>{refs.vehicleName(c.vehicle_id)}</Link>
                        : c.shared ? <span className="t3">Split — see the item</span> : <span className="t3">Not assigned</span>}
                    </td>
                    <td style={{ textAlign: "right" }}><Amt m={c.active_amount} /></td>
                    <td style={{ textAlign: "right" }}><Amt m={c.amount_paid} /></td>
                    <td style={{ textAlign: "right" }}><Amt m={c.remaining} bold /></td>
                    <td className="t3 fs13">{c.occurred_at ? <When iso={c.occurred_at} format="date" /> : "Not recorded"}</td>
                    <td className="actions">
                      <Button size="xs" variant="soft" disabled={!canWrite} disabledReason="Only the owner writes to Finance." onClick={() => setPaying(c)}>Mark paid…</Button>
                    </td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          </GlassPanel>
        </Section>
      ))}

      <Notice tone="neutral" lead="AZKT never pays anyone">
        Marking a bill paid records what you did, with the reference to check it against. No card is charged and no transfer is made from here.
      </Notice>

      {paying ? <MarkPaidDialog item={paying} canWrite={canWrite} onClose={() => setPaying(null)} onDone={q.reload} /> : null}
    </div>
  );
}
