/* The drill-down behind every business-overview tile: the exact sales, vehicles and cost items the number
   was computed from (GET /api/home/metrics/drilldown?metric=…), plus the link that opens Finance on the
   identical cohort. Sheet on a phone, dialog on desktop. Nothing here is recomputed in the browser — the
   rows are the server's own contributing records. */
import { Link } from "react-router-dom";
import { api } from "../../../lib/api";
import { useQuery } from "../../../lib/useQuery";
import { humanize } from "../../../lib/links";
import { Button, Chip, ErrorState, Loading, NotRecorded, ResponsiveDialog, Table, Tr, When } from "../../../ui";
import { drilldownPath, financeHref, type PeriodQuery } from "../api";
import type { DrilldownMetric, DrilldownResp, DurationMetric, SaleRow } from "../types";
import { CATEGORY_LABELS, METRIC_TITLES } from "../types";
import { Amt, Explain, sentence } from "./parts";

function SaleTable({ rows, hidden }: { rows: SaleRow[]; hidden: boolean }) {
  return (
    <Table minWidth={620} caption="Every completed sale in this period, with the costs allocated to that vehicle.">
      <thead>
        <tr>
          <th scope="col">Vehicle</th>
          <th scope="col">Sold</th>
          <th scope="col" className="num">Net sale value</th>
          <th scope="col" className="num">Costs</th>
          <th scope="col" className="num">Gross profit</th>
          <th scope="col" className="num">Days to sale</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <Tr key={r.sale_id}>
            <td>
              <Link to={`/vehicles/${r.vehicle_id}`}>{r.stock_no || `Vehicle ${r.vehicle_id.slice(0, 8)}`}</Link>
              {r.flags.no_costs ? <div className="fs12 hm-overdue">No costs recorded</div> : null}
              {r.flags.fx_missing_lines ? <div className="fs12 t3">{r.flags.fx_missing_lines} line(s) without a USD conversion</div> : null}
              {r.flags.estimated_lines ? <div className="fs12 t3">{r.flags.estimated_lines} estimated line(s)</div> : null}
            </td>
            <td className="tnum">{r.completed_at ? <When iso={r.completed_at} format="date" /> : <NotRecorded />}</td>
            <td className="num"><Amt m={r.net_sale_value} hidden={hidden} /></td>
            <td className="num"><Amt m={r.costs} hidden={hidden} /></td>
            <td className="num"><Amt m={r.gross_profit} hidden={hidden} /></td>
            <td className="num">{r.days_to_sale === null ? <NotRecorded /> : r.days_to_sale}</td>
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}

function Duration({ d }: { d: DurationMetric }) {
  return (
    <div className="hm-duration">
      <span className="hm-duration__value tnum">
        {d.median_days === null ? <NotRecorded /> : <>{Math.round(d.median_days * 10) / 10} days</>}
      </span>
      <span className="fs13 t2">{d.label}</span>
      <Explain>{d.definition}</Explain>
      <span className="fs12 t4 tnum">
        {d.included} counted · {d.excluded} excluded
        {d.median_days === null && d.unavailable_reason ? ` — ${d.unavailable_reason}` : ""}
      </span>
    </div>
  );
}

function VehicleIds({ ids, title }: { ids: string[] | undefined; title: string }) {
  if (!ids || !ids.length) return null;
  return (
    <div className="stack-sm" style={{ gap: 6 }}>
      <span className="eyebrow">{title} · {ids.length}</span>
      <div className="row-wrap" style={{ gap: 6 }}>
        {ids.slice(0, 40).map((id) => (
          <Link key={id} className="fs13" to={`/vehicles/${id}`}>{id.slice(0, 8)}</Link>
        ))}
        {ids.length > 40 ? <span className="fs12 t4">and {ids.length - 40} more</span> : null}
      </div>
    </div>
  );
}

function Body({ d }: { d: DrilldownResp }) {
  const hidden = !!d.money_hidden;
  const metric = d.metric;
  return (
    <div className="stack">
      <div className="hm-drill-head fs13 t3 tnum">
        <span>{d.period.label} · {d.period.from.slice(0, 10)} → {d.period.to.slice(0, 10)}</span>
        <span>as of <When iso={d.as_of} format="datetime" /></span>
        <span>{d.currency}</span>
        {d.stale ? <Chip size="sm" tone="risk">Saved snapshot</Chip> : null}
        {hidden ? <Chip size="sm" tone="soft">Amounts hidden for your role</Chip> : null}
      </div>

      <Explain>Cohort: {d.cohort.definition}.</Explain>

      {d.completeness && !d.completeness.recorded && d.completeness.reasons.length ? (
        <div className="hm-complete">
          <Chip size="sm" tone="risk">Estimated</Chip>
          <ul className="hm-reasons">
            {d.completeness.reasons.map((r, i) => <li key={i}>{sentence(r)}</li>)}
          </ul>
        </div>
      ) : null}

      {d.by_category && Object.keys(d.by_category).length ? (
        <div className="row-wrap" style={{ gap: 6 }}>
          {Object.entries(d.by_category).map(([cat, t]) => (
            <Chip key={cat} size="sm" tone={t.fx_missing || t.estimated ? "amber" : "soft"} title={`${t.lines} line(s)`}>
              {CATEGORY_LABELS[cat] || cat} <Amt m={t.usd} hidden={hidden} />
            </Chip>
          ))}
        </div>
      ) : null}

      {d.unallocated && d.unallocated.length ? (
        <div className="stack-sm" style={{ gap: 4 }}>
          <span className="eyebrow">Sold vehicles with no recorded costs · {d.unallocated.length}</span>
          <ul className="hm-items">
            {d.unallocated.map((u) => (
              <li key={u.vehicle_id}>
                <Link to={`/vehicles/${u.vehicle_id}`}>{u.vehicle_id.slice(0, 8)}</Link>
                <span className="t3"> — {u.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {metric === "days_to_sale" && d.durations ? (
        <div className="hm-durations">
          {Object.entries(d.durations).map(([key, v]) => <Duration key={key} d={v} />)}
        </div>
      ) : null}

      {metric === "cash_flows" && d.cash_flows ? (
        <div className="stack-sm">
          <div className="hm-tiles hm-tiles--secondary">
            <div className="hm-tile hm-tile--static">
              <span className="hm-tile__label">Money in</span>
              <span className="hm-tile__value tnum"><Amt m={d.cash_flows.receipts} hidden={hidden} /></span>
              <Explain>{d.cash_flows.counts.receipts} settled receipt(s), by payment date.</Explain>
            </div>
            <div className="hm-tile hm-tile--static">
              <span className="hm-tile__label">Refunded</span>
              <span className="hm-tile__value tnum"><Amt m={d.cash_flows.refunds} hidden={hidden} /></span>
              <Explain>{d.cash_flows.counts.refunds} refund(s) dated inside this period.</Explain>
            </div>
            <div className="hm-tile hm-tile--static">
              <span className="hm-tile__label">Paid out</span>
              <span className="hm-tile__value tnum"><Amt m={d.cash_flows.payouts} hidden={hidden} /></span>
              <Explain>{d.cash_flows.counts.payouts} payout(s). Cash movement is never profit.</Explain>
            </div>
          </div>
          {d.cash_flows.counts.unsettled_excluded ? (
            <span className="fs13 t3">
              {d.cash_flows.counts.unsettled_excluded} payment(s) are claimed or pending and are excluded until the provider settles them.
            </span>
          ) : null}
          {d.cash_flows.counts.unknown_currency ? (
            <span className="fs13 t3">{d.cash_flows.counts.unknown_currency} payment(s) are in a currency with no USD basis and are listed, not added.</span>
          ) : null}
        </div>
      ) : null}

      {d.rows && d.rows.length ? <SaleTable rows={d.rows} hidden={hidden} /> : null}

      {metric === "unsold_inventory_cost" || metric === "projected_gross_profit"
        ? <VehicleIds ids={d.vehicle_ids} title="Vehicles included" />
        : null}

      {d.unknown_components && d.unknown_components.length ? (
        <div className="stack-sm" style={{ gap: 4 }}>
          <span className="eyebrow">Not included in the estimate · {d.unknown_components.length}</span>
          <ul className="hm-items">
            {d.unknown_components.map((u, i) => (
              <li key={i}>
                {u.vehicle_id ? <Link to={`/vehicles/${u.vehicle_id}`}>{u.stock_no || u.vehicle_id.slice(0, 8)}</Link> : (u.stock_no || "A vehicle")}
                <span className="t3"> — {u.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {d.excluded_vehicle_ids && d.excluded_vehicle_ids.length ? (
        <VehicleIds ids={d.excluded_vehicle_ids} title="Excluded — an endpoint is not recorded" />
      ) : null}

      {d.cost_item_ids && d.cost_item_ids.length ? (
        <span className="fs12 t4 tnum">{d.cost_item_ids.length} cost item(s) contribute to this number.</span>
      ) : null}

      {d.restatements && d.restatements.length ? (
        <div className="stack-sm" style={{ gap: 4 }}>
          <span className="eyebrow">Restatements · {d.restatements.length}</span>
          <ul className="hm-items">
            {d.restatements.map((r, i) => (
              <li key={i}>
                {r.field ? `${humanize(String(r.field))}: ` : ""}
                {typeof r.note === "string" ? r.note : typeof r.reason === "string" ? r.reason : "A later correction changed this cohort"}
                {typeof r.at === "string" ? <span className="t4"> ({r.at.slice(0, 10)})</span> : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

export function MetricDrilldown({
  metric, periodQuery, tz, mobile, onClose,
}: {
  metric: DrilldownMetric;
  periodQuery: PeriodQuery;
  tz: string;
  mobile: boolean;
  onClose: () => void;
}) {
  const path = drilldownPath(metric, periodQuery, tz);
  const q = useQuery<DrilldownResp>((signal) => api.get<DrilldownResp>(path, { signal }), [path]);
  const d = q.data;
  const finance = d ? financeHref(d.finance_filters) : null;

  return (
    <ResponsiveDialog
      mobile={mobile}
      open
      onClose={onClose}
      size="xl"
      align="top"
      eyebrow="Business overview"
      title={METRIC_TITLES[metric] || humanize(metric)}
      label={`${METRIC_TITLES[metric] || metric} detail`}
      footer={
        <>
          <Button
            variant="soft"
            size={mobile ? "xl" : "md"}
            to={finance || undefined}
            disabled={!finance}
            disabledReason="Finance has no view for this measure yet."
          >
            Open in Finance
          </Button>
          <Button variant="ghost" size={mobile ? "xl" : "md"} onClick={onClose}>Close</Button>
        </>
      }
    >
      {q.loading && !d ? <Loading label="Loading the records behind this number" rows={4} /> : null}
      {q.error ? <ErrorState error={q.error} onRetry={q.reload} title="Couldn't load this detail" /> : null}
      {d ? <Body d={d} /> : null}
    </ResponsiveDialog>
  );
}
