/* Coverage strip above the list (GET /api/inbox/coverage, spec §4.2 / F5 / H11).
   Per mailbox: connected + freshness, the window actually covered, unresolved gaps in plain words,
   catch-up progress, watch expiry and how many messages were deliberately excluded.
   Nothing connected → a calm setup notice with Connect and Paste a thread. Never claims all-clear. */
import { useState } from "react";
import { Link } from "react-router-dom";
import { Button, Chip, GlassPanel, Notice, Skeleton, StatusDot, When } from "../../../ui";
import { formatCount, relativeTime } from "../../../lib/format";
import type { CoverageAccount, CoverageGap, CoverageResp, Freshness } from "../types";

function dotTone(f: Freshness): "ok" | "risk" | "blocked" | "wait" | "muted" {
  switch (f.state) {
    case "ok": return "ok";
    case "warn": return "risk";
    case "degraded": return "wait";
    case "expired": return "blocked";
    default: return "muted";
  }
}

/** "12 messages between … and … are not synced yet" — from the gap window, never invented. */
function gapSentence(g: CoverageGap): string {
  const kind = (g.kind || "sync gap").replace(/_/g, " ");
  if (g.from && g.to) return `${kind}: messages between ${new Date(g.from).toLocaleString()} and ${new Date(g.to).toLocaleString()} are not synced yet.`;
  if (g.from) return `${kind}: messages after ${new Date(g.from).toLocaleString()} are not synced yet.`;
  return `${kind}: part of this mailbox is not synced yet.`;
}

function AccountCard({ a }: { a: CoverageAccount }) {
  const gaps = a.gaps || [];
  const excluded = a.excluded?.total || 0;
  const catchUp = a.catch_up || {};
  const watch = a.watch_expires_at ? new Date(a.watch_expires_at) : null;
  const watchSoon = !!watch && watch.getTime() - Date.now() < 48 * 3600 * 1000;
  return (
    <div className="ib-cov__card">
      <div className="ib-cov__top">
        <StatusDot tone={dotTone(a.freshness)} label={a.freshness.label} />
        <span className="ib-cov__name">{a.label}</span>
        {a.account_identity ? <span className="ib-cov__ident t4 fs12">{a.account_identity}</span> : null}
      </div>
      <div className="ib-cov__line t3 fs13">
        {a.connected ? a.freshness.label : "Not connected"}
        {a.freshness.last_success_at ? <> · last synced {relativeTime(a.freshness.last_success_at)}</> : null}
      </div>
      {a.connected && (a.coverage?.from || a.coverage?.to) ? (
        <div className="ib-cov__line t4 fs12 tnum">
          Covers {a.coverage.from ? <When iso={a.coverage.from} format="datetime" /> : "the start of the mailbox"}
          {" → "}
          {a.coverage.to ? <When iso={a.coverage.to} format="datetime" /> : "now"}
        </div>
      ) : null}
      {catchUp.pending ? (
        <div className="ib-cov__line fs12" style={{ color: "var(--wait)" }}>
          Catching up{catchUp.reason ? ` after ${catchUp.reason.replace(/_/g, " ")}` : ""}
          {typeof catchUp.processed === "number" ? ` · ${formatCount(catchUp.processed)} messages so far` : ""}. Numbers here are incomplete until it finishes.
        </div>
      ) : catchUp.completed_at ? (
        <div className="ib-cov__line t4 fs12">Catch-up finished {relativeTime(catchUp.completed_at)}{typeof catchUp.processed === "number" ? ` · ${formatCount(catchUp.processed)} messages` : ""}.</div>
      ) : null}
      {gaps.length ? (
        <ul className="ib-cov__gaps">
          {gaps.slice(0, 3).map((g, i) => (
            <li key={`${g.at || i}`}>{gapSentence(g)}{g.detail ? <span className="t3"> {g.detail}</span> : null}</li>
          ))}
          {gaps.length > 3 ? <li className="t3">and {gaps.length - 3} more unresolved gaps.</li> : null}
        </ul>
      ) : null}
      <div className="ib-cov__chips">
        {gaps.length ? <Chip size="sm" tone="risk">{gaps.length === 1 ? "1 gap not synced" : `${gaps.length} gaps not synced`}</Chip> : null}
        {excluded ? <Chip size="sm" tone="soft" title={Object.entries(a.excluded?.by_reason || {}).map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`).join(" · ")}>{formatCount(excluded)} not admitted</Chip> : null}
        {watchSoon ? <Chip size="sm" tone="risk">Mail watch ends {relativeTime(a.watch_expires_at)}</Chip> : null}
      </div>
    </div>
  );
}

export default function CoverageStrip({
  data, loading, error, onPaste, canDraft, draftReason,
}: {
  data: CoverageResp | null;
  loading: boolean;
  error: unknown;
  onPaste: () => void;
  canDraft: boolean;
  draftReason: string;
}) {
  const [open, setOpen] = useState(false);

  if (loading && !data) {
    return <GlassPanel padded className="ib-cov"><Skeleton height={16} width="42%" /><Skeleton height={12} width="72%" /></GlassPanel>;
  }
  // Coverage is context, not the screen: a failure here must not hide the threads.
  if (error || !data) {
    return (
      <Notice tone="risk" lead="Mailbox status unavailable">
        AZKT could not read how far the mailboxes are synced, so anything below may be incomplete.{" "}
        <Link to="/settings/connections">Check connections</Link>
      </Notice>
    );
  }

  const accounts = data.accounts || [];
  const connected = accounts.filter((a) => a.connected);

  if (!connected.length) {
    return (
      <Notice
        tone="neutral"
        lead="No mailbox is connected yet"
        action={
          <div className="row-wrap">
            <Button variant="primary" to="/settings/connections">Connect email</Button>
            <Button variant="soft" onClick={onPaste} disabled={!canDraft} disabledReason={draftReason}>Paste a thread</Button>
          </div>
        }
      >
        AZKT is not reading any mail, so this list stays empty. Connect the business mailbox, or paste a thread by hand
        to draft a reply in the meantime.
      </Notice>
    );
  }

  const gapCount = connected.reduce((n, a) => n + (a.gaps?.length || 0), 0);
  const stale = data.stale || [];
  const catching = connected.filter((a) => a.catch_up?.pending);
  const tone = gapCount || stale.length ? "risk" : "ok";
  const summary = gapCount
    ? `${gapCount === 1 ? "1 stretch of mail is" : `${gapCount} stretches of mail are`} not synced yet — this list is incomplete.`
    : stale.length
      ? `${stale.join(" · ")} ${stale.length === 1 ? "is" : "are"} behind, so newer mail may not be here yet.`
      : catching.length
        ? "Catching up on mail now — this list is still filling in."
        : "Mail is up to date for every connected mailbox.";

  return (
    <GlassPanel padded className="ib-cov" aria-label="Mailbox coverage">
      <div className="ib-cov__head">
        <StatusDot tone={tone === "ok" ? "ok" : "risk"} label={tone === "ok" ? "Up to date" : "Needs attention"} />
        <span className="ib-cov__summary">{summary}</span>
        <div className="ib-cov__headactions">
          <Button size="xs" variant="ghost" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
            {open ? "Hide mailbox detail" : "Mailbox detail"}
          </Button>
          <Button size="xs" variant="soft" onClick={onPaste} disabled={!canDraft} disabledReason={draftReason}>Paste a thread</Button>
        </div>
      </div>
      {open ? (
        <>
          <div className="ib-cov__grid">{accounts.map((a) => <AccountCard key={a.provider} a={a} />)}</div>
          <div className="fs12 t4">
            Coverage comes from the mailbox connection itself. <Link to="/settings/connections">Connections</Link>
          </div>
        </>
      ) : null}
    </GlassPanel>
  );
}
