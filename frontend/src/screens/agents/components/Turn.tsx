/* One turn of the conversation. Web and Telegram share the thread, so every turn says which channel it
   came from. Everything else on the card is what the server actually returned: the tools it ran, the
   approvals it prepared, the question it still needs answered, and the records it changed. */
import { Link } from "react-router-dom";
import { entityHref, entityLabel, humanize, shortId } from "../../../lib/links";
import { Button, Chip, When } from "../../../ui";
import { openApproval } from "../../approvals/useApprovalReview";
import { describeContext, contextHref } from "../context";
import type { ChatMessage, ChangedRef, NeededInput } from "../types";
import { ROLE_META, type AgentRole } from "../types";

const CHANNEL_LABEL: Record<string, string> = { web: "Web", telegram: "Telegram", mcp: "Connector", job: "Scheduled" };

const TOOL_RESULT: Record<string, string> = {
  ok: "done",
  needs_review: "waiting for your review",
  blocked: "blocked",
  error: "failed",
  running: "running",
};

function changedLabel(c: ChangedRef): string {
  const kind = humanize(entityLabel(c.kind));
  if (c.label) return `${kind}: ${c.label}`;
  return c.id ? `${kind} ${shortId(c.id, 6)}` : kind;
}

function NeededInputCard({ n }: { n: NeededInput }) {
  const reasons = (n.reasons || []).filter(Boolean);
  const candidates = (n.candidates || []).filter(Boolean);
  return (
    <div className="ag-card ag-card--ask">
      <div className="ag-card__title">It needs one answer from you</div>
      {n.question ? <div className="ag-card__body">{n.question}</div> : null}
      {reasons.length ? (
        <ul className="ag-card__list">{reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
      ) : null}
      {candidates.length ? (
        <div className="ag-card__row">
          <span className="fs13 t3 nowrap">Closest matches:</span>
          <div className="row-wrap">
            {candidates.slice(0, 6).map((c, i) => {
              const o = c as Record<string, unknown>;
              const id = typeof o.id === "string" ? o.id : typeof o.vehicle_id === "string" ? o.vehicle_id : "";
              const name = String(o.stock_no || o.title || o.label || (id ? shortId(id, 8) : `Option ${i + 1}`));
              const href = entityHref("vehicle", id);
              return href
                ? <Link key={`${id}-${i}`} className="chip chip--sm" to={href}>{name}</Link>
                : <Chip key={`${id}-${i}`} size="sm" tone="soft">{name}</Chip>;
            })}
          </div>
        </div>
      ) : null}
      <div className="fs13 t3">Nothing was written while it waits. Answer below and it carries on.</div>
    </div>
  );
}

export interface TurnProps {
  m: ChatMessage;
  role: AgentRole;
  /** Only the newest agent reply is announced, so a screen reader is not read the whole history. */
  live?: boolean;
  onReload?: () => void;
}

export function Turn({ m, role, live = false, onReload }: TurnProps) {
  const mine = m.who === "me";
  const channel = CHANNEL_LABEL[m.channel] || humanize(m.channel) || "Web";
  const trail = m.trail || [];
  const approvals = (m.approvals || []).filter((a) => a && a.id);
  const changed = (m.changed || []).filter((c) => c && (c.id || c.kind));
  const ctxLabel = describeContext(m.context);
  const ctxLink = contextHref(m.context);

  return (
    <article className={["ag-turn", mine ? "ag-turn--me" : "ag-turn--agent"].join(" ")}>
      <header className="ag-turn__head">
        <span className="ag-turn__who">{mine ? "You" : ROLE_META[role].label}</span>
        <Chip size="sm" tone="soft">{channel}</Chip>
        {m.at ? <When iso={m.at} relative className="fs12 t4" /> : null}
        {ctxLabel && mine ? (
          ctxLink ? <Link className="fs12" to={ctxLink}>About {ctxLabel}</Link> : <span className="fs12 t4 truncate">About {ctxLabel}</span>
        ) : null}
      </header>

      <div className="ag-bubble" aria-live={live && !mine ? "polite" : undefined} aria-atomic="false">
        {m.text ? (
          <p className="ag-text">{m.text}</p>
        ) : m.streaming ? (
          <p className="ag-text t4"><span className="ag-caret" aria-hidden="true">▍</span><span className="sr-only">Answer in progress</span></p>
        ) : m.failure ? null : (
          <p className="ag-text t4">No words came back with this turn.</p>
        )}
      </div>

      {m.attachments && m.attachments.length ? (
        <div className="ag-turn__meta">{m.attachments.length} file{m.attachments.length === 1 ? "" : "s"} sent with this message.</div>
      ) : null}

      {trail.length ? (
        <div className="ag-trail">
          <span className="ag-trail__lead">Doing:</span>
          {trail.map((t) => (
            <Chip key={t.id} size="sm" tone={t.status === "blocked" || t.status === "error" ? "risk" : t.status === "needs_review" ? "wait" : "soft"}>
              {humanize(t.tool)} · {TOOL_RESULT[t.status] || t.status}
            </Chip>
          ))}
        </div>
      ) : null}

      {approvals.map((a) => (
        <div key={a.id || a.title} className="ag-card ag-card--review">
          <div className="ag-card__title">Waiting for your review</div>
          <div className="ag-card__body">
            {a.title || humanize(a.kind) || "An action was prepared"} — nothing was sent, ordered, published or paid.
            {a.expires_at ? <> Expires <When iso={a.expires_at} relative />.</> : null}
          </div>
          <div className="row-wrap">
            <Button size="md" variant="primary" onClick={() => a.id && openApproval(a.id)}>Review</Button>
            {a.id ? <Link className="fs13" to={a.review_path || `/approvals/${a.id}`}>Open the full page</Link> : null}
          </div>
        </div>
      ))}

      {m.neededInput ? <NeededInputCard n={m.neededInput} /> : null}

      {changed.length ? (
        <div className="ag-trail">
          <span className="ag-trail__lead">Changed:</span>
          {changed.map((c, i) => {
            const href = entityHref(c.kind, c.id);
            return href
              ? <Link key={`${c.id}-${i}`} className="chip chip--sm" to={href}>{changedLabel(c)}</Link>
              : <Chip key={`${c.id}-${i}`} size="sm" tone="soft">{changedLabel(c)}</Chip>;
          })}
        </div>
      ) : null}

      {m.reasons && m.reasons.length ? (
        <div className="ag-card ag-card--blocked">
          <div className="ag-card__title">Blocked</div>
          <ul className="ag-card__list">{m.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
        </div>
      ) : null}

      {!mine && m.usedModel === false ? (
        <div className="ag-turn__meta">Answered from your records without the AI model.</div>
      ) : null}

      {m.failure ? (
        <div className="ag-card ag-card--blocked">
          <div className="ag-card__title">This answer was interrupted</div>
          <div className="ag-card__body">{m.failure}</div>
          {onReload ? <div><Button size="md" variant="soft" onClick={onReload}>Reload the conversation</Button></div> : null}
        </div>
      ) : null}
    </article>
  );
}
