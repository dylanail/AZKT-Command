/* Shapes copied from backend/app/services/listings.py (serialize_package, serialize_publication,
   package_view, preview_payload, diff_packages) and services/site_profile.py (serialize_profile).
   Nothing here is invented: every field below exists in one of those serializers. */

/* ---------- package ---------- */
export interface SpecItem {
  key: string;
  value: string;
  unit?: string | null;
  status?: string;        // recorded | confirmed | reported
  source?: string | null;
  source_ref?: string | null;
  observed_at?: string | null;
  fact_id?: string | null;
}
export interface DisclosureItem { text: string; source?: string; issue_id?: string; status?: string }
export interface MediaItem {
  asset_id: string;
  sha256?: string | null;
  slot?: string | null;
  position?: number | null;
  pre_arrival?: boolean;
  url: string;            // "/api/assets/<id>/web"
  alt?: string | null;
  source?: string | null;
  provider_link?: string | null;
}
export interface ReadinessCheck {
  requirement: string;
  label: string;
  ok: boolean;
  detail: string;
  blocking?: boolean;
  param?: Record<string, unknown>;
}
export interface PackageEvidence {
  eta?: { kind?: string; at?: string | null; status?: string; source?: string | null; source_ref?: string | null; milestone_id?: string } | null;
  price?: { amount?: string | null; approved_at?: string | null; missing?: boolean } | null;
  sku?: string | null;
  stock_no?: string | null;
  frame_no?: string | null;
  logistics_state?: string | null;
  recon_state?: string | null;
  documents_state?: string | null;
  specs_from?: string | null;
  media_from?: string | null;
}
export interface DiffDetailEntry { from: unknown; to: unknown }
export interface PackageDiff {
  first_version?: boolean;
  changed?: string[];
  detail?: Record<string, DiffDetailEntry>;
  from_version?: number;
  from_hash?: string;
}
export interface ListingPackage {
  id: string;
  version: number;
  vehicle_id: string;
  package_version: number;
  listing_class: string;          // en_route | ready_for_sale
  channel: string;
  headline: string | null;
  body: string | null;
  short_description: string | null;
  price: string | null;
  currency: string | null;
  specs: SpecItem[];
  disclosures: DisclosureItem[];
  media: string[];
  media_detail: MediaItem[];
  availability: string | null;    // available | reserved | sold | en_route
  profile_id: string | null;
  profile_version: number | null;
  package_hash: string | null;
  diff: PackageDiff;
  readiness: ReadinessCheck[];
  status: string;                 // draft | review | approved | published | superseded | invalidated
  approval_id: string | null;
  supersedes_id: string | null;
  evidence: PackageEvidence;
  generated_by: string | null;    // model | template
  blocked_reasons: string[];
  ready: boolean;
  built_at: string | null;
  created_at: string | null;
}

/* ---------- publication ---------- */
export interface PublicationHistoryEntry { state: string; at: string; detail?: string; [k: string]: unknown }
export interface Publication {
  id: string;
  version: number;
  package_id: string | null;
  vehicle_id: string;
  channel: string;
  external_id: string | null;
  external_url: string | null;
  desired_state: string | null;
  observed_state: string | null;
  state: string;                  // queued | accepted | published | verified | pending_verification | mismatch | failed | unknown | needs_review | unsupported | cleanup_pending
  external_action_id: string | null;
  receipt: Record<string, unknown>;
  media_map: Record<string, unknown>;
  last_verified_at: string | null;
  error: string | null;
  error_kind: string | null;
  manual_task_id: string | null;
  profile_id: string | null;
  profile_version: number | null;
  package_version: number | null;
  package_hash: string | null;
  attempts: number | null;
  verification: Record<string, unknown>;
  history: PublicationHistoryEntry[];
  unsupported_reason: string | null;
  cleanup_required: boolean;
}

/* ---------- reads ---------- */
export interface PreviewPayload {
  payload: Record<string, unknown>;
  valid: boolean;
  errors: string[];
  warnings: string[];
  profile_version: number | null;
  target: string | null;
  written: boolean;
}
/** GET /api/listings/packages/{id}/preview */
export interface PackagePreviewResp extends PreviewPayload { package: ListingPackage }

/** GET /api/listings/vehicles/{id}/package?channel= */
export interface PackageViewResp {
  package: ListingPackage | null;
  diff: PackageDiff;
  readiness: ReadinessCheck[];
  ready: boolean;
  blocked_reasons: string[];
  listing_class: string;
  publications: Publication[];
  profile: { id: string; version: number; status: string; writes_paused: boolean; reason: string | null } | null;
  preview: PreviewPayload | null;
}

/** GET /api/listings/vehicles/{id}/diff — inside CommandResult.data (read command). */
export interface DiffResp {
  current: ListingPackage | null;
  diff: PackageDiff;
  readiness: ReadinessCheck[];
  ready: boolean;
  would_hash: string;
}

/** GET /api/listings/publications */
export interface PublicationsResp { items: Publication[]; channels: { supported: string[] } }

/* command results */
export interface BuildResult { package: ListingPackage; diff: PackageDiff; created: boolean; readiness: ReadinessCheck[]; ready: boolean }
export interface SubmitResult { package: ListingPackage; preview: PreviewPayload; review_task_id: string | null }
export interface PublishResult { publication: Publication; state: string; queued?: boolean; external_action_id?: string }
export interface AvailabilityEntry {
  publication_id: string; channel: string; desired_state: string; cancelled_queued: string[];
  queued: boolean; status?: string; manual?: boolean; error?: string; approval_id?: string | null;
}
export interface AvailabilityResult { publications: AvailabilityEntry[]; changed: boolean; note?: string }

/* ---------- labels ---------- */
export const CHANNEL_LABELS: Record<string, string> = { website: "Website" };
export function channelLabel(c: string | null | undefined): string {
  if (!c) return "Channel";
  return CHANNEL_LABELS[c] || c.replace(/_/g, " ");
}

export const CLASS_LABELS: Record<string, string> = {
  en_route: "On its way",
  ready_for_sale: "Ready for sale",
};
export function classLabel(c: string | null | undefined): string {
  if (!c) return "Not set";
  return CLASS_LABELS[c] || c.replace(/_/g, " ");
}

export const AVAILABILITY_LABELS: Record<string, string> = {
  available: "Available", reserved: "Reserved", sold: "Sold", en_route: "On its way",
};
export function availabilityLabel(a: string | null | undefined): string {
  if (!a) return "Not set";
  return AVAILABILITY_LABELS[a] || a.replace(/_/g, " ");
}

export type Tone = "neutral" | "soft" | "act" | "amber" | "blocked" | "risk" | "ok" | "wait";

/** Package status, in the owner's words, with the exact backend word kept in the meta line. */
export const PACKAGE_STATUS: Record<string, { label: string; tone: Tone; blurb: string }> = {
  draft: { label: "Draft", tone: "soft", blurb: "Nothing is on the website yet." },
  review: { label: "Waiting for review", tone: "wait", blurb: "Frozen for the owner to read before it can be published." },
  approved: { label: "Approved", tone: "act", blurb: "Approved and queued for the site." },
  published: { label: "Published", tone: "ok", blurb: "This version was sent to the site. The publication row below says what the site did with it." },
  superseded: { label: "Replaced", tone: "neutral", blurb: "A newer version of this package was built." },
  invalidated: { label: "Invalidated", tone: "risk", blurb: "Something changed, so this version can't be used." },
};
export function packageStatusView(status: string | null | undefined): { label: string; tone: Tone; blurb: string } {
  if (!status) return { label: "Not recorded", tone: "neutral", blurb: "" };
  return PACKAGE_STATUS[status] || { label: status.replace(/_/g, " "), tone: "neutral", blurb: "" };
}

/** Publication state, in the owner's words. Never claims more than the record says. */
export const PUBLICATION_STATE: Record<string, { label: string; tone: Tone; blurb: string }> = {
  queued: { label: "Queued", tone: "wait", blurb: "Waiting to be sent to the site." },
  accepted: { label: "Accepted by the site", tone: "wait", blurb: "The site took the draft. Not confirmed live yet." },
  published: { label: "Published, not verified", tone: "wait", blurb: "The site reported it published. AZKT hasn't confirmed the public page yet." },
  verified: { label: "Live and verified", tone: "ok", blurb: "The site's data and the public page both match what was approved." },
  pending_verification: { label: "Waiting to verify", tone: "wait", blurb: "The public page hasn't caught up yet. AZKT keeps checking." },
  mismatch: { label: "Doesn't match", tone: "blocked", blurb: "What's on the site differs from what was approved." },
  failed: { label: "Failed", tone: "blocked", blurb: "The site refused the write. Nothing was published." },
  unknown: { label: "Result unknown", tone: "risk", blurb: "The answer was lost. AZKT looks the listing up before ever writing again — it never posts twice." },
  needs_review: { label: "Needs a decision", tone: "risk", blurb: "An existing site listing may be the same truck. Confirm the mapping first." },
  unsupported: { label: "Hand-off to a person", tone: "amber", blurb: "This channel has no verified connection, so posting is a task for a person." },
  cleanup_pending: { label: "Cleanup pending", tone: "blocked", blurb: "The channel still shows the old state. The task stays open until it's verified." },
};
export function publicationStateView(state: string | null | undefined): { label: string; tone: Tone; blurb: string } {
  if (!state) return { label: "Not recorded", tone: "neutral", blurb: "" };
  return PUBLICATION_STATE[state] || { label: state.replace(/_/g, " "), tone: "neutral", blurb: "" };
}

/** Which vehicle tab fixes a failing check, and what to do there. */
export function checkFix(requirement: string, vehicleId: string): { label: string; to: string; how: string } | null {
  const v = encodeURIComponent(vehicleId);
  switch (requirement) {
    case "approved_price":
      return { label: "Set the asking price", to: `/vehicles/${v}?tab=sale`, how: "Set and approve the asking price on the vehicle's Sale tab." };
    case "media_checklist":
      return { label: "Add the photos", to: `/vehicles/${v}?tab=files`, how: "Add the missing public photos on the vehicle's Files tab, then rebuild." };
    case "recon_verified":
      return { label: "Open the shop work", to: `/vehicles/${v}?tab=work`, how: "Finish and verify recon on the vehicle's Work tab." };
    case "documents_ready":
      return { label: "Open documents", to: `/vehicles/${v}?tab=files`, how: "Record the document evidence on the vehicle's Files tab." };
    case "status_truthful":
    case "eta_sourced":
      return { label: "Open the vehicle", to: `/vehicles/${v}`, how: "Record the milestone with its source so the listing can state it." };
    case "disclosures_written":
      return { label: "Open the Sale tab", to: `/vehicles/${v}?tab=sale`, how: "Write the disclosures on the vehicle's Sale tab." };
    case "configuration":
      return { label: "Website settings", to: "/settings/website", how: "Configure the publication checks for this listing class." };
    default:
      return null;
  }
}

/** Human names for the diff's field keys. */
export const DIFF_FIELD_LABELS: Record<string, string> = {
  headline: "Headline", body: "Description", short_description: "Short description", price: "Price",
  availability: "Availability", listing_class: "Listing type", disclosures: "Disclosures",
  specs: "Specifications", media: "Photos",
};
export function diffFieldLabel(k: string): string {
  return DIFF_FIELD_LABELS[k] || k.replace(/_/g, " ");
}

/** Short, readable rendering of a diff value (arrays become counts / joined text). */
export function diffValueText(v: unknown): string {
  if (v === null || v === undefined || v === "") return "Not recorded";
  if (Array.isArray(v)) {
    if (!v.length) return "none";
    if (v.every((x) => typeof x === "string")) return v.length > 3 ? `${v.length} items` : (v as string[]).join(", ");
    return `${v.length} items`;
  }
  if (typeof v === "object") return JSON.stringify(v).slice(0, 160);
  const s = String(v);
  return s.length > 200 ? `${s.slice(0, 200)}…` : s;
}
