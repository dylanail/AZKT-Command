/* Files → Photos. A slot grid: a filled slot shows its photo, an empty one a dashed
   "No photo yet · <slot>" placeholder. Uploads use the assets contract (prepare → PUT → finalize → link).
   Selecting a photo reveals its actions: set slot (the grid's order), make main photo, remove link.
   Classification, private and pre-arrival are labelled, never implied by colour alone. */
import { useRef, useState, type ChangeEvent } from "react";
import { Badge, Button, Chip, Menu, Notice, useToast } from "../../../ui";
import { api, command, describeError } from "../../../lib/api";
import { CLASSIFICATION_LABELS, slotLabel, thumbUrl, type AssetBrief } from "../types";
import { UploadsUnavailable, humanSize, linkAsset, uploadOne } from "../uploads";

export interface PhotoSlotsProps {
  vehicleId: string;
  photos: AssetBrief[];
  requiredSlots: string[];
  missingSlots: string[];
  heroAssetId?: string | null;
  canWrite: boolean;
  writeReason?: string;
  onChanged: () => void;
  /** Link to the intake composer for this vehicle ("Add photos/update with Manager"). */
  intakeHref?: string;
}

interface Pending {
  key: string;
  name: string;
  size: number;
  type: string;
  file: File;
  slot: string | null;
  status: "pending" | "uploading" | "linking" | "done" | "failed";
  progress: number;
  error?: string;
}

function photoOfSlot(photos: AssetBrief[], slot: string): AssetBrief | undefined {
  return photos.find((p) => p.link?.slot === slot);
}

export function PhotoSlots({ vehicleId, photos, requiredSlots, missingSlots, heroAssetId, canWrite, writeReason, onChanged, intakeHref }: PhotoSlotsProps) {
  const { toast } = useToast();
  const [selected, setSelected] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending[]>([]);
  const [uploadsOff, setUploadsOff] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const targetSlot = useRef<string | null>(null);

  const slots = requiredSlots.length ? requiredSlots : [];
  const slotted = new Set(slots.map((s) => photoOfSlot(photos, s)?.id).filter(Boolean) as string[]);
  const extras = photos.filter((p) => !slotted.has(p.id));
  const filledCount = photos.length;
  const requiredFilled = slots.filter((s) => !!photoOfSlot(photos, s)).length;

  const runUploads = async (items: Pending[]) => {
    for (const item of items) {
      setPending((xs) => xs.map((x) => (x.key === item.key ? { ...x, status: "uploading", progress: 0, error: undefined } : x)));
      try {
        const assetId = await uploadOne(item.file, {
          purpose: "intake",
          onProgress: (pct) => setPending((xs) => xs.map((x) => (x.key === item.key ? { ...x, progress: pct } : x))),
        });
        setPending((xs) => xs.map((x) => (x.key === item.key ? { ...x, status: "linking", progress: 100 } : x)));
        await linkAsset(assetId, "vehicle", vehicleId, { role: "photo", slot: item.slot });
        setPending((xs) => xs.filter((x) => x.key !== item.key));
        onChanged();
      } catch (e) {
        const off = e instanceof UploadsUnavailable;
        if (off) setUploadsOff(true);
        setPending((xs) => xs.map((x) => (x.key === item.key ? { ...x, status: "failed", error: off ? "Uploads aren't available yet." : describeError(e) } : x)));
      }
    }
  };

  const addFiles = (e: ChangeEvent<HTMLInputElement>) => {
    const list = Array.from(e.target.files || []);
    e.target.value = "";
    const slot = targetSlot.current;
    targetSlot.current = null;
    if (!list.length) return;
    const next: Pending[] = list.map((f, i) => ({
      key: `${Date.now()}-${i}-${f.name}`,
      name: f.name, size: f.size, type: f.type || "image/jpeg", file: f,
      slot: slot ?? null, status: "pending", progress: 0,
    }));
    setPending((xs) => [...xs, ...next]);
    void runUploads(next);
  };

  const setSlot = async (asset: AssetBrief, slot: string) => {
    setBusy(asset.id);
    try {
      await linkAsset(asset.id, "vehicle", vehicleId, { role: asset.link?.role || "photo", slot });
      toast({ message: `Moved to ${slotLabel(slot)}`, tone: "ok" });
      onChanged();
    } catch (e) {
      toast({ message: describeError(e), tone: "blocked" });
    } finally {
      setBusy(null);
    }
  };

  const removeLink = async (asset: AssetBrief) => {
    setBusy(asset.id);
    try {
      const body: Record<string, unknown> = { entity_kind: "vehicle", entity_id: vehicleId, role: asset.link?.role || "photo", reason: "removed from the vehicle" };
      if (asset.link?.id) body.link_id = asset.link.id;
      await api.post(`/api/assets/${encodeURIComponent(asset.id)}/unlink`, body);
      toast({ message: "Photo removed from this vehicle. The file itself is kept.", tone: "ok" });
      setSelected(null);
      onChanged();
    } catch (e) {
      toast({ message: describeError(e), tone: "blocked" });
    } finally {
      setBusy(null);
    }
  };

  const makeHero = async (asset: AssetBrief) => {
    setBusy(asset.id);
    try {
      const r = await command(`/api/vehicles/${encodeURIComponent(vehicleId)}/update`, { hero_asset_id: asset.id, source_kind: "manual" });
      if (r.status === "ok") { toast({ message: "Main photo set", tone: "ok" }); onChanged(); }
      else toast({ message: r.decision.reasons.join(" · ") || "Not changed.", tone: "risk" });
    } catch (e) {
      toast({ message: describeError(e), tone: "blocked" });
    } finally {
      setBusy(null);
    }
  };

  const sel = selected ? photos.find((p) => p.id === selected) || null : null;

  const addButton = (slot: string | null, label: string, size: "sm" | "lg" = "sm") => (
    <Button
      variant={size === "lg" ? "glass" : "soft"}
      size={size === "lg" ? "xl" : "sm"}
      className="ik-photo-btn"
      disabled={!canWrite || uploadsOff}
      disabledReason={!canWrite ? writeReason || "Your role can't add photos." : "Uploads aren't available yet."}
      onClick={() => { targetSlot.current = slot; }}
    >
      {label}
      {canWrite && !uploadsOff ? (
        <input type="file" accept="image/*" multiple onChange={addFiles} aria-label={label} onClick={() => { targetSlot.current = slot; }} />
      ) : null}
    </Button>
  );

  return (
    <div className="stack-sm">
      <div className="vh-sect__head">
        <h2>Photos <span className="count">{slots.length ? `${requiredFilled} of ${slots.length} slots` : `${filledCount}`}</span></h2>
        <div className="row-wrap">
          {addButton(null, "Add photos")}
          {intakeHref ? <Button size="sm" variant="ghost" to={intakeHref}>Add with Manager</Button> : null}
        </div>
      </div>

      {missingSlots.length ? (
        <span className="fs12 t3">Missing: {missingSlots.map(slotLabel).join(", ")}</span>
      ) : null}

      <div className="vh-photos">
        {slots.map((slot) => {
          const p = photoOfSlot(photos, slot);
          return p ? (
            <SlotCell key={slot} asset={p} slot={slot} hero={p.id === heroAssetId} selected={selected === p.id} onSelect={() => setSelected(selected === p.id ? null : p.id)} />
          ) : (
            <div key={slot} className="vh-slot vh-slot--empty">
              <span>No photo yet · {slotLabel(slot)}</span>
              {canWrite && !uploadsOff ? addButton(slot, "Add") : null}
            </div>
          );
        })}
        {extras.map((p) => (
          <SlotCell key={p.id} asset={p} slot={p.link?.slot || null} hero={p.id === heroAssetId} selected={selected === p.id} onSelect={() => setSelected(selected === p.id ? null : p.id)} />
        ))}
        {!slots.length && !extras.length ? (
          <div className="vh-slot vh-slot--empty"><span>No photo yet</span></div>
        ) : null}
      </div>

      {sel ? (
        <div className="vh-photo-acts" role="group" aria-label={`Actions for ${sel.original_name || "photo"}`}>
          <span className="fs12 t3 truncate" style={{ maxWidth: 200 }}>{sel.original_name || "Photo"}</span>
          {sel.classification ? <Chip size="sm" tone="soft">{CLASSIFICATION_LABELS[sel.classification] || sel.classification}</Chip> : null}
          {sel.sensitive ? <Chip size="sm" tone="risk">Private</Chip> : null}
          {sel.pre_arrival ? <Chip size="sm" tone="wait">Pre-arrival</Chip> : null}
          {sel.public_eligible ? <Chip size="sm" tone="ok">Listing eligible</Chip> : <Chip size="sm" tone="soft">Private to AZKT</Chip>}
          <Menu
            label="Move photo to a slot"
            align="right"
            heading={<span>Slot: {slotLabel(sel.link?.slot)}</span>}
            items={(slots.length ? slots : []).map((s) => ({
              label: slotLabel(s),
              meta: photoOfSlot(photos, s) ? "taken" : undefined,
              current: sel.link?.slot === s,
              onSelect: () => void setSlot(sel, s),
              disabled: !canWrite,
              disabledReason: writeReason || "Your role can't change photos.",
            }))}
            trigger={<Button size="sm" variant="soft" disabled={!slots.length} disabledReason="This vehicle has no photo slots configured.">Move to slot</Button>}
          />
          <Button size="sm" variant="soft" loading={busy === sel.id} disabled={!canWrite || sel.id === heroAssetId} disabledReason={sel.id === heroAssetId ? "Already the main photo." : writeReason || "Your role can't change photos."} onClick={() => void makeHero(sel)}>Make main photo</Button>
          <Button size="sm" variant="ghost" loading={busy === sel.id} disabled={!canWrite} disabledReason={writeReason || "Your role can't change photos."} onClick={() => void removeLink(sel)}>Remove</Button>
          <a href={sel.urls?.web || sel.urls?.original || thumbUrl(sel.id) || "#"} target="_blank" rel="noreferrer" className="fs12">Open full size</a>
        </div>
      ) : (
        <span className="fs12 t4">Select a photo to set its slot, make it the main photo or remove it. Photos stay private until a reviewed listing package uses them.</span>
      )}

      {pending.length ? (
        <div className="stack-sm">
          {pending.map((p) => (
            <div key={p.key} className="stack-sm" style={{ gap: 4 }}>
              <div className="between fs13">
                <span className="truncate">{p.name}{p.slot ? ` · ${slotLabel(p.slot)}` : ""}</span>
                <span className="row" style={{ gap: 8 }}>
                  <span className="fs12" style={{ color: p.status === "failed" ? "var(--blocked)" : "var(--t3)" }}>
                    {p.status === "uploading" ? `Uploading… ${p.progress}%` : p.status === "linking" ? "Saving to the vehicle…" : p.status === "failed" ? "Not in AZKT" : humanSize(p.size)}
                  </span>
                  {p.status === "failed" ? (
                    <>
                      <button type="button" className="linklike fs12" onClick={() => void runUploads([p])}>Retry</button>
                      <button type="button" className="linklike fs12" onClick={() => setPending((xs) => xs.filter((x) => x.key !== p.key))}>Remove</button>
                    </>
                  ) : null}
                </span>
              </div>
              {p.status === "uploading" ? (
                <div className="ik-progress" role="progressbar" aria-valuenow={p.progress} aria-valuemin={0} aria-valuemax={100} aria-label={`Uploading ${p.name}`}>
                  <span style={{ width: `${p.progress}%` }} />
                </div>
              ) : null}
              {p.error ? <span className="fs12" style={{ color: "var(--blocked)" }}>{p.error}</span> : null}
            </div>
          ))}
        </div>
      ) : null}

      {uploadsOff ? <Notice tone="risk" lead="Uploads unavailable">Photos can't be sent to AZKT right now. Nothing was saved; try again later.</Notice> : null}
    </div>
  );
}

function SlotCell({ asset, slot, hero, selected, onSelect }: { asset: AssetBrief; slot: string | null; hero: boolean; selected: boolean; onSelect: () => void }) {
  const [bad, setBad] = useState(false);
  const url = thumbUrl(asset.id);
  return (
    <button
      type="button"
      className="vh-slot"
      aria-pressed={selected}
      onClick={onSelect}
      style={selected ? { outline: "2px solid var(--act)", outlineOffset: 2 } : undefined}
      title={asset.original_name || slotLabel(slot)}
    >
      {url && !bad ? <img src={url} alt={asset.original_name || slotLabel(slot)} loading="lazy" onError={() => setBad(true)} /> : <span>Photo saved · preview unavailable</span>}
      <span className="vh-slot__cap">
        <span>{slotLabel(slot)}</span>
        {hero ? <Badge tone="act">Main</Badge> : asset.sensitive ? <Badge tone="risk">Private</Badge> : null}
      </span>
    </button>
  );
}

export default PhotoSlots;
