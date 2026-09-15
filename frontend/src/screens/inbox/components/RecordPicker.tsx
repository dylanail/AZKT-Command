/* Link picker: search the four record types a thread can be about and hand back one choice.
   Vehicles /api/vehicles · leads /api/sales/opportunities · import requests /api/import-requests ·
   people /api/contacts. A type the role cannot read answers 403 and says so instead of failing silently. */
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../../lib/api";
import { useQuery } from "../../../lib/useQuery";
import { Button, EmptyState, ErrorState, Input, Loading, ResponsiveDialog, SegmentedControl } from "../../../ui";
import { pickerPath } from "../api";

export type PickerKind = "vehicle" | "opportunity" | "import_request" | "contact";
export interface PickedRecord { kind: PickerKind; id: string; label: string }

const KIND_LABELS: Record<PickerKind, string> = {
  vehicle: "Vehicle",
  opportunity: "Sales lead",
  import_request: "Import request",
  contact: "Person",
};

interface Row { id: string; label: string; meta: string }

/* eslint-disable @typescript-eslint/no-explicit-any */
function toRows(kind: PickerKind, items: any[]): Row[] {
  return (items || []).map((it): Row => {
    if (kind === "vehicle") {
      return {
        id: String(it.id),
        label: [it.stock_no, it.title].filter(Boolean).join(" · ") || String(it.id).slice(0, 8),
        meta: [it.situation, it.states?.commercial].filter(Boolean).join(" · "),
      };
    }
    if (kind === "opportunity") {
      return {
        id: String(it.id),
        label: it.card?.name || it.enquiry || String(it.id).slice(0, 8),
        meta: [it.card?.subject || it.enquiry, it.stage_label].filter(Boolean).join(" · "),
      };
    }
    if (kind === "import_request") {
      return { id: String(it.id), label: it.title || String(it.id).slice(0, 8), meta: [it.contact?.name, it.status].filter(Boolean).join(" · ") };
    }
    return { id: String(it.id), label: it.name || String(it.id).slice(0, 8), meta: [it.company, it.primary_email].filter(Boolean).join(" · ") };
  });
}
/* eslint-enable @typescript-eslint/no-explicit-any */

export default function RecordPicker({ open, onClose, onPick, mobile, busy, kinds, note }: {
  open: boolean;
  onClose: () => void;
  onPick: (r: PickedRecord) => Promise<boolean>;
  mobile: boolean;
  busy: boolean;
  /** Which record types this thread can be linked to right now. */
  kinds: PickerKind[];
  /** Why a type is missing, in plain words. */
  note?: string;
}) {
  const initialKind = kinds[0] || "vehicle";
  const [kind, setKind] = useState<PickerKind>(initialKind);
  const [raw, setRaw] = useState("");
  const [q, setQ] = useState("");
  const [chosen, setChosen] = useState<Row | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => { if (open) { setKind(initialKind); setRaw(""); setQ(""); setChosen(null); } }, [open, initialKind]);
  useEffect(() => {
    const h = window.setTimeout(() => setQ(raw.trim()), 250);
    return () => window.clearTimeout(h);
  }, [raw]);
  useEffect(() => { setChosen(null); }, [kind, q]);

  const res = useQuery<{ items?: unknown[] } | null>(
    (signal) => (open ? api.get<{ items?: unknown[] } | null>(pickerPath(kind, q), { signal, tolerate: [403, 404, 501] }) : Promise.resolve(null)),
    [open, kind, q],
  );
  const rows = useMemo(() => toRows(kind, (res.data?.items as unknown[]) || []), [kind, res.data]);
  const denied = open && !res.loading && !res.error && res.data === null;

  const confirm = async () => {
    if (!chosen) return;
    const ok = await onPick({ kind, id: chosen.id, label: chosen.label });
    if (ok) onClose();
  };

  return (
    <ResponsiveDialog
      mobile={mobile}
      open={open}
      onClose={onClose}
      title="Link this thread to a record"
      description="Linking tells AZKT which vehicle, lead, request or person the thread is about. Changing it sends any unsent draft back for review."
      size="md"
      align="top"
      initialFocusRef={inputRef}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={busy} onClick={() => void confirm()} disabled={!chosen}
            disabledReason="Pick a record from the list first.">Link</Button>
        </>
      }
    >
      <div className="stack-sm">
        <SegmentedControl<PickerKind> label="Record type" size="sm" block value={kind} onChange={setKind}
          options={kinds.map((k) => ({ value: k, label: KIND_LABELS[k] }))} />
        <Input ref={inputRef} aria-label="Search records" value={raw} placeholder="Search by name, stock number or subject"
          onChange={(e) => setRaw(e.target.value)} />
        {note ? <p className="fs12 t4" style={{ margin: 0 }}>{note}</p> : null}
        <div className="ib-picker">
          {res.loading ? <Loading label="Searching" rows={3} />
            : res.error ? <ErrorState error={res.error} onRetry={res.reload} />
            : denied ? <EmptyState align="left" title="Not available for your role" body="You can't search this kind of record, so it can't be linked from here." />
            : !rows.length ? <EmptyState align="left" title="Nothing found" body={q ? "Try a shorter word." : "Type to search."} />
            : rows.map((r) => (
              <button key={r.id} type="button" className={["ib-picker__row", chosen?.id === r.id ? "ib-picker__row--sel" : ""].filter(Boolean).join(" ")}
                onClick={() => setChosen(r)} aria-pressed={chosen?.id === r.id}>
                <span className="ib-picker__label">{r.label}</span>
                {r.meta ? <span className="ib-picker__meta t3 fs12">{r.meta}</span> : null}
              </button>
            ))}
        </div>
      </div>
    </ResponsiveDialog>
  );
}
