/* Candidate vehicle for an import request: facts vs requirements, Mandatory-Unknown → "Needs confirmation",
   send to buyer (timestamped), bid approval (owner).
   TODO(screen builder): GET /api/candidates/{id} ; POST /api/candidates/{id}/send ; POST /api/candidates/{id}/bid (→ needs_review). */
import { useParams } from "react-router-dom";
import { Scaffold } from "../scaffold";

export default function CandidateDetail() {
  const { id = "" } = useParams();
  return <Scaffold title={`Candidate ${id}`} crumbs={[{ label: "Import requests", to: "/requests" }, { label: id }]} probe={`/api/candidates/${encodeURIComponent(id)}`} emptyTitle="Candidate not found" emptyBody={`Nothing recorded for ${id} yet.`} />;
}
