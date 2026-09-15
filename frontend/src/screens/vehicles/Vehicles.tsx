/* Vehicles (spec §2.3, §8.4). Default list with the saved views All · Sourcing · Shipping · Shop · Sales
   and their counts, "Needs attention" / "Document issue" filters, and — in Shop — a List | Board toggle.
   The board has the four fixed columns Needs inspection → In recon → Finalization → Ready for sale; every
   card carries a keyboard-operable "Move stage" menu that POSTs the same shop command a drag would.
   A blocked move opens the gate dialog with the unmet requirements and the work it created.
   Mechanics see only the vehicles the API scopes to them, and no prices. */
import { useCallback, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ApiError, api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { useQuery, type QueryState } from "../../lib/useQuery";
import { can, isEmployeeRole } from "../../lib/perms";
import { useIsMobile } from "../../lib/viewport";
import {
  Button, Chip, EmptyState, ErrorState, GlassPanel, HealthLabel, Loading, MoveStageButton,
  PageHeader, SegmentedControl, Tabs, useToast,
} from "../../ui";
import { ReasonDialog } from "../tasks/TaskSheets";
import { usePeople } from "../tasks/usePeople";
import HealthRow, { VehicleThumb } from "./components/HealthRow";
import GateDialog from "./components/GateDialog";
import { movePath, useVehicleCommand } from "./components/useVehicle";
import {
  RECON_STATES, SHOP_STAGES, VIEWS, VIEW_LABELS, hasDocumentIssue, stateLabel, vehicleHealth,
  type BoardResp, type GateItem, type GateTaskRef, type MoveResult, type VehicleListItem, type VehicleListResp, type VehicleView,
} from "./types";
import "./vehicles.css";

type Layout = "list" | "board";

interface GateState {
  vehicle: VehicleListItem;
  from: string;
  to: string;
  gates: GateItem[];
  tasks: GateTaskRef[];
}

function isView(v: string | null): v is VehicleView {
  return !!v && (VIEWS as readonly string[]).includes(v);
}

export default function Vehicles() {
  const { user } = useAuth();
  const mobile = useIsMobile();
  const { toast } = useToast();
  const [params, setParams] = useSearchParams();
  const employee = isEmployeeRole(user?.role);
  const people = usePeople(!employee);
  const cmd = useVehicleCommand();

  const q = params.get("q") || "";
  const view: VehicleView = isView(params.get("view")) ? (params.get("view") as VehicleView) : "all";
  const layout: Layout = params.get("layout") === "board" && view === "shop" ? "board" : "list";
  const attention = params.get("attention") === "1";
  const docs = params.get("docs") === "1";

  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  const [gate, setGate] = useState<GateState | null>(null);
  const [backward, setBackward] = useState<{ vehicle: VehicleListItem; to: string } | null>(null);

  const setParam = (k: string, v: string | null) => {
    const next = new URLSearchParams(params);
    if (v === null) next.delete(k); else next.set(k, v);
    setParams(next, { replace: true });
  };

  const listPath = `/api/vehicles?view=${encodeURIComponent(view)}&limit=200${q ? `&q=${encodeURIComponent(q)}` : ""}`;
  const list = useQuery<VehicleListResp | null>(
    (signal) => api.get<VehicleListResp | null>(listPath, { signal, tolerate: [404, 501] }),
    [listPath, tick],
  );
  const counts = useQuery<{ counts: Record<string, number> } | null>(
    (signal) => api.get<{ counts: Record<string, number> } | null>("/api/vehicles/views", { signal, tolerate: [403, 404, 501] }),
    [tick],
  );
  const board = useQuery<BoardResp | null>(
    (signal) => (view === "shop" && layout === "board"
      ? api.get<BoardResp | null>("/api/shop/board", { signal, tolerate: [404, 501] })
      : Promise.resolve(null)),
    [view, layout, tick],
  );

  const items = useMemo(() => {
    let xs = list.data?.items || [];
    if (attention) xs = xs.filter((v) => v.health === "blocked" || v.health === "risk" || !!v.exception);
    if (docs) xs = xs.filter(hasDocumentIssue);
    return xs;
  }, [list.data, attention, docs]);

  const denied = list.error instanceof ApiError && list.error.isDenied;

  /* ---------- stage moves (button, menu and keyboard all run the same command) ---------- */
  const doMove = useCallback(async (v: VehicleListItem, to: string, opts: { reason?: string; overrides?: Record<string, string> } = {}) => {
    const body: Record<string, unknown> = { to_state: to, source: "button", expected_version: v.version };
    if (opts.reason) body.reason = opts.reason;
    if (opts.overrides) body.overrides = opts.overrides;
    const out = await cmd.run<MoveResult>(`move:${v.id}`, movePath(v.id), body, { quietBlocked: true });
    const data = out.data;
    if (out.error) return false;
    if (data && data.decision === "Blocked") {
      setGate({ vehicle: v, from: data.from, to: data.to, gates: data.gates || [], tasks: data.tasks || [] });
      return false;
    }
    if (out.ok && data?.moved) {
      toast({ message: `${v.stock_no || v.title} moved to ${stateLabel(to)}`, tone: "ok" });
      setGate(null);
      reload();
      return true;
    }
    if (out.ok && data && !data.moved) {
      toast({ message: data.reasons?.[0] || "Nothing changed.", tone: "risk" });
    }
    return false;
  }, [cmd, reload, toast]);

  const requestMove = useCallback((v: VehicleListItem, to: string) => {
    const from = v.states?.recon || "needs_inspection";
    const back = RECON_STATES.indexOf(to as (typeof RECON_STATES)[number]) < RECON_STATES.indexOf(from as (typeof RECON_STATES)[number]);
    if (back) { setBackward({ vehicle: v, to }); return; }
    void doMove(v, to);
  }, [doMove]);

  const canMove = can(user, "vehicles.write");
  const canIntake = can(user, "intake");

  /* ---------- toolbar ---------- */
  const viewTabs = VIEWS.map((v) => ({ id: v, label: VIEW_LABELS[v], count: counts.data?.counts?.[v] }));

  const header = (
    <PageHeader
      title={employee ? "Vehicles you work on" : "Vehicles"}
      subtitle={
        q ? `Search: "${q}"`
          : employee ? "Only vehicles with your tasks. No prices or customer details."
            // The board is its own list (GET /api/shop/board) and can hold more than the Shop view —
            // count what is actually on screen rather than the list endpoint's total.
            : board.data ? `${board.data.total} on the shop board`
              : list.data ? `${items.length} of ${list.data.total} in ${VIEW_LABELS[view]}${attention || docs ? " · filtered" : ""}`
                : "List by default. Shop also has a board."
      }
      actions={
        <>
          <Button
            variant="primary"
            to={canIntake ? "/vehicles/intake" : undefined}
            disabled={!canIntake}
            disabledReason="Your role can't add photos or notes to a vehicle."
          >
            {employee ? "Add photos or a note" : "Book in vehicle"}
          </Button>
        </>
      }
    >
      {employee ? null : (
        <div className="vh-toolbar">
          <Tabs<VehicleView>
            label="Vehicle views"
            idPrefix="vhv"
            value={view}
            onChange={(v) => {
              const next = new URLSearchParams(params);
              next.set("view", v);
              if (v !== "shop") next.delete("layout");
              setParams(next, { replace: true });
            }}
            tabs={viewTabs}
          />
          <div className="vh-filters">
            <Chip size="sm" selected={attention} tone={attention ? "act" : "neutral"} onClick={() => setParam("attention", attention ? null : "1")}>
              Needs attention
            </Chip>
            <Chip size="sm" selected={docs} tone={docs ? "act" : "neutral"} onClick={() => setParam("docs", docs ? null : "1")}>
              Document issue
            </Chip>
            {q ? <Chip size="sm" tone="soft" onRemove={() => setParam("q", null)} removeLabel="Clear search">“{q}”</Chip> : null}
            {view === "shop" ? (
              <SegmentedControl
                label="Shop layout"
                size="sm"
                value={layout}
                onChange={(l) => setParam("layout", l === "board" ? "board" : null)}
                options={[{ value: "list", label: "List" }, { value: "board", label: "Board" }]}
              />
            ) : null}
          </div>
        </div>
      )}
    </PageHeader>
  );

  /* ---------- body ---------- */
  let body;
  if (denied) {
    body = (
      <GlassPanel clip>
        <EmptyState title="Vehicles aren't available to your role" body="Ask the owner if you need to see this list." />
      </GlassPanel>
    );
  } else if (view === "shop" && layout === "board") {
    body = (
      <BoardView
        state={board}
        canMove={canMove}
        mobile={mobile}
        onMove={requestMove}
        busyId={cmd.busyKey}
        ownerName={(id: string | null | undefined) => (employee ? undefined : people.nameOf(id))}
      />
    );
  } else if (list.loading) {
    body = <GlassPanel clip><Loading label="Loading vehicles" rows={4} /></GlassPanel>;
  } else if (list.error) {
    body = <ErrorState error={list.error} onRetry={list.reload} />;
  } else if (!list.data || !items.length) {
    body = (
      <GlassPanel clip>
        <EmptyState
          title={q ? "No vehicles match" : attention || docs ? "Nothing needs attention here" : list.data?.empty_state || "No vehicles yet"}
          body={
            q ? "Try a stock number, frame number, model or year."
              : attention || docs ? "Clear the filters to see the rest of this view."
                : employee ? "Vehicles appear here once a task on them is assigned to you."
                  : "Book one in with photos and a note, or add it from New › Vehicle."
          }
          action={
            q || attention || docs
              ? <Button size="sm" variant="soft" onClick={() => setParams(new URLSearchParams(view === "all" ? {} : { view }), { replace: true })}>Clear filters</Button>
              : canIntake ? <Button size="sm" variant="primary" to="/vehicles/intake">Book in vehicle</Button> : undefined
          }
        />
      </GlassPanel>
    );
  } else if (mobile) {
    body = (
      <GlassPanel clip>
        {items.map((v) => <HealthRow key={v.id} vehicle={v} compact hideAllocation={employee} ownerName={employee ? undefined : people.nameOf(v.next_action_owner_id)} />)}
      </GlassPanel>
    );
  } else {
    body = (
      <GlassPanel clip style={{ overflowX: "auto" }}>
        <div className="vh-head" aria-hidden="true">
          <span>Vehicle</span>
          <span>{employee ? "Where" : "Allocation"}</span>
          <span>Current situation</span>
          <span>Next action</span>
          <span>Attention</span>
        </div>
        {items.map((v) => (
          <HealthRow key={v.id} vehicle={v} hideAllocation={employee} ownerName={employee ? undefined : people.nameOf(v.next_action_owner_id)} />
        ))}
      </GlassPanel>
    );
  }

  return (
    <div className="page page-wide">
      {header}
      {body}

      {gate ? (
        <GateDialog
          open
          onClose={() => setGate(null)}
          from={gate.from}
          to={gate.to}
          gates={gate.gates}
          tasks={gate.tasks}
          busy={cmd.busy(`move:${gate.vehicle.id}`)}
          onOverride={async (overrides) => { await doMove(gate.vehicle, gate.to, { overrides }); }}
          extraAction={<Button size="sm" variant="ghost" to={`/vehicles/${encodeURIComponent(gate.vehicle.id)}?tab=work`}>Open Work</Button>}
        />
      ) : null}

      <ReasonDialog
        open={!!backward}
        onClose={() => setBackward(null)}
        title="Move back a stage"
        description={backward ? `${backward.vehicle.title} → ${stateLabel(backward.to)}` : undefined}
        label="Why is it going back?"
        placeholder="e.g. new damage found during finalization"
        confirmLabel="Move back"
        required
        onConfirm={async (reason) => {
          if (!backward) return false;
          const ok = await doMove(backward.vehicle, backward.to, { reason });
          if (ok) setBackward(null);
          return ok;
        }}
      />
    </div>
  );
}

/* ---------- board ---------- */
function BoardView({ state, canMove, mobile, onMove, busyId, ownerName }: {
  state: QueryState<BoardResp | null>;
  canMove: boolean;
  mobile: boolean;
  onMove: (v: VehicleListItem, to: string) => void;
  busyId: string | null;
  ownerName: (id: string | null | undefined) => string | undefined;
}) {
  if (state.loading) return <GlassPanel clip><Loading label="Loading the shop board" rows={4} /></GlassPanel>;
  if (state.error) return <ErrorState error={state.error} onRetry={state.reload} />;
  const cols = state.data?.columns?.length
    ? state.data.columns
    : RECON_STATES.map((s) => ({ state: s, label: stateLabel(s), vehicles: [] as VehicleListItem[], count: 0 }));
  if (state.data && state.data.total === 0) {
    return <GlassPanel clip><EmptyState title={state.data.empty_state || "No vehicles in the shop"} body="Vehicles appear here once they are received and not yet ready for sale." /></GlassPanel>;
  }
  return (
    <div className="vh-board">
      {cols.map((col) => (
        <section key={col.state} className="vh-boardcol" aria-label={col.label}>
          <div className="vh-boardcol__head">
            <b>{col.label}</b>
            <span className="tnum">{col.count}</span>
          </div>
          {col.vehicles.map((v) => {
            const h = vehicleHealth(v);
            return (
              <article key={v.id} className="vh-card">
                <Link to={`/vehicles/${encodeURIComponent(v.id)}`} className="vh-card__open">
                  <VehicleThumb assetId={v.photo?.asset_id} size="sm" />
                  <span className="vh-col">
                    <span className="vh-title">{v.title}</span>
                    <span className="vh-col__sub">{v.stock_no || "No stock number"}</span>
                  </span>
                </Link>
                <div className="fs13 t2">
                  {v.next_action || "No next action recorded"}
                  <span className="t3"> · {ownerName(v.next_action_owner_id) || (v.next_action_owner_id ? "Assigned" : "Unassigned")}</span>
                </div>
                <div className="vh-card__foot">
                  <HealthLabel health={h.health} label={v.exception || h.label} />
                  <MoveStageButton
                    stages={SHOP_STAGES}
                    current={col.state}
                    size={mobile ? "lg" : "sm"}
                    busy={busyId === `move:${v.id}`}
                    disabledReason={!canMove ? "Your role can't move vehicles between stages." : undefined}
                    onMove={(to) => onMove(v, to)}
                  />
                </div>
              </article>
            );
          })}
          {!col.vehicles.length ? <div className="vh-boardcol__empty">Nothing here</div> : null}
        </section>
      ))}
    </div>
  );
}
