/* Parts keep physical state separate from payment state (spec §8.4, acceptance G10):
   Requested → Approved/Ordered → Arrived → Installed → Verified, with cancelled / returned branches.
   "Paid" is shown as its own label and never implies arrival or installation.
   Installation needs evidence, so the Installed action uploads a photo first. */
import { useEffect, useState, type ChangeEvent } from "react";
import { Button, Chip, Field, Notice, ResponsiveDialog, Textarea, When, useToast } from "../../../ui";
import { describeError } from "../../../lib/api";
import { useIsMobile } from "../../../lib/viewport";
import { PART_LABELS, type PartData } from "../types";
import { UploadsUnavailable, uploadOne } from "../uploads";
import { partPath, useVehicleCommand } from "./useVehicle";

/** Requested → Approved/Ordered → Arrived → Installed → Verified, with the current step named in text. */
const CHAIN_STEPS: { key: string; label: string; members: string[] }[] = [
  { key: "requested", label: "Requested", members: ["requested"] },
  { key: "approved", label: "Approved/Ordered", members: ["approved", "ordered"] },
  { key: "arrived", label: "Arrived", members: ["arrived"] },
  { key: "installed", label: "Installed", members: ["installed"] },
  { key: "verified", label: "Verified", members: ["verified"] },
];

export function PartChainBar({ state }: { state: string }) {
  const off = state === "cancelled" || state === "returned";
  const at = CHAIN_STEPS.findIndex((s) => s.members.includes(state));
  return (
    <span className="vh-chain" role="img" aria-label={`Part state: ${PART_LABELS[state] || state}`}>
      {CHAIN_STEPS.map((s, i) => {
        const cls = off ? "vh-chain__step--off" : at >= 0 && i < at ? "vh-chain__step--done" : i === at ? "vh-chain__step--now" : "";
        return (
          <span key={s.key} className={["vh-chain__step", cls].filter(Boolean).join(" ")}>
            {i > 0 ? <span className="vh-chain__sep" aria-hidden="true">›</span> : null}
            {s.label}
          </span>
        );
      })}
      {off ? <span className="vh-chain__step vh-chain__step--off"><span className="vh-chain__sep" aria-hidden="true">·</span>{PART_LABELS[state]}</span> : null}
    </span>
  );
}

export interface PartsChainProps {
  part: PartData;
  vehicleId: string;
  can: (perm: string) => boolean;
  onChanged: () => void;
}

export function PartsChain({ part: p, vehicleId, can, onChanged }: PartsChainProps) {
  const cmd = useVehicleCommand();
  const [installOpen, setInstallOpen] = useState(false);
  const closed = p.state === "cancelled" || p.state === "returned";

  const act = async (action: string, body: Record<string, unknown>, okMessage: string) => {
    const out = await cmd.run(`part:${p.id}:${action}`, partPath(p.id, action), { vehicle_id: vehicleId, expected_version: p.version, ...body }, { okMessage });
    if (out.ok) onChanged();
  };

  const canRequest = can("parts.request");
  const canOrder = can("parts.order");
  const canWork = can("tasks.write");
  const canVerify = can("tasks.verify");

  const meta = [
    p.part_no ? `Part no. ${p.part_no}` : null,
    p.quantity && p.quantity > 1 ? `×${p.quantity}` : null,
    p.order_ref ? `Order ${p.order_ref}` : null,
  ].filter(Boolean).join(" · ");

  return (
    <div className="vh-item">
      <div className="vh-item__main">
        <div className="vh-item__title">{p.name}</div>
        <PartChainBar state={p.state} />
        <div className="vh-item__meta">
          {meta ? <span>{meta}</span> : <span className="not-recorded">No part number recorded</span>}
          {p.arrived_at ? <> · arrived <When iso={p.arrived_at} format="date" /></> : null}
          {p.installed_at ? <> · installed <When iso={p.installed_at} format="date" /></> : null}
          {p.verified_at ? <> · verified <When iso={p.verified_at} format="date" /></> : null}
        </div>
        <div className="row-wrap" style={{ gap: 6 }}>
          <Chip size="sm" tone={p.state === "verified" ? "ok" : closed ? "risk" : "soft"}>{p.physical_label || PART_LABELS[p.state] || p.state}</Chip>
          <Chip size="sm" tone={p.payment_state === "paid" ? "wait" : "soft"}>
            {p.payment_state === "paid" ? "Paid" : "Payment not recorded"}
          </Chip>
          {p.payment_state === "paid" && p.state !== "installed" && p.state !== "verified" ? (
            <span className="fs12 t3">Paid is not proof of arrival or installation.</span>
          ) : null}
        </div>
      </div>
      <div className="vh-item__acts">
        {closed ? <span className="fs13 t3">{PART_LABELS[p.state]}</span> : (
          <>
            {p.state === "requested" ? (
              <Button
                size="xs" variant="soft" loading={cmd.busy(`part:${p.id}:order`)}
                disabled={!canOrder} disabledReason="Ordering parts needs the owner."
                onClick={() => void act("order", { part_name: p.name }, `Order recorded for ${p.name}`)}
              >Record order</Button>
            ) : null}
            {["requested", "approved", "ordered"].includes(p.state) ? (
              <Button
                size="xs" variant="primary" loading={cmd.busy(`part:${p.id}:arrived`)}
                disabled={!canRequest} disabledReason="Your role can't update parts."
                onClick={() => void act("arrived", {}, `${p.name} marked arrived`)}
              >Mark arrived</Button>
            ) : null}
            {p.state === "arrived" ? (
              <Button
                size="xs" variant="primary"
                disabled={!canWork} disabledReason="Your role can't record installation."
                onClick={() => setInstallOpen(true)}
              >Mark installed</Button>
            ) : null}
            {p.state === "installed" ? (
              <Button
                size="xs" variant="primary" loading={cmd.busy(`part:${p.id}:verify`)}
                disabled={!canVerify} disabledReason="Only the owner verifies finished work."
                onClick={() => void act("verify", {}, `${p.name} verified`)}
              >Verify</Button>
            ) : null}
            {p.state === "verified" ? <span className="fs13" style={{ color: "var(--ok)" }}>Verified</span> : null}
          </>
        )}
      </div>

      <InstallDialog
        open={installOpen}
        onClose={() => setInstallOpen(false)}
        partName={p.name}
        onConfirm={async (assetIds, note) => {
          const out = await cmd.run(`part:${p.id}:installed`, partPath(p.id, "installed"),
            { vehicle_id: vehicleId, expected_version: p.version, asset_ids: assetIds, note: note || null },
            { okMessage: `${p.name} marked installed` });
          if (out.ok) { onChanged(); return true; }
          return false;
        }}
      />
    </div>
  );
}

/** Installation needs evidence: the photo is uploaded first, then the command carries its asset id. */
function InstallDialog({ open, onClose, partName, onConfirm }: {
  open: boolean; onClose: () => void; partName: string;
  onConfirm: (assetIds: string[], note: string) => Promise<boolean>;
}) {
  const mobile = useIsMobile();
  const { toast } = useToast();
  const [files, setFiles] = useState<Array<{ key: string; name: string; file: File; assetId?: string; progress: number; status: string; error?: string }>>([]);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [uploadsOff, setUploadsOff] = useState(false);

  useEffect(() => { if (open) { setFiles([]); setNote(""); setUploadsOff(false); } }, [open]);

  const add = (e: ChangeEvent<HTMLInputElement>) => {
    const list = Array.from(e.target.files || []);
    e.target.value = "";
    setFiles((xs) => [...xs, ...list.map((f, i) => ({ key: `${Date.now()}-${i}`, name: f.name, file: f, progress: 0, status: "pending" }))]);
  };

  const submit = async () => {
    setBusy(true);
    let current = files;
    for (let i = 0; i < current.length; i++) {
      const f = current[i];
      if (f.assetId) continue;
      current = current.map((x, j) => (j === i ? { ...x, status: "uploading", progress: 0, error: undefined } : x));
      setFiles(current);
      try {
        const assetId = await uploadOne(f.file, {
          purpose: "evidence",
          onProgress: (pct) => setFiles((xs) => xs.map((x, j) => (j === i ? { ...x, progress: pct } : x))),
        });
        current = current.map((x, j) => (j === i ? { ...x, assetId, status: "done", progress: 100 } : x));
        setFiles(current);
      } catch (e) {
        if (e instanceof UploadsUnavailable) setUploadsOff(true);
        current = current.map((x, j) => (j === i ? { ...x, status: "failed", error: describeError(e) } : x));
        setFiles(current);
        toast({ message: "The photo didn't upload. Nothing was recorded; try again.", tone: "risk" });
        setBusy(false);
        return;
      }
    }
    const ids = current.map((f) => f.assetId).filter(Boolean) as string[];
    const ok = await onConfirm(ids, note.trim());
    setBusy(false);
    if (ok) onClose();
  };

  const ready = files.some((f) => f.assetId) || files.length > 0;

  return (
    <ResponsiveDialog
      mobile={mobile}
      open={open}
      onClose={onClose}
      title="Mark installed"
      description={partName}
      size="sm"
      footer={
        <>
          <Button variant="primary" loading={busy} disabled={!ready} disabledReason="Add a photo of the installed part first." onClick={() => void submit()}>Save installation</Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <div className="stack">
        <Notice tone="wait" lead="Evidence required">A photo of the installed part is required before the state changes.</Notice>
        <Button variant="glass" size="xl" className="ik-photo-btn" block>
          <span aria-hidden="true">📷</span> {files.length ? "Add another photo" : "Add photo"}
          <input type="file" accept="image/*" capture="environment" multiple onChange={add} aria-label="Add photo of the installed part" />
        </Button>
        {files.map((f) => (
          <div key={f.key} className="stack-sm" style={{ gap: 4 }}>
            <div className="between fs13">
              <span className="truncate">{f.name}</span>
              <span className="fs12" style={{ color: f.status === "failed" ? "var(--blocked)" : f.assetId ? "var(--ok)" : "var(--t3)" }}>
                {f.assetId ? "Uploaded" : f.status === "uploading" ? `Uploading… ${f.progress}%` : f.status === "failed" ? "Not in AZKT" : "Ready to send"}
              </span>
            </div>
            {f.status === "uploading" ? <div className="ik-progress"><span style={{ width: `${f.progress}%` }} /></div> : null}
            {f.error ? <span className="fs12" style={{ color: "var(--blocked)" }}>{f.error}</span> : null}
          </div>
        ))}
        <Field label="Note" hint="Anything unexpected, part numbers, readings.">
          <Textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)} placeholder="Optional" />
        </Field>
        {uploadsOff ? <Notice tone="risk" lead="Uploads unavailable">Nothing was recorded. The part stays Arrived.</Notice> : null}
      </div>
    </ResponsiveDialog>
  );
}

export default PartsChain;
