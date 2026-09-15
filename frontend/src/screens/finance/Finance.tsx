/* Finance (/finance?tab=).
   Who sees what (A03): the owner and anyone with costs.read see amounts; someone with finance.status only
   sees a sanitized status view — counts and states, never a number; everyone else gets Denied.
   Reads: GET /api/finance/summary · needs-matching · receivables · payables · vehicle-costs ·
   vehicles/{id}/money · sold-cohort · ledger/mappings · ledger/rows · export.csv
   Writes: every button posts a command under /api/finance/... and the CommandResult envelope is explained
   by useCommand (ok / needs review / blocked). */
import { useSearchParams } from "react-router-dom";
import "../../styles/finance.css";
import { api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { useQuery } from "../../lib/useQuery";
import { can } from "../../lib/perms";
import { Button, Chip, ErrorState, GlassPanel, Loading, Menu, Notice, PageHeader, Tabs, When } from "../../ui";
import Denied from "../auth/Denied";
import { Amt, Kpis, MoneyMap } from "./parts";
import { useRefs } from "./useRefs";
import { NeedsMatchingTab } from "./NeedsMatchingTab";
import { ReceivablesTab } from "./ReceivablesTab";
import { PayablesTab } from "./PayablesTab";
import { VehicleCostsTab } from "./VehicleCostsTab";
import { SoldCohortTab } from "./SoldCohortTab";
import { LedgerTab } from "./LedgerTab";
import { matchingTotal, type FinanceSummary } from "./types";

const TABS = ["matching", "receivables", "payables", "vehicles", "sold", "ledger"] as const;
type TabId = (typeof TABS)[number];

const TAB_LABELS: Record<TabId, string> = {
  matching: "Needs matching",
  receivables: "Receivables",
  payables: "Payables",
  vehicles: "Vehicle costs",
  sold: "Sold cohort",
  ledger: "Ledger",
};

const EXPORTS: Array<{ kind: string; label: string; meta: string }> = [
  { kind: "allocations", label: "Confirmed cost allocations", meta: "per vehicle" },
  { kind: "costs", label: "Cost items", meta: "with observations" },
  { kind: "payments", label: "Payments", meta: "with allocations" },
];

/** finance.status without costs.read: states and counts, never an amount (A03). */
function StatusView({ s, loading, error, reload }: { s: FinanceSummary | null; loading: boolean; error: unknown; reload: () => void }) {
  if (loading) return <GlassPanel clip><Loading label="Loading finance status" rows={3} /></GlassPanel>;
  if (error) return <GlassPanel clip><ErrorState error={error} onRetry={reload} /></GlassPanel>;
  if (!s) return null;
  const c = s.needs_matching;
  return (
    <div className="stack">
      <Notice tone="neutral" lead="Status only">
        Your role sees whether money has been matched, received or paid — not how much. Amounts, margins and prices stay with
        the owner. Ask the owner if you need a figure.
      </Notice>
      <Kpis items={[
        { label: "Waiting to be matched", value: matchingTotal(c), tone: matchingTotal(c) ? "risk" : "ok", sub: `${c.evidence} evidence · ${c.payments_reported} reported payments` },
        { label: "Obligations open", value: s.receivables_count, tone: s.status.receivables_open ? "wait" : "ok", sub: s.status.receivables_open ? "Customers still owe something" : "Nothing owed to you" },
        { label: "Bills outstanding", value: s.payables_count, tone: s.status.payables_open ? "wait" : "ok", sub: s.status.payables_open ? "Vendors still to pay" : "Nothing outstanding" },
        { label: "Sold this month", value: s.vehicles_sold_this_month, sub: `${s.period.from.slice(0, 10)} → ${s.period.to.slice(0, 10)}` },
      ]} />
      <GlassPanel clip>
        <div className="set-row">
          <div className="set-row__main">
            <span className="set-row__title">Cost evidence waiting <Chip size="sm" tone={c.evidence ? "risk" : "ok"}>{c.evidence}</Chip></span>
            <span className="set-row__meta">Ledger rows, invoices and receipts that still need the owner's decision.</span>
          </div>
        </div>
        <div className="set-row">
          <div className="set-row__main">
            <span className="set-row__title">Payments reported, not confirmed <Chip size="sm" tone={c.payments_reported ? "risk" : "ok"}>{c.payments_reported}</Chip></span>
            <span className="set-row__meta">A claim that money arrived. Nothing downstream moves until the owner confirms it from evidence.</span>
          </div>
        </div>
        <div className="set-row">
          <div className="set-row__main">
            <span className="set-row__title">Payments waiting to be applied <Chip size="sm" tone={c.payment_allocations ? "wait" : "ok"}>{c.payment_allocations}</Chip></span>
            <span className="set-row__meta">Confirmed money proposed against an obligation, still to be confirmed.</span>
          </div>
        </div>
        <div className="set-row">
          <div className="set-row__main">
            <span className="set-row__title">Cost splits waiting for review <Chip size="sm" tone={c.cost_allocations ? "wait" : "ok"}>{c.cost_allocations}</Chip></span>
            <span className="set-row__meta">One bill covering several vehicles, split but not yet confirmed.</span>
          </div>
        </div>
      </GlassPanel>
      <div className="set-foot">As of <When iso={s.as_of} format="datetime" />. Nothing on this page carries an amount.</div>
    </div>
  );
}

export default function Finance() {
  const { user } = useAuth();
  const canCosts = can(user, "costs.read");
  const canStatus = can(user, "finance.status");
  const canWrite = can(user, "finance.write");
  const isOwner = user?.role === "owner";

  const [params, setParams] = useSearchParams();
  const raw = params.get("tab") as TabId | null;
  const tab: TabId = raw && TABS.includes(raw) ? raw : "matching";
  const setTab = (id: TabId) => {
    const next = new URLSearchParams(params);
    next.set("tab", id);
    setParams(next, { replace: true });
  };

  const refs = useRefs(canCosts);
  const summaryQ = useQuery<FinanceSummary | null>(
    async (signal) => (canCosts || canStatus ? api.get<FinanceSummary>("/api/finance/summary", { signal }) : null),
    [canCosts, canStatus],
  );
  const s = summaryQ.data;

  if (!canCosts && !canStatus) return <Denied />;

  if (!canCosts) {
    return (
      <div className="page">
        <PageHeader title="Finance" subtitle="Payment status for your role. Amounts are the owner's." />
        <StatusView s={s} loading={summaryQ.loading} error={summaryQ.error} reload={summaryQ.reload} />
      </div>
    );
  }

  const counts: Record<TabId, number | undefined> = {
    matching: s ? matchingTotal(s.needs_matching) : undefined,
    receivables: s?.receivables_count,
    payables: s?.payables_count,
    vehicles: undefined,
    sold: s?.vehicles_sold_this_month,
    ledger: undefined,
  };

  const exportMenu = (
    <Menu
      label="Export CSV"
      align="right"
      heading="Filtered CSV with source links"
      trigger={<Button variant="soft" size="md">Export CSV</Button>}
      items={EXPORTS.map((e) => ({
        label: e.label,
        meta: e.meta,
        onSelect: () => { window.location.assign(`/api/finance/export.csv?kind=${e.kind}`); },
      }))}
    />
  );

  return (
    <div className="page page-wide">
      <PageHeader
        title="Finance"
        subtitle={canWrite
          ? "Feeds are read-only. AZKT proposes; you confirm. Nothing here moves money."
          : "Read-only for your role. Only the owner confirms matches and payments."}
        actions={exportMenu}
      >
        <Tabs<TabId>
          label="Finance tabs"
          value={tab}
          onChange={setTab}
          idPrefix="fin"
          tabs={TABS.map((id) => ({ id, label: TAB_LABELS[id], count: counts[id] }))}
        />
      </PageHeader>

      {summaryQ.error ? <GlassPanel clip><ErrorState error={summaryQ.error} onRetry={summaryQ.reload} title="Couldn't load the summary" /></GlassPanel> : null}

      {s ? (
        <Kpis items={[
          {
            label: "Waiting to be matched",
            value: matchingTotal(s.needs_matching),
            tone: matchingTotal(s.needs_matching) ? "risk" : "ok",
            sub: `${s.needs_matching.evidence} evidence · ${s.needs_matching.payments_reported} reported`,
          },
          { label: "Owed to you", value: <MoneyMap map={s.receivables_outstanding} empty="Nothing" />, sub: `${s.receivables_count} open obligation${s.receivables_count === 1 ? "" : "s"}` },
          { label: "Owed to vendors", value: <MoneyMap map={s.payables_outstanding} empty="Nothing" />, sub: `${s.payables_count} bill${s.payables_count === 1 ? "" : "s"}` },
          {
            label: s.sold_cohort?.gross_profit_label || "Gross profit this month",
            value: <Amt m={s.sold_cohort?.gross_profit} />,
            sub: `${s.vehicles_sold_this_month} sold · ${s.sold_cohort?.gross_margin ? `${(Number(s.sold_cohort.gross_margin) * 100).toFixed(1)}% margin` : s.sold_cohort?.gross_margin_note || "margin unavailable"}`,
          },
        ]} />
      ) : summaryQ.loading ? <GlassPanel clip><Loading label="Loading the finance summary" rows={2} /></GlassPanel> : null}

      <div role="tabpanel" id={`fin-panel-${tab}`} aria-labelledby={`fin-tab-${tab}`} tabIndex={0} className="tabpanel">
        {tab === "matching" ? <NeedsMatchingTab canWrite={canWrite} isOwner={!!isOwner} refs={refs} /> : null}
        {tab === "receivables" ? <ReceivablesTab canWrite={canWrite} refs={refs} /> : null}
        {tab === "payables" ? <PayablesTab canWrite={canWrite} refs={refs} /> : null}
        {tab === "vehicles" ? <VehicleCostsTab refs={refs} /> : null}
        {tab === "sold" ? <SoldCohortTab refs={refs} /> : null}
        {tab === "ledger" ? <LedgerTab canWrite={canWrite} isOwner={!!isOwner} /> : null}
      </div>
    </div>
  );
}
