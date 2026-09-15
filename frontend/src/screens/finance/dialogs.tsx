/* Review dialogs for the Needs matching tab.
   POST /api/finance/costs/confirm-match | correct-match | leave-unmatched | confirm-allocations
   POST /api/finance/payments/record-manual-confirmed | propose-allocation | confirm-allocation
   Every one of them is a command; the CommandResult envelope is explained by useCommand(). */
import { useMemo, useRef, useState, type FormEvent } from "react";
import { useCommand } from "../../lib/useCommand";
import { useIsMobile } from "../../lib/viewport";
import { humanize } from "../../lib/links";
import { Button, Chip, Field, Input, Loading, Notice, ResponsiveDialog, Select, Switch, Textarea, When } from "../../ui";
import { Amt, DiscrepancyBlock, Reasons } from "./parts";
import { useCostItems, useOpenInvoices, type VehicleRef } from "./useRefs";
import {
  ALLOCATION_FLAG_LABELS, CATEGORY_LABELS, COST_CATEGORIES, EVIDENCE_KIND_LABELS, INVOICE_KIND_LABELS, PAYMENT_METHODS,
  type CostItem, type Evidence, type Invoice, type Payment, type PaymentAllocation,
} from "./types";

function toIso(local: string): string | null {
  if (!local) return null;
  const d = new Date(local);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

function costItemLabel(c: CostItem): string {
  const who = c.vendor_name ? `${c.vendor_name} · ` : "";
  const ref = c.invoice_ref ? ` · ${c.invoice_ref}` : "";
  return `${who}${c.description || CATEGORY_LABELS[c.category] || c.category}${ref}`;
}

/** Search + choose one cost item. Used by Confirm (ambiguous) and Correct. */
function CostItemPicker({ value, onChange, enabled }: { value: string | null; onChange: (id: string | null) => void; enabled: boolean }) {
  const { items, loading, error } = useCostItems(enabled);
  const [q, setQ] = useState("");
  const list = useMemo(() => {
    const all = items || [];
    const needle = q.trim().toLowerCase();
    const filtered = needle
      ? all.filter((c) => `${c.vendor_name || ""} ${c.description || ""} ${c.invoice_ref || ""} ${c.order_ref || ""} ${c.category}`.toLowerCase().includes(needle))
      : all;
    return filtered.slice(0, 40);
  }, [items, q]);
  const chosen = (items || []).find((c) => c.id === value) || null;

  return (
    <div className="stack-sm">
      <Field label="Cost item" hint="One economic expense. A quote, an invoice and a receipt for the same purchase belong to the same item.">
        <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search vendor, description or invoice number" />
      </Field>
      {chosen ? (
        <div className="row-wrap">
          <Chip tone="act" onRemove={() => onChange(null)} removeLabel="Clear the chosen cost item">{costItemLabel(chosen)}</Chip>
          <span className="fs12 t3"><Amt m={chosen.active_amount} /> · {humanize(chosen.status)}</span>
        </div>
      ) : null}
      <div className="fin-picker" role="listbox" aria-label="Cost items">
        {loading ? <Loading label="Loading cost items" rows={2} /> : null}
        {error ? <span className="fs13" style={{ color: "var(--blocked)", padding: "8px 10px" }}>Cost items could not be loaded.</span> : null}
        {!loading && !error && !list.length ? <span className="fs13 t3" style={{ padding: "8px 10px" }}>No cost item matches that.</span> : null}
        {list.map((c) => (
          <button key={c.id} type="button" role="option" aria-selected={c.id === value} className="fin-picker__item"
            onClick={() => onChange(c.id === value ? null : c.id)}>
            <span className="truncate">{costItemLabel(c)}</span>
            <span className="fs12 t3 nowrap"><Amt m={c.active_amount} /> · {humanize(c.status)}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

export type EvidenceMode = "confirm" | "correct" | "leave";

export function EvidenceReviewDialog({
  evidence, mode, vehicles, canWrite, onClose, onDone,
}: {
  evidence: Evidence; mode: EvidenceMode; vehicles: VehicleRef[]; canWrite: boolean;
  onClose: () => void; onDone: () => void;
}) {
  const isMobile = useIsMobile();
  const { run, busy } = useCommand();
  const firstRef = useRef<HTMLElement>(null);

  const defaultItem = evidence.cost_item_id || evidence.proposed_cost_item_id
    || (evidence.candidates?.length === 1 ? evidence.candidates[0].cost_item_id : null);
  const [costItemId, setCostItemId] = useState<string | null>(mode === "correct" ? null : defaultItem);
  const [vehicleId, setVehicleId] = useState<string>(mode === "correct" ? (evidence.proposed_vehicle_id || "") : "");
  const [category, setCategory] = useState<string>(mode === "correct" ? (evidence.proposed_category || "") : "");
  const [resolve, setResolve] = useState<string>("");
  const [note, setNote] = useState("");
  const [ignore, setIgnore] = useState(false);

  const conflict = evidence.match_state === "conflict" && !!Object.keys(evidence.discrepancy || {}).length;
  const key = `ev:${mode}:${evidence.id}`;

  let blocked: string | null = null;
  if (!canWrite) blocked = "Only the owner writes to Finance.";
  else if (mode === "confirm" && !costItemId) blocked = "Choose which cost item this belongs to.";
  else if (mode === "confirm" && conflict && !resolve) blocked = "Say which value is right before confirming.";
  else if (mode === "correct" && !costItemId && !evidence.cost_item_id) blocked = "This evidence isn't linked yet — pick the cost item it belongs to.";
  else if (mode === "correct" && !costItemId && !vehicleId && !category) blocked = "Change the vehicle, the category or the cost item.";
  else if (mode === "leave" && !note.trim()) blocked = "A reason is required — nothing is deleted, so say why.";

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (blocked) return;
    const base = { evidence_id: evidence.id, expected_version: evidence.version };
    let path = "/api/finance/costs/confirm-match";
    let body: Record<string, unknown> = { ...base, cost_item_id: costItemId, note: note.trim() || null };
    let success = "Match confirmed.";
    if (mode === "confirm" && conflict) body.resolve_discrepancy = resolve;
    if (mode === "correct") {
      path = "/api/finance/costs/correct-match";
      body = { ...base, cost_item_id: costItemId || null, vehicle_id: vehicleId || null, category: category || null, note: note.trim() || null };
      success = "Match corrected.";
    }
    if (mode === "leave") {
      path = "/api/finance/costs/leave-unmatched";
      body = { ...base, reason: note.trim(), ignore };
      success = ignore ? "Left ignored. It stays on record." : "Left unmatched. It stays on record.";
    }
    const r = await run(key, path, body, { success });
    if (r?.status === "ok") { onDone(); onClose(); }
  };

  const title = mode === "confirm" ? "Confirm this match" : mode === "correct" ? "Correct this match" : "Leave unmatched";

  return (
    <ResponsiveDialog
      mobile={isMobile} open onClose={onClose} size="lg" align="top" title={title}
      eyebrow={`${EVIDENCE_KIND_LABELS[evidence.kind] || humanize(evidence.kind)}${evidence.vendor ? ` · ${evidence.vendor}` : ""}`}
      initialFocusRef={firstRef}
      footer={
        <>
          <Button type="submit" form="ev-review" variant={mode === "leave" ? "danger" : "primary"} loading={busy(key)}
            disabled={!!blocked} disabledReason={blocked || undefined}>
            {mode === "confirm" ? "Confirm match" : mode === "correct" ? "Save correction" : ignore ? "Ignore it" : "Leave unmatched"}
          </Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form id="ev-review" className="stack" onSubmit={submit}>
        <dl className="apv-facts fs14" style={{ margin: 0 }}>
          <dt>Amount</dt><dd><Amt m={evidence.amount} bold /></dd>
          <dt>Vendor</dt><dd>{evidence.vendor || "Not recorded"}</dd>
          <dt>Reference</dt><dd>{evidence.invoice_no || evidence.order_no || "Not recorded"}</dd>
          <dt>Date</dt><dd>{evidence.occurred_at ? <When iso={evidence.occurred_at} format="date" /> : "Not recorded"}</dd>
          <dt>Vehicle text</dt><dd>{evidence.vehicle_ref_text || "Not recorded"}</dd>
        </dl>

        {conflict ? <DiscrepancyBlock d={evidence.discrepancy} /> : null}
        <Reasons items={evidence.match_reasons} />

        {mode === "confirm" ? (
          <>
            {(evidence.candidates || []).length > 1 ? (
              <Field label="Which cost item is this?" hint="Several items share this supplier and reference. Nothing is picked for you.">
                <div className="opts">
                  {evidence.candidates.map((c) => (
                    <button key={c.cost_item_id} type="button" role="radio" aria-checked={costItemId === c.cost_item_id} className="opt"
                      onClick={() => setCostItemId(c.cost_item_id)}>
                      <span className="opt__mark" aria-hidden="true" />
                      <span className="stack-sm" style={{ gap: 2 }}>
                        <span className="opt__label">Cost item {c.cost_item_id.slice(0, 8)}</span>
                        <span className="opt__desc">{c.reasons.join(" · ")}</span>
                      </span>
                    </button>
                  ))}
                </div>
              </Field>
            ) : costItemId ? (
              <Notice tone="wait" lead="Links to">Cost item {costItemId.slice(0, 8)}{evidence.cost_item_id === costItemId ? " (already linked)" : ""}</Notice>
            ) : (
              <>
                <Notice tone="risk" lead="No cost item">Nothing proposed for this evidence. Pick one below, or use Correct.</Notice>
                <CostItemPicker value={costItemId} onChange={setCostItemId} enabled />
              </>
            )}
            {conflict ? (
              <Field label="Which value is right?" hint="The other value stays on record as a disagreement. Nothing is averaged.">
                <div className="opts">
                  {[["evidence", "The evidence", `${evidence.discrepancy.evidence ?? ""}`], ["item", "The recorded cost item", `${evidence.discrepancy.item ?? ""}`]].map(([v, label, val]) => (
                    <button key={v} type="button" role="radio" aria-checked={resolve === v} className="opt" onClick={() => setResolve(v)}>
                      <span className="opt__mark" aria-hidden="true" />
                      <span className="stack-sm" style={{ gap: 2 }}>
                        <span className="opt__label">{label}</span>
                        <span className="opt__desc">{val}</span>
                      </span>
                    </button>
                  ))}
                </div>
              </Field>
            ) : null}
          </>
        ) : null}

        {mode === "correct" ? (
          <>
            <Field label="Vehicle" hint="Leave blank to keep whatever the item already points at.">
              <Select ref={firstRef as React.RefObject<HTMLSelectElement>} value={vehicleId} onChange={(e) => setVehicleId(e.target.value)}>
                <option value="">No change</option>
                {vehicles.map((v) => <option key={v.id} value={v.id}>{v.title || v.stock_no || v.id.slice(0, 8)}{v.stock_no && v.title ? ` · ${v.stock_no}` : ""}</option>)}
              </Select>
            </Field>
            <Field label="Category">
              <Select value={category} onChange={(e) => setCategory(e.target.value)}>
                <option value="">No change</option>
                {COST_CATEGORIES.map((c) => <option key={c} value={c}>{CATEGORY_LABELS[c]}</option>)}
              </Select>
            </Field>
            <CostItemPicker value={costItemId} onChange={setCostItemId} enabled />
          </>
        ) : null}

        <Field label={mode === "leave" ? "Reason (required)" : "Note (optional)"}
          hint={mode === "leave" ? "Recorded on the evidence and in Activity. Nothing is deleted." : "Recorded with the review."}>
          <Textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)}
            placeholder={mode === "leave" ? "e.g. personal card charge, not a vehicle cost" : ""} />
        </Field>
        {mode === "leave" ? (
          <Switch checked={ignore} onChange={setIgnore} label="Ignore it entirely" meta="Stops it coming back on this tab; the row stays on record" />
        ) : null}
      </form>
    </ResponsiveDialog>
  );
}

/* ── payments ─────────────────────────────────────────────────────────── */

export function ConfirmPaymentDialog({
  payment, canWrite, onClose, onDone,
}: { payment: Payment | null; canWrite: boolean; onClose: () => void; onDone: () => void }) {
  const isMobile = useIsMobile();
  const { run, busy } = useCommand();
  const firstRef = useRef<HTMLElement>(null);
  const [amount, setAmount] = useState(payment?.amount?.amount || "");
  const [currency, setCurrency] = useState(payment?.amount?.currency || "USD");
  const [method, setMethod] = useState<string>("wire");
  const [evidenceRef, setEvidenceRef] = useState("");
  const [assetId, setAssetId] = useState("");
  const [payer, setPayer] = useState(payment?.payer_name || "");
  const [occurred, setOccurred] = useState("");
  const [note, setNote] = useState("");

  const key = `pay:confirm:${payment?.id || "new"}`;
  const blocked = !canWrite ? "Only the owner confirms a payment."
    : !amount.trim() ? "An amount is required."
    : !evidenceRef.trim() && !assetId.trim() ? "Verified evidence is required — a bank line reference or an uploaded receipt."
    : null;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (blocked) return;
    const r = await run(key, "/api/finance/payments/record-manual-confirmed", {
      amount: amount.trim(), currency: currency.trim().toUpperCase() || "USD", method,
      evidence_ref: evidenceRef.trim() || null, evidence_asset_id: assetId.trim() || null,
      payer_name: payer.trim() || null, contact_id: payment?.contact_id || null,
      occurred_at: toIso(occurred), reported_payment_id: payment?.id || null, note: note.trim() || null,
    }, { success: "Payment confirmed from verified evidence. Confirming is not allocating." });
    if (r?.status === "ok") { onDone(); onClose(); }
  };

  return (
    <ResponsiveDialog
      mobile={isMobile} open onClose={onClose} size="md" align="top"
      title={payment ? "Confirm this reported payment" : "Record a confirmed payment"}
      eyebrow={payment ? payment.status_label : "Manual evidence"}
      initialFocusRef={firstRef}
      footer={<><Button type="submit" form="pay-confirm" variant="primary" loading={busy(key)} disabled={!!blocked} disabledReason={blocked || undefined}>Confirm payment</Button><Button variant="ghost" onClick={onClose}>Cancel</Button></>}
    >
      <form id="pay-confirm" className="stack" onSubmit={submit}>
        {payment?.report_flags?.length ? (
          <Notice tone="risk" lead="Reported, not verified">{payment.report_flags.map((f) => humanize(f)).join(" · ")}</Notice>
        ) : null}
        <div className="fs13 t2">Confirming records that the money arrived. It does not apply the payment to any obligation — that is a separate, explicit step.</div>
        <div className="form-grid">
          <Field label="Amount as verified" required>
            <Input ref={firstRef as React.RefObject<HTMLInputElement>} inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="0.00" />
          </Field>
          <Field label="Currency" required><Input value={currency} maxLength={3} onChange={(e) => setCurrency(e.target.value.toUpperCase())} /></Field>
          <Field label="How it arrived">
            <Select value={method} onChange={(e) => setMethod(e.target.value)}>
              {PAYMENT_METHODS.map((m) => <option key={m} value={m}>{humanize(m)}</option>)}
            </Select>
          </Field>
          <Field label="When it arrived" hint="Blank = now."><Input type="datetime-local" value={occurred} onChange={(e) => setOccurred(e.target.value)} /></Field>
        </div>
        <Field label="Bank line or receipt reference" hint="One of these two is required. A claim that money was sent is not evidence.">
          <Input value={evidenceRef} onChange={(e) => setEvidenceRef(e.target.value)} placeholder="e.g. Chase 09/12 wire 4471" />
        </Field>
        <Field label="Uploaded evidence id" hint="An asset id if you filed the receipt in AZKT.">
          <Input value={assetId} onChange={(e) => setAssetId(e.target.value)} placeholder="asset id" />
        </Field>
        <Field label="Payer"><Input value={payer} onChange={(e) => setPayer(e.target.value)} placeholder="Who paid" /></Field>
        <Field label="Note"><Input value={note} onChange={(e) => setNote(e.target.value)} /></Field>
        {payment && payment.amount && amount.trim() && amount.trim() !== payment.amount.amount ? (
          <Notice tone="risk" lead="Different from the claim">The claim said <Amt m={payment.amount} />. The difference is kept on the payment as an exception.</Notice>
        ) : null}
      </form>
    </ResponsiveDialog>
  );
}

export function ProposeAllocationDialog({
  payment, canWrite, onClose, onDone,
}: { payment: Payment; canWrite: boolean; onClose: () => void; onDone: () => void }) {
  const isMobile = useIsMobile();
  const { run, busy } = useCommand();
  const { items, loading } = useOpenInvoices(true);
  const [invoiceId, setInvoiceId] = useState("");
  const [amount, setAmount] = useState("");
  const [rate, setRate] = useState("");
  const [source, setSource] = useState("");
  const [fxDate, setFxDate] = useState("");
  const [note, setNote] = useState("");

  const chosen = (items || []).find((i) => i.id === invoiceId) || null;
  const mismatch = !!chosen && !!payment.amount && chosen.amount_due?.currency !== payment.amount.currency;
  const key = `pay:propose:${payment.id}`;
  const blocked = !canWrite ? "Only the owner writes to Finance."
    : payment.is_payout ? "A payout to the bank is not a customer payment."
    : mismatch && (!rate.trim() || !source.trim()) ? "A cross-currency allocation needs an explicit rate and its source."
    : null;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (blocked) return;
    const r = await run(key, "/api/finance/payments/propose-allocation", {
      payment_id: payment.id,
      invoice_id: invoiceId || null,
      amount: amount.trim() || null,
      conversion: mismatch ? { rate: rate.trim(), source: source.trim(), date: fxDate || null } : null,
      note: note.trim() || null,
    }, { success: "Allocation proposed. It still needs your confirmation." });
    if (r?.status === "ok") { onDone(); onClose(); }
  };

  return (
    <ResponsiveDialog
      mobile={isMobile} open onClose={onClose} size="md" align="top" title="Apply this payment to an obligation"
      eyebrow={`${payment.provider} · ${payment.status_label}`}
      footer={<><Button type="submit" form="pay-propose" variant="primary" loading={busy(key)} disabled={!!blocked} disabledReason={blocked || undefined}>Propose allocation</Button><Button variant="ghost" onClick={onClose}>Cancel</Button></>}
    >
      <form id="pay-propose" className="stack" onSubmit={submit}>
        <dl className="apv-facts fs14" style={{ margin: 0 }}>
          <dt>Payment</dt><dd><Amt m={payment.amount} bold /> · {payment.payer_name || "payer not recorded"}</dd>
          <dt>Unallocated</dt><dd><Amt m={payment.unallocated_amount} /></dd>
        </dl>
        <Field label="Obligation" hint="Leave on 'let AZKT find candidates' and it proposes every open obligation that could be this payment — it never picks between them.">
          {loading ? <Loading label="Loading obligations" rows={1} /> : (
            <Select value={invoiceId} onChange={(e) => setInvoiceId(e.target.value)}>
              <option value="">Let AZKT find candidates</option>
              {(items || []).map((i) => (
                <option key={i.id} value={i.id}>
                  {INVOICE_KIND_LABELS[i.kind] || humanize(i.kind)} · {i.remaining ? `${i.remaining.amount} ${i.remaining.currency} remaining` : "amount not recorded"} · {i.id.slice(0, 8)}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <Field label="Amount to apply" hint="Blank applies the smaller of what's left on the payment and what's owed. Type a larger amount to record an overpayment explicitly.">
          <Input inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="whole remaining amount" />
        </Field>
        {mismatch ? (
          <>
            <Notice tone="risk" lead="Different currencies">
              The payment is in {payment.amount?.currency} and the obligation in {chosen?.amount_due?.currency}. AZKT will not guess a rate.
            </Notice>
            <div className="form-grid">
              <Field label="Rate" required><Input inputMode="decimal" value={rate} onChange={(e) => setRate(e.target.value)} /></Field>
              <Field label="Rate source" required><Input value={source} onChange={(e) => setSource(e.target.value)} placeholder="e.g. bank advice 09/12" /></Field>
              <Field label="Rate date"><Input type="date" value={fxDate} onChange={(e) => setFxDate(e.target.value)} /></Field>
            </div>
          </>
        ) : null}
        <Field label="Note"><Input value={note} onChange={(e) => setNote(e.target.value)} /></Field>
      </form>
    </ResponsiveDialog>
  );
}

export function ConfirmAllocationDialog({
  allocation, invoice, siblings, isOwner, onClose, onDone,
}: {
  allocation: PaymentAllocation; invoice: Invoice | null; siblings: PaymentAllocation[];
  isOwner: boolean; onClose: () => void; onDone: () => void;
}) {
  const isMobile = useIsMobile();
  const { run, busy } = useCommand();
  const [amount, setAmount] = useState("");
  const [rate, setRate] = useState((allocation.conversion?.rate as string) || "");
  const [source, setSource] = useState((allocation.conversion?.source as string) || "");
  const [note, setNote] = useState("");

  const needsConversion = allocation.flags.includes("conversion_required");
  const ambiguous = allocation.flags.includes("ambiguous") || allocation.ambiguous;
  const key = `alloc:confirm:${allocation.id}`;
  const blocked = !isOwner ? "Only the owner confirms an allocation."
    : needsConversion && (!rate.trim() || !source.trim()) ? "Give the conversion rate and where it came from."
    : null;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (blocked) return;
    const r = await run(key, "/api/finance/payments/confirm-allocation", {
      allocation_id: allocation.id, expected_version: allocation.version,
      amount: amount.trim() || null,
      conversion: needsConversion ? { rate: rate.trim(), source: source.trim() } : null,
      note: note.trim() || null,
    }, { success: "Allocation confirmed." });
    if (r?.status === "ok") { onDone(); onClose(); }
  };

  return (
    <ResponsiveDialog
      mobile={isMobile} open onClose={onClose} size="md" align="top" title="Confirm this allocation"
      eyebrow={invoice ? `${INVOICE_KIND_LABELS[invoice.kind] || humanize(invoice.kind)} · ${invoice.id.slice(0, 8)}` : "Obligation"}
      footer={<><Button type="submit" form="alloc-confirm" variant="primary" loading={busy(key)} disabled={!!blocked} disabledReason={blocked || undefined}>Confirm allocation</Button><Button variant="ghost" onClick={onClose}>Cancel</Button></>}
    >
      <form id="alloc-confirm" className="stack" onSubmit={submit}>
        <dl className="apv-facts fs14" style={{ margin: 0 }}>
          <dt>Applying</dt><dd><Amt m={allocation.amount} bold /></dd>
          <dt>Obligation</dt><dd>{invoice ? <><Amt m={invoice.amount_due} /> due · <Amt m={invoice.remaining} /> remaining</> : "Not loaded"}</dd>
        </dl>
        {allocation.flags.length ? (
          <div className="row-wrap">
            {allocation.flags.map((f) => <Chip key={f} size="sm" tone={f === "partial" ? "wait" : "risk"}>{ALLOCATION_FLAG_LABELS[f] || humanize(f)}</Chip>)}
          </div>
        ) : null}
        {ambiguous && siblings.length > 1 ? (
          <Notice tone="risk" lead="Two obligations could be this payment" role="alert">
            Confirming this one leaves the {siblings.length - 1} other candidate{siblings.length > 2 ? "s" : ""} untouched. Nothing is guessed on your behalf.
          </Notice>
        ) : null}
        {allocation.flags.includes("overpayment") ? (
          <Notice tone="risk" lead="More than is owed">The extra stays recorded as an overpayment on the obligation; it is not turned into revenue.</Notice>
        ) : null}
        {allocation.flags.includes("partial") ? (
          <Notice tone="wait" lead="Less than is owed">The obligation stays partially paid. A deposit gate only opens when the configured terms are fully met.</Notice>
        ) : null}
        <Field label="Amount to confirm" hint="Blank confirms the proposed amount exactly."><Input inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder={allocation.amount?.amount || ""} /></Field>
        {needsConversion ? (
          <div className="form-grid">
            <Field label="Conversion rate" required><Input inputMode="decimal" value={rate} onChange={(e) => setRate(e.target.value)} /></Field>
            <Field label="Rate source" required><Input value={source} onChange={(e) => setSource(e.target.value)} /></Field>
          </div>
        ) : null}
        <Field label="Note"><Input value={note} onChange={(e) => setNote(e.target.value)} /></Field>
      </form>
    </ResponsiveDialog>
  );
}
