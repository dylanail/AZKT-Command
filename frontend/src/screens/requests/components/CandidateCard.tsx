/* One candidate as it stands for ONE import request: auction identity, deadline in Japan and Arizona,
   every requirement outcome as Pass / Fail / Unknown, why it is (not) bid ready, and the actions that
   are actually available. A Mandatory Fail or Mandatory Unknown is always spelled out (G01). */
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Button, Chip, HealthLabel, Menu, type MenuItem } from "../../../ui";
import { When } from "../../../ui";
import { TZ } from "../../../lib/format";
import { DualTime, deadlineTone } from "./DualTime";
import {
  CHECK_LABEL, TRANSLATION_LABEL, auctionIdentity, bidReadyReason, candidateTitle, checkTone,
  type CandidateMatch, type Translation,
} from "../types";

export interface CandidateCardProps {
  match: CandidateMatch;
  /** The candidate's current translation, when the page loaded one. */
  translation?: Translation | null;
  /** Rank within this request's eligible candidates (1-based), when known. */
  rank?: number | null;
  onRequestTranslation?: () => void;
  onPrepareMessage?: () => void;
  onMarkSent?: () => void;
  onReject?: () => void;
  onOpenApproval?: (approvalId: string) => void;
  /** Approval id when the translation request is waiting for a decision. */
  pendingApprovalId?: string | null;
  busyKey?: (key: string) => boolean;
  writeReason?: string;
  footer?: ReactNode;
}

export function CandidateCard({
  match: m, translation, rank, onRequestTranslation, onPrepareMessage, onMarkSent, onReject,
  onOpenApproval, pendingApprovalId, busyKey, writeReason, footer,
}: CandidateCardProps) {
  const c = m.candidate || null;
  const notReady = bidReadyReason(m);
  const tone = deadlineTone(c?.deadline_at, c?.deadline_passed);
  const draft = m.buyer_draft || null;
  const translationBusy = translation && ["requested", "pending_approval", "detected", "incomplete"].includes(translation.status);

  const more: MenuItem[] = [];
  if (onMarkSent) {
    more.push({
      label: "Record that the buyer message was sent",
      meta: draft?.sent_at ? "already recorded" : "needs the message reference",
      onSelect: onMarkSent,
      disabled: !!writeReason || !draft || !!draft.sent_at || !!draft.invalidated,
      disabledReason: writeReason || (!draft ? "Prepare the buyer message first." : draft.invalidated ? "The draft was invalidated — prepare a new one." : "Already recorded as sent."),
    });
  }
  if (c) more.push({ label: "Open the auction listing", to: undefined, onSelect: () => { if (c.source_url) window.open(c.source_url, "_blank", "noopener"); }, disabled: !c.source_url, disabledReason: "No listing link recorded.", sepBefore: true });
  if (onReject) {
    more.push({
      label: "Reject for this buyer…",
      meta: "remembered as an exclusion",
      onSelect: onReject,
      disabled: !!writeReason || m.status === "passed" || m.status === "rejected",
      disabledReason: writeReason || "Already excluded for this request.",
      sepBefore: true,
    });
  }

  return (
    <article className="cnd">
      <div className="cnd__head">
        <div className="cnd__id">
          <div className="cnd__title">
            {rank ? <Chip size="sm" tone="soft" title="Rank among candidates without a must-have failure">#{rank}</Chip> : null}
            <Link to={`/candidates/${encodeURIComponent(m.candidate_id)}`} className="wrap">{candidateTitle(c)}</Link>
            {m.mandatory_fail ? <HealthLabel health="blocked" label="Fails a must-have" />
              : m.mandatory_unknown ? <HealthLabel health="risk" label="Needs confirmation" />
              : m.stale ? <HealthLabel health="risk" label="Needs re-evaluation" />
              : m.bid_ready ? <HealthLabel health="ok" label="Bid ready" /> : null}
          </div>
          <div className="cnd__meta">
            <span>{auctionIdentity(c)}</span>
            {typeof m.score === "number" ? <span>· preference score {m.score}</span> : null}
            <span>· {m.status.replace(/_/g, " ")}</span>
          </div>
        </div>
        <div className="cnd__when">
          <span className="fs12 t4">Bid deadline</span>
          <DualTime value={c?.deadline_at} />
          {tone ? <HealthLabel health={tone === "ok" ? "ok" : tone} label={tone === "blocked" ? "Deadline passed" : tone === "risk" ? "Under 24 hours" : "Open"} /> : null}
        </div>
      </div>

      <div className="cnd__checks">
        {(m.checks || []).length === 0 ? (
          <span className="fs13 t4">No requirement outcomes recorded for this match yet.</span>
        ) : (
          (m.checks || []).map((ch) => (
            <Chip
              key={ch.key}
              size="sm"
              tone={checkTone(ch.result)}
              title={`${ch.tier} · ${ch.text || ch.key} — ${ch.evidence?.observed !== undefined && ch.evidence?.observed !== null ? `observed ${String(ch.evidence.observed)}` : ch.evidence?.reason || "no evidence recorded"}`}
            >
              {ch.tier === "must" ? "Must · " : ch.tier === "avoid" ? "Avoid · " : ""}{ch.text || ch.key}: {CHECK_LABEL[ch.result]}
            </Chip>
          ))
        )}
      </div>

      {notReady ? <p className="cnd__why">{notReady}. A suitability score never overrides a must-have.</p> : null}

      <div className="cnd__state">
        <span className="fs12 t3">
          Translation: {translation ? TRANSLATION_LABEL[translation.status] || translation.status : "not requested"}
          {translation?.revision_no ? ` · r${translation.revision_no}` : ""}
        </span>
        <span className="fs12 t3">
          Buyer message: {draft?.sent_at ? <>sent <When iso={draft.sent_at} tz={TZ.phoenix} format="datetime" /></> : draft ? `draft v${draft.version}${draft.invalidated ? " · invalidated" : " · not sent"}` : "none"}
        </span>
      </div>

      <div className="cnd__actions">
        <Button size="sm" variant="glass" to={`/candidates/${encodeURIComponent(m.candidate_id)}`}>Open candidate</Button>
        {onRequestTranslation ? (
          <Button
            size="sm"
            variant="soft"
            loading={busyKey?.(`translate:${m.candidate_id}`)}
            onClick={onRequestTranslation}
            disabled={!!writeReason || !!translationBusy || translation?.status === "complete"}
            disabledReason={writeReason || (translation?.status === "complete" ? "The translation is already complete." : translationBusy ? "A translation request is already in flight." : undefined)}
          >
            Request translation
          </Button>
        ) : null}
        {pendingApprovalId && onOpenApproval ? (
          <Button size="sm" variant="glass" onClick={() => onOpenApproval(pendingApprovalId)}>Review translation request</Button>
        ) : null}
        {onPrepareMessage ? (
          <Button
            size="sm"
            variant="soft"
            loading={busyKey?.(`draft:${m.id}`)}
            onClick={onPrepareMessage}
            disabled={!!writeReason || m.mandatory_fail || m.stale || ["rejected", "passed", "lost"].includes(m.status)}
            disabledReason={writeReason || (m.mandatory_fail ? "It fails a must-have — nothing to offer this buyer." : m.stale ? "Re-evaluate the match first." : ["rejected", "passed", "lost"].includes(m.status) ? "This candidate is closed for this request." : undefined)}
          >
            {draft ? "Prepare new buyer message" : "Prepare buyer message"}
          </Button>
        ) : null}
        <Menu label="More candidate actions" align="right" items={more} trigger={<Button size="sm" variant="ghost">More</Button>} />
      </div>
      {footer}
    </article>
  );
}

export default CandidateCard;
