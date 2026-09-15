/* 2. Business overview — cost, profit, sales and turnaround for the selected period (spec §2.4).
   The period selector changes this section only; approvals, attention and today are never filtered by it.
   Every number arrives computed from canonical records (services/reporting.py), with its own completeness,
   as-of and currency. A missing value stays "Not recorded" — never 0 — and a role without costs.read sees
   counts and timing with the amounts masked. Each tile opens the contributing records. */
import type { FormEvent, ReactNode } from "react";
import { Button, Chip, Field, GlassPanel, Input, Notice, NotRecorded, SegmentedControl, When } from "../../../ui";
import type { BusinessOverview, DrilldownMetric, PeriodKind } from "../types";
import { CATEGORY_LABELS } from "../labels";
import { Amt, Explain, sentence } from "./parts";

export interface PeriodState { period: PeriodKind; start: string; end: string }

const PERIOD_OPTIONS: Array<{ value: PeriodKind; label: string }> = [
  { value: "month", label: "This month" },
  { value: "7d", label: "7 days" },
  { value: "30d", label: "30 days" },
  { value: "custom", label: "Custom" },
];

interface TileProps {
  metric: DrilldownMetric;
  label: string;
  value: ReactNode;
  explain: ReactNode;
  sub?: ReactNode;
  tone?: "ok" | "risk" | "blocked" | "wait";
  onOpen: (m: DrilldownMetric) => void;
  mobile: boolean;
}

function Tile({ metric, label, value, explain, sub, tone, onOpen, mobile }: TileProps) {
  return (
    <button
      type="button"
      className={["hm-tile", tone ? `hm-tile--${tone}` : ""].filter(Boolean).join(" ")}
      onClick={() => onOpen(metric)}
      style={mobile ? { minHeight: 44 } : undefined}
    >
      <span className="hm-tile__label">{label}</span>
      <span className="hm-tile__value tnum">{value}</span>
      <Explain>{explain}</Explain>
      {sub ? <span className="hm-tile__sub tnum">{sub}</span> : null}
      <span className="sr-only">Open the records behind this number</span>
    </button>
  );
}

function days(n: number | null | undefined): ReactNode {
  if (n === null || n === undefined) return <NotRecorded />;
  const rounded = Math.round(n * 10) / 10;
  return <>{rounded} {rounded === 1 ? "day" : "days"}</>;
}

export function BusinessOverviewSection({
  data, periodState, onPeriod, onOpenMetric, mobile, refreshing,
}: {
  data: BusinessOverview | null | undefined;
  periodState: PeriodState;
  /** commit=true means "apply this custom range now" — typing in the date inputs must not requery. */
  onPeriod: (next: PeriodState, commit?: boolean) => void;
  onOpenMetric: (m: DrilldownMetric) => void;
  mobile: boolean;
  refreshing: boolean;
}) {
  const applyRange = (e: FormEvent) => {
    e.preventDefault();
    onPeriod({ ...periodState, period: "custom" }, true);
  };

  const control = (
    <div className="hm-period">
      <SegmentedControl<PeriodKind>
        label="Reporting period"
        size="sm"
        value={periodState.period}
        onChange={(v) => onPeriod({ ...periodState, period: v })}
        options={PERIOD_OPTIONS}
      />
      {data?.period ? (
        <span className="fs12 t3 tnum">
          {data.period.label} · {data.period.from.slice(0, 10)} → {data.period.to.slice(0, 10)}
          {data.as_of ? <> · as of <When iso={data.as_of} format="datetime" /></> : null}
          {data.currency ? ` · ${data.currency}` : ""}
        </span>
      ) : null}
      {refreshing ? <span className="fs12 t4" role="status">Updating…</span> : null}
    </div>
  );

  const customForm = periodState.period === "custom" ? (
    <form className="hm-range" onSubmit={applyRange}>
      <Field label="From">
        <Input
          type="date"
          value={periodState.start}
          max={periodState.end || undefined}
          onChange={(e) => onPeriod({ ...periodState, period: "custom", start: e.target.value })}
        />
      </Field>
      <Field label="To" hint="The end day is included.">
        <Input
          type="date"
          value={periodState.end}
          min={periodState.start || undefined}
          onChange={(e) => onPeriod({ ...periodState, period: "custom", end: e.target.value })}
        />
      </Field>
      <Button
        type="submit"
        size={mobile ? "xl" : "md"}
        variant="soft"
        disabled={!periodState.start || !periodState.end}
        disabledReason="Pick both a start and an end date."
      >
        Apply
      </Button>
      {!periodState.start || !periodState.end ? (
        <span className="fs12 t3">
          Pick both dates and apply. Until then the numbers below are still {data?.period ? data.period.label : "the last applied period"}.
        </span>
      ) : null}
    </form>
  ) : null;

  if (data && data.available === false) {
    return (
      <div className="stack-sm">
        {control}
        {customForm}
        <GlassPanel clip>
          <div className="hm-unavailable">
            {sentence(data.reason) || "Financial summaries aren't shown for your role."}
            {data.degraded ? " Try again in a moment." : ""}
          </div>
        </GlassPanel>
      </div>
    );
  }

  const v = data?.values;
  if (!data || !v) {
    return (
      <div className="stack-sm">
        {control}
        {customForm}
        <GlassPanel clip>
          <div className="hm-tiles hm-tiles--skeleton" aria-busy="true">
            {[0, 1, 2, 3].map((i) => <div key={i} className="hm-tile hm-tile--ghost" />)}
          </div>
        </GlassPanel>
      </div>
    );
  }

  const hidden = !!data.money_hidden;
  const c = data.completeness;
  const profit = v.gross_profit;
  const margin = v.gross_margin;
  const dts = v.days_to_sale;
  const costs = v.vehicle_costs_sold_cohort;
  const unsold = v.unsold_inventory_cost;
  const projected = v.projected_gross_profit;
  const cash = data.cash_flows;

  const missing = costs.missing_lines;
  const costFlags = [
    missing.estimated ? `${missing.estimated} estimated` : null,
    missing.fx_missing ? `${missing.fx_missing} without a USD conversion` : null,
    missing.needing_review ? `${missing.needing_review} split needs review` : null,
    costs.unallocated.length ? `${costs.unallocated.length} sold vehicle(s) with no costs` : null,
  ].filter(Boolean) as string[];

  return (
    <div className="stack-sm">
      {control}
      {customForm}

      {data.stale ? (
        <Notice tone="risk" lead="Saved totals">
          These numbers come from a snapshot taken {data.as_of ? <When iso={data.as_of} format="datetime" /> : "earlier"}
          {data.stale_reason ? ` — ${data.stale_reason}` : ""}. A fresh total is recalculated in the background;
          nothing here is a zero standing in for a missing number.
        </Notice>
      ) : null}

      {hidden ? (
        <Notice tone="neutral" lead="Counts and timing only">
          Your role sees how many and how long, not how much. Amounts, margins and prices stay with the owner.
        </Notice>
      ) : null}

      <GlassPanel clip padded>
        <div className="hm-tiles">
          <Tile
            metric="vehicles_sold"
            label={v.vehicles_sold.label}
            value={<span>{v.vehicles_sold.count}</span>}
            explain={v.vehicles_sold.definition}
            sub={data.period ? data.period.label : undefined}
            onOpen={onOpenMetric}
            mobile={mobile}
          />
          <Tile
            metric="vehicle_costs_sold_cohort"
            label={costs.label}
            value={<Amt m={costs.value} hidden={hidden} bold />}
            explain="Costs allocated to the vehicles sold in this period. An invoice and its payment are never counted twice."
            sub={costFlags.length ? costFlags.join(" · ") : undefined}
            tone={costFlags.length ? "risk" : undefined}
            onOpen={onOpenMetric}
            mobile={mobile}
          />
          <Tile
            metric="gross_profit"
            label={profit.label}
            value={<Amt m={profit.value} hidden={hidden} bold title={profit.unavailable_reason || undefined} />}
            explain={profit.caveat}
            sub={
              margin.available && margin.percent
                ? `Margin ${margin.percent}% · ${margin.basis}`
                : hidden
                  ? "Margin hidden for your role"
                  : margin.unavailable_reason
                    ? `Margin unavailable — ${margin.unavailable_reason}`
                    : undefined
            }
            tone={profit.state === "recorded" ? "ok" : "risk"}
            onOpen={onOpenMetric}
            mobile={mobile}
          />
          <Tile
            metric="days_to_sale"
            label={dts.label}
            value={days(dts.median_days)}
            explain={dts.definition}
            sub={
              dts.median_days === null
                ? dts.unavailable_reason || "no record has both endpoints recorded"
                : `${dts.included} counted · ${dts.excluded} excluded (an endpoint is not recorded)`
            }
            onOpen={onOpenMetric}
            mobile={mobile}
          />
        </div>

        <div className="hm-tiles hm-tiles--secondary">
          <Tile
            metric="unsold_inventory_cost"
            label={unsold.label}
            value={<Amt m={unsold.value} hidden={hidden} />}
            explain="What is tied up in stock right now — a balance, not part of this period's sold costs."
            sub={
              <>
                {unsold.vehicles} vehicle{unsold.vehicles === 1 ? "" : "s"}
                {unsold.reserved.length ? ` · ${unsold.reserved.length} reserved` : ""}
                {unsold.as_of ? <> · as of <When iso={unsold.as_of} format="datetime" /></> : null}
              </>
            }
            onOpen={onOpenMetric}
            mobile={mobile}
          />
          <Tile
            metric="projected_gross_profit"
            label={projected.label}
            value={<Amt m={projected.value} hidden={hidden} title={projected.unavailable_reason || undefined} />}
            explain="An estimate for unsold stock: approved asking price less expected cost. Never added to recorded profit."
            sub={`Covers ${projected.coverage.ratio} unsold vehicles${projected.unknown_components.length ? ` · ${projected.unknown_components.length} missing a price or cost basis` : ""}`}
            tone="wait"
            onOpen={onOpenMetric}
            mobile={mobile}
          />
          {cash ? (
            <Tile
              metric="cash_flows"
              label="Cash in and out"
              value={
                cash.money_hidden || hidden
                  ? <Amt m={null} hidden />
                  : <><Amt m={cash.receipts} /> <span className="t4">in</span></>
              }
              explain="Money that actually settled in this period, by payment date. Cash movement is never profit."
              sub={`${cash.counts.receipts} receipt${cash.counts.receipts === 1 ? "" : "s"} · ${cash.counts.refunds} refund${cash.counts.refunds === 1 ? "" : "s"} · ${cash.counts.payouts} payout${cash.counts.payouts === 1 ? "" : "s"}${cash.counts.unsettled_excluded ? ` · ${cash.counts.unsettled_excluded} not settled yet, excluded` : ""}`}
              onOpen={onOpenMetric}
              mobile={mobile}
            />
          ) : null}
        </div>
      </GlassPanel>

      {c ? (
        <GlassPanel clip padded>
          <div className="hm-complete">
            <div className="row-wrap">
              <Chip size="sm" tone={c.recorded ? "ok" : "risk"}>{c.recorded ? "Recorded" : "Estimated"}</Chip>
              <span className="fs13 t2">
                {c.recorded
                  ? `Every required cost category (${c.required_cost_categories.join(", ")}) is recorded for the vehicles sold in this period.`
                  : "Gross profit is labelled Estimated because:"}
              </span>
            </div>
            {!c.recorded && c.reasons.length ? (
              <ul className="hm-reasons">
                {c.reasons.map((r, i) => <li key={i}>{sentence(r)}</li>)}
              </ul>
            ) : null}
            {c.missing_required.length ? (
              <span className="fs12 t3 tnum">
                Missing a required cost on: {c.missing_required.slice(0, 6).map((m) => m.stock_no || m.vehicle_id.slice(0, 8)).join(", ")}
                {c.missing_required.length > 6 ? ` and ${c.missing_required.length - 6} more` : ""}.
              </span>
            ) : null}
            {costs.by_category && Object.keys(costs.by_category).length ? (
              <div className="row-wrap" style={{ gap: 6 }}>
                {Object.entries(costs.by_category).map(([cat, t]) => (
                  <Chip
                    key={cat}
                    size="sm"
                    tone={t.fx_missing || t.estimated ? "amber" : "soft"}
                    title={`${t.lines} line(s)${t.estimated ? ` · ${t.estimated} estimated` : ""}${t.fx_missing ? ` · ${t.fx_missing} without a USD conversion` : ""}`}
                  >
                    {CATEGORY_LABELS[cat] || cat} <Amt m={t.usd} hidden={hidden} />
                  </Chip>
                ))}
              </div>
            ) : null}
            {data.restatements && data.restatements.length ? (
              <span className="fs12 t3">
                {data.restatements.length} correction{data.restatements.length === 1 ? "" : "s"} restated an earlier sale or cost in this cohort — open a metric to read them.
              </span>
            ) : null}
            {data.cohort ? <span className="fs12 t4">Cohort: {data.cohort.definition}.</span> : null}
          </div>
        </GlassPanel>
      ) : null}
    </div>
  );
}
