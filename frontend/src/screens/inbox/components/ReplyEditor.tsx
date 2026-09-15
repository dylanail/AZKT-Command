/* Right pane (bottom): the reply. Account and recipients stay visible above the editor; the body is
   editable with an unsent copy kept in sessionStorage; Save posts reply.edit with expected_version;
   Review & send posts reply.submit_for_approval and is disabled with the server's exact reason until
   every blocking check passes. After approval the thread says truthfully what happened to the send. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button, Chip, Expander, Field, GlassPanel, Notice, NotRecorded, Textarea, When } from "../../../ui";
import { TZ } from "../../../lib/format";
import { useCommand } from "../../../lib/useCommand";
import { openApproval } from "../../approvals/useApprovalReview";
import { AnswerPlan, ChecksPanel } from "./ChecksPanel";
import DraftVersions from "./DraftVersions";
import { actionPath, clearCachedBody, draftCacheKey, readCachedBody, writeCachedBody } from "../api";
import { accountLabel, blockingFailures, liveDraft, sendStateOf, type Draft, type ThreadDetail } from "../types";

const SEND_COPY = "Sends after the owner approves. Nothing leaves AZKT until then.";

function Recipients({ draft, detail }: { draft: Draft | null; detail: ThreadDetail }) {
  const c = detail.conversation;
  const from = draft?.facts?.account?.identity || detail.connection?.account_identity || c.account;
  const to = draft?.to?.length ? draft.to : [];
  const cc = draft?.cc || [];
  return (
    <div className="ib-reply__addr">
      <div><span className="fs12 t3">From</span> <span>{accountLabel(detail.connection?.provider, c.account)}{from ? ` · ${from}` : ""}</span></div>
      <div>
        <span className="fs12 t3">To</span>{" "}
        {to.length ? <span>{to.join(", ")}</span> : <NotRecorded text="set when a draft is prepared" />}
        {cc.length ? <span className="t3"> · CC {cc.join(", ")}</span> : null}
      </div>
    </div>
  );
}

function SendStatus({ draft, onReconcile, busy, canDraft, draftReason }: {
  draft: Draft; onReconcile: () => void; busy: boolean; canDraft: boolean; draftReason: string;
}) {
  const state = sendStateOf(draft);
  const receiptId = (draft.receipt || {})["message_id"];
  if (state === "sent") {
    return (
      <Notice tone="ok" role="status" lead="Sent">
        Delivered {draft.sent_at ? <When iso={draft.sent_at} tz={TZ.phoenix} format="long" /> : "at a time the mailbox did not record"}.
        {typeof receiptId === "string" ? <span className="fs12 t4"> Mailbox receipt {receiptId.slice(0, 16)}</span> : null}
      </Notice>
    );
  }
  if (state === "running") {
    return (
      <Notice
        tone="wait"
        role="status"
        lead="Approved — the result is not confirmed yet"
        action={<Button variant="soft" onClick={onReconcile} loading={busy} disabled={!canDraft} disabledReason={draftReason}>Check what happened</Button>}
      >
        AZKT handed this to the mailbox but has not seen a receipt. Checking searches Sent for this exact message
        instead of sending again, so it can never go out twice.
      </Notice>
    );
  }
  if (state === "awaiting_approval") {
    return (
      <Notice
        tone="wait"
        role="status"
        lead="Waiting for the owner to approve"
        action={draft.approval_id ? <Button variant="primary" onClick={() => openApproval(draft.approval_id as string)}>Review</Button> : undefined}
      >
        The approval is bound to this exact wording. Editing the draft cancels it and a new review is needed.
      </Notice>
    );
  }
  if (state === "declined") {
    return <Notice tone="blocked" lead="Declined">{draft.invalidated_reason || "The owner declined this reply. Edit it and ask again."}</Notice>;
  }
  if (state === "stale") {
    return (
      <Notice tone="risk" lead="This draft is out of date">
        {draft.invalidated_reason || "Something the draft relied on changed."} Prepare a new reply so the facts are current.
      </Notice>
    );
  }
  if (state === "blocked" && draft.blocked_reason) {
    return <Notice tone="blocked" lead="Blocked">{draft.blocked_reason}</Notice>;
  }
  return null;
}

export default function ReplyEditor({ detail, canDraft, draftReason, onChanged, mobile }: {
  detail: ThreadDetail;
  canDraft: boolean;
  draftReason: string;
  onChanged: () => void;
  mobile: boolean;
}) {
  const { run, busy } = useCommand();
  const c = detail.conversation;
  const draft = useMemo(() => liveDraft(detail.drafts), [detail.drafts]);
  const cacheKey = draft ? draftCacheKey(c.id, draft.id) : "";
  const serverBody = draft?.body || "";

  const [body, setBody] = useState(serverBody);
  const [versionsOpen, setVersionsOpen] = useState(false);
  const bodyRef = useRef<HTMLTextAreaElement>(null);

  // Load the server body, preferring an unsent local copy for this exact draft version.
  useEffect(() => {
    if (!draft) { setBody(""); return; }
    const cached = readCachedBody(cacheKey);
    setBody(cached !== null && cached !== serverBody ? cached : serverBody);
  }, [draft?.id, draft?.version, cacheKey, serverBody, draft]);

  const dirty = !!draft && body !== serverBody;
  useEffect(() => {
    if (!draft) return;
    if (dirty) writeCachedBody(cacheKey, body); else clearCachedBody(cacheKey);
  }, [dirty, body, cacheKey, draft]);

  const hasInbound = (detail.messages || []).some((m) => m.direction === "in");
  const takenOver = detail.takeover.state;
  const failing = blockingFailures(draft);
  const sendState = sendStateOf(draft);
  const locked = sendState === "sent" || sendState === "running" || sendState === "awaiting_approval";

  const prepareReason = !canDraft ? draftReason
    : takenOver ? "Resume the thread first — a person has taken it over."
    : detail.takeover.paused ? "Automation is paused for this thread. Resume it first."
    : !hasInbound ? "There is no message to reply to yet."
    : "";

  const prepare = useCallback(async () => {
    const r = await run("prepare", actionPath(c.id, "prepare"), { reason: "prepare", use_model: true },
      { success: "Draft ready. Check it before it goes anywhere." });
    if (r) onChanged();
  }, [run, c.id, onChanged]);

  const save = useCallback(async () => {
    if (!draft) return;
    const r = await run("edit", actionPath(c.id, "edit"), {
      draft_id: draft.id, body, expected_version: draft.version, note: "edited in the inbox",
    }, { success: "Saved. The checks ran again on the new wording." });
    if (r?.status === "ok") { clearCachedBody(cacheKey); onChanged(); } else if (r === null) { onChanged(); }
  }, [run, draft, body, c.id, cacheKey, onChanged]);

  const restore = useCallback(async (v: Draft) => {
    if (!draft) return false;
    const r = await run("restore", actionPath(c.id, "edit"), {
      draft_id: draft.id, body: v.body || "", subject: v.subject || undefined, expected_version: draft.version,
      note: `restored wording from version ${v.draft_version}`,
    }, { success: `Version ${v.draft_version} is now the working draft.` });
    if (r?.status === "ok") { clearCachedBody(cacheKey); onChanged(); return true; }
    return false;
  }, [run, draft, c.id, cacheKey, onChanged]);

  const submit = useCallback(async () => {
    if (!draft) return;
    const r = await run("submit", actionPath(c.id, "submit"), { draft_id: draft.id, expected_version: draft.version },
      { success: "Queued to send. AZKT will record the real receipt." });
    if (r) onChanged();
  }, [run, draft, c.id, onChanged]);

  const reconcile = useCallback(async () => {
    if (!draft) return;
    const r = await run<{ state?: string; reconciled?: boolean; reason?: string }>(
      "reconcile", actionPath(c.id, "reconcile"), { draft_id: draft.id },
      { success: "Checked with the mailbox." });
    if (r) onChanged();
  }, [run, draft, c.id, onChanged]);

  const submitReason = !canDraft ? draftReason
    : !draft ? "Prepare a reply first."
    : locked ? (sendState === "sent" ? "This reply has already been sent." : "This reply is already waiting on the owner.")
    : dirty ? "Save your changes first so the review matches what you wrote."
    : failing.length ? (failing[0].remediation || failing[0].label)
    : "";

  return (
    <GlassPanel padded className="stack-sm ib-reply" aria-label="Reply">
      <div className="ib-reply__head">
        <h3 className="ib-context__title">Reply</h3>
        {draft ? <Chip size="sm" tone="soft">Version {draft.draft_version}{draft.generator ? ` · ${draft.generator}` : ""}</Chip> : null}
      </div>

      <Recipients draft={draft} detail={detail} />

      {draft ? <SendStatus draft={draft} onReconcile={() => void reconcile()} busy={busy("reconcile")} canDraft={canDraft} draftReason={draftReason} /> : null}

      {!draft ? (
        <div className="stack-sm">
          <p className="fs13 t3" style={{ margin: 0 }}>
            No reply has been drafted for this thread yet. AZKT reads the last message, pulls the current facts and
            writes a draft you can edit — it never sends on its own.
          </p>
          <div className="row-wrap">
            <Button variant="primary" size="lg" onClick={() => void prepare()} loading={busy("prepare")}
              disabled={!!prepareReason} disabledReason={prepareReason}>Prepare reply</Button>
          </div>
          {prepareReason ? <p className="fs12 t4" style={{ margin: 0 }}>{prepareReason}</p> : null}
        </div>
      ) : (
        <>
          <Field
            label="Draft"
            hint={dirty ? "Not saved yet — kept on this device until you save." : "Edited wording is checked again when you save."}
            aside={<button type="button" className="linklike fs13" onClick={() => setVersionsOpen(true)}>Versions</button>}
          >
            <Textarea ref={bodyRef} rows={mobile ? 10 : 14} value={body} onChange={(e) => setBody(e.target.value)}
              readOnly={locked} aria-readonly={locked}
              placeholder="The draft body." />
          </Field>

          <div className="row-wrap ib-reply__actions">
            <Button variant="soft" onClick={() => void save()} loading={busy("edit")}
              disabled={!canDraft || !dirty || locked}
              disabledReason={!canDraft ? draftReason : locked ? "This reply is already on its way." : "Nothing has changed yet."}>
              Save
            </Button>
            <Button variant="ghost" onClick={() => { setBody(serverBody); clearCachedBody(cacheKey); }}
              disabled={!dirty} disabledReason="Nothing has changed yet.">Discard changes</Button>
            <Button variant="primary" size="lg" onClick={() => void submit()} loading={busy("submit")}
              disabled={!!submitReason} disabledReason={submitReason}>Review &amp; send</Button>
          </div>
          <p className="fs12 t4" style={{ margin: 0 }}>{SEND_COPY}</p>

          <div className="stack-sm">
            <h4 className="ib-reply__sub">Checks</h4>
            <ChecksPanel checks={draft.checks || []} />
          </div>

          <Expander title="What they asked">
            <AnswerPlan plan={draft.answer_plan || []} />
          </Expander>

          <Expander title="What this draft is based on">
            {draft.sources?.length ? (
              <ul className="ib-sources">
                {draft.sources.map((s, i) => {
                  const kind = typeof s.kind === "string" ? s.kind : "source";
                  const id = typeof s.id === "string" ? s.id : "";
                  const field = typeof s.field === "string" ? s.field : "";
                  return <li key={`${kind}-${id}-${i}`}>{kind.replace(/_/g, " ")}{field ? ` · ${field}` : ""}{id ? ` · ${id.slice(0, 8)}` : ""}</li>;
                })}
              </ul>
            ) : <NotRecorded text="No sources were attached to this draft." />}
            {draft.facts?.money_hidden ? <p className="fs12 t4" style={{ margin: "6px 0 0" }}>Prices are hidden for your role, so any amount in this draft cannot be checked here.</p> : null}
          </Expander>

          <DraftVersions
            open={versionsOpen}
            onClose={() => setVersionsOpen(false)}
            draftId={draft.id}
            currentId={draft.id}
            mobile={mobile}
            canDraft={canDraft}
            draftReason={draftReason}
            restoring={busy("restore")}
            onRestore={restore}
          />
        </>
      )}
    </GlassPanel>
  );
}
