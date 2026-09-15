/* Evidence file → asset id, through the upload contract in backend/app/routers/assets.py:
     POST /api/uploads            → slot (limits + allowed types first)
     PUT  /api/uploads/{id}       → the raw bytes
     POST /api/uploads/{id}/finalize → the stored asset
   The caller receives `asset:<id>`, which is what the domain commands store as source_ref.
   Nothing is claimed until finalize answers with a ready asset. */
import { useRef, useState } from "react";
import { ApiError, command, describeError } from "../../../lib/api";
import { Button, Notice } from "../../../ui";

interface UploadSlot {
  upload?: { id: string; put_url?: string; max_bytes?: number; allowed_types?: string[] };
  put_url?: string;
  max_bytes?: number;
  allowed_types?: string[];
}
interface FinalizeResp {
  status?: string;
  asset?: { id: string; kind?: string; content_type?: string | null } | null;
  error?: string | null;
  deduplicated?: boolean;
}

export interface EvidenceUploadProps {
  /** Called with `asset:<id>` once the file is stored. */
  onUploaded: (sourceRef: string, assetId: string) => void;
  purpose?: "evidence" | "document" | "intake";
  label?: string;
  hint?: string;
  disabled?: boolean;
  disabledReason?: string;
  accept?: string;
}

export function EvidenceUpload({
  onUploaded, purpose = "document", label = "Upload the signed copy",
  hint = "PDF or photo. The file is stored privately and its asset id becomes the evidence reference.",
  disabled, disabledReason, accept = "application/pdf,image/*",
}: EvidenceUploadProps) {
  const ref = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [name, setName] = useState<string | null>(null);

  const pick = (file: File) => { void upload(file); };

  const upload = async (file: File) => {
    setBusy(true);
    setError(null);
    setDone(null);
    setName(file.name);
    try {
      const slot = await command<UploadSlot>("/api/uploads", {
        purpose,
        content_type: file.type || undefined,
        size_bytes: file.size,
        filename: file.name,
      });
      if (slot.status === "blocked") { setError(slot.decision.reasons.join(" · ") || "The upload was blocked."); return; }
      const id = slot.data?.upload?.id;
      const putUrl = slot.data?.put_url || slot.data?.upload?.put_url || (id ? `/api/uploads/${id}` : null);
      if (!id || !putUrl) { setError("The upload slot came back without an id. Try again."); return; }

      const put = await fetch(putUrl, {
        method: "PUT",
        credentials: "include",
        headers: file.type ? { "Content-Type": file.type } : undefined,
        body: file,
      });
      if (!put.ok) {
        let msg = `Upload failed (${put.status}).`;
        try { const b = await put.json(); if (b && typeof b.message === "string") msg = b.message; } catch { /* keep the status message */ }
        setError(msg);
        return;
      }

      const fin = await command<FinalizeResp>(`/api/uploads/${encodeURIComponent(id)}/finalize`, { source: "upload" });
      if (fin.status === "blocked") { setError(fin.decision.reasons.join(" · ") || "The file was rejected."); return; }
      const d = fin.data;
      if (!d || d.status === "failed" || !d.asset?.id) { setError(d?.error || "The file could not be stored."); return; }
      setDone(d.asset.id);
      onUploaded(`asset:${d.asset.id}`, d.asset.id);
    } catch (e) {
      setError(e instanceof ApiError && e.isDenied ? "Your role can't upload files." : describeError(e));
    } finally {
      setBusy(false);
      if (ref.current) ref.current.value = "";
    }
  };

  return (
    <div className="stack-sm">
      <input
        ref={ref}
        type="file"
        accept={accept}
        className="sr-only"
        onChange={(e) => { const f = e.target.files?.[0]; if (f) pick(f); }}
        tabIndex={-1}
        aria-hidden="true"
      />
      <div className="row-wrap">
        <Button size="sm" variant="soft" loading={busy} disabled={disabled || busy} disabledReason={disabledReason} onClick={() => ref.current?.click()}>
          {busy ? "Uploading…" : label}
        </Button>
        {name && !busy ? <span className="fs12 t4 truncate" style={{ maxWidth: 220 }}>{name}</span> : null}
      </div>
      <span className="field__hint">{hint}</span>
      {done ? <Notice tone="ok" lead="Stored">Evidence asset <code>{done}</code> is attached as the source reference.</Notice> : null}
      {error ? <Notice tone="blocked" lead="Not stored" role="alert">{error}</Notice> : null}
    </div>
  );
}

/** Small helper so callers can check the upload permission the same way the API does. */
export function uploadDisabledReason(canIntake: boolean): string | undefined {
  return canIntake ? undefined : "Your role can't upload files. Paste an existing reference instead.";
}

export default EvidenceUpload;
