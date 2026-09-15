/* Left pane: segmented filters with counts, mailbox selector, search, and the thread rows.
   GET /api/inbox/threads?filter=&account=&q=&limit=&offset= — paged with "Load older".
   Rows are distinguished by a bar plus a chip, never by colour alone. j/k move, Enter opens. */
import { useCallback, useEffect, useRef, type KeyboardEvent } from "react";
import { Button, Chip, EmptyState, ErrorState, GlassPanel, Input, Loading, SearchIcon, SegmentedControl, Select } from "../../../ui";
import { relativeTime } from "../../../lib/format";
import {
  CLASSIFICATION_LABELS, FILTER_EMPTY, FILTER_LABELS, LINK_KIND_LABELS, STATE_LABELS, THREAD_FILTERS,
  accountLabel, threadTitle, threadWho, type Conversation, type ThreadCounts, type ThreadFilter,
} from "../types";

export interface AccountOption { value: string; label: string }

/** State chips, in the order they matter for triage. */
function stateChips(c: Conversation) {
  const out: Array<{ key: string; label: string; tone: "risk" | "act" | "wait" | "blocked" | "soft" | "neutral" | "ok" }> = [];
  const s = c.state;
  if (s === "needs_reply") out.push({ key: "needs", label: "Needs reply", tone: "risk" });
  else if (s === "drafting") out.push({ key: "draft", label: "Draft ready", tone: "act" });
  else if (s === "blocked") out.push({ key: "blocked", label: "Draft blocked", tone: "blocked" });
  else if (s === "awaiting_approval") out.push({ key: "appr", label: "Waiting for approval", tone: "wait" });
  else if (s === "taken_over") out.push({ key: "taken", label: "Taken over", tone: "wait" });
  else if (s === "unmatched") out.push({ key: "unmatched", label: "Not matched", tone: "risk" });
  else if (s === "replied") out.push({ key: "replied", label: "Replied", tone: "ok" });
  else if (STATE_LABELS[s]) out.push({ key: "state", label: STATE_LABELS[s], tone: "soft" });
  if (c.classification === "spam") out.push({ key: "spam", label: "Suspected spam", tone: "soft" });
  const link = (c.links || [])[0];
  if (link) {
    out.push({
      key: "link",
      label: `${LINK_KIND_LABELS[link.kind] || link.kind}${link.match === "proposed" ? " (suggested)" : ""}`,
      tone: link.match === "proposed" ? "soft" : "neutral",
    });
  }
  return out;
}

function reasonLine(c: Conversation): string {
  const bits = [c.triage_reason, c.no_reply_reason, c.spam_reason, (c.classification_reasons || [])[0]];
  const first = bits.find((b) => b && b.trim());
  if (first) return first.trim();
  if (c.classification && CLASSIFICATION_LABELS[c.classification]) return CLASSIFICATION_LABELS[c.classification];
  return "";
}

function ThreadRow({ c, selected, contactName, onOpen }: {
  c: Conversation; selected: boolean; contactName: string | null; onOpen: () => void;
}) {
  const needsAttention = c.state === "needs_reply" || c.state === "unmatched" || c.state === "blocked";
  const who = contactName || threadWho(c) || "Unknown sender";
  const reason = reasonLine(c);
  // The opening of the newest message, quoted text already stripped by the server.
  const snippet = (c.snippet || c.last_message?.snippet || "").trim();
  const outbound = c.last_message?.direction === "out";
  const cls = ["ib-row", selected ? "ib-row--sel" : "", needsAttention ? "ib-row--attention" : ""].filter(Boolean).join(" ");
  return (
    <button type="button" className={cls} onClick={onOpen} data-thread-row={c.id}
      aria-current={selected ? "true" : undefined}
      aria-label={`${who}, ${threadTitle(c)}, ${STATE_LABELS[c.state] || c.state}`}>
      <span className="ib-row__bar" aria-hidden="true" />
      <span className="ib-row__main">
        <span className="ib-row__top">
          <span className="ib-row__who">{who}</span>
          <span className="ib-row__age tnum">{c.last_inbound_at ? relativeTime(c.last_inbound_at) : "no inbound"}</span>
        </span>
        <span className="ib-row__subject">{threadTitle(c)}</span>
        {snippet ? (
          <span className="ib-row__snippet">{outbound ? <span className="t4">You: </span> : null}{snippet}</span>
        ) : null}
        {reason ? <span className="ib-row__snippet ib-row__why">{reason}</span> : null}
        <span className="ib-row__chips">
          {stateChips(c).map((s) => <Chip key={s.key} size="sm" tone={s.tone}>{s.label}</Chip>)}
          <span className="ib-row__acct t4 fs12">{accountLabel(null, c.account)}</span>
        </span>
      </span>
    </button>
  );
}

export default function ThreadList({
  filter, onFilter, counts, account, accounts, onAccount, query, onQuery,
  items, loading, error, onReload, selectedId, onOpen, contactNames,
  total, hasMore, onMore, loadingMore, personalHint,
}: {
  filter: ThreadFilter;
  onFilter: (f: ThreadFilter) => void;
  counts: ThreadCounts;
  account: string;
  accounts: AccountOption[];
  onAccount: (v: string) => void;
  query: string;
  onQuery: (v: string) => void;
  items: Conversation[];
  loading: boolean;
  error: unknown;
  onReload: () => void;
  selectedId: string | null;
  onOpen: (id: string) => void;
  contactNames: Record<string, string>;
  total: number | null;
  hasMore: boolean;
  onMore: () => void;
  loadingMore: boolean;
  personalHint: boolean;
}) {
  const listRef = useRef<HTMLDivElement>(null);

  /** Row lookup without CSS.escape — ids are opaque and older WebViews lack it. */
  const rowEl = useCallback((id: string): HTMLElement | null => {
    const rows = listRef.current?.querySelectorAll<HTMLElement>("[data-thread-row]");
    if (!rows) return null;
    for (const el of Array.from(rows)) if (el.dataset.threadRow === id) return el;
    return null;
  }, []);

  const move = useCallback((delta: number) => {
    const idx = items.findIndex((i) => i.id === selectedId);
    const next = items[Math.max(0, Math.min(items.length - 1, (idx < 0 ? -1 : idx) + delta))];
    if (next) {
      onOpen(next.id);
      const el = rowEl(next.id);
      el?.scrollIntoView({ block: "nearest" });
      el?.focus({ preventScroll: true });
    }
  }, [items, selectedId, onOpen, rowEl]);

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "j" || e.key === "ArrowDown") { e.preventDefault(); move(1); }
    else if (e.key === "k" || e.key === "ArrowUp") { e.preventDefault(); move(-1); }
  };

  // Keep the selected row visible when the selection arrives from the URL.
  useEffect(() => {
    if (!selectedId) return;
    rowEl(selectedId)?.scrollIntoView({ block: "nearest" });
  }, [selectedId, rowEl]);

  return (
    <div className="ib-list stack-sm">
      <SegmentedControl<ThreadFilter>
        label="Inbox filter"
        size="sm"
        block
        value={filter}
        onChange={onFilter}
        options={THREAD_FILTERS.map((f) => ({ value: f, label: FILTER_LABELS[f], count: counts[f] }))}
      />
      <div className="ib-list__tools">
        {accounts.length > 1 ? (
          <Select aria-label="Mailbox" value={account} onChange={(e) => onAccount(e.target.value)}>
            {accounts.map((a) => <option key={a.value} value={a.value}>{a.label}</option>)}
          </Select>
        ) : null}
        <div className="ib-search">
          <SearchIcon />
          <Input aria-label="Search threads by subject or mailbox" value={query} placeholder="Search subject"
            onChange={(e) => onQuery(e.target.value)} />
        </div>
      </div>
      {personalHint ? <p className="fs12 t4" style={{ margin: 0 }}>Personal mail is only ever shown to the owner.</p> : null}

      <GlassPanel clip className="ib-list__panel">
        {loading && !items.length ? (
          <Loading label="Loading threads" rows={5} />
        ) : error ? (
          <ErrorState error={error} onRetry={onReload} />
        ) : !items.length ? (
          <EmptyState title={query ? "Nothing matches that search" : FILTER_LABELS[filter]} body={query ? "Try a shorter word from the subject." : FILTER_EMPTY[filter]} />
        ) : (
          <div ref={listRef} className="ib-rows" onKeyDown={onKey}>
            {items.map((c) => (
              <ThreadRow key={c.id} c={c} selected={c.id === selectedId}
                contactName={c.contact_id ? contactNames[c.contact_id] || null : null}
                onOpen={() => onOpen(c.id)} />
            ))}
          </div>
        )}
      </GlassPanel>

      {items.length ? (
        <div className="ib-list__foot">
          <span className="fs12 t4 tnum">{total === null ? `${items.length} shown` : `${items.length} of ${total} shown`}</span>
          <Button size="xs" variant="soft" onClick={onMore} loading={loadingMore} disabled={!hasMore}
            disabledReason="Everything in this filter is already listed.">Load older</Button>
        </div>
      ) : null}
    </div>
  );
}
