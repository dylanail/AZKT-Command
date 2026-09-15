/* Approval review as a full page (deep link target for email/Telegram and the phone view).
   GET /api/approvals/{id} · POST approve {expected_version} · POST decline {expected_version, note} ·
   POST edit {payload} → navigates to the new version. Desktop lists open the same content as a dialog
   through openApproval(id) (see useApprovalReview.tsx). */
import { useNavigate, useParams } from "react-router-dom";
import { ApiError } from "../../lib/api";
import { Button, EmptyState, ErrorState, GlassPanel, Loading, PageHeader } from "../../ui";
import { ApprovalReviewActions, ApprovalReviewBody, ApprovalStateLine } from "./ApprovalReviewBody";
import { useApprovalDetail } from "./useApprovalDetail";
import { kindLabel } from "./types";

export default function ApprovalReview() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const s = useApprovalDetail(id, { onNewVersion: (next) => nav(`/approvals/${next}`, { replace: true }) });
  const d = s.data;
  const denied = s.error instanceof ApiError && s.error.isDenied;
  const missing = s.error instanceof ApiError && s.error.isNotFound;

  return (
    <div className="page" style={{ maxWidth: 760 }}>
      <PageHeader
        title={d ? d.title : s.loading ? "Approval" : missing ? "Approval not found" : "Approval"}
        subtitle={d ? <span className="stack-sm" style={{ gap: 4 }}><span className="eyebrow">Approval · {kindLabel(d.kind)}</span><ApprovalStateLine d={d} /></span> : <span className="tnum">{id}</span>}
        crumbs={[{ label: "Home", to: "/" }, { label: "Approvals", to: "/approvals" }, { label: d ? `v${d.version}` : "Review" }]}
      />
      <GlassPanel padded>
        {s.loading ? <Loading label="Loading approval" rows={4} /> : denied ? (
          <EmptyState align="left" title="Only the owner reviews approvals" body="Ask the owner to open this one; it is waiting in their queue." action={<Button to="/" variant="soft" size="sm">Back to Home</Button>} />
        ) : missing ? (
          <EmptyState align="left" title="Nothing recorded for this approval" body={`${id} does not exist or was never created. Approvals list under Home › Needs your decision.`} action={<Button to="/approvals" variant="soft" size="sm">All approvals</Button>} />
        ) : s.error ? <ErrorState error={s.error} onRetry={s.reload} /> : <ApprovalReviewBody s={s} />}
      </GlassPanel>
      {d ? <ApprovalReviewActions s={s} /> : null}
    </div>
  );
}
