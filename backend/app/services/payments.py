"""Payment allocation, aging, and outstanding-balance reporting — symmetric
across Accounts Receivable (customer payments) and Accounts Payable
(supplier payments), the same "one direction, one shared function" pattern
pricing.py uses for O2C/P2P GST and procurement.py/sales.py use for
PO/SO pricing. `party_type` ("customer" | "supplier") picks the side;
almost everything else is shared code.

Split like matching.py/pricing.py: `allocate_payment()` and
`aging_bucket_for()` are pure functions (no DB access, table-driven-
tested); `record_payment()`, `outstanding_balance()` and `aging_report()`
are thin async wrappers that load rows, call the pure functions, and (for
`record_payment()`) persist `Payment` + `PaymentAllocation` rows and
update invoice `amount_paid`/`status`. Like every other services/ module,
nothing here commits — the caller owns the transaction.

**Allocation** (roadmap 2.10's "allocation, partial payments"): a payment
either carries explicit `allocations` (caller picks which invoice(s) get
how much — used when a customer says "this cheque is for INV-042") or
none (auto-allocate oldest-due-first — the standard collections/dunning
default, and what a bulk bank-statement import would use). Either way the
math lives in `allocate_payment()`, a pure function: given a payment
amount and the party's open invoices, it returns an `AllocationPlan` —
per-invoice amounts plus whatever's left unapplied.

**Over-payment handling** (roadmap 2.10): the `Payment` row itself always
records the full amount actually received/sent — never capped or split
across a synthetic row, since that's real money movement and the ledger
should reflect it. What CAN be capped is how much of it applies to any
one invoice: `customer_invoices`/`supplier_invoices` both have a
`CHECK (amount_paid <= total)`, so an allocation can never push an
invoice's `amount_paid` past its `total`. Any payment amount beyond what
every open invoice can absorb comes back as `unapplied_amount` on both
`AllocationPlan` and `RecordPaymentResult` — surfaced, not silently
dropped. **This is deliberately NOT persisted as a "customer credit
balance" or "on-account cash" row**: the Phase 1 schema has no such
table (nothing tracks money received that isn't tied to an invoice).
Same situation procurement.py documented for supplier price history and
matching.py documented for bank-detail-change risk — the fact isn't
derivable/storable yet, not silently swept under the rug. A future
`account_credits` table (or a zero-invoice `PaymentAllocation` variant)
is the natural place to add it; until then, an over-payment's excess is
simply an under-allocated `Payment` row a human applies manually later.

**Aging buckets** (roadmap 2.10, literally "0-30/31-60/61-90/90+"):
`aging_bucket_for()` computes `days_overdue = max(0, as_of - due_date)`
and buckets it into exactly those four ranges — an invoice not yet due
(negative raw days) clamps to 0 and lands in "0-30" alongside genuinely-
just-overdue invoices, which is the standard AR/AP aging convention (a
"not yet due" column is a Phase 5/6 reporting nicety, not a different
bucket scheme).

**"Open" invoice statuses are a deliberate, documented choice per side**:
customer invoices are payable once ISSUED (DRAFT hasn't been sent yet;
PAID/VOID have nothing left to pay); supplier invoices are payable once
RECEIVED/MATCHED/APPROVED but NOT BLOCKED — paying a three-way-match-
blocked invoice is exactly the kind of "AI/automation moves money
without a human gate" mistake the roadmap's guardrails forbid, so it's
excluded here at the query level, not left to the caller to remember.

**No PARTIALLY_PAID for supplier invoices**: `SupplierInvoiceStatus` has
no such value (see `app/db/models/enums.py`) — only `customer_invoices`'
`InvoiceStatus` does. A partially-paid supplier invoice's `amount_paid`
still updates correctly; its `status` just isn't moved (stays whatever
matching.py/the future approvals flow left it at) until it's fully paid,
at which point it flips to PAID. Documented here rather than silently
inconsistent with the customer side.
"""

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.db.models.enums import InvoiceStatus, SupplierInvoiceStatus
from app.db.models.finance import Payment, PaymentAllocation
from app.db.models.partners import Customer, Supplier
from app.db.models.purchase import SupplierInvoice
from app.db.models.sales import CustomerInvoice

ZERO = Decimal("0")

PARTY_CUSTOMER = "customer"
PARTY_SUPPLIER = "supplier"

_CUSTOMER_OPEN_STATUSES = (
    InvoiceStatus.ISSUED,
    InvoiceStatus.PARTIALLY_PAID,
    InvoiceStatus.OVERDUE,
)
_SUPPLIER_OPEN_STATUSES = (
    SupplierInvoiceStatus.RECEIVED,
    SupplierInvoiceStatus.MATCHED,
    SupplierInvoiceStatus.APPROVED,
)

AGING_BUCKETS = ("0-30", "31-60", "61-90", "90+")


class AllocationLine(BaseModel):
    invoice_id: uuid.UUID
    amount: Decimal = Field(gt=0)


class OpenInvoiceInput(BaseModel):
    invoice_id: uuid.UUID
    total: Decimal
    amount_paid: Decimal
    due_date: date


class AllocationPlan(BaseModel):
    allocations: list[AllocationLine]
    unapplied_amount: Decimal


class InvoiceAllocationResult(BaseModel):
    invoice_id: uuid.UUID
    amount_applied: Decimal
    new_amount_paid: Decimal
    new_status: str


class RecordPaymentResult(BaseModel):
    payment_id: uuid.UUID
    party_type: str
    party_id: uuid.UUID
    amount: Decimal
    invoices: list[InvoiceAllocationResult]
    unapplied_amount: Decimal


class AgingLine(BaseModel):
    invoice_id: uuid.UUID
    party_id: uuid.UUID
    due_date: date
    outstanding_amount: Decimal
    days_overdue: int
    bucket: str


class AgingSummary(BaseModel):
    as_of: date
    total_outstanding: Decimal
    bucket_totals: dict[str, Decimal]


def _remaining(invoice: OpenInvoiceInput) -> Decimal:
    return invoice.total - invoice.amount_paid


def allocate_payment(
    *,
    payment_amount: Decimal,
    invoices: list[OpenInvoiceInput],
    requested: list[AllocationLine] | None = None,
) -> AllocationPlan:
    """Pure function — no DB access.

    `requested=None` auto-allocates oldest-due-first, greedily filling
    each invoice's remaining balance until the payment is exhausted.
    `requested=[...]` applies exactly those amounts (each capped to that
    invoice's own remaining balance — exceeding it is a caller error, not
    silently clamped, since an explicit allocation is presumed intentional).
    Either way, `AllocationPlan.unapplied_amount` is whatever's left of
    `payment_amount` after every allocation — zero when it lines up
    exactly, positive on an over-payment.
    """
    if payment_amount <= ZERO:
        raise ValueError("payment_amount must be positive")

    invoices_by_id = {invoice.invoice_id: invoice for invoice in invoices}

    if requested is not None:
        if not requested:
            raise ValueError("requested allocations, if provided, must not be empty")
        seen: set[uuid.UUID] = set()
        total_requested = ZERO
        for line in requested:
            if line.invoice_id in seen:
                raise ValueError(f"duplicate allocation for invoice {line.invoice_id}")
            seen.add(line.invoice_id)
            invoice = invoices_by_id.get(line.invoice_id)
            if invoice is None:
                raise ValueError(f"invoice {line.invoice_id} is not an open invoice for this party")
            remaining = _remaining(invoice)
            if line.amount > remaining:
                raise ValueError(
                    f"allocation of {line.amount} to invoice {line.invoice_id} exceeds its "
                    f"outstanding balance of {remaining}"
                )
            total_requested += line.amount
        if total_requested > payment_amount:
            raise ValueError(
                f"requested allocations total {total_requested} exceed payment amount "
                f"{payment_amount}"
            )
        return AllocationPlan(
            allocations=list(requested), unapplied_amount=payment_amount - total_requested
        )

    remaining_payment = payment_amount
    allocations: list[AllocationLine] = []
    for invoice in sorted(invoices, key=lambda inv: inv.due_date):
        if remaining_payment <= ZERO:
            break
        remaining_balance = _remaining(invoice)
        if remaining_balance <= ZERO:
            continue
        applied = min(remaining_payment, remaining_balance)
        allocations.append(AllocationLine(invoice_id=invoice.invoice_id, amount=applied))
        remaining_payment -= applied

    return AllocationPlan(allocations=allocations, unapplied_amount=remaining_payment)


def aging_bucket_for(*, due_date: date, as_of: date) -> tuple[int, str]:
    """Pure function. Returns (days_overdue, bucket) — days_overdue is
    clamped at 0 for an invoice not yet due (it still lands in "0-30").
    """
    days_overdue = max(0, (as_of - due_date).days)
    if days_overdue <= 30:
        return days_overdue, "0-30"
    if days_overdue <= 60:
        return days_overdue, "31-60"
    if days_overdue <= 90:
        return days_overdue, "61-90"
    return days_overdue, "90+"


def summarize_aging(lines: list[AgingLine], *, as_of: date) -> AgingSummary:
    """Pure function — aggregates aging_report() output into bucket totals."""
    bucket_totals: dict[str, Decimal] = dict.fromkeys(AGING_BUCKETS, ZERO)
    total = ZERO
    for line in lines:
        bucket_totals[line.bucket] += line.outstanding_amount
        total += line.outstanding_amount
    return AgingSummary(as_of=as_of, total_outstanding=total, bucket_totals=bucket_totals)


def _validate_party_type(party_type: str) -> None:
    if party_type not in (PARTY_CUSTOMER, PARTY_SUPPLIER):
        raise ValidationError(f"party_type must be 'customer' or 'supplier', got {party_type!r}")


async def _load_open_invoices(
    session: AsyncSession, *, org_id: uuid.UUID, party_type: str, party_id: uuid.UUID
) -> list[OpenInvoiceInput]:
    _validate_party_type(party_type)
    if party_type == PARTY_CUSTOMER:
        customer_rows = (
            (
                await session.execute(
                    select(CustomerInvoice).where(
                        CustomerInvoice.org_id == org_id,
                        CustomerInvoice.customer_id == party_id,
                        CustomerInvoice.status.in_([s.value for s in _CUSTOMER_OPEN_STATUSES]),
                    )
                )
            )
            .scalars()
            .all()
        )
        return [
            OpenInvoiceInput(
                invoice_id=row.id,
                total=Decimal(row.total),
                amount_paid=Decimal(row.amount_paid),
                due_date=row.due_date,
            )
            for row in customer_rows
        ]

    supplier_rows = (
        (
            await session.execute(
                select(SupplierInvoice).where(
                    SupplierInvoice.org_id == org_id,
                    SupplierInvoice.supplier_id == party_id,
                    SupplierInvoice.status.in_([s.value for s in _SUPPLIER_OPEN_STATUSES]),
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        OpenInvoiceInput(
            invoice_id=row.id,
            total=Decimal(row.total),
            amount_paid=Decimal(row.amount_paid),
            due_date=row.due_date,
        )
        for row in supplier_rows
    ]


async def record_payment(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    party_type: str,
    party_id: uuid.UUID,
    amount: Decimal,
    payment_date: date,
    method: str | None = None,
    reference: str | None = None,
    razorpay_payment_id: str | None = None,
    razorpay_payment_link_id: str | None = None,
    allocations: list[AllocationLine] | None = None,
) -> RecordPaymentResult:
    _validate_party_type(party_type)
    if amount <= ZERO:
        raise ValidationError("payment amount must be positive")

    party: Customer | Supplier | None
    if party_type == PARTY_CUSTOMER:
        party = await session.get(Customer, party_id)
    else:
        party = await session.get(Supplier, party_id)
    if party is None or party.org_id != org_id:
        raise NotFoundError(f"{party_type} {party_id} not found")

    open_invoices = await _load_open_invoices(
        session, org_id=org_id, party_type=party_type, party_id=party_id
    )

    try:
        plan = allocate_payment(
            payment_amount=amount, invoices=open_invoices, requested=allocations
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    payment = Payment(
        org_id=org_id,
        direction="inbound" if party_type == PARTY_CUSTOMER else "outbound",
        customer_id=party_id if party_type == PARTY_CUSTOMER else None,
        supplier_id=party_id if party_type == PARTY_SUPPLIER else None,
        amount=amount,
        payment_date=payment_date,
        method=method,
        razorpay_payment_id=razorpay_payment_id,
        razorpay_payment_link_id=razorpay_payment_link_id,
        reference=reference,
    )
    session.add(payment)
    await session.flush()

    invoice_results: list[InvoiceAllocationResult] = []
    for line in plan.allocations:
        session.add(
            PaymentAllocation(
                payment_id=payment.id,
                customer_invoice_id=line.invoice_id if party_type == PARTY_CUSTOMER else None,
                supplier_invoice_id=line.invoice_id if party_type == PARTY_SUPPLIER else None,
                amount=line.amount,
            )
        )
        if party_type == PARTY_CUSTOMER:
            customer_invoice = await session.get(CustomerInvoice, line.invoice_id)
            assert customer_invoice is not None  # just validated via open_invoices
            new_amount_paid = Decimal(customer_invoice.amount_paid) + line.amount
            customer_invoice.amount_paid = new_amount_paid  # type: ignore[assignment]  # Mapped[float] hint is cosmetic, see module docstring
            customer_invoice.status = (
                InvoiceStatus.PAID
                if new_amount_paid >= Decimal(customer_invoice.total)
                else InvoiceStatus.PARTIALLY_PAID
            )
            invoice_results.append(
                InvoiceAllocationResult(
                    invoice_id=line.invoice_id,
                    amount_applied=line.amount,
                    new_amount_paid=new_amount_paid,
                    new_status=customer_invoice.status,
                )
            )
        else:
            supplier_invoice = await session.get(SupplierInvoice, line.invoice_id)
            assert supplier_invoice is not None  # just validated via open_invoices
            new_amount_paid = Decimal(supplier_invoice.amount_paid) + line.amount
            supplier_invoice.amount_paid = new_amount_paid  # type: ignore[assignment]  # Mapped[float] hint is cosmetic, see module docstring
            if new_amount_paid >= Decimal(supplier_invoice.total):
                supplier_invoice.status = SupplierInvoiceStatus.PAID
            invoice_results.append(
                InvoiceAllocationResult(
                    invoice_id=line.invoice_id,
                    amount_applied=line.amount,
                    new_amount_paid=new_amount_paid,
                    new_status=supplier_invoice.status,
                )
            )

    await session.flush()

    return RecordPaymentResult(
        payment_id=payment.id,
        party_type=party_type,
        party_id=party_id,
        amount=amount,
        invoices=invoice_results,
        unapplied_amount=plan.unapplied_amount,
    )


async def outstanding_balance(
    session: AsyncSession, *, org_id: uuid.UUID, party_type: str, party_id: uuid.UUID
) -> Decimal:
    invoices = await _load_open_invoices(
        session, org_id=org_id, party_type=party_type, party_id=party_id
    )
    return sum((_remaining(invoice) for invoice in invoices), ZERO)


async def aging_report(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    party_type: str,
    party_id: uuid.UUID | None = None,
    as_of: date | None = None,
) -> list[AgingLine]:
    """Every open invoice with an outstanding balance, org-scoped and
    optionally narrowed to one party — `party_id=None` is the org-wide AR
    (or AP) aging report; a specific `party_id` is one customer's/
    supplier's statement.
    """
    _validate_party_type(party_type)
    as_of = as_of or date.today()

    source: list[tuple[uuid.UUID, uuid.UUID, Decimal, Decimal, date]]
    if party_type == PARTY_CUSTOMER:
        customer_stmt = select(CustomerInvoice).where(
            CustomerInvoice.org_id == org_id,
            CustomerInvoice.status.in_([s.value for s in _CUSTOMER_OPEN_STATUSES]),
        )
        if party_id is not None:
            customer_stmt = customer_stmt.where(CustomerInvoice.customer_id == party_id)
        customer_rows = (await session.execute(customer_stmt)).scalars().all()
        source = [
            (row.id, row.customer_id, Decimal(row.total), Decimal(row.amount_paid), row.due_date)
            for row in customer_rows
        ]
    else:
        supplier_stmt = select(SupplierInvoice).where(
            SupplierInvoice.org_id == org_id,
            SupplierInvoice.status.in_([s.value for s in _SUPPLIER_OPEN_STATUSES]),
        )
        if party_id is not None:
            supplier_stmt = supplier_stmt.where(SupplierInvoice.supplier_id == party_id)
        supplier_rows = (await session.execute(supplier_stmt)).scalars().all()
        source = [
            (row.id, row.supplier_id, Decimal(row.total), Decimal(row.amount_paid), row.due_date)
            for row in supplier_rows
        ]

    lines: list[AgingLine] = []
    for invoice_id, invoice_party_id, total, amount_paid, due_date in source:
        outstanding = Decimal(total) - Decimal(amount_paid)
        if outstanding <= ZERO:
            continue
        days_overdue, bucket = aging_bucket_for(due_date=due_date, as_of=as_of)
        lines.append(
            AgingLine(
                invoice_id=invoice_id,
                party_id=invoice_party_id,
                due_date=due_date,
                outstanding_amount=outstanding,
                days_overdue=days_overdue,
                bucket=bucket,
            )
        )
    return lines
