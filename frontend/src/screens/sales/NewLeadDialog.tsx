/* New lead (dialog on desktop, sheet on phones): name, phone or email, pipeline, enquiry or vehicle, budget.
   POST /api/sales/opportunities with an inline contact; repeated ingestion returns the existing open lead. */
import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, command, describeError } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { useIsMobile } from "../../lib/viewport";
import { Button, Field, Input, Notice, ResponsiveDialog, SegmentedControl, Select, useToast } from "../../ui";
import { fetchVehicleOptions, vehicleLabel, type VehicleOption } from "../tasks/useNames";
import { PIPELINES, type Opportunity, type Pipeline } from "./types";

export interface NewLeadResult { opportunity: Opportunity; created: boolean; matched_by?: string; contact_created?: boolean; }

export function NewLeadDialog({ open, onClose, onCreated, defaultPipeline = "vehicle" }: { open: boolean; onClose: () => void; onCreated: (r: NewLeadResult) => void; defaultPipeline?: Pipeline }) {
  const mobile = useIsMobile();
  const { toast } = useToast();
  const { user } = useAuth();
  const [name, setName] = useState("");
  const [contact, setContact] = useState("");
  const [pipeline, setPipeline] = useState<Pipeline>(defaultPipeline);
  const [enquiry, setEnquiry] = useState("");
  const [vehicleId, setVehicleId] = useState("");
  const [budget, setBudget] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [vehicles, setVehicles] = useState<VehicleOption[] | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setName(""); setContact(""); setPipeline(defaultPipeline); setEnquiry(""); setVehicleId(""); setBudget(""); setCurrency("USD"); setErr(null);
    if (vehicles === undefined) fetchVehicleOptions().then(setVehicles).catch(() => setVehicles(null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const contactKind = contact.includes("@") ? "email" : "phone";
  const budgetNum = budget.trim() ? Number(budget.replace(/[^0-9.]/g, "")) : null;
  const valid = name.trim().length > 0 && (pipeline === "irq" || vehicleId.trim().length > 0) && (budgetNum === null || Number.isFinite(budgetNum));
  const whyNotValid = !name.trim() ? "Add a name first." : pipeline === "vehicle" && !vehicleId.trim() ? "Pick the vehicle they asked about." : budgetNum !== null && !Number.isFinite(budgetNum) ? "Budget must be a number." : undefined;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!valid) return;
    setBusy(true);
    setErr(null);
    const body: Record<string, unknown> = {
      pipeline,
      contact: { name: name.trim(), roles: ["buyer"], identities: contact.trim() ? [{ kind: contactKind, value: contact.trim(), is_primary: true }] : [], source: "manual" },
      enquiry: pipeline === "irq" ? enquiry.trim() : "",
      source: "manual",
      owner_user_id: user?.id || undefined,
      ...(pipeline === "vehicle" ? { vehicle_id: vehicleId.trim() } : {}),
      ...(budgetNum !== null ? { budget_amount: budgetNum, budget_currency: currency } : {}),
    };
    try {
      const res = await command<NewLeadResult>("/api/sales/opportunities", body);
      if (res.status === "needs_review") { toast({ title: "Sent for approval", message: "The owner reviews new leads from your role.", tone: "wait" }); onClose(); return; }
      if (res.status === "blocked") { setErr(res.decision?.reasons?.join("; ") || "Blocked"); return; }
      if (!res.data?.opportunity) { setErr("Saved, but no lead came back. Refresh the board."); return; }
      if (res.data.created === false) toast({ message: `${name.trim()} already has an open lead here — opened it instead.`, tone: "wait" });
      else toast({ message: `${name.trim()} added to New Lead`, tone: "ok" });
      onCreated(res.data);
      onClose();
    } catch (ex) {
      if (ex instanceof ApiError && ex.code === "blocked") {
        const hint = ex.detail.hint as { reason?: string } | undefined;
        setErr(hint?.reason === "vehicle_sold" ? `${ex.message} Switch to IRQ or pick another vehicle.` : ex.message);
      } else setErr(describeError(ex));
    } finally {
      setBusy(false);
    }
  };

  return (
    <ResponsiveDialog mobile={mobile} open={open} onClose={onClose} title="New lead" initialFocusRef={nameRef}
      footer={<><Button type="submit" form="sb-newlead" variant="primary" loading={busy} disabled={!valid} disabledReason={whyNotValid}>Add to New Lead</Button><Button variant="ghost" onClick={onClose}>Cancel</Button><span className="fs12 t4">Opens the lead so you can add the first task.</span></>}>
      <form id="sb-newlead" className="stack" onSubmit={submit}>
        {err ? <Notice tone="blocked" lead="Not saved" role="alert">{err}</Notice> : null}
        <Field label="Name" required>
          <Input ref={nameRef} value={name} onChange={(e) => setName(e.target.value)} placeholder="Full name" maxLength={200} autoComplete="off" />
        </Field>
        <Field label="Phone or email" hint={contact.trim() ? (contactKind === "email" ? "Saved as an email address." : "Saved as a phone number (US unless it starts with +).") : "Optional, but needed to match replies."}>
          <Input value={contact} onChange={(e) => setContact(e.target.value)} placeholder="+1 … or name@…" inputMode="email" autoComplete="off" />
        </Field>
        <div className="field">
          <span className="field__label"><span>Pipeline</span></span>
          <SegmentedControl<Pipeline> label="Pipeline" block value={pipeline} onChange={setPipeline} options={PIPELINES.map((p) => ({ value: p.id, label: p.label }))} />
        </div>
        {pipeline === "irq" ? (
          <Field label="What they're looking for">
            <Input value={enquiry} onChange={(e) => setEnquiry(e.target.value)} placeholder="e.g. 4WD Carry, under 9,000 USD, AZ delivery" />
          </Field>
        ) : (
          <Field label="Vehicle" required hint={vehicles === null ? "Vehicle list isn't available yet — paste the vehicle id or stock number." : vehicles && !vehicles.length ? "No vehicles in stock yet — paste a vehicle id." : "Sold vehicles can't take a new lead; use IRQ instead."}>
            {vehicles && vehicles.length ? (
              <Select value={vehicleId} onChange={(e) => setVehicleId(e.target.value)}>
                <option value="">Choose a vehicle…</option>
                {vehicles.map((v) => <option key={v.id} value={v.id}>{vehicleLabel(v)}</option>)}
              </Select>
            ) : <Input value={vehicleId} onChange={(e) => setVehicleId(e.target.value)} placeholder="Vehicle id" />}
          </Field>
        )}
        <div className="tk-grid2">
          <Field label="Budget" hint="Optional.">
            <Input value={budget} onChange={(e) => setBudget(e.target.value)} placeholder="e.g. 9000" inputMode="decimal" />
          </Field>
          <Field label="Currency">
            <Select value={currency} onChange={(e) => setCurrency(e.target.value)}>
              <option value="USD">USD</option>
              <option value="JPY">JPY</option>
              <option value="CAD">CAD</option>
            </Select>
          </Field>
        </div>
      </form>
    </ResponsiveDialog>
  );
}

export default NewLeadDialog;
