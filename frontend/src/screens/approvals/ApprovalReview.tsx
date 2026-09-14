/* Approval review: exact payload, recipient, attachment, scope, checks; "Sources and technical details" expander;
   Approve & send → Awaiting execution → Confirmed (receipt). Edit creates a new version and invalidates the old one.
   TODO(screen builder): GET /api/approvals/{id} ; POST /api/approvals/{id}/approve {expected_version} ;
   POST /api/approvals/{id}/decline ; POST /api/approvals/{id}/edit {body} (→ new version) ; poll receipt. */
import { useParams } from "react-router-dom";
import { useAuth } from "../../lib/auth";
import { api } from "../../lib/api";
import { useQuery } from "../../lib/useQuery";
import { Button, EmptyState, ErrorState, Expander, GlassPanel, KeyValues, Loading, PageHeader } from "../../ui";

export default function ApprovalReview() {
  const { id = "" } = useParams();
  const { user } = useAuth();
  const q = useQuery<Record<string, unknown> | null>((signal) => api.get("/api/approvals/" + encodeURIComponent(id), { signal, tolerate: [404, 501] }), [id]);
  const owner = user?.role === "owner";
  return (
    <div className="page page-narrow" style={{ maxWidth: 720 }}>
      <PageHeader title="Approval" subtitle={<span className="tnum">{id}</span>} crumbs={[{ label: "Home", to: "/" }, { label: "Approval" }]} />
      <GlassPanel clip>
        {q.loading ? <Loading label="Loading approval" rows={3} /> : q.error ? <ErrorState error={q.error} onRetry={q.reload} /> : !q.data ? (
          <EmptyState title="Approval not found" body={`Nothing recorded for ${id}. Approvals list on Home under "Needs your decision".`} action={<Button to="/" size="sm" variant="soft">Back to Home</Button>} />
        ) : (
          <div className="stack" style={{ padding: 18 }}>
            <KeyValues items={[["To", "Not recorded"], ["Subject", "Not recorded"], ["Scope", "Not recorded"]]} />
            <Expander><KeyValues items={[["Sources", "Not recorded"], ["Payload", `${id}`], ["Authority", "Owner approval required."]]} /></Expander>
          </div>
        )}
      </GlassPanel>
      <div className="row-wrap">
        <Button variant="primary" disabled disabledReason={owner ? "Approval actions connect with the approval data." : "Only the owner approves."}>Approve &amp; send</Button>
        <Button variant="glass" disabled disabledReason="Editing creates a new version once approvals load.">Edit</Button>
        <Button variant="ghost" disabled disabledReason={owner ? "Approval actions connect with the approval data." : "Only the owner declines."}>Decline</Button>
      </div>
    </div>
  );
}
