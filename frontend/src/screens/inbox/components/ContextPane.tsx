/* Right pane (top): who the thread is with, the records it is about, and the reversible triage actions.
   Link / Unlink go through inbox.link_record / inbox.unlink_record and say out loud that correcting a
   link sends unsent drafts back for review. */
import { Link } from "react-router-dom";
import { Button, Chip, GlassPanel, Menu, NotRecorded, type MenuItem } from "../../../ui";
import { entityHref } from "../../../lib/links";
import { LINK_KIND_LABELS, type ContextVehicle, type RecordLink, type ThreadDetail } from "../types";

function LinkedRow({ link, vehicle, onUnlink, canDraft, draftReason, busy }: {
  link: RecordLink;
  vehicle?: ContextVehicle;
  onUnlink: (l: RecordLink) => void;
  canDraft: boolean;
  draftReason: string;
  busy: boolean;
}) {
  const href = entityHref(link.kind, link.id);
  const label = vehicle
    ? [vehicle.stock_no, vehicle.title].filter(Boolean).join(" · ") || vehicle.id.slice(0, 8)
    : `${LINK_KIND_LABELS[link.kind] || link.kind} ${link.id.slice(0, 8)}`;
  const reason = link.evidence?.reason || null;
  return (
    <div className="ib-link">
      <div className="ib-link__main">
        <span className="ib-link__kind fs12 t3">{LINK_KIND_LABELS[link.kind] || link.kind}</span>
        <span className="ib-link__label">{href ? <Link to={href}>{label}</Link> : label}</span>
        <span className="ib-link__meta fs12 t4">
          {link.match === "proposed" ? "Suggested — confirm or change it" : "Confirmed"}
          {vehicle?.commercial_state ? ` · ${vehicle.commercial_state.replace(/_/g, " ")}` : ""}
          {reason ? ` · ${reason}` : ""}
        </span>
      </div>
      <Button size="xs" variant="ghost" onClick={() => onUnlink(link)} loading={busy}
        disabled={!canDraft} disabledReason={draftReason}>Unlink</Button>
    </div>
  );
}

export default function ContextPane({
  detail, canDraft, draftReason, onLink, onUnlink, onSpam, onNotSpam, onArchive, onClassify, onManualReply,
  busyKey,
}: {
  detail: ThreadDetail;
  canDraft: boolean;
  draftReason: string;
  onLink: () => void;
  onUnlink: (l: RecordLink) => void;
  onSpam: () => void;
  onNotSpam: () => void;
  onArchive: () => void;
  onClassify: () => void;
  onManualReply: () => void;
  busyKey: (k: string) => boolean;
}) {
  const c = detail.conversation;
  const contact = detail.context.contact;
  const links = detail.context.links || [];
  const vehiclesById = new Map(detail.context.vehicles.map((v) => [v.id, v]));
  const spam = c.classification === "spam";
  const archived = c.state === "archived";

  const menu: MenuItem[] = [
    { label: "Change what kind of thread this is", onSelect: onClassify, disabled: !canDraft, disabledReason: draftReason },
    spam
      ? { label: "Not spam", meta: "Put it back where it was", onSelect: onNotSpam, disabled: !canDraft, disabledReason: draftReason }
      : { label: "Mark as suspected spam", meta: "Reversible · nothing is deleted", onSelect: onSpam, disabled: !canDraft, disabledReason: draftReason },
    { label: "I replied outside AZKT", meta: "Record what you sent", onSelect: onManualReply, disabled: !canDraft, disabledReason: draftReason, sepBefore: true },
    { label: archived ? "Already archived in AZKT" : "Archive in AZKT", meta: archived ? undefined : "Stays in the mailbox", onSelect: archived ? undefined : onArchive, disabled: !canDraft || archived, disabledReason: archived ? "This thread is already archived." : draftReason },
  ];

  return (
    <GlassPanel padded className="stack-sm ib-context" aria-label="Thread context">
      <div className="ib-context__head">
        <h3 className="ib-context__title">Who and what</h3>
        <Menu label="More thread actions" align="right" items={menu}
          trigger={<Button size="xs" variant="soft">More</Button>} />
      </div>

      <div className="ib-context__person">
        <span className="fs12 t3">Person</span>
        {contact ? (
          <>
            <Link to={`/contacts/${contact.id}`}>{contact.name || "Unnamed contact"}</Link>
            <span className="fs12 t4">
              {contact.status ? contact.status.replace(/_/g, " ") : ""}
              {c.contact_match && c.contact_match !== "matched" ? ` · ${c.contact_match.replace(/_/g, " ")}` : ""}
            </span>
          </>
        ) : (
          <>
            <NotRecorded text="Not matched to anyone yet" />
            <span className="fs12 t4">{(c.participants || []).join(", ") || "No sender recorded"}</span>
          </>
        )}
        {(c.match_reasons || []).length ? <span className="fs12 t4">Matched by {c.match_reasons.join(" · ")}</span> : null}
      </div>

      <div className="stack-sm">
        <div className="ib-context__head">
          <span className="fs12 t3">Records this thread is about</span>
          <Button size="xs" variant="soft" onClick={onLink} disabled={!canDraft} disabledReason={draftReason}>Link</Button>
        </div>
        {links.length ? (
          links.map((l) => (
            <LinkedRow key={`${l.kind}:${l.id}`} link={l} vehicle={vehiclesById.get(l.id)} onUnlink={onUnlink}
              canDraft={canDraft} draftReason={draftReason} busy={busyKey(`unlink:${l.kind}:${l.id}`)} />
          ))
        ) : (
          <p className="fs13 t3" style={{ margin: 0 }}>
            Nothing linked yet. Link the vehicle, lead or request so a draft can use its current facts.
          </p>
        )}
        {links.length ? (
          <p className="fs12 t4" style={{ margin: 0 }}>
            Changing a link sends any unsent draft back for review. Messages already sent are flagged, never resent.
          </p>
        ) : null}
      </div>

      {c.sensitivity && c.sensitivity !== "normal" ? (
        <div><Chip size="sm" tone="risk">Sensitive · {c.sensitivity.replace(/_/g, " ")}</Chip></div>
      ) : null}
      {c.case_id ? <div className="fs12 t4">Part of case {c.case_id.slice(0, 8)}</div> : null}
    </GlassPanel>
  );
}
