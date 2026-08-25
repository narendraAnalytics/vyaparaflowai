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
from dataclasses import dataclass
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
    Delivery,
    DeliveryItem,
    SalesOrder,
    SalesOrderItem,
)
from app.db.models.workflow import AuditLog, Document
from app.integrations.storage.minio_client import upload_bytes
from app.services import inventory
from app.services.documents import (
    CustomerInvoicePdfInput,
    CustomerInvoicePdfLine,
    render_customer_invoice_pdf,
)
from app.services.numbering import next_document_number
from app.services.outbox import write_event
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


class RetryReservationResult(BaseModel):
    sales_order_id: uuid.UUID
    status: str
    lines: list[SalesOrderLineResult]


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

    has_shortage = any(line.shortage_qty > ZERO for line in line_results)
    # A DRAFT (quote) order's has_shortage is structurally always True
    # (inventory is skipped entirely) but nothing has been committed to
    # yet - see the module docstring. Only a real, non-quote shortage is
    # an outbox-worthy fact WF-02 should act on.
    if not request.is_quote and has_shortage:
        await write_event(
            session,
            aggregate_type="sales_order",
            aggregate_id=sales_order.id,
            event_type="shortage.detected",
            payload={
                "org_id": str(org_id),
                "sales_order_id": str(sales_order.id),
                "order_number": order_number,
                "warehouse_id": str(request.warehouse_id),
            },
        )

    await session.flush()

    return CreateSalesOrderResult(
        sales_order_id=sales_order.id,
        order_number=order_number,
        status=sales_order.status,
        subtotal=priced.subtotal,
        tax_total=priced.tax_total,
        total=priced.grand_total,
        lines=line_results,
        has_shortage=has_shortage,
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

    has_shortage = any(line.shortage_qty > ZERO for line in line_results)
    # Unlike create_sales_order()'s DRAFT case, confirming IS the moment
    # this order becomes binding - a real shortage here is outbox-worthy.
    if has_shortage:
        await write_event(
            session,
            aggregate_type="sales_order",
            aggregate_id=sales_order.id,
            event_type="shortage.detected",
            payload={
                "org_id": str(org_id),
                "sales_order_id": str(sales_order.id),
                "order_number": sales_order.order_number,
                "warehouse_id": str(warehouse_id),
            },
        )

    await session.flush()

    return CreateSalesOrderResult(
        sales_order_id=sales_order.id,
        order_number=sales_order.order_number,
        status=sales_order.status,
        subtotal=sales_order.subtotal,
        tax_total=sales_order.tax_total,
        total=sales_order.total,
        lines=line_results,
        has_shortage=has_shortage,
    )


async def retry_reservation(
    session: AsyncSession, *, org_id: uuid.UUID, sales_order_id: uuid.UUID, warehouse_id: uuid.UUID
) -> RetryReservationResult:
    """Re-attempt best-effort reservation for whatever's still short on a
    CONFIRMED or PARTIALLY_RESERVED order, e.g. once new stock arrives via
    a goods receipt (WF-05, roadmap.txt 3.8's "if SO waiting, trigger
    reserve"). `warehouse_id` is a required caller-supplied param, not
    read off the sales order, because SalesOrder itself has no
    warehouse_id column - same convention as GET /sales-orders/{id}/
    shortages already requiring it explicitly (2.8) - so WF-05 passes the
    warehouse the goods receipt was just recorded against.

    RESERVED (nothing left to retry) and DRAFT (an unconfirmed quote -
    reservation was skipped on purpose, see the module docstring) are
    both rejected rather than silently no-opping, since either would
    otherwise look like a successful retry that did nothing.
    """
    sales_order = await session.get(SalesOrder, sales_order_id)
    if sales_order is None or sales_order.org_id != org_id:
        raise NotFoundError(f"sales order {sales_order_id} not found")
    if sales_order.status not in (SalesOrderStatus.CONFIRMED, SalesOrderStatus.PARTIALLY_RESERVED):
        raise ConflictError(
            f"sales order {sales_order_id} is {sales_order.status}, nothing to retry"
        )
    await _validate_warehouse(session, org_id=org_id, warehouse_id=warehouse_id)

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
        already_reserved = Decimal(item.reserved_qty)
        shortage = quantity - already_reserved
        if shortage > ZERO:
            additional = await _best_effort_reserve(
                session,
                product_id=item.product_id,
                warehouse_id=warehouse_id,
                quantity=shortage,
                ref_id=sales_order.id,
            )
            already_reserved += additional
            item.reserved_qty = already_reserved  # type: ignore[assignment]  # Mapped[float] hint is cosmetic, see module docstring
        line_results.append(
            SalesOrderLineResult(
                product_id=item.product_id,
                quantity=quantity,
                reserved_qty=already_reserved,
                shortage_qty=quantity - already_reserved,
                unit_price=Decimal(item.unit_price),
                gst_rate=Decimal(item.gst_rate),
                line_subtotal=Decimal(item.line_subtotal),
                line_tax=Decimal(item.line_tax),
                line_total=Decimal(item.line_total),
            )
        )

    _finalize_status(sales_order, line_results)
    await session.flush()

    return RetryReservationResult(
        sales_order_id=sales_order.id, status=sales_order.status, lines=line_results
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


class DeliveryLineInput(BaseModel):
    sales_order_item_id: uuid.UUID
    quantity: Decimal = Field(gt=0)


class CreateDeliveryRequest(BaseModel):
    sales_order_id: uuid.UUID
    warehouse_id: uuid.UUID
    lines: list[DeliveryLineInput]
    dispatched_at: date | None = None


class DeliveryLineResult(BaseModel):
    sales_order_item_id: uuid.UUID
    product_id: uuid.UUID
    quantity: Decimal


class CreateDeliveryResult(BaseModel):
    delivery_id: uuid.UUID
    delivery_number: str
    sales_order_id: uuid.UUID
    sales_order_status: str
    lines: list[DeliveryLineResult]


_DISPATCHABLE_STATUSES = (
    SalesOrderStatus.RESERVED,
    SalesOrderStatus.PARTIALLY_RESERVED,
    SalesOrderStatus.DISPATCHED,
)


async def create_delivery(
    session: AsyncSession, *, org_id: uuid.UUID, request: CreateDeliveryRequest
) -> CreateDeliveryResult:
    """Dispatch reserved stock against a sales order (WF-06, roadmap.txt
    3.9). Only quantities already RESERVED (services/inventory.py's
    reserved_qty column) can be dispatched - a delivery consumes an
    existing reservation, it never reserves on its own. Guarded against
    each line's own OUTSTANDING deliverable quantity (reserved minus
    already-delivered across every prior delivery for that line), the
    same "sum of partials never exceeds the total" pattern
    services/receiving.py's over-receipt guard uses on the P2P side.

    SalesOrderStatus.DISPATCHED covers both a first partial delivery and
    any later one for the same order. SalesOrderStatus.DELIVERED (a
    distinct, later "customer confirmed receipt" step the Phase 1 schema
    already has a column for - deliveries.delivered_at) is intentionally
    NOT set here - nothing in this project yet confirms physical receipt,
    same "documented, not silently skipped" treatment every other
    not-yet-reachable enum value in this codebase gets.
    """
    if not request.lines:
        raise ValidationError("delivery requires at least one line")

    sales_order = await session.get(SalesOrder, request.sales_order_id)
    if sales_order is None or sales_order.org_id != org_id:
        raise NotFoundError(f"sales order {request.sales_order_id} not found")
    if sales_order.status not in _DISPATCHABLE_STATUSES:
        raise ConflictError(
            f"sales order {request.sales_order_id} has nothing reserved to dispatch "
            f"(status={sales_order.status})"
        )
    await _validate_warehouse(session, org_id=org_id, warehouse_id=request.warehouse_id)

    so_item_ids = {line.sales_order_item_id for line in request.lines}
    so_items = (
        (
            await session.execute(
                select(SalesOrderItem).where(
                    SalesOrderItem.id.in_(so_item_ids),
                    SalesOrderItem.sales_order_id == sales_order.id,
                )
            )
        )
        .scalars()
        .all()
    )
    so_item_by_id = {item.id: item for item in so_items}
    missing = so_item_ids - set(so_item_by_id)
    if missing:
        raise NotFoundError(
            f"sales order item(s) not found on this order: {', '.join(str(m) for m in missing)}"
        )

    prior_delivered_rows = (
        await session.execute(
            select(
                DeliveryItem.sales_order_item_id,
                func.coalesce(func.sum(DeliveryItem.quantity), ZERO),
            )
            .where(DeliveryItem.sales_order_item_id.in_(so_item_ids))
            .group_by(DeliveryItem.sales_order_item_id)
        )
    ).all()
    prior_delivered: dict[uuid.UUID, Decimal] = {
        item_id: Decimal(total) for item_id, total in prior_delivered_rows
    }

    for line in request.lines:
        so_item = so_item_by_id[line.sales_order_item_id]
        already_delivered = prior_delivered.get(line.sales_order_item_id, ZERO)
        outstanding = Decimal(so_item.reserved_qty) - already_delivered
        if line.quantity > outstanding:
            raise ConflictError(
                f"sales order item {line.sales_order_item_id}: only {outstanding} reserved "
                f"and undelivered, cannot dispatch {line.quantity}"
            )

    dispatched_at = request.dispatched_at or date.today()
    delivery_number = await next_document_number(
        session, org_id=org_id, doc_type="delivery", on=dispatched_at
    )
    delivery = Delivery(
        org_id=org_id,
        sales_order_id=sales_order.id,
        warehouse_id=request.warehouse_id,
        delivery_number=delivery_number,
        dispatched_at=dispatched_at,
    )
    session.add(delivery)
    await session.flush()

    line_results: list[DeliveryLineResult] = []
    for line in request.lines:
        so_item = so_item_by_id[line.sales_order_item_id]
        session.add(
            DeliveryItem(
                delivery_id=delivery.id,
                sales_order_item_id=so_item.id,
                product_id=so_item.product_id,
                quantity=line.quantity,
            )
        )
        line_results.append(
            DeliveryLineResult(
                sales_order_item_id=so_item.id,
                product_id=so_item.product_id,
                quantity=line.quantity,
            )
        )

    # inventory.issue() sorts its own lines by (product_id, warehouse_id)
    # internally (deadlock avoidance) - no need to pre-sort here, same as
    # receiving.py's create_goods_receipt().
    await inventory.issue(
        session,
        lines=[
            inventory.InventoryLine(
                product_id=so_item_by_id[line.sales_order_item_id].product_id,
                warehouse_id=request.warehouse_id,
                quantity=line.quantity,
            )
            for line in request.lines
        ],
        ref_type="delivery",
        ref_id=delivery.id,
        from_reservation=True,
    )

    sales_order.status = SalesOrderStatus.DISPATCHED
    await session.flush()

    return CreateDeliveryResult(
        delivery_id=delivery.id,
        delivery_number=delivery_number,
        sales_order_id=sales_order.id,
        sales_order_status=sales_order.status,
        lines=line_results,
    )


class CreateCustomerInvoiceFromDeliveryRequest(BaseModel):
    delivery_id: uuid.UUID
    invoice_date: date | None = None


class CustomerInvoiceLineResult(BaseModel):
    product_id: uuid.UUID
    quantity: Decimal
    unit_price: Decimal
    gst_rate: Decimal
    line_subtotal: Decimal
    line_tax: Decimal
    line_total: Decimal


class CreateCustomerInvoiceFromDeliveryResult(BaseModel):
    customer_invoice_id: uuid.UUID
    invoice_number: str
    status: str
    customer_name: str
    customer_email: str | None
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    lines: list[CustomerInvoiceLineResult]


async def create_customer_invoice_from_delivery(
    session: AsyncSession, *, org_id: uuid.UUID, request: CreateCustomerInvoiceFromDeliveryRequest
) -> CreateCustomerInvoiceFromDeliveryResult:
    """Bills exactly what a delivery actually shipped (WF-06, roadmap.txt
    3.9). Reuses each line's ORIGINAL sales-order per-unit economics
    (SalesOrderItem.line_subtotal/line_tax proportionally sliced by
    delivered ÷ ordered quantity) rather than re-pricing through
    services/pricing.py - the sales order is the binding commercial
    agreement (price + any discount already locked in at order time), and
    SalesOrderItem doesn't retain the original discount_percent/
    discount_amount inputs needed to reconstruct that price through
    price_order() again. Proportional slicing preserves the exact rate
    without needing those inputs (GST is a flat percentage of taxable
    value, so it scales linearly with quantity).

    customer_invoices.delivery_id carries a UNIQUE constraint - the DB
    itself rejects invoicing the same delivery twice, not just this
    function's own logic.
    """
    delivery = await session.get(Delivery, request.delivery_id)
    if delivery is None or delivery.org_id != org_id:
        raise NotFoundError(f"delivery {request.delivery_id} not found")

    sales_order = await session.get(SalesOrder, delivery.sales_order_id)
    if sales_order is None:
        raise NotFoundError(f"sales order {delivery.sales_order_id} not found")
    customer = await _validate_customer(session, org_id=org_id, customer_id=sales_order.customer_id)
    await _validate_organization(session, org_id=org_id)

    delivery_items = (
        (await session.execute(select(DeliveryItem).where(DeliveryItem.delivery_id == delivery.id)))
        .scalars()
        .all()
    )
    if not delivery_items:
        raise ValidationError(f"delivery {delivery.id} has no lines")

    so_item_ids = {item.sales_order_item_id for item in delivery_items}
    so_items = (
        (await session.execute(select(SalesOrderItem).where(SalesOrderItem.id.in_(so_item_ids))))
        .scalars()
        .all()
    )
    so_item_by_id = {item.id: item for item in so_items}

    products = (
        (
            await session.execute(
                select(Product).where(Product.id.in_({item.product_id for item in delivery_items}))
            )
        )
        .scalars()
        .all()
    )
    products_by_id = {p.id: p for p in products}

    invoice_date = request.invoice_date or date.today()
    invoice_number = await next_document_number(
        session, org_id=org_id, doc_type="customer_invoice", on=invoice_date
    )
    due_date = invoice_date + timedelta(days=customer.payment_terms_days)

    customer_invoice = CustomerInvoice(
        org_id=org_id,
        customer_id=customer.id,
        sales_order_id=sales_order.id,
        delivery_id=delivery.id,
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        due_date=due_date,
        status=InvoiceStatus.ISSUED,
        subtotal=ZERO,
        tax_total=ZERO,
        total=ZERO,
        amount_paid=ZERO,
    )
    session.add(customer_invoice)
    await session.flush()

    line_results: list[CustomerInvoiceLineResult] = []
    subtotal = ZERO
    tax_total = ZERO
    total = ZERO
    for d_item in delivery_items:
        so_item = so_item_by_id[d_item.sales_order_item_id]
        ratio = Decimal(d_item.quantity) / Decimal(so_item.quantity)
        line_subtotal = (Decimal(so_item.line_subtotal) * ratio).quantize(Decimal("0.01"))
        line_tax = (Decimal(so_item.line_tax) * ratio).quantize(Decimal("0.01"))
        line_total = line_subtotal + line_tax
        unit_price = Decimal(so_item.unit_price)
        gst_rate = Decimal(so_item.gst_rate)

        session.add(
            CustomerInvoiceItem(
                customer_invoice_id=customer_invoice.id,
                product_id=d_item.product_id,
                hsn_code=products_by_id[d_item.product_id].hsn_code,
                quantity=d_item.quantity,
                unit_price=unit_price,
                gst_rate=gst_rate,
                line_subtotal=line_subtotal,
                line_tax=line_tax,
                line_total=line_total,
            )
        )
        line_results.append(
            CustomerInvoiceLineResult(
                product_id=d_item.product_id,
                quantity=Decimal(d_item.quantity),
                unit_price=unit_price,
                gst_rate=gst_rate,
                line_subtotal=line_subtotal,
                line_tax=line_tax,
                line_total=line_total,
            )
        )
        subtotal += line_subtotal
        tax_total += line_tax
        total += line_total

    customer_invoice.subtotal = subtotal  # type: ignore[assignment]  # Mapped[float] hint is cosmetic, see module docstring
    customer_invoice.tax_total = tax_total  # type: ignore[assignment]
    customer_invoice.total = total  # type: ignore[assignment]

    # Advance the order to INVOICED only once every delivery raised
    # against it has been billed - a partial shipment can be invoiced on
    # its own without blocking a later delivery/invoice pair for the rest
    # of the order.
    all_delivery_ids = (
        (
            await session.execute(
                select(Delivery.id).where(Delivery.sales_order_id == sales_order.id)
            )
        )
        .scalars()
        .all()
    )
    invoiced_delivery_ids = set(
        (
            await session.execute(
                select(CustomerInvoice.delivery_id).where(
                    CustomerInvoice.delivery_id.in_(all_delivery_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    invoiced_delivery_ids.add(delivery.id)
    if invoiced_delivery_ids >= set(all_delivery_ids):
        sales_order.status = SalesOrderStatus.INVOICED

    await session.flush()

    return CreateCustomerInvoiceFromDeliveryResult(
        customer_invoice_id=customer_invoice.id,
        invoice_number=invoice_number,
        status=customer_invoice.status,
        customer_name=customer.name,
        customer_email=customer.email,
        subtotal=subtotal,
        tax_total=tax_total,
        total=total,
        lines=line_results,
    )


@dataclass
class CustomerInvoicePdfResult:
    document_id: uuid.UUID
    invoice_number: str
    storage_uri: str
    pdf_bytes: bytes


async def generate_customer_invoice_pdf(
    session: AsyncSession, *, org_id: uuid.UUID, customer_invoice_id: uuid.UUID
) -> CustomerInvoicePdfResult:
    """Mirrors procurement.py's generate_purchase_order_pdf() - renders
    (services/documents.py, pure), uploads to object storage, and
    persists a `documents` row; returns raw bytes so n8n (WF-06) can
    attach the invoice to the customer email in one round-trip, without
    needing its own MinIO credentials.
    """
    invoice = await session.get(CustomerInvoice, customer_invoice_id)
    if invoice is None or invoice.org_id != org_id:
        raise NotFoundError(f"customer invoice {customer_invoice_id} not found")
    customer = await session.get(Customer, invoice.customer_id)
    if customer is None:
        raise NotFoundError(f"customer {invoice.customer_id} not found")
    organization = await session.get(Organization, org_id)
    if organization is None:
        raise NotFoundError(f"organization {org_id} not found")

    items = (
        (
            await session.execute(
                select(CustomerInvoiceItem).where(
                    CustomerInvoiceItem.customer_invoice_id == invoice.id
                )
            )
        )
        .scalars()
        .all()
    )
    products = (
        (
            await session.execute(
                select(Product).where(Product.id.in_({item.product_id for item in items}))
            )
        )
        .scalars()
        .all()
    )
    products_by_id = {p.id: p for p in products}

    pdf_input = CustomerInvoicePdfInput(
        invoice_number=invoice.invoice_number,
        invoice_date=invoice.invoice_date,
        due_date=invoice.due_date,
        org_name=organization.name,
        org_gstin=organization.gstin,
        org_address=organization.address,
        customer_name=customer.name,
        customer_gstin=customer.gstin,
        customer_address=customer.address,
        lines=[
            CustomerInvoicePdfLine(
                sku=products_by_id[item.product_id].sku,
                product_name=products_by_id[item.product_id].name,
                quantity=Decimal(item.quantity),
                unit_price=Decimal(item.unit_price),
                gst_rate=Decimal(item.gst_rate),
                line_total=Decimal(item.line_total),
            )
            for item in items
        ],
        subtotal=Decimal(invoice.subtotal),
        tax_total=Decimal(invoice.tax_total),
        total=Decimal(invoice.total),
    )
    pdf_bytes = render_customer_invoice_pdf(pdf_input)

    storage_uri, checksum = upload_bytes(
        key=f"customer-invoices/{org_id}/{invoice.invoice_number}.pdf",
        data=pdf_bytes,
        content_type="application/pdf",
    )

    document = (
        await session.execute(select(Document).where(Document.checksum == checksum))
    ).scalar_one_or_none()
    if document is None:
        document = Document(
            org_id=org_id,
            doc_type="customer_invoice_pdf",
            storage_uri=storage_uri,
            checksum=checksum,
            content_type="application/pdf",
            size_bytes=len(pdf_bytes),
        )
        session.add(document)
        await session.flush()

    invoice.pdf_storage_uri = storage_uri
    await session.flush()

    return CustomerInvoicePdfResult(
        document_id=document.id,
        invoice_number=invoice.invoice_number,
        storage_uri=storage_uri,
        pdf_bytes=pdf_bytes,
    )


class CustomerInvoiceStatusResult(BaseModel):
    customer_invoice_id: uuid.UUID
    invoice_number: str
    status: str


async def mark_customer_invoice_sent(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    customer_invoice_id: uuid.UUID,
    actor_id: uuid.UUID | None = None,
) -> CustomerInvoiceStatusResult:
    """WF-06's last step, once the customer email has actually sent.
    InvoiceStatus (Phase 1 schema) has no dedicated SENT value the way
    PurchaseOrderStatus does for the P2P side - status stays ISSUED (set
    at creation) and this only records an audit-log entry, the same
    "document the schema asymmetry, don't silently paper over it"
    treatment services/payments.py already gave supplier invoices'
    missing PARTIALLY_PAID value.
    """
    invoice = await session.get(CustomerInvoice, customer_invoice_id)
    if invoice is None or invoice.org_id != org_id:
        raise NotFoundError(f"customer invoice {customer_invoice_id} not found")
    session.add(
        AuditLog(
            org_id=org_id,
            actor_id=actor_id,
            action="customer_invoice.sent",
            entity_type="customer_invoice",
            entity_id=invoice.id,
            after_json={"status": invoice.status},
        )
    )
    await session.flush()
    return CustomerInvoiceStatusResult(
        customer_invoice_id=invoice.id,
        invoice_number=invoice.invoice_number,
        status=invoice.status,
    )
