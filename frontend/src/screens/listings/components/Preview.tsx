/* Preview pane: the package rendered the way the website would show it.
   Photos come from ListingPackage.media_detail in slot order, main photo first, alt text from the slot
   label. The mapped site payload and its validation (GET /api/listings/packages/{id}/preview) sit inside
   the technical expander — nothing here is written to the site. */
import { Chip, Expander, KeyValues, Money, NotRecorded } from "../../../ui";
import { slotLabel } from "../../vehicles/types";
import { availabilityLabel, classLabel, type ListingPackage, type MediaItem, type PreviewPayload } from "../types";

function orderedMedia(media: MediaItem[]): MediaItem[] {
  return [...media].sort((a, b) => (a.position ?? 0) - (b.position ?? 0));
}

function altFor(m: MediaItem, index: number): string {
  const base = (m.alt || "").trim();
  const slot = m.slot ? slotLabel(m.slot) : "";
  if (base && slot) return `${base} — ${slot}`;
  if (base) return `${base} — photo ${index + 1}`;
  if (slot) return `Vehicle photo — ${slot}`;
  return `Vehicle photo ${index + 1}`;
}

export function Photos({ media }: { media: MediaItem[] }) {
  const ordered = orderedMedia(media);
  if (!ordered.length) {
    return <div className="lst-photo-empty">No approved public photo yet. The site would show no gallery.</div>;
  }
  const [hero, ...rest] = ordered;
  return (
    <div className="stack-sm">
      <figure className="lst-figure">
        <div className="lst-hero">
          <img src={hero.url} alt={altFor(hero, 0)} loading="lazy" decoding="async" />
        </div>
        <figcaption className="lst-figure__cap">
          Main photo · {hero.slot ? slotLabel(hero.slot) : "no slot recorded"}
          {hero.pre_arrival ? " · taken before arrival" : ""}
        </figcaption>
      </figure>
      {rest.length ? (
        <div className="lst-thumbs">
          {rest.map((m, i) => (
            <figure key={m.asset_id} className="lst-thumb">
              <div className="lst-thumb__box">
                <img src={m.url} alt={altFor(m, i + 1)} loading="lazy" decoding="async" />
              </div>
              <figcaption className="lst-thumb__cap" title={m.slot ? slotLabel(m.slot) : `Photo ${i + 2}`}>
                {i + 2}. {m.slot ? slotLabel(m.slot) : "no slot"}
              </figcaption>
            </figure>
          ))}
        </div>
      ) : null}
      <span className="lst-sect__note">
        {ordered.length === 1 ? "1 photo" : `${ordered.length} photos`} in this order. Only approved, public-eligible
        photos are included — documents never enter the gallery.
      </span>
    </div>
  );
}

export function ListingPreview({ pkg, preview }: { pkg: ListingPackage; preview: PreviewPayload | null }) {
  const specs = pkg.specs || [];
  const disclosures = pkg.disclosures || [];
  return (
    <div className="lst-preview">
      <Photos media={pkg.media_detail || []} />

      <div className="lst-preview__head">
        <span className="lst-preview__title">{pkg.headline || <NotRecorded text="No headline yet" />}</span>
        <span className="lst-preview__row">
          <span className="lst-preview__price">
            {pkg.price ? <Money amount={pkg.price} currency={pkg.currency || "USD"} /> : <NotRecorded text="No price on the listing" />}
          </span>
          <Chip size="sm" tone={pkg.availability === "sold" ? "blocked" : pkg.availability === "reserved" ? "amber" : "soft"}>
            {availabilityLabel(pkg.availability)}
          </Chip>
          <Chip size="sm" tone="soft">{classLabel(pkg.listing_class)}</Chip>
        </span>
      </div>

      {pkg.body ? <div className="lst-preview__body">{pkg.body}</div> : <NotRecorded text="No description yet" />}

      {specs.length ? (
        <div className="lst-sect">
          <div className="lst-sect__head"><h2>Specifications</h2></div>
          <div className="lst-specs">
            {specs.map((s, i) => (
              <div key={`${s.key}-${i}`} className="lst-spec">
                <span className="lst-spec__k">{s.key.replace(/_/g, " ")}</span>
                <span className="lst-spec__v">
                  {s.value}{s.unit ? ` ${s.unit}` : ""}
                  {s.status || s.source ? (
                    <span className="lst-spec__src"> · {[s.status, s.source].filter(Boolean).join(", ")}</span>
                  ) : null}
                </span>
              </div>
            ))}
          </div>
          <span className="lst-sect__note">Every line comes from a recorded fact and keeps its label and source.</span>
        </div>
      ) : (
        <span className="not-recorded">No recorded specifications to show.</span>
      )}

      {disclosures.length ? (
        <div className="lst-sect">
          <div className="lst-sect__head"><h2>Disclosures</h2></div>
          {disclosures.map((d, i) => (
            <div key={i} className="lst-bullet">
              <span className="lst-bullet__dot" aria-hidden="true">•</span>
              <span className="lst-bullet__body">{d.text}</span>
            </div>
          ))}
        </div>
      ) : null}

      <Expander title="What would be sent to the site">
        {preview ? (
          <div className="stack-sm">
            <KeyValues items={[
              ["Target", preview.target || <NotRecorded text="No site URL on the active profile" />],
              ["Site profile version", preview.profile_version ?? <NotRecorded />],
              ["Valid for this site", preview.valid ? "Yes" : "No"],
              ["Written to the site", "No — a preview never touches the site"],
            ]} />
            {preview.errors?.length ? (
              <div className="stack-sm">
                <span className="fs13" style={{ color: "var(--blocked)" }}>The site would reject this:</span>
                {preview.errors.map((e, i) => <span key={i} className="fs13">{e}</span>)}
              </div>
            ) : null}
            {preview.warnings?.length ? (
              <div className="stack-sm">
                <span className="fs13 t3">Warnings:</span>
                {preview.warnings.map((w, i) => <span key={i} className="fs13 t3">{w}</span>)}
              </div>
            ) : null}
            <pre className="act-pre">{JSON.stringify(preview.payload ?? {}, null, 2)}</pre>
          </div>
        ) : (
          <span className="not-recorded">The mapped payload isn't available — there is no active site profile yet.</span>
        )}
      </Expander>
    </div>
  );
}
