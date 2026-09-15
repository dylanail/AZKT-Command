/* Settings › Website. The versioned site profile that decides what AZKT may write to WordPress/WooCommerce,
   and every publication that still needs a person.

   GET  /api/site/profile                  → {active, versions[], writes_paused, reason, listing_gates}
   POST /api/site/profile/discover         → {base_url?, staging_url?, note?}     (owner · connections)
   POST /api/site/profile/validate         → {profile_id, staging_url?}           (listings.draft)
   POST /api/site/profile/activate         → {profile_id, note?, expected_version?}(owner · connections)
   POST /api/site/profile/resume           → {profile_id, note?}                   (owner · connections)
   GET  /api/listings/publications         → every channel result, newest first
   GET  /api/connections                   → whether WordPress/WooCommerce are set up at all
   Shapes copied from backend/app/services/site_profile.py and services/listings.py. */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../../lib/api";
import { useAuth } from "../../../lib/auth";
import { can, whyNot } from "../../../lib/perms";
import { useCommand } from "../../../lib/useCommand";
import { useQuery } from "../../../lib/useQuery";
import {
  Button, Chip, EmptyState, ErrorState, Expander, Field, GlassPanel, Input, KeyValues, Loading, Notice,
  NotRecorded, Select, When,
} from "../../../ui";
import type { ConnectionsResp } from "./types";

/* ---- shapes (backend/app/services/site_profile.py serialize_profile) ---- */
interface SiteProfile {
  id: string; version: number; profile_version: number; provider: string | null;
  base_url: string | null; staging_url: string | null; content_type: string | null;
  status: string;                       // draft | validated | active | drift | superseded
  discovered: Record<string, unknown>;
  field_map: Record<string, string>;
  field_ownership: Record<string, string>;
  media_rules: Record<string, unknown>;
  availability_map: Record<string, Record<string, unknown>>;
  validation: Record<string, unknown>;
  supported_ops: string[];
  limitations: string[];
  listing_gates: Record<string, Array<{ requirement: string; label?: string; param?: Record<string, unknown> }>>;
  drift: Record<string, unknown>;
  writes_paused: boolean;
  pause_reason: string | null;
  preview: { at?: string; staging_url?: string; ok?: boolean; errors?: string[]; warnings?: string[]; written?: boolean };
  connection_id: string | null;
  sku_strategy?: string;
  sku_prefix?: string | null;
  category_ids?: Array<string | number>;
  attribute_map?: Record<string, { id?: string | number; name?: string; visible?: boolean }>;
  validated_at: string | null; activated_at: string | null; activated_by: string | null;
  discovered_at: string | null; drift_detected_at: string | null; created_at: string | null;
}
/** The slices of `discovered` this screen reads (adapters/wordpress.py discover()). */
interface SiteTaxonomy {
  product_categories?: Array<{ id: number | string; name?: string; slug?: string }>;
  product_attributes?: Array<{ id: number | string; name?: string; slug?: string }>;
}
/* Recorded vehicle facts a shop usually shows on the product. Mapping is per site attribute and
   entirely optional; an unmapped spec still lives on the vehicle and in the listing copy. */
const SPEC_KEYS: Array<{ key: string; label: string }> = [
  { key: "make", label: "Make" },
  { key: "model", label: "Model" },
  { key: "model_year", label: "Year" },
  { key: "color", label: "Colour" },
  { key: "grade", label: "Grade" },
  { key: "transmission", label: "Transmission" },
  { key: "mileage", label: "Mileage" },
  { key: "engine", label: "Engine" },
];

interface ProfileOverview {
  active: SiteProfile | null;
  versions: SiteProfile[];
  writes_paused: boolean;
  reason: string | null;
  listing_gates: Record<string, Array<{ requirement: string; label?: string; param?: Record<string, unknown> }>>;
}
interface PublicationRow {
  id: string; package_id: string | null; vehicle_id: string; channel: string; state: string;
  external_url: string | null; error: string | null; error_kind: string | null; desired_state: string | null;
  observed_state: string | null; last_verified_at: string | null; cleanup_required: boolean;
  unsupported_reason: string | null; manual_task_id: string | null;
}

const STATUS: Record<string, { label: string; tone: "ok" | "wait" | "risk" | "blocked" | "soft"; blurb: string }> = {
  draft: { label: "Found, not checked", tone: "wait", blurb: "AZKT has read the site's shape but has not previewed anything on staging yet. Writes stay off." },
  validated: { label: "Checked on staging", tone: "wait", blurb: "A draft rendered cleanly on the staging site. Activate it to allow writes." },
  active: { label: "Active", tone: "ok", blurb: "This version decides what AZKT may write to the site." },
  drift: { label: "Paused — the site changed", tone: "blocked", blurb: "Something on the site no longer matches this version, so writes are paused until it is checked again." },
  superseded: { label: "Replaced", tone: "soft", blurb: "A newer version took over." },
};
function statusView(s: string | null | undefined) {
  if (!s) return { label: "Not set up", tone: "soft" as const, blurb: "" };
  return STATUS[s] || { label: s.replace(/_/g, " "), tone: "soft" as const, blurb: "" };
}

const NEEDS_ATTENTION = new Set(["failed", "mismatch", "unknown", "cleanup_pending", "needs_review", "unsupported"]);
const PUB_STATE: Record<string, string> = {
  queued: "Queued", accepted: "Accepted by the site", published: "Published, not verified", verified: "Live and verified",
  pending_verification: "Waiting to verify", mismatch: "Doesn't match", failed: "Failed", unknown: "Result unknown",
  needs_review: "Needs a decision", unsupported: "Hand-off to a person", cleanup_pending: "Cleanup pending",
};

export function WebsiteSection() {
  const { user } = useAuth();
  const manage = can(user, "connections");
  const draft = can(user, "listings.draft");
  const { run, busy } = useCommand();
  const [tick, setTick] = useState(0);
  const reload = () => setTick((t) => t + 1);

  const q = useQuery<ProfileOverview | null>(
    (signal) => api.get<ProfileOverview | null>("/api/site/profile", { signal, tolerate: [403, 404] }), [tick]);
  const conns = useQuery<ConnectionsResp | null>(
    (signal) => api.get<ConnectionsResp | null>("/api/connections", { signal, tolerate: [403] }), []);
  const pubs = useQuery<{ items: PublicationRow[] } | null>(
    (signal) => api.get<{ items: PublicationRow[] } | null>("/api/listings/publications", { signal, tolerate: [403, 404] }), [tick]);

  const profile = q.data?.active || q.data?.versions?.[0] || null;
  const [baseUrl, setBaseUrl] = useState("");
  const [stagingUrl, setStagingUrl] = useState("");
  const [skuStrategy, setSkuStrategy] = useState("preserve");
  const [skuPrefix, setSkuPrefix] = useState("");
  useEffect(() => {
    if (!profile) return;
    setBaseUrl((b) => b || profile.base_url || "");
    setStagingUrl((s) => s || profile.staging_url || "");
    setSkuStrategy(profile.sku_strategy || "preserve");
    setSkuPrefix(profile.sku_prefix || "");
  }, [profile?.id]);  // eslint-disable-line react-hooks/exhaustive-deps
  const skuDirty = !!profile
    && (skuStrategy !== (profile.sku_strategy || "preserve") || skuPrefix !== (profile.sku_prefix || ""));

  /* Shop categories and product attributes, chosen only from taxonomy discovery actually found. */
  const siteCategories = ((profile?.discovered as SiteTaxonomy | undefined)?.product_categories) || [];
  const siteAttributes = ((profile?.discovered as SiteTaxonomy | undefined)?.product_attributes) || [];
  const [categoryIds, setCategoryIds] = useState<string[]>([]);
  const [attrMap, setAttrMap] = useState<Record<string, string>>({});
  useEffect(() => {
    if (!profile) return;
    setCategoryIds((profile.category_ids || []).map(String));
    const m: Record<string, string> = {};
    for (const [key, cfg] of Object.entries(profile.attribute_map || {})) {
      if (cfg?.id != null) m[key] = String(cfg.id);
    }
    setAttrMap(m);
  }, [profile?.id]);  // eslint-disable-line react-hooks/exhaustive-deps
  const savedCats = (profile?.category_ids || []).map(String).sort().join(",");
  const savedAttrs = JSON.stringify(Object.fromEntries(
    Object.entries(profile?.attribute_map || {}).filter(([, c]) => c?.id != null).map(([k, c]) => [k, String(c!.id)])));
  const mappingDirty = !!profile
    && (categoryIds.slice().sort().join(",") !== savedCats || JSON.stringify(attrMap) !== savedAttrs);
  const saveMapping = () => act("product-mapping", {
    profile_id: profile!.id,
    category_ids: categoryIds,
    attribute_map: Object.fromEntries(Object.entries(attrMap)
      .filter(([, id]) => id)
      .map(([key, id]) => [key, { id, visible: true }])),
  }, "Saved. It applies to the next publish.");

  /* Is the site connected at all? The adapter refuses with these exact reasons (adapters/wordpress.py). */
  const wp = conns.data?.items?.find((c) => c.provider === "wordpress") || null;
  const woo = conns.data?.items?.find((c) => c.provider === "woocommerce") || null;
  const siteUrlSet = !!((wp?.config?.site_url as string) || (woo?.config?.site_url as string) || profile?.base_url);
  const credsSet = !!(wp && wp.status !== "disconnected") || !!(woo && woo.status !== "disconnected");
  const setupBlocked = !siteUrlSet
    ? "The website address isn't set. Add the site URL under Settings → Connections."
    : !credsSet
      ? "No website credentials are stored yet. Add a WordPress application password or WooCommerce key and secret under Settings → Connections."
      : null;

  const view = statusView(profile?.status);
  const paused = q.data?.writes_paused ?? true;
  const pauseReason = profile?.pause_reason || q.data?.reason || null;

  const act = async (action: string, body: Record<string, unknown>, success: string) => {
    const r = await run(action, `/api/site/profile/${action}`, body, { success });
    if (r) reload();
  };

  const discoverReason = !manage ? whyNot("connections") : setupBlocked || undefined;
  const validateReason = !draft ? "Your role can't preview the site."
    : !profile ? "Check the site first — there's nothing to preview yet."
      : !stagingUrl.trim() ? "Enter a staging address. A preview never touches the live site."
        : stagingUrl.trim().replace(/\/+$/, "") === (profile.base_url || "").replace(/\/+$/, "")
          ? "The staging address must be different from the live site."
          : undefined;
  const activateReason = !manage ? whyNot("connections")
    : !profile ? "Check the site first."
      : profile.status === "active" ? "This version is already active."
        : !profile.preview?.ok ? "Preview it on staging first — a version without a passing preview can never publish."
          : undefined;
  const resumeReason = !manage ? whyNot("connections")
    : !profile || !profile.writes_paused ? "Writes aren't paused."
      : !profile.preview?.ok ? "Preview it on staging again before resuming writes."
        : undefined;

  const attention = (pubs.data?.items || []).filter((p) => NEEDS_ATTENTION.has(p.state) || p.cleanup_required);

  return (
    <div className="stack">
      {setupBlocked ? (
        <Notice tone="risk" lead="The website isn't connected" action={<Button size="sm" variant="soft" to="/settings/connections">Connections</Button>}>
          {setupBlocked}
        </Notice>
      ) : null}
      {profile && paused && pauseReason ? (
        <Notice tone={profile.status === "drift" ? "blocked" : "risk"} lead="Website writes are paused">
          {pauseReason} Drafting and reviewing listings still work — only the write to the site is held.
        </Notice>
      ) : null}

      <GlassPanel padded>
        {q.loading ? <Loading rows={4} label="Loading the website profile" />
          : q.error ? <ErrorState error={q.error} onRetry={q.reload} />
            : q.data === null ? <EmptyState align="left" title="Not available for your role" body={whyNot("connections")} />
              : !profile ? (
                <div className="stack-sm">
                  <EmptyState
                    align="left"
                    title="No website profile yet"
                    body="AZKT reads the installed site first — which fields exist, whether trucks are WooCommerce products or a custom post type, how media and availability work — and stores that as a version. Nothing is written until you activate one."
                  />
                  <div className="row-wrap">
                    <Button variant="primary" loading={busy("discover")} disabled={!!discoverReason} disabledReason={discoverReason}
                      onClick={() => act("discover", { base_url: baseUrl.trim() || null, staging_url: stagingUrl.trim() || null }, "Read the site and stored a new profile version.")}>
                      Read the site
                    </Button>
                  </div>
                </div>
              ) : (
                <div className="stack">
                  <div className="set-row" style={{ borderBottom: 0, paddingTop: 0 }}>
                    <div className="set-row__main">
                      <span className="set-row__title">
                        <span>Site profile v{profile.profile_version}</span>
                        <Chip size="sm" tone={view.tone}>{view.label}</Chip>
                      </span>
                      <span className="set-row__meta">
                        {profile.provider === "woocommerce" ? "WooCommerce products" : "WordPress posts"}
                        {profile.content_type ? ` · ${profile.content_type.replace(/_/g, " ")}` : ""}
                        {profile.base_url ? ` · ${profile.base_url}` : ""}
                      </span>
                      {view.blurb ? <span className="fs13 t3">{view.blurb}</span> : null}
                    </div>
                  </div>

                  <KeyValues items={[
                    ["Live site", profile.base_url || <NotRecorded text="Not recorded" />],
                    ["Staging site", profile.staging_url || <NotRecorded text="No staging address — previews can't run" />],
                    ["Read on", profile.discovered_at ? <When iso={profile.discovered_at} format="long" /> : <NotRecorded />],
                    ["Preview on staging", profile.preview?.at
                      ? <>{profile.preview.ok ? "Passed" : "Failed"} · <When iso={profile.preview.at} format="long" /></>
                      : <NotRecorded text="Never previewed" />],
                    ["Activated", profile.activated_at ? <When iso={profile.activated_at} format="long" /> : <NotRecorded text="Not activated" />],
                    ["Writes", profile.writes_paused ? "Paused" : "Allowed"],
                  ]} />

                  {profile.preview?.errors?.length ? (
                    <div className="stack-sm">
                      <span className="fs13" style={{ color: "var(--blocked)" }}>The staging preview reported:</span>
                      {profile.preview.errors.map((e, i) => <span key={i} className="fs13">{e}</span>)}
                    </div>
                  ) : null}
                  {profile.limitations?.length ? (
                    <div className="stack-sm">
                      <span className="fs13 t3">What this site can't do:</span>
                      {profile.limitations.map((l, i) => <span key={i} className="fs13 t3">· {l}</span>)}
                    </div>
                  ) : null}

                  <div className="form-grid">
                    <Field label="Live site address" hint="Stored on the Connections page; shown here for context.">
                      <Input type="url" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://…" disabled={!manage} />
                    </Field>
                    <Field label="Staging address" hint="A preview is rendered here and never on the live site.">
                      <Input type="url" value={stagingUrl} onChange={(e) => setStagingUrl(e.target.value)} placeholder="https://staging…" disabled={!manage && !draft} />
                    </Field>
                  </div>

                  <div className="form-grid">
                    <Field label="Product numbers (SKU)"
                      hint="Your shop numbers its own products, so by default AZKT never touches that column and
                            recognises its own listings by a hidden marker instead.">
                      <Select value={skuStrategy} onChange={(e) => setSkuStrategy(e.target.value)} disabled={!manage}>
                        <option value="preserve">Leave the site's SKU alone (recommended)</option>
                        <option value="stock_no">Write the AZKT stock number as the SKU</option>
                        <option value="prefix">Write a prefixed stock number</option>
                      </Select>
                    </Field>
                    {skuStrategy === "prefix" ? (
                      <Field label="Prefix" hint="Goes in front of the stock number, e.g. AZKT-">
                        <Input value={skuPrefix} onChange={(e) => setSkuPrefix(e.target.value)}
                          placeholder="AZKT-" disabled={!manage} />
                      </Field>
                    ) : null}
                  </div>
                  {siteCategories.length || siteAttributes.length ? (
                    <div className="stack-sm">
                      <span className="fs13 t3">
                        Where a published truck lands in the shop. Nothing here is guessed — these are the
                        categories and attributes read from your site. Leaving them empty means AZKT sends
                        neither, so anything your editors set stays exactly as it is.
                      </span>
                      {siteCategories.length ? (
                        <Field label="Shop categories"
                          hint={categoryIds.length ? undefined
                            : "A product with no category never appears on a category page."}>
                          <div className="row-wrap">
                            {siteCategories.map((c) => {
                              const id = String(c.id);
                              const on = categoryIds.includes(id);
                              return (
                                <Button key={id} size="xs" variant={on ? "primary" : "soft"} disabled={!manage}
                                  disabledReason={whyNot("connections")}
                                  onClick={() => setCategoryIds((cur) =>
                                    on ? cur.filter((x) => x !== id) : [...cur, id])}>
                                  {c.name || c.slug || id}
                                </Button>
                              );
                            })}
                          </div>
                        </Field>
                      ) : null}
                      {siteAttributes.length ? (
                        <div className="form-grid">
                          {SPEC_KEYS.map(({ key, label }) => (
                            <Field key={key} label={label}>
                              <Select value={attrMap[key] || ""} disabled={!manage}
                                onChange={(e) => setAttrMap((m) => ({ ...m, [key]: e.target.value }))}>
                                <option value="">Don't show it on the product</option>
                                {siteAttributes.map((a) => (
                                  <option key={String(a.id)} value={String(a.id)}>{a.name || a.slug}</option>
                                ))}
                              </Select>
                            </Field>
                          ))}
                        </div>
                      ) : null}
                      {mappingDirty ? (
                        <div className="row-wrap">
                          <Button variant="soft" loading={busy("product-mapping")} disabled={!manage}
                            disabledReason={whyNot("connections")} onClick={saveMapping}>
                            Save where listings go
                          </Button>
                          <Button variant="ghost" onClick={() => {
                            setCategoryIds((profile.category_ids || []).map(String));
                            setAttrMap(Object.fromEntries(Object.entries(profile.attribute_map || {})
                              .filter(([, c]) => c?.id != null).map(([k, c]) => [k, String(c!.id)])));
                          }}>Undo</Button>
                        </div>
                      ) : null}
                    </div>
                  ) : null}

                  {skuDirty ? (
                    <div className="row-wrap">
                      <Button variant="soft" loading={busy("sku")} disabled={!manage} disabledReason={whyNot("connections")}
                        onClick={() => act("sku", { profile_id: profile.id, sku_strategy: skuStrategy,
                                                    sku_prefix: skuPrefix.trim() || null },
                                          "Saved. It applies to the next publish.")}>
                        Save the SKU rule
                      </Button>
                      <Button variant="ghost" onClick={() => {
                        setSkuStrategy(profile.sku_strategy || "preserve");
                        setSkuPrefix(profile.sku_prefix || "");
                      }}>Undo</Button>
                    </div>
                  ) : null}

                  <div className="row-wrap">
                    <Button variant="soft" loading={busy("discover")} disabled={!!discoverReason} disabledReason={discoverReason}
                      onClick={() => act("discover", { base_url: baseUrl.trim() || null, staging_url: stagingUrl.trim() || null }, "Read the site again and stored a new profile version.")}>
                      Read the site again
                    </Button>
                    <Button variant="soft" loading={busy("validate")} disabled={!!validateReason} disabledReason={validateReason}
                      onClick={() => act("validate", { profile_id: profile.id, staging_url: stagingUrl.trim() || null }, "Previewed on staging. Nothing was written to the live site.")}>
                      Preview on staging
                    </Button>
                    <Button variant="primary" loading={busy("activate")} disabled={!!activateReason} disabledReason={activateReason}
                      onClick={() => act("activate", { profile_id: profile.id, expected_version: profile.version }, "This version is now the write authority")}>
                      Activate this version
                    </Button>
                    {profile.writes_paused ? (
                      <Button variant="soft" loading={busy("resume")} disabled={!!resumeReason} disabledReason={resumeReason}
                        onClick={() => act("resume", { profile_id: profile.id }, "Writes resumed")}>
                        Resume writes
                      </Button>
                    ) : null}
                  </div>
                  <div className="set-foot">
                    Reading the site and activating a version are owner decisions and go through approval. A preview
                    writes nothing anywhere.
                  </div>
                </div>
              )}
      </GlassPanel>

      {profile ? (
        <GlassPanel padded>
          <div className="stack-sm">
            <h3 style={{ margin: 0, fontSize: 15 }}>What AZKT writes, and what stays yours</h3>
            <KeyValues items={Object.entries(profile.field_map || {}).slice(0, 14).map(([logical, target]) => [
              logical.replace(/_/g, " "),
              <span key={logical}>
                → <span className="tnum">{target}</span>
                {profile.field_ownership?.[logical] === "site" ? <span className="t4 fs12"> · the site's editors own this</span> : null}
              </span>,
            ])} />
            {!Object.keys(profile.field_map || {}).length ? <span className="not-recorded">No field mapping recorded yet.</span> : null}
            <span className="set-foot" style={{ padding: 0 }}>
              Categories, tags, SEO, theme settings and payment/tax/shipping stay with the site's editors — a listing
              never changes them. Anything else an editor changes on an AZKT field pauses writes instead of being
              overwritten.
            </span>
            <Expander title="Availability, media and supported operations">
              <KeyValues items={[
                ["Availability mapping", Object.keys(profile.availability_map || {}).length
                  ? Object.keys(profile.availability_map).join(", ") : <NotRecorded />],
                ["Media rules", JSON.stringify(profile.media_rules || {})],
                ["Supported operations", profile.supported_ops?.length ? profile.supported_ops.join(", ") : <NotRecorded />],
                ["Profile id", <span className="tnum">{profile.id}</span>],
              ]} />
            </Expander>
          </div>
        </GlassPanel>
      ) : null}

      {q.data?.listing_gates && Object.keys(q.data.listing_gates).length ? (
        <GlassPanel padded>
          <div className="stack-sm">
            <h3 style={{ margin: 0, fontSize: 15 }}>Checks before a listing may be published</h3>
            {Object.entries(q.data.listing_gates).map(([cls, rules]) => (
              <div key={cls} className="stack-sm">
                <span className="eyebrow">{cls === "en_route" ? "On its way" : cls === "ready_for_sale" ? "Ready for sale" : cls.replace(/_/g, " ")}</span>
                {(rules || []).map((r, i) => (
                  <span key={i} className="fs13">· {r.label || r.requirement.replace(/_/g, " ")}
                    {r.param && Object.keys(r.param).length ? <span className="t4"> ({Object.entries(r.param).map(([k, v]) => `${k} ${String(v)}`).join(", ")})</span> : null}
                  </span>
                ))}
              </div>
            ))}
            <span className="set-foot" style={{ padding: 0 }}>
              A listing class with no checks configured can be drafted but never published.
            </span>
          </div>
        </GlassPanel>
      ) : null}

      <GlassPanel clip>
        <div style={{ padding: "14px 16px 6px" }}>
          <h3 style={{ margin: 0, fontSize: 15 }}>Publications that need a person</h3>
        </div>
        {pubs.loading ? <div style={{ padding: 16 }}><Loading rows={2} label="Loading publications" /></div>
          : pubs.error ? <div style={{ padding: 16 }}><ErrorState error={pubs.error} onRetry={pubs.reload} /></div>
            : pubs.data === null ? <div style={{ padding: 16 }}><span className="not-recorded">Not available for your role.</span></div>
              : !attention.length ? (
                <div style={{ padding: "0 16px 16px" }}>
                  <EmptyState align="left" title="Nothing waiting" body="Every publication either verified or hasn't been attempted." />
                </div>
              ) : attention.map((p) => (
                <div key={p.id} className="set-row">
                  <div className="set-row__main">
                    <span className="set-row__title">
                      <span>{p.channel === "website" ? "Website" : p.channel.replace(/_/g, " ")}</span>
                      <Chip size="sm" tone={p.state === "unsupported" ? "amber" : "blocked"}>{PUB_STATE[p.state] || p.state.replace(/_/g, " ")}</Chip>
                      {p.cleanup_required ? <Chip size="sm" tone="amber">Cleanup open</Chip> : null}
                    </span>
                    <span className="set-row__meta">
                      Should show {p.desired_state || "—"}
                      {p.observed_state ? ` · site shows ${p.observed_state}` : ""}
                      {p.last_verified_at ? <> · checked <When iso={p.last_verified_at} relative /></> : ""}
                    </span>
                    {p.error ? <span className="fs12" style={{ color: "var(--blocked)" }}>{p.error}</span> : null}
                    {p.unsupported_reason ? <span className="fs12 t3">{p.unsupported_reason}</span> : null}
                  </div>
                  <div className="set-row__right">
                    {p.package_id ? <Button size="sm" variant="soft" to={`/listings/${encodeURIComponent(p.package_id)}`}>Open listing</Button> : null}
                    <Button size="sm" variant="ghost" to={`/vehicles/${encodeURIComponent(p.vehicle_id)}?tab=sale`}>Vehicle</Button>
                    {p.manual_task_id ? <Link className="fs13" to={`/tasks/${encodeURIComponent(p.manual_task_id)}`}>Task</Link> : null}
                  </div>
                </div>
              ))}
      </GlassPanel>

      {q.data?.versions?.length ? (
        <Expander title="Earlier site profile versions">
          <div className="stack-sm">
            {q.data.versions.map((p) => (
              <span key={p.id} className="fs13">
                v{p.profile_version} · {statusView(p.status).label}
                {p.activated_at ? <> · activated <When iso={p.activated_at} format="date" /></> : p.discovered_at ? <> · read <When iso={p.discovered_at} format="date" /></> : null}
              </span>
            ))}
          </div>
        </Expander>
      ) : null}
    </div>
  );
}
