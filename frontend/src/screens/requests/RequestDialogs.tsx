/* Dialogs for the import request page. Each one carries the exact evidence its command demands —
   a signed agreement needs a source reference, a must-have change needs buyer evidence and bumps the
   requirements version, a purchase needs its own evidence. Nothing here invents a value. */
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useCommand } from "../../lib/useCommand";
import { useIsMobile } from "../../lib/viewport";
import { Button, Chip, Expander, Field, Input, Notice, ResponsiveDialog, Select, Textarea } from "../../ui";
import { EvidenceUpload } from "./components/EvidenceUpload";
import { criterionText } from "./components/RequirementTiers";
import { CURRENCIES, requestAction, matchAction } from "./api";
import { OPS, TIERS, TIER_LABEL, type ImportRequest, type Requirement, type Tier } from "./types";

type Done = () => void;
interface Base { open: boolean; onClose: () => void; r: ImportRequest; onDone: Done }

/* ---------- pause / resume ---------- */
export function PauseDialog({ open, onClose, r, onDone }: Base) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [reason, setReason] = useState("");
  useEffect(() => { if (open) setReason(r.paused ? "" : ""); }, [open, r.paused]);
  if (!open) return null;
  const resuming = r.paused;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const res = await run("pause", requestAction(r.id, resuming ? "resume" : "pause"), {
      reason: reason.trim() || null, expected_version: r.version,
    }, { success: resuming ? "Search resumed" : "Search paused" });
    if (res?.status === "ok") onDone();
  };
  return (
    <ResponsiveDialog
      mobile={mobile} open onClose={onClose} size="sm"
      title={resuming ? "Resume this search" : "Pause this search"}
      description={resuming ? "Candidates start being evaluated again. Exclusions are kept." : "Matching and bids stop. Nothing already recorded is lost."}
      footer={
        <>
          <Button variant="primary" onClick={submit} loading={busy("pause")}>{resuming ? "Resume" : "Pause"}</Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <Field label="Reason" hint="Optional, but it is what everyone sees next to the paused badge.">
          <Textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2} placeholder={resuming ? "Buyer is ready again" : "Buyer travelling until October"} />
        </Field>
      </form>
    </ResponsiveDialog>
  );
}

/* ---------- requirements revision ---------- */
interface Row { key: string; tier: Tier; text: string; field: string; op: string; value: string }

function toRow(r: Requirement): Row {
  const v = r.value;
  return {
    key: r.key,
    tier: r.tier,
    text: r.text || "",
    field: r.field || "",
    op: r.op || "eq",
    value: v === null || v === undefined ? "" : Array.isArray(v) ? v.join(", ") : String(v),
  };
}

function parseValue(op: string, raw: string): unknown {
  const s = raw.trim();
  if (op === "in" || op === "not_in") return s.split(",").map((x) => x.trim()).filter(Boolean);
  if (s === "true") return true;
  if (s === "false") return false;
  if (s !== "" && Number.isFinite(Number(s))) return Number(s);
  return s;
}

function toPayload(rows: Row[]): Array<Record<string, unknown>> {
  return rows.filter((x) => x.text.trim() || x.key).map((x) => {
    const out: Record<string, unknown> = { key: x.key || undefined, tier: x.tier, text: x.text.trim() };
    if (x.field.trim()) {
      out.field = x.field.trim();
      out.op = x.op;
      if (x.op !== "truthy" && x.op !== "falsy") out.value = parseValue(x.op, x.value);
    }
    return out;
  });
}

/** Mirror of services/requirements.must_set: a must-have's key, text or check changing is a revision. */
function mustSignature(rows: Array<{ key?: string; tier: string; text?: string; field?: unknown; op?: unknown; value?: unknown }>): string {
  const m = rows.filter((x) => x.tier === "must").map((x) => JSON.stringify([x.key || x.text, x.field ?? null, x.op ?? null, x.value ?? null, x.text ?? null]));
  return m.sort().join("|");
}

export function ReviseRequirementsDialog({ open, onClose, r, onDone }: Base) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [rows, setRows] = useState<Row[]>([]);
  const [sourceRef, setSourceRef] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => {
    if (!open) return;
    setRows((r.requirements || []).map(toRow));
    setSourceRef("");
    setNote("");
  }, [open, r.requirements]);

  const before = useMemo(() => mustSignature((r.requirements || []).map((x) => ({ key: x.key, tier: x.tier, text: x.text, field: x.field, op: x.op, value: x.value }))), [r.requirements]);
  const after = useMemo(() => mustSignature(toPayload(rows).map((x) => ({ key: x.key as string | undefined, tier: String(x.tier), text: x.text as string, field: x.field, op: x.op, value: x.value }))), [rows]);
  const mustChanged = before !== after;
  const needsEvidence = mustChanged && !sourceRef.trim();

  if (!open) return null;
  const set = (i: number, patch: Partial<Row>) => setRows((xs) => xs.map((x, n) => (n === i ? { ...x, ...patch } : x)));
  const add = (tier: Tier) => setRows((xs) => [...xs, { key: "", tier, text: "", field: "", op: "eq", value: "" }]);
  const remove = (i: number) => setRows((xs) => xs.filter((_, n) => n !== i));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (needsEvidence) return;
    const res = await run("revise", requestAction(r.id, "revise-requirements"), {
      requirements: toPayload(rows),
      source_ref: sourceRef.trim() || null,
      note: note.trim() || null,
      expected_version: r.version,
    }, { success: "Requirements revised" });
    if (res?.status === "ok") onDone();
  };

  return (
    <ResponsiveDialog
      mobile={mobile} open onClose={onClose} size="xl" align="top"
      title="Revise requirements"
      description={`Currently v${r.requirements_version}. Saving records a new version; matches evaluated against the old one are marked for re-evaluation.`}
      footer={
        <>
          <Button variant="primary" onClick={submit} loading={busy("revise")} disabled={needsEvidence} disabledReason="A must-have changed — add the buyer evidence first.">
            Save as v{r.requirements_version + 1}
          </Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          {mustChanged ? <span className="apv-actions__note">Must-haves changed</span> : <span className="apv-actions__note">Must-haves unchanged</span>}
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        {mustChanged ? (
          <Notice tone="amber" lead="A must-have changed">
            Changing what the buyer must have needs their own evidence — the message, call note or signed change that says so. AZKT records it against the new version.
          </Notice>
        ) : null}

        <div className="req-edit">
          {TIERS.map((tier) => (
            <div key={tier} className="stack-sm">
              <div className="req__tier-title">
                <span>{TIER_LABEL[tier]}</span>
                <Button size="xs" variant="soft" onClick={() => add(tier)}>Add line</Button>
              </div>
              {rows.map((row, i) => (row.tier !== tier ? null : (
                <div key={i} className="req__line">
                  <div className="req-edit__row">
                    <Input value={row.text} onChange={(e) => set(i, { text: e.target.value })} placeholder="What the buyer said" aria-label={`${TIER_LABEL[tier]} requirement`} />
                    <Button size="sm" variant="ghost" onClick={() => remove(i)} aria-label={`Remove ${row.text || "requirement"}`}>Remove</Button>
                  </div>
                  <Expander title={row.field ? `Check: ${criterionText({ key: row.key, tier: row.tier, text: row.text, field: row.field, op: row.op, value: parseValue(row.op, row.value) })}` : "Add a checkable criterion (otherwise this stays Unknown)"}>
                    <div className="req-edit__check">
                      <Field label="Snapshot field"><Input value={row.field} onChange={(e) => set(i, { field: e.target.value })} placeholder="transmission" /></Field>
                      <Field label="Comparison">
                        <Select value={row.op} onChange={(e) => set(i, { op: e.target.value })}>
                          {OPS.map((o) => <option key={o} value={o}>{o}</option>)}
                        </Select>
                      </Field>
                      <Field label="Value" hint={row.op === "in" || row.op === "not_in" ? "Comma separated." : row.op === "truthy" || row.op === "falsy" ? "Not used for this comparison." : undefined}>
                        <Input value={row.value} onChange={(e) => set(i, { value: e.target.value })} disabled={row.op === "truthy" || row.op === "falsy"} placeholder="manual" />
                      </Field>
                    </div>
                    {row.key ? <span className="fs12 t4">Key <code>{row.key}</code> — keep it to preserve this line's history.</span> : <span className="fs12 t4">A key is generated from the text when you save.</span>}
                  </Expander>
                </div>
              )))}
              {rows.filter((x) => x.tier === tier).length === 0 ? <p className="fs13 t4" style={{ margin: 0 }}>None recorded</p> : null}
            </div>
          ))}
        </div>

        <Field
          label="Buyer evidence"
          required={mustChanged}
          error={needsEvidence ? "Required: a must-have changed." : undefined}
          hint="Message id, call-note reference or document id where the buyer asked for this."
        >
          <Input value={sourceRef} onChange={(e) => setSourceRef(e.target.value)} placeholder="msg:AAMkAD… or call-note:2026-09-12" />
        </Field>
        <Field label="Note" hint="Optional context kept with the version.">
          <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Buyer dropped the colour preference" />
        </Field>
      </form>
    </ResponsiveDialog>
  );
}

/* ---------- agreement ---------- */
export function AgreementDialog({ open, onClose, r, onDone, mode, canUpload }: Base & { mode: "sent" | "signed"; canUpload: boolean }) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [agreementId, setAgreementId] = useState("");
  const [sourceRef, setSourceRef] = useState("");
  const [signedAt, setSignedAt] = useState("");

  useEffect(() => {
    if (!open) return;
    setAgreementId(r.agreement_id || "");
    setSourceRef("");
    setSignedAt("");
  }, [open, r.agreement_id]);

  if (!open) return null;
  const signing = mode === "signed";
  const blocked = signing && !sourceRef.trim();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (blocked) return;
    const res = await run("agreement", requestAction(r.id, "set-agreement"), {
      status: mode,
      agreement_id: agreementId.trim() || null,
      source_ref: sourceRef.trim() || null,
      signed_at: signedAt ? new Date(signedAt).toISOString() : null,
      expected_version: r.version,
    }, { success: signing ? "Agreement recorded as signed" : "Agreement recorded as sent" });
    if (res?.status === "ok") onDone();
  };

  return (
    <ResponsiveDialog
      mobile={mobile} open onClose={onClose} size="md"
      title={signing ? "Mark the agreement signed" : "Record the agreement as sent"}
      description={signing
        ? "The Active Search gate opens on evidence, not on a checkbox: attach the signed copy (or paste its reference)."
        : "Records that the search agreement went out. It does not open Active Search."}
      footer={
        <>
          <Button variant="primary" onClick={submit} loading={busy("agreement")} disabled={blocked} disabledReason="Attach the signed copy or paste its reference first.">
            {signing ? "Mark signed" : "Record as sent"}
          </Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <Field label="Agreement id or link" hint="Optional: the document this refers to.">
          <Input value={agreementId} onChange={(e) => setAgreementId(e.target.value)} placeholder="AGR-2026-0142" />
        </Field>
        {signing ? (
          <>
            <EvidenceUpload
              onUploaded={(ref) => setSourceRef(ref)}
              purpose="document"
              label="Upload the signed copy"
              disabled={!canUpload}
              disabledReason={canUpload ? undefined : "Your role can't upload files — paste the reference instead."}
            />
            <Field label="Evidence reference" required error={blocked ? "Required for a signed agreement." : undefined}
              hint="Filled in by the upload, or paste an existing asset / message id.">
              <Input value={sourceRef} onChange={(e) => setSourceRef(e.target.value)} placeholder="asset:… or msg:…" />
            </Field>
            <Field label="Signed on" hint="The buyer's signing time from the document. Leave empty when it isn't recorded — AZKT never fills in 'now'.">
              <Input type="datetime-local" value={signedAt} onChange={(e) => setSignedAt(e.target.value)} />
            </Field>
          </>
        ) : null}
      </form>
    </ResponsiveDialog>
  );
}

/* ---------- deposit rule ---------- */
export function DepositRuleDialog({ open, onClose, r, onDone }: Base) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [sourceRef, setSourceRef] = useState("");

  useEffect(() => {
    if (!open) return;
    setAmount(r.deposit_rule?.amount || "");
    setCurrency(r.deposit_rule?.currency || "USD");
    setSourceRef("");
  }, [open, r.deposit_rule]);

  if (!open) return null;
  const valid = Number(amount) > 0;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!valid) return;
    const res = await run("deposit-rule", requestAction(r.id, "set-deposit-rule"), {
      amount: amount.trim(), currency, source_ref: sourceRef.trim() || null, expected_version: r.version,
    }, { success: "Deposit rule recorded" });
    if (res?.status === "ok") onDone();
  };
  return (
    <ResponsiveDialog
      mobile={mobile} open onClose={onClose} size="sm"
      title="Set the required deposit"
      description="The owner sets the exact amount. Until it is set, the deposit gate stays blocked — AZKT never assumes an amount."
      footer={
        <>
          <Button variant="primary" onClick={submit} loading={busy("deposit-rule")} disabled={!valid} disabledReason="Enter a positive amount.">Save rule</Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <div className="form-grid">
          <Field label="Amount" required><Input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" placeholder="2000" /></Field>
          <Field label="Currency" required>
            <Select value={currency} onChange={(e) => setCurrency(e.target.value)}>
              {CURRENCIES.map((c) => <option key={c} value={c}>{c}</option>)}
            </Select>
          </Field>
        </div>
        <Field label="Where this comes from" hint="The agreement clause or terms reference.">
          <Input value={sourceRef} onChange={(e) => setSourceRef(e.target.value)} placeholder="AGR-2026-0142 §3" />
        </Field>
        {r.deposit_status === "confirmed" || r.deposit_status === "partial" ? (
          <Notice tone="risk" lead="Payments are re-checked">Recorded payment evidence is compared against the new rule. It is never re-confirmed by assumption.</Notice>
        ) : null}
      </form>
    </ResponsiveDialog>
  );
}

/* ---------- purchase ---------- */
export function RecordPurchaseDialog({ open, onClose, r, onDone, canUpload }: Base & { canUpload: boolean }) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [vehicleId, setVehicleId] = useState("");
  const [title, setTitle] = useState("");
  const [sourceRef, setSourceRef] = useState("");
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState("JPY");
  const [purchasedAt, setPurchasedAt] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => {
    if (!open) return;
    setVehicleId(""); setTitle(""); setSourceRef(""); setAmount(""); setCurrency("JPY"); setPurchasedAt(""); setNote("");
  }, [open]);

  if (!open) return null;
  const blocked = !sourceRef.trim() || (!vehicleId.trim() && !title.trim());
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (blocked) return;
    const res = await run("purchase", requestAction(r.id, "record-purchase"), {
      vehicle_id: vehicleId.trim() || null,
      vehicle: vehicleId.trim() ? null : { title: title.trim() },
      evidence: {
        source_ref: sourceRef.trim(),
        amount: amount.trim() || null,
        currency: amount.trim() ? currency : null,
        purchased_at: purchasedAt ? new Date(purchasedAt).toISOString() : null,
        note: note.trim() || null,
      },
      expected_version: r.version,
    }, { success: "Purchase recorded" });
    if (res?.status === "ok") onDone();
  };
  return (
    <ResponsiveDialog
      mobile={mobile} open onClose={onClose} size="md" align="top"
      title="Record the purchase"
      description="Links exactly one purchased vehicle to this request. Recording a purchase here never claims AZKT placed a bid."
      footer={
        <>
          <Button variant="primary" onClick={submit} loading={busy("purchase")} disabled={blocked}
            disabledReason={!sourceRef.trim() ? "Purchase evidence is required." : "Give the existing vehicle id or a title for the new one."}>
            Record purchase
          </Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <Field label="Existing vehicle id" hint="Leave empty to create the vehicle from the title below.">
          <Input value={vehicleId} onChange={(e) => setVehicleId(e.target.value)} placeholder="veh_…" />
        </Field>
        <Field label="New vehicle title" hint="Used only when no vehicle id is given.">
          <Input value={title} onChange={(e) => setTitle(e.target.value)} disabled={!!vehicleId.trim()} placeholder="1999 Subaru Sambar Dias 4WD" />
        </Field>
        <EvidenceUpload onUploaded={(ref) => setSourceRef(ref)} purpose="document" label="Upload the invoice or auction result"
          disabled={!canUpload} disabledReason={canUpload ? undefined : "Your role can't upload files — paste the reference instead."} />
        <Field label="Purchase evidence" required error={!sourceRef.trim() ? "Required." : undefined} hint="Invoice, auction result or payment reference.">
          <Input value={sourceRef} onChange={(e) => setSourceRef(e.target.value)} placeholder="asset:… or inv:…" />
        </Field>
        <div className="form-grid">
          <Field label="Price" hint="Optional. Hidden from roles without cost access."><Input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" placeholder="1180000" /></Field>
          <Field label="Currency">
            <Select value={currency} onChange={(e) => setCurrency(e.target.value)}>
              {CURRENCIES.map((c) => <option key={c} value={c}>{c}</option>)}
            </Select>
          </Field>
        </div>
        <Field label="Purchased on"><Input type="datetime-local" value={purchasedAt} onChange={(e) => setPurchasedAt(e.target.value)} /></Field>
        <Field label="Note"><Input value={note} onChange={(e) => setNote(e.target.value)} /></Field>
      </form>
    </ResponsiveDialog>
  );
}

/* ---------- next check ---------- */
export function NextCheckDialog({ open, onClose, r, onDone }: Base) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [at, setAt] = useState("");
  useEffect(() => { if (open) setAt(r.next_check_at ? new Date(r.next_check_at).toISOString().slice(0, 16) : ""); }, [open, r.next_check_at]);
  if (!open) return null;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const res = await run("next-check", requestAction(r.id, "update"), {
      next_check_at: at ? new Date(at).toISOString() : null, expected_version: r.version,
    }, { success: at ? "Next check saved" : "Next check cleared" });
    if (res?.status === "ok") onDone();
  };
  return (
    <ResponsiveDialog
      mobile={mobile} open onClose={onClose} size="sm"
      title="When should AZKT look again?"
      description="A durable check, not a reminder email. Leave it empty to stop checking."
      footer={
        <>
          <Button variant="primary" onClick={submit} loading={busy("next-check")}>Save</Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <Field label="Next check" hint="Your local time; stored in UTC.">
          <Input type="datetime-local" value={at} onChange={(e) => setAt(e.target.value)} />
        </Field>
      </form>
    </ResponsiveDialog>
  );
}

/* ---------- candidate: reject with a reason ---------- */
export function RejectCandidateDialog({ open, onClose, matchId, candidateTitle: label, onDone }: { open: boolean; onClose: () => void; matchId: string | null; candidateTitle: string; onDone: Done }) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [reason, setReason] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { if (open) setReason(""); }, [open]);
  if (!open || !matchId) return null;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!reason.trim()) return;
    const res = await run("reject", matchAction(matchId, "pass"), { reason: reason.trim() }, { success: "Candidate excluded for this buyer" });
    if (res?.status === "ok") onDone();
  };
  return (
    <ResponsiveDialog
      mobile={mobile} open onClose={onClose} size="sm"
      title="Reject this candidate"
      description={`${label} stops being offered for this request. The reason is kept so it is never presented again.`}
      initialFocusRef={ref}
      footer={
        <>
          <Button variant="danger" onClick={submit} loading={busy("reject")} disabled={!reason.trim()} disabledReason="Give the reason first.">Reject</Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <Field label="Reason" required hint="Shown in the exclusions list and in Activity.">
          <Textarea ref={ref} value={reason} onChange={(e) => setReason(e.target.value)} rows={3} placeholder="Buyer said no to the colour; over budget after fees" />
        </Field>
      </form>
    </ResponsiveDialog>
  );
}

/* ---------- candidate: record that the buyer message went out ---------- */
export function MarkSentDialog({ open, onClose, matchId, onDone }: { open: boolean; onClose: () => void; matchId: string | null; onDone: Done }) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [ref, setRef] = useState("");
  const [sentAt, setSentAt] = useState("");
  useEffect(() => { if (open) { setRef(""); setSentAt(""); } }, [open]);
  if (!open || !matchId) return null;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!ref.trim()) return;
    const res = await run("mark-sent", matchAction(matchId, "mark-sent"), {
      message_ref: ref.trim(), sent_at: sentAt ? new Date(sentAt).toISOString() : null,
    }, { success: "Recorded as sent" });
    if (res?.status === "ok") onDone();
  };
  return (
    <ResponsiveDialog
      mobile={mobile} open onClose={onClose} size="sm"
      title="Record the buyer message as sent"
      description="AZKT records what was actually sent through an authorized channel — it never claims a send it did not make."
      footer={
        <>
          <Button variant="primary" onClick={submit} loading={busy("mark-sent")} disabled={!ref.trim()} disabledReason="The message reference is the evidence.">Record</Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <Field label="Message reference" required hint="Gmail / Teams message id, or the thread reference.">
          <Input value={ref} onChange={(e) => setRef(e.target.value)} placeholder="msg:AAMkAD…" />
        </Field>
        <Field label="Sent at" hint="Leave empty to use the time it was recorded."><Input type="datetime-local" value={sentAt} onChange={(e) => setSentAt(e.target.value)} /></Field>
        <Chip size="sm" tone="soft">Interest from a buyer is never a bid authorization</Chip>
      </form>
    </ResponsiveDialog>
  );
}
