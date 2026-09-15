/* The assets upload contract (backend/app/routers/assets.py):
     POST /api/uploads                 → a bounded, expiring slot with limits and allowed types
     PUT  /api/uploads/{id}            → raw bytes (XHR so we can show progress)
     POST /api/uploads/{id}/finalize   → validate / dedupe / derive → asset id
   A failed file stays failed and retryable; nothing partial ever becomes an asset, and the caller keeps
   the bytes on the device until an asset id comes back ("Saved on device, not in AZKT"). */
import { ApiError, api } from "../../lib/api";

export class UploadsUnavailable extends Error {
  constructor() { super("Uploads aren't available yet."); this.name = "UploadsUnavailable"; }
}

type Loose = Record<string, unknown> | null | undefined;
const pick = (o: Loose, ...keys: string[]): unknown => {
  for (const k of keys) { const v = o?.[k]; if (v !== undefined && v !== null) return v; }
  return undefined;
};
/** Command endpoints answer with the CommandResult envelope; reads may answer bare. */
function payload(raw: unknown): Loose {
  const o = raw as Loose;
  const d = o?.["data"];
  return (d && typeof d === "object" ? (d as Record<string, unknown>) : o) as Loose;
}

export interface UploadSlot {
  uploadId: string;
  putUrl: string;
  maxBytes: number | null;
  allowedTypes: string[];
}

export interface UploadLimits {
  maxBytes: number | null;
  allowedTypes: string[];
}

export async function prepareUpload(file: { name: string; type: string; size: number }, opts: { purpose?: string; intakeId?: string } = {}): Promise<UploadSlot> {
  const body: Record<string, unknown> = {
    purpose: opts.purpose || "intake",
    content_type: file.type || "image/jpeg",
    size_bytes: file.size,
    filename: file.name,
  };
  if (opts.intakeId) body.intake_id = opts.intakeId;
  const raw = await api.post<unknown>("/api/uploads", body, { tolerate: [404, 405, 501] });
  if (raw === null) throw new UploadsUnavailable();
  const d = payload(raw);
  const upload = pick(d, "upload") as Loose;
  const uploadId = (pick(upload, "id") ?? pick(d, "upload_id", "id")) as string | undefined;
  if (!uploadId) throw new Error("The upload slot came back without an id.");
  const putUrl = (pick(d, "put_url") as string | undefined) || (pick(upload, "put_url") as string | undefined) || `/api/uploads/${encodeURIComponent(uploadId)}`;
  const maxBytes = Number(pick(d, "max_bytes") ?? pick(upload, "max_bytes"));
  const allowed = (pick(d, "allowed_types") ?? pick(upload, "allowed_types")) as string[] | undefined;
  return { uploadId, putUrl, maxBytes: Number.isFinite(maxBytes) ? maxBytes : null, allowedTypes: allowed || [] };
}

export function putBytes(url: string, blob: Blob, type: string, onProgress: (pct: number) => void, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url, true);
    xhr.withCredentials = true;
    xhr.setRequestHeader("Content-Type", type || "application/octet-stream");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100)); };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) { onProgress(100); resolve(); }
      else if (xhr.status === 404 || xhr.status === 501) reject(new UploadsUnavailable());
      else if (xhr.status === 413) reject(new ApiError(413, "blocked", "That file is larger than the upload limit."));
      else reject(new ApiError(xhr.status, "http", xhr.responseText ? xhr.responseText.slice(0, 200) : `Upload failed (${xhr.status})`));
    };
    xhr.onerror = () => reject(new ApiError(0, "network", "Could not reach AZKT. The photo is kept on this device."));
    xhr.onabort = () => reject(new ApiError(0, "network", "Upload cancelled."));
    if (signal) signal.addEventListener("abort", () => xhr.abort(), { once: true });
    xhr.send(blob);
  });
}

export interface FinalizeResult { assetId: string | null; status: string; error: string | null }

export async function finalizeUpload(uploadId: string, opts: { classification?: string; preArrival?: boolean; capturedAt?: string | null } = {}): Promise<FinalizeResult> {
  const body: Record<string, unknown> = { source: "upload" };
  if (opts.classification) body.classification = opts.classification;
  if (opts.preArrival) body.pre_arrival = true;
  if (opts.capturedAt) body.captured_at = opts.capturedAt;
  const raw = await api.post<unknown>(`/api/uploads/${encodeURIComponent(uploadId)}/finalize`, body, { tolerate: [404, 501] });
  if (raw === null) throw new UploadsUnavailable();
  const d = payload(raw);
  const status = String(pick(d, "status") || "unknown");
  const err = pick(d, "error") as string | null | undefined;
  if (status === "failed") return { assetId: null, status, error: err || "The file failed its checks." };
  const asset = pick(d, "asset") as Loose;
  const assetId = (pick(asset, "id") ?? pick(d, "asset_id")) as string | undefined;
  if (!assetId) return { assetId: null, status, error: err || "The upload finished without an asset id." };
  return { assetId, status, error: null };
}

/** prepare → PUT → finalize. Resolves with the asset id; throws with a plain-language message otherwise. */
export async function uploadOne(
  file: Blob & { name?: string; type: string; size: number },
  opts: { purpose?: string; intakeId?: string; classification?: string; preArrival?: boolean; onProgress?: (pct: number) => void; onSlot?: (uploadId: string) => void } = {},
): Promise<string> {
  const slot = await prepareUpload({ name: file.name || "photo.jpg", type: file.type, size: file.size }, opts);
  opts.onSlot?.(slot.uploadId);
  if (slot.maxBytes && file.size > slot.maxBytes) {
    throw new ApiError(413, "blocked", `That file is ${Math.round(file.size / 1048576)} MB; the limit is ${Math.round(slot.maxBytes / 1048576)} MB.`);
  }
  await putBytes(slot.putUrl, file, file.type, opts.onProgress || (() => undefined));
  const fin = await finalizeUpload(slot.uploadId, opts);
  if (!fin.assetId) throw new Error(fin.error || "The upload did not finish.");
  return fin.assetId;
}

/** Link a saved asset to a record (vehicle photo/document, task evidence…). */
export async function linkAsset(assetId: string, entityKind: string, entityId: string, opts: { role?: string; slot?: string | null; position?: number } = {}) {
  const body: Record<string, unknown> = { entity_kind: entityKind, entity_id: entityId, role: opts.role || "photo" };
  if (opts.slot) body.slot = opts.slot;
  if (opts.position !== undefined) body.position = opts.position;
  return api.post<unknown>(`/api/assets/${encodeURIComponent(assetId)}/link`, body);
}

/* ---------- local previews / drafts ---------- */
const MAX_DRAFT_BYTES = 3 * 1024 * 1024;

export function readAsDataUrl(file: File): Promise<string | undefined> {
  if (file.size > MAX_DRAFT_BYTES) return Promise.resolve(undefined);
  return new Promise((res) => {
    const r = new FileReader();
    r.onload = () => res(typeof r.result === "string" ? r.result : undefined);
    r.onerror = () => res(undefined);
    r.readAsDataURL(file);
  });
}

export function dataUrlToBlob(dataUrl: string): Blob {
  const [head, body] = dataUrl.split(",");
  const type = /data:([^;]+)/.exec(head)?.[1] || "application/octet-stream";
  const bin = atob(body || "");
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type });
}

export function humanSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1048576) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1048576).toFixed(1)} MB`;
}
