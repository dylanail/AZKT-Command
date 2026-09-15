/* Shapes served by backend/app/routers/finance.py via services/finance.py (serialize_*),
   finance_queries.py (tabs, vehicle money, sold cohort) and ledger.py (mapping, rows).
   Money is always {amount: "123.45", currency: "USD"} or null — never a bare number. */

export interface MoneyV { amount: string; currency: string }

export const COST_CATEGORIES = ["purchase", "import", "transport", "recon", "parts", "labor", "selling", "storage", "other"] as const;
export type CostCategory = (typeof COST_CATEGORIES)[number];

export const EVIDENCE_KIND_LABELS: Record<string, string> = {
  ledger_row: "Ledger row",
  email_invoice: "Emailed invoice",
  email_receipt: "Emailed receipt",
  quote: "Quote",
  receipt_photo: "Receipt photo",
  manual: "Entered by hand",
  credit_note: "Credit note",
};

export const MATCH_STATE_LABELS: Record<string, string> = {
  matched: "Matched",
  proposed: "Proposed match",
  ambiguous: "Several possible matches",
  unmatched: "Unmatched",
  ignored: "Ignored",
  conflict: "Conflict",
};

export const CATEGORY_LABELS: Record<string, string> = {
  purchase: "Purchase",
  import: "Import",
  transport: "Transport",
  recon: "Recon",
  parts: "Parts",
  labor: "Labor",
  selling: "Selling",
  storage: "Storage",
  other: "Other",
};

export const INVOICE_KIND_LABELS: Record<string, string> = {
  deposit: "Deposit",
  balance: "Balance",
  reservation: "Reservation",
  shipping: "Shipping",
};

export const ALLOCATION_FLAG_LABELS: Record<string, string> = {
  partial: "Partial — less than the obligation",
  overpayment: "Overpaid — more than the obligation",
  currency_mismatch: "Payment and obligation are in different currencies",
  conversion_required: "Needs an explicit conversion rate and source",
  ambiguous: "Could be either obligation — pick one",
};

export const PAYMENT_METHODS = ["cash", "zelle", "wire", "check", "card", "other"] as const;

export interface EvidenceCandidate { cost_item_id: string; reasons: string[]; score: number }
export interface Discrepancy {
  field?: string; evidence?: string; item?: string; currency?: string; difference?: string;
  [k: string]: unknown;
}

export interface Evidence {
  id: string; version: number; cost_item_id: string | null; kind: string;
  source_ref: string | null; source_hash: string | null; source_link: string | null;
  extracted: Record<string, unknown>;
  amount: MoneyV | null; vendor: string | null; invoice_no: string | null; order_no: string | null;
  occurred_at: string | null; vehicle_ref_text: string | null;
  proposed_vehicle_id: string | null; proposed_cost_item_id: string | null; proposed_category: string | null;
  confidence: string | null; match_state: string; match_reasons: string[];
  discrepancy: Discrepancy; candidates: EvidenceCandidate[];
  reviewed_by: string | null; reviewed_at: string | null;
  is_settlement: boolean; is_credit: boolean; revision: number; supersedes_id: string | null; is_current: boolean;
  ledger_row_id: string | null; review_note: string | null; applied: Record<string, unknown>; created_at: string | null;
}

export interface CostAllocation {
  id: string; version: number; cost_item_id: string; vehicle_id: string;
  amount: MoneyV | null; amount_credited: MoneyV | null; net_amount: MoneyV | null;
  basis: string; state: string; weight: string | null;
  components: Record<string, unknown>; credits: unknown[]; review_reasons: string[];
  confirmed_by: string | null; confirmed_at: string | null;
}

export interface CostItem {
  id: string; version: number; vehicle_id: string | null; category: string; description: string;
  vendor_name: string | null; vendor_contact_id: string | null; currency: string; status: string;
  amount_estimated: MoneyV | null; amount_quoted: MoneyV | null; amount_invoiced: MoneyV | null;
  amount_paid: MoneyV | null; amount_credited: MoneyV | null; active_amount: MoneyV | null;
  net_amount: MoneyV | null; remaining_payable: MoneyV | null;
  tax_amount: MoneyV | null; freight_amount: MoneyV | null; discount_amount: MoneyV | null;
  invoice_ref: string | null; order_ref: string | null; occurred_at: string | null;
  fx: { rate: string | null; source: string | null; date: string | null; usd_amount: MoneyV | null; actual: boolean };
  shared: boolean; is_estimate_only: boolean; allocation_state: string | null;
  observations: Record<string, unknown>[]; restatements: Record<string, unknown>[]; credits: Record<string, unknown>[];
  restated_from_id: string | null; restatement_note: string | null; notes: string | null;
  is_pass_through: boolean; dedupe_key: string | null;
  allocations: CostAllocation[] | null; created_at: string | null;
  /** payables tab only */
  remaining?: MoneyV | null;
}

export interface Payment {
  id: string; version: number; provider: string; merchant_id: string | null;
  provider_payment_id: string | null; provider_invoice_id: string | null;
  amount: MoneyV | null; fee_amount: MoneyV | null; net_amount: MoneyV | null; refunded_amount: MoneyV | null;
  available_amount: MoneyV | null; allocated_amount: MoneyV | null; unallocated_amount: MoneyV | null;
  status: string; status_label: string; payer_name: string | null; payer_email: string | null;
  contact_id: string | null; occurred_at: string | null; fetched_at: string | null;
  source_kind: string | null; source_ref: string | null;
  confirmed_by: string | null; confirmed_at: string | null;
  refunds: unknown[]; disputes: unknown[]; is_payout: boolean;
  report_flags: string[]; report_source: Record<string, unknown>;
  exceptions: Record<string, unknown>[]; method: string | null;
  evidence_asset_id: string | null; evidence_ref: string | null; created_at: string | null;
}

export interface PaymentAllocation {
  id: string; version: number; payment_id: string; invoice_id: string;
  amount: MoneyV | null; applied_amount: MoneyV | null; state: string;
  confirmed_by: string | null; confirmed_at: string | null; reversed_at: string | null;
  reason: string | null; candidate_group: string | null; ambiguous: boolean;
  conversion: Record<string, unknown>; flags: string[];
  proposed_by: string | null; reversal_kind: string | null; created_at: string | null;
}

export interface Invoice {
  id: string; version: number; kind: string; contact_id: string | null; opportunity_id: string | null;
  import_request_id: string | null; sale_id: string | null; vehicle_id: string | null; agreement_id: string | null;
  amount_due: MoneyV | null; amount_allocated: MoneyV | null; remaining: MoneyV | null; overpaid_by: MoneyV | null;
  due_at: string | null; status: string; provider_invoice_id: string | null; satisfied_at: string | null;
  notes: string | null; terms_source: string | null; dedupe_key: string | null;
  handoff: Record<string, unknown>; reopened_at: string | null; exceptions: Record<string, unknown>[];
  created_at: string | null;
}

/* ── tabs ─────────────────────────────────────────────────────────────── */

export type MatchingItem =
  | (Evidence & { item_kind: "cost_evidence" })
  | (PaymentAllocation & { item_kind: "payment_allocation" })
  | (CostAllocation & { item_kind: "cost_allocation" })
  | (Payment & { item_kind: "payment_reported" });

export interface NeedsMatchingResp {
  items: MatchingItem[];
  total: number;
  counts: { evidence: number; payment_allocations: number; cost_allocations: number; payments_reported: number };
}

export interface ReceivablesResp { items: Invoice[]; total: number; outstanding: Record<string, string> }
export interface PayablesResp { items: CostItem[]; total: number; outstanding: Record<string, string> }

export interface CategoryTotal { usd: MoneyV | null; lines: number; fx_missing: number; estimated: number }

export interface VehicleCostRow {
  vehicle_id: string; stock_no: string | null; title: string | null; allocation: string | null;
  estimated_total: MoneyV | null; committed_invoiced: MoneyV | null; lines: number;
  by_category: Record<string, CategoryTotal>;
  flags: { fx_missing_lines: number; estimated_lines: number; allocations_needing_review: number };
  label: string;
}
export interface VehicleCostsResp { items: VehicleCostRow[]; total: number }

export interface VehicleMoney {
  vehicle_id: string; stock_no: string | null; currency_basis: string; missing?: boolean;
  estimated_total: MoneyV | null; committed_invoiced: MoneyV | null; cash_paid: MoneyV | null;
  remaining_payable: MoneyV | null;
  unmatched_evidence: { count: number; totals: Record<string, string> };
  approved_sale_price: MoneyV | null; sale_price_label: string | null;
  sale_id: string | null; sale_status: string | null;
  estimated_margin: MoneyV | null; margin_label: string;
  completeness: {
    label: string; estimated_lines: number; fx_missing_lines: number; cash_fx_missing: number;
    unmatched_evidence: number; allocations_needing_review: number; notes: string[];
    components: { estimated: number; invoiced: number };
  };
  by_category: Record<string, CategoryTotal>;
  lines: Array<{
    cost_item_id: string; category: string; description: string; vendor: string | null; currency: string;
    amount: MoneyV | null; usd_amount: MoneyV | null; fx_missing: boolean; status: string; estimate: boolean;
    basis: string; allocation_state: string | null; needs_review: boolean; pass_through: boolean; shared: boolean;
    paid: MoneyV | null; credited: MoneyV | null; occurred_at: string | null; fx_actual: boolean;
  }>;
  as_of: string;
}

export interface SoldSaleRow {
  sale: Record<string, unknown>; vehicle_id: string; stock_no: string | null;
  net_sale_value: MoneyV | null; net_sale_value_usd: MoneyV | null;
  sales_tax_excluded: MoneyV | null; pass_through_excluded: MoneyV | null;
  costs_usd: MoneyV | null; by_category: Record<string, CategoryTotal>;
  gross_profit_usd: MoneyV | null;
  flags: { fx_missing_lines: number; estimated_lines: number; allocations_needing_review: number; no_costs: boolean };
  days_to_sale: number | null; completed_at: string | null; restatements: Record<string, unknown>[];
}

export interface SoldCohortResp {
  period: { from: string; to: string }; currency: string; as_of: string;
  vehicles_sold: number;
  net_sales_value: MoneyV | null; costs_total: MoneyV | null; costs_by_category: Record<string, CategoryTotal>;
  gross_profit: MoneyV | null; gross_profit_label: string;
  gross_margin: string | null; gross_margin_note: string | null;
  days_to_sale_median: number | null; days_to_sale_included: number; days_to_sale_excluded: number;
  flags: {
    fx_missing_lines: number; estimated_lines: number; allocations_needing_review: number;
    unallocated: Array<{ vehicle_id: string; reason: string }>;
    sale_currencies: string[]; unknown_net: boolean; excluded: string[];
  };
  restatements: Record<string, unknown>[];
  sales: SoldSaleRow[];
}

export interface FinanceSummary {
  as_of: string;
  needs_matching: { evidence: number; payment_allocations: number; cost_allocations: number; payments_reported: number };
  receivables_count: number; payables_count: number;
  vehicles_sold_this_month: number; period: { from: string; to: string };
  status: { needs_matching: boolean; receivables_open: boolean; payables_open: boolean };
  receivables_outstanding: Record<string, string> | null;
  payables_outstanding: Record<string, string> | null;
  sold_cohort: {
    net_sales_value: MoneyV | null; costs_total: MoneyV | null; gross_profit: MoneyV | null;
    gross_profit_label: string; gross_margin: string | null; gross_margin_note: string | null;
    flags: SoldCohortResp["flags"];
  } | null;
  money_hidden: boolean;
}

/* ── ledger ───────────────────────────────────────────────────────────── */

export const LEDGER_FIELDS = [
  "date", "vendor", "description", "amount", "currency", "vehicle_ref", "invoice_no", "category", "paid_flag", "row_id",
] as const;
export type LedgerField = (typeof LEDGER_FIELDS)[number];

export const LEDGER_FIELD_LABELS: Record<string, string> = {
  date: "Date",
  vendor: "Vendor / payee",
  description: "Description",
  amount: "Amount",
  currency: "Currency",
  vehicle_ref: "Vehicle reference",
  invoice_no: "Invoice / receipt number",
  category: "Category",
  paid_flag: "Paid flag",
  row_id: "Row id (stable identity)",
};

export const LEDGER_REQUIRED_FIELDS: LedgerField[] = ["date", "vendor", "amount"];

export interface LedgerPreviewSample {
  row_number: number | null; fingerprint: string;
  parsed: Record<string, unknown>;
  already_imported: boolean;
  match: { state: string; reasons: string[]; cost_item_id: string | null } | null;
  formulas: Record<string, string>;
}

export interface LedgerMapping {
  id: string; version: number; connection_id: string | null;
  sheet_id: string; sheet_title: string | null; tab_id: string | null; tab_title: string | null;
  header_row: number | null; columns: Record<string, string>;
  id_strategy: string | null; currency_default: string | null; mapping_version: number | null;
  status: string;
  preview: {
    at?: string; source_revision?: string | null; rows_total?: number; sampled?: number;
    samples?: LedgerPreviewSample[];
    exceptions?: Array<{ row_number: number | null; exception: unknown }>;
    id_strategy?: string | null;
    settlement?: { column: string | null; verified: boolean; note: string | null };
  };
  activated_at: string | null; activated_by: string | null;
  settlement_column: string | null; settlement_verified: boolean;
  detected: Record<string, unknown>; source_revision: string | null;
  last_import_at: string | null; last_import: Record<string, unknown>;
  sheet_link: string; created_at: string | null;
}
export interface LedgerMappingsResp { items: LedgerMapping[]; total: number }

export interface LedgerRow {
  id: string; version: number; mapping_id: string; row_fingerprint: string; stable_key: string | null;
  external_row_id: string | null; row_number_seen: number | null; previous_row_number: number | null;
  values: Record<string, unknown>; formulas: Record<string, string>; parsed: Record<string, unknown>;
  source_revision: string | null; retrieved_at: string | null; last_seen_at: string | null;
  status: string; cost_evidence_id: string | null; revision: number;
  history: unknown[]; exceptions: unknown[]; value_hash: string | null;
}
export interface LedgerRowsResp { items: LedgerRow[]; total: number }

export const LEDGER_ROW_STATUS_LABELS: Record<string, string> = {
  new: "New",
  matched: "Matched",
  ambiguous: "Ambiguous",
  changed: "Value changed",
  moved: "Moved row",
  missing: "Gone from the sheet",
  imported: "Imported",
  ignored: "Ignored",
};

export function ledgerRowTone(status: string): Tone {
  if (status === "matched") return "ok";
  if (status === "ambiguous" || status === "missing") return "risk";
  if (status === "changed") return "wait";
  if (status === "moved") return "soft";
  return "amber";
}

export const LEDGER_MAPPING_STATUS_LABELS: Record<string, string> = {
  draft: "Draft",
  previewed: "Previewed",
  active: "Active",
  superseded: "Superseded",
};

/* ── shared helpers ───────────────────────────────────────────────────── */

export type Tone = "neutral" | "soft" | "act" | "amber" | "blocked" | "risk" | "ok" | "wait";

export function matchStateTone(state: string): Tone {
  if (state === "conflict") return "blocked";
  if (state === "ambiguous" || state === "unmatched") return "risk";
  if (state === "proposed") return "wait";
  if (state === "matched") return "ok";
  return "soft";
}

export function invoiceStatusTone(status: string): Tone {
  if (status === "paid") return "ok";
  if (status === "overpaid") return "risk";
  if (status === "partially_paid") return "wait";
  if (status === "void" || status === "cancelled") return "soft";
  return "amber";
}

export function n(v: number | null | undefined): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

/** Sum of the matching counts — the number the tab badge shows. */
export function matchingTotal(c: FinanceSummary["needs_matching"] | undefined): number {
  if (!c) return 0;
  return n(c.evidence) + n(c.payment_allocations) + n(c.cost_allocations) + n(c.payments_reported);
}

/** "2 400.00 USD · 118 000 JPY" for the {currency: amount} maps the tabs return. */
export function outstandingPairs(m: Record<string, string> | null | undefined): Array<[string, string]> {
  if (!m) return [];
  return Object.entries(m);
}
