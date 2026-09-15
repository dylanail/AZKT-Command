/* Sold cohort — GET /api/finance/sold-cohort?period=month|7d|30d or ?from=&to=.
   Only vehicles whose sale completed inside the period. Deposits, unsold inventory, payouts, sales tax and
   pass-through charges are excluded by the server and said out loud here. */
import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../../lib/api";
import { useQuery } from "../../lib/useQuery";
import { humanize } from "../../lib/links";
import { Button, Chip, EmptyState, ErrorState, Field, GlassPanel, Input, Loading, Notice, SegmentedControl, Table, Tr, When } from "../../ui";
import { Amt, CategoryChips, Kpis, RestatementNotes } from "./parts";
import type { useRefs } from "./useRefs";
import type { SoldCohortResp } from "./types";

type Period = "month" | "7d" | "30d" | "custom";

export function SoldCohortTab({ refs }: { refs: ReturnType<typeof useRefs> }) {
  // The period lives in the URL so a Home drill-down ("Open in Finance") lands on the identical cohort (K01)
  // and Back returns to the same view.
  const [params, setParams] = useSearchParams();
  const urlPeriod = params.get("period");
  const urlFrom = params.get("from") || "";
  const urlTo = params.get("to") || "";
  const period: Period = urlPeriod === "7d" || urlPeriod === "30d" || urlPeriod === "custom" ? urlPeriod
    : (urlFrom && urlTo ? "custom" : "month");
  const [from, setFrom] = useState(urlFrom.slice(0, 10));
  const [to, setTo] = useState(urlTo.slice(0, 10));
  const range = urlFrom && urlTo ? { from: urlFrom, to: urlTo } : null;
  const setPeriod = (p: Period) => {
    const next = new URLSearchParams(params);
    next.set("period", p);
    if (p !== "custom") { next.delete("from"); next.delete("to"); }
    setParams(next, { replace: true });
  };
  const setRange = (r: { from: string; to: string }) => {
    const next = new URLSearchParams(params);
    next.set("period", "custom"); next.set("from", r.from); next.set("to", r.to);
    setParams(next, { replace: true });
  };

  const query = period === "custom"
    ? (range ? `from=${encodeURIComponent(range.from)}&to=${encodeURIComponent(range.to)}` : null)
    : `period=${period}`;

  const q = useQuery<SoldCohortResp | null>(
    async (signal) => (query ? api.get<SoldCohortResp>(`/api/finance/sold-cohort?${query}`, { signal }) : null),
    [query],
  );

  const applyRange = (e: FormEvent) => {
    e.preventDefault();
    if (from && to) setRange({ from, to });
  };

  const d = q.data;
  const f = d?.flags;
  const complete = !!d && !f?.fx_missing_lines && !f?.estimated_lines && !f?.allocations_needing_review && !f?.unallocated.length && !f?.unknown_net;

  return (
    <div className="stack">
      <div className="row-wrap" style={{ justifyContent: "space-between" }}>
        <SegmentedControl<Period>
          label="Period"
          value={period}
          onChange={setPeriod}
          options={[
            { value: "month", label: "This month" },
            { value: "7d", label: "Last 7 days" },
            { value: "30d", label: "Last 30 days" },
            { value: "custom", label: "Custom" },
          ]}
        />
        {d ? <span className="fs12 t3">{d.period.from.slice(0, 10)} → {d.period.to.slice(0, 10)} · as of <When iso={d.as_of} format="datetime" /></span> : null}
      </div>

      {period === "custom" ? (
        <form className="row-wrap" onSubmit={applyRange} style={{ alignItems: "flex-end" }}>
          <Field label="From"><Input type="date" value={from} onChange={(e) => setFrom(e.target.value)} /></Field>
          <Field label="To" hint="The end day is not included."><Input type="date" value={to} onChange={(e) => setTo(e.target.value)} /></Field>
          <Button type="submit" size="md" variant="soft" disabled={!from || !to} disabledReason="Pick both dates.">Show</Button>
        </form>
      ) : null}

      {q.loading ? <GlassPanel clip><Loading label="Loading the sold cohort" rows={3} /></GlassPanel>
        : q.error ? <GlassPanel clip><ErrorState error={q.error} onRetry={q.reload} /></GlassPanel>
        : !d ? <GlassPanel clip><EmptyState title="Pick a period" body="Choose both dates to see the vehicles sold in that window." /></GlassPanel>
        : (
          <div className="stack">
            <Kpis items={[
              { label: "Vehicles sold", value: d.vehicles_sold, sub: d.days_to_sale_median !== null ? `median ${d.days_to_sale_median} days to sale` : "days to sale not available" },
              { label: "Net sales value", value: <Amt m={d.net_sales_value} />, sub: f?.unknown_net ? "One sale is not in USD — no single total" : `${f?.sale_currencies.join(" · ") || "USD"}` },
              { label: "Allocated costs", value: <Amt m={d.costs_total} />, sub: "Non-duplicated cost basis" },
              {
                label: d.gross_profit_label,
                value: <Amt m={d.gross_profit} />,
                tone: complete ? "ok" : "risk",
                sub: d.gross_margin ? `${(Number(d.gross_margin) * 100).toFixed(1)}% margin` : d.gross_margin_note || "Margin unavailable",
              },
            ]} />

            {!complete && d.vehicles_sold ? (
              <Notice tone="risk" lead="Estimated, not recorded" role="status">
                {[
                  f?.estimated_lines ? `${f.estimated_lines} cost line(s) are still estimates` : null,
                  f?.fx_missing_lines ? `${f.fx_missing_lines} line(s) have no FX conversion` : null,
                  f?.allocations_needing_review ? `${f.allocations_needing_review} split(s) still need review` : null,
                  f?.unallocated.length ? `${f.unallocated.length} sold vehicle(s) have no costs recorded` : null,
                  f?.unknown_net ? "a sale is not in USD, so there is no single net total" : null,
                ].filter(Boolean).join(" · ")}.
              </Notice>
            ) : null}

            <div className="stack-sm">
              <span className="eyebrow">Costs by category</span>
              <CategoryChips by={d.costs_by_category} />
            </div>

            <GlassPanel clip>
              {!d.sales.length ? (
                <EmptyState title="No vehicle completed a sale in this period" body="Reservations, deposits and unsold inventory are deliberately not counted here." />
              ) : (
                <Table minWidth={900} aria-label="Sold vehicles">
                  <thead>
                    <tr>
                      <th>Vehicle</th><th>Completed</th><th style={{ textAlign: "right" }}>Net sale value</th>
                      <th style={{ textAlign: "right" }}>Allocated costs</th><th style={{ textAlign: "right" }}>Gross profit</th><th>Flags</th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.sales.map((s) => (
                      <Tr key={s.vehicle_id}>
                        <td>
                          <div className="stack-sm" style={{ gap: 4 }}>
                            <Link to={`/vehicles/${s.vehicle_id}?tab=money`}>{s.stock_no || refs.vehicleName(s.vehicle_id)}</Link>
                            <CategoryChips by={s.by_category} />
                          </div>
                        </td>
                        <td className="t3 fs13">
                          <When iso={s.completed_at} format="date" />
                          {s.days_to_sale !== null ? <div className="fs12 t4">{s.days_to_sale} days held</div> : null}
                        </td>
                        <td style={{ textAlign: "right" }}>
                          <Amt m={s.net_sale_value} bold />
                          {s.net_sale_value_usd === null ? <div className="fs12" style={{ color: "var(--risk)" }}>No USD basis</div> : null}
                          <div className="fs12 t4">tax and pass-through excluded</div>
                        </td>
                        <td style={{ textAlign: "right" }}><Amt m={s.costs_usd} /></td>
                        <td style={{ textAlign: "right" }}><Amt m={s.gross_profit_usd} bold /></td>
                        <td>
                          <span className="row-wrap" style={{ gap: 4 }}>
                            {s.flags.no_costs ? <Chip size="sm" tone="blocked">No costs recorded</Chip> : null}
                            {s.flags.estimated_lines ? <Chip size="sm" tone="amber" count={s.flags.estimated_lines}>estimated</Chip> : null}
                            {s.flags.fx_missing_lines ? <Chip size="sm" tone="risk" count={s.flags.fx_missing_lines}>no FX</Chip> : null}
                            {s.flags.allocations_needing_review ? <Chip size="sm" tone="wait" count={s.flags.allocations_needing_review}>splits</Chip> : null}
                            {s.restatements.length ? <Chip size="sm" tone="wait" count={s.restatements.length}>restated</Chip> : null}
                            {!s.flags.no_costs && !s.flags.estimated_lines && !s.flags.fx_missing_lines && !s.flags.allocations_needing_review ? <Chip size="sm" tone="ok">Complete</Chip> : null}
                          </span>
                        </td>
                      </Tr>
                    ))}
                  </tbody>
                </Table>
              )}
            </GlassPanel>

            {f?.unallocated.length ? (
              <div className="stack-sm">
                <span className="eyebrow">Sold with nothing allocated · {f.unallocated.length}</span>
                <ul className="fs13 t2" style={{ margin: 0, paddingLeft: 18, lineHeight: 1.55 }}>
                  {f.unallocated.map((u) => (
                    <li key={u.vehicle_id}><Link to={`/vehicles/${u.vehicle_id}?tab=money`}>{refs.vehicleName(u.vehicle_id)}</Link> — {u.reason}</li>
                  ))}
                </ul>
              </div>
            ) : null}

            <RestatementNotes items={d.restatements} title="Restatements in this period" />

            <div className="set-foot">
              Excluded from this cohort: {(f?.excluded || []).map((x) => humanize(x).toLowerCase()).join(", ")}.
              A deposit is not revenue, a payout to your bank is not a sale, and an unsold vehicle's cost is not a loss yet.
            </div>
          </div>
        )}
    </div>
  );
}
