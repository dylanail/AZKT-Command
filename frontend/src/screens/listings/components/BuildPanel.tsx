/* What the package was built from (ListingPackage.evidence), and what a rebuild from today's vehicle
   record would change (GET /api/listings/vehicles/{id}/diff, and the same diff inside the package view). */
import { Link } from "react-router-dom";
import { EmptyState, Money, NotRecorded, When } from "../../../ui";
import { classLabel, diffFieldLabel, diffValueText, type ListingPackage, type PackageDiff } from "../types";

export function PulledFrom({ pkg }: { pkg: ListingPackage }) {
  const ev = pkg.evidence || {};
  const price = ev.price || {};
  const eta = ev.eta || null;
  const media = pkg.media_detail || [];
  const slots = media.filter((m) => m.slot).length;
  return (
    <div className="stack-sm">
      <div className="lst-pulled">
        <div className="lst-pulled__item">
          <span className="lst-pulled__label">Listing type</span>
          <span className="lst-pulled__value">{classLabel(pkg.listing_class)}</span>
          <span className="lst-pulled__sub">
            {ev.logistics_state ? `from where the truck is: ${ev.logistics_state.replace(/_/g, " ")}` : "from the vehicle's stage"}
          </span>
        </div>
        <div className="lst-pulled__item">
          <span className="lst-pulled__label">Price</span>
          <span className="lst-pulled__value">
            {pkg.price ? <Money amount={pkg.price} currency={pkg.currency || "USD"} /> : <NotRecorded text="Not recorded" />}
          </span>
          <span className="lst-pulled__sub">
            {price.approved_at
              ? <>approved <When iso={price.approved_at} format="date" /></>
              : "only an owner-approved asking price reaches a listing"}
          </span>
        </div>
        <div className="lst-pulled__item">
          <span className="lst-pulled__label">Facts used</span>
          <span className="lst-pulled__value">{(pkg.specs || []).length} recorded</span>
          <span className="lst-pulled__sub">{ev.specs_from || "confirmed and reported facts only"}</span>
        </div>
        <div className="lst-pulled__item">
          <span className="lst-pulled__label">Photo slots</span>
          <span className="lst-pulled__value">{media.length} photo{media.length === 1 ? "" : "s"}</span>
          <span className="lst-pulled__sub">
            {slots ? `${slots} in a named slot · ` : ""}{ev.media_from || "approved public photos only"}
          </span>
        </div>
        <div className="lst-pulled__item">
          <span className="lst-pulled__label">Copy</span>
          <span className="lst-pulled__value">{pkg.generated_by === "model" ? "Written by the assistant" : "Written from the template"}</span>
          <span className="lst-pulled__sub">Either way, only recorded facts are used.</span>
        </div>
        <div className="lst-pulled__item">
          <span className="lst-pulled__label">Stock number</span>
          <span className="lst-pulled__value">{ev.stock_no || ev.sku || <NotRecorded />}</span>
          <span className="lst-pulled__sub">{ev.frame_no ? `frame ${ev.frame_no}` : "used as the site's SKU where the site allows it"}</span>
        </div>
      </div>
      {pkg.listing_class === "en_route" ? (
        <span className="lst-sect__note">
          {eta?.at
            ? <>Arrival stated as <When iso={eta.at} format="date" /> — estimated, from {eta.source || "a recorded milestone"}.</>
            : "No arrival date is claimed: none has a source yet, so the listing says the date isn't confirmed."}
        </span>
      ) : null}
    </div>
  );
}

export function DiffPanel({ diff, vehicleId, emptyText }: { diff: PackageDiff | null; vehicleId: string; emptyText: string }) {
  const changed = diff?.changed || [];
  if (diff?.first_version) {
    return (
      <EmptyState
        align="left"
        title="First version"
        body={<>Nothing to compare against — this is the first package built for this truck. <Link to={`/vehicles/${encodeURIComponent(vehicleId)}`}>Open the vehicle</Link></>}
      />
    );
  }
  if (!changed.length) {
    return <EmptyState align="left" title="Nothing has changed" body={emptyText} />;
  }
  const detail = diff?.detail || {};
  return (
    <div className="lst-diff">
      {changed.map((field) => {
        const d = detail[field];
        return (
          <div key={field} className="lst-diff__row">
            <span className="lst-diff__field">{diffFieldLabel(field)}</span>
            <span className="lst-diff__values">
              {d ? (
                <>
                  <span className="lst-diff__from">{diffValueText(d.from)}</span>
                  <span className="lst-diff__to">{diffValueText(d.to)}</span>
                </>
              ) : (
                <span className="lst-diff__to">Changed. The exact before-and-after wasn't recorded for this field.</span>
              )}
            </span>
          </div>
        );
      })}
    </div>
  );
}
