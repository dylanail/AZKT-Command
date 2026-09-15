/* Listing editor — route /listings/:id, where :id is a listing package id (spec §7.3).
   Reads:  GET /api/listings/packages/{id}/preview          (this exact version: copy, photos, site payload)
           GET /api/listings/vehicles/{vid}/package?channel= (current package, checks, diff, site profile)
           GET /api/listings/publications?vehicle_id=        (history + the channels the API supports)
           GET /api/vehicles/{vid}                           (identity for the header; 403/404 tolerated)
   Writes: POST /api/listings/vehicles/{vid}/build · /packages/{id}/submit · /packages/{id}/publish ·
           /api/listings/publish-availability — all through useCommand, all showing the server's reasons.
   The copy itself is generated from recorded facts; there is no edit endpoint, so it is read-only here
   and the screen says so. Nothing claims a publication the publication record doesn't show. */
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { can, whyNot } from "../../lib/perms";
import { useCommand } from "../../lib/useCommand";
import { useQuery } from "../../lib/useQuery";
import { useIsMobile } from "../../lib/viewport";
import {
  Button, Chip, ErrorState, Expander, GlassPanel, KeyValues, Loading, Notice, NotRecorded, PageHeader,
  SegmentedControl, Select, Field, Switch, Textarea, When, useToast,
} from "../../ui";
import { fetchPreview, fetchPublications, fetchVehiclePackage, listingPaths } from "./api";
import { ChecksList, FirstBlocker } from "./components/Checks";
import { DiffPanel, PulledFrom } from "./components/BuildPanel";
import { ListingPreview } from "./components/Preview";
import { LivePublicationSummary, Publications } from "./components/Publications";
import {
  availabilityLabel, channelLabel, classLabel, packageStatusView,
  type AvailabilityResult, type BuildResult, type PublishResult,
} from "./types";
import "../../styles/listings.css";

interface VehicleBrief { vehicle: { id: string; version: number; title: string; stock_no?: string | null } }

export default function ListingEditor() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const { user } = useAuth();
  const mobile = useIsMobile();
  const { toast } = useToast();
  const [params, setParams] = useSearchParams();
  const { run, busy } = useCommand();
  const [tick, setTick] = useState(0);
  const reloadAll = () => setTick((t) => t + 1);

  const pane = params.get("pane") === "preview" ? "preview" : "editor";
  const setPane = (next: "editor" | "preview") => {
    const p = new URLSearchParams(params);
    if (next === "editor") p.delete("pane"); else p.set("pane", next);
    setParams(p, { replace: true });
  };

  const previewQ = useQuery((signal) => fetchPreview(id, signal), [id, tick]);
  const pkg = previewQ.data?.package || null;
  const vehicleId = pkg?.vehicle_id || "";
  const channel = params.get("channel") || pkg?.channel || "website";

  const viewQ = useQuery(
    (signal) => (vehicleId ? fetchVehiclePackage(vehicleId, channel, signal) : Promise.resolve(null)),
    [vehicleId, channel, tick],
  );
  const pubsQ = useQuery(
    (signal) => (vehicleId ? fetchPublications(vehicleId, signal) : Promise.resolve(null)),
    [vehicleId, tick],
  );
  const vehicleQ = useQuery(
    (signal) => (vehicleId
      ? api.get<VehicleBrief | null>(`/api/vehicles/${encodeURIComponent(vehicleId)}`, { signal, tolerate: [403, 404] })
      : Promise.resolve(null)),
    [vehicleId],
  );

  const [listingClass, setListingClass] = useState<string>("");
  const [useModel, setUseModel] = useState(true);
  const [note, setNote] = useState("");

  if (previewQ.loading) return <div className="page" aria-busy="true"><Loading label="Loading the listing package" rows={5} /></div>;
  if (previewQ.error) {
    return (
      <div className="page">
        <PageHeader title="Listing" crumbs={[{ label: "Vehicles", to: "/vehicles?view=sales" }, { label: "Listing" }]} />
        <ErrorState error={previewQ.error} onRetry={previewQ.reload} title="Couldn't open this listing package" />
      </div>
    );
  }
  if (!pkg) {
    return (
      <div className="page">
        <PageHeader title="Listing not found" crumbs={[{ label: "Vehicles", to: "/vehicles?view=sales" }, { label: "Listing" }]} />
        <GlassPanel padded>
          <div className="stack-sm">
            <span>No listing package is recorded with this id.</span>
            <span className="fs13 t3">A package is created from a vehicle's Sale tab, not on its own.</span>
            <div><Button variant="soft" to="/vehicles?view=sales">Back to vehicles for sale</Button></div>
          </div>
        </GlassPanel>
      </div>
    );
  }

  const view = viewQ.data;
  const pubs = pubsQ.data?.items || view?.publications || [];
  const supported = pubsQ.data?.channels?.supported || [pkg.channel];
  const vehicle = vehicleQ.data?.vehicle || null;
  const vehicleName = vehicle?.title || pkg.headline || "This truck";
  const status = packageStatusView(pkg.status);
  const checks = pkg.readiness || [];
  const failing = checks.filter((c) => !c.ok);
  const current = view?.package || null;
  const isCurrent = !current || current.id === pkg.id;
  const profile = view?.profile || null;

  /* Can the website be written to at all? The server decides again; this only explains the button. */
  const siteReason = !profile
    ? "No website profile is active yet. Set one up in Settings → Website."
    : profile.writes_paused
      ? (profile.reason || "Website writes are paused until the site profile is re-checked.")
      : null;

  const draft = can(user, "listings.draft");
  const publishPerm = can(user, "listings.publish");
  const terminal = pkg.status === "published" || pkg.status === "superseded" || pkg.status === "invalidated";

  const submitReason =
    !draft ? "Your role can't send listings for review."
      : terminal ? `This version is ${status.label.toLowerCase()}. Build a new version first.`
        : !pkg.ready ? `Clear the checks first — ${failing[0]?.label || "one check hasn't passed"}.`
          : siteReason || undefined;
  const publishReason =
    !publishPerm ? whyNot("listings.publish")
      : terminal ? `This version is ${status.label.toLowerCase()}. Build a new version first.`
        : !pkg.ready ? `Clear the checks first — ${failing[0]?.label || "one check hasn't passed"}.`
          : siteReason || undefined;

  const doBuild = async (label: string) => {
    const body: Record<string, unknown> = { channel, use_model: useModel };
    if (listingClass) body.listing_class = listingClass;
    if (note.trim()) body.note = note.trim();
    const res = await run<BuildResult>("build", listingPaths.build(vehicleId), body, { success: label });
    if (res?.status === "ok") {
      setNote("");
      const built = res.data?.package;
      /* A rebuild makes a new version; follow it so the screen never shows a stale one silently. */
      if (built && built.id !== pkg.id) nav(`/listings/${encodeURIComponent(built.id)}`);
      else reloadAll();
    }
  };

  const doSubmit = async () => {
    const res = await run("submit", listingPaths.submit(pkg.id), { channel, note: note.trim() || null },
      { success: "Frozen for review. The owner reads this exact version before anything is published." });
    if (res?.status === "ok") { setNote(""); reloadAll(); }
  };

  const doPublish = async () => {
    const res = await run<PublishResult>("publish", listingPaths.publish(pkg.id), {
      channel,
      expected_package_hash: pkg.package_hash,
      expected_profile_version: pkg.profile_version,
      note: note.trim() || null,
    });
    if (res?.status === "ok") {
      const state = res.data?.state;
      if (state === "unsupported") {
        toast({
          title: "Handed to a person",
          tone: "risk",
          message: res.data?.publication?.unsupported_reason || `${channelLabel(channel)} has no verified connection, so posting is a task.`,
          duration: 8000,
        });
      } else {
        toast({
          title: "Queued for the website",
          tone: "ok",
          message: "The site write runs once, as an approved action. The publication below shows what the site actually did.",
          duration: 7000,
        });
      }
      setNote("");
    }
    if (res) reloadAll();
  };

  /* An en-route truck's listing says "on its way"; a ready one says "available". Both can go reserved or sold. */
  const availabilities = pkg.listing_class === "en_route"
    ? ["en_route", "reserved", "sold"]
    : ["available", "reserved", "sold"];
  const recorded = pubs.find((p) => p.channel === channel)?.desired_state || pkg.availability || availabilities[0];
  const desired = availabilities.includes(recorded) ? recorded : availabilities[0];
  const availabilityReason = !draft
    ? "Your role can't change what the listing shows."
    : !pubs.length
      ? "Nothing is published for this truck yet, so there is nothing to update on a channel."
      : undefined;
  const doAvailability = async (next: string) => {
    const res = await run<AvailabilityResult>("availability", listingPaths.availability(),
      { vehicle_id: vehicleId, availability: next, reason: note.trim() || null });
    if (res?.status === "ok") {
      const entries = res.data?.publications || [];
      const manual = entries.filter((e) => e.manual);
      toast({
        tone: manual.length ? "risk" : "ok",
        title: manual.length ? "A person has to update a channel" : `Website set to show ${availabilityLabel(next)}`,
        message: manual.length
          ? `${manual.map((m) => channelLabel(m.channel)).join(", ")} has no live mapping — a task stays open until it is verified.`
          : entries.some((e) => e.queued)
            ? "Queued under the standing permission. It stays open until the channel is verified."
            : res.data?.note || "Nothing needed changing on a channel.",
        duration: 7000,
      });
      reloadAll();
    }
  };

  const editorPane = (
    <div className="lst-col">
      {failing.length ? (
        <FirstBlocker checks={checks} vehicleId={vehicleId} onRefresh={() => doBuild("Rebuilt from the vehicle record")} refreshing={busy("build")} />
      ) : null}

      <GlassPanel padded>
        <div className="lst-sect">
          <div className="lst-sect__head">
            <h2>Checks this version must pass</h2>
            <span className="lst-sect__note">{checks.length - failing.length} of {checks.length} passed</span>
          </div>
          <ChecksList checks={checks} vehicleId={vehicleId} />
          <span className="lst-sect__note">
            These come from the website profile's rules for a {classLabel(pkg.listing_class).toLowerCase()} listing.
            A rule that isn't configured blocks publishing — it is never skipped.
          </span>
        </div>
      </GlassPanel>

      <GlassPanel padded>
        <div className="lst-sect">
          <div className="lst-sect__head">
            <h2>Built from the vehicle</h2>
            <span className="lst-sect__note">
              {pkg.built_at ? <>built <When iso={pkg.built_at} format="long" /></> : "build time not recorded"}
            </span>
          </div>
          <PulledFrom pkg={pkg} />
          <div className="stack-sm" style={{ marginTop: 4 }}>
            <Field label="Listing type" hint="Leave on Automatic and AZKT uses where the truck actually is.">
              <Select value={listingClass} onChange={(e) => setListingClass(e.target.value)} disabled={!draft}>
                <option value="">Automatic — from the vehicle's stage</option>
                <option value="en_route">On its way</option>
                <option value="ready_for_sale">Ready for sale</option>
              </Select>
            </Field>
            <Switch
              checked={useModel}
              onChange={setUseModel}
              disabled={!draft}
              disabledReason="Your role can't build listing drafts."
              label="Let the assistant write the wording"
              meta="Off uses the plain template. Either way only recorded facts are used."
            />
            <Field label="Note (optional)" hint="Kept with the build, the review and the publication.">
              <Textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)} disabled={!draft}
                placeholder="Why you're rebuilding, or anything the owner should know." />
            </Field>
            <div className="row-wrap">
              <Button
                variant="primary"
                size={mobile ? "xl" : "lg"}
                loading={busy("build")}
                disabled={!draft}
                disabledReason="Your role can't build listing drafts."
                onClick={() => doBuild(pkg ? "Rebuilt from the vehicle record" : "Listing package built")}
              >
                Refresh from the vehicle
              </Button>
              <span className="fs12 t4">
                Rebuilding makes a new version. The wording, price, photos and specs always come from the vehicle
                record — they can't be typed in here.
              </span>
            </div>
          </div>
        </div>
      </GlassPanel>

      <GlassPanel padded>
        <div className="lst-sect">
          <div className="lst-sect__head">
            <h2>Changed since this version was built</h2>
            {viewQ.loading ? <span className="lst-sect__note">checking…</span> : null}
          </div>
          {viewQ.loading ? <Loading rows={2} label="Comparing with the vehicle record" />
            : viewQ.error ? <ErrorState error={viewQ.error} onRetry={viewQ.reload} title="Couldn't compare" />
              : <DiffPanel diff={view?.diff || null} vehicleId={vehicleId}
                  emptyText="Rebuilding from today's vehicle record would produce the same listing." />}
          <span className="lst-sect__note">
            Rebuilding from today's vehicle record would change these fields. Nothing is sent to the website by looking.
          </span>
        </div>
      </GlassPanel>

      <GlassPanel padded>
        <div className="lst-sect">
          <div className="lst-sect__head">
            <h2>What the website should show</h2>
            <span className="lst-sect__note">
              now: {availabilityLabel(recorded)}{recorded !== desired ? " (not one of the choices below)" : ""}
            </span>
          </div>
          <SegmentedControl
            label="Listing availability"
            block={mobile}
            value={desired}
            onChange={(v) => void doAvailability(v)}
            options={availabilities.map((a) => ({
              value: a, label: availabilityLabel(a), disabled: !!availabilityReason || busy("availability"),
              disabledReason: availabilityReason || "Sending the change…",
            }))}
          />
          <span className="lst-sect__note">
            {availabilityReason
              || "Channel updates queue under the standing permission, or wait for approval. A task stays open until the channel is verified — a reserved truck never stays purchasable."}
          </span>
        </div>
      </GlassPanel>

      <GlassPanel clip>
        <div style={{ padding: "14px 16px 0" }}>
          <div className="lst-sect__head"><h2>Publications</h2></div>
        </div>
        {pubsQ.loading ? <div style={{ padding: 16 }}><Loading rows={2} label="Loading publications" /></div>
          : pubsQ.error ? <div style={{ padding: 16 }}><ErrorState error={pubsQ.error} onRetry={pubsQ.reload} /></div>
            : <Publications items={pubs} onChanged={reloadAll} />}
      </GlassPanel>

      <Expander title="Sources and technical details">
        <KeyValues items={[
          ["Package id", <span className="tnum">{pkg.id}</span>],
          ["Version", `v${pkg.package_version}`],
          ["Package hash", pkg.package_hash ? <span className="tnum">{pkg.package_hash.slice(0, 16)}…</span> : <NotRecorded />],
          ["Site profile version", pkg.profile_version ?? <NotRecorded text="No profile bound yet" />],
          ["Replaces", pkg.supersedes_id ? <Link to={`/listings/${encodeURIComponent(pkg.supersedes_id)}`}>the previous version</Link> : <NotRecorded text="First version" />],
          ["Approval", pkg.approval_id ? <Link to={`/approvals/${encodeURIComponent(pkg.approval_id)}`}>Open the approval</Link> : <NotRecorded text="Not approved yet" />],
          ["Changed from the version before", (pkg.diff?.changed || []).length ? (pkg.diff?.changed || []).join(", ") : "nothing"],
        ]} />
      </Expander>
    </div>
  );

  const previewPane = (
    <GlassPanel padded>
      <div className="lst-sect">
        <div className="lst-sect__head">
          <h2>Preview</h2>
          <span className="lst-sect__note">as the website would show it</span>
        </div>
        <ListingPreview pkg={pkg} preview={previewQ.data} />
      </div>
    </GlassPanel>
  );

  return (
    <div className="page">
      <PageHeader
        title={vehicleName}
        crumbs={[
          { label: "Vehicles", to: "/vehicles?view=sales" },
          { label: vehicleName, to: vehicleId ? `/vehicles/${encodeURIComponent(vehicleId)}?tab=sale` : undefined },
          { label: `Listing v${pkg.package_version}` },
        ]}
        subtitle={status.blurb || vehicle?.stock_no
          ? <>{status.blurb}{vehicle?.stock_no ? <> · stock {vehicle.stock_no}</> : null}</>
          : undefined}
        actions={
          <>
            <Button variant="soft" loading={busy("submit")} disabled={!!submitReason} disabledReason={submitReason} onClick={doSubmit}>
              Send for review
            </Button>
            <Button variant="primary" loading={busy("publish")} disabled={!!publishReason} disabledReason={publishReason} onClick={doPublish}>
              Publish
            </Button>
          </>
        }
      >
        <div className="row-wrap">
          <Chip tone={status.tone}>{status.label}</Chip>
          <Chip tone="soft">{classLabel(pkg.listing_class)}</Chip>
          <Chip tone="soft">Version {pkg.package_version}</Chip>
          <LivePublicationSummary items={pubs.filter((p) => p.channel === channel)} />
        </div>
        {supported.length > 1 ? (
          <SegmentedControl
            label="Publishing channel"
            value={channel}
            onChange={(c) => { const p = new URLSearchParams(params); p.set("channel", c); setParams(p, { replace: true }); }}
            options={supported.map((c) => ({ value: c, label: channelLabel(c) }))}
          />
        ) : (
          <span className="fs12 t4">Channel: {channelLabel(channel)} — the only one with a verified connection. Other channels get the copy, photos and a checklist for a person.</span>
        )}
        {mobile ? (
          <SegmentedControl
            label="Show"
            block
            value={pane}
            onChange={(p) => setPane(p as "editor" | "preview")}
            options={[{ value: "editor", label: "Listing" }, { value: "preview", label: "Preview" }]}
          />
        ) : null}
      </PageHeader>

      {channel !== pkg.channel ? (
        <Notice tone="wait" lead={`${channelLabel(channel)} listing`}
          action={current
            ? <Button size="sm" variant="soft" to={`/listings/${encodeURIComponent(current.id)}`}>Open it</Button>
            : <Button size="sm" variant="primary" loading={busy("build")} disabled={!draft}
                disabledReason="Your role can't build listing drafts."
                onClick={() => doBuild(`Package built for ${channelLabel(channel)}`)}>Build one</Button>}>
          {current
            ? `This page is showing the ${channelLabel(pkg.channel)} package. There is a separate one for ${channelLabel(channel)}.`
            : `Nothing has been built for ${channelLabel(channel)} yet. The page below is the ${channelLabel(pkg.channel)} package.`}
        </Notice>
      ) : null}
      {channel === pkg.channel && !isCurrent && current ? (
        <Notice tone="risk" lead="Not the latest version"
          action={<Button size="sm" variant="soft" to={`/listings/${encodeURIComponent(current.id)}`}>Open v{current.package_version}</Button>}>
          You're looking at version {pkg.package_version}. Version {current.package_version} was built after it.
        </Notice>
      ) : null}
      {siteReason ? (
        <Notice tone="risk" lead="The website can't be written to"
          action={<Button size="sm" variant="soft" to="/settings/website">Website settings</Button>}>
          {siteReason} Drafting and reviewing still work.
        </Notice>
      ) : null}
      {pkg.status === "review" ? (
        <Notice tone="wait" lead="Waiting for the owner">
          This exact version is frozen for review. Rebuilding replaces it and the review starts again.
        </Notice>
      ) : null}

      {mobile ? (
        pane === "preview" ? previewPane : editorPane
      ) : (
        <div className="lst-grid">
          {editorPane}
          <aside className="lst-col lst-grid__side" aria-label="Listing preview">{previewPane}</aside>
        </div>
      )}
    </div>
  );
}
