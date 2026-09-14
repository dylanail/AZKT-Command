/* Listing / publication editor (not yet designed in the prototype): draft copy, photos, price, channels; publish → approval.
   TODO(screen builder): GET /api/listings/{id} ; PATCH /api/listings/{id} ; POST /api/listings/{id}/publish (→ needs_review). */
import { useParams } from "react-router-dom";
import { useCan } from "../../lib/auth";
import { whyNot } from "../../lib/perms";
import { Button } from "../../ui";
import { Scaffold } from "../scaffold";

export default function ListingEditor() {
  const { id = "" } = useParams();
  const can = useCan();
  return (
    <Scaffold
      title={`Listing ${id}`}
      crumbs={[{ label: "Vehicles", to: "/vehicles?view=sales" }, { label: `Listing ${id}` }]}
      probe={`/api/listings/${encodeURIComponent(id)}`}
      emptyTitle="Listing not found"
      emptyBody={`Nothing recorded for ${id} yet.`}
      actions={<Button variant="primary" disabled disabledReason={can("listings.publish") ? "Publishing connects with the listing data." : whyNot("listings.publish")}>Publish</Button>}
    />
  );
}
