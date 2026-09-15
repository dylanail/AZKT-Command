/* Inbox — /inbox and /inbox/:threadId (spec §2.3 Inbox row, §4 Email).
   Desktop: filters + list · thread · context + reply. Phone: list → thread → reply sheet, each with Back.
   Filter, mailbox, search and the open thread all live in the URL so Back returns to the same view.
   Every write goes through useCommand; nothing here ever claims a send happened. */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { can, whyNot } from "../../lib/perms";
import { useQuery } from "../../lib/useQuery";
import { useCommand } from "../../lib/useCommand";
import { useIsMobile, useMediaQuery } from "../../lib/viewport";
import { Button, EmptyState, ErrorState, GlassPanel, Loading, PageHeader, Sheet } from "../../ui";
import { ApiError } from "../../lib/api";
import CoverageStrip from "./components/CoverageStrip";
import ThreadList, { type AccountOption } from "./components/ThreadList";
import ThreadView from "./components/ThreadView";
import ContextPane from "./components/ContextPane";
import ReplyEditor from "./components/ReplyEditor";
import RecordPicker, { type PickedRecord, type PickerKind } from "./components/RecordPicker";
import { ClassifyDialog, ManualReplyDialog, PasteThreadDialog, ReasonDialog } from "./components/dialogs";
import { COVERAGE_PATH, actionPath, threadPath, type ThreadAction } from "./api";
import { useContactNames, useDebounced, useFilterCounts, useThreads } from "./useInboxData";
import {
  FILTER_LABELS, accountLabel, isThreadFilter, liveDraft, threadTitle,
  type CoverageResp, type RecordLink, type ThreadDetail, type ThreadFilter,
} from "./types";
import "../../styles/inbox.css";

type Prompt = "takeover" | "spam" | "not-spam" | "archive" | null;

export default function Inbox() {
  const { threadId = "" } = useParams();
  const nav = useNavigate();
  const { user } = useAuth();
  const mobile = useIsMobile();
  const threePane = useMediaQuery("(min-width: 1280px)");
  const { run, busy } = useCommand();
  const [params, setParams] = useSearchParams();

  const canRead = can(user, "inbox.read");
  const canDraft = can(user, "inbox.draft");
  const draftReason = whyNot("inbox.draft");
  const isOwner = user?.role === "owner";

  const filter: ThreadFilter = isThreadFilter(params.get("filter")) ? (params.get("filter") as ThreadFilter) : "needs_reply";
  const account = params.get("account") || "";
  const rawQuery = params.get("q") || "";
  const [queryInput, setQueryInput] = useState(rawQuery);
  useEffect(() => { setQueryInput(rawQuery); }, [rawQuery]);
  const query = useDebounced(queryInput, 300);

  // Keep the typed search in the URL (replace, so Back doesn't walk every keystroke).
  useEffect(() => {
    if (query === rawQuery) return;
    const p = new URLSearchParams(params);
    if (query) p.set("q", query); else p.delete("q");
    setParams(p, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);

  const setParam = useCallback((k: string, v: string | null) => {
    const p = new URLSearchParams(params);
    if (v) p.set(k, v); else p.delete(k);
    setParams(p, { replace: true });
  }, [params, setParams]);

  const search = params.toString() ? `?${params.toString()}` : "";
  const openThread = useCallback((id: string) => { nav(`/inbox/${encodeURIComponent(id)}${search}`); }, [nav, search]);
  const backToList = useCallback(() => { nav(`/inbox${search}`); }, [nav, search]);

  const [tick, setTick] = useState(0);
  const reloadAll = useCallback(() => setTick((t) => t + 1), []);

  const threads = useThreads(filter, account, query, tick);
  const counts = useFilterCounts(account, query, tick);
  const contactNames = useContactNames(canRead && can(user, "contacts.read"));

  const coverage = useQuery<CoverageResp | null>(
    (signal) => (canRead ? api.get<CoverageResp>(COVERAGE_PATH, { signal }) : Promise.resolve(null)),
    [canRead, tick],
  );

  const detail = useQuery<ThreadDetail | null>(
    (signal) => (threadId && canRead ? api.get<ThreadDetail>(threadPath(threadId), { signal }) : Promise.resolve(null)),
    [threadId, canRead, tick],
  );
  const d = detail.data;
  const conv = d?.conversation || null;

  /* ---------- mailbox options: what the threads actually carry, named by the connection ---------- */
  const accounts: AccountOption[] = useMemo(() => {
    const byValue = new Map<string, string>();
    for (const a of coverage.data?.accounts || []) {
      if (a.account_identity) byValue.set(a.account_identity, a.label || a.account_identity);
    }
    for (const t of threads.items) {
      if (t.account && !byValue.has(t.account)) byValue.set(t.account, t.account);
    }
    return [{ value: "", label: "All mailboxes" }, ...Array.from(byValue, ([value, label]) => ({ value, label }))];
  }, [coverage.data, threads.items]);

  /* ---------- dialogs ---------- */
  const [paste, setPaste] = useState(false);
  const [prompt, setPrompt] = useState<Prompt>(null);
  const [picker, setPicker] = useState(false);
  const [classify, setClassify] = useState(false);
  const [manual, setManual] = useState(false);
  const [unlinking, setUnlinking] = useState<RecordLink | null>(null);
  const [replySheet, setReplySheet] = useState(false);
  useEffect(() => { setReplySheet(false); }, [threadId]);

  /* ---------- Esc on a phone closes the thread ---------- */
  useEffect(() => {
    if (!mobile || !threadId) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const t = e.target as HTMLElement | null;
      if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
      backToList();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [mobile, threadId, backToList]);

  /* ---------- thread commands ---------- */
  const version = conv?.version ?? 0;
  const act = useCallback(async (key: string, action: ThreadAction, body: Record<string, unknown>, success: string) => {
    if (!conv) return false;
    const r = await run(key, actionPath(conv.id, action), { ...body, expected_version: version }, { success });
    if (r) { detail.reload(); reloadAll(); }
    return r?.status === "ok";
  }, [conv, run, version, detail, reloadAll]);

  const onPick = useCallback(async (p: PickedRecord) => {
    if (!conv) return false;
    const existing = (conv.links || [])[0];
    const body = p.kind === "contact"
      // The command always carries a record; a person is corrected alongside the record already linked.
      ? { kind: existing?.kind, id: existing?.id, contact_id: p.id, match: "matched", reason: `set to ${p.label} by a person` }
      : { kind: p.kind, id: p.id, match: "matched", reason: `linked to ${p.label} by a person` };
    return act("link", "link", body, p.kind === "contact" ? `Person set to ${p.label}.` : `Linked to ${p.label}.`);
  }, [conv, act]);

  const linkKinds: PickerKind[] = (conv?.links || []).length
    ? ["vehicle", "opportunity", "import_request", "contact"]
    : ["vehicle", "opportunity", "import_request"];
  const linkNote = (conv?.links || []).length
    ? undefined
    : "Once a vehicle, lead or request is linked you can also correct which person the thread is with.";

  /* ---------- denied / loading shells ---------- */
  if (!canRead) {
    return (
      <div className="page">
        <PageHeader title="Inbox" subtitle="Customer threads and the replies waiting on you." />
        <GlassPanel clip>
          <EmptyState title="The inbox isn't open to your role" body="Customer messages are limited to the people who handle them. Ask the owner if you need access." />
        </GlassPanel>
      </div>
    );
  }

  const detailDenied = detail.error instanceof ApiError && (detail.error.isDenied || detail.error.isNotFound);

  const listPane = (
    <ThreadList
      filter={filter}
      onFilter={(f) => setParam("filter", f === "needs_reply" ? null : f)}
      counts={counts}
      account={account}
      accounts={accounts}
      onAccount={(v) => setParam("account", v || null)}
      query={queryInput}
      onQuery={setQueryInput}
      items={threads.items}
      loading={threads.loading}
      error={threads.error}
      onReload={threads.reload}
      selectedId={threadId || null}
      onOpen={openThread}
      contactNames={contactNames}
      hasMore={threads.hasMore}
      onMore={threads.loadMore}
      loadingMore={threads.loadingMore}
      personalHint={isOwner}
    />
  );

  const threadPane = !threadId ? (
    <GlassPanel clip className="ib-placeholder">
      <EmptyState title="Pick a thread" body={`${FILTER_LABELS[filter]} is showing on the left. Open one to read it and draft a reply.`} />
    </GlassPanel>
  ) : detail.loading && !d ? (
    <GlassPanel padded><Loading label="Loading thread" rows={5} /></GlassPanel>
  ) : detailDenied ? (
    <GlassPanel clip>
      <EmptyState title="This thread isn't available to you"
        body="It may belong to the personal mailbox, or to a vehicle outside your access. Nothing is hidden by accident — ask the owner if you need it."
        action={<Button variant="soft" onClick={backToList}>Back to the list</Button>} />
    </GlassPanel>
  ) : detail.error ? (
    <ErrorState error={detail.error} onRetry={detail.reload} />
  ) : d ? (
    <ThreadView
      detail={d}
      onTakeOver={() => setPrompt("takeover")}
      onResume={() => void act("resume", "resume", { regenerate: true }, "Resumed. AZKT reloaded the thread and checked it again.")}
      busyTakeOver={busy("take-over")}
      busyResume={busy("resume")}
      canDraft={canDraft}
      draftReason={draftReason}
    />
  ) : null;

  const contextPane = d ? (
    <div className="stack-sm">
      <ContextPane
        detail={d}
        canDraft={canDraft}
        draftReason={draftReason}
        onLink={() => setPicker(true)}
        onUnlink={(l) => setUnlinking(l)}
        onSpam={() => setPrompt("spam")}
        onNotSpam={() => setPrompt("not-spam")}
        onArchive={() => setPrompt("archive")}
        onClassify={() => setClassify(true)}
        onManualReply={() => setManual(true)}
        busyKey={busy}
      />
      {mobile ? (
        <Button variant="primary" size="xl" block onClick={() => setReplySheet(true)}>
          {liveDraft(d.drafts) ? "Open the reply" : "Prepare a reply"}
        </Button>
      ) : (
        <ReplyEditor detail={d} canDraft={canDraft} draftReason={draftReason} onChanged={() => { detail.reload(); reloadAll(); }} mobile={mobile} />
      )}
    </div>
  ) : null;

  const subtitle = threads.loading
    ? "Loading threads…"
    : `${threads.items.length}${threads.hasMore ? "+" : ""} in ${FILTER_LABELS[filter].toLowerCase()}${account ? ` · ${accounts.find((a) => a.value === account)?.label || account}` : ""} · replies are sent only after the owner approves`;

  return (
    <div className="page page-wide ib-page">
      {mobile && threadId ? (
        <PageHeader
          title={threadTitle(conv)}
          subtitle={conv ? `${accountLabel(d?.connection?.provider, conv.account)}${conv.last_inbound_at ? "" : " · no inbound message"}` : undefined}
          crumbs={[{ label: "Inbox", to: `/inbox${search}` }]}
          actions={<Button variant="soft" onClick={backToList}>Back</Button>}
        />
      ) : (
        <PageHeader title="Inbox" subtitle={subtitle} />
      )}

      {!(mobile && threadId) ? (
        <CoverageStrip
          data={coverage.data}
          loading={coverage.loading}
          error={coverage.error}
          onPaste={() => setPaste(true)}
          canDraft={canDraft}
          draftReason={draftReason}
        />
      ) : null}

      {mobile ? (
        threadId ? <div className="stack-sm">{threadPane}{contextPane}</div> : listPane
      ) : (
        <div className={["ib", threePane ? "ib--three" : "ib--two"].join(" ")}>
          <div className="ib__col ib__col--list">{listPane}</div>
          <div className="ib__col ib__col--thread">
            {threadPane}
            {!threePane ? contextPane : null}
          </div>
          {threePane ? <div className="ib__col ib__col--side">{contextPane}</div> : null}
        </div>
      )}

      {/* ---------- dialogs ---------- */}
      <PasteThreadDialog open={paste} onClose={() => setPaste(false)} mobile={mobile}
        onCreated={(id) => { reloadAll(); openThread(id); }} />

      <ReasonDialog
        open={prompt === "takeover"} mobile={mobile} onClose={() => setPrompt(null)}
        title="Take this thread over"
        description="AZKT stops drafting and sending here until you resume. Any unsent draft and its approval are cancelled."
        label="Why (optional)" placeholder="Calling them instead" confirmLabel="Take over"
        onConfirm={(note) => act("take-over", "take-over", { note }, "You have this thread. AZKT will not draft or send here.")}
      />
      <ReasonDialog
        open={prompt === "spam"} mobile={mobile} onClose={() => setPrompt(null)}
        title="Mark as suspected spam" description="Reversible. Nothing is deleted from the mailbox."
        label="Why (optional)" placeholder="Cold sales pitch" confirmLabel="Mark as spam" tone="danger"
        onConfirm={(reason) => act("spam", "spam", { reason }, "Filed as suspected spam.")}
      />
      <ReasonDialog
        open={prompt === "not-spam"} mobile={mobile} onClose={() => setPrompt(null)}
        title="Not spam" description="The thread goes back to where it was before it was filed."
        label="Why (optional)" placeholder="This is a real buyer" confirmLabel="Not spam"
        onConfirm={(reason) => act("not-spam", "not-spam", { reason }, "Put back. AZKT will treat it normally again.")}
      />
      <ReasonDialog
        open={prompt === "archive"} mobile={mobile} onClose={() => setPrompt(null)}
        title="Archive in AZKT" description="It leaves your lists here. The message stays in the mailbox untouched."
        label="Why (optional)" placeholder="Handled on the phone" confirmLabel="Archive"
        onConfirm={(reason) => act("archive", "archive", { reason, also_in_gmail: false }, "Archived in AZKT.")}
      />
      <ReasonDialog
        open={!!unlinking} mobile={mobile} onClose={() => setUnlinking(null)}
        title="Remove this link" description="Any unsent draft that relied on it goes back for review."
        label="Why (optional)" confirmLabel="Unlink" tone="danger"
        onConfirm={async (reason) => {
          const l = unlinking;
          if (!l) return false;
          return act(`unlink:${l.kind}:${l.id}`, "unlink", { kind: l.kind, id: l.id, reason }, "Link removed.");
        }}
      />

      {conv ? (
        <>
          <RecordPicker open={picker} onClose={() => setPicker(false)} onPick={onPick} mobile={mobile}
            busy={busy("link")} kinds={linkKinds} note={linkNote} />
          <ClassifyDialog conversationId={conv.id} open={classify} onClose={() => setClassify(false)} mobile={mobile}
            current={conv.classification} version={conv.version} onSaved={() => { detail.reload(); reloadAll(); }} />
          <ManualReplyDialog conversationId={conv.id} open={manual} onClose={() => setManual(false)} mobile={mobile}
            defaultTo={(conv.participants || []).filter((p) => p && p !== conv.account)}
            defaultSubject={conv.subject || ""}
            onSaved={() => { detail.reload(); reloadAll(); }} />
        </>
      ) : null}

      {mobile && d ? (
        <Sheet open={replySheet} onClose={() => setReplySheet(false)} title="Reply"
          footer={<Button variant="soft" block onClick={() => setReplySheet(false)}>Back to the thread</Button>}>
          <ReplyEditor detail={d} canDraft={canDraft} draftReason={draftReason}
            onChanged={() => { detail.reload(); reloadAll(); }} mobile />
        </Sheet>
      ) : null}
    </div>
  );
}
