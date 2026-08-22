import uuid
from datetime import date

from sqlalchemy import CheckConstraint, Date, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

from .enums import CreditDebitNoteType, LedgerAccount, check_values
from .mixins import TimestampMixin, UUIDPkMixin


class Payment(UUIDPkMixin, TimestampMixin, Base):
    """A single money movement, either received from a customer or sent to a
    supplier. direction disambiguates which side; exactly one of
    customer_id/supplier_id is set (enforced by ck_payments_single_party).
    """

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        CheckConstraint("direction IN ('inbound', 'outbound')", name="ck_payments_direction"),
        CheckConstraint(
            "(customer_id IS NOT NULL)::int + (supplier_id IS NOT NULL)::int = 1",
            name="ck_payments_single_party",
        ),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("customers.id", ondelete="RESTRICT")
    )
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="RESTRICT")
    )
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    method: Mapped[str | None] = mapped_column(String(30))
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(50))
    razorpay_payment_link_id: Mapped[str | None] = mapped_column(String(50))
    reference: Mapped[str | None] = mapped_column(String(100))


class PaymentAllocation(UUIDPkMixin, Base):
    """One payment can settle many invoices, and one invoice can be settled
    by many partial payments — this join table carries the amount applied
    to each pairing. Exactly one of customer_invoice_id/supplier_invoice_id
    is set, matching the payment's direction.
    """

    __tablename__ = "payment_allocations"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payment_allocations_amount_positive"),
        CheckConstraint(
            "(customer_invoice_id IS NOT NULL)::int + (supplier_invoice_id IS NOT NULL)::int = 1",
            name="ck_payment_allocations_single_invoice",
        ),
    )

    payment_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("payments.id", ondelete="CASCADE"), nullable=False
    )
    customer_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("customer_invoices.id", ondelete="RESTRICT")
    )
    supplier_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("supplier_invoices.id", ondelete="RESTRICT")
    )
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)


class CreditDebitNote(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "credit_debit_notes"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_credit_debit_notes_amount_positive"),
        CheckConstraint(
            f"note_type IN ({check_values(*CreditDebitNoteType)})",
            name="ck_credit_debit_notes_note_type",
        ),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    note_type: Mapped[str] = mapped_column(String(30), nullable=False)
    note_number: Mapped[str] = mapped_column(String(30), nullable=False)
    customer_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("customer_invoices.id", ondelete="RESTRICT")
    )
    supplier_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("supplier_invoices.id", ondelete="RESTRICT")
    )
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    reason: Mapped[str] = mapped_column(String(300), nullable=False)


class LedgerEntry(UUIDPkMixin, Base):
    """Double-entry postings. Every business event that touches money or
    inventory value posts a balanced set of rows here (debits == credits) —
    this is the general ledger, separate from stock_ledger (physical units).
    Append-only, same rule as stock_ledger.
    """

    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint(
            f"account IN ({check_values(*LedgerAccount)})", name="ck_ledger_entries_account"
        ),
        CheckConstraint("entry_side IN ('debit', 'credit')", name="ck_ledger_entries_entry_side"),
        CheckConstraint("amount > 0", name="ck_ledger_entries_amount_positive"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    account: Mapped[str] = mapped_column(String(30), nullable=False)
    entry_side: Mapped[str] = mapped_column(String(10), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    ref_type: Mapped[str] = mapped_column(String(50), nullable=False)
    ref_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    # Groups the balanced set of rows for one business event — sum(debit) ==
    # sum(credit) within a transaction_group_id, enforced in services/, not
    # the DB (a DB-level balance check needs a deferred trigger; deferred to
    # Phase 2 if it turns out to be worth the complexity).
    transaction_group_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
