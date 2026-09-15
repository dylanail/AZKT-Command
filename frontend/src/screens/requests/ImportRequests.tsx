/* Import requests: the buyer-side list (and an optional lifecycle board) over
   GET /api/import-requests. Rows carry the buyer, what they are looking for, where the request sits in the
   lifecycle, whether it is paused, the agreement/deposit state, how many candidates exist and the next check.
   New request → POST /api/import-requests (contact + title + must/prefer/avoid + budget).
   Stages are never set by hand: the board moves a request only through the commands the API allows. */
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { can, whyNot } from "../../lib/perms";
import { useQuery } from "../../lib/useQuery";
import { useCommand } from "../../lib/useCommand";
import { useIsMobile } from "../../lib/viewport";
import { relativeTime, TZ } from "../../lib/format";
import {
  Badge, Button, Chip, EmptyState, ErrorState, Field, GlassPanel, HealthLabel, Input, ListRow, Loading, Menu,
  Money, Notice, PageHeader, PlusIcon, ResponsiveDialog, SegmentedControl, Select, Textarea, When, type MenuItem,
} from "../../ui";
import { DeniedOrError } from "./components/DeniedPanel";
import { requestAction, requestsPath, CURRENCIES } from "./api";
import {
  BOARD_COLUMNS, OPEN_STATUSES, agreementLabel, depositLabel, depositRuleSet, lifecycleHealth, lifecycleLabel,
  type ImportRequest, type RequestListResp,
} from "./types";
import "./requests.css";

type FilterKey = "all" | "open" | "active_search" | "paused" | "purchased" | "closed";
const FILTERS: { key: FilterKey; label: string }[] = [
  { key: "all", label: "All" },
  { key: "open", label: "Open" },
  { key: "active_search", label: "Active search" },
  { key: "paused", label: "Paused" },
  { key: "purchased", label: "Purchased" },
  { key: "closed", label: "Closed" },
];

function matchesFilter(r: ImportRequest, f: FilterKey): boolean {
  switch (f) {
    case "open": return OPEN_STATUSES.includes(r.status as never) && !r.paused;
    case "active_search": return r.status === "active_search";
    case "paused": return !!r.paused;
    case "purchased": return r.status === "purchased";
    case "closed": return r.status === "closed" || r.status === "delivered";
    default: return true;
  }
}

/** The next thing this request is waiting on, from recorded state only. */
function nextStep(r: ImportRequest): string {
  if (r.paused) return `Paused${r.paused_reason ? ` · ${r.paused_reason}` : ""}`;
  if (r.status === "closed") return "Closed";
  if (r.status === "delivered") return "Delivered";
  if (r.status === "purchased") return "Fulfilment";
  const gate = r.gates || r.active_search_gate;
  const blocked = gate?.gates?.filter((g) => !g.ok) || [];
  if (r.status === "active_search") {
    const c = r.candidate_counts;
    if (!c || c.candidates === 0) return "Waiting for candidates";
    if (c.bid_ready > 0) return `${c.bid_ready} bid ready`;
    return "Candidates need confirmation";
  }
  if (blocked.length) return blocked[0].reason || "Gate not met";
  return "Qualifying";
}

export default function ImportRequests() {
  const { user } = useAuth();
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  const q = params.get("q") || "";
  const filter = (FILTERS.find((f) => f.key === params.get("filter"))?.key || "open") as FilterKey;
  const view = params.get("view") === "board" ? "board" : "list";
  const [search, setSearch] = useState(q);
  const [newOpen, setNewOpen] = useState(params.get("new") === "1");
  const write = can(user, "requests.write");
  const costs = can(user, "costs.read");

  const setParam = (k: string, v: string | null) => {
    const p = new URLSearchParams(params);
    if (v) p.set(k, v); else p.delete(k);
    setParams(p, { replace: true });
  };

  useEffect(() => { setSearch(q); }, [q]);
  useEffect(() => {
    const h = window.setTimeout(() => { if (search.trim() !== q) setParam("q", search.trim() || null); }, 300);
    return () => window.clearTimeout(h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);

  const list = useQuery<RequestListResp | null>(
    (signal) => api.get<RequestListResp | null>(
      requestsPath(`?include_closed=true&limit=500${q ? `&q=${encodeURIComponent(q)}` : ""}`),
      { signal, tolerate: [404, 501] },
    ),
    [q],
  );

  const rows = useMemo(() => list.data?.items || [], [list.data]);
  const counts = useMemo(() => {
    const out = {} as Record<FilterKey, number>;
    for (const f of FILTERS) out[f.key] = rows.filter((r) => matchesFilter(r, f.key)).length;
    return out;
  }, [rows]);
  const shown = useMemo(() => rows.filter((r) => matchesFilter(r, filter)), [rows, filter]);
  const truncated = !!list.data && list.data.total > rows.length;

  const subtitle = q
    ? `Search: "${q}" · ${rows.length} loaded`
    : list.data
      ? `${counts.open} open · ${counts.active_search} in active search · ${counts.paused} paused`
      : "Buyer requirements, agreement, deposit, candidates and bids.";

  return (
    <div className="page page-wide">
      <PageHeader
        title="Import requests"
        subtitle={subtitle}
        actions={
          <>
            <SegmentedControl
              label="View"
              size="sm"
              value={view}
              onChange={(v) => setParam("view", v === "board" ? "board" : null)}
              options={[{ value: "list", label: "List" }, { value: "board", label: "Lifecycle board" }]}
            />
            <Button variant="primary" iconLeft={<PlusIcon />} onClick={() => setNewOpen(true)} disabled={!write} disabledReason={whyNot("requests.write")}>
              New request
            </Button>
          </>
        }
      >
        <div className="irq-toolbar">
          <div className="irq-chips" role="group" aria-label="Filter import requests">
            {FILTERS.map((f) => (
              <Chip
                key={f.key}
                selected={filter === f.key}
                tone={filter === f.key ? "act" : "neutral"}
                count={list.data ? counts[f.key] : undefined}
                onClick={() => setParam("filter", f.key === "open" ? null : f.key)}
              >
                {f.label}
              </Chip>
            ))}
          </div>
          <Input className="irq-search" pill value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search title or notes…" aria-label="Search import requests" />
        </div>
      </PageHeader>

      {list.loading ? (
        <GlassPanel clip><Loading label="Loading import requests" rows={4} /></GlassPanel>
      ) : list.error ? (
        <GlassPanel clip><DeniedOrError error={list.error} onRetry={list.reload} what="import requests" /></GlassPanel>
      ) : list.data === null ? (
        <GlassPanel clip><EmptyState title="Import requests aren't connected yet" body="This list fills in once the import-requests API is live." /></GlassPanel>
      ) : view === "board" ? (
        <Board rows={rows} filter={filter} write={write} costs={costs} onChanged={list.reload} />
      ) : (
        <GlassPanel clip>
          {shown.length === 0 ? (
            <EmptyState
              title={q ? "No requests match" : `Nothing in ${FILTERS.find((f) => f.key === filter)?.label.toLowerCase()}`}
              body={q ? "Try the buyer's words from the enquiry, or clear the search." : "Requests start from a buyer's enquiry: what they must have, prefer and avoid."}
              action={write && !q ? <Button size="sm" variant="soft" onClick={() => setNewOpen(true)}>New request</Button> : undefined}
            />
          ) : (
            shown.map((r) => <RequestRow key={r.id} r={r} costs={costs} />)
          )}
        </GlassPanel>
      )}

      {truncated ? <span className="fs12 t4">Showing the {rows.length} most recently updated of {list.data?.total}. Narrow with search.</span> : null}

      <NewRequestDialog
        open={newOpen}
        onClose={() => { setNewOpen(false); if (params.get("new")) setParam("new", null); }}
        onCreated={(r) => { list.reload(); nav(`/requests/${encodeURIComponent(r.id)}`); }}
      />
    </div>
  );
}

/* ---------- list row ---------- */
function RequestRow({ r, costs }: { r: ImportRequest; costs: boolean }) {
  const health = lifecycleHealth(r);
  const c = r.candidate_counts;
  const meta = (
    <span className="row-wrap" style={{ gap: 6 }}>
      <span className="truncate" style={{ maxWidth: 360 }}>{r.title || "No enquiry text recorded"}</span>
      <span>· Agreement {agreementLabel(r).toLowerCase()}</span>
      <span>· Deposit {depositLabel(r).toLowerCase()}</span>
      <span>· {c ? (c.candidates === 0 ? "no candidates" : `${c.candidates} candidate${c.candidates === 1 ? "" : "s"}${c.bid_ready ? ` · ${c.bid_ready} bid ready` : ""}${c.rejected ? ` · ${c.rejected} excluded` : ""}`) : "candidates not counted"}</span>
      {r.budget_amount || r.money_hidden ? <span>· budget <Money amount={r.budget_amount} currency={r.budget_currency || "USD"} hidden={!costs} /></span> : null}
    </span>
  );
  return (
    <ListRow
      to={`/requests/${encodeURIComponent(r.id)}`}
      title={r.contact?.name || "Buyer not linked"}
      health={health?.health}
      healthLabel={health?.label}
      tags={
        <span className="irq-row__tags">
          <Chip size="sm" tone="soft">{lifecycleLabel(r.status)}</Chip>
          {r.paused ? <Badge tone="risk">Paused</Badge> : null}
        </span>
      }
      meta={meta}
      right={
        <span className="irq-row__right">
          <span className="irq-row__next">{nextStep(r)}</span>
          <span className="irq-row__when">
            {r.next_check_at ? <>next check <When iso={r.next_check_at} tz={TZ.phoenix} format="datetime" /></> : r.updated_at ? `updated ${relativeTime(r.updated_at)}` : "Not recorded"}
          </span>
        </span>
      }
    />
  );
}

/* ---------- lifecycle board ---------- */
function Board({ rows, filter, write, costs, onChanged }: { rows: ImportRequest[]; filter: FilterKey; write: boolean; costs: boolean; onChanged: () => void }) {
  const [close, setClose] = useState<{ r: ImportRequest; outcome: "closed" | "delivered" } | null>(null);
  const shown = rows.filter((r) => matchesFilter(r, filter));
  return (
    <>
      <Notice tone="neutral" lead="Stages follow the evidence">
        A request moves when the agreement, deposit and requirements are recorded — there is no manual stage override. Use the actions on each card.
      </Notice>
      <div className="irq-board">
        {BOARD_COLUMNS.map((col) => {
          const cards = shown.filter((r) => col.statuses.includes(r.status as never));
          return (
            <GlassPanel key={col.key} className="irq-col" radius="lg">
              <div className="irq-col__head">
                <span>{col.label}</span>
                <span className="count">{cards.length}</span>
              </div>
              {cards.length === 0 ? <span className="irq-col__empty">Nothing here</span> : cards.map((r) => (
                <BoardCard key={r.id} r={r} write={write} costs={costs} onClose={(outcome) => setClose({ r, outcome })} />
              ))}
            </GlassPanel>
          );
        })}
      </div>
      <CloseDialog
        state={close}
        onClose={() => setClose(null)}
        onDone={() => { setClose(null); onChanged(); }}
      />
    </>
  );
}

function BoardCard({ r, write, costs, onClose }: { r: ImportRequest; write: boolean; costs: boolean; onClose: (outcome: "closed" | "delivered") => void }) {
  const gate = r.gates || r.active_search_gate;
  const blocked = gate?.gates?.filter((g) => !g.ok) || [];
  const items: MenuItem[] = [];
  if (r.status !== "closed") {
    items.push({
      label: r.agreement_status === "signed" ? "Agreement recorded" : "Record the agreement…",
      meta: "opens the request",
      to: `/requests/${encodeURIComponent(r.id)}#agreement`,
      disabled: r.agreement_status === "signed",
      disabledReason: "The signed agreement and its evidence are already recorded.",
    });
    items.push({
      label: r.deposit_status === "confirmed" ? "Deposit confirmed" : "Record the deposit…",
      meta: depositRuleSet(r) ? "rule set" : "rule not set",
      to: `/requests/${encodeURIComponent(r.id)}#deposit`,
      disabled: r.deposit_status === "confirmed",
      disabledReason: "The deposit is already confirmed from payment evidence.",
    });
    items.push({
      label: "Record the purchase…",
      meta: "moves to Purchased",
      to: `/requests/${encodeURIComponent(r.id)}#purchase`,
      disabled: r.status !== "active_search" && r.status !== "purchased",
      disabledReason: "A purchase is recorded once the request is in active search.",
      sepBefore: true,
    });
    items.push({
      label: "Mark delivered…",
      onSelect: () => onClose("delivered"),
      disabled: !write || r.status !== "purchased",
      disabledReason: !write ? whyNot("requests.write") : "Only a purchased request can be marked delivered.",
    });
    items.push({
      label: "Close with a reason…",
      onSelect: () => onClose("closed"),
      disabled: !write,
      disabledReason: whyNot("requests.write"),
    });
  } else {
    items.push({ label: "Open the request", to: `/requests/${encodeURIComponent(r.id)}` });
  }
  return (
    <div className="irq-card">
      <div className="irq-card__title">
        <Link to={`/requests/${encodeURIComponent(r.id)}`}>{r.contact?.name || "Buyer not linked"}</Link>
      </div>
      <div className="irq-card__meta">{r.title || "No enquiry text recorded"}</div>
      <div className="irq-card__meta">
        {r.candidate_counts ? `${r.candidate_counts.candidates} candidate${r.candidate_counts.candidates === 1 ? "" : "s"}` : "candidates not counted"}
        {r.budget_amount || r.money_hidden ? <> · <Money amount={r.budget_amount} currency={r.budget_currency || "USD"} hidden={!costs} /></> : null}
      </div>
      {blocked.length && r.status !== "closed" ? <div className="irq-card__meta" style={{ color: "var(--t3)" }}>{blocked[0].reason}</div> : null}
      <div className="irq-card__foot">
        {r.paused ? <HealthLabel health="risk" label="Paused" /> : <span className="fs12 t4">{r.updated_at ? relativeTime(r.updated_at) : ""}</span>}
        <Menu label="Move this request" align="right" items={items} trigger={<Button size="xs" variant="soft">Move</Button>} />
      </div>
    </div>
  );
}

function CloseDialog({ state, onClose, onDone }: { state: { r: ImportRequest; outcome: "closed" | "delivered" } | null; onClose: () => void; onDone: () => void }) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [reason, setReason] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { if (state) setReason(""); }, [state]);
  if (!state) return null;
  const delivered = state.outcome === "delivered";
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!reason.trim()) return;
    const res = await run("close", requestAction(state.r.id, "close"), {
      reason: reason.trim(), outcome: state.outcome, expected_version: state.r.version,
    }, { success: delivered ? "Marked delivered" : "Request closed" });
    if (res && res.status === "ok") onDone();
  };
  return (
    <ResponsiveDialog
      mobile={mobile}
      open
      onClose={onClose}
      size="sm"
      title={delivered ? "Mark delivered" : "Close this request"}
      description={delivered ? "Recorded against the purchased vehicle. History and exclusions are kept." : "Open bid packets are invalidated. History and exclusions are kept."}
      footer={
        <>
          <Button variant="primary" onClick={submit} loading={busy("close")} disabled={!reason.trim()} disabledReason="Write the reason first.">
            {delivered ? "Mark delivered" : "Close request"}
          </Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <Field label="Reason" required hint="Stored on the request and in Activity.">
          <Textarea ref={ref} value={reason} onChange={(e) => setReason(e.target.value)} rows={3} placeholder={delivered ? "Delivered to the buyer on…" : "Buyer withdrew, bought locally…"} />
        </Field>
      </form>
    </ResponsiveDialog>
  );
}

/* ---------- new request ---------- */
interface BuyerContact { id: string; name: string; company?: string | null; roles?: string[] }
interface ContactsResp { items: BuyerContact[]; total: number }

export function NewRequestDialog({ open, onClose, onCreated, contactId }: { open: boolean; onClose: () => void; onCreated: (r: ImportRequest) => void; contactId?: string }) {
  const mobile = useIsMobile();
  const { run, busy } = useCommand();
  const [picked, setPicked] = useState<string | null>(contactId || null);
  const [cq, setCq] = useState("");
  const [title, setTitle] = useState("");
  const [must, setMust] = useState("");
  const [prefer, setPrefer] = useState("");
  const [avoid, setAvoid] = useState("");
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [notes, setNotes] = useState("");
  const titleRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setPicked(contactId || null); setCq(""); setTitle(""); setMust(""); setPrefer(""); setAvoid(""); setAmount(""); setCurrency("USD"); setNotes("");
  }, [open, contactId]);

  const buyers = useQuery<ContactsResp | null>(
    (signal) => (open ? api.get<ContactsResp | null>(`/api/contacts?tab=buyers&limit=50${cq ? `&q=${encodeURIComponent(cq)}` : ""}`, { signal, tolerate: [403, 404, 501] }) : Promise.resolve(null)),
    [open, cq],
  );

  const lines = (s: string) => s.split("\n").map((x) => x.trim()).filter(Boolean);
  const requirements = [
    ...lines(must).map((text) => ({ tier: "must", text })),
    ...lines(prefer).map((text) => ({ tier: "prefer", text })),
    ...lines(avoid).map((text) => ({ tier: "avoid", text })),
  ];
  const amountValid = !amount.trim() || Number.isFinite(Number(amount));
  const canSubmit = !!picked && !!title.trim() && amountValid;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    const res = await run<{ request?: ImportRequest }>("create", requestsPath(), {
      contact_id: picked,
      title: title.trim(),
      requirements,
      budget_amount: amount.trim() ? amount.trim() : null,
      budget_currency: amount.trim() ? currency : null,
      notes: notes.trim(),
    }, { success: "Import request created" });
    const r = res?.data?.request;
    if (res && res.status === "ok" && r) onCreated(r);
  };

  if (!open) return null;
  const rows = buyers.data?.items || [];
  return (
    <ResponsiveDialog
      mobile={mobile}
      open
      onClose={onClose}
      size="lg"
      align="top"
      title="New import request"
      description="What the buyer must have, prefers and wants to avoid. Active search opens later, once the agreement and deposit are recorded."
      initialFocusRef={titleRef}
      footer={
        <>
          <Button variant="primary" onClick={submit} loading={busy("create")} disabled={!canSubmit}
            disabledReason={!picked ? "Choose the buyer first." : !title.trim() ? "Give the request a title." : "Budget must be a number."}>
            Create request
          </Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      }
    >
      <form onSubmit={submit} className="stack">
        <Field label="Buyer" required hint="Buyers come from Contacts. The request is private to this buyer.">
          <Input pill value={cq} onChange={(e) => setCq(e.target.value)} placeholder="Search buyers by name or company…" aria-label="Search buyers" />
        </Field>
        <div className="irq-picker" role="radiogroup" aria-label="Buyer">
          {buyers.loading ? <Loading label="Loading buyers" rows={2} />
            : buyers.data === null ? <p className="fs13 t4">Contacts aren't reachable for your role. Ask the owner to create the request.</p>
            : rows.length === 0 ? <p className="fs13 t4">No buyers match. Add them in Contacts first.</p>
            : rows.map((c) => (
              <button
                key={c.id}
                type="button"
                role="radio"
                aria-checked={picked === c.id}
                className="irq-picker__row"
                onClick={() => setPicked(c.id)}
              >
                <span className="truncate">{c.name}{c.company ? <span className="t4"> · {c.company}</span> : null}</span>
                {picked === c.id ? <Chip size="sm" tone="ok">Selected</Chip> : null}
              </button>
            ))}
        </div>

        <Field label="Title" required hint="What the buyer asked for, in their words.">
          <Input ref={titleRef} value={title} onChange={(e) => setTitle(e.target.value)} placeholder="4WD Sambar Dias, manual, supercharged" />
        </Field>

        <Field label="Must have" hint="One per line. A must-have that fails blocks a candidate outright; one that can't be checked stays Unknown and blocks bidding.">
          <Textarea value={must} onChange={(e) => setMust(e.target.value)} rows={3} placeholder={"4WD\nManual transmission\nSupercharger"} />
        </Field>
        <Field label="Prefer" hint="One per line. These rank candidates; they never override a must-have.">
          <Textarea value={prefer} onChange={(e) => setPrefer(e.target.value)} rows={2} placeholder={"Under 90,000 km\nWhite or silver"} />
        </Field>
        <Field label="Avoid" hint="One per line. Finding one of these counts as a failure.">
          <Textarea value={avoid} onChange={(e) => setAvoid(e.target.value)} rows={2} placeholder={"Frame rust\nSalvage grade"} />
        </Field>

        <div className="form-grid">
          <Field label="Budget" error={amountValid ? undefined : "Use digits only."} hint="Optional. Hidden from roles without cost access.">
            <Input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" placeholder="15000" />
          </Field>
          <Field label="Currency">
            <Select value={currency} onChange={(e) => setCurrency(e.target.value)}>
              {CURRENCIES.map((c) => <option key={c} value={c}>{c}</option>)}
            </Select>
          </Field>
        </div>
        <Field label="Notes" hint="Anything the buyer said that isn't a requirement.">
          <Textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={2} />
        </Field>
        <p className="fs12 t4" style={{ margin: 0 }}>
          Requirements are recorded as written. Add a checkable criterion (field, comparison, value) on the request page so candidates can be matched automatically.
        </p>
      </form>
    </ResponsiveDialog>
  );
}
