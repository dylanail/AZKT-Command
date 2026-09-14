/* Import request detail: requirements (must/prefer/avoid), agreement state, deposit, candidates
   (sent timestamps), bid approval (owner). Mandatory-Unknown blocks bid readiness ("Needs confirmation").
   TODO(screen builder): GET /api/requests/{id} ; POST /api/requests/{id}/agreement ; POST /api/requests/{id}/candidates ;
   POST /api/requests/{id}/candidates/{cid}/send ; POST /api/requests/{id}/bids (→ needs_review → /approvals/:id). */
import { useParams } from "react-router-dom";
import { Scaffold } from "../scaffold";

export default function ImportRequestDetail() {
  const { id = "" } = useParams();
  return (
    <Scaffold
      title={`Request ${id}`}
      crumbs={[{ label: "Import requests", to: "/requests" }, { label: id }]}
      probe={`/api/requests/${encodeURIComponent(id)}`}
      emptyTitle="Request not found"
      emptyBody={`Nothing recorded for ${id} yet.`}
    />
  );
}
