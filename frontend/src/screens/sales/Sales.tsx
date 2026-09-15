/* Sales: Vehicle Sales | IRQ pipelines from GET /api/sales/board; Board | List | Tasks views.
   Board columns New Lead → In Conversation → Awaiting Deposit → Deposit Paid (fixed order) with HTML5 drag and a
   keyboard Move menu on every card; drop → POST /api/sales/opportunities/{id}/move-stage. Deposit Paid is never a
   free checkbox: the API's blocked reason is shown with a Record payment link. Lost is a separate toggle.
   Lead opens in the inspector (desktop) or a sheet (phone). */
import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type KeyboardEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { can, whyNot } from "../../lib/perms";
import { useQuery } from "../../lib/useQuery";
import { TZ } from "../../lib/format";
import { useIsMobile } from "../../lib/viewport";
import { useInspector } from "../../app/Inspector";
import { Button, Chip, EmptyState, ErrorState, GlassPanel, Loading, Menu, PageHeader, PlusIcon, SegmentedControl, Sheet, Table, Tr, When, useToast, type MenuItem } from "../../ui";
import { ReasonDialog, RescheduleSheet, taskPath, useTaskCommand } from "../tasks/TaskSheets";
import { isOverdue, reminderLabel, showsTokyo, typeLabel, withinNextHours, type TaskListResp, type TaskView } from "../tasks/types";
import { moveStage, reopenLead } from "./api";
import { DepositGateDialog, LeadPane, useMoveOutcome } from "./LeadPane";
import { NewLeadDialog } from "./NewLeadDialog";
import { BOARD_STAGES, PAID_HANDOFF_NOTE, PIPELINES, pipelineLabel, stageLabel, type BoardResp, type LeadCard, type Pipeline, type Stage } from "./types";
import "./sales.css";

type View = "board" | "list" | "tasks";
const isView = (s: string | null): s is View => s === "board" || s === "list" || s === "tasks";

function openCount(b: BoardResp | null | undefined): number {
  if (!b) return 0;
  return b.columns.filter((c) => c.stage !== "deposit_paid").reduce((a, c) => a + c.count, 0);
}
function allCards(b: BoardResp | null | undefined): LeadCard[] {
  if (!b) return [];
  return [...b.columns.flatMap((c) => c.items), ...b.lost];
}

/* ---------- next-task line on a card ---------- */
function NextTask({ card, onAdd }: { card: LeadCard; onAdd?: () => void }) {
  const t = card.next_task;
  if (!t) {
    return <div className="sb-card__none">No next action{onAdd ? <> · <button type="button" className="linklike fs13" onClick={(e) => { e.stopPropagation(); onAdd(); }}>add one</button></> : null}</div>;
  }
  const over = !!t.overdue;
  return (
    <div className={["sb-card__task", over ? "sb-card__task--overdue" : ""].filter(Boolean).join(" ")}>
      <span className="sb-card__dot" aria-hidden="true" />
      <span style={{ fontWeight: 500, whiteSpace: "nowrap" }}>{typeLabel(t)}{t.type === "operational" ? ` · ${t.title}` : ""}</span>
      <span style={{ whiteSpace: "nowrap", color: over ? "var(--blocked)" : "var(--t3)" }}>· {over ? "Overdue · " : ""}{t.due_at ? <When iso={t.due_at} tz={TZ.phoenix} withTokyo={t.timezone === TZ.tokyo} /> : "no time"}</span>
    </div>
  );
}

export default function Sales() {
  const { user } = useAuth();
  const mobile = useIsMobile();
  const insp = useInspector();
  const { toast } = useToast();
  const [params, setParams] = useSearchParams();
  const pipeline: Pipeline = params.get("pipeline") === "irq" ? "irq" : "vehicle";
  const view: View = isView(params.get("view")) ? (params.get("view") as View) : "board";
  const setParam = useCallback((k: string, v: string | null) => {
    const p = new URLSearchParams(params);
    if (v) p.set(k, v); else p.delete(k);
    setParams(p, { replace: true });
  }, [params, setParams]);
  const write = can(user, "sales.write");

  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  const board = useQuery<BoardResp | null>((signal) => api.get<BoardResp | null>(`/api/sales/board?pipeline=${pipeline}`, { signal, tolerate: [404, 501] }), [pipeline, tick]);
  const otherPipe: Pipeline = pipeline === "irq" ? "vehicle" : "irq";
  const other = useQuery<BoardResp | null>((signal) => api.get<BoardResp | null>(`/api/sales/board?pipeline=${otherPipe}&lost_limit=0`, { signal, tolerate: [403, 404, 501] }).catch(() => null), [otherPipe, tick]);
  const [local, setLocal] = useState<BoardResp | null>(null);
  useEffect(() => { setLocal(board.data); }, [board.data]);

  const [showLost, setShowLost] = useState(false);
  const [newOpen, setNewOpen] = useState(params.get("new") === "1");
  useEffect(() => { if (params.get("new") === "1") setNewOpen(true); }, [params]);
  const [sheetLead, setSheetLead] = useState<{ id: string; taskType?: "call" | "meeting" | "follow_up" | null } | null>(null);
  const [lostFor, setLostFor] = useState<LeadCard | null>(null);
  const [mStage, setMStage] = useState<Stage>("new");
  const { gate, setGate, handle } = useMoveOutcome();

  // ?lead=<id> stays in the URL while the pane is open, so a refresh or Back returns to the same lead.
  const openLead = useCallback((id: string, taskType?: "call" | "meeting" | "follow_up" | null) => {
    if (mobile) setSheetLead({ id, taskType: taskType || null });
    else insp.openLead(id);
    setParam("lead", id);
  }, [mobile, insp, setParam]);
  const closeLead = useCallback(() => {
    setSheetLead(null);
    setParam("lead", null);
  }, [setParam]);

  // Register the inspector's lead renderer while this screen is mounted; close a lead pane on the way out.
  const register = insp.registerLeadPane;
  const closeInsp = insp.close;
  const modeRef = useRef(insp.mode);
  modeRef.current = insp.mode;
  useEffect(() => {
    register((id) => <LeadPane leadId={id} onChanged={reload} />);
    return () => { register(null); if (modeRef.current === "lead") closeInsp(); };
  }, [register, closeInsp, reload]);

  // Deep link: /sales?lead=<id> (from Tasks, Contacts, notifications, or a reload of this page).
  const leadParam = params.get("lead");
  const openedRef = useRef<string | null>(null);
  useEffect(() => {
    if (!leadParam) { openedRef.current = null; return; }
    if (openedRef.current === leadParam) return;
    openedRef.current = leadParam;
    if (mobile) setSheetLead({ id: leadParam, taskType: null });
    else insp.openLead(leadParam);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [leadParam, mobile]);

  // Closing the pane (or switching the inspector to Ask) drops the parameter, so Back doesn't reopen it.
  const inspectorShowsLead = !mobile && insp.open && insp.mode === "lead" && insp.leadId === leadParam;
  const wasOpenRef = useRef(false);
  useEffect(() => {
    if (mobile) { wasOpenRef.current = false; return; }
    if (inspectorShowsLead) { wasOpenRef.current = true; return; }
    if (!wasOpenRef.current) return;
    wasOpenRef.current = false;
    if (leadParam) setParam("lead", null);
  }, [mobile, inspectorShowsLead, leadParam, setParam]);

  /* ---------- stage moves ---------- */
  const [dragId, setDragId] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState<string | null>(null);
  const findCard = (id: string) => allCards(local).find((c) => c.id === id) || null;

  const applyLocal = (id: string, stage: Stage) => {
    setLocal((b) => {
      if (!b) return b;
      const card = allCards(b).find((c) => c.id === id);
      if (!card) return b;
      const moved: LeadCard = { ...card, stage, stage_label: stageLabel(stage) };
      const columns = b.columns.map((c) => {
        const items = c.items.filter((x) => x.id !== id);
        if (c.stage === stage) items.push(moved);
        return { ...c, items, count: items.length };
      });
      const lost = b.lost.filter((x) => x.id !== id);
      if (stage === "lost") lost.unshift(moved);
      return { ...b, columns, lost };
    });
  };

  const move = async (id: string, stage: Stage, reason?: string) => {
    const card = findCard(id);
    if (!card || card.stage === stage) return false;
    if (!write) { toast({ message: whyNot("sales.write"), tone: "risk" }); return false; }
    if (stage === "lost" && !reason) { setLostFor(card); return false; }
    if (card.stage === "lost") {
      const out = await reopenLead(id, { stage: stage === "deposit_paid" ? "new" : stage, expected_version: card.version });
      if (handle(out, `${card.name} reopened → ${stageLabel(stage === "deposit_paid" ? "new" : stage)}`)) reload();
      return out.kind === "ok";
    }
    const optimistic = stage !== "deposit_paid";
    if (optimistic) applyLocal(id, stage);
    const out = await moveStage(id, stage, { reason, expected_version: card.version });
    const ok = handle(out, `${card.name} → ${stageLabel(stage)}${stage === "deposit_paid" ? " · handed off" : ""}`);
    if (ok) reload(); else if (optimistic) setLocal(board.data);
    return ok;
  };

  const onDragStart = (e: DragEvent, card: LeadCard) => {
    e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", card.id); } catch { /* older WebViews */ }
    setDragId(card.id);
  };
  const onDragEnd = () => { setDragId(null); setDragOver(null); };
  const onDragOverCol = (e: DragEvent, stage: string) => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; if (dragOver !== stage) setDragOver(stage); };
  const onDragLeaveCol = (e: DragEvent) => { if (!(e.currentTarget as HTMLElement).contains(e.relatedTarget as Node | null)) setDragOver(null); };
  const onDropCol = (e: DragEvent, stage: Stage) => {
    e.preventDefault();
    const id = dragId || e.dataTransfer.getData("text/plain");
    setDragId(null);
    setDragOver(null);
    if (id) void move(id, stage);
  };

  const moveItems = (card: LeadCard): MenuItem[] => {
    const items: MenuItem[] = [];
    if (card.stage === "lost") {
      items.push({ label: "Reopen as New Lead", meta: "Reopen", onSelect: () => void move(card.id, "new") });
      return items;
    }
    const idx = BOARD_STAGES.findIndex((s) => s.id === card.stage);
    for (let i = 0; i < BOARD_STAGES.length; i++) {
      const s = BOARD_STAGES[i];
      if (s.id === card.stage) continue;
      items.push({ label: s.label, meta: s.id === "deposit_paid" ? "From payment evidence" : i > idx ? "Forward" : "Back", onSelect: () => void move(card.id, s.id) });
    }
    items.push({ label: "Mark lost / not moving forward", sepBefore: true, onSelect: () => setLostFor(card) });
    return items;
  };

  /* ---------- derived ---------- */
  const cards = useMemo(() => allCards(local), [local]);
  const overdueCount = cards.filter((c) => c.overdue && c.stage !== "lost").length;
  const counts: Record<Pipeline, number> = { [pipeline]: openCount(local), [otherPipe]: openCount(other.data) } as Record<Pipeline, number>;
  const subtitle = board.loading ? "Loading…" : local ? `${openCount(local)} open in ${pipelineLabel(pipeline)}${overdueCount ? ` · ${overdueCount} overdue` : ""} · calls, meetings and follow-ups are timed tasks, not stages` : "Calls, meetings and follow-ups are timed tasks, not stages.";

  const header = (
    <PageHeader
      title="Sales"
      subtitle={subtitle}
      actions={<Button variant="primary" iconLeft={<PlusIcon />} onClick={() => setNewOpen(true)} disabled={!write} disabledReason={whyNot("sales.write")}>New lead</Button>}
    >
      <div className="sb-toolbar">
        <SegmentedControl<Pipeline> label="Pipeline" block={mobile} value={pipeline} onChange={(v) => { setParam("pipeline", v === "vehicle" ? null : v); setMStage("new"); }}
          options={PIPELINES.map((p) => ({ value: p.id, label: p.label, count: other.loading && p.id !== pipeline ? undefined : counts[p.id] }))} />
        <SegmentedControl<View> label="View" size="sm" block={mobile} value={view} onChange={(v) => setParam("view", v === "board" ? null : v)}
          options={[{ value: "board", label: "Board" }, { value: "list", label: "List" }, { value: "tasks", label: "Tasks", count: overdueCount || undefined }]} />
      </div>
    </PageHeader>
  );

  const body = board.loading && !local ? <GlassPanel clip><Loading label="Loading leads" rows={4} /></GlassPanel>
    : board.error ? <ErrorState error={board.error} onRetry={board.reload} />
    : !local ? <GlassPanel clip><EmptyState title="Sales isn't connected yet" body="Leads appear here once the sales API is live." /></GlassPanel>
    : view === "tasks" ? <SalesTasks cards={cards} onOpenLead={(id) => openLead(id)} onChanged={reload} />
    : view === "list" ? <LeadList board={local} showLost={showLost} onOpen={(id) => openLead(id)} />
    : mobile ? (
      <MobileBoard board={local} stage={mStage} setStage={setMStage} onOpen={(id, tt) => openLead(id, tt)} />
    ) : (
      <div className="stack" style={{ gap: 10 }}>
        <div className="sb-board">
          {local.columns.map((col) => (
            <div key={col.stage} className={["sb-col", dragOver === col.stage ? "sb-col--over" : ""].filter(Boolean).join(" ")}
              onDragOver={(e) => onDragOverCol(e, col.stage)} onDragLeave={onDragLeaveCol} onDrop={(e) => onDropCol(e, col.stage as Stage)} aria-label={col.label}>
              <div className="sb-col__head"><span className="sb-col__label">{col.label}</span><span className="sb-col__count">{col.count}</span></div>
              {col.items.map((c) => (
                <BoardCard key={c.id} card={c} dragging={dragId === c.id} onOpen={() => openLead(c.id)} onAddTask={() => openLead(c.id, "call")} onDragStart={(e) => onDragStart(e, c)} onDragEnd={onDragEnd} moveItems={moveItems(c)} write={write} />
              ))}
              {col.items.length === 0 ? <div className="sb-col__empty">{col.stage === "new" ? "New enquiries land here" : col.stage === "deposit_paid" ? "Set from matched payment evidence" : "Drop a lead here"}</div> : null}
              {col.stage === "deposit_paid" ? <div className="sb-col__note">{PAID_HANDOFF_NOTE}</div> : null}
            </div>
          ))}
          {showLost ? (
            <div className={["sb-col", "sb-col--lost", dragOver === "lost" ? "sb-col--over" : ""].filter(Boolean).join(" ")}
              onDragOver={(e) => onDragOverCol(e, "lost")} onDragLeave={onDragLeaveCol} onDrop={(e) => onDropCol(e, "lost")} aria-label="Lost / Not moving forward">
              <div className="sb-col__head"><span className="sb-col__label">Lost / Not moving forward</span><span className="sb-col__count">{local.lost.length}</span></div>
              {local.lost.map((c) => (
                <BoardCard key={c.id} card={c} lost dragging={dragId === c.id} onOpen={() => openLead(c.id)} onDragStart={(e) => onDragStart(e, c)} onDragEnd={onDragEnd} moveItems={moveItems(c)} write={write} />
              ))}
              {local.lost.length === 0 ? <div className="sb-col__empty">Drop a lead here to park it</div> : null}
            </div>
          ) : null}
        </div>
        <div className="sb-foot">
          <button type="button" className="linklike fs13" onClick={() => setShowLost((v) => !v)}>{showLost ? "Hide Lost" : `Show Lost / Not moving forward (${local.lost.length})`}</button>
          <span>Drag a card to move stage, or use Move on the card. Deposit Paid is set from payment evidence.</span>
        </div>
      </div>
    );

  return (
    <div className="page page-wide">
      {header}
      {body}
      {view === "list" && local ? (
        <div className="sb-foot"><button type="button" className="linklike fs13" onClick={() => setShowLost((v) => !v)}>{showLost ? "Hide Lost" : `Show Lost / Not moving forward (${local.lost.length})`}</button></div>
      ) : null}

      <NewLeadDialog open={newOpen} onClose={() => { setNewOpen(false); if (params.get("new")) setParam("new", null); }} defaultPipeline={pipeline}
        onCreated={(r) => {
          const p = (r.opportunity.pipeline === "irq" ? "irq" : "vehicle") as Pipeline;
          if (p !== pipeline) setParam("pipeline", p === "vehicle" ? null : p);
          reload();
          openLead(r.opportunity.id, r.created ? "call" : null);
        }} />
      <ReasonDialog open={!!lostFor} onClose={() => setLostFor(null)} title="Mark lost / not moving forward" description={lostFor?.name} label="Why" placeholder="e.g. bought elsewhere, no reply for 3 weeks" confirmLabel="Mark lost" required tone="danger"
        onConfirm={async (reason) => { if (!lostFor) return false; return move(lostFor.id, "lost", reason); }} />
      <DepositGateDialog message={gate} onClose={() => setGate(null)} />
      {mobile ? (
        <Sheet open={!!sheetLead} onClose={closeLead} title={findCard(sheetLead?.id || "")?.name || "Lead"}>
          {sheetLead ? <LeadPane leadId={sheetLead.id} onChanged={reload} initialTaskType={sheetLead.taskType} /> : null}
        </Sheet>
      ) : null}
    </div>
  );
}

/* ---------- board card ---------- */
function BoardCard({ card, lost = false, dragging, onOpen, onAddTask, onDragStart, onDragEnd, moveItems, write }: {
  card: LeadCard; lost?: boolean; dragging: boolean; onOpen: () => void; onAddTask?: () => void;
  onDragStart: (e: DragEvent) => void; onDragEnd: () => void; moveItems: MenuItem[]; write: boolean;
}) {
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget) return;
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(); }
  };
  return (
    <div className={["sb-card", dragging ? "sb-card--dragging" : "", lost ? "sb-card--lost" : ""].filter(Boolean).join(" ")}
      role="button" tabIndex={0} draggable={write} onDragStart={onDragStart} onDragEnd={onDragEnd} onClick={onOpen} onKeyDown={onKey}
      aria-label={`${card.name}, ${stageLabel(card.stage)}`}>
      <div className="sb-card__top">
        <span className="sb-card__name">{card.name}</span>
        <span className="sb-card__age">{card.age}</span>
      </div>
      <div className="sb-card__subject">{card.subject}</div>
      {lost ? <div className="sb-card__none">Lost{card.lost_reason ? ` · ${card.lost_reason}` : ""}</div> : <NextTask card={card} onAdd={onAddTask} />}
      <div className="sb-card__foot" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
        <span className="sb-card__owner">{card.owner?.name || "No owner"}</span>
        <Menu label={`Move ${card.name}`} align="right" heading={<span>Now: <b style={{ fontWeight: 500 }}>{stageLabel(card.stage)}</b></span>} items={moveItems}
          trigger={<Button size="xs" variant="soft" disabled={!write} disabledReason={whyNot("sales.write")}>Move</Button>} />
      </div>
    </div>
  );
}

/* ---------- list view ---------- */
/* Stage moves from the list happen in the lead pane (stage chips) — a popover inside the scrolling table would be clipped. */
function LeadList({ board, showLost, onOpen }: { board: BoardResp; showLost: boolean; onOpen: (id: string) => void }) {
  const rows = [...board.columns.flatMap((c) => c.items), ...(showLost ? board.lost : [])];
  if (!rows.length) return <GlassPanel clip><EmptyState title="No leads here" body="Add one from New lead." /></GlassPanel>;
  return (
    <GlassPanel clip>
      <Table minWidth={760}>
        <thead><tr><th>Lead</th><th>{board.pipeline === "irq" ? "Looking for" : "Vehicle"}</th><th>Stage</th><th>Next</th><th>Owner</th><th className="num">Age</th><th aria-label="Actions" /></tr></thead>
        <tbody>
          {rows.map((c) => {
            const t = c.next_task;
            return (
              <Tr key={c.id} clickable onClick={() => onOpen(c.id)}>
                <td><div style={{ fontWeight: 500 }}>{c.name}</div>{c.company ? <div className="fs12 t3">{c.company}</div> : null}</td>
                <td className="t2">{c.subject}</td>
                <td><Chip size="sm" tone={c.stage === "deposit_paid" ? "ok" : c.stage === "lost" ? "soft" : "neutral"}>{stageLabel(c.stage)}</Chip></td>
                <td>{t ? <span style={{ color: t.overdue ? "var(--blocked)" : undefined }}>{typeLabel(t)} · {t.overdue ? "Overdue · " : ""}{t.due_at ? <When iso={t.due_at} withTokyo={t.timezone === TZ.tokyo} /> : "no time"}</span> : <span className="t4">No next action</span>}</td>
                <td className="t2">{c.owner?.name || <span className="t4">No owner</span>}</td>
                <td className="num t3">{c.age}</td>
                <td onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
                  <Button size="xs" variant="soft" onClick={() => onOpen(c.id)} title="Open the lead to change stage">Open</Button>
                </td>
              </Tr>
            );
          })}
        </tbody>
      </Table>
    </GlassPanel>
  );
}

/* ---------- mobile board: stage chips + cards ---------- */
function MobileBoard({ board, stage, setStage, onOpen }: { board: BoardResp; stage: Stage; setStage: (s: Stage) => void; onOpen: (id: string, taskType?: "call" | "meeting" | "follow_up" | null) => void }) {
  const col = board.columns.find((c) => c.stage === stage);
  const items = stage === "lost" ? board.lost : col?.items || [];
  return (
    <div className="stack" style={{ gap: 12 }}>
      <div className="sb-mchips" role="group" aria-label="Stage">
        {board.columns.map((c) => (
          <Chip key={c.stage} tone={stage === c.stage ? "act" : "neutral"} selected={stage === c.stage} onClick={() => setStage(c.stage as Stage)} count={c.count}>{c.label}</Chip>
        ))}
        <Chip tone={stage === "lost" ? "act" : "neutral"} selected={stage === "lost"} onClick={() => setStage("lost")} count={board.lost.length}>Lost</Chip>
      </div>
      <GlassPanel clip>
        {items.length === 0 ? <EmptyState title={`No ${pipelineLabel(board.pipeline)} leads in ${stageLabel(stage)}`} /> : items.map((c) => (
          <button key={c.id} type="button" className="sb-mcard" onClick={() => onOpen(c.id)}>
            <span className="sb-mcard__main">
              <span className="sb-card__top"><span className="sb-card__name">{c.name}</span><span className="sb-card__age">{c.age}</span></span>
              <span className="sb-card__subject">{c.subject}</span>
              {stage === "lost" ? <span className="sb-card__none">{c.lost_reason || "Lost"}</span> : <NextTask card={c} />}
            </span>
            <span className="list-row__chev" aria-hidden="true">›</span>
          </button>
        ))}
      </GlassPanel>
      {stage === "deposit_paid" ? <span className="fs12 t4">{PAID_HANDOFF_NOTE}</span> : null}
    </div>
  );
}

/* ---------- shared task view for this pipeline ---------- */
function SalesTasks({ cards, onOpenLead, onChanged }: { cards: LeadCard[]; onOpenLead: (id: string) => void; onChanged: () => void }) {
  const { user } = useAuth();
  const run = useTaskCommand();
  const [tick, setTick] = useState(0);
  const [reschedule, setReschedule] = useState<TaskView | null>(null);
  const [cancel, setCancel] = useState<TaskView | null>(null);
  const ids = useMemo(() => new Set(cards.map((c) => c.id)), [cards]);
  const byId = useMemo(() => new Map(cards.map((c) => [c.id, c])), [cards]);
  const q = useQuery<TaskListResp | null>((signal) => api.get<TaskListResp | null>("/api/tasks?view=all&limit=500", { signal, tolerate: [403, 404, 501] }), [tick]);
  const rows = useMemo(() => (q.data?.items || []).filter((t) => t.opportunity_id && ids.has(t.opportunity_id)), [q.data, ids]);
  const groups = [
    { key: "over", label: "Overdue", rows: rows.filter((t) => isOverdue(t)), empty: "Nothing overdue" },
    { key: "today", label: "Next 24 hours", rows: rows.filter((t) => !isOverdue(t) && withinNextHours(t, 24)), empty: "Nothing due in the next day" },
    { key: "up", label: "Upcoming", rows: rows.filter((t) => !isOverdue(t) && !withinNextHours(t, 24)), empty: "Nothing scheduled further out" },
  ];
  const refresh = () => { setTick((t) => t + 1); onChanged(); };
  const write = can(user, "tasks.write");
  const done = async (t: TaskView) => {
    const out = await run(taskPath(t.id, "complete"), { expected_version: t.version }, { okMessage: `${typeLabel(t)} done` });
    if (out.result?.status === "ok") refresh();
  };
  if (q.loading) return <GlassPanel clip><Loading label="Loading sales tasks" rows={3} /></GlassPanel>;
  if (q.error) return <ErrorState error={q.error} onRetry={q.reload} />;
  if (q.data === null) return <GlassPanel clip><EmptyState title="Tasks aren't available for your role" body="Sales tasks show here for people who can read tasks." /></GlassPanel>;
  return (
    <div className="stack">
      {groups.map((g) => (
        <section key={g.key}>
          <div className="section-title"><h2>{g.label} <span className="count">{g.rows.length}</span></h2></div>
          <GlassPanel clip>
            {g.rows.length === 0 ? <EmptyState title={g.empty} /> : g.rows.map((t) => {
              const lead = byId.get(t.opportunity_id as string);
              const over = isOverdue(t);
              return (
                <div key={t.id} className="st-row">
                  <div className={["st-row__when", over ? "st-row__when--overdue" : ""].filter(Boolean).join(" ")}>
                    <strong>{over ? "Overdue · " : ""}{t.due_at ? <When iso={t.due_at} tz={TZ.phoenix} withTokyo={showsTokyo(t)} /> : "Not scheduled"}</strong>
                    <span className="fs12 t4">{reminderLabel(t)}</span>
                  </div>
                  <div className="st-row__main">
                    <span><span style={{ fontWeight: 500 }}>{typeLabel(t)}</span> · <button type="button" className="linklike" onClick={() => onOpenLead(t.opportunity_id as string)}>{lead?.name || "Lead"}</button></span>
                    <span className="st-row__meta">{pipelineLabel(lead?.pipeline)} · {lead?.subject || ""}{t.notes ? ` · ${t.notes}` : ""}</span>
                  </div>
                  <div className="st-row__actions">
                    <Button size="xs" variant="primary" onClick={() => void done(t)} disabled={!write || t.status === "awaiting_verification"} disabledReason={t.status === "awaiting_verification" ? "Waiting for the owner to verify." : whyNot("tasks.write")}>Done</Button>
                    <Button size="xs" variant="soft" onClick={() => setReschedule(t)} disabled={!write} disabledReason={whyNot("tasks.write")}>Reschedule</Button>
                    <Button size="xs" variant="ghost" onClick={() => setCancel(t)} disabled={!write} disabledReason={whyNot("tasks.write")}>Cancel</Button>
                  </div>
                </div>
              );
            })}
          </GlassPanel>
        </section>
      ))}
      <span className="fs12 t4">Reminders fire from the server at the chosen offset. <Link to="/tasks">All tasks</Link></span>
      <RescheduleSheet task={reschedule} open={!!reschedule} onClose={() => setReschedule(null)} onSaved={() => refresh()} />
      <ReasonDialog open={!!cancel} onClose={() => setCancel(null)} title="Cancel task" description={cancel?.title} label="Why (optional)" confirmLabel="Cancel task" tone="danger"
        onConfirm={async (reason) => { const t = cancel; if (!t) return false; const out = await run(taskPath(t.id, "cancel"), { reason: reason || undefined, expected_version: t.version }, { okMessage: "Cancelled · reminders removed" }); if (out.result?.status === "ok") refresh(); return !out.error; }} />
    </div>
  );
}
