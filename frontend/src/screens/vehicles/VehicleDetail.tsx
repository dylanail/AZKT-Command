/* Vehicle detail: identity + one health label + one primary action in the header; sections
   Overview · Work · Files · Sale · Money (owner). Facts open Source details in the inspector.
   TODO(screen builder): GET /api/vehicles/{id} (facts with provenance, links, activity, tasks, recon,
   shipment, photos, docs, saleState, gate[], money[]); POST /api/vehicles/{id}/move-stage;
   POST /api/vehicles/{id}/facts/{k}/verify; GET /api/vehicles/{id}/history. */
import { useState } from "react";
import { useParams } from "react-router-dom";
import { useAuth } from "../../lib/auth";
import { useCan } from "../../lib/auth";
import { useInspector } from "../../app/Inspector";
import { api } from "../../lib/api";
import { useQuery } from "../../lib/useQuery";
import { Button, EmptyState, ErrorState, GlassPanel, HealthLabel, Loading, MoveStageButton, PageHeader, TabPanel, Tabs } from "../../ui";

type Tab = "overview" | "work" | "files" | "sale" | "money";
const SHOP_STAGES = [
  { id: "inspection", label: "Needs inspection" },
  { id: "recon", label: "In recon" },
  { id: "finalization", label: "Finalization" },
  { id: "ready", label: "Ready for sale" },
];

export default function VehicleDetail() {
  const { id = "" } = useParams();
  const { user } = useAuth();
  const can = useCan();
  const insp = useInspector();
  const [tab, setTab] = useState<Tab>("overview");
  const q = useQuery<Record<string, unknown> | null>((signal) => api.get("/api/vehicles/" + encodeURIComponent(id), { signal, tolerate: [404, 501] }), [id]);
  const owner = user?.role === "owner";
  const v = q.data;
  const title = (v?.title as string) || id;

  const tabs = [
    { id: "overview" as const, label: "Overview" },
    { id: "work" as const, label: "Work" },
    { id: "files" as const, label: "Files" },
    { id: "sale" as const, label: "Sale" },
    ...(can("costs.read") ? [{ id: "money" as const, label: "Money" }] : []),
  ];

  return (
    <div className="page">
      <PageHeader
        crumbs={[{ label: "Vehicles", to: "/vehicles" }, { label: title }]}
        title={<span className="row-wrap" style={{ gap: 12 }}>{title}{v ? <HealthLabel health="wait" label="Not recorded" size="md" /> : null}</span>}
        subtitle={<span className="tnum">{id}</span>}
        actions={
          <>
            <MoveStageButton stages={SHOP_STAGES} current="inspection" onMove={() => undefined} disabledReason={q.loading ? "Loading…" : !v ? "This vehicle isn't loaded yet." : !owner && !can("vehicles.write") ? "Your role can't move vehicles." : "Stage moves connect with the vehicle data."} />
            <Button variant="glass" onClick={() => insp.openAsk({ label: title, href: `/vehicles/${id}` })}>Ask about this</Button>
          </>
        }
      >
        <Tabs<Tab> label="Vehicle sections" idPrefix="veh" tabs={tabs} value={tab} onChange={setTab} />
      </PageHeader>

      {q.loading ? <GlassPanel clip><Loading label="Loading vehicle" rows={4} /></GlassPanel> :
        q.error ? <ErrorState error={q.error} onRetry={q.reload} /> :
        !v ? <GlassPanel clip><EmptyState title="Vehicle not found" body={`Nothing recorded for ${id} yet.`} action={<Button to="/vehicles" variant="soft" size="sm">Back to vehicles</Button>} /></GlassPanel> : (
          <>
            <TabPanel id="overview" idPrefix="veh" active={tab === "overview"}>
              <GlassPanel clip><EmptyState title="Facts not recorded" body="Each fact will show its source; tap one to open Source details." action={<Button size="sm" variant="soft" onClick={() => insp.openSource({ k: "Example fact", v: "Not recorded", status: "No source yet", statusTone: "wait", rows: [{ k: "Vehicle", v: id }] })}>Preview Source details</Button>} /></GlassPanel>
            </TabPanel>
            <TabPanel id="work" idPrefix="veh" active={tab === "work"}><GlassPanel clip><EmptyState title="No tasks recorded" /></GlassPanel></TabPanel>
            <TabPanel id="files" idPrefix="veh" active={tab === "files"}><GlassPanel clip><EmptyState title="No files recorded" body="Photos and documents appear here with their checks." /></GlassPanel></TabPanel>
            <TabPanel id="sale" idPrefix="veh" active={tab === "sale"}><GlassPanel clip><EmptyState title="Not in sale" /></GlassPanel></TabPanel>
            <TabPanel id="money" idPrefix="veh" active={tab === "money"}><GlassPanel clip><EmptyState title="No money lines recorded" body="Landed cost and recon lines come from Finance matches." /></GlassPanel></TabPanel>
          </>
        )}
    </div>
  );
}
