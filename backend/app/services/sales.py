"""Order-to-cash entry point: create_sales_order() validates a new sales
order, prices it (services/pricing.py), checks the customer's credit, and
best-effort reserves stock (services/inventory.py) — one caller-owned
transaction, same convention as numbering.py/pricing.py/inventory.py (this
module never commits, so it composes inside a larger transaction).

Validation order is deliberate: customer/warehouse/product existence and
the credit check all happen BEFORE a document number is allocated or any
row is persisted, so a rejected order never burns a gapless sales_order
number (see services/numbering.py's module docstring on why gaps matter).
Stock reservation happens LAST, after the order row already exists, since
partial/zero reservation is a valid, expected outcome — not a reason to
reject the order (see below).

Stock reservation is deliberately best-effort per line, not all-or-nothing
for the whole order: `sales_order_items.reserved_qty` has a CHECK
constraint of `0 <= reserved_qty <= quantity` (Phase 1 schema) precisely
to allow a single line to be partially reserved. For each line this module
first tries to reserve the full ordered quantity (one atomic call into
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
valid and priced but nothing could be reserved at all). Turning a shortage
into a purchase requisition is procurement.py's job (Phase 2.8), not this
module's — this module only reports `shortage_qty` per line so that
caller can act on it.
"""

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models.catalog import Product, Warehouse
from app.db.models.enums import InvoiceStatus, SalesOrderStatus
from app.db.models.org import Organization
from app.db.models.partners import Customer
from app.db.models.sales import CustomerInvoice, SalesOrder, SalesOrderItem
from app.services import inventory
from app.services.numbering import next_document_number
from app.services.pricing import PricingLineInput, price_order

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


async def _customer_outstanding_balance(
    session: AsyncSession, *, customer_id: uuid.UUID
) -> Decimal:
    result = await session.execute(
        select(
            func.coalesce(func.sum(CustomerInvoice.total - CustomerInvoice.amount_paid), 0)
        ).where(
            CustomerInvoice.customer_id == customer_id,
            CustomerInvoice.status.notin_([InvoiceStatus.PAID, InvoiceStatus.VOID]),
        )
    )
    return Decimal(result.scalar_one())


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


async def create_sales_order(
    session: AsyncSession, *, org_id: uuid.UUID, request: CreateSalesOrderRequest
) -> CreateSalesOrderResult:
    if not request.lines:
        raise ValidationError("sales order requires at least one line")

    customer = await session.get(Customer, request.customer_id)
    if customer is None or customer.org_id != org_id:
        raise NotFoundError(f"customer {request.customer_id} not found")
    if not customer.is_active:
        raise ConflictError(f"customer {request.customer_id} is inactive")

    warehouse = await session.get(Warehouse, request.warehouse_id)
    if warehouse is None or warehouse.org_id != org_id:
        raise NotFoundError(f"warehouse {request.warehouse_id} not found")

    # customer.org_id has an FK to organizations.id (ON DELETE RESTRICT), so
    # if the customer check above passed, this row is guaranteed to exist —
    # defensive only, not reachable under the current schema.
    organization = await session.get(Organization, org_id)
    if organization is None:
        raise NotFoundError(f"organization {org_id} not found")

    product_ids = {line.product_id for line in request.lines}
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
        for line in request.lines
    ]
    priced = price_order(
        origin_state_code=organization.state_code or "",
        place_of_supply_state_code=customer.state_code or "",
        lines=pricing_lines,
        header_discount_percent=request.header_discount_percent,
        header_discount_amount=request.header_discount_amount,
    )

    outstanding = await _customer_outstanding_balance(session, customer_id=customer.id)
    if outstanding + priced.grand_total > customer.credit_limit:
        raise ConflictError(
            f"order total {priced.grand_total} would push customer {customer.id}'s "
            f"outstanding balance to {outstanding + priced.grand_total}, over their "
            f"credit limit of {customer.credit_limit}"
        )

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
        line_results.append(
            SalesOrderLineResult(
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
        )

    fully_reserved = all(line.shortage_qty == ZERO for line in line_results)
    any_reserved = any(line.reserved_qty > ZERO for line in line_results)
    if fully_reserved:
        sales_order.status = SalesOrderStatus.RESERVED
    elif any_reserved:
        sales_order.status = SalesOrderStatus.PARTIALLY_RESERVED
    else:
        sales_order.status = SalesOrderStatus.CONFIRMED

    await session.flush()

    return CreateSalesOrderResult(
        sales_order_id=sales_order.id,
        order_number=order_number,
        status=sales_order.status,
        subtotal=priced.subtotal,
        tax_total=priced.tax_total,
        total=priced.grand_total,
        lines=line_results,
        has_shortage=not fully_reserved,
    )
