/* One shipment: which vehicles are on it, the container/vessel/voyage and route, the ETA and storage
   deadline with the notice each came from, every leg, the milestones (planned / estimated / completed,
   container-wide or a per-vehicle exception) and the shipping-quote cases with their separate
   forwarding and booking approvals. Reads GET /api/shipments/{id}. */
import { useMemo, useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { can, whyNot } from "../../lib/perms";
import { useQuery } from "../../lib/useQuery";
import { useCommand } from "../../lib/useCommand";
import { relativeTime, TZ } from "../../lib/format";
import {
  Button, Chip, EmptyState, ErrorState, Expander, GlassPanel, HealthLabel, Loading, Menu, Notice,
  PageHeader, PageLoading, When, type MenuItem,
} from "../../ui";
import { openApproval } from "../approvals/useApprovalReview";
import { DeniedOrError } from "../requests/components/DeniedPanel";
import { DualTime } from "../requests/components/DualTime";
import { LegsTable } from "./components/LegsTable";
import { MilestoneList } from "./components/MilestoneList";
import { QuoteCase } from "./components/QuoteCase";
import {
  AddLegDialog, BookQuoteDialog, DeclineQuoteDialog, EditShipmentDialog, ForwardQuoteDialog, LegCancelDialog,
  LegUpdateDialog, RecordMilestoneDialog, RecordReplyDialog, RequestQuoteDialog, StartQuoteDialog, StorageDeadlineDialog,
} from "./components/ShipmentDialogs";
import { activityPath, quoteAction, shipmentPath } from "./api";
import { MILESTONE_LABEL, SHIPMENT_LABEL, shipmentHealth, sourceLabel, type Leg, type Quote, type ShipmentDetailResp } from "./types";
import type { ActivityRow } from "../requests/types";
import "./shipments.css";
import "../requests/requests.css";

type Dialog = "edit" | "milestone" | "storage" | "add-leg" | "start-quote" | null;

export default function ShipmentDetail() {
  const { id = "" } = useParams();
  const { user } = useAuth();
  const { run, busy } = useCommand();
  const [dialog, setDialog] = useState<Dialog>(null);
  const [legEdit, setLegEdit] = useState<Leg | null>(null);
  const [legCancel, setLegCancel] = useState<Leg | null>(null);
  const [quoteRequest, setQuoteRequest] = useState<Quote | null>(null);
  const [quoteReply, setQuoteReply] = useState<Quote | null>(null);
  const [quoteForward, setQuoteForward] = useState<Quote | null>(null);
  const [quoteBook, setQuoteBook] = useState<Quote | null>(null);
  const [quoteDecline, setQuoteDecline] = useState<Quote | null>(null);

  const q = useQuery<ShipmentDetailResp | null>(
    (signal) => api.get<ShipmentDetailResp | null>(shipmentPath(id), { signal, tolerate: [404] }),
    [id],
  );
  const activity = useQuery<{ items: ActivityRow[] } | null>(
    (signal) => api.get<{ items: ActivityRow[] } | null>(activityPath("shipment", id), { signal, tolerate: [403, 404, 501] }),
    [id],
  );

  const write = can(user, "shipping.write");
  const costs = can(user, "costs.read");
  const writeReason = write ? undefined : whyNot("shipping.write");

  const d = q.data;
  const s = d?.shipment;

  const nextCheck = useMemo(() => {
    const times = [d?.case?.next_check_at, ...(d?.quotes || []).map((x) => x.next_check_at)].filter(Boolean) as string[];
    if (!times.length) return null;
    return times.sort()[0];
  }, [d]);

  const reload = () => {
    q.reload(); activity.reload();
    setDialog(null); setLegEdit(null); setLegCancel(null);
    setQuoteRequest(null); setQuoteReply(null); setQuoteForward(null); setQuoteBook(null); setQuoteDecline(null);
  };

  if (q.loading) return <PageLoading />;
  if (q.error) return <div className="page"><GlassPanel clip><DeniedOrError error={q.error} onRetry={q.reload} what="this shipment" backTo="/shipments" backLabel="Back to shipments" /></GlassPanel></div>;
  if (!d || !s) {
    return (
      <div className="page">
        <PageHeader title="Shipment not found" crumbs={[{ label: "Shipments", to: "/shipments" }, { label: id }]} />
        <GlassPanel clip><EmptyState title="Nothing recorded for this shipment" body="It may have been removed, or none of its vehicles are within your access." action={<Button size="sm" variant="soft" to="/shipments">Back to shipments</Button>} /></GlassPanel>
      </div>
    );
  }

  const health = shipmentHealth(s.status);
  const current = d.milestones?.current || [];
  const history = d.milestones?.history || [];
  const effective = d.milestones?.effective;

  const doCompare = async (quote: Quote) => {
    const res = await run(`quote-compare:${quote.id}`, quoteAction(quote.id, "compare"), {}, { success: "Compared against recorded quotes" });
    if (res) q.reload();
  };

  const menu: MenuItem[] = [
    { label: "Edit identifiers, route and ETA…", onSelect: () => setDialog("edit"), disabled: !write, disabledReason: writeReason },
    { label: "Record a milestone…", onSelect: () => setDialog("milestone"), disabled: !write, disabledReason: writeReason },
    { label: "Record the storage deadline…", onSelect: () => setDialog("storage"), disabled: !write, disabledReason: writeReason },
    { label: "Add a leg…", onSelect: () => setDialog("add-leg"), disabled: !write, disabledReason: writeReason, sepBefore: true },
    { label: "Start a quote case…", onSelect: () => setDialog("start-quote"), disabled: !write, disabledReason: writeReason },
  ];

  return (
    <div className="page page-wide">
      <PageHeader
        title={s.ref || `Shipment ${s.id.slice(0, 8)}`}
        crumbs={[{ label: "Shipments", to: "/shipments" }, { label: s.ref || s.id.slice(0, 8) }]}
        subtitle={
          <span className="row-wrap" style={{ gap: 6 }}>
            <span>{s.route_from || "origin not recorded"} → {s.route_to || "destination not recorded"}</span>
            <span>· {d.vehicles.length} vehicle{d.vehicles.length === 1 ? "" : "s"}</span>
            {nextCheck ? <span>· next check <When iso={nextCheck} tz={TZ.phoenix} format="datetime" /></span> : null}
            {s.updated_at ? <span>· updated {relativeTime(s.updated_at)}</span> : null}
          </span>
        }
        actions={
          <>
            <Button variant="primary" onClick={() => setDialog("milestone")} disabled={!write} disabledReason={writeReason}>Record milestone</Button>
            <Menu label="Shipment actions" align="right" items={menu} trigger={<Button variant="glass">Actions</Button>} />
          </>
        }
      >
        <div className="shp-head__tags">
          <Chip tone="soft">{SHIPMENT_LABEL[s.status] || s.status}</Chip>
          {health ? <HealthLabel health={health} label={s.status === "exception" ? "Exception" : SHIPMENT_LABEL[s.status] || s.status} /> : null}
          {s.container_no ? <Chip size="sm" tone="soft">Container {s.container_no}</Chip> : null}
          {d.case ? <Chip size="sm" tone="wait">Case {d.case.status}{d.case.waiting_on ? ` · waiting on ${d.case.waiting_on}` : ""}</Chip> : null}
        </div>
      </PageHeader>

      {s.exception_summary ? <Notice tone="blocked" lead="Exception">{s.exception_summary}</Notice> : null}
      {!costs ? <Notice tone="neutral" lead="Money hidden">Leg, quote and booking amounts are hidden for your role. Statuses, evidence and deadlines are not.</Notice> : null}

      <div className="shp-grid">
        {/* identifiers */}
        <GlassPanel className="shp-sec" radius="lg">
          <div className="shp-sec__title"><span>Shipment</span><span className="count">{s.ref || s.id.slice(0, 8)}</span></div>
          <dl className="shp-facts">
            <dt>Container</dt><dd>{s.container_no || <span className="not-recorded">Not recorded</span>}</dd>
            <dt>Vessel / voyage</dt><dd>{s.vessel || s.voyage ? `${s.vessel || "vessel not recorded"} · ${s.voyage || "voyage not recorded"}` : <span className="not-recorded">Not recorded</span>}</dd>
            <dt>Route</dt><dd>{s.route_from || s.route_to ? `${s.route_from || "?"} → ${s.route_to || "?"}` : <span className="not-recorded">Not recorded</span>}</dd>
            <dt>ETA</dt>
            <dd>
              <DualTime value={s.eta} />
              <div className="fs12 t3">{sourceLabel(s.eta_source, "estimate")}</div>
            </dd>
            <dt>Storage deadline</dt>
            <dd>
              {d.release_evidence?.storage_deadline?.utc ? (
                <>
                  <DualTime value={d.release_evidence.storage_deadline} />
                  <div className="fs12 t3">
                    {sourceLabel(d.release_evidence.source, "recorded from")}
                    {d.release_evidence.source_ref ? ` · ${d.release_evidence.source_ref}` : ""}
                    {d.release_evidence.note ? ` · ${d.release_evidence.note}` : ""}
                  </div>
                </>
              ) : <span className="not-recorded">Not recorded</span>}
            </dd>
            <dt>Exporter</dt>
            <dd>{s.exporter_contact_id ? <Link to={`/contacts/${encodeURIComponent(s.exporter_contact_id)}`}>Exporter contact</Link> : <span className="not-recorded">Not recorded</span>}</dd>
          </dl>
          <div className="shp-actions">
            <Button size="sm" variant="soft" onClick={() => setDialog("edit")} disabled={!write} disabledReason={writeReason}>Edit</Button>
            <Button size="sm" variant="soft" onClick={() => setDialog("storage")} disabled={!write} disabledReason={writeReason}>
              {d.release_evidence?.storage_deadline?.utc ? "Update the storage deadline" : "Record the storage deadline"}
            </Button>
          </div>
        </GlassPanel>

        {/* vehicles */}
        <GlassPanel className="shp-sec" radius="lg">
          <div className="shp-sec__title"><span>Vehicles on board</span><span className="count">{d.vehicles.length}</span></div>
          {d.vehicles.length === 0 ? (
            <EmptyState align="left" title="No vehicles recorded" body="Membership is what makes a container-wide notice apply to a truck." />
          ) : (
            <ul className="req__lines">
              {d.vehicles.map((v) => (
                <li key={v.id} className="req__line">
                  <span className="req__text"><Link to={`/vehicles/${encodeURIComponent(v.id)}`}>{v.title || v.stock_no || v.id}</Link></span>
                  <span className="req__meta">
                    {v.stock_no ? <span>{v.stock_no}</span> : null}
                    {v.logistics_state ? <Chip size="sm" tone="soft">{v.logistics_state.replace(/_/g, " ")}</Chip> : null}
                    {effective?.per_vehicle?.[v.id] ? (
                      <span className="t4">
                        {Object.values(effective.per_vehicle[v.id]).filter((m) => m.status === "completed").length} of {Object.keys(effective.per_vehicle[v.id]).length} milestones completed
                      </span>
                    ) : null}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {effective?.per_vehicle && Object.keys(effective.per_vehicle).length ? (
            <Expander title="Effective milestone per vehicle (a vehicle exception beats the container notice)">
              {Object.entries(effective.per_vehicle).map(([vid, kinds]) => (
                <div key={vid} className="stack-sm" style={{ marginBottom: 8 }}>
                  <strong className="fs13">{d.vehicles.find((v) => v.id === vid)?.title || vid}</strong>
                  {Object.entries(kinds).map(([kind, m]) => (
                    <div key={kind} className="fs12 t3">
                      {MILESTONE_LABEL[kind] || kind}: {m.status === "not_recorded" ? "not recorded" : m.status}
                      {m.at?.utc ? <> · <DualTime value={m.at} /></> : ""}
                      {m.scope ? ` · ${m.scope}` : ""}
                      {m.source_kind ? ` · source ${m.source_kind}` : ""}
                    </div>
                  ))}
                </div>
              ))}
            </Expander>
          ) : null}
        </GlassPanel>
      </div>

      {/* legs */}
      <GlassPanel clip radius="lg">
        <div className="shp-sec" style={{ paddingBottom: 0 }}>
          <div className="shp-sec__title"><span>Legs</span><span className="count">{d.legs.length}</span></div>
        </div>
        <LegsTable legs={d.legs} vehicles={d.vehicles} costs={costs} writeReason={writeReason} onUpdate={setLegEdit} onCancel={setLegCancel} />
        <div className="shp-sec" style={{ paddingTop: 0 }}>
          <div className="shp-actions">
            <Button size="sm" variant="soft" onClick={() => setDialog("add-leg")} disabled={!write} disabledReason={writeReason}>Add a leg</Button>
          </div>
        </div>
      </GlassPanel>

      {/* milestones */}
      <GlassPanel className="shp-sec" radius="lg">
        <div className="shp-sec__title"><span>Milestones</span><span className="count">{current.length} current · {history.length} superseded</span></div>
        <MilestoneList rows={current} vehicles={d.vehicles} />
        <div className="shp-actions">
          <Button size="sm" variant="soft" onClick={() => setDialog("milestone")} disabled={!write} disabledReason={writeReason}>Record a milestone</Button>
        </div>
        {history.length ? (
          <Expander title={`Superseded notices (${history.length})`}>
            <MilestoneList rows={history} vehicles={d.vehicles} superseded />
          </Expander>
        ) : null}
      </GlassPanel>

      {/* quotes */}
      <GlassPanel clip radius="lg">
        <div className="shp-sec" style={{ paddingBottom: 0 }}>
          <div className="shp-sec__title"><span>Shipping quotes</span><span className="count">{d.quotes.length}</span></div>
          <span className="fs12 t4">Requesting, forwarding and booking are three separate decisions. Permission for one never authorizes another.</span>
        </div>
        {d.quotes.length === 0 ? (
          <EmptyState
            title="No quote case yet"
            body="A case gathers the buyer, vehicle, route, dimensions, operability and timing from what is already recorded, and lists whatever is missing."
            action={<Button size="sm" variant="soft" onClick={() => setDialog("start-quote")} disabled={!write} disabledReason={writeReason}>Start a quote case</Button>}
          />
        ) : (
          d.quotes.map((quote) => (
            <QuoteCase
              key={quote.id}
              q={quote}
              costs={costs}
              writeReason={writeReason}
              busy={busy}
              onOpenApproval={openApproval}
              onRequest={() => setQuoteRequest(quote)}
              onRecordReply={() => setQuoteReply(quote)}
              onCompare={() => void doCompare(quote)}
              onForward={() => setQuoteForward(quote)}
              onBook={() => setQuoteBook(quote)}
              onDecline={() => setQuoteDecline(quote)}
            />
          ))
        )}
        {d.quotes.length ? (
          <div className="shp-sec" style={{ paddingTop: 0 }}>
            <div className="shp-actions">
              <Button size="sm" variant="soft" onClick={() => setDialog("start-quote")} disabled={!write} disabledReason={writeReason}>Start another quote case</Button>
            </div>
          </div>
        ) : null}
      </GlassPanel>

      <div className="shp-grid">
        {/* tasks + notes */}
        <GlassPanel className="shp-sec" radius="lg">
          <div className="shp-sec__title"><span>Open work</span><span className="count">{d.tasks.length}</span></div>
          {d.tasks.length === 0 ? <span className="not-recorded">No open tasks</span> : (
            <ul className="req__lines">
              {d.tasks.map((t) => (
                <li key={t.id} className="fs13">
                  <Link to={`/tasks/${encodeURIComponent(t.id)}`}>{t.title}</Link>
                  <span className="t4"> · {t.status.replace(/_/g, " ")}</span>
                  {t.due_at ? <> · <When iso={t.due_at} tz={TZ.phoenix} format="datetime" /></> : null}
                </li>
              ))}
            </ul>
          )}
          {d.case ? (
            <div className="fs12 t3">
              Case: {d.case.summary || d.case.status}
              {d.case.next_action ? ` · next: ${d.case.next_action}` : ""}
              {d.case.next_check_at ? <> · checks again <When iso={d.case.next_check_at} tz={TZ.phoenix} format="datetime" /></> : null}
            </div>
          ) : null}
          {s.notes ? <><div className="shp-sec__title"><span>Notes</span></div><p className="irq-notes">{s.notes}</p></> : null}
        </GlassPanel>

        {/* activity */}
        <GlassPanel className="shp-sec" radius="lg">
          <div className="shp-sec__title"><span>Activity</span><span className="count">{(activity.data?.items || []).length}</span></div>
          <ActivityList rows={activity.data?.items || []} denied={activity.data === null && !activity.loading} loading={activity.loading} />
        </GlassPanel>
      </div>

      {/* dialogs */}
      <EditShipmentDialog open={dialog === "edit"} onClose={() => setDialog(null)} s={s} onDone={reload} />
      <RecordMilestoneDialog open={dialog === "milestone"} onClose={() => setDialog(null)} s={s} vehicles={d.vehicles} onDone={reload} />
      <StorageDeadlineDialog open={dialog === "storage"} onClose={() => setDialog(null)} s={s} onDone={reload} />
      <AddLegDialog open={dialog === "add-leg"} onClose={() => setDialog(null)} s={s} vehicles={d.vehicles} onDone={reload} />
      <StartQuoteDialog open={dialog === "start-quote"} onClose={() => setDialog(null)} s={s} vehicles={d.vehicles} onDone={reload} />
      <LegUpdateDialog open={!!legEdit} leg={legEdit} onClose={() => setLegEdit(null)} onDone={reload} />
      <LegCancelDialog open={!!legCancel} leg={legCancel} onClose={() => setLegCancel(null)} onDone={reload} />
      <RequestQuoteDialog open={!!quoteRequest} q={quoteRequest} onClose={() => setQuoteRequest(null)} onDone={reload} />
      <RecordReplyDialog open={!!quoteReply} q={quoteReply} onClose={() => setQuoteReply(null)} onDone={reload} />
      <ForwardQuoteDialog open={!!quoteForward} q={quoteForward} onClose={() => setQuoteForward(null)} onDone={reload} />
      <BookQuoteDialog open={!!quoteBook} q={quoteBook} vehicles={d.vehicles} onClose={() => setQuoteBook(null)} onDone={reload} />
      <DeclineQuoteDialog open={!!quoteDecline} q={quoteDecline} onClose={() => setQuoteDecline(null)} onDone={reload} />
    </div>
  );
}

function ActivityList({ rows, denied, loading }: { rows: ActivityRow[]; denied: boolean; loading: boolean }) {
  if (loading) return <Loading label="Loading activity" rows={3} />;
  if (denied) return <p className="fs13 t4" style={{ margin: 0 }}>Activity is hidden for your role.</p>;
  if (!rows.length) return <span className="not-recorded">Nothing recorded yet</span>;
  const node = (a: ActivityRow): ReactNode => (
    <span className={["irq-act__what", a.exception ? "irq-act__what--exc" : ""].filter(Boolean).join(" ")}>
      {a.what}{a.state ? <span className="t4"> · {a.state.replace(/_/g, " ")}</span> : null}
    </span>
  );
  return (
    <div className="irq-act">
      {rows.slice(0, 40).map((a) => (
        <div key={a.id} className="irq-act__row">
          <span className="irq-act__time"><When iso={a.at} tz={TZ.phoenix} format="long" /></span>
          {node(a)}
        </div>
      ))}
    </div>
  );
}
