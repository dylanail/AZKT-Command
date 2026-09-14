from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, BusinessRow

COST_CATEGORIES = ("purchase", "import", "transport", "recon", "parts", "labor", "selling", "storage", "other")


class CostItem(Base, BusinessRow):
    """One economic expense. estimate → quote → invoice are observations, not additive (spec §6.1–6.2)."""
    __tablename__ = "cost_items"
    vehicle_id: Mapped[str | None] = mapped_column(ForeignKey("vehicles.id"), nullable=True, index=True)
    category: Mapped[str] = mapped_column(String, default="other", index=True)
    description: Mapped[str] = mapped_column(String, default="")
    vendor_contact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    vendor_name: Mapped[str | None] = mapped_column(String, nullable=True)
    currency: Mapped[str] = mapped_column(String, default="USD")
    amount_estimated: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    amount_quoted: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    amount_invoiced: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    amount_credited: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    status: Mapped[str] = mapped_column(String, default="estimated", index=True)  # estimated|quoted|invoiced|partially_paid|paid|credited|cancelled
    invoice_ref: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    order_ref: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    fx_source: Mapped[str | None] = mapped_column(String, nullable=True)
    fx_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    usd_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)  # actual converted, when known
    shared: Mapped[bool] = mapped_column(Boolean, default=False)  # allocated across vehicles
    is_estimate_only: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    restated_from_id: Mapped[str | None] = mapped_column(String, nullable=True)
    restatement_note: Mapped[str | None] = mapped_column(String, nullable=True)

    @property
    def active_amount(self) -> Decimal | None:
        """Non-duplicated committed amount: invoiced, else quoted, else estimated."""
        for v in (self.amount_invoiced, self.amount_quoted, self.amount_estimated):
            if v is not None:
                return v
        return None


class CostEvidence(Base, BusinessRow):
    """A ledger row / emailed invoice / receipt observation, matched to one cost item (spec §6.1)."""
    __tablename__ = "cost_evidence"
    cost_item_id: Mapped[str | None] = mapped_column(ForeignKey("cost_items.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String, index=True)  # ledger_row|email_invoice|email_receipt|quote|receipt_photo|manual
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)  # message id / ledger row id / asset id
    source_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    extracted: Mapped[dict] = mapped_column(JSON, default=dict)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String, nullable=True)
    vendor: Mapped[str | None] = mapped_column(String, nullable=True)
    invoice_no: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    order_no: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    vehicle_ref_text: Mapped[str | None] = mapped_column(String, nullable=True)
    proposed_vehicle_id: Mapped[str | None] = mapped_column(String, nullable=True)
    proposed_cost_item_id: Mapped[str | None] = mapped_column(String, nullable=True)
    proposed_category: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[str] = mapped_column(String, default="low")
    match_state: Mapped[str] = mapped_column(String, default="unmatched", index=True)  # matched|proposed|ambiguous|unmatched|ignored|conflict
    match_reasons: Mapped[list] = mapped_column(JSON, default=list)
    discrepancy: Mapped[dict] = mapped_column(JSON, default=dict)
    reviewed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_settlement: Mapped[bool] = mapped_column(Boolean, default=False)  # only if ledger schema records verified settlement


class CostAllocation(Base, BusinessRow):
    """Allocation of a (shared) cost item to vehicles; totals balance to the source (invariant 5)."""
    __tablename__ = "cost_allocations"
    __table_args__ = (UniqueConstraint("cost_item_id", "vehicle_id", name="uq_cost_alloc"),)
    cost_item_id: Mapped[str] = mapped_column(ForeignKey("cost_items.id"), index=True)
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("vehicles.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String, default="USD")
    basis: Mapped[str] = mapped_column(String, default="explicit")  # explicit|equal|weighted
    confirmed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Payment(Base, BusinessRow):
    """Provider or manual payment record; namespaced by merchant (invariant 1; spec §6.3)."""
    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("provider", "merchant_id", "provider_payment_id", name="uq_payment_provider"),)
    provider: Mapped[str] = mapped_column(String, default="manual", index=True)  # square|manual|stripe|bank
    connection_id: Mapped[str | None] = mapped_column(String, nullable=True)
    merchant_id: Mapped[str] = mapped_column(String, default="")
    location_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    provider_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_customer_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_invoice_id: Mapped[str | None] = mapped_column(String, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String, default="USD")
    fee_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    net_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    refunded_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    status: Mapped[str] = mapped_column(String, default="reported", index=True)  # reported|pending|completed|refunded|partially_refunded|disputed|failed|cancelled
    payer_name: Mapped[str | None] = mapped_column(String, nullable=True)
    payer_email: Mapped[str | None] = mapped_column(String, nullable=True)
    contact_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    provider_version: Mapped[str | None] = mapped_column(String, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_kind: Mapped[str] = mapped_column(String, default="manual")  # webhook|api|email|manual
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String, nullable=True)  # manual evidence confirmation
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refunds: Mapped[list] = mapped_column(JSON, default=list)
    disputes: Mapped[list] = mapped_column(JSON, default=list)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    is_payout: Mapped[bool] = mapped_column(Boolean, default=False)  # bank payouts are not customer revenue


class Invoice(Base, BusinessRow):
    """A customer obligation (deposit, balance, reservation, shipping)."""
    __tablename__ = "invoices"
    kind: Mapped[str] = mapped_column(String, default="deposit", index=True)
    contact_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    opportunity_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    import_request_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    sale_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    vehicle_id: Mapped[str | None] = mapped_column(String, nullable=True)
    agreement_id: Mapped[str | None] = mapped_column(String, nullable=True)
    amount_due: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String, default="USD")
    amount_allocated: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String, default="open", index=True)  # open|partially_paid|paid|overpaid|cancelled|refunded
    provider_invoice_id: Mapped[str | None] = mapped_column(String, nullable=True)
    satisfied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")


class PaymentAllocation(Base, BusinessRow):
    """Confirmed allocation of a payment to an obligation (invariant 4)."""
    __tablename__ = "payment_allocations"
    payment_id: Mapped[str] = mapped_column(ForeignKey("payments.id"), index=True)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String, default="USD")
    state: Mapped[str] = mapped_column(String, default="proposed", index=True)  # proposed|confirmed|reversed
    confirmed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)


class Sale(Base, BusinessRow):
    """Reservation / sale case on a vehicle (spec §8.5). At most one active per vehicle (invariant 7)."""
    __tablename__ = "sales"
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("vehicles.id"), index=True)
    buyer_contact_id: Mapped[str] = mapped_column(ForeignKey("contacts.id"), index=True)
    opportunity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    agreement_id: Mapped[str | None] = mapped_column(String, nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String, default="USD")
    sales_tax: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    pass_through: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    credits: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))  # returns / price credits
    status: Mapped[str] = mapped_column(String, default="reserved", index=True)  # reserved|agreed|paid|completed|delivered|cancelled|expired
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reservation_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    agreed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)  # sale completion date (cohort)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    documents_checklist: Mapped[list] = mapped_column(JSON, default=list)
    aftercare: Mapped[list] = mapped_column(JSON, default=list)
    delivery_appointment_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    handoff_evidence: Mapped[list] = mapped_column(JSON, default=list)
    restatements: Mapped[list] = mapped_column(JSON, default=list)  # [{at, note, field, old, new}]
    notes: Mapped[str] = mapped_column(Text, default="")


class Agreement(Base, BusinessRow):
    __tablename__ = "agreements"
    kind: Mapped[str] = mapped_column(String, default="import")  # import|reservation|sale
    contact_id: Mapped[str] = mapped_column(ForeignKey("contacts.id"), index=True)
    import_request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    sale_id: Mapped[str | None] = mapped_column(String, nullable=True)
    agreement_version: Mapped[int] = mapped_column(Integer, default=1)
    terms: Mapped[dict] = mapped_column(JSON, default=dict)
    deposit_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    deposit_currency: Mapped[str | None] = mapped_column(String, nullable=True)
    exceptions: Mapped[list] = mapped_column(JSON, default=list)  # customer-specific concessions
    status: Mapped[str] = mapped_column(String, default="draft", index=True)  # draft|sent|signed|superseded|cancelled
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence_asset_id: Mapped[str | None] = mapped_column(String, nullable=True)
    supersedes_id: Mapped[str | None] = mapped_column(String, nullable=True)


class Document(Base, BusinessRow):
    __tablename__ = "documents"
    entity_kind: Mapped[str] = mapped_column(String, index=True)  # vehicle|sale|import_request|shipment
    entity_id: Mapped[str] = mapped_column(String, index=True)
    type: Mapped[str] = mapped_column(String)  # export_certificate|bill_of_lading|title|invoice|id|agreement|release|other
    status: Mapped[str] = mapped_column(String, default="pending")  # pending|on_file|conflicted|missing|verified
    asset_id: Mapped[str | None] = mapped_column(String, nullable=True)
    visibility: Mapped[str] = mapped_column(String, default="owner")
    notes: Mapped[str] = mapped_column(Text, default="")
    conflict: Mapped[dict] = mapped_column(JSON, default=dict)


class LedgerMapping(Base, BusinessRow):
    """Versioned column mapping for the Google Sheets ledger (spec §6.1)."""
    __tablename__ = "ledger_mappings"
    connection_id: Mapped[str | None] = mapped_column(String, nullable=True)
    sheet_id: Mapped[str] = mapped_column(String)
    sheet_title: Mapped[str | None] = mapped_column(String, nullable=True)
    tab_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tab_title: Mapped[str | None] = mapped_column(String, nullable=True)
    header_row: Mapped[int] = mapped_column(Integer, default=1)
    columns: Mapped[dict] = mapped_column(JSON, default=dict)  # {field: column letter/name}
    id_strategy: Mapped[str] = mapped_column(String, default="fingerprint")  # column:<name>|fingerprint
    currency_default: Mapped[str] = mapped_column(String, default="USD")
    mapping_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="draft", index=True)  # draft|previewed|active|superseded
    preview: Mapped[dict] = mapped_column(JSON, default=dict)  # sample matches + exceptions
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_by: Mapped[str | None] = mapped_column(String, nullable=True)


class LedgerRow(Base, BusinessRow):
    __tablename__ = "ledger_rows"
    __table_args__ = (UniqueConstraint("mapping_id", "row_fingerprint", name="uq_ledger_row"),)
    mapping_id: Mapped[str] = mapped_column(ForeignKey("ledger_mappings.id"), index=True)
    row_fingerprint: Mapped[str] = mapped_column(String)
    external_row_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    row_number_seen: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    values: Mapped[dict] = mapped_column(JSON, default=dict)
    formulas: Mapped[dict] = mapped_column(JSON, default=dict)
    source_revision: Mapped[str | None] = mapped_column(String, nullable=True)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String, default="new", index=True)  # new|matched|ambiguous|ignored|changed|moved
    cost_evidence_id: Mapped[str | None] = mapped_column(String, nullable=True)
