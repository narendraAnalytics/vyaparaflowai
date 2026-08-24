import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete

from app.core.exceptions import NotFoundError, ValidationError
from app.db.models.finance import Payment, PaymentAllocation
from app.db.models.org import Organization
from app.db.models.partners import Customer, Supplier
from app.db.models.purchase import SupplierInvoice
from app.db.models.sales import CustomerInvoice
from app.db.session import AsyncSessionLocal
from app.services.payments import (
    PARTY_CUSTOMER,
    PARTY_SUPPLIER,
    AllocationLine,
    OpenInvoiceInput,
    aging_bucket_for,
    aging_report,
    allocate_payment,
    outstanding_balance,
    record_payment,
    summarize_aging,
)

TELANGANA = "36"
TODAY = date(2026, 8, 24)


def _invoice(**overrides: object) -> OpenInvoiceInput:
    defaults: dict[str, object] = dict(
        invoice_id=uuid.uuid4(),
        total=Decimal("1000"),
        amount_paid=Decimal("0"),
        due_date=TODAY,
    )
    defaults.update(overrides)
    return OpenInvoiceInput.model_validate(defaults)


# ---------------------------------------------------------------------------
# Pure allocate_payment() table-driven tests
# ---------------------------------------------------------------------------


def test_full_payment_settles_single_invoice_exactly():
    invoice = _invoice(total=Decimal("1000"))
    plan = allocate_payment(payment_amount=Decimal("1000"), invoices=[invoice])
    expected = AllocationLine(invoice_id=invoice.invoice_id, amount=Decimal("1000"))
    assert plan.allocations == [expected]
    assert plan.unapplied_amount == Decimal("0")


def test_partial_payment_applies_less_than_invoice_total():
    invoice = _invoice(total=Decimal("1000"))
    plan = allocate_payment(payment_amount=Decimal("400"), invoices=[invoice])
    [line] = plan.allocations
    assert line.amount == Decimal("400")
    assert plan.unapplied_amount == Decimal("0")


def test_overpayment_leaves_unapplied_amount():
    invoice = _invoice(total=Decimal("1000"))
    plan = allocate_payment(payment_amount=Decimal("1500"), invoices=[invoice])
    [line] = plan.allocations
    assert line.amount == Decimal("1000")
    assert plan.unapplied_amount == Decimal("500")


def test_no_open_invoices_leaves_entire_payment_unapplied():
    plan = allocate_payment(payment_amount=Decimal("500"), invoices=[])
    assert plan.allocations == []
    assert plan.unapplied_amount == Decimal("500")


def test_auto_allocation_prefers_oldest_due_date_first():
    older = _invoice(due_date=TODAY - timedelta(days=10), total=Decimal("300"))
    newer = _invoice(due_date=TODAY, total=Decimal("300"))
    plan = allocate_payment(payment_amount=Decimal("300"), invoices=[newer, older])
    [line] = plan.allocations
    assert line.invoice_id == older.invoice_id


def test_auto_allocation_spreads_across_multiple_invoices():
    first = _invoice(due_date=TODAY - timedelta(days=5), total=Decimal("300"))
    second = _invoice(due_date=TODAY, total=Decimal("300"))
    plan = allocate_payment(payment_amount=Decimal("500"), invoices=[first, second])
    amounts = {line.invoice_id: line.amount for line in plan.allocations}
    assert amounts[first.invoice_id] == Decimal("300")
    assert amounts[second.invoice_id] == Decimal("200")
    assert plan.unapplied_amount == Decimal("0")


def test_auto_allocation_skips_already_fully_paid_invoice():
    settled = _invoice(
        due_date=TODAY - timedelta(days=5), total=Decimal("300"), amount_paid=Decimal("300")
    )
    open_one = _invoice(due_date=TODAY, total=Decimal("300"))
    plan = allocate_payment(payment_amount=Decimal("300"), invoices=[settled, open_one])
    [line] = plan.allocations
    assert line.invoice_id == open_one.invoice_id


def test_auto_allocation_partially_settles_existing_partial_invoice():
    invoice = _invoice(total=Decimal("1000"), amount_paid=Decimal("600"))
    plan = allocate_payment(payment_amount=Decimal("300"), invoices=[invoice])
    [line] = plan.allocations
    assert line.amount == Decimal("300")


def test_manual_allocation_applies_exact_requested_amounts():
    invoice_a = _invoice(total=Decimal("500"))
    invoice_b = _invoice(total=Decimal("500"))
    plan = allocate_payment(
        payment_amount=Decimal("500"),
        invoices=[invoice_a, invoice_b],
        requested=[AllocationLine(invoice_id=invoice_a.invoice_id, amount=Decimal("500"))],
    )
    expected = AllocationLine(invoice_id=invoice_a.invoice_id, amount=Decimal("500"))
    assert plan.allocations == [expected]
    assert plan.unapplied_amount == Decimal("0")


def test_manual_allocation_exceeding_invoice_balance_rejected():
    invoice = _invoice(total=Decimal("500"), amount_paid=Decimal("400"))
    with pytest.raises(ValueError, match="exceeds its outstanding balance"):
        allocate_payment(
            payment_amount=Decimal("200"),
            invoices=[invoice],
            requested=[AllocationLine(invoice_id=invoice.invoice_id, amount=Decimal("200"))],
        )


def test_manual_allocation_total_exceeding_payment_amount_rejected():
    invoice_a = _invoice(total=Decimal("500"))
    invoice_b = _invoice(total=Decimal("500"))
    with pytest.raises(ValueError, match="exceed payment amount"):
        allocate_payment(
            payment_amount=Decimal("100"),
            invoices=[invoice_a, invoice_b],
            requested=[
                AllocationLine(invoice_id=invoice_a.invoice_id, amount=Decimal("60")),
                AllocationLine(invoice_id=invoice_b.invoice_id, amount=Decimal("60")),
            ],
        )


def test_manual_allocation_duplicate_invoice_rejected():
    invoice = _invoice(total=Decimal("500"))
    with pytest.raises(ValueError, match="duplicate allocation"):
        allocate_payment(
            payment_amount=Decimal("500"),
            invoices=[invoice],
            requested=[
                AllocationLine(invoice_id=invoice.invoice_id, amount=Decimal("200")),
                AllocationLine(invoice_id=invoice.invoice_id, amount=Decimal("200")),
            ],
        )


def test_manual_allocation_unknown_invoice_rejected():
    invoice = _invoice(total=Decimal("500"))
    with pytest.raises(ValueError, match="not an open invoice"):
        allocate_payment(
            payment_amount=Decimal("100"),
            invoices=[invoice],
            requested=[AllocationLine(invoice_id=uuid.uuid4(), amount=Decimal("100"))],
        )


def test_manual_allocation_empty_list_rejected():
    with pytest.raises(ValueError, match="must not be empty"):
        allocate_payment(payment_amount=Decimal("100"), invoices=[], requested=[])


def test_non_positive_payment_amount_rejected():
    with pytest.raises(ValueError, match="must be positive"):
        allocate_payment(payment_amount=Decimal("0"), invoices=[])


# ---------------------------------------------------------------------------
# Pure aging_bucket_for() table-driven tests
# ---------------------------------------------------------------------------

AGING_TABLE = [
    ("not yet due (negative days) clamps into 0-30", TODAY + timedelta(days=10), 0, "0-30"),
    ("due today is 0 days overdue, bucket 0-30", TODAY, 0, "0-30"),
    ("exactly 30 days overdue stays in 0-30", TODAY - timedelta(days=30), 30, "0-30"),
    ("31 days overdue rolls into 31-60", TODAY - timedelta(days=31), 31, "31-60"),
    ("exactly 60 days overdue stays in 31-60", TODAY - timedelta(days=60), 60, "31-60"),
    ("61 days overdue rolls into 61-90", TODAY - timedelta(days=61), 61, "61-90"),
    ("exactly 90 days overdue stays in 61-90", TODAY - timedelta(days=90), 90, "61-90"),
    ("91 days overdue rolls into 90+", TODAY - timedelta(days=91), 91, "90+"),
    ("far overdue stays in 90+", TODAY - timedelta(days=400), 400, "90+"),
]


@pytest.mark.parametrize(
    "label,due_date,expect_days,expect_bucket", AGING_TABLE, ids=[c[0] for c in AGING_TABLE]
)
def test_aging_bucket_table(label, due_date, expect_days, expect_bucket):
    days_overdue, bucket = aging_bucket_for(due_date=due_date, as_of=TODAY)
    assert days_overdue == expect_days, label
    assert bucket == expect_bucket, label


def test_summarize_aging_aggregates_by_bucket():
    from app.services.payments import AgingLine

    lines = [
        AgingLine(
            invoice_id=uuid.uuid4(),
            party_id=uuid.uuid4(),
            due_date=TODAY,
            outstanding_amount=Decimal("100"),
            days_overdue=0,
            bucket="0-30",
        ),
        AgingLine(
            invoice_id=uuid.uuid4(),
            party_id=uuid.uuid4(),
            due_date=TODAY - timedelta(days=40),
            outstanding_amount=Decimal("250"),
            days_overdue=40,
            bucket="31-60",
        ),
    ]
    summary = summarize_aging(lines, as_of=TODAY)
    assert summary.total_outstanding == Decimal("350")
    assert summary.bucket_totals["0-30"] == Decimal("100")
    assert summary.bucket_totals["31-60"] == Decimal("250")
    assert summary.bucket_totals["61-90"] == Decimal("0")
    assert summary.bucket_totals["90+"] == Decimal("0")


# ---------------------------------------------------------------------------
# DB-backed integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def rig():
    async with AsyncSessionLocal() as session:
        org = Organization(name=f"test-payments-{uuid.uuid4()}", state_code=TELANGANA)
        session.add(org)
        await session.flush()

        customer = Customer(org_id=org.id, name="Test Customer", state_code=TELANGANA)
        supplier = Supplier(org_id=org.id, name="Test Supplier", state_code=TELANGANA)
        session.add_all([customer, supplier])
        await session.commit()

        ids = {"org_id": org.id, "customer_id": customer.id, "supplier_id": supplier.id}

    yield ids

    async with AsyncSessionLocal() as session:
        await session.execute(delete(PaymentAllocation))
        await session.execute(delete(Payment).where(Payment.org_id == ids["org_id"]))
        await session.execute(
            delete(CustomerInvoice).where(CustomerInvoice.org_id == ids["org_id"])
        )
        await session.execute(
            delete(SupplierInvoice).where(SupplierInvoice.org_id == ids["org_id"])
        )
        await session.execute(delete(Customer).where(Customer.id == ids["customer_id"]))
        await session.execute(delete(Supplier).where(Supplier.id == ids["supplier_id"]))
        await session.execute(delete(Organization).where(Organization.id == ids["org_id"]))
        await session.commit()


async def _make_customer_invoice(session, *, org_id, customer_id, total, due_date, status="issued"):
    invoice = CustomerInvoice(
        org_id=org_id,
        customer_id=customer_id,
        invoice_number=f"INV-TEST-{uuid.uuid4().hex[:8]}",
        invoice_date=due_date,
        due_date=due_date,
        status=status,
        subtotal=total,
        tax_total=Decimal("0"),
        total=total,
        amount_paid=Decimal("0"),
    )
    session.add(invoice)
    await session.flush()
    return invoice


async def _make_supplier_invoice(
    session, *, org_id, supplier_id, total, due_date, status="approved"
):
    invoice = SupplierInvoice(
        org_id=org_id,
        supplier_id=supplier_id,
        invoice_number=f"SINV-TEST-{uuid.uuid4().hex[:8]}",
        invoice_date=due_date,
        due_date=due_date,
        status=status,
        subtotal=total,
        tax_total=Decimal("0"),
        total=total,
        amount_paid=Decimal("0"),
    )
    session.add(invoice)
    await session.flush()
    return invoice


@pytest.mark.asyncio
async def test_record_payment_full_customer_payment_marks_paid(rig):
    async with AsyncSessionLocal() as session:
        invoice = await _make_customer_invoice(
            session,
            org_id=rig["org_id"],
            customer_id=rig["customer_id"],
            total=Decimal("1000"),
            due_date=TODAY,
        )
        await session.commit()
        invoice_id = invoice.id

    async with AsyncSessionLocal() as session:
        result = await record_payment(
            session,
            org_id=rig["org_id"],
            party_type=PARTY_CUSTOMER,
            party_id=rig["customer_id"],
            amount=Decimal("1000"),
            payment_date=TODAY,
        )
        await session.commit()

    assert result.unapplied_amount == Decimal("0")
    [line] = result.invoices
    assert line.invoice_id == invoice_id
    assert line.new_status == "paid"

    async with AsyncSessionLocal() as session:
        row = await session.get(CustomerInvoice, invoice_id)
        assert row.status == "paid"
        assert row.amount_paid == Decimal("1000")


@pytest.mark.asyncio
async def test_record_payment_partial_customer_payment_marks_partially_paid(rig):
    async with AsyncSessionLocal() as session:
        invoice = await _make_customer_invoice(
            session,
            org_id=rig["org_id"],
            customer_id=rig["customer_id"],
            total=Decimal("1000"),
            due_date=TODAY,
        )
        await session.commit()
        invoice_id = invoice.id

    async with AsyncSessionLocal() as session:
        result = await record_payment(
            session,
            org_id=rig["org_id"],
            party_type=PARTY_CUSTOMER,
            party_id=rig["customer_id"],
            amount=Decimal("400"),
            payment_date=TODAY,
        )
        await session.commit()

    [line] = result.invoices
    assert line.new_status == "partially_paid"

    async with AsyncSessionLocal() as session:
        row = await session.get(CustomerInvoice, invoice_id)
        assert row.status == "partially_paid"
        assert row.amount_paid == Decimal("400")


@pytest.mark.asyncio
async def test_record_payment_auto_allocates_oldest_due_first_across_invoices(rig):
    async with AsyncSessionLocal() as session:
        older = await _make_customer_invoice(
            session,
            org_id=rig["org_id"],
            customer_id=rig["customer_id"],
            total=Decimal("300"),
            due_date=TODAY - timedelta(days=10),
        )
        newer = await _make_customer_invoice(
            session,
            org_id=rig["org_id"],
            customer_id=rig["customer_id"],
            total=Decimal("300"),
            due_date=TODAY,
        )
        await session.commit()
        older_id, newer_id = older.id, newer.id

    async with AsyncSessionLocal() as session:
        result = await record_payment(
            session,
            org_id=rig["org_id"],
            party_type=PARTY_CUSTOMER,
            party_id=rig["customer_id"],
            amount=Decimal("400"),
            payment_date=TODAY,
        )
        await session.commit()

    amounts = {line.invoice_id: line.amount_applied for line in result.invoices}
    assert amounts[older_id] == Decimal("300")
    assert amounts[newer_id] == Decimal("100")


@pytest.mark.asyncio
async def test_record_payment_over_payment_leaves_unapplied_amount(rig):
    async with AsyncSessionLocal() as session:
        await _make_customer_invoice(
            session,
            org_id=rig["org_id"],
            customer_id=rig["customer_id"],
            total=Decimal("500"),
            due_date=TODAY,
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        result = await record_payment(
            session,
            org_id=rig["org_id"],
            party_type=PARTY_CUSTOMER,
            party_id=rig["customer_id"],
            amount=Decimal("800"),
            payment_date=TODAY,
        )
        await session.commit()

    assert result.unapplied_amount == Decimal("300")


@pytest.mark.asyncio
async def test_record_payment_manual_allocation_unknown_invoice_rejected(rig):
    async with AsyncSessionLocal() as session:
        await _make_customer_invoice(
            session,
            org_id=rig["org_id"],
            customer_id=rig["customer_id"],
            total=Decimal("500"),
            due_date=TODAY,
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        with pytest.raises(ValidationError):
            await record_payment(
                session,
                org_id=rig["org_id"],
                party_type=PARTY_CUSTOMER,
                party_id=rig["customer_id"],
                amount=Decimal("100"),
                payment_date=TODAY,
                allocations=[AllocationLine(invoice_id=uuid.uuid4(), amount=Decimal("100"))],
            )


@pytest.mark.asyncio
async def test_record_payment_unknown_customer_rejected(rig):
    async with AsyncSessionLocal() as session:
        with pytest.raises(NotFoundError):
            await record_payment(
                session,
                org_id=rig["org_id"],
                party_type=PARTY_CUSTOMER,
                party_id=uuid.uuid4(),
                amount=Decimal("100"),
                payment_date=TODAY,
            )


@pytest.mark.asyncio
async def test_record_payment_non_positive_amount_rejected(rig):
    async with AsyncSessionLocal() as session:
        with pytest.raises(ValidationError):
            await record_payment(
                session,
                org_id=rig["org_id"],
                party_type=PARTY_CUSTOMER,
                party_id=rig["customer_id"],
                amount=Decimal("0"),
                payment_date=TODAY,
            )


@pytest.mark.asyncio
async def test_record_payment_supplier_side_marks_paid_without_partial_status(rig):
    async with AsyncSessionLocal() as session:
        invoice = await _make_supplier_invoice(
            session,
            org_id=rig["org_id"],
            supplier_id=rig["supplier_id"],
            total=Decimal("1000"),
            due_date=TODAY,
        )
        await session.commit()
        invoice_id = invoice.id

    async with AsyncSessionLocal() as session:
        result = await record_payment(
            session,
            org_id=rig["org_id"],
            party_type=PARTY_SUPPLIER,
            party_id=rig["supplier_id"],
            amount=Decimal("400"),
            payment_date=TODAY,
        )
        await session.commit()

    [line] = result.invoices
    # SupplierInvoiceStatus has no PARTIALLY_PAID value — status is left
    # untouched (still "approved") until the invoice is fully paid.
    assert line.new_status == "approved"

    async with AsyncSessionLocal() as session:
        row = await session.get(SupplierInvoice, invoice_id)
        assert row.status == "approved"
        assert row.amount_paid == Decimal("400")

    async with AsyncSessionLocal() as session:
        await record_payment(
            session,
            org_id=rig["org_id"],
            party_type=PARTY_SUPPLIER,
            party_id=rig["supplier_id"],
            amount=Decimal("600"),
            payment_date=TODAY,
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        row = await session.get(SupplierInvoice, invoice_id)
        assert row.status == "paid"
        assert row.amount_paid == Decimal("1000")


@pytest.mark.asyncio
async def test_outstanding_balance_sums_open_invoices(rig):
    async with AsyncSessionLocal() as session:
        await _make_customer_invoice(
            session,
            org_id=rig["org_id"],
            customer_id=rig["customer_id"],
            total=Decimal("500"),
            due_date=TODAY,
        )
        await _make_customer_invoice(
            session,
            org_id=rig["org_id"],
            customer_id=rig["customer_id"],
            total=Decimal("300"),
            due_date=TODAY,
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        balance = await outstanding_balance(
            session, org_id=rig["org_id"], party_type=PARTY_CUSTOMER, party_id=rig["customer_id"]
        )
    assert balance == Decimal("800")


@pytest.mark.asyncio
async def test_aging_report_buckets_invoices_correctly(rig):
    async with AsyncSessionLocal() as session:
        current = await _make_customer_invoice(
            session,
            org_id=rig["org_id"],
            customer_id=rig["customer_id"],
            total=Decimal("100"),
            due_date=TODAY,
        )
        overdue_45 = await _make_customer_invoice(
            session,
            org_id=rig["org_id"],
            customer_id=rig["customer_id"],
            total=Decimal("200"),
            due_date=TODAY - timedelta(days=45),
        )
        await session.commit()
        current_id, overdue_45_id = current.id, overdue_45.id

    async with AsyncSessionLocal() as session:
        lines = await aging_report(
            session,
            org_id=rig["org_id"],
            party_type=PARTY_CUSTOMER,
            party_id=rig["customer_id"],
            as_of=TODAY,
        )

    by_id = {line.invoice_id: line for line in lines}
    assert by_id[current_id].bucket == "0-30"
    assert by_id[overdue_45_id].bucket == "31-60"
    assert by_id[overdue_45_id].outstanding_amount == Decimal("200")
