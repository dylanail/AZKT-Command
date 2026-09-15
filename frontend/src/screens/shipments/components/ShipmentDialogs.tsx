/* Every write on the shipment page, each carrying the evidence its command demands: a completed or
   estimated milestone needs a sourced time, a storage deadline needs real source evidence, a quote
   request shows exactly the data that will leave the business, and forwarding and booking are separate
   approvals. Nothing here fills in a value the records do not have. */
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useCommand } from "../../../lib/useCommand";
import { useIsMobile } from "../../../lib/viewport";
import { Button, Chip, Expander, Field, Input, Notice, ResponsiveDialog, Select, Switch, Textarea } from "../../../ui";
import { JsonDetail } from "../../shared/JsonDetail";
import { CURRENCIES, legAction, quoteAction, shipmentAction, startQuoteCase } from "../api";
import {
  LEG_KINDS, LEG_KIND_LABEL, MILESTONE_KINDS, MILESTONE_LABEL, MILESTONE_SOURCE_KINDS, MILESTONE_STATUSES,
  QUOTE_FIELDS, QUOTE_FIELD_LABEL, SHIPMENT_LABEL, SHIPMENT_STATUSES, sharedData,
  type Leg, type Quote, type Shipment, type VehicleMember,
} from "../types";

const isoOrNull = (local: string) => (local ? new Date(local).toISOString() : null);
const toLocal = (iso: string | null | undefined) => (iso ? new Date(iso).toISOString().slice(0, 16) : "");

interface ShipBase { open: boolean; onClose: () => void; s: Shipment; onDone: () => void }

/* ---------- shipment identifiers, ETA and status ---------- */
export function EditShipmentDialog({ open, onClose, s, onDone }: ShipBase) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [status, setStatus] = useState(s.status);
  const [containerNo, setContainerNo] = useState("");
  const [vessel, setVessel] = useState("");
  const [voyage, setVoyage] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [eta, setEta] = useState("");
  const [etaSource, setEtaSource] = useState("");
  const [notes, setNotes] = useState("");

  useEffect(() => {
    if (!open) return;
    setStatus(s.status);
    setContainerNo(s.container_no || "");
    setVessel(s.vessel || "");
    setVoyage(s.voyage || "");
    setFrom(s.route_from || "");
    setTo(s.route_to || "");
    setEta(toLocal(s.eta?.utc));
    setEtaSource(s.eta_source || "");
    setNotes(s.notes || "");
  }, [open, s]);

  if (!open) return null;
  const etaNeedsSource = !!eta && !etaSource.trim();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (etaNeedsSource) return;
    const res = await run("ship-update", shipmentAction(s.id, "update"), {
      status, container_no: containerNo.trim() || null, vessel: vessel.trim() || null, voyage: voyage.trim() || null,
      route_from: from.trim() || null, route_to: to.trim() || null,
      eta_at: isoOrNull(eta), eta_source: etaSource.trim() || null,
      notes, expected_version: s.version,
    }, { success: "Shipment updated" });
    if (res?.status === "ok") onDone();
  };
  return (
    <ResponsiveDialog
      mobile={mobile} open onClose={onClose} size="lg" align="top"
      title="Edit the shipment"
      description="Identifiers, route and status. An ETA is only useful with the notice it came from."
      footer={
        <>
          <Button variant="primary" onClick={submit} loading={busy("ship-update")} disabled={etaNeedsSource} disabledReason="An ETA needs the source it came from.">Save</Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <Field label="Status">
          <Select value={status} onChange={(e) => setStatus(e.target.value)}>
            {SHIPMENT_STATUSES.map((x) => <option key={x} value={x}>{SHIPMENT_LABEL[x]}</option>)}
          </Select>
        </Field>
        <div className="form-grid">
          <Field label="Container"><Input value={containerNo} onChange={(e) => setContainerNo(e.target.value)} placeholder="TCLU1234567" /></Field>
          <Field label="Vessel"><Input value={vessel} onChange={(e) => setVessel(e.target.value)} /></Field>
          <Field label="Voyage"><Input value={voyage} onChange={(e) => setVoyage(e.target.value)} /></Field>
        </div>
        <div className="form-grid">
          <Field label="From"><Input value={from} onChange={(e) => setFrom(e.target.value)} placeholder="Yokohama" /></Field>
          <Field label="To"><Input value={to} onChange={(e) => setTo(e.target.value)} placeholder="Long Beach" /></Field>
        </div>
        <div className="form-grid">
          <Field label="ETA"><Input type="datetime-local" value={eta} onChange={(e) => setEta(e.target.value)} /></Field>
          <Field label="ETA source" required={!!eta} error={etaNeedsSource ? "Required with an ETA." : undefined} hint="Carrier notice, port schedule, exporter message.">
            <Input value={etaSource} onChange={(e) => setEtaSource(e.target.value)} placeholder="carrier notice 2026-09-12" />
          </Field>
        </div>
        <Field label="Notes"><Textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={2} /></Field>
      </form>
    </ResponsiveDialog>
  );
}

/* ---------- milestone ---------- */
export function RecordMilestoneDialog({ open, onClose, s, vehicles, onDone }: ShipBase & { vehicles: VehicleMember[] }) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [kind, setKind] = useState<string>(MILESTONE_KINDS[0]);
  const [status, setStatus] = useState<string>("completed");
  const [at, setAt] = useState("");
  const [sourceKind, setSourceKind] = useState<string>("carrier");
  const [sourceRef, setSourceRef] = useState("");
  const [vehicleId, setVehicleId] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => {
    if (!open) return;
    setKind(MILESTONE_KINDS[0]); setStatus("completed"); setAt(""); setSourceKind("carrier"); setSourceRef(""); setVehicleId(""); setNote("");
  }, [open]);

  if (!open) return null;
  const needsTime = status === "estimated" || status === "completed";
  const needsRef = needsTime && sourceKind !== "manual";
  const blocked = (needsTime && !at) || (needsRef && !sourceRef.trim());
  const reason = needsTime && !at ? `A ${status} milestone needs its sourced time — leave it planned instead of inventing a date.`
    : needsRef && !sourceRef.trim() ? "A sourced milestone needs the notice, message or document reference." : undefined;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (blocked) return;
    const res = await run("milestone", shipmentAction(s.id, "record-milestone"), {
      kind, status, at: isoOrNull(at), source_kind: sourceKind, source_ref: sourceRef.trim() || null,
      vehicle_id: vehicleId || null, note: note.trim() || null, expected_version: s.version,
    }, { success: "Milestone recorded" });
    if (res?.status === "ok") onDone();
  };

  return (
    <ResponsiveDialog
      mobile={mobile} open onClose={onClose} size="md" align="top"
      title="Record a milestone"
      description="A container-wide notice applies to every vehicle on board. Recording it for one vehicle makes an exception that later container notices will not overwrite."
      footer={
        <>
          <Button variant="primary" onClick={submit} loading={busy("milestone")} disabled={blocked} disabledReason={reason}>Record</Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <div className="form-grid">
          <Field label="Milestone" required>
            <Select value={kind} onChange={(e) => setKind(e.target.value)}>
              {MILESTONE_KINDS.map((k) => <option key={k} value={k}>{MILESTONE_LABEL[k]}</option>)}
            </Select>
          </Field>
          <Field label="State" required hint="Planned needs no time. Estimated and completed do.">
            <Select value={status} onChange={(e) => setStatus(e.target.value)}>
              {MILESTONE_STATUSES.map((x) => <option key={x} value={x}>{x}</option>)}
            </Select>
          </Field>
        </div>
        <Field label="Time" required={needsTime} error={needsTime && !at ? "Required." : undefined} hint="Your local time; stored in UTC and shown in Japan and Arizona time.">
          <Input type="datetime-local" value={at} onChange={(e) => setAt(e.target.value)} disabled={status === "planned"} />
        </Field>
        <div className="form-grid">
          <Field label="Source" required>
            <Select value={sourceKind} onChange={(e) => setSourceKind(e.target.value)}>
              {MILESTONE_SOURCE_KINDS.map((x) => <option key={x} value={x}>{x}</option>)}
            </Select>
          </Field>
          <Field label="Source reference" required={needsRef} error={needsRef && !sourceRef.trim() ? "Required." : undefined} hint="Notice number, message id or document id.">
            <Input value={sourceRef} onChange={(e) => setSourceRef(e.target.value)} placeholder="notice:PORT-88213" />
          </Field>
        </div>
        <Field label="Applies to" hint="Whole container unless this is a per-vehicle exception.">
          <Select value={vehicleId} onChange={(e) => setVehicleId(e.target.value)}>
            <option value="">Whole container ({s.vehicle_ids?.length || 0} vehicles)</option>
            {vehicles.map((v) => <option key={v.id} value={v.id}>{v.title || v.stock_no || v.id}</option>)}
          </Select>
        </Field>
        <Field label="Note"><Input value={note} onChange={(e) => setNote(e.target.value)} /></Field>
      </form>
    </ResponsiveDialog>
  );
}

/* ---------- storage deadline ---------- */
export function StorageDeadlineDialog({ open, onClose, s, onDone }: ShipBase) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [at, setAt] = useState("");
  const [sourceKind, setSourceKind] = useState("port");
  const [sourceRef, setSourceRef] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => {
    if (!open) return;
    setAt(toLocal(s.storage_deadline?.utc));
    setSourceKind(s.storage_deadline_source && s.storage_deadline_source !== "manual" ? s.storage_deadline_source : "port");
    setSourceRef(s.storage_deadline_source_ref || "");
    setNote(s.storage_deadline_note || "");
  }, [open, s]);

  if (!open) return null;
  const blocked = !at || !sourceRef.trim() || sourceKind === "manual";
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (blocked) return;
    const res = await run("storage", shipmentAction(s.id, "set-storage-deadline"), {
      at: isoOrNull(at), source_kind: sourceKind, source_ref: sourceRef.trim(), note: note.trim() || null, expected_version: s.version,
    }, { success: "Storage deadline recorded" });
    if (res?.status === "ok") onDone();
  };
  return (
    <ResponsiveDialog
      mobile={mobile} open onClose={onClose} size="md"
      title="Record the storage deadline"
      description="Free time ends on a date somebody published. It always needs the notice it came from — a manual guess is refused."
      footer={
        <>
          <Button variant="primary" onClick={submit} loading={busy("storage")} disabled={blocked}
            disabledReason={!at ? "Give the deadline." : sourceKind === "manual" ? "A storage deadline needs real source evidence, not a manual entry." : "Give the notice reference."}>
            Record deadline
          </Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <Field label="Deadline" required><Input type="datetime-local" value={at} onChange={(e) => setAt(e.target.value)} /></Field>
        <Field label="Source" required hint="Manual is not accepted for a storage deadline.">
          <Select value={sourceKind} onChange={(e) => setSourceKind(e.target.value)}>
            {MILESTONE_SOURCE_KINDS.filter((x) => x !== "manual").map((x) => <option key={x} value={x}>{x}</option>)}
          </Select>
        </Field>
        <Field label="Source reference" required error={!sourceRef.trim() ? "Required." : undefined}>
          <Input value={sourceRef} onChange={(e) => setSourceRef(e.target.value)} placeholder="notice:LB-FREE-TIME-4412" />
        </Field>
        <Field label="Note"><Input value={note} onChange={(e) => setNote(e.target.value)} /></Field>
      </form>
    </ResponsiveDialog>
  );
}

/* ---------- legs ---------- */
export function AddLegDialog({ open, onClose, s, vehicles, onDone }: ShipBase & { vehicles: VehicleMember[] }) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [kind, setKind] = useState<string>("domestic");
  const [vehicleId, setVehicleId] = useState("");
  const [carrier, setCarrier] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [appointment, setAppointment] = useState("");
  const [conditions, setConditions] = useState("");

  useEffect(() => {
    if (!open) return;
    setKind("domestic"); setVehicleId(""); setCarrier(""); setFrom(s.route_from || ""); setTo(s.route_to || ""); setAppointment(""); setConditions("");
  }, [open, s]);

  if (!open) return null;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const res = await run("add-leg", shipmentAction(s.id, "add-leg"), {
      kind, vehicle_id: vehicleId || null, carrier_name: carrier.trim() || null,
      route_from: from.trim() || null, route_to: to.trim() || null,
      appointment_at: isoOrNull(appointment), conditions: conditions.trim(), status: "planned",
    }, { success: "Leg added" });
    if (res?.status === "ok") onDone();
  };
  return (
    <ResponsiveDialog
      mobile={mobile} open onClose={onClose} size="md" align="top"
      title="Add a leg"
      description="A new leg starts planned. It becomes booked only through an approved booking."
      footer={
        <>
          <Button variant="primary" onClick={submit} loading={busy("add-leg")}>Add leg</Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <div className="form-grid">
          <Field label="Leg" required>
            <Select value={kind} onChange={(e) => setKind(e.target.value)}>
              {LEG_KINDS.map((k) => <option key={k} value={k}>{LEG_KIND_LABEL[k]}</option>)}
            </Select>
          </Field>
          <Field label="Vehicle" hint="Leave on the whole container for ocean legs.">
            <Select value={vehicleId} onChange={(e) => setVehicleId(e.target.value)}>
              <option value="">Whole container</option>
              {vehicles.map((v) => <option key={v.id} value={v.id}>{v.title || v.stock_no || v.id}</option>)}
            </Select>
          </Field>
        </div>
        <Field label="Carrier"><Input value={carrier} onChange={(e) => setCarrier(e.target.value)} /></Field>
        <div className="form-grid">
          <Field label="From"><Input value={from} onChange={(e) => setFrom(e.target.value)} /></Field>
          <Field label="To"><Input value={to} onChange={(e) => setTo(e.target.value)} /></Field>
        </div>
        <Field label="Appointment"><Input type="datetime-local" value={appointment} onChange={(e) => setAppointment(e.target.value)} /></Field>
        <Field label="Conditions"><Textarea value={conditions} onChange={(e) => setConditions(e.target.value)} rows={2} /></Field>
      </form>
    </ResponsiveDialog>
  );
}

export function LegUpdateDialog({ open, onClose, leg, onDone }: { open: boolean; onClose: () => void; leg: Leg | null; onDone: () => void }) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [status, setStatus] = useState("planned");
  const [carrier, setCarrier] = useState("");
  const [driver, setDriver] = useState("");
  const [appointment, setAppointment] = useState("");
  const [pickup, setPickup] = useState("");
  const [delivered, setDelivered] = useState("");
  const [notes, setNotes] = useState("");

  useEffect(() => {
    if (!open || !leg) return;
    setStatus(leg.status === "booked" ? "in_progress" : leg.status);
    setCarrier(leg.carrier_name || "");
    setDriver(leg.driver_contact || "");
    setAppointment(toLocal(leg.appointment?.utc));
    setPickup(toLocal(leg.pickup_at));
    setDelivered(toLocal(leg.delivered_at));
    setNotes(leg.notes || "");
  }, [open, leg]);

  if (!open || !leg) return null;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const res = await run("leg-update", legAction(leg.id, "update"), {
      status, carrier_name: carrier.trim() || null, driver_contact: driver.trim() || null,
      appointment_at: isoOrNull(appointment), pickup_at: isoOrNull(pickup), delivered_at: isoOrNull(delivered),
      notes, expected_version: leg.version,
    }, { success: "Leg updated" });
    if (res?.status === "ok") onDone();
  };
  return (
    <ResponsiveDialog
      mobile={mobile} open onClose={onClose} size="md" align="top"
      title={`Update the ${LEG_KIND_LABEL[leg.kind] || leg.kind} leg`}
      description="Driver, appointment, pickup and delivery. Booked is not a state you can set here — it comes from an approved booking."
      footer={
        <>
          <Button variant="primary" onClick={submit} loading={busy("leg-update")}>Save</Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <Field label="Status">
          <Select value={status} onChange={(e) => setStatus(e.target.value)}>
            {["planned", "quoted", "in_progress", "complete"].map((x) => <option key={x} value={x}>{x.replace(/_/g, " ")}</option>)}
          </Select>
        </Field>
        <div className="form-grid">
          <Field label="Carrier"><Input value={carrier} onChange={(e) => setCarrier(e.target.value)} /></Field>
          <Field label="Driver"><Input value={driver} onChange={(e) => setDriver(e.target.value)} placeholder="Name and phone" /></Field>
        </div>
        <div className="form-grid">
          <Field label="Appointment"><Input type="datetime-local" value={appointment} onChange={(e) => setAppointment(e.target.value)} /></Field>
          <Field label="Picked up"><Input type="datetime-local" value={pickup} onChange={(e) => setPickup(e.target.value)} /></Field>
          <Field label="Delivered"><Input type="datetime-local" value={delivered} onChange={(e) => setDelivered(e.target.value)} /></Field>
        </div>
        <Field label="Notes"><Textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={2} /></Field>
      </form>
    </ResponsiveDialog>
  );
}

export function LegCancelDialog({ open, onClose, leg, onDone }: { open: boolean; onClose: () => void; leg: Leg | null; onDone: () => void }) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [reason, setReason] = useState("");
  useEffect(() => { if (open) setReason(""); }, [open]);
  if (!open || !leg) return null;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!reason.trim()) return;
    const res = await run("leg-cancel", legAction(leg.id, "remove"), { reason: reason.trim(), expected_version: leg.version }, { success: "Leg cancelled" });
    if (res?.status === "ok") onDone();
  };
  return (
    <ResponsiveDialog
      mobile={mobile} open onClose={onClose} size="sm"
      title="Cancel this leg"
      description="The leg is kept for history with the reason."
      footer={
        <>
          <Button variant="danger" onClick={submit} loading={busy("leg-cancel")} disabled={!reason.trim()} disabledReason="Give the reason first.">Cancel leg</Button>
          <Button variant="ghost" onClick={onClose}>Keep it</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <Field label="Reason" required><Textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2} /></Field>
      </form>
    </ResponsiveDialog>
  );
}

/* ---------- quote case ---------- */
export function StartQuoteDialog({ open, onClose, s, vehicles, onDone }: ShipBase & { vehicles: VehicleMember[] }) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [vehicleId, setVehicleId] = useState("");
  const [vendor, setVendor] = useState("");
  const [origin, setOrigin] = useState("");
  const [destination, setDestination] = useState("");
  const [service, setService] = useState("");
  const [earliest, setEarliest] = useState("");
  const [latest, setLatest] = useState("");
  const [timingSource, setTimingSource] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => {
    if (!open) return;
    setVehicleId(vehicles.length === 1 ? vehicles[0].id : "");
    setVendor(""); setOrigin(s.route_to || ""); setDestination(""); setService("");
    setEarliest(""); setLatest(""); setTimingSource(""); setNote("");
  }, [open, s, vehicles]);

  if (!open) return null;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const timing = earliest || latest ? { earliest: isoOrNull(earliest), latest: isoOrNull(latest), source: timingSource.trim() || null } : null;
    const res = await run("start-quote", startQuoteCase(), {
      shipment_id: s.id, vehicle_id: vehicleId || null, vendor_name: vendor.trim() || null,
      origin: origin.trim() || null, destination: destination.trim() || null,
      service: service || null, timing, note: note.trim() || null,
    }, { success: "Quote case opened" });
    if (res?.status === "ok") onDone();
  };
  return (
    <ResponsiveDialog
      mobile={mobile} open onClose={onClose} size="md" align="top"
      title="Start a quote case"
      description="AZKT builds the case from what is already recorded. Anything missing is listed as needed information rather than guessed."
      footer={
        <>
          <Button variant="primary" onClick={submit} loading={busy("start-quote")}>Start case</Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <Field label="Vehicle" hint="A quote for one vehicle is easier to compare later.">
          <Select value={vehicleId} onChange={(e) => setVehicleId(e.target.value)}>
            <option value="">Not tied to one vehicle</option>
            {vehicles.map((v) => <option key={v.id} value={v.id}>{v.title || v.stock_no || v.id}</option>)}
          </Select>
        </Field>
        <Field label="Vendor"><Input value={vendor} onChange={(e) => setVendor(e.target.value)} placeholder="Montway" /></Field>
        <div className="form-grid">
          <Field label="From"><Input value={origin} onChange={(e) => setOrigin(e.target.value)} placeholder="Long Beach, CA" /></Field>
          <Field label="To"><Input value={destination} onChange={(e) => setDestination(e.target.value)} placeholder="Mesa, AZ" /></Field>
        </div>
        <Field label="Service" hint="Left empty, it is recorded as needed information — not assumed.">
          <Select value={service} onChange={(e) => setService(e.target.value)}>
            <option value="">Not specified</option>
            <option value="open">Open</option>
            <option value="enclosed">Enclosed</option>
          </Select>
        </Field>
        <div className="form-grid">
          <Field label="Earliest"><Input type="datetime-local" value={earliest} onChange={(e) => setEarliest(e.target.value)} /></Field>
          <Field label="Latest"><Input type="datetime-local" value={latest} onChange={(e) => setLatest(e.target.value)} /></Field>
          <Field label="Timing came from"><Input value={timingSource} onChange={(e) => setTimingSource(e.target.value)} placeholder="buyer message" /></Field>
        </div>
        <Field label="Note"><Input value={note} onChange={(e) => setNote(e.target.value)} /></Field>
      </form>
    </ResponsiveDialog>
  );
}

interface QuoteBase { open: boolean; onClose: () => void; q: Quote | null; onDone: () => void }

export function RequestQuoteDialog({ open, onClose, q, onDone }: QuoteBase) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [recipients, setRecipients] = useState("");
  const [channel, setChannel] = useState("email");
  const [fields, setFields] = useState<string[]>([...QUOTE_FIELDS]);
  const [message, setMessage] = useState("");
  const [binding, setBinding] = useState("nonbinding");

  useEffect(() => {
    if (!open || !q) return;
    setRecipients((q.recipients || []).join(", "));
    setChannel(q.channel || "email");
    // only offer fields the case actually has recorded; an unrecorded field cannot be shared
    setFields(QUOTE_FIELDS.filter((f) => Object.prototype.hasOwnProperty.call(q.request_payload || {}, f)));
    setMessage("");
    setBinding(q.binding || "nonbinding");
  }, [open, q]);

  const shared = useMemo(() => (q ? sharedData(q, fields) : {}), [q, fields]);
  if (!open || !q) return null;

  const list = recipients.split(",").map((x) => x.trim()).filter(Boolean);
  const blocked = list.length === 0 || fields.length === 0;
  const toggle = (f: string) => setFields((xs) => (xs.includes(f) ? xs.filter((x) => x !== f) : [...xs, f]));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (blocked) return;
    const res = await run(`quote-request:${q.id}`, quoteAction(q.id, "request"), {
      recipients: list, channel, fields, data: shared, message: message.trim() || null, binding,
    }, { success: "Quote requested" });
    if (res) onDone();
  };

  return (
    <ResponsiveDialog
      mobile={mobile} open onClose={onClose} size="lg" align="top"
      title="Request a quote"
      description="This is exactly what leaves the business. A new recipient or an extra field is a new approval."
      footer={
        <>
          <Button variant="primary" onClick={submit} loading={busy(`quote-request:${q.id}`)} disabled={blocked}
            disabledReason={list.length === 0 ? "Give at least one recipient." : "Choose at least one field to share."}>
            Send for approval
          </Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <Field label="Recipients" required hint="Comma separated. Recipients are bound to the approval.">
          <Input value={recipients} onChange={(e) => setRecipients(e.target.value)} placeholder="quotes@vendor.com" />
        </Field>
        <div className="form-grid">
          <Field label="Channel">
            <Select value={channel} onChange={(e) => setChannel(e.target.value)}>
              <option value="email">Email</option>
              <option value="web_form">Web form</option>
              <option value="manual">Manual (a task with the exact content)</option>
            </Select>
          </Field>
          <Field label="Binding" hint="A nonbinding quote is a price indication, not a commitment.">
            <Select value={binding} onChange={(e) => setBinding(e.target.value)}>
              <option value="nonbinding">Nonbinding</option>
              <option value="binding">Binding</option>
            </Select>
          </Field>
        </div>

        <Field label="Data shared" required hint="Only recorded facts are offered. A fact that is not recorded cannot be shared.">
          <div className="qc-fieldlist">
            {QUOTE_FIELDS.map((f) => {
              const has = Object.prototype.hasOwnProperty.call(q.request_payload || {}, f);
              const on = fields.includes(f);
              return (
                <button key={f} type="button" role="checkbox" aria-checked={on} className="qc-field" onClick={() => toggle(f)} disabled={!has} title={has ? undefined : "Not recorded on this case yet."}>
                  <span className="qc-field__mark" aria-hidden="true" />
                  <span>
                    {QUOTE_FIELD_LABEL[f] || f}
                    {!has ? <span className="t4"> · not recorded</span> : null}
                  </span>
                </button>
              );
            })}
          </div>
        </Field>

        <div className="qc-share">
          <strong>Exactly this will be sent:</strong>
          <JsonDetail value={shared} emptyText="Nothing selected — there would be nothing to send." />
          <span className="fs12 t4">AZKT re-checks these facts immediately before sending. If any of them changed, the request stops and comes back for review.</span>
        </div>

        <Field label="Message" hint="Optional covering note. It becomes part of the approval.">
          <Textarea value={message} onChange={(e) => setMessage(e.target.value)} rows={3} placeholder="Please quote open transport for the vehicle below." />
        </Field>
        {channel === "manual" ? <Notice tone="neutral" lead="Manual route">A task is created with the exact content. AZKT never claims a send it did not make.</Notice> : null}
      </form>
    </ResponsiveDialog>
  );
}

export function RecordReplyDialog({ open, onClose, q, onDone }: QuoteBase) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [messageId, setMessageId] = useState("");
  const [fromAddr, setFromAddr] = useState("");
  const [body, setBody] = useState("");
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [scope, setScope] = useState("");
  const [inclusions, setInclusions] = useState("");
  const [exclusions, setExclusions] = useState("");
  const [timing, setTiming] = useState("");
  const [expires, setExpires] = useState("");

  useEffect(() => {
    if (!open || !q) return;
    setMessageId(""); setFromAddr((q.recipients || [])[0] || ""); setBody("");
    setAmount(""); setCurrency(q.currency || "USD"); setScope(""); setInclusions(""); setExclusions(""); setTiming(""); setExpires("");
  }, [open, q]);

  if (!open || !q) return null;
  const blocked = !messageId.trim();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (blocked) return;
    const res = await run(`quote-reply:${q.id}`, quoteAction(q.id, "record-reply"), {
      message_id: messageId.trim(),
      from_addr: fromAddr.trim() || null,
      body: body.trim() || null,
      extracted: {
        amount: amount.trim() || null,
        currency: amount.trim() ? currency : null,
        scope: scope.trim() || null,
        inclusions: inclusions.split("\n").map((x) => x.trim()).filter(Boolean),
        exclusions: exclusions.split("\n").map((x) => x.trim()).filter(Boolean),
        timing: timing.trim() || null,
        expires_at: isoOrNull(expires),
      },
      expected_version: q.version,
    }, { success: "Reply recorded" });
    if (res?.status === "ok") onDone();
  };
  return (
    <ResponsiveDialog
      mobile={mobile} open onClose={onClose} size="lg" align="top"
      title="Record the vendor's reply"
      description="Each part is recorded separately so the quote can be compared honestly later. Leave anything the vendor did not say empty."
      footer={
        <>
          <Button variant="primary" onClick={submit} loading={busy(`quote-reply:${q.id}`)} disabled={blocked} disabledReason="The message id is the evidence for this reply.">Record reply</Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <div className="form-grid">
          <Field label="Message id" required error={blocked ? "Required." : undefined}><Input value={messageId} onChange={(e) => setMessageId(e.target.value)} placeholder="msg:AAMkAD…" /></Field>
          <Field label="From"><Input value={fromAddr} onChange={(e) => setFromAddr(e.target.value)} placeholder="quotes@vendor.com" /></Field>
        </div>
        <div className="form-grid">
          <Field label="Price"><Input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" placeholder="1150" /></Field>
          <Field label="Currency">
            <Select value={currency} onChange={(e) => setCurrency(e.target.value)}>
              {CURRENCIES.map((c) => <option key={c} value={c}>{c}</option>)}
            </Select>
          </Field>
          <Field label="Quote expires"><Input type="datetime-local" value={expires} onChange={(e) => setExpires(e.target.value)} /></Field>
        </div>
        <Field label="Scope"><Input value={scope} onChange={(e) => setScope(e.target.value)} placeholder="Port to door, open carrier" /></Field>
        <div className="form-grid">
          <Field label="Includes" hint="One per line."><Textarea value={inclusions} onChange={(e) => setInclusions(e.target.value)} rows={3} /></Field>
          <Field label="Excludes" hint="One per line."><Textarea value={exclusions} onChange={(e) => setExclusions(e.target.value)} rows={3} /></Field>
        </div>
        <Field label="Timing"><Input value={timing} onChange={(e) => setTiming(e.target.value)} placeholder="Pickup within 3–5 days" /></Field>
        <Field label="Reply text" hint="Kept with the case so the extraction can be checked."><Textarea value={body} onChange={(e) => setBody(e.target.value)} rows={4} /></Field>
        <Notice tone="neutral" lead="Several vehicles in one reply?">Record it as it is — the case moves to clarifying and raises a task instead of guessing which vehicle the price covers.</Notice>
      </form>
    </ResponsiveDialog>
  );
}

export function ForwardQuoteDialog({ open, onClose, q, onDone }: QuoteBase) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [to, setTo] = useState("");
  const [body, setBody] = useState("");
  const [includeComparison, setIncludeComparison] = useState(false);

  useEffect(() => {
    if (!open || !q) return;
    setTo("");
    setBody("");
    setIncludeComparison(false);
  }, [open, q]);

  if (!open || !q) return null;
  const list = to.split(",").map((x) => x.trim()).filter(Boolean);
  const blocked = list.length === 0 || !body.trim();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (blocked) return;
    const res = await run(`quote-forward:${q.id}`, quoteAction(q.id, "forward"), {
      to: list, body: body.trim(), include_comparison: includeComparison,
    }, { success: "Forward prepared" });
    if (res) onDone();
  };
  return (
    <ResponsiveDialog
      mobile={mobile} open onClose={onClose} size="lg" align="top"
      title="Forward the quote to the customer"
      description="Its own approval. Forwarding a quote never books anything — booking is a separate decision with its own authority."
      footer={
        <>
          <Button variant="primary" onClick={submit} loading={busy(`quote-forward:${q.id}`)} disabled={blocked}
            disabledReason={list.length === 0 ? "Give at least one customer address." : "Write the message the customer will read."}>
            Send for approval
          </Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <Field label="To" required hint="Comma separated customer addresses.">
          <Input value={to} onChange={(e) => setTo(e.target.value)} placeholder="buyer@example.com" />
        </Field>
        <Field label="Message" required hint="Exactly this text is reviewed; editing it later creates a new approval version.">
          <Textarea value={body} onChange={(e) => setBody(e.target.value)} rows={6} placeholder="Here is the transport quote for your truck…" />
        </Field>
        <Switch checked={includeComparison} onChange={setIncludeComparison} label="Include the comparison" meta="how this price sits against comparable quotes" />
        {q.comparison && Object.keys(q.comparison).length && (q.comparison as { evidence?: string }).evidence === "weak" ? (
          <Notice tone="risk" lead="The comparison is weak">Say so plainly if you include it: fewer than two genuinely comparable quotes, or unknown attributes on this one.</Notice>
        ) : null}
      </form>
    </ResponsiveDialog>
  );
}

export function BookQuoteDialog({ open, onClose, q, vehicles, onDone }: QuoteBase & { vehicles: VehicleMember[] }) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [carrier, setCarrier] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [vehicleId, setVehicleId] = useState("");
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [conditions, setConditions] = useState("");
  const [acceptanceRef, setAcceptanceRef] = useState("");
  const [legKind, setLegKind] = useState("domestic");

  useEffect(() => {
    if (!open || !q) return;
    setCarrier(q.vendor_name || "");
    setFrom(q.route_from || "");
    setTo(q.route_to || "");
    setVehicleId(q.vehicle_id || (vehicles.length === 1 ? vehicles[0].id : ""));
    setAmount(q.amount || "");
    setCurrency(q.currency || "USD");
    setConditions(q.scope || "");
    setAcceptanceRef("");
    setLegKind("domestic");
  }, [open, q, vehicles]);

  if (!open || !q) return null;
  const blocked = !carrier.trim() || !from.trim() || !to.trim() || !vehicleId || !amount.trim() || !conditions.trim();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (blocked) return;
    const res = await run(`quote-book:${q.id}`, quoteAction(q.id, "book"), {
      carrier_name: carrier.trim(), route_from: from.trim(), route_to: to.trim(), vehicle_id: vehicleId,
      amount: amount.trim(), currency, conditions: conditions.trim(),
      customer_acceptance_ref: acceptanceRef.trim() || null, leg_kind: legKind,
    }, { success: "Booking sent for approval" });
    if (res) onDone();
  };
  return (
    <ResponsiveDialog
      mobile={mobile} open onClose={onClose} size="lg" align="top"
      title="Book transport"
      description="Its own exact approval. The carrier, route, vehicle, amount and conditions below are what is authorized — nothing else."
      footer={
        <>
          <Button variant="primary" onClick={submit} loading={busy(`quote-book:${q.id}`)} disabled={blocked}
            disabledReason="Carrier, route, vehicle, amount and conditions are all part of what gets approved.">
            Send booking for approval
          </Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <Field label="Carrier" required><Input value={carrier} onChange={(e) => setCarrier(e.target.value)} /></Field>
        <div className="form-grid">
          <Field label="From" required><Input value={from} onChange={(e) => setFrom(e.target.value)} /></Field>
          <Field label="To" required><Input value={to} onChange={(e) => setTo(e.target.value)} /></Field>
        </div>
        <Field label="Vehicle" required>
          <Select value={vehicleId} onChange={(e) => setVehicleId(e.target.value)}>
            <option value="">Choose the vehicle</option>
            {vehicles.map((v) => <option key={v.id} value={v.id}>{v.title || v.stock_no || v.id}</option>)}
          </Select>
        </Field>
        <div className="form-grid">
          <Field label="Amount" required><Input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" /></Field>
          <Field label="Currency" required>
            <Select value={currency} onChange={(e) => setCurrency(e.target.value)}>
              {CURRENCIES.map((c) => <option key={c} value={c}>{c}</option>)}
            </Select>
          </Field>
          <Field label="Leg">
            <Select value={legKind} onChange={(e) => setLegKind(e.target.value)}>
              {LEG_KINDS.map((k) => <option key={k} value={k}>{LEG_KIND_LABEL[k]}</option>)}
            </Select>
          </Field>
        </div>
        <Field label="Conditions" required hint="What the carrier is agreeing to. Mismatches against the quote stop the booking."><Textarea value={conditions} onChange={(e) => setConditions(e.target.value)} rows={3} /></Field>
        <Field label="Customer acceptance" hint="Only needed when the quote was not forwarded first: the message where the customer accepted.">
          <Input value={acceptanceRef} onChange={(e) => setAcceptanceRef(e.target.value)} placeholder="msg:…" />
        </Field>
        <Expander title="What the vendor quoted"><JsonDetail value={{ amount: q.amount, currency: q.currency, scope: q.scope, inclusions: q.inclusions, exclusions: q.exclusions, expires_at: q.expires_at }} /></Expander>
        <Chip size="sm" tone="soft">A forward approval never authorizes a booking</Chip>
      </form>
    </ResponsiveDialog>
  );
}

export function DeclineQuoteDialog({ open, onClose, q, onDone }: QuoteBase) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [reason, setReason] = useState("");
  useEffect(() => { if (open) setReason(""); }, [open]);
  if (!open || !q) return null;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!reason.trim()) return;
    const res = await run(`quote-decline:${q.id}`, quoteAction(q.id, "decline"), { reason: reason.trim(), expected_version: q.version }, { success: "Quote declined" });
    if (res?.status === "ok") onDone();
  };
  return (
    <ResponsiveDialog
      mobile={mobile} open onClose={onClose} size="sm"
      title="Decline this quote"
      description="The case closes with your reason. A priced quote is still kept for future comparisons."
      footer={
        <>
          <Button variant="danger" onClick={submit} loading={busy(`quote-decline:${q.id}`)} disabled={!reason.trim()} disabledReason="Give the reason first.">Decline</Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <Field label="Reason" required><Textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2} placeholder="Too expensive for this route; going with the other carrier" /></Field>
      </form>
    </ResponsiveDialog>
  );
}
