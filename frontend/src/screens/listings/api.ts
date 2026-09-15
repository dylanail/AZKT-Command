/* Exact paths from backend/app/routers/listings.py. Reads tolerate 404 so a vehicle without a package
   renders an honest empty state instead of an error page. */
import { api } from "../../lib/api";
import type { PackagePreviewResp, PackageViewResp, PublicationsResp } from "./types";

const enc = encodeURIComponent;

export const listingPaths = {
  /** GET — the current package for a vehicle/channel plus readiness, diff, publications and the site profile. */
  vehiclePackage: (vehicleId: string, channel: string) =>
    `/api/listings/vehicles/${enc(vehicleId)}/package?channel=${enc(channel)}`,
  /** GET — the rendered copy, photos, price and the site payload for one exact package version. */
  preview: (packageId: string) => `/api/listings/packages/${enc(packageId)}/preview`,
  /** POST — build or refresh the package from vehicle facts and photos. */
  build: (vehicleId: string) => `/api/listings/vehicles/${enc(vehicleId)}/build`,
  /** GET — what would change if the package were rebuilt from today's vehicle record. */
  diff: (vehicleId: string, channel: string) =>
    `/api/listings/vehicles/${enc(vehicleId)}/diff?channel=${enc(channel)}`,
  /** POST — freeze a ready package for owner review (→ needs_review). */
  submit: (packageId: string) => `/api/listings/packages/${enc(packageId)}/submit`,
  /** POST — publish an approved package (owner only, listings.publish). */
  publish: (packageId: string) => `/api/listings/packages/${enc(packageId)}/publish`,
  /** POST — set the desired availability and queue the channel updates. */
  availability: () => "/api/listings/publish-availability",
  /** GET — publication history (all vehicles, or one). */
  publications: (vehicleId?: string) =>
    vehicleId ? `/api/listings/publications?vehicle_id=${enc(vehicleId)}` : "/api/listings/publications",
};

export function fetchPreview(packageId: string, signal?: AbortSignal) {
  return api.get<PackagePreviewResp | null>(listingPaths.preview(packageId), { signal, tolerate: [404] });
}

export function fetchVehiclePackage(vehicleId: string, channel: string, signal?: AbortSignal) {
  return api.get<PackageViewResp | null>(listingPaths.vehiclePackage(vehicleId, channel), { signal, tolerate: [404] });
}

export function fetchPublications(vehicleId: string | undefined, signal?: AbortSignal) {
  return api.get<PublicationsResp | null>(listingPaths.publications(vehicleId), { signal, tolerate: [403, 404] });
}
