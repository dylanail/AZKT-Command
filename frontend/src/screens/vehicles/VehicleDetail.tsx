/* Vehicle detail (spec §2.3, route /vehicles/:id?tab=).
   Header: photo, title, stock · frame · allocation, one health label with its exception, the primary
   next action, Add update (intake in existing mode), Ask about this, and a More menu.
   Tabs: Overview · Work · Files · Sale · Money. Money appears only with costs.read and tolerates 403/404.
   Facts carry a Source button that opens the single inspector in source-details mode with the provenance. */
import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { can, isEmployeeRole, whyNot } from "../../lib/perms";
import { useQuery } from "../../lib/useQuery";
import { useIsMobile } from "../../lib/viewport";
import { useInspector } from "../../app/Inspector";
import {
  Button, Chip, EmptyState, ErrorState, Expander, Field, GlassPanel, HealthLabel, Input, KeyValues, Loading,
  Menu, Money, MoveStageButton, Notice, NotRecorded, PageHeader, ResponsiveDialog, SegmentedControl, Select,
  TabPanel, Tabs, Textarea, When, useToast, type MenuItem,
} from "../../ui";
import { TaskRow } from "../tasks/TaskRow";
import { NewTaskDialog, ReasonDialog, RescheduleSheet, AssignSheet, useTaskDialogs } from "../tasks/TaskSheets";
import { usePeople } from "../tasks/usePeople";
import type { TaskView } from "../tasks/types";
import FactRow from "./components/FactRow";
import GateDialog, { GateList } from "./components/GateDialog";
import MilestoneList from "./components/MilestoneList";
import PartsChain from "./components/PartsChain";
import PhotoSlots from "./components/PhotoSlots";
import { VehicleThumb } from "./components/HealthRow";
import {
  fetchGates, issuePath, movePath, partsPath, useVehicle, useVehicleCommand, useVehicleMoney, vehiclePath, workOrderPath,
} from "./components/useVehicle";
import {
  CLASSIFICATION_LABELS, CONDITION_SOURCE_LABELS, ISSUE_STATUS_LABELS, MILESTONE_LABELS, MILESTONE_ORDER,
  RECON_STATES, SEVERITY_LABELS, SHOP_STAGES, allocationLabel, situationOf, stateLabel, vehicleHealth,
  type ConditionBullet, type GateItem, type GateTaskRef, type IssueData, type MoveResult, type VehicleDetailResp,
} from "./types";
import "./vehicles.css";
import "../tasks/tasks.css";

type Tab = "overview" | "work" | "files" | "sale" | "money";
const TAB_IDS: Tab[] = ["overview", "work", "files", "sale", "money"];

export default function VehicleDetail() {
  const { id = "" } = useParams();
  const { user } = useAuth();
  const mobile = useIsMobile();
  const insp = useInspector();
  const { toast } = useToast();
  const [params, setParams] = useSearchParams();
  const employee = isEmployeeRole(user?.role);
  const people = usePeople(!employee);
  const cmd = useVehicleCommand();

  const { detail, activity, reload, tick } = useVehicle(id);
  const costsRead = can(user, "costs.read");
  const money = useVehicleMoney(id, costsRead, tick);

  const raw = params.get("tab");
  const wanted: Tab = (TAB_IDS as string[]).includes(raw || "") ? (raw as Tab) : "overview";
  // A tab the role can't open falls back to Overview rather than showing an empty panel.
  const tab: Tab = (wanted === "money" && !can(user, "costs.read")) || (wanted === "sale" && employee) ? "overview" : wanted;
  const setTab = (t: Tab) => {
    const next = new URLSearchParams(params);
    if (t === "overview") next.delete("tab"); else next.set("tab", t);
    setParams(next, { replace: true });
  };

  const [gate, setGate] = useState<{ from: string; to: string; gates: GateItem[]; tasks: GateTaskRef[] } | null>(null);
  const [backward, setBackward] = useState<string | null>(null);

  const d = detail.data;
  const v = d?.vehicle;

  const doMove = useCallback(async (to: string, opts: { reason?: string; overrides?: Record<string, string> } = {}) => {
    if (!v) return false;
    const body: Record<string, unknown> = { to_state: to, source: "button", expected_version: v.version };
    if (opts.reason) body.reason = opts.reason;
    if (opts.overrides) body.overrides = opts.overrides;
    const out = await cmd.run<MoveResult>("move", movePath(v.id), body, { quietBlocked: true });
    if (out.error) return false;
    const data = out.data;
    if (data && data.decision === "Blocked") {
      setGate({ from: data.from, to: data.to, gates: data.gates || [], tasks: data.tasks || [] });
      return false;
    }
    if (out.ok && data?.moved) {
      toast({ message: `Moved to ${stateLabel(to)}`, tone: "ok" });
      setGate(null);
      reload();
      return true;
    }
    if (out.ok && data && !data.moved) toast({ message: data.reasons?.[0] || "Nothing changed.", tone: "risk" });
    return false;
  }, [cmd, reload, toast, v]);

  const requestMove = useCallback((to: string) => {
    if (!v) return;
    const from = v.recon_state;
    const back = RECON_STATES.indexOf(to as (typeof RECON_STATES)[number]) < RECON_STATES.indexOf(from as (typeof RECON_STATES)[number]);
    if (back) { setBackward(to); return; }
    void doMove(to);
  }, [doMove, v]);

  /* ---------- loading / error / missing ---------- */
  const crumbs = [{ label: employee ? "Vehicles you work on" : "Vehicles", to: "/vehicles" }, { label: v?.title || (v?.stock_no ?? id) }];
  if (detail.loading) {
    return (
      <div className="page page-wide">
        <PageHeader crumbs={crumbs} title={<span className="skeleton" style={{ display: "inline-block", width: 240, height: 28 }} />} />
        <GlassPanel clip><Loading label="Loading vehicle" rows={5} /></GlassPanel>
      </div>
    );
  }
  if (detail.error) {
    return (
      <div className="page page-wide">
        <PageHeader crumbs={crumbs} title="Vehicle" />
        <ErrorState error={detail.error} onRetry={detail.reload} />
      </div>
    );
  }
  if (!d || !v) {
    return (
      <div className="page page-wide">
        <PageHeader crumbs={crumbs} title="Vehicle not found" />
        <GlassPanel clip>
          <EmptyState
            title="Vehicle not found"
            body={`Nothing recorded for ${id}. It may have been archived, or it isn't yours to see.`}
            action={<Button variant="soft" to="/vehicles">Back to vehicles</Button>}
          />
        </GlassPanel>
      </div>
    );
  }

  const health = vehicleHealth(v);
  const tasks = d.tabs.work.tasks || [];
  const nextTask = v.next_action ? tasks.find((t) => t.title === v.next_action) : undefined;
  const canWrite = can(user, "vehicles.write");
  const canIntake = can(user, "intake");

  const tabs = [
    { id: "overview" as const, label: "Overview" },
    { id: "work" as const, label: "Work", count: tasks.filter((t) => !["completed", "cancelled"].includes(t.status)).length || undefined },
    { id: "files" as const, label: "Files", count: d.tabs.files.photos.length + d.tabs.files.documents.length || undefined },
    ...(employee ? [] : [{ id: "sale" as const, label: "Sale" }]),
    ...(costsRead ? [{ id: "money" as const, label: "Money" }] : []),
  ];

  const moreItems: MenuItem[] = [
    { label: "Open the shop board", to: "/vehicles?view=shop&layout=board" },
    { label: "Full activity", to: `/activity?entity_kind=vehicle&entity_id=${encodeURIComponent(v.id)}` },
    { label: v.archived_at ? "Restore vehicle" : "Archive vehicle", sepBefore: true, disabled: !canWrite, disabledReason: whyNot("vehicles.write"),
      onSelect: () => { void archive(); } },
  ];

  async function archive() {
    if (!v) return;
    const action = v.archived_at ? "restore" : "archive";
    const out = await cmd.run(action, vehiclePath(v.id, action), { expected_version: v.version },
      { okMessage: v.archived_at ? "Vehicle restored" : "Vehicle archived. It stays searchable." });
    if (out.ok) reload();
  }

  /* Ask about this truck. The office desktop opens the inspector; phones — and employees, who have no
     inspector in their shell — open the Agents screen with the pinned context it reads
     (?context=kind:id&label=). */
  const askLabel = v.stock_no || v.title || "this truck";
  const askHref = `/agents?context=${encodeURIComponent(`vehicle:${v.id}`)}&label=${encodeURIComponent(askLabel)}`;
  const askAbout = () => insp.openAsk({ label: askLabel, href: `/vehicles/${v.id}` });
  const canAsk = can(user, "agents.chat");
  const askReason = canAsk ? undefined : "Talking to AZKT is turned off for your account.";
  const askOnAgentsScreen = mobile || employee;

  return (
    <div className="page page-wide">
      <nav className="crumbs" aria-label="Breadcrumb">
        <Link to="/vehicles">{employee ? "Vehicles you work on" : "Vehicles"}</Link>
        <span aria-hidden="true">›</span>
        <span className="truncate" style={{ color: "var(--t2)" }}>{v.stock_no || v.title}</span>
      </nav>

      <header className="vh-header">
        <VehicleThumb assetId={v.hero_asset_id} size={mobile ? "lg" : "xl"} />
        <div className="vh-header__main">
          <h1>{v.title || "Vehicle"}</h1>
          <div className="vh-header__sub">
            {[v.stock_no || "No stock number", v.frame_no_raw || "Frame not recorded", employee ? v.location || "Location not recorded" : allocationLabel(v.allocation)].join(" · ")}
          </div>
          <div className="row-wrap" style={{ gap: 12 }}>
            <HealthLabel health={health.health} label={health.label} size="md" dot />
            {v.exception ? <span className="t2">{v.exception}</span> : null}
            {v.intake_status && v.intake_status !== "complete" ? (
              <Chip size="sm" tone="amber">Intake {v.intake_status.replace(/_/g, " ")}</Chip>
            ) : null}
            {v.archived_at ? <Chip size="sm" tone="risk">Archived</Chip> : null}
          </div>
          <div className="vh-header__actions">
            {nextTask ? (
              <Button variant="primary" to={`/tasks/${encodeURIComponent(nextTask.id)}`}>{v.next_action}</Button>
            ) : v.next_action ? (
              <Button variant="primary" onClick={() => setTab("work")}>{v.next_action}</Button>
            ) : (
              <Button variant="primary" onClick={() => setTab("work")} disabled={!tasks.length} disabledReason="No next action recorded for this vehicle.">
                {tasks.length ? "Open the work" : "No next action"}
              </Button>
            )}
            <Button
              variant="glass"
              to={canIntake ? `/vehicles/${encodeURIComponent(v.id)}/intake` : undefined}
              disabled={!canIntake}
              disabledReason="Your role can't add photo or voice updates."
            >
              Add update
            </Button>
            {askOnAgentsScreen ? (
              <Button variant="glass" to={canAsk ? askHref : undefined} disabled={!canAsk} disabledReason={askReason}>Ask about this truck</Button>
            ) : (
              <Button variant="glass" onClick={askAbout} disabled={!canAsk} disabledReason={askReason}>Ask about this truck</Button>
            )}
            {!employee ? (
              <MoveStageButton
                stages={SHOP_STAGES}
                current={v.recon_state}
                size="md"
                busy={cmd.busy("move")}
                disabledReason={!canWrite ? "Your role can't move vehicles between stages." : v.archived_at ? "This vehicle is archived." : undefined}
                onMove={requestMove}
              />
            ) : null}
            <Menu label="More actions" align="right" items={moreItems} trigger={<Button variant="glass" aria-label="More actions">···</Button>} />
          </div>
        </div>
      </header>

      {v.health_reason && v.health !== "ok" ? (
        <Notice tone={health.health === "blocked" ? "blocked" : health.health === "risk" ? "risk" : "wait"} lead={health.label} role={health.health === "blocked" ? "alert" : "status"}>
          {v.health_reason}
        </Notice>
      ) : null}

      {mobile ? (
        <SegmentedControl<Tab>
          label="Vehicle sections"
          block
          size="sm"
          value={tab}
          onChange={setTab}
          options={tabs.map((t) => ({ value: t.id, label: t.label }))}
        />
      ) : (
        <Tabs<Tab> label="Vehicle sections" idPrefix="veh" tabs={tabs} value={tab} onChange={setTab} />
      )}

      <TabPanel id="overview" idPrefix="veh" active={tab === "overview"}>
        <OverviewTab d={d} activity={activity.data?.items || null} activityDenied={activity.data === null && !activity.loading} reload={reload} cmd={cmd} canWrite={canWrite} isOwner={user?.role === "owner"} />
      </TabPanel>

      <TabPanel id="work" idPrefix="veh" active={tab === "work"}>
        <WorkTab d={d} reload={reload} cmd={cmd} nameOf={people.nameOf} />
      </TabPanel>

      <TabPanel id="files" idPrefix="veh" active={tab === "files"}>
        <FilesTab d={d} reload={reload} canWrite={canWrite || canIntake} />
      </TabPanel>

      {employee ? null : (
        <TabPanel id="sale" idPrefix="veh" active={tab === "sale"}>
          <SaleTab d={d} reload={reload} cmd={cmd} isOwner={user?.role === "owner"} costsRead={costsRead}
            onMoveReady={() => requestMove("ready_for_sale")} moving={cmd.busy("move")} canWrite={canWrite} />
        </TabPanel>
      )}

      {costsRead ? (
        <TabPanel id="money" idPrefix="veh" active={tab === "money"}>
          <MoneyTabView state={money} vehicle={d} />
        </TabPanel>
      ) : null}

      {gate ? (
        <GateDialog
          open
          onClose={() => setGate(null)}
          from={gate.from}
          to={gate.to}
          gates={gate.gates}
          tasks={gate.tasks}
          busy={cmd.busy("move")}
          onOverride={async (overrides) => { await doMove(gate.to, { overrides }); }}
          extraAction={<Button size="sm" variant="ghost" onClick={() => { setGate(null); setTab("work"); }}>Open Work</Button>}
        />
      ) : null}

      <ReasonDialog
        open={!!backward}
        onClose={() => setBackward(null)}
        title="Move back a stage"
        description={backward ? `${v.title} → ${stateLabel(backward)}` : undefined}
        label="Why is it going back?"
        placeholder="e.g. new damage found during finalization"
        confirmLabel="Move back"
        required
        onConfirm={async (reason) => {
          if (!backward) return false;
          return doMove(backward, { reason });
        }}
      />
    </div>
  );
}

/* ================= Overview ================= */
function OverviewTab({ d, activity, activityDenied, reload, cmd, canWrite, isOwner }: {
  d: VehicleDetailResp;
  activity: Array<{ id: string; at?: string | null; what?: string; exception?: boolean; actor?: { display_name?: string | null } | null }> | null;
  activityDenied: boolean;
  reload: () => void;
  cmd: ReturnType<typeof useVehicleCommand>;
  canWrite: boolean;
  isOwner: boolean;
}) {
  const v = d.vehicle;
  const o = d.tabs.overview;
  const [editing, setEditing] = useState<ConditionBullet | null>(null);
  const [adding, setAdding] = useState(false);
  const [milestone, setMilestone] = useState(false);

  const links: Array<{ label: string; to: string; meta: string }> = [];
  for (const s of d.tabs.work.shipments || []) {
    links.push({ label: `Shipment ${s.ref || s.id}`, to: `/shipments/${encodeURIComponent(s.id)}`, meta: s.status ? stateLabel(s.status) : "Status not recorded" });
  }
  if (d.tabs.sale.buyer_contact_id) links.push({ label: "Buyer contact", to: `/contacts/${encodeURIComponent(d.tabs.sale.buyer_contact_id)}`, meta: "Contact" });
  for (const op of d.tabs.sale.opportunities || []) {
    links.push({ label: `Lead ${op.stage ? `· ${stateLabel(op.stage)}` : ""}`.trim(), to: `/sales?lead=${encodeURIComponent(op.id)}`, meta: op.pipeline || "Sales" });
  }
  if (v.origin_candidate_id) links.push({ label: "Auction candidate", to: `/candidates/${encodeURIComponent(v.origin_candidate_id)}`, meta: "Sourcing" });

  const saveBullet = async (text: string, bullet: ConditionBullet | null) => {
    if (bullet) {
      const out = await cmd.run("bullet", vehiclePath(v.id, "condition_bullet"),
        { bullet_id: bullet.id, text, expected_version: v.version }, { okMessage: "Condition updated. The earlier wording is kept in history." });
      if (out.ok) { reload(); return true; }
      return false;
    }
    const out = await cmd.run("bullet", vehiclePath(v.id, "condition"),
      { bullets: [{ text, source: "owner_reported" }], mode: "append" }, { okMessage: "Condition bullet added" });
    if (out.ok) { reload(); return true; }
    return false;
  };
  const removeBullet = async (bullet: ConditionBullet) => {
    const out = await cmd.run("bullet", vehiclePath(v.id, "condition_bullet"),
      { bullet_id: bullet.id, remove: true, expected_version: v.version }, { okMessage: "Removed from the current summary. The evidence is kept." });
    if (out.ok) reload();
  };

  const verifyFact = isOwner
    ? (f: { key: string; value: string | null; id: string }) => {
      void cmd.run("fact", vehiclePath(v.id, "confirm_fact"), { fact_id: f.id, key: f.key }, { okMessage: `${f.key.replace(/_/g, " ")} confirmed` })
        .then((out) => { if (out.ok) reload(); });
    }
    : undefined;

  return (
    <div className="stack-lg">
      <div className="vh-two">
        <section className="vh-sect">
          <div className="vh-sect__head"><h2>Facts <span className="count">{o.facts.length}</span></h2></div>
          {o.facts.length ? (
            o.facts.map((f) => <FactRow key={f.id} fact={f} all={o.facts} onVerify={verifyFact ? () => verifyFact(f) : undefined} />)
          ) : (
            <EmptyState align="left" title="No facts recorded" body="Facts arrive from intake, documents and auction sheets, each with its source." />
          )}
          {o.identity.missing_identity_fields?.length ? (
            <Notice tone="amber" lead="Intake incomplete">
              Missing: {o.identity.missing_identity_fields.map((k) => k.replace(/_/g, " ")).join(", ")}. Nothing is guessed — a focused task tracks each one.
            </Notice>
          ) : null}
        </section>

        <div className="stack-lg">
          <section className="vh-sect">
            <div className="vh-sect__head"><h2>Situation</h2></div>
            <div className="fs14">{situationOf({ situation: o.situation, states: o.states })}</div>
            <KeyValues items={[
              ["Logistics", stateLabel(o.states.logistics)],
              ["Shop", stateLabel(o.states.recon)],
              ["Commercial", stateLabel(o.states.commercial)],
              ["Documents", stateLabel(o.states.documents)],
              ["Next", o.health.next_action || <NotRecorded text="No next action recorded" />],
              ["Due", o.health.due_at ? <When iso={o.health.due_at} format="long" /> : <NotRecorded text="No time set" />],
            ]} />
          </section>

          <section className="vh-sect">
            <div className="vh-sect__head"><h2>Linked records</h2></div>
            {links.length ? links.map((l, i) => (
              <div key={i} className="vh-link">
                <Link to={l.to} className="wrap">{l.label}</Link>
                <span className="vh-link__meta">{l.meta}</span>
              </div>
            )) : <span className="not-recorded">Nothing linked yet.</span>}
          </section>

          <section className="vh-sect">
            <div className="vh-sect__head"><h2>Recent activity</h2></div>
            {activityDenied ? (
              <span className="not-recorded">Activity isn't available for your role.</span>
            ) : activity === null ? (
              <Loading rows={2} label="Loading activity" />
            ) : activity.length === 0 ? (
              <span className="not-recorded">No activity recorded yet.</span>
            ) : (
              <>
                {activity.slice(0, 8).map((a) => (
                  <div key={a.id} className="vh-act">
                    <span className="vh-act__time">{a.at ? <When iso={a.at} format="datetime" /> : "—"}</span>
                    <span className="vh-act__what" style={a.exception ? { color: "var(--risk)" } : undefined}>
                      {a.what}
                      {a.actor?.display_name ? <span className="t4"> · {a.actor.display_name}</span> : null}
                    </span>
                  </div>
                ))}
                <Link to={`/activity?entity_kind=vehicle&entity_id=${encodeURIComponent(v.id)}`} className="fs13">View full activity</Link>
              </>
            )}
          </section>
        </div>
      </div>

      <section className="vh-sect">
        <div className="vh-sect__head">
          <h2>Condition at intake <span className="count">{o.condition_version ? `v${o.condition_version}` : ""}</span></h2>
          <Button size="sm" variant="soft" onClick={() => setAdding(true)} disabled={!canWrite} disabledReason={whyNot("vehicles.write")}>Add bullet</Button>
        </div>
        {o.condition.length ? o.condition.map((b) => (
          <div key={b.id} className="vh-bullet">
            <span className="vh-bullet__dot" aria-hidden="true">•</span>
            <span className="vh-bullet__body">
              <span>{b.text}</span>
              <span className="vh-bullet__src">
                {CONDITION_SOURCE_LABELS[b.source] || b.source}
                {b.evidence?.length ? ` · ${b.evidence.length} photo${b.evidence.length === 1 ? "" : "s"}` : ""}
                {b.added_at ? <> · <When iso={b.added_at} format="date" /></> : null}
                {b.version && b.version > 1 ? ` · v${b.version}` : ""}
              </span>
            </span>
            <span className="vh-bullet__acts">
              <button type="button" className="linklike fs12" disabled={!canWrite} onClick={() => setEditing(b)}>Edit</button>
              <button type="button" className="linklike fs12" disabled={!canWrite} onClick={() => void removeBullet(b)}>Remove</button>
            </span>
          </div>
        )) : (
          <EmptyState align="left" title="No condition recorded" body="Book the vehicle in with photos and a note and the bullets appear here with their sources." />
        )}
      </section>

      <section className="vh-sect">
        <div className="vh-sect__head">
          <h2>Milestones</h2>
          <Button size="sm" variant="soft" onClick={() => setMilestone(true)} disabled={!canWrite} disabledReason={whyNot("vehicles.write")}>Record milestone</Button>
        </div>
        <MilestoneList milestones={o.milestones} showMissing />
        <span className="fs12 t4">Planned, estimated and completed are different things. A milestone with no date reads "Not recorded" — nothing is guessed.</span>
      </section>

      <Expander title="Sources and technical details">
        <KeyValues items={[
          ["Vehicle id", v.id],
          ["Record version", `v${v.version}`],
          ["Stock number", v.stock_no || "Not allocated"],
          ["Frame (raw)", v.frame_no_raw || "Not recorded"],
          ["Frame (search)", v.frame_no_norm || "Not recorded"],
          ["Intake status", v.intake_status || "Not recorded"],
          ["Updated", v.updated_at ? <When iso={v.updated_at} format="long" /> : "Not recorded"],
        ]} />
      </Expander>

      <BulletDialog
        open={adding || !!editing}
        bullet={editing}
        onClose={() => { setAdding(false); setEditing(null); }}
        onSave={async (text) => {
          const ok = await saveBullet(text, editing);
          if (ok) { setAdding(false); setEditing(null); }
          return ok;
        }}
      />
      <MilestoneDialog
        open={milestone}
        onClose={() => setMilestone(false)}
        onSave={async (kind, status, at, note) => {
          const body: Record<string, unknown> = { kind, status, source_kind: "owner_reported", expected_version: v.version };
          if (at) body.at = at;
          if (note) body.note = note;
          const out = await cmd.run("milestone", vehiclePath(v.id, "milestone"), body, { okMessage: "Milestone recorded with its source" });
          if (out.ok) { reload(); return true; }
          return false;
        }}
      />
    </div>
  );
}

function MilestoneDialog({ open, onClose, onSave }: {
  open: boolean; onClose: () => void;
  onSave: (kind: string, status: string, at: string | null, note: string) => Promise<boolean>;
}) {
  const mobile = useIsMobile();
  const [kind, setKind] = useState("received");
  const [status, setStatus] = useState("completed");
  const [date, setDate] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) { setKind("received"); setStatus("completed"); setDate(""); setNote(""); } }, [open]);
  const at = date ? new Date(`${date}T12:00:00`).toISOString() : null;
  const future = !!at && new Date(at).getTime() > Date.now();
  const invalid = status === "completed" && future;
  return (
    <ResponsiveDialog
      mobile={mobile} open={open} onClose={onClose} title="Record milestone" size="sm"
      description="Recorded as owner-reported with your name. It does not imply inspection, payment or readiness."
      footer={
        <>
          <Button variant="primary" loading={busy} disabled={invalid} disabledReason="A completed milestone can't be in the future — record it as planned or estimated."
            onClick={async () => { setBusy(true); const ok = await onSave(kind, status, at, note.trim()); setBusy(false); if (ok) onClose(); }}>Save</Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <div className="stack">
        <Field label="Milestone" required>
          <Select value={kind} onChange={(e) => setKind(e.target.value)}>
            {MILESTONE_ORDER.map((k) => <option key={k} value={k}>{MILESTONE_LABELS[k] || k}</option>)}
          </Select>
        </Field>
        <div className="tk-grid2">
          <Field label="Status">
            <Select value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="completed">Completed</option>
              <option value="estimated">Estimated</option>
              <option value="planned">Planned</option>
            </Select>
          </Field>
          <Field label="Date" hint="Leave empty for “Not recorded”.">
            <Input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
          </Field>
        </div>
        <Field label="Note"><Input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Optional, e.g. arrived at the shop" /></Field>
      </div>
    </ResponsiveDialog>
  );
}

function BulletDialog({ open, bullet, onClose, onSave }: { open: boolean; bullet: ConditionBullet | null; onClose: () => void; onSave: (text: string) => Promise<boolean> }) {
  const mobile = useIsMobile();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) setText(bullet?.text || ""); }, [open, bullet]);
  return (
    <ResponsiveDialog
      mobile={mobile}
      open={open}
      onClose={onClose}
      title={bullet ? "Edit condition bullet" : "Add condition bullet"}
      description={bullet ? "The current summary is updated; the earlier wording stays in history." : "Recorded as owner-reported with your name."}
      size="sm"
      footer={
        <>
          <Button variant="primary" loading={busy} disabled={!text.trim()} disabledReason="Write the bullet first."
            onClick={async () => { setBusy(true); await onSave(text.trim()); setBusy(false); }}>Save</Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <Field label="Condition" required>
        <Textarea rows={3} value={text} onChange={(e) => setText(e.target.value)} placeholder="e.g. Dent on the left door, paint intact" maxLength={500} />
      </Field>
    </ResponsiveDialog>
  );
}

/* ================= Work ================= */
function WorkTab({ d, reload, cmd, nameOf }: {
  d: VehicleDetailResp; reload: () => void; cmd: ReturnType<typeof useVehicleCommand>; nameOf: (id: string | null | undefined) => string;
}) {
  const { user } = useAuth();
  const v = d.vehicle;
  const w = d.tabs.work;
  const dialogs = useTaskDialogs();
  const [newTask, setNewTask] = useState(false);
  const [defer, setDefer] = useState<IssueData | null>(null);
  const [resolve, setResolve] = useState<IssueData | null>(null);
  const [newPart, setNewPart] = useState(false);
  const [newWo, setNewWo] = useState(false);
  const canTasks = can(user, "tasks.write");
  const canParts = can(user, "parts.request");
  const perm = (p: string) => can(user, p);

  const open = (t: TaskView) => !["completed", "cancelled"].includes(t.status);
  const activeTasks = w.tasks.filter(open);
  const doneTasks = w.tasks.filter((t) => !open(t));
  const openIssues = w.recon_issues.filter((i) => ["open", "in_progress"].includes(i.status));
  const closedIssues = w.recon_issues.filter((i) => !["open", "in_progress"].includes(i.status));

  return (
    <div className="stack-lg">
      <section className="vh-sect">
        <div className="vh-sect__head">
          <h2>Tasks <span className="count">{activeTasks.length} open</span></h2>
          <Button size="sm" variant="soft" onClick={() => setNewTask(true)} disabled={!canTasks} disabledReason={whyNot("tasks.write")}>Add task</Button>
        </div>
        <GlassPanel clip>
          {w.tasks.length ? (
            <>
              {activeTasks.map((t) => (
                <TaskRow key={t.id} task={t} dialogs={dialogs} onChanged={() => reload()} ownerName={nameOf(t.owner_user_id)} about={null} />
              ))}
              {doneTasks.length ? (
                <Expander title={`Finished (${doneTasks.length})`} className="vh-sect" >
                  {doneTasks.map((t) => (
                    <TaskRow key={t.id} task={t} dialogs={dialogs} onChanged={() => reload()} ownerName={nameOf(t.owner_user_id)} about={null} />
                  ))}
                </Expander>
              ) : null}
            </>
          ) : (
            <EmptyState title="No tasks on this vehicle" body="Add one, or book in an update and AZKT proposes the work." />
          )}
        </GlassPanel>
      </section>

      <section className="vh-sect">
        <div className="vh-sect__head">
          <h2>Recon issues <span className="count">{openIssues.length} open</span></h2>
        </div>
        <GlassPanel clip>
          {w.recon_issues.length ? [...openIssues, ...closedIssues].map((i) => (
            <div key={i.id} className="vh-item">
              <div className="vh-item__main">
                <div className="vh-item__title">{i.title}</div>
                <div className="vh-item__meta">
                  {SEVERITY_LABELS[i.severity] || i.severity} · {ISSUE_STATUS_LABELS[i.status] || i.status}
                  {i.source_kind ? ` · ${i.source_kind.replace(/_/g, " ")}` : ""}
                  {i.disclosure_required ? " · must be disclosed" : ""}
                  {i.detail ? ` · ${i.detail}` : ""}
                  {i.deferred_reason ? ` · deferred: ${i.deferred_reason}` : ""}
                  {i.resolution_note ? ` · ${i.resolution_note}` : ""}
                </div>
                {i.task_id ? <Link to={`/tasks/${encodeURIComponent(i.task_id)}`} className="fs13">Open the linked task</Link> : null}
              </div>
              <div className="vh-item__acts">
                {["open", "in_progress"].includes(i.status) ? (
                  <>
                    <Button size="xs" variant="primary" onClick={() => setResolve(i)} disabled={!canTasks} disabledReason={whyNot("tasks.write")}>Resolve</Button>
                    <Button size="xs" variant="soft" onClick={() => setDefer(i)} disabled={!canTasks} disabledReason={whyNot("tasks.write")}>Defer</Button>
                  </>
                ) : (
                  <span className="fs13 t3">{ISSUE_STATUS_LABELS[i.status] || i.status}</span>
                )}
              </div>
            </div>
          )) : <EmptyState title="No recon issues" body="Findings from an inspection or an intake note land here." />}
        </GlassPanel>
      </section>

      <section className="vh-sect">
        <div className="vh-sect__head">
          <h2>Work orders <span className="count">{w.work_orders.length}</span></h2>
          <Button size="sm" variant="soft" onClick={() => setNewWo(true)} disabled={!canTasks} disabledReason={whyNot("tasks.write")}>Add work order</Button>
        </div>
        <GlassPanel clip>
          {w.work_orders.length ? w.work_orders.map((o) => (
            <div key={o.id} className="vh-item">
              <div className="vh-item__main">
                <div className="vh-item__title">{o.ref ? `${o.ref} · ` : ""}{o.title}</div>
                <div className="vh-item__meta">{stateLabel(o.status)} · {nameOf(o.assignee_user_id)}{o.notes ? ` · ${o.notes}` : ""}</div>
              </div>
              <div className="vh-item__acts">{o.cost_item_id ? <Chip size="sm" tone="soft">Cost linked</Chip> : null}</div>
            </div>
          )) : <EmptyState title="No work orders" body="A work order groups the labour and parts for one job." />}
        </GlassPanel>
      </section>

      <section className="vh-sect">
        <div className="vh-sect__head">
          <h2>Parts <span className="count">{w.parts.length}</span></h2>
          <Button size="sm" variant="soft" onClick={() => setNewPart(true)} disabled={!canParts} disabledReason="Your role can't request parts.">Request part</Button>
        </div>
        <GlassPanel clip>
          {w.parts.length ? w.parts.map((p) => (
            <PartsChain key={p.id} part={p} vehicleId={v.id} can={perm} onChanged={reload} />
          )) : <EmptyState title="No parts recorded" body="Requested parts move Requested → Approved/Ordered → Arrived → Installed → Verified." />}
        </GlassPanel>
      </section>

      <section className="vh-sect">
        <div className="vh-sect__head"><h2>Shipment</h2></div>
        {w.shipments?.length ? w.shipments.map((s) => (
          <div key={s.id} className="vh-link">
            <Link to={`/shipments/${encodeURIComponent(s.id)}`} className="wrap">
              {s.ref || s.id}{s.vessel ? ` · ${s.vessel}` : ""}{s.container_no ? ` · ${s.container_no}` : ""}
            </Link>
            <span className="vh-link__meta">
              {s.status ? stateLabel(s.status) : "Status not recorded"}
              {s.eta_at ? <> · ETA <When iso={s.eta_at} format="date" /></> : null}
            </span>
          </div>
        )) : <span className="not-recorded">No shipment linked.</span>}
      </section>

      <NewTaskDialog open={newTask} onClose={() => setNewTask(false)} onCreated={() => reload()} defaults={{ vehicle_id: v.id }} />
      <RescheduleSheet task={dialogs.reschedule} open={!!dialogs.reschedule} onClose={() => dialogs.setReschedule(null)} onSaved={() => reload()} />
      <AssignSheet task={dialogs.assign} open={!!dialogs.assign} onClose={() => dialogs.setAssign(null)} onSaved={() => reload()} />
      <ReasonDialog
        open={!!dialogs.cancel} onClose={() => dialogs.setCancel(null)} title="Cancel task" description={dialogs.cancel?.title}
        label="Why (optional)" confirmLabel="Cancel task" tone="danger"
        onConfirm={async (reason) => {
          const t = dialogs.cancel; if (!t) return false;
          const out = await cmd.run("task", `/api/tasks/${encodeURIComponent(t.id)}/cancel`, { reason: reason || undefined, expected_version: t.version }, { okMessage: "Cancelled" });
          if (out.ok) reload();
          return out.ok;
        }}
      />
      <ReasonDialog
        open={!!dialogs.reject} onClose={() => dialogs.setReject(null)} title="Reject evidence" description={dialogs.reject?.title}
        label="What's missing or wrong" confirmLabel="Reject and reopen" required tone="danger"
        onConfirm={async (reason) => {
          const t = dialogs.reject; if (!t) return false;
          const out = await cmd.run("task", `/api/tasks/${encodeURIComponent(t.id)}/reject`, { reason, expected_version: t.version }, { okMessage: "Reopened with your reason" });
          if (out.ok) reload();
          return out.ok;
        }}
      />

      <ReasonDialog
        open={!!resolve} onClose={() => setResolve(null)} title="Resolve recon issue" description={resolve?.title}
        label="What was done" placeholder="The linked task still needs the owner's verification." confirmLabel="Mark resolved"
        onConfirm={async (note) => {
          if (!resolve) return false;
          const out = await cmd.run("issue", issuePath(resolve.id, "resolve"), { vehicle_id: v.id, note: note || null, expected_version: resolve.version }, { okMessage: "Issue resolved" });
          if (out.ok) reload();
          return out.ok;
        }}
      />
      <ReasonDialog
        open={!!defer} onClose={() => setDefer(null)} title="Defer recon issue" description={defer?.title}
        label="Why is it being deferred?" required confirmLabel="Defer"
        onConfirm={async (reason) => {
          if (!defer) return false;
          const out = await cmd.run("issue", issuePath(defer.id, "defer"), { vehicle_id: v.id, reason, expected_version: defer.version }, { okMessage: "Issue deferred with your reason" });
          if (out.ok) reload();
          return out.ok;
        }}
      />

      <SimpleFormDialog
        open={newPart} onClose={() => setNewPart(false)} title="Request part" label="What part?" placeholder="e.g. A/C compressor"
        extraLabel="Part number (optional)" confirmLabel="Request"
        onConfirm={async (name, partNo) => {
          const out = await cmd.run("part-new", partsPath(v.id), { name, part_no: partNo || null }, { okMessage: `Requested ${name}` });
          if (out.ok) reload();
          return out.ok;
        }}
      />
      <SimpleFormDialog
        open={newWo} onClose={() => setNewWo(false)} title="Add work order" label="What is the job?" placeholder="e.g. Replace A/C compressor"
        extraLabel="Notes (optional)" confirmLabel="Create"
        onConfirm={async (title, notes) => {
          const out = await cmd.run("wo-new", workOrderPath(v.id), { title, notes: notes || "" }, { okMessage: "Work order created" });
          if (out.ok) reload();
          return out.ok;
        }}
      />
    </div>
  );
}

function SimpleFormDialog({ open, onClose, title, label, placeholder, extraLabel, confirmLabel, onConfirm }: {
  open: boolean; onClose: () => void; title: string; label: string; placeholder?: string;
  extraLabel?: string; confirmLabel: string; onConfirm: (main: string, extra: string) => Promise<boolean>;
}) {
  const mobile = useIsMobile();
  const [main, setMain] = useState("");
  const [extra, setExtra] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) { setMain(""); setExtra(""); } }, [open]);
  return (
    <ResponsiveDialog
      mobile={mobile} open={open} onClose={onClose} title={title} size="sm"
      footer={
        <>
          <Button variant="primary" loading={busy} disabled={!main.trim()} disabledReason="Fill this in first."
            onClick={async () => { setBusy(true); const ok = await onConfirm(main.trim(), extra.trim()); setBusy(false); if (ok) onClose(); }}>{confirmLabel}</Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <div className="stack">
        <Field label={label} required><Input value={main} onChange={(e) => setMain(e.target.value)} placeholder={placeholder} maxLength={200} /></Field>
        {extraLabel ? <Field label={extraLabel}><Input value={extra} onChange={(e) => setExtra(e.target.value)} /></Field> : null}
      </div>
    </ResponsiveDialog>
  );
}

/* ================= Files ================= */
const DOC_TONE: Record<string, string> = { conflicted: "var(--risk)", missing: "var(--blocked)", complete: "var(--ok)", pending: "var(--t3)" };

function FilesTab({ d, reload, canWrite }: { d: VehicleDetailResp; reload: () => void; canWrite: boolean }) {
  const v = d.vehicle;
  const f = d.tabs.files;
  return (
    <div className="vh-two">
      <section className="vh-sect">
        <PhotoSlots
          vehicleId={v.id}
          photos={f.photos}
          requiredSlots={f.required_slots || []}
          missingSlots={f.missing_slots || []}
          heroAssetId={f.hero_asset_id}
          canWrite={canWrite}
          writeReason={whyNot("vehicles.write")}
          onChanged={reload}
          intakeHref={`/vehicles/${encodeURIComponent(v.id)}/intake`}
        />
      </section>

      <div className="stack-lg">
        <section className="vh-sect">
          <div className="vh-sect__head">
            <h2>Documents <span className="count">{f.documents.length}</span></h2>
            <span className="fs12" style={{ color: DOC_TONE[v.documents_state] || "var(--t3)" }}>{stateLabel(v.documents_state)}</span>
          </div>
          {f.documents.length ? f.documents.map((doc) => (
            <div key={doc.id} className="vh-doc">
              <span className="vh-doc__name">
                <span>{doc.original_name || CLASSIFICATION_LABELS[doc.classification || ""] || "Document"}</span>
                <span className="fs12 t3">
                  {CLASSIFICATION_LABELS[doc.classification || ""] || "Not classified"}
                  {doc.sensitive ? " · private" : ""}
                  {doc.pre_arrival ? " · pre-arrival" : ""}
                  {doc.uploaded_at ? <> · <When iso={doc.uploaded_at} format="date" /></> : null}
                </span>
              </span>
              <span className="vh-doc__state" style={{ color: doc.status === "ready" ? "var(--t3)" : "var(--risk)" }}>
                {doc.status === "ready" ? "On file" : doc.error || doc.status}
              </span>
            </div>
          )) : (
            <EmptyState align="left" title="No documents on file" body={`Documents are ${stateLabel(v.documents_state).toLowerCase()}. Export certificate, bill of lading and title appear here with their checks.`} />
          )}
          {v.documents_state === "conflicted" ? (
            <Notice tone="blocked" lead="Documents conflicted" role="alert">Two sources disagree. Open the conflicting fact's Source in Overview to see both.</Notice>
          ) : null}
        </section>

        {f.audio?.length ? (
          <section className="vh-sect">
            <div className="vh-sect__head"><h2>Voice notes <span className="count">{f.audio.length}</span></h2></div>
            {f.audio.map((a) => (
              <div key={a.id} className="vh-doc">
                <span className="vh-doc__name">
                  <span>{a.original_name || "Voice note"}</span>
                  {a.transcript ? <span className="fs12 t3">{a.transcript.slice(0, 160)}{a.transcript.length > 160 ? "…" : ""}</span> : <span className="fs12 t4">No transcript recorded</span>}
                </span>
                <span className="vh-doc__state t3">{a.uploaded_at ? <When iso={a.uploaded_at} format="date" /> : ""}</span>
              </div>
            ))}
          </section>
        ) : null}
      </div>
    </div>
  );
}

/* ================= Sale ================= */
/* Subset of GET /api/listings/vehicles/{id}/package (backend/app/services/listings.py package_view).
   The full shapes live in screens/listings/types.ts; only what this block shows is repeated here. */
interface ListingPackageView {
  package: { id: string; package_version: number; status: string; ready: boolean } | null;
  ready: boolean;
  blocked_reasons: string[];
  publications: Array<{ id: string; channel: string; state: string; external_url: string | null }>;
}

function SaleTab({ d, reload, cmd, isOwner, costsRead, onMoveReady, moving, canWrite }: {
  d: VehicleDetailResp; reload: () => void; cmd: ReturnType<typeof useVehicleCommand>;
  isOwner: boolean; costsRead: boolean; onMoveReady: () => void; moving: boolean; canWrite: boolean;
}) {
  const v = d.vehicle;
  const s = d.tabs.sale;
  const nav = useNavigate();
  const { user } = useAuth();
  const canDraftListing = can(user, "listings.draft");
  const [priceOpen, setPriceOpen] = useState(false);
  const [showGates, setShowGates] = useState(true);

  const ready = v.recon_state === "ready_for_sale";
  const gates = useQuery<{ gates: GateItem[]; decision: string } | null>(
    (signal) => (ready ? Promise.resolve(null) : fetchGates(v.id, "ready_for_sale", signal)),
    [v.id, v.version, ready],
  );
  /* Listing package for the website channel (backend/app/routers/listings.py). 403/404 → null, so a role
     without listings.read or a vehicle without a package gets an honest line, never an error. */
  const listing = useQuery<ListingPackageView | null>(
    (signal) => api.get<ListingPackageView | null>(
      `/api/listings/vehicles/${encodeURIComponent(v.id)}/package?channel=website`, { signal, tolerate: [403, 404, 501] }),
    [v.id, v.version],
  );
  const pkg = listing.data?.package || null;
  const livePub = (listing.data?.publications || []).find((p) => p.external_url) || (listing.data?.publications || [])[0] || null;
  const startListing = async () => {
    const out = await cmd.run<{ package?: { id: string } }>("listing-build", `/api/listings/vehicles/${encodeURIComponent(v.id)}/build`,
      { channel: "website" }, { okMessage: "Listing package built from the vehicle record" });
    const newId = out.data?.package?.id;
    if (out.ok && newId) nav(`/listings/${encodeURIComponent(newId)}`);
    else if (out.ok) listing.reload();
  };

  return (
    <div className="stack-lg" style={{ maxWidth: 720 }}>
      <section className="vh-sect">
        <div className="between" style={{ flexWrap: "wrap", gap: 12 }}>
          <div>
            <div style={{ fontWeight: 500 }}>{ready ? "Ready for sale" : "Not ready for sale"} · {stateLabel(s.commercial_state)}</div>
            <div className="fs13 t3">
              {ready
                ? "The shop gate has passed. The listing package can start."
                : "Listing cannot start until the shop gate passes."}
            </div>
          </div>
          <Button
            variant="primary"
            loading={moving}
            onClick={onMoveReady}
            disabled={ready || !canWrite}
            disabledReason={ready ? "Already ready for sale." : "Your role can't move vehicles between stages."}
          >
            Mark ready for sale
          </Button>
        </div>
      </section>

      {!ready ? (
        <section className="vh-sect">
          <button type="button" className="expander__btn" aria-expanded={showGates} onClick={() => setShowGates((x) => !x)} style={{ color: "var(--amber-t)" }}>
            <span className="expander__caret" aria-hidden="true">▶</span>
            <span>What's missing for Ready for sale?</span>
          </button>
          {showGates ? (
            gates.loading ? <Loading rows={2} label="Checking the gate" />
              : gates.data === null ? <span className="not-recorded">The gate check isn't available right now.</span>
                : <GateList gates={gates.data.gates || []} />
          ) : null}
          <span className="fs12 t4">Factual requirements can't be overridden — record the evidence and they turn green by themselves.</span>
        </section>
      ) : null}

      <section className="vh-sect">
        <div className="vh-sect__head"><h2>Reservation &amp; buyer</h2></div>
        <KeyValues items={[
          ["Commercial", stateLabel(s.commercial_state)],
          ["Allocation", allocationLabel(s.allocation)],
          ["Buyer", s.buyer_contact_id ? <Link to={`/contacts/${encodeURIComponent(s.buyer_contact_id)}`}>Open the contact</Link> : <NotRecorded text="No buyer recorded" />],
          ["Reserved", s.active_sale?.reserved_at ? <When iso={s.active_sale.reserved_at} format="long" /> : <NotRecorded />],
          ["Reservation ends", s.active_sale?.reservation_expires_at ? <When iso={s.active_sale.reservation_expires_at} format="long" /> : <NotRecorded />],
          ["Sale status", s.active_sale?.status ? stateLabel(s.active_sale.status) : <NotRecorded text="No active sale" />],
          ["Delivered", s.active_sale?.delivered_at ? <When iso={s.active_sale.delivered_at} format="long" /> : <NotRecorded />],
        ]} />
      </section>

      {!costsRead ? (
        <section className="vh-sect">
          <div className="vh-sect__head"><h2>Asking price</h2></div>
          <span className="not-recorded">Hidden for your role.</span>
        </section>
      ) : (
      <section className="vh-sect">
        <div className="vh-sect__head">
          <h2>Asking price</h2>
          <Button size="sm" variant="soft" onClick={() => setPriceOpen(true)} disabled={!isOwner} disabledReason="Only the owner sets the asking price.">
            {s.asking_price ? "Change price" : "Set price"}
          </Button>
        </div>
        <div className="row-wrap">
          {s.asking_price
            ? <Money amount={s.asking_price} currency={s.asking_currency || "USD"} />
            : <NotRecorded text="No asking price approved" />}
          {s.price_approved_at ? <span className="fs12 t3">approved <When iso={s.price_approved_at} format="date" /></span> : <span className="fs12 t4">Not approved</span>}
        </div>
      </section>
      )}

      <section className="vh-sect">
        <div className="vh-sect__head">
          <h2>Listing package</h2>
          {pkg ? <Button size="sm" variant="soft" to={`/listings/${encodeURIComponent(pkg.id)}`}>Open listing</Button> : null}
        </div>
        {listing.loading ? <Loading rows={1} label="Looking for a listing package" />
          : listing.data === null ? (
            <span className="not-recorded">Listings aren't available for your role.</span>
          ) : pkg ? (
            <div className="stack-sm">
              <div className="row-wrap">
                <span>Version {pkg.package_version} · {stateLabel(pkg.status)}</span>
                {listing.data.ready
                  ? <Chip size="sm" tone="ok">Checks passed</Chip>
                  : <Chip size="sm" tone="amber">{(listing.data.blocked_reasons || []).length || 1} to clear</Chip>}
                {livePub ? <Chip size="sm" tone={livePub.state === "verified" ? "ok" : "wait"}>{stateLabel(livePub.state)}</Chip> : null}
              </div>
              {!listing.data.ready && listing.data.blocked_reasons?.[0]
                ? <span className="fs13 t3">{listing.data.blocked_reasons[0]}</span> : null}
              {livePub?.external_url
                ? <a className="fs13" href={livePub.external_url} target="_blank" rel="noreferrer noopener">Open the live page</a>
                : <span className="fs13 t4">Not on the website yet.</span>}
            </div>
          ) : (
            <div className="stack-sm">
              <span className="not-recorded">No listing package yet.</span>
              <span className="fs13 t3">
                {ready
                  ? "The shop gate has passed — the package can be built from the vehicle record."
                  : "Listing cannot start until the shop gate passes."}
              </span>
              <div>
                <Button
                  size="sm"
                  variant="primary"
                  loading={cmd.busy("listing-build")}
                  disabled={!ready || !canDraftListing}
                  disabledReason={!canDraftListing ? "Your role can't build listing drafts." : "The shop gate hasn't passed yet."}
                  onClick={startListing}
                >
                  Start listing package
                </Button>
              </div>
            </div>
          )}
      </section>

      {s.disclosures?.length ? (
        <section className="vh-sect">
          <div className="vh-sect__head"><h2>Disclosures <span className="count">{s.disclosures.length}</span></h2></div>
          {s.disclosures.map((x, i) => <div key={i} className="vh-bullet"><span className="vh-bullet__dot" aria-hidden="true">•</span><span className="vh-bullet__body">{String(x)}</span></div>)}
        </section>
      ) : null}

      <PriceDialog
        open={priceOpen}
        onClose={() => setPriceOpen(false)}
        current={s.asking_price}
        currency={s.asking_currency || "USD"}
        onSave={async (amount, currency) => {
          const out = await cmd.run("price", vehiclePath(v.id, "price"), { amount, currency, expected_version: v.version }, { okMessage: "Asking price set" });
          if (out.ok) { reload(); return true; }
          return false;
        }}
      />
    </div>
  );
}

function PriceDialog({ open, onClose, current, currency, onSave }: {
  open: boolean; onClose: () => void; current?: string | null; currency: string;
  onSave: (amount: string, currency: string) => Promise<boolean>;
}) {
  const mobile = useIsMobile();
  const [amount, setAmount] = useState("");
  const [cur, setCur] = useState(currency);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) { setAmount(current || ""); setCur(currency); } }, [open, current, currency]);
  const valid = /^\d+(\.\d{1,2})?$/.test(amount.trim());
  return (
    <ResponsiveDialog
      mobile={mobile} open={open} onClose={onClose} title="Asking price" size="sm"
      description="Owner-approved. Margins use this figure, so nothing is written until you save."
      footer={
        <>
          <Button variant="primary" loading={busy} disabled={!valid} disabledReason="Enter an amount like 8500 or 8500.00"
            onClick={async () => { setBusy(true); const ok = await onSave(amount.trim(), cur); setBusy(false); if (ok) onClose(); }}>Save price</Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <div className="tk-grid2">
        <Field label="Amount" required><Input inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="8500" /></Field>
        <Field label="Currency"><Input value={cur} onChange={(e) => setCur(e.target.value.toUpperCase().slice(0, 3))} /></Field>
      </div>
    </ResponsiveDialog>
  );
}

/* ================= Money ================= */
function MoneyTabView({ state, vehicle }: { state: ReturnType<typeof useVehicleMoney>; vehicle: VehicleDetailResp }) {
  if (state.kind === "loading") return <GlassPanel clip><Loading label="Loading money" rows={3} /></GlassPanel>;
  if (state.kind === "hidden") {
    return <GlassPanel clip><EmptyState title="Money is hidden for your role" body="Costs and margins need the costs.read permission." /></GlassPanel>;
  }
  if (state.kind === "unavailable") {
    return (
      <GlassPanel clip>
        <EmptyState title="Money is not available" body="Finance hasn't recorded costs for this vehicle yet, or the figures could not be loaded." />
      </GlassPanel>
    );
  }
  const m = state.money;
  const c = m.completeness;
  const cell = (label: string, amount: string | null | undefined, cur: string | undefined, sub?: string) => (
    <div className="kpi" key={label}>
      <span className="kpi__label">{label}</span>
      <span className="kpi__value">{amount ? <Money amount={amount} currency={cur || "USD"} /> : <NotRecorded />}</span>
      {sub ? <span className="kpi__sub">{sub}</span> : null}
    </div>
  );
  const unmatched = m.unmatched_evidence?.count || 0;

  return (
    <div className="stack">
      <div className="kpis">
        {cell("Estimated total", m.estimated_total?.amount, m.estimated_total?.currency, c ? `${c.estimated_lines} estimated line${c.estimated_lines === 1 ? "" : "s"}` : undefined)}
        {cell("Committed / invoiced", m.committed_invoiced?.amount, m.committed_invoiced?.currency)}
        {cell("Cash paid", m.cash_paid?.amount, m.cash_paid?.currency, c?.cash_fx_missing ? `${c.cash_fx_missing} line(s) without FX` : undefined)}
        {cell("Remaining payable", m.remaining_payable?.amount, m.remaining_payable?.currency)}
      </div>

      <div className="kpis">
        <div className={["kpi", unmatched ? "kpi--risk" : ""].filter(Boolean).join(" ")}>
          <span className="kpi__label">Unmatched evidence</span>
          <span className="kpi__value">{unmatched}</span>
          <span className="kpi__sub">{unmatched ? "Reported payments are evidence to reconcile, not proof." : "Nothing waiting to be matched."}</span>
        </div>
        {cell(m.sale_price_label ? m.sale_price_label.replace(/^./, (x) => x.toUpperCase()) : "Approved price", m.approved_sale_price?.amount, m.approved_sale_price?.currency,
          m.sale_status ? stateLabel(m.sale_status) : undefined)}
        <div className="kpi">
          <span className="kpi__label">{m.margin_label || "Estimated margin"}</span>
          <span className="kpi__value">{m.estimated_margin?.amount ? <Money amount={m.estimated_margin.amount} currency={m.estimated_margin.currency} /> : <NotRecorded text="Unavailable" />}</span>
          <span className="kpi__sub">{c ? `${c.label} · ${c.notes?.length ? c.notes.join(" · ") : "gross of a single vehicle, not business profit"}` : ""}</span>
        </div>
      </div>

      {c && (c.fx_missing_lines || c.allocations_needing_review) ? (
        <Notice tone="risk" lead="Incomplete">
          {[c.fx_missing_lines ? `${c.fx_missing_lines} cost line(s) have no FX conversion` : null,
            c.allocations_needing_review ? `${c.allocations_needing_review} allocation(s) need review` : null].filter(Boolean).join(" · ")}
          . The total stays labelled Estimated until these are resolved.
        </Notice>
      ) : null}

      {m.by_category && Object.keys(m.by_category).length ? (
        <GlassPanel clip>
          <div className="vh-sect" style={{ padding: "12px 16px" }}>
            <div className="vh-sect__head"><h2>By category</h2></div>
            {Object.entries(m.by_category).map(([cat, row]) => (
              <div key={cat} className="vh-doc">
                <span className="vh-doc__name">
                  <span>{cat.replace(/_/g, " ")}</span>
                  <span className="fs12 t3">{row.lines} line{row.lines === 1 ? "" : "s"}{row.estimated ? ` · ${row.estimated} estimated` : ""}{row.fx_missing ? ` · ${row.fx_missing} without FX` : ""}</span>
                </span>
                <span className="vh-doc__state">{row.usd?.amount ? <Money amount={row.usd.amount} currency="USD" /> : <NotRecorded />}</span>
              </div>
            ))}
          </div>
        </GlassPanel>
      ) : null}

      <span className="fs12 t4">
        Owner only. Estimates, invoices and payments are distinct states of one expense, not columns to add up.
        {vehicle.tabs.money.coverage ? ` Coverage: ${vehicle.tabs.money.coverage}.` : ""}
        {m.as_of ? <> As of <When iso={m.as_of} format="long" />.</> : null}
      </span>
    </div>
  );
}
