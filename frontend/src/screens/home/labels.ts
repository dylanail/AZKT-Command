/* Plain-English labels and the tiny helpers the Home sections share. Kept out of types.ts so that file
   stays a pure transcription of the server's shapes. Nothing here invents a value — every label describes
   a field the API actually sent. */
export const METRIC_TITLES: Record<string, string> = {
  vehicles_sold: "Vehicles sold",
  net_vehicle_sales_value: "Net vehicle sales value",
  vehicle_costs_sold_cohort: "Vehicle costs",
  gross_profit: "Gross profit",
  gross_margin: "Gross margin",
  days_to_sale: "Days to sale",
  unsold_inventory_cost: "Unsold inventory cost",
  projected_gross_profit: "Projected gross profit",
  cash_flows: "Cash in and out",
};

export const CATEGORY_LABELS: Record<string, string> = {
  purchase: "Purchase", import: "Import", transport: "Transport", recon: "Recon", parts: "Parts",
  labor: "Labor", selling: "Selling", storage: "Storage", other: "Other",
};

export const STAGE_DIMENSION_LABELS: Record<string, string> = {
  logistics: "Logistics", recon: "Shop", commercial: "Sale", documents: "Documents",
};

export const ATTENTION_KIND_LABELS: Record<string, string> = {
  blocker: "Blocked work",
  missing_document: "Documents",
  overdue_commitment: "Overdue promise",
  overdue_task: "Overdue tasks",
  unmatched_payment: "Money to match",
  unmatched_message: "Unmatched messages",
  failed_publication: "Publishing",
  stale_connection: "Connections",
};

/** Health colour for an attention group. High severity reads blocked, everything else needs attention. */
export function severityHealth(severity: string): "blocked" | "risk" {
  return severity === "high" ? "blocked" : "risk";
}

/** "purchased" → "Purchased", "ready_for_sale" → "Ready for sale". */
export function stateLabel(s: string | null | undefined): string {
  if (!s) return "Not recorded";
  const t = s.replace(/_/g, " ");
  return t.charAt(0).toUpperCase() + t.slice(1);
}
