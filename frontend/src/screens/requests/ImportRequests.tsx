/* Import requests: list with stages; New creates a quick request.
   TODO(screen builder): GET /api/requests?q= ; POST /api/requests ; counts per stage. */
import { useSearchParams } from "react-router-dom";
import { Scaffold } from "../scaffold";

export default function ImportRequests() {
  const [params] = useSearchParams();
  const q = params.get("q") || "";
  return (
    <Scaffold
      title="Import requests"
      subtitle={q ? `Search: "${q}"` : "Buyer requirements, agreement, deposit, candidates and bids."}
      probe={`/api/requests${q ? `?q=${encodeURIComponent(q)}` : ""}`}
      emptyTitle={q ? "No requests match" : "No import requests yet"}
      emptyBody={q ? undefined : "Requests start from a buyer's enquiry: what they must have, prefer and avoid."}
    />
  );
}
