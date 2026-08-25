"""Goods-receipt and supplier-invoice intake — the two capture steps
matching.py's three-way match assumes already happened, but that (per
roadmap.txt's Phase 2 Definition of Done, "known gap") never had its own
service/route. Without these the P2P curl chain stops at PO creation.

Both are intake of an EXTERNAL document (the supplier's delivery, the
supplier's invoice) rather than something this system prices/generates —
unlike sales.py's customer_invoice creation, neither function calls
services/pricing.py: a goods receipt has no money at all, and a supplier
invoice's amounts are whatever the supplier actually billed (line_total is
taken as given, header subtotal/tax_total are derived by summing lines),
not recomputed by our own GST engine.

`create_goods_receipt()` is the one place besides sales.py/procurement.py
that calls services/inventory.py — `inventory.receive()` for every line
with `accepted_quantity > 0` (rejected/damaged units never entered
stock), inside the same transaction as the GoodsReceipt/GoodsReceiptItem
rows, same "ledger row and dependent state change in one transaction"
discipline as everywhere else. Over-receipt is guarded against the PO
line's OWN outstanding quantity (ordered minus already accepted across
every prior GRN for that line), not just the DB's per-row CHECK
constraints, since a series of small partial GRNs must never sum past
what was actually ordered.

Like every other services/ module, neither function commits — the caller
owns the transaction.
"""

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models.catalog import Product, Warehouse
from app.db.models.enums import PurchaseOrderStatus, SupplierInvoiceStatus
from app.db.models.partners import Supplier
from app.db.models.purchase import (
    GoodsReceipt,
    GoodsReceiptItem,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseRequisition,
    SupplierInvoice,
    SupplierInvoiceItem,
)
from app.services import inventory
from app.services.numbering import next_document_number

ZERO = Decimal("0")

# PO statuses a goods receipt can no longer be raised against — the
# purchase either never left DRAFT/approval, or the cycle already closed.
_PO_STATUSES_CLOSED_TO_RECEIPT = frozenset(
    {
        PurchaseOrderStatus.DRAFT,
        PurchaseOrderStatus.PENDING_APPROVAL,
        PurchaseOrderStatus.CANCELLED,
        PurchaseOrderStatus.INVOICED,
        PurchaseOrderStatus.PAID,
    }
)


class GoodsReceiptLineInput(BaseModel):
    purchase_order_item_id: uuid.UUID
    received_quantity: Decimal = Field(ge=0)
    accepted_quantity: Decimal = Field(ge=0)
    rejected_quantity: Decimal = Field(default=ZERO, ge=0)
    damaged_quantity: Decimal = Field(default=ZERO, ge=0)


class CreateGoodsReceiptRequest(BaseModel):
    purchase_order_id: uuid.UUID
    warehouse_id: uuid.UUID
    received_date: date
    lines: list[GoodsReceiptLineInput]


class GoodsReceiptLineResult(BaseModel):
    product_id: uuid.UUID
    purchase_order_item_id: uuid.UUID
    ordered_quantity: Decimal
    received_quantity: Decimal
    accepted_quantity: Decimal
    rejected_quantity: Decimal
    damaged_quantity: Decimal


class CreateGoodsReceiptResult(BaseModel):
    goods_receipt_id: uuid.UUID
    grn_number: str
    purchase_order_status: str
    lines: list[GoodsReceiptLineResult]
    triggered_by_sales_order_id: uuid.UUID | None


async def create_goods_receipt(
    session: AsyncSession, *, org_id: uuid.UUID, request: CreateGoodsReceiptRequest
) -> CreateGoodsReceiptResult:
    if not request.lines:
        raise ValidationError("goods receipt requires at least one line")

    purchase_order = await session.get(PurchaseOrder, request.purchase_order_id)
    if purchase_order is None or purchase_order.org_id != org_id:
        raise NotFoundError(f"purchase order {request.purchase_order_id} not found")
    if purchase_order.status in _PO_STATUSES_CLOSED_TO_RECEIPT:
        raise ConflictError(
            f"purchase order {request.purchase_order_id} is {purchase_order.status} "
            "and cannot receive goods"
        )

    warehouse = await session.get(Warehouse, request.warehouse_id)
    if warehouse is None or warehouse.org_id != org_id:
        raise NotFoundError(f"warehouse {request.warehouse_id} not found")

    po_item_ids = {line.purchase_order_item_id for line in request.lines}
    po_items = (
        (
            await session.execute(
                select(PurchaseOrderItem).where(
                    PurchaseOrderItem.id.in_(po_item_ids),
                    PurchaseOrderItem.purchase_order_id == purchase_order.id,
                )
            )
        )
        .scalars()
        .all()
    )
    po_item_by_id = {item.id: item for item in po_items}
    missing = po_item_ids - set(po_item_by_id)
    if missing:
        raise NotFoundError(
            f"purchase order item(s) not found on this PO: {', '.join(str(m) for m in missing)}"
        )

    prior_accepted_rows = (
        await session.execute(
            select(
                GoodsReceiptItem.purchase_order_item_id,
                func.coalesce(func.sum(GoodsReceiptItem.accepted_quantity), ZERO),
            )
            .where(GoodsReceiptItem.purchase_order_item_id.in_(po_item_ids))
            .group_by(GoodsReceiptItem.purchase_order_item_id)
        )
    ).all()
    prior_accepted: dict[uuid.UUID, Decimal] = {
        item_id: Decimal(total) for item_id, total in prior_accepted_rows
    }

    for line in request.lines:
        if line.accepted_quantity > line.received_quantity:
            raise ValidationError(
                f"purchase order item {line.purchase_order_item_id}: accepted quantity cannot "
                "exceed received quantity"
            )
        if line.accepted_quantity + line.rejected_quantity + line.damaged_quantity > (
            line.received_quantity
        ):
            raise ValidationError(
                f"purchase order item {line.purchase_order_item_id}: accepted + rejected + "
                "damaged cannot exceed received quantity"
            )
        po_item = po_item_by_id[line.purchase_order_item_id]
        already_accepted = Decimal(prior_accepted.get(line.purchase_order_item_id, ZERO))
        outstanding = Decimal(po_item.quantity) - already_accepted
        if line.received_quantity > outstanding:
            raise ConflictError(
                f"purchase order item {line.purchase_order_item_id}: only {outstanding} "
                f"still outstanding, cannot receive {line.received_quantity}"
            )

    grn_number = await next_document_number(
        session, org_id=org_id, doc_type="goods_receipt", on=request.received_date
    )
    goods_receipt = GoodsReceipt(
        org_id=org_id,
        purchase_order_id=purchase_order.id,
        warehouse_id=warehouse.id,
        grn_number=grn_number,
        received_date=request.received_date,
    )
    session.add(goods_receipt)
    await session.flush()

    line_results: list[GoodsReceiptLineResult] = []
    receive_lines: list[inventory.InventoryLine] = []
    for line in request.lines:
        po_item = po_item_by_id[line.purchase_order_item_id]
        session.add(
            GoodsReceiptItem(
                goods_receipt_id=goods_receipt.id,
                purchase_order_item_id=po_item.id,
                product_id=po_item.product_id,
                ordered_quantity=po_item.quantity,
                received_quantity=line.received_quantity,
                accepted_quantity=line.accepted_quantity,
                rejected_quantity=line.rejected_quantity,
                damaged_quantity=line.damaged_quantity,
            )
        )
        line_results.append(
            GoodsReceiptLineResult(
                product_id=po_item.product_id,
                purchase_order_item_id=po_item.id,
                ordered_quantity=Decimal(po_item.quantity),
                received_quantity=line.received_quantity,
                accepted_quantity=line.accepted_quantity,
                rejected_quantity=line.rejected_quantity,
                damaged_quantity=line.damaged_quantity,
            )
        )
        if line.accepted_quantity > ZERO:
            receive_lines.append(
                inventory.InventoryLine(
                    product_id=po_item.product_id,
                    warehouse_id=warehouse.id,
                    quantity=line.accepted_quantity,
                )
            )

    if receive_lines:
        await inventory.receive(
            session,
            lines=receive_lines,
            ref_type="goods_receipt",
            ref_id=goods_receipt.id,
        )

    # Fully received once every PO line's total accepted quantity (across
    # this GRN and every prior one) meets its ordered quantity.
    all_po_items = (
        (
            await session.execute(
                select(PurchaseOrderItem).where(
                    PurchaseOrderItem.purchase_order_id == purchase_order.id
                )
            )
        )
        .scalars()
        .all()
    )
    new_accepted_by_item = {
        line.purchase_order_item_id: line.accepted_quantity for line in request.lines
    }
    fully_received = True
    for item in all_po_items:
        already_accepted = Decimal(prior_accepted.get(item.id, ZERO))
        total_accepted = already_accepted + Decimal(new_accepted_by_item.get(item.id, ZERO))
        if total_accepted < Decimal(item.quantity):
            fully_received = False
            break

    purchase_order.status = (
        PurchaseOrderStatus.RECEIVED if fully_received else PurchaseOrderStatus.PARTIALLY_RECEIVED
    )

    # WF-05 (roadmap.txt 3.8) needs to know whether this PO traces back to
    # a specific sales order's shortage (reactive procurement, 2.8) so it
    # can retry that order's reservation now that stock arrived - resolved
    # here, in the same transaction, rather than making n8n chase
    # PO -> requisition -> triggered_by_sales_order_id itself over two more
    # round-trips.
    triggered_by_sales_order_id: uuid.UUID | None = None
    if purchase_order.purchase_requisition_id is not None:
        requisition = await session.get(PurchaseRequisition, purchase_order.purchase_requisition_id)
        if requisition is not None:
            triggered_by_sales_order_id = requisition.triggered_by_sales_order_id

    await session.flush()

    return CreateGoodsReceiptResult(
        goods_receipt_id=goods_receipt.id,
        grn_number=grn_number,
        purchase_order_status=purchase_order.status,
        lines=line_results,
        triggered_by_sales_order_id=triggered_by_sales_order_id,
    )


class SupplierInvoiceLineInput(BaseModel):
    product_id: uuid.UUID | None = None
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    gst_rate: Decimal = Field(ge=0)
    line_total: Decimal = Field(ge=0)


class CreateSupplierInvoiceRequest(BaseModel):
    supplier_id: uuid.UUID
    purchase_order_id: uuid.UUID | None = None
    invoice_number: str
    invoice_date: date
    due_date: date
    lines: list[SupplierInvoiceLineInput]


class SupplierInvoiceLineResult(BaseModel):
    product_id: uuid.UUID | None
    quantity: Decimal
    unit_price: Decimal
    gst_rate: Decimal
    line_total: Decimal


class CreateSupplierInvoiceResult(BaseModel):
    supplier_invoice_id: uuid.UUID
    invoice_number: str
    status: str
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    lines: list[SupplierInvoiceLineResult]


async def create_supplier_invoice(
    session: AsyncSession, *, org_id: uuid.UUID, request: CreateSupplierInvoiceRequest
) -> CreateSupplierInvoiceResult:
    if not request.lines:
        raise ValidationError("supplier invoice requires at least one line")
    if request.due_date < request.invoice_date:
        raise ValidationError("due date cannot be before invoice date")

    supplier = await session.get(Supplier, request.supplier_id)
    if supplier is None or supplier.org_id != org_id:
        raise NotFoundError(f"supplier {request.supplier_id} not found")

    if request.purchase_order_id is not None:
        purchase_order = await session.get(PurchaseOrder, request.purchase_order_id)
        if purchase_order is None or purchase_order.org_id != org_id:
            raise NotFoundError(f"purchase order {request.purchase_order_id} not found")
        if purchase_order.supplier_id != supplier.id:
            raise ConflictError(
                f"purchase order {request.purchase_order_id} was not raised against "
                f"supplier {request.supplier_id}"
            )

    product_ids = {line.product_id for line in request.lines if line.product_id is not None}
    if product_ids:
        products = (
            (
                await session.execute(
                    select(Product).where(Product.id.in_(product_ids), Product.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )
        missing = product_ids - {p.id for p in products}
        if missing:
            raise NotFoundError(f"product(s) not found: {', '.join(str(m) for m in missing)}")

    subtotal = sum((line.quantity * line.unit_price for line in request.lines), ZERO)
    total = sum((line.line_total for line in request.lines), ZERO)
    tax_total = total - subtotal

    supplier_invoice = SupplierInvoice(
        org_id=org_id,
        supplier_id=supplier.id,
        purchase_order_id=request.purchase_order_id,
        invoice_number=request.invoice_number,
        invoice_date=request.invoice_date,
        due_date=request.due_date,
        status=SupplierInvoiceStatus.RECEIVED,
        subtotal=subtotal,
        tax_total=tax_total,
        total=total,
    )
    session.add(supplier_invoice)
    await session.flush()

    line_results: list[SupplierInvoiceLineResult] = []
    for line in request.lines:
        session.add(
            SupplierInvoiceItem(
                supplier_invoice_id=supplier_invoice.id,
                product_id=line.product_id,
                quantity=line.quantity,
                unit_price=line.unit_price,
                gst_rate=line.gst_rate,
                line_total=line.line_total,
            )
        )
        line_results.append(
            SupplierInvoiceLineResult(
                product_id=line.product_id,
                quantity=line.quantity,
                unit_price=line.unit_price,
                gst_rate=line.gst_rate,
                line_total=line.line_total,
            )
        )
    await session.flush()

    return CreateSupplierInvoiceResult(
        supplier_invoice_id=supplier_invoice.id,
        invoice_number=request.invoice_number,
        status=supplier_invoice.status,
        subtotal=subtotal,
        tax_total=tax_total,
        total=total,
        lines=line_results,
    )
