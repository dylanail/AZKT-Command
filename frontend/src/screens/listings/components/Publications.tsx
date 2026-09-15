/* Publication history for one vehicle (Publication rows from GET /api/listings/vehicles/{id}/package).
   Each row says exactly what the record says: channel, state, when it was last verified, the site's
   receipt/URL, the mismatch or failure reason, and whether a person still has cleanup to do. */
import { Chip, EmptyState, NotRecorded, When } from "../../../ui";
import { channelLabel, availabilityLabel, publicationStateView, type Publication } from "../types";

function receiptRef(pub: Publication): string | null {
  const r = pub.receipt || {};
  const ref = r["external_id"] ?? r["provider_ref"] ?? pub.external_id;
  return typeof ref === "string" && ref ? ref : null;
}

export function PublicationRow({ pub }: { pub: Publication }) {
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

      <span className="lst-pub__links">
        {pub.external_url ? (
          <a href={pub.external_url} target="_blank" rel="noreferrer noopener">Open the live page</a>
        ) : (
          <span className="not-recorded">No public URL recorded</span>
        )}
        {ref ? <span className="t3 tnum">Site reference {ref}</span> : null}
        {pub.manual_task_id ? <a href={`/tasks/${encodeURIComponent(pub.manual_task_id)}`}>Open the task</a> : null}
      </span>
    </div>
  );
}

export function Publications({ items }: { items: Publication[] }) {
  if (!items.length) {
    return (
      <EmptyState
        align="left"
        title="Nothing published yet"
        body="Once a package is approved and sent to a channel, every attempt and its result is listed here."
      />
    );
  }
  return <>{items.map((p) => <PublicationRow key={p.id} pub={p} />)}</>;
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
