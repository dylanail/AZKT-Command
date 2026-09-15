/* Every version of the reply for this thread (GET /api/drafts/{draft_id}/versions).
   Restoring does not rewind anything: it writes that wording into a new version through reply.edit,
   so the history stays complete and the checks run again. */
import { useState } from "react";
import { api } from "../../../lib/api";
import { useQuery } from "../../../lib/useQuery";
import { Button, Chip, EmptyState, ErrorState, Loading, ResponsiveDialog, When } from "../../../ui";
import { TZ } from "../../../lib/format";
import { draftVersionsPath } from "../api";
import type { Draft, DraftVersionsResp } from "../types";

const STATUS_LABELS: Record<string, string> = {
  draft: "Draft", blocked: "Blocked", pending_approval: "Waiting for approval", approved: "Approved",
  sending: "Being sent", sent: "Sent", invalidated: "Out of date", superseded: "Replaced", declined: "Declined",
};

export default function DraftVersions({
  open, onClose, draftId, currentId, mobile, canDraft, draftReason, restoring, onRestore,
}: {
  open: boolean;
  onClose: () => void;
  draftId: string;
  currentId: string;
  mobile: boolean;
  canDraft: boolean;
  draftReason: string;
  restoring: boolean;
  onRestore: (v: Draft) => Promise<boolean>;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const q = useQuery<DraftVersionsResp | null>(
    (signal) => (open ? api.get<DraftVersionsResp | null>(draftVersionsPath(draftId), { signal, tolerate: [403, 404] }) : Promise.resolve(null)),
    [open, draftId],
  );
  const items = q.data?.items || [];

  return (
    <ResponsiveDialog
      mobile={mobile}
      open={open}
      onClose={onClose}
      title="Draft versions"
      description="Each edit becomes a new version. Nothing is overwritten."
      size="lg"
      align="top"
      footer={<Button variant="ghost" onClick={onClose}>Close</Button>}
    >
      {q.loading ? <Loading label="Loading versions" rows={3} />
        : q.error ? <ErrorState error={q.error} onRetry={q.reload} />
        : !items.length ? <EmptyState title="No versions yet" body="The first draft appears here once it is prepared." />
        : (
          <div className="stack-sm">
            {items.map((v) => {
              const isCurrent = v.id === currentId;
              const shown = expanded === v.id;
              return (
                <div key={v.id} className="ib-ver">
                  <div className="ib-ver__head">
                    <span className="ib-ver__num">Version {v.draft_version}</span>
                    <Chip size="sm" tone={v.status === "sent" ? "ok" : v.status === "blocked" || v.status === "declined" ? "blocked" : isCurrent ? "act" : "soft"}>
                      {isCurrent ? "Working draft" : STATUS_LABELS[v.status] || v.status}
                    </Chip>
                    <span className="t4 fs12 tnum">
                      {v.updated_at ? <When iso={v.updated_at} tz={TZ.phoenix} format="long" /> : null}
                      {v.generator ? ` · ${v.generator}` : ""}
                    </span>
                  </div>
                  {v.invalidated_reason ? <div className="fs12" style={{ color: "var(--risk)" }}>{v.invalidated_reason}</div> : null}
                  {shown ? <pre className="ib-ver__body">{v.body || "(empty)"}</pre> : <div className="ib-ver__snip t3 fs13">{(v.body || "").slice(0, 160) || "(empty)"}</div>}
                  <div className="row-wrap">
                    <Button size="xs" variant="ghost" onClick={() => setExpanded(shown ? null : v.id)}>
                      {shown ? "Hide wording" : "Show wording"}
                    </Button>
                    <Button size="xs" variant="soft" loading={restoring}
                      onClick={async () => { const ok = await onRestore(v); if (ok) onClose(); }}
                      disabled={!canDraft || isCurrent || !v.body}
                      disabledReason={!canDraft ? draftReason : isCurrent ? "This is already the working draft." : "This version has no wording to restore."}>
                      Use this wording
                    </Button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
    </ResponsiveDialog>
  );
}
