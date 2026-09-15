/* Modal helper for "Needs your decision" lists. `openApproval(id)` opens the exact review as a Dialog
   over the current page on desktop and navigates to the full page (/approvals/:id) on phones.
   Mount <ApprovalReviewHost /> once in the shell. Works from any component (Home, Inbox, toasts). */
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useIsMobile } from "../../lib/viewport";
import { registerReviewOpener } from "../../lib/useCommand";
import { Dialog, ErrorState, Loading } from "../../ui";
import { ApprovalReviewActions, ApprovalReviewBody, ApprovalStateLine } from "./ApprovalReviewBody";
import { useApprovalDetail } from "./useApprovalDetail";
import { kindLabel } from "./types";

type Listener = (id: string | null) => void;
const listeners = new Set<Listener>();
let current: string | null = null;

export function openApproval(id: string) {
  current = id;
  listeners.forEach((l) => l(id));
}
export function closeApproval() {
  current = null;
  listeners.forEach((l) => l(null));
}
export function useApprovalReview() {
  return { openApproval, closeApproval };
}

function HostDialog({ id, onSwap, onClose }: { id: string; onSwap: (id: string) => void; onClose: () => void }) {
  const s = useApprovalDetail(id, { onNewVersion: onSwap });
  const d = s.data;
  return (
    <Dialog
      open
      onClose={onClose}
      size="lg"
      align="top"
      eyebrow={d ? `Approval · ${kindLabel(d.kind)}` : "Approval"}
      title={d ? d.title : s.loading ? "Loading approval…" : "Approval"}
      description={d ? <ApprovalStateLine d={d} /> : undefined}
      footer={d ? <ApprovalReviewActions s={s} onClose={onClose} /> : undefined}
      label="Approval review"
    >
      {s.loading ? <Loading label="Loading approval" rows={4} /> : s.error ? <ErrorState error={s.error} onRetry={s.reload} /> : <ApprovalReviewBody s={s} />}
    </Dialog>
  );
}

/** Renders the dialog when something called openApproval(). One per app. */
export function ApprovalReviewHost() {
  const [id, setId] = useState<string | null>(current);
  const isMobile = useIsMobile();
  const nav = useNavigate();

  useEffect(() => { registerReviewOpener(openApproval); return () => registerReviewOpener(null); }, []);
  useEffect(() => {
    const l: Listener = (v) => {
      if (v && isMobile) { current = null; setId(null); nav(`/approvals/${v}`); return; }
      setId(v);
    };
    listeners.add(l);
    return () => { listeners.delete(l); };
  }, [isMobile, nav]);

  const close = useCallback(() => closeApproval(), []);
  const swap = useCallback((next: string) => { current = next; setId(next); }, []);
  if (!id) return null;
  return <HostDialog key={id} id={id} onSwap={swap} onClose={close} />;
}
