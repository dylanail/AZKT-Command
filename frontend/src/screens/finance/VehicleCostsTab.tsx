/* Vehicle costs — GET /api/finance/vehicle-costs for the list, GET /api/finance/vehicles/{id}/money when a row
   is opened. §6.2: estimated total, committed/invoiced, cash paid, remaining, unmatched evidence, approved price
   and estimated margin, each labelled with how complete it is and which currency it assumes. */
import { Fragment, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../lib/api";
import { useQuery } from "../../lib/useQuery";
import { humanize } from "../../lib/links";
import { Button, Chip, EmptyState, ErrorState, GlassPanel, Loading, Table, Tr } from "../../ui";
import { Amt, CategoryChips, CompletenessLine, Kpis } from "./parts";
import type { useRefs } from "./useRefs";
import { CATEGORY_LABELS, type VehicleCostsResp, type VehicleMoney } from "./types";

function plural(n: number, one: string, many = `${one}s`) { return `${n} ${n === 1 ? one : many}`; }

function VehicleMoneyDetail({ vehicleId }: { vehicleId: string }) {
  const q = useQuery<VehicleMoney>((signal) => api.get<VehicleMoney>(`/api/finance/vehicles/${encodeURIComponent(vehicleId)}/money`, { signal }), [vehicleId]);
  if (q.loading) return <Loading label="Loading vehicle money" rows={2} />;
  if (q.error) return <ErrorState error={q.error} onRetry={q.reload} />;
  const d = q.data;
  if (!d || d.missing) return <EmptyState title="No money recorded for this vehicle" align="left" />;
  const c = d.completeness;

  return (
    <div className="stack">
      <Kpis items={[
        { label: "Estimated total", value: <Amt m={d.estimated_total} />, sub: `${plural(c.components.estimated, "estimated line")} · basis ${d.currency_basis}` },
        { label: "Committed / invoiced", value: <Amt m={d.committed_invoiced} />, sub: plural(c.components.invoiced, "invoiced line") },
        { label: "Cash paid", value: <Amt m={d.cash_paid} />, sub: c.cash_fx_missing ? `${plural(c.cash_fx_missing, "line")} without an FX conversion` : "From verified settlement" },
        { label: "Remaining payable", value: <Amt m={d.remaining_payable} />, tone: Number(d.remaining_payable?.amount ?? "0") > 0 ? "risk" : undefined },
      ]} />

      <Kpis items={[
        { label: d.sale_price_label ? humanize(d.sale_price_label) : "Approved price", value: <Amt m={d.approved_sale_price} />, sub: d.sale_status ? `Sale ${humanize(d.sale_status)}` : "No approved price yet" },
        {
          label: d.margin_label,
          value: <Amt m={d.estimated_margin} />,
          tone: d.estimated_margin ? (Number(d.estimated_margin.amount) < 0 ? "blocked" : undefined) : "risk",
          sub: c.notes.length ? c.notes.join(" · ") : `${c.label} · ${d.currency_basis} basis`,
        },
        {
          label: "Unmatched evidence",
          value: d.unmatched_evidence.count,
          tone: d.unmatched_evidence.count ? "risk" : undefined,
          sub: Object.entries(d.unmatched_evidence.totals).map(([cur, amt]) => `${amt} ${cur}`).join(" · ") || "Nothing waiting",
        },
        { label: "Splits needing review", value: c.allocations_needing_review, tone: c.allocations_needing_review ? "wait" : undefined, sub: c.allocations_needing_review ? "Confirm them on Needs matching" : "All confirmed" },
      ]} />

      <div className="stack-sm">
        <span className="eyebrow">By category</span>
        <CategoryChips by={d.by_category} />
        <CompletenessLine label={c.label} parts={[
          c.estimated_lines ? `${plural(c.estimated_lines, "line")} still estimated` : null,
          c.fx_missing_lines ? `${plural(c.fx_missing_lines, "line")} without an FX conversion` : null,
          c.unmatched_evidence ? `${plural(c.unmatched_evidence, "piece")} of unmatched evidence` : null,
        ]} />
      </div>

      {d.lines.length ? (
        <Table minWidth={760} aria-label="Cost lines">
          <thead><tr><th>Line</th><th>Category</th><th style={{ textAlign: "right" }}>Amount</th><th style={{ textAlign: "right" }}>USD</th><th>State</th></tr></thead>
          <tbody>
            {d.lines.map((l, i) => (
              <tr key={`${l.cost_item_id}:${i}`}>
                <td>
                  <span className="stack-sm" style={{ gap: 1 }}>
                    <span>{l.description || CATEGORY_LABELS[l.category] || l.category}</span>
                    <span className="fs12 t3">{l.vendor || "Vendor not recorded"}{l.shared ? " · split across vehicles" : ""}{l.pass_through ? " · pass-through, excluded from cost" : ""}</span>
                  </span>
                </td>
                <td>{CATEGORY_LABELS[l.category] || humanize(l.category)}</td>
                <td style={{ textAlign: "right" }}><Amt m={l.amount} /></td>
                <td style={{ textAlign: "right" }}>{l.fx_missing ? <span style={{ color: "var(--risk)" }}>No FX rate</span> : <Amt m={l.usd_amount} />}</td>
                <td>
                  <span className="row-wrap" style={{ gap: 4 }}>
                    <Chip size="sm" tone={l.estimate ? "amber" : "ok"}>{l.estimate ? "Estimated" : "Invoiced"}</Chip>
                    {l.needs_review ? <Chip size="sm" tone="wait">Split needs review</Chip> : null}
                    {l.fx_actual ? <Chip size="sm" tone="soft">Actual FX</Chip> : null}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      ) : <EmptyState title="No cost lines yet" align="left" />}
    </div>
  );
}

export function VehicleCostsTab({ refs }: { refs: ReturnType<typeof useRefs> }) {
  const q = useQuery<VehicleCostsResp>((signal) => api.get<VehicleCostsResp>("/api/finance/vehicle-costs", { signal }), []);
  const [open, setOpen] = useState<string | null>(null);
  const nav = useNavigate();
  const items = q.data?.items || [];

  if (q.loading) return <GlassPanel clip><Loading label="Loading vehicle costs" rows={4} /></GlassPanel>;
  if (q.error) return <GlassPanel clip><ErrorState error={q.error} onRetry={q.reload} /></GlassPanel>;
  if (!items.length) {
    return (
      <GlassPanel clip>
        <EmptyState title="No vehicle has costs recorded" body="Once a purchase, shipping or recon cost lands on a vehicle it is totalled here." />
      </GlassPanel>
    );
  }

  return (
    <div className="stack">
      <GlassPanel clip>
        <Table minWidth={880} aria-label="Vehicle costs">
          <thead>
            <tr>
              <th>Vehicle</th><th style={{ textAlign: "right" }}>Estimated total</th>
              <th style={{ textAlign: "right" }}>Committed / invoiced</th><th>Lines</th><th>Completeness</th>
              <th><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {items.map((v) => {
              const isOpen = open === v.vehicle_id;
              return (
                <Fragment key={v.vehicle_id}>
                  <Tr clickable onClick={() => nav(`/vehicles/${v.vehicle_id}?tab=money`)}>
                    <td>
                      <span className="stack-sm" style={{ gap: 1 }}>
                        <span>{v.title || refs.vehicleName(v.vehicle_id)}</span>
                        <span className="fs12 t3">{v.stock_no || "No stock number"}{v.allocation ? ` · ${humanize(v.allocation)}` : ""}</span>
                      </span>
                    </td>
                    <td style={{ textAlign: "right" }}><Amt m={v.estimated_total} bold /></td>
                    <td style={{ textAlign: "right" }}><Amt m={v.committed_invoiced} /></td>
                    <td className="t3 fs13">{v.lines}</td>
                    <td>
                      <span className="row-wrap" style={{ gap: 4 }}>
                        <Chip size="sm" tone={v.label === "Recorded" ? "ok" : "amber"}>{v.label}</Chip>
                        {v.flags.estimated_lines ? <Chip size="sm" tone="amber" count={v.flags.estimated_lines}>estimated</Chip> : null}
                        {v.flags.fx_missing_lines ? <Chip size="sm" tone="risk" count={v.flags.fx_missing_lines}>no FX</Chip> : null}
                        {v.flags.allocations_needing_review ? <Chip size="sm" tone="wait" count={v.flags.allocations_needing_review}>splits to review</Chip> : null}
                      </span>
                    </td>
                    <td className="actions">
                      <Button size="xs" variant="soft" aria-expanded={isOpen}
                        onClick={(e) => { e.stopPropagation(); setOpen(isOpen ? null : v.vehicle_id); }}>
                        {isOpen ? "Hide money" : "Money"}
                      </Button>
                    </td>
                  </Tr>
                  {isOpen ? (
                    <tr>
                      <td colSpan={6} style={{ background: "var(--sheet-soft)" }}>
                        <VehicleMoneyDetail vehicleId={v.vehicle_id} />
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })}
          </tbody>
        </Table>
      </GlassPanel>
      <div className="set-foot">
        Estimate, quote and invoice for one expense are stages of the same amount, never three costs. Payments settle a liability;
        they are not added to the vehicle's cost. Pass-through charges are excluded from cost entirely.
      </div>
    </div>
  );
}
