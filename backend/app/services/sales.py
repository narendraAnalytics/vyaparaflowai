"""Order-to-cash entry points.

Three flows live here, all one caller-owned transaction each (this module
never commits, same convention as numbering.py/pricing.py/inventory.py):

- `create_sales_order()` — a binding order. Validates, prices (services/
  pricing.py), checks credit, and best-effort reserves stock (services/
  inventory.py) per line.
- `create_sales_order(..., is_quote=True)` / `confirm_sales_order()` — a
  non-binding quotation, confirmed later. Reuses `sales_orders.status =
  DRAFT` for "this is a quote" rather than a new table or enum value —
  Odoo models a quotation the same way (a Sales Order in draft state, not
  a separate object), and ERPNext doesn't even require a quotation stage
  before a Sales Order. A quote is priced and persisted like a normal
  order but skips inventory entirely (reserved_qty stays 0 on every line);
  `confirm_sales_order()` re-checks the customer's credit (time may have
  passed) and then runs the exact same best-effort reservation pass a
  normal order gets at creation, using the prices locked in at quote time
  (a quote's whole point is a locked price, not a live re-price).
- `create_counter_sale()` — a walk-in/till sale: skips Sales Order and
  Delivery entirely and creates a `customer_invoice` directly
  (`sales_order_id` is nullable for exactly this — Phase 1 schema already
  supports it), reducing stock immediately. Unlike an order's best-effort
  per-line reservation, a counter sale is all-or-nothing: you can't hand a
  customer partial goods at a physical counter, so this reuses
  `inventory.issue(lines=[...])`'s batch all-or-nothing behavior directly
  rather than the best-effort fallback loop below.

Validation order is deliberate everywhere in this module: customer/
warehouse/product existence, pricing, and the credit check all happen
BEFORE a document number is allocated or any row is persisted — not
because a rolled-back transaction would leave a gap (it wouldn't; the
number allocation is inside the same uncommitted transaction and rolls
back with everything else), but to avoid a database round-trip for input
that was already known to be invalid.

Stock reservation for a normal order is deliberately best-effort per line,
not all-or-nothing for the whole order: `sales_order_items.reserved_qty`
has a CHECK constraint of `0 <= reserved_qty <= quantity` (Phase 1 schema)
precisely to allow a single line to be partially reserved. For each line
this tries the full ordered quantity first (one atomic call into
services/inventory.py); if that's rejected for insufficient stock, it
re-reads current availability and reserves whatever's actually left,
falling back to zero. Lines are processed in `product_id` order — the same
deadlock-avoidance convention documented in services/inventory.py, applied
here across the separate per-line calls this function makes (rather than
one inventory.reserve(lines=[...]) batch call, which is all-or-nothing and
can't express "reserve what's available" for an individual line).

The resulting sales_orders.status reflects the reservation outcome:
RESERVED (every line fully reserved), PARTIALLY_RESERVED (some stock
reserved but a shortage remains somewhere), or CONFIRMED (the order is
valid and priced but nothing could be reserved at all) — DRAFT means "this
is an unconfirmed quote," not "nothing reserved yet," so a caller must
check status, not just `has_shortage`, before reacting to a shortage: a
DRAFT order's `has_shortage` is always True but must NOT trigger
procurement (Phase 2.8) — nothing has been committed to yet. Turning a
real (non-quote) shortage into a purchase requisition is procurement.py's
job, not this module's — this module only reports `shortage_qty` per line.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models.catalog import Product, Warehouse
from app.db.models.enums import InvoiceStatus, SalesOrderStatus
from app.db.models.org import Organization
from app.db.models.partners import Customer
from app.db.models.sales import (
    CustomerInvoice,
    CustomerInvoiceItem,
    SalesOrder,
    SalesOrderItem,
)
from app.services import inventory
from app.services.numbering import next_document_number
from app.services.pricing import PricingLineInput, PricingLineResult, PricingResult, price_order

ZERO = Decimal("0")


class SalesOrderLineInput(BaseModel):
    product_id: uuid.UUID
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    discount_percent: Decimal = Field(default=ZERO, ge=0, le=100)
    discount_amount: Decimal = Field(default=ZERO, ge=0)


class CreateSalesOrderRequest(BaseModel):
    customer_id: uuid.UUID
    warehouse_id: uuid.UUID
    lines: list[SalesOrderLineInput]
    order_date: date | None = None
    header_discount_percent: Decimal = Field(default=ZERO, ge=0, le=100)
    header_discount_amount: Decimal = Field(default=ZERO, ge=0)
    notes: str | None = None
    is_quote: bool = False


class SalesOrderLineResult(BaseModel):
    product_id: uuid.UUID
    quantity: Decimal
    reserved_qty: Decimal
    shortage_qty: Decimal
    unit_price: Decimal
    gst_rate: Decimal
    line_subtotal: Decimal
    line_tax: Decimal
    line_total: Decimal


class CreateSalesOrderResult(BaseModel):
    sales_order_id: uuid.UUID
    order_number: str
    status: str
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    lines: list[SalesOrderLineResult]
    has_shortage: bool


class CounterSaleLineResult(BaseModel):
    product_id: uuid.UUID
    quantity: Decimal
    unit_price: Decimal
    gst_rate: Decimal
    line_subtotal: Decimal
    line_tax: Decimal
    line_total: Decimal


class CreateCounterSaleRequest(BaseModel):
    customer_id: uuid.UUID
    warehouse_id: uuid.UUID
    lines: list[SalesOrderLineInput]
    invoice_date: date | None = None
    header_discount_percent: Decimal = Field(default=ZERO, ge=0, le=100)
    header_discount_amount: Decimal = Field(default=ZERO, ge=0)


class CreateCounterSaleResult(BaseModel):
    customer_invoice_id: uuid.UUID
    invoice_number: str
    status: str
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    lines: list[CounterSaleLineResult]


async def _validate_customer(
    session: AsyncSession, *, org_id: uuid.UUID, customer_id: uuid.UUID
) -> Customer:
    customer = await session.get(Customer, customer_id)
    if customer is None or customer.org_id != org_id:
        raise NotFoundError(f"customer {customer_id} not found")
    if not customer.is_active:
        raise ConflictError(f"customer {customer_id} is inactive")
    return customer


async def _validate_warehouse(
    session: AsyncSession, *, org_id: uuid.UUID, warehouse_id: uuid.UUID
) -> Warehouse:
    warehouse = await session.get(Warehouse, warehouse_id)
    if warehouse is None or warehouse.org_id != org_id:
        raise NotFoundError(f"warehouse {warehouse_id} not found")
    return warehouse


async def _validate_organization(session: AsyncSession, *, org_id: uuid.UUID) -> Organization:
    # customer.org_id has an FK to organizations.id (ON DELETE RESTRICT), so
    # if a customer validated above, this row is guaranteed to exist —
    # defensive only, not reachable under the current schema.
    organization = await session.get(Organization, org_id)
    if organization is None:
        raise NotFoundError(f"organization {org_id} not found")
    return organization


async def _validate_products(
    session: AsyncSession, *, org_id: uuid.UUID, product_ids: set[uuid.UUID]
) -> dict[uuid.UUID, Product]:
    products = (
        (
            await session.execute(
                select(Product).where(Product.id.in_(product_ids), Product.org_id == org_id)
            )
        )
        .scalars()
        .all()
    )
    products_by_id = {product.id: product for product in products}
    missing = product_ids - products_by_id.keys()
    if missing:
        raise NotFoundError(f"product(s) not found: {', '.join(str(m) for m in missing)}")
    inactive_skus = [p.sku for p in products if not p.is_active]
    if inactive_skus:
        raise ConflictError(f"product(s) inactive: {', '.join(inactive_skus)}")
    return products_by_id


def _price_lines(
    *,
    organization: Organization,
    customer: Customer,
    lines: list[SalesOrderLineInput],
    products_by_id: dict[uuid.UUID, Product],
    header_discount_percent: Decimal,
    header_discount_amount: Decimal,
) -> PricingResult:
    pricing_lines = [
        PricingLineInput(
            product_id=line.product_id,
            hsn_code=products_by_id[line.product_id].hsn_code,
            uom=products_by_id[line.product_id].uom,
            quantity=line.quantity,
            unit_price=line.unit_price,
            gst_rate=products_by_id[line.product_id].gst_rate,
            discount_percent=line.discount_percent,
            discount_amount=line.discount_amount,
        )
        for line in lines
    ]
    # place of supply: the customer's own state if known, else assume it
    # matches the seller's (most walk-in/local customers never get a
    # state_code recorded) rather than letting a blank string mismatch the
    # org's real state and silently misclassify a local sale as inter-state.
    place_of_supply = customer.state_code or organization.state_code or ""
    return price_order(
        origin_state_code=organization.state_code or "",
        place_of_supply_state_code=place_of_supply,
        lines=pricing_lines,
        header_discount_percent=header_discount_percent,
        header_discount_amount=header_discount_amount,
    )


async def _check_credit(session: AsyncSession, *, customer: Customer, order_total: Decimal) -> None:
    result = await session.execute(
        select(
            func.coalesce(func.sum(CustomerInvoice.total - CustomerInvoice.amount_paid), 0)
        ).where(
            CustomerInvoice.customer_id == customer.id,
            CustomerInvoice.status.notin_([InvoiceStatus.PAID, InvoiceStatus.VOID]),
        )
    )
    outstanding = Decimal(result.scalar_one())
    if outstanding + order_total > customer.credit_limit:
        raise ConflictError(
            f"order total {order_total} would push customer {customer.id}'s outstanding "
            f"balance to {outstanding + order_total}, over their credit limit of "
            f"{customer.credit_limit}"
        )


async def _best_effort_reserve(
    session: AsyncSession,
    *,
    product_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    quantity: Decimal,
    ref_id: uuid.UUID,
) -> Decimal:
    try:
        await inventory.reserve(
            session,
            lines=[
                inventory.InventoryLine(
                    product_id=product_id, warehouse_id=warehouse_id, quantity=quantity
                )
            ],
            ref_type="sales_order",
            ref_id=ref_id,
        )
        return quantity
    except ConflictError:
        pass

    available = await inventory.get_available(
        session, product_id=product_id, warehouse_id=warehouse_id
    )
    if available <= ZERO:
        return ZERO
    try:
        await inventory.reserve(
            session,
            lines=[
                inventory.InventoryLine(
                    product_id=product_id, warehouse_id=warehouse_id, quantity=available
                )
            ],
            ref_type="sales_order",
            ref_id=ref_id,
        )
        return available
    except ConflictError:
        # lost a race against another concurrent reservation between the
        # get_available read above and this attempt — not an oversell (the
        # inner reserve() call is still atomic), just a conservative miss.
        return ZERO


def _finalize_status(sales_order: SalesOrder, line_results: list[SalesOrderLineResult]) -> None:
    fully_reserved = all(line.shortage_qty == ZERO for line in line_results)
    any_reserved = any(line.reserved_qty > ZERO for line in line_results)
    if fully_reserved:
        sales_order.status = SalesOrderStatus.RESERVED
    elif any_reserved:
        sales_order.status = SalesOrderStatus.PARTIALLY_RESERVED
    else:
        sales_order.status = SalesOrderStatus.CONFIRMED


def _line_result(priced_line: PricingLineResult, reserved_qty: Decimal) -> SalesOrderLineResult:
    return SalesOrderLineResult(
        product_id=priced_line.product_id,
        quantity=priced_line.quantity,
        reserved_qty=reserved_qty,
        shortage_qty=priced_line.quantity - reserved_qty,
        unit_price=priced_line.unit_price,
        gst_rate=priced_line.gst_rate,
        line_subtotal=priced_line.taxable_value,
        line_tax=priced_line.tax_amount,
        line_total=priced_line.line_total,
    )


async def create_sales_order(
    session: AsyncSession, *, org_id: uuid.UUID, request: CreateSalesOrderRequest
) -> CreateSalesOrderResult:
    if not request.lines:
        raise ValidationError("sales order requires at least one line")

    customer = await _validate_customer(session, org_id=org_id, customer_id=request.customer_id)
    await _validate_warehouse(session, org_id=org_id, warehouse_id=request.warehouse_id)
    organization = await _validate_organization(session, org_id=org_id)
    products_by_id = await _validate_products(
        session, org_id=org_id, product_ids={line.product_id for line in request.lines}
    )

    priced = _price_lines(
        organization=organization,
        customer=customer,
        lines=request.lines,
        products_by_id=products_by_id,
        header_discount_percent=request.header_discount_percent,
        header_discount_amount=request.header_discount_amount,
    )
    await _check_credit(session, customer=customer, order_total=priced.grand_total)

    order_date = request.order_date or date.today()
    order_number = await next_document_number(
        session, org_id=org_id, doc_type="sales_order", on=order_date
    )

    sales_order = SalesOrder(
        org_id=org_id,
        customer_id=customer.id,
        order_number=order_number,
        order_date=order_date,
        status=SalesOrderStatus.DRAFT,
        subtotal=priced.subtotal,
        tax_total=priced.tax_total,
        total=priced.grand_total,
        notes=request.notes,
    )
    session.add(sales_order)
    await session.flush()

    line_results: list[SalesOrderLineResult] = []
    if request.is_quote:
        # non-binding: persist the priced lines but never touch inventory,
        # and leave status at DRAFT — see confirm_sales_order().
        for priced_line in priced.lines:
            session.add(
                SalesOrderItem(
                    sales_order_id=sales_order.id,
                    product_id=priced_line.product_id,
                    quantity=priced_line.quantity,
                    reserved_qty=ZERO,
                    unit_price=priced_line.unit_price,
                    gst_rate=priced_line.gst_rate,
                    line_subtotal=priced_line.taxable_value,
                    line_tax=priced_line.tax_amount,
                    line_total=priced_line.line_total,
                )
            )
            line_results.append(_line_result(priced_line, ZERO))
    else:
        for priced_line in sorted(priced.lines, key=lambda line: str(line.product_id)):
            reserved_qty = await _best_effort_reserve(
                session,
                product_id=priced_line.product_id,
                warehouse_id=request.warehouse_id,
                quantity=priced_line.quantity,
                ref_id=sales_order.id,
            )
            session.add(
                SalesOrderItem(
                    sales_order_id=sales_order.id,
                    product_id=priced_line.product_id,
                    quantity=priced_line.quantity,
                    reserved_qty=reserved_qty,
                    unit_price=priced_line.unit_price,
                    gst_rate=priced_line.gst_rate,
                    line_subtotal=priced_line.taxable_value,
                    line_tax=priced_line.tax_amount,
                    line_total=priced_line.line_total,
                )
            )
            line_results.append(_line_result(priced_line, reserved_qty))
        _finalize_status(sales_order, line_results)

    await session.flush()

    return CreateSalesOrderResult(
        sales_order_id=sales_order.id,
        order_number=order_number,
        status=sales_order.status,
        subtotal=priced.subtotal,
        tax_total=priced.tax_total,
        total=priced.grand_total,
        lines=line_results,
        has_shortage=any(line.shortage_qty > ZERO for line in line_results),
    )


async def confirm_sales_order(
    session: AsyncSession, *, org_id: uuid.UUID, sales_order_id: uuid.UUID, warehouse_id: uuid.UUID
) -> CreateSalesOrderResult:
    """Convert a quote (status=DRAFT) into a binding order: re-check credit,
    then best-effort reserve stock at the given warehouse — same reservation
    logic create_sales_order() runs at creation time for a non-quote order.
    Prices are NOT recomputed; a quote's price is locked at quote time.
    """
    sales_order = await session.get(SalesOrder, sales_order_id)
    if sales_order is None or sales_order.org_id != org_id:
        raise NotFoundError(f"sales order {sales_order_id} not found")
    if sales_order.status != SalesOrderStatus.DRAFT:
        raise ConflictError(
            f"sales order {sales_order_id} is not a draft/quote (status={sales_order.status})"
        )

    customer = await _validate_customer(session, org_id=org_id, customer_id=sales_order.customer_id)
    await _validate_warehouse(session, org_id=org_id, warehouse_id=warehouse_id)
    # sales_order.total is Mapped[float] in the ORM's type hint, but
    # SQLAlchemy's Numeric column actually round-trips as Decimal at
    # runtime (see backend/CLAUDE.md) — the cast below is for mypy, not a
    # real conversion.
    await _check_credit(session, customer=customer, order_total=Decimal(sales_order.total))

    items = (
        (
            await session.execute(
                select(SalesOrderItem).where(SalesOrderItem.sales_order_id == sales_order.id)
            )
        )
        .scalars()
        .all()
    )

    line_results: list[SalesOrderLineResult] = []
    for item in sorted(items, key=lambda i: str(i.product_id)):
        quantity = Decimal(item.quantity)
        reserved_qty = await _best_effort_reserve(
            session,
            product_id=item.product_id,
            warehouse_id=warehouse_id,
            quantity=quantity,
            ref_id=sales_order.id,
        )
        item.reserved_qty = reserved_qty  # type: ignore[assignment]  # Mapped[float] hint is cosmetic, see module docstring
        line_results.append(
            SalesOrderLineResult(
                product_id=item.product_id,
                quantity=quantity,
                reserved_qty=reserved_qty,
                shortage_qty=quantity - reserved_qty,
                unit_price=Decimal(item.unit_price),
                gst_rate=Decimal(item.gst_rate),
                line_subtotal=Decimal(item.line_subtotal),
                line_tax=Decimal(item.line_tax),
                line_total=Decimal(item.line_total),
            )
        )

    _finalize_status(sales_order, line_results)
    await session.flush()

    return CreateSalesOrderResult(
        sales_order_id=sales_order.id,
        order_number=sales_order.order_number,
        status=sales_order.status,
        subtotal=sales_order.subtotal,
        tax_total=sales_order.tax_total,
        total=sales_order.total,
        lines=line_results,
        has_shortage=any(line.shortage_qty > ZERO for line in line_results),
    )


async def create_counter_sale(
    session: AsyncSession, *, org_id: uuid.UUID, request: CreateCounterSaleRequest
) -> CreateCounterSaleResult:
    """Walk-in/till sale: skips Sales Order + Delivery, creates a
    customer_invoice directly (sales_order_id left null) and reduces stock
    immediately. All-or-nothing across every line — see module docstring.
    """
    if not request.lines:
        raise ValidationError("counter sale requires at least one line")

    customer = await _validate_customer(session, org_id=org_id, customer_id=request.customer_id)
    await _validate_warehouse(session, org_id=org_id, warehouse_id=request.warehouse_id)
    organization = await _validate_organization(session, org_id=org_id)
    products_by_id = await _validate_products(
        session, org_id=org_id, product_ids={line.product_id for line in request.lines}
    )

    priced = _price_lines(
        organization=organization,
        customer=customer,
        lines=request.lines,
        products_by_id=products_by_id,
        header_discount_percent=request.header_discount_percent,
        header_discount_amount=request.header_discount_amount,
    )
    await _check_credit(session, customer=customer, order_total=priced.grand_total)

    invoice_date = request.invoice_date or date.today()
    invoice_number = await next_document_number(
        session, org_id=org_id, doc_type="customer_invoice", on=invoice_date
    )
    due_date = invoice_date + timedelta(days=customer.payment_terms_days)

    customer_invoice = CustomerInvoice(
        org_id=org_id,
        customer_id=customer.id,
        sales_order_id=None,
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        due_date=due_date,
        status=InvoiceStatus.ISSUED,
        subtotal=priced.subtotal,
        tax_total=priced.tax_total,
        total=priced.grand_total,
        amount_paid=ZERO,
    )
    session.add(customer_invoice)
    await session.flush()

    # all-or-nothing: inventory.issue()'s batch call raises on the first
    # line with insufficient stock, leaving nothing committed — a counter
    # sale can't hand over partial goods the way an order can defer a
    # shortage to procurement.
    await inventory.issue(
        session,
        lines=[
            inventory.InventoryLine(
                product_id=line.product_id,
                warehouse_id=request.warehouse_id,
                quantity=line.quantity,
            )
            for line in priced.lines
        ],
        ref_type="customer_invoice",
        ref_id=customer_invoice.id,
        from_reservation=False,
    )

    line_results: list[CounterSaleLineResult] = []
    for priced_line in priced.lines:
        session.add(
            CustomerInvoiceItem(
                customer_invoice_id=customer_invoice.id,
                product_id=priced_line.product_id,
                hsn_code=priced_line.hsn_code,
                quantity=priced_line.quantity,
                unit_price=priced_line.unit_price,
                gst_rate=priced_line.gst_rate,
                line_subtotal=priced_line.taxable_value,
                line_tax=priced_line.tax_amount,
                line_total=priced_line.line_total,
            )
        )
        line_results.append(
            CounterSaleLineResult(
                product_id=priced_line.product_id,
                quantity=priced_line.quantity,
                unit_price=priced_line.unit_price,
                gst_rate=priced_line.gst_rate,
                line_subtotal=priced_line.taxable_value,
                line_tax=priced_line.tax_amount,
                line_total=priced_line.line_total,
            )
        )

    await session.flush()

    return CreateCounterSaleResult(
        customer_invoice_id=customer_invoice.id,
        invoice_number=invoice_number,
        status=customer_invoice.status,
        subtotal=priced.subtotal,
        tax_total=priced.tax_total,
        total=priced.grand_total,
        lines=line_results,
    )
