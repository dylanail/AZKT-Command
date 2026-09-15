/* Publication history for one vehicle (Publication rows from GET /api/listings/vehicles/{id}/package).
   Each row says exactly what the record says: channel, state, when it was last verified, the site's
   receipt/URL, the mismatch or failure reason, and whether a person still has cleanup to do.

   A row in `needs_review` is AZKT refusing to guess: the site has a listing that looks like this truck
   but carries no AZKT marker, so a person confirms which one it is (or says it is none of them) before
   anything is written. That confirmation is the only thing on this panel that changes a record. */
import { Link } from "react-router-dom";
import { useAuth } from "../../../lib/auth";
import { can, whyNot } from "../../../lib/perms";
import { useCommand } from "../../../lib/useCommand";
import { Button, Chip, EmptyState, Money, NotRecorded, When } from "../../../ui";
import { listingPaths } from "../api";
import {
  channelLabel, availabilityLabel, pendingCandidates, publicationStateView,
  type ListingCandidate, type Publication,
} from "../types";

function receiptRef(pub: Publication): string | null {
  const r = pub.receipt || {};
  const ref = r["external_id"] ?? r["provider_ref"] ?? pub.external_id;
  return typeof ref === "string" && ref ? ref : null;
}

function CandidateChoice({ pub, onLinked }: { pub: Publication; onLinked: () => void }) {
  const { user } = useAuth();
  const { run, busy } = useCommand();
  const candidates = pendingCandidates(pub);
  const allowed = can(user, "listings.publish");
  if (!candidates.length) return null;
  const link = async (c: ListingCandidate) => {
    const r = await run(`listing:link:${pub.id}:${c.external_id}`, listingPaths.link(pub.vehicle_id),
      { external_id: c.external_id, channel: pub.channel },
      { success: "Linked. The next publish updates that listing." });
    if (r?.status === "ok") onLinked();
  };
  return (
    <div className="lst-pub__choice">
      <span className="fs13 t3">
        Confirm which listing on the site is this truck. Nothing is written until you publish.
      </span>
      {candidates.map((c) => (
        <div key={c.external_id} className="row-wrap" style={{ gap: 8, justifyContent: "space-between" }}>
          <span className="row-wrap" style={{ gap: 8 }}>
            {c.url
              ? <a href={c.url} target="_blank" rel="noreferrer noopener">{c.title || `Listing ${c.external_id}`}</a>
              : <span>{c.title || `Listing ${c.external_id}`}</span>}
            {c.sku ? <span className="fs12 t4 tnum">SKU {c.sku}</span> : null}
            {c.price ? <span className="fs12 t4"><Money amount={c.price} currency="USD" /></span> : null}
            {c.status ? <Chip size="sm" tone="neutral">{c.status}</Chip> : null}
          </span>
          <Button size="xs" variant="primary" onClick={() => link(c)}
            loading={busy(`listing:link:${pub.id}:${c.external_id}`)}
            disabled={!allowed} disabledReason={whyNot("listings.publish")}>
            This is the one
          </Button>
        </div>
      ))}
      <span className="fs12 t4">
        None of these? Publish anyway and AZKT creates a new listing.
      </span>
    </div>
  );
}

function LinkedTo({ pub, onUnlinked }: { pub: Publication; onUnlinked: () => void }) {
  const { user } = useAuth();
  const { run, busy } = useCommand();
  if (!pub.external_id) return null;
  const key = `listing:unlink:${pub.id}`;
  const unlink = async () => {
    const r = await run(key, listingPaths.link(pub.vehicle_id), { channel: pub.channel, confirm_unlink: true },
      { success: "Unlinked. The listing is still on the site and is no longer managed here." });
    if (r?.status === "ok") onUnlinked();
  };
  return (
    <Button size="xs" variant="ghost" onClick={unlink} loading={busy(key)}
      disabled={!can(user, "listings.publish")} disabledReason={whyNot("listings.publish")}>
      Unlink
    </Button>
  );
}

export function PublicationRow({ pub, onChanged }: { pub: Publication; onChanged?: () => void }) {
  const view = publicationStateView(pub.state);
  const ref = receiptRef(pub);
  const last = pub.last_verified_at || pub.history?.[pub.history.length - 1]?.at || null;
  const mismatch = pub.state === "mismatch";
  return (
    <div className="lst-pub">
      <div className="lst-pub__head">
        <span className="lst-pub__title">
          <span>{channelLabel(pub.channel)}</span>
          <Chip size="sm" tone={view.tone}>{view.label}</Chip>
          {mismatch ? <Chip size="sm" tone="blocked">Mismatch</Chip> : null}
          {pub.cleanup_required ? <Chip size="sm" tone="amber">Cleanup open</Chip> : null}
        </span>
        <span className="lst-pub__meta">
          {last ? <>last checked <When iso={last} format="datetime" /></> : "not checked yet"}
          {pub.attempts ? ` · ${pub.attempts} attempt${pub.attempts === 1 ? "" : "s"}` : ""}
        </span>
      </div>

      <span className="lst-pub__meta">
        Should show {availabilityLabel(pub.desired_state)}
        {pub.observed_state ? ` · site shows ${pub.observed_state.replace(/_/g, " ")}` : " · the site hasn't been read back yet"}
        {pub.package_version ? ` · package v${pub.package_version}` : ""}
        {pub.profile_version ? ` · site profile v${pub.profile_version}` : ""}
      </span>

      {view.blurb ? <span className="fs13 t3">{view.blurb}</span> : null}
      {pub.unsupported_reason ? <span className="fs13 t3">{pub.unsupported_reason}</span> : null}
      {pub.error ? <span className="lst-pub__err">{pub.error_kind ? `${pub.error_kind.replace(/_/g, " ")}: ` : ""}{pub.error}</span> : null}
      <CandidateChoice pub={pub} onLinked={() => onChanged?.()} />

      <span className="lst-pub__links">
        {pub.external_url ? (
          <a href={pub.external_url} target="_blank" rel="noreferrer noopener">Open the live page</a>
        ) : (
          <span className="not-recorded">No public URL recorded</span>
        )}
        {ref ? <span className="t3 tnum">Site reference {ref}</span> : null}
        {pub.manual_task_id ? <Link to={`/tasks/${encodeURIComponent(pub.manual_task_id)}`}>Open the task</Link> : null}
        {pub.external_id ? <LinkedTo pub={pub} onUnlinked={() => onChanged?.()} /> : null}
      </span>
    </div>
  );
}

export function Publications({ items, onChanged }: { items: Publication[]; onChanged?: () => void }) {
  if (!items.length) {
    return (
      <EmptyState
        align="left"
        title="Nothing published yet"
        body="Once a package is approved and sent to a channel, every attempt and its result is listed here."
      />
    );
  }
  return <>{items.map((p) => <PublicationRow key={p.id} pub={p} onChanged={onChanged} />)}</>;
}

/** Small read-only summary used in the header. */
export function LivePublicationSummary({ items }: { items: Publication[] }) {
  const live = items.find((p) => p.state === "verified") || items.find((p) => p.external_url) || items[0];
  if (!live) return <NotRecorded text="Not on the website yet" />;
  const view = publicationStateView(live.state);
  return (
    <span className="row-wrap" style={{ gap: 8 }}>
      <Chip size="sm" tone={view.tone}>{channelLabel(live.channel)} · {view.label}</Chip>
      {live.external_url ? <a className="fs13" href={live.external_url} target="_blank" rel="noreferrer noopener">Open the live page</a> : null}
    </span>
  );
}
