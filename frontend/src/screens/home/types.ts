/* Shapes served by backend/app/routers/home.py:
     GET /api/home?period=&start=&end=&tz=&horizon_days=   → services/home.py home()
     GET /api/home/metrics                                  → services/reporting.py metrics()
     GET /api/home/metrics/drilldown?metric=                → services/reporting.py drilldown()
     GET /api/home/timeline?compact=                        → services/timeline.py compact()
   Money is always {amount:"123.45", currency:"USD"} or null — never a bare number, never 0 for "unknown".
   A section the signed-in person may not see comes back as {available:false, reason}; a section that broke
   adds degraded:true. Nothing is silently omitted, so the screen renders every key it is given. */

export interface MoneyV { amount: string; currency: string }

/** Every section can arrive unavailable (permission) or degraded (it threw server-side). */
export interface Unavailable {
  available?: boolean;
  reason?: string;
  degraded?: boolean;
}

export const PERIOD_KINDS = ["month", "7d", "30d", "custom"] as const;
export type PeriodKind = (typeof PERIOD_KINDS)[number];
export function isPeriodKind(s: string | null): s is PeriodKind {
  return !!s && (PERIOD_KINDS as readonly string[]).includes(s);
}

export interface Period { kind: string; from: string; to: string; timezone: string; label: string }

/* ── 1. status ───────────────────────────────────────────────────────────── */
export type FreshnessState = "ok" | "warn" | "expired" | "degraded" | "disconnected" | string;
export interface Freshness { state: FreshnessState; label: string; last_success_at: string | null }
export interface ConnectionRow {
  provider: string;
  label: string;
  status: string;
  freshness: Freshness;
  last_success_at: string | null;
  required: boolean;
}
export interface HomeCounts { needs_decision: number; needs_attention: number; today: number }
export interface StatusSection extends Unavailable {
  summary: string;
  all_clear: boolean;
  all_clear_possible: boolean;
  stale_connections: string[];
  counts: HomeCounts;
  connections: ConnectionRow[];
  required_connections?: string[];
  as_of?: string;
  timezone?: string;
  model_used?: boolean;
}

/* ── 2. business overview ────────────────────────────────────────────────── */
export interface CategoryTotal { usd: MoneyV | null; lines: number; fx_missing: number; estimated: number }

export interface CountMetric { label: string; count: number; definition: string }
export interface MoneyMetric {
  label: string;
  value: MoneyV | null;
  currency: string;
  unavailable_reason?: string | null;
  note?: string;
}
export interface NetSalesMetric extends MoneyMetric { excludes: string[] }
export interface CostsMetric extends MoneyMetric {
  by_category: Record<string, CategoryTotal> | null;
  unallocated: Array<{ vehicle_id: string; reason: string }>;
  missing_lines: { fx_missing: number; estimated: number; needing_review: number };
}
export interface ProfitMetric extends MoneyMetric {
  /** "recorded" once the configured cost-completeness checks pass, else "estimated". */
  state: string;
  reasons: string[];
  caveat: string;
}
export interface MarginMetric {
  label: string;
  available: boolean;
  value: string | null;
  percent?: string | null;
  numerator: MoneyV | null;
  denominator: MoneyV | null;
  basis: string;
  unavailable_reason: string | null;
}
export interface DurationMetric {
  label: string;
  definition: string;
  median_days: number | null;
  included: number;
  excluded: number;
  excluded_vehicle_ids: string[];
  unavailable_reason: string | null;
}
export interface DaysToSaleMetric extends DurationMetric {
  drilldown: { received_to_ready: DurationMetric; listed_to_sold: DurationMetric };
}
export interface UnsoldVehicle {
  vehicle_id: string;
  stock_no: string | null;
  allocation: string;
  label: string;
  cost_usd: MoneyV | null;
  flags: { fx_missing_lines: number; estimated_lines: number };
  asking_price: MoneyV | null;
}
export interface UnsoldMetric extends MoneyMetric {
  as_of: string;
  point_in_time: boolean;
  vehicles: number;
  reserved: UnsoldVehicle[];
  coverage: { vehicles_without_costs: string[]; fx_missing_lines: number; estimated_lines: number };
}
export interface ProjectedMetric extends MoneyMetric {
  state: string;
  vehicle_ids: string[];
  coverage: { eligible_vehicles: number; considered: number; ratio: string };
  unknown_components: Array<{ vehicle_id: string | null; stock_no: string | null; reason: string }>;
}
export interface CashFlows {
  label: string;
  basis?: string;
  period?: { from: string; to: string };
  currency?: string;
  receipts?: MoneyV | null;
  refunds?: MoneyV | null;
  payouts?: MoneyV | null;
  counts: { receipts: number; refunds: number; payouts: number; unsettled_excluded: number; unknown_currency: number };
  unsettled?: Array<Record<string, unknown>>;
  note?: string;
  money_hidden?: boolean;
}
export interface MetricValues {
  vehicles_sold: CountMetric;
  net_vehicle_sales_value: NetSalesMetric;
  vehicle_costs_sold_cohort: CostsMetric;
  gross_profit: ProfitMetric;
  gross_margin: MarginMetric;
  days_to_sale: DaysToSaleMetric;
  unsold_inventory_cost: UnsoldMetric;
  projected_gross_profit: ProjectedMetric;
}
export interface Completeness {
  label: string;
  recorded: boolean;
  required_cost_categories: string[];
  required_source: string;
  missing_required: Array<{ vehicle_id: string; stock_no: string | null; sale_id: string; gaps: Array<{ category: string; reason: string }> }>;
  sale_adjustment_gaps: Array<{ sale_id: string; exceptions: unknown[] }>;
  reasons: string[];
  counts: {
    sales: number; cost_items: number; estimated_lines: number; fx_missing_lines: number;
    allocations_needing_review: number; vehicles_without_costs: number;
  };
}
export interface Cohort {
  definition: string;
  basis: string;
  sale_ids: string[];
  vehicle_ids: string[];
  excluded: string[];
  sale_currencies: string[];
}
export interface Restatement { source?: string; field?: string; at?: string; note?: string; reason?: string; [k: string]: unknown }

export interface BusinessOverview extends Unavailable {
  period?: Period;
  currency?: string;
  as_of?: string;
  stale?: boolean;
  served_from?: string;
  stale_reason?: string | null;
  money_hidden?: boolean;
  values?: MetricValues;
  completeness?: Completeness;
  cohort?: Cohort;
  restatements?: Restatement[];
  cash_flows?: CashFlows;
  drilldown_metrics?: string[];
  note?: string;
}

/* ── 3. needs your decision ──────────────────────────────────────────────── */
export interface DecisionItem {
  approval_id: string;
  kind: string;
  action: string;
  title: string;
  related_record: { kind: string | null; id: string | null };
  consequence: Record<string, unknown> & { amount?: string; currency?: string; money_hidden?: boolean };
  targets: Record<string, unknown>;
  deadline: string | null;
  deadline_label: string;
  expired: boolean;
  review_path: string;
  version: number;
  requested_by: string | null;
  requested_at: string | null;
}
export interface NeedsDecisionSection extends Unavailable { items: DecisionItem[]; total: number; note?: string }

/* ── 4. needs attention ──────────────────────────────────────────────────── */
export type AttentionKind =
  | "blocker" | "missing_document" | "overdue_commitment" | "overdue_task"
  | "unmatched_payment" | "unmatched_message" | "failed_publication" | "stale_connection" | string;

export interface AttentionItem {
  kind?: string;
  id?: string;
  title?: string;
  reason?: string | null;
  type?: string;
  status?: string;
  vehicle_id?: string | null;
  owner_user_id?: string | null;
  due_at?: string | null;
  text?: string;
  provider?: string;
  label?: string;
  state?: string;
  channel?: string;
  last_success_at?: string | null;
  [k: string]: unknown;
}
export interface AttentionGroup {
  /** Stable key for the underlying problem — the API already grouped duplicates, never re-flatten. */
  problem: string;
  kind: AttentionKind;
  title: string;
  detail: string;
  owner_user_id: string | null;
  next_action: string;
  due_at: string | null;
  due_label: string | null;
  count: number;
  items: AttentionItem[];
  link: string | null;
  severity: "high" | "normal" | string;
}
export interface NeedsAttentionSection extends Unavailable {
  groups: AttentionGroup[];
  total: number;
  items_total: number;
  note?: string;
}

/* ── 5. today ────────────────────────────────────────────────────────────── */
export interface TodayItem {
  kind: "task" | "delivery" | string;
  id: string;
  title: string;
  type: string | null;
  status: string;
  at: string | null;
  at_label: string;
  owner_user_id: string | null;
  vehicle_id: string | null;
  contact_id: string | null;
  opportunity_id: string | null;
  pipeline: "sales" | "operations" | string;
  priority: string | null;
}
export interface TodaySection extends Unavailable {
  items: TodayItem[];
  total: number;
  day?: { from: string; to: string };
  timezone?: string;
  note?: string;
}

/* ── 6. vehicle timeline (compact) ───────────────────────────────────────── */
export interface UpcomingEvent {
  kind: string;
  label: string;
  at: string;
  at_label: string;
  /** "sourced" · "internal target" · "planned milestone" · "estimated shipment notice" — never invented. */
  basis: string;
  source: { kind: string | null; ref: string | null; shipment_id?: string | null };
  ref: Record<string, string | null>;
}
export interface NextAction {
  title: string | null;
  owner_id: string | null;
  due_at: string | null;
  due_label: string;
  source: string | null;
}
export interface TimelineItem {
  vehicle_id: string;
  stock_no: string | null;
  title: string | null;
  allocation: string | null;
  health: string | null;
  states: { logistics: string; recon: string; commercial: string; documents: string };
  current_stage: string;
  current_stage_dimension: string;
  days_in_stage: number | null;
  days_since_acquisition: number | null;
  last_milestone: { kind: string; label: string; at: string } | null;
  next_event: UpcomingEvent | null;
  next_action: NextAction;
  blocked_work: number;
  open_shipment: boolean;
  shipment_open_while_sold: boolean;
}
export interface TimelineSection extends Unavailable {
  items: TimelineItem[];
  total: number;
  returned: number;
  as_of?: string;
  timezone?: string;
  horizon_days?: number;
  turnaround_excluded?: Record<string, number>;
}

/* ── 7. in progress ──────────────────────────────────────────────────────── */
export interface ProgressItem {
  kind: "mission" | "case" | string;
  id: string;
  title: string;
  status: string;
  waiting_on: string | null;
  role: string | null;
  next_action?: string | null;
  next_check_at: string | null;
  next_check_label: string;
  owner_user_id: string | null;
  case_id?: string | null;
  vehicle_id?: string | null;
  case_kind?: string | null;
}
export interface InProgressSection extends Unavailable { items: ProgressItem[]; total: number; note?: string }

/* ── 8. completed ────────────────────────────────────────────────────────── */
export interface CompletedItem {
  id: string;
  at: string | null;
  at_label: string;
  what: string;
  kind: string | null;
  state: string | null;
  entity_kind: string | null;
  entity_id: string | null;
  actor: string | null;
  activity_path: string;
}
export interface CompletedSection extends Unavailable {
  items: CompletedItem[];
  total: number;
  collapsed: boolean;
  since?: string;
  link?: string;
}

/* ── the page ────────────────────────────────────────────────────────────── */
export interface HomeResp {
  as_of: string;
  timezone: string;
  model_used: boolean;
  status: StatusSection;
  business_overview: BusinessOverview;
  needs_decision: NeedsDecisionSection;
  needs_attention: NeedsAttentionSection;
  today: TodaySection;
  vehicle_timeline: TimelineSection;
  in_progress: InProgressSection;
  completed: CompletedSection;
  sections: string[];
}

/* ── drill-down ──────────────────────────────────────────────────────────── */
export const DRILLDOWN_METRICS = [
  "vehicles_sold", "net_vehicle_sales_value", "vehicle_costs_sold_cohort", "gross_profit",
  "gross_margin", "days_to_sale", "unsold_inventory_cost", "projected_gross_profit", "cash_flows",
] as const;
export type DrilldownMetric = (typeof DRILLDOWN_METRICS)[number];
export function isDrilldownMetric(s: string | null): s is DrilldownMetric {
  return !!s && (DRILLDOWN_METRICS as readonly string[]).includes(s);
}

/** One sold vehicle inside a drill-down (reporting.compute per_vehicle). */
export interface SaleRow {
  sale_id: string;
  vehicle_id: string;
  stock_no: string | null;
  net_sale_value: MoneyV | null;
  net_sale_value_is_usd: boolean;
  costs: MoneyV | null;
  gross_profit: MoneyV | null;
  flags: { fx_missing_lines: number; estimated_lines: number; allocations_needing_review: number; no_costs: boolean };
  by_category: Record<string, CategoryTotal> | null;
  days_to_sale: number | null;
  completed_at: string | null;
  cost_item_ids: string[];
}
export interface FinanceFilters {
  /** The API path Finance reads for exactly this cohort — mapped to a Finance tab by financeHref(). */
  path: string;
  period?: string;
  from?: string;
  to?: string;
  tz?: string;
  cohort?: string;
  as_of?: string;
}
export interface DrilldownResp {
  metric: string;
  period: Period;
  currency: string;
  as_of: string;
  stale: boolean;
  money_hidden: boolean;
  finance_filters: FinanceFilters;
  cohort: Cohort;
  completeness: Completeness;
  sale_ids?: string[];
  vehicle_ids?: string[];
  cost_item_ids?: string[];
  rows?: SaleRow[];
  by_category?: Record<string, CategoryTotal> | null;
  unallocated?: Array<{ vehicle_id: string; reason: string }>;
  durations?: Record<string, DurationMetric>;
  excluded_vehicle_ids?: string[];
  unknown_components?: Array<{ vehicle_id: string | null; stock_no: string | null; reason: string }>;
  cash_flows?: CashFlows;
  restatements?: Restatement[];
}
