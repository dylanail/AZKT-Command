/* Inbox: threads list + thread view; reply shows the unresolved check first, passed checks summarised,
   "Review & send" disabled with a reason until resolved; Take over pauses automation visibly; Resume revalidates.
   TODO(screen builder): GET /api/inbox/threads ; GET /api/inbox/threads/{id} ; POST /api/inbox/threads/{id}/draft ;
   POST /api/inbox/threads/{id}/send (→ needs_review) ; POST /api/inbox/threads/{id}/takeover|resume. */
import { useParams } from "react-router-dom";
import { Scaffold } from "../scaffold";

export default function Inbox() {
  const { threadId } = useParams();
  return (
    <Scaffold
      title="Inbox"
      subtitle={threadId ? `Thread ${threadId}` : "Customer threads. Drafts need approval before they send."}
      probe={threadId ? `/api/inbox/threads/${encodeURIComponent(threadId)}` : "/api/inbox/threads"}
      emptyTitle={threadId ? "Thread not found" : "No threads yet"}
      emptyBody={threadId ? undefined : "Connected mailboxes and messaging channels fill this list."}
      wide
    />
  );
}
