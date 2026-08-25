"""Order-to-cash HTTP surface: wraps services/sales.py. Request/response
bodies reuse that module's own Pydantic models directly (CreateSalesOrder
Request/Result, CreateCounterSaleRequest/Result) rather than duplicating
them into app/schemas/ — those models already ARE the wire contract, the
same way master-data CRUD's app/schemas/master_data.py models are.
"""

import uuid

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_perm
from app.core.exceptions import ConflictError
from app.db.models.org import User
from app.db.session import get_db
from app.services.sales import (
    CreateCounterSaleRequest,
    CreateCounterSaleResult,
    CreateCustomerInvoiceFromDeliveryRequest,
    CreateCustomerInvoiceFromDeliveryResult,
    CreateDeliveryRequest,
    CreateDeliveryResult,
    CreateSalesOrderRequest,
    CreateSalesOrderResult,
    CustomerInvoiceStatusResult,
    RetryReservationResult,
    confirm_sales_order,
    create_counter_sale,
    create_customer_invoice_from_delivery,
    create_delivery,
    create_sales_order,
    generate_customer_invoice_pdf,
    mark_customer_invoice_sent,
    retry_reservation,
)

router = APIRouter(tags=["sales"])


class ConfirmSalesOrderRequest(BaseModel):
    warehouse_id: uuid.UUID


@router.post(
    "/sales-orders",
    response_model=CreateSalesOrderResult,
    status_code=201,
    operation_id="createSalesOrder",
    summary="Create a sales order or quote",
    description=(
        "Prices the lines, checks customer credit, and best-effort reserves stock "
        "(unless `is_quote=true`, which skips inventory entirely). Resulting status is "
        "one of RESERVED / PARTIALLY_RESERVED / CONFIRMED for a binding order, or DRAFT "
        "for a quote — see services/sales.py for the full state machine."
    ),
)
async def create_sales_order_endpoint(
    payload: CreateSalesOrderRequest,
    user: User = Depends(require_perm("sales_order.create")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> CreateSalesOrderResult:
    result = await create_sales_order(db, org_id=user.org_id, request=payload)
    await db.commit()
    return result


@router.post(
    "/sales-orders/{sales_order_id}/confirm",
    response_model=CreateSalesOrderResult,
    operation_id="confirmSalesOrder",
    summary="Confirm a quote into a binding order",
    description=(
        "Re-checks credit and best-effort reserves stock at the given warehouse, using "
        "the price locked in at quote time. Only valid for a DRAFT (quote) sales order."
    ),
)
async def confirm_sales_order_endpoint(
    sales_order_id: uuid.UUID,
    payload: ConfirmSalesOrderRequest,
    user: User = Depends(require_perm("sales_order.create")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> CreateSalesOrderResult:
    result = await confirm_sales_order(
        db, org_id=user.org_id, sales_order_id=sales_order_id, warehouse_id=payload.warehouse_id
    )
    await db.commit()
    return result


@router.post(
    "/sales-orders/{sales_order_id}/retry-reservation",
    response_model=RetryReservationResult,
    operation_id="retrySalesOrderReservation",
    summary="Re-attempt reservation for a sales order's outstanding shortage",
    description=(
        "WF-05's (roadmap.txt 3.8) 'if SO waiting, trigger reserve' step - called once a "
        "goods receipt brings in stock for a product a CONFIRMED/PARTIALLY_RESERVED order "
        "was still short on. `warehouse_id` is caller-supplied (SalesOrder has no "
        "warehouse_id column, same as the existing shortages endpoint)."
    ),
)
async def retry_sales_order_reservation_endpoint(
    sales_order_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    user: User = Depends(require_perm("sales_order.create")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> RetryReservationResult:
    result = await retry_reservation(
        db, org_id=user.org_id, sales_order_id=sales_order_id, warehouse_id=warehouse_id
    )
    await db.commit()
    return result


@router.post(
    "/deliveries",
    response_model=CreateDeliveryResult,
    status_code=201,
    operation_id="createDelivery",
    summary="Dispatch reserved stock against a sales order",
    description=(
        "WF-06 (roadmap.txt 3.9): consumes an existing reservation (never reserves on its "
        "own), guarded per line against what's still reserved-and-undelivered across every "
        "prior delivery for that line. Advances the sales order to DISPATCHED."
    ),
)
async def create_delivery_endpoint(
    payload: CreateDeliveryRequest,
    user: User = Depends(require_perm("delivery.create")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> CreateDeliveryResult:
    result = await create_delivery(db, org_id=user.org_id, request=payload)
    await db.commit()
    return result


@router.post(
    "/customer-invoices",
    response_model=CreateCustomerInvoiceFromDeliveryResult,
    status_code=201,
    operation_id="createCustomerInvoiceFromDelivery",
    summary="Bill exactly what a delivery shipped",
    description=(
        "WF-06 (roadmap.txt 3.9): reuses the sales order's original locked-in per-unit "
        "price/discount (proportionally sliced by delivered vs. ordered quantity) rather "
        "than re-pricing. One invoice per delivery - enforced by a DB UNIQUE constraint on "
        "customer_invoices.delivery_id, not just application logic."
    ),
)
async def create_customer_invoice_endpoint(
    payload: CreateCustomerInvoiceFromDeliveryRequest,
    user: User = Depends(require_perm("invoice.create")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> CreateCustomerInvoiceFromDeliveryResult:
    try:
        result = await create_customer_invoice_from_delivery(
            db, org_id=user.org_id, request=payload
        )
    except IntegrityError as exc:
        raise ConflictError(f"delivery {payload.delivery_id} has already been invoiced") from exc
    await db.commit()
    return result


@router.post(
    "/customer-invoices/{customer_invoice_id}/pdf",
    operation_id="generateCustomerInvoicePdf",
    summary="Render a customer invoice to PDF, store it, and return the bytes",
    description=(
        "Renders the invoice, uploads it to object storage, persists a documents row, and "
        "returns the raw PDF bytes so WF-06 can attach it directly to the customer email "
        "without needing its own storage credentials - mirrors /purchase-orders/{id}/pdf."
    ),
)
async def generate_customer_invoice_pdf_endpoint(
    customer_invoice_id: uuid.UUID,
    user: User = Depends(require_perm("invoice.create")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    result = await generate_customer_invoice_pdf(
        db, org_id=user.org_id, customer_invoice_id=customer_invoice_id
    )
    await db.commit()
    return Response(
        content=result.pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{result.invoice_number}.pdf"'},
    )


@router.post(
    "/customer-invoices/{customer_invoice_id}/mark-sent",
    response_model=CustomerInvoiceStatusResult,
    operation_id="markCustomerInvoiceSent",
    summary="Record that a customer invoice email was sent",
    description=(
        "WF-06's last step, once the Resend send has actually gone out. InvoiceStatus has "
        "no dedicated SENT value (unlike PurchaseOrderStatus) - this records an audit-log "
        "entry rather than a status transition."
    ),
)
async def mark_customer_invoice_sent_endpoint(
    customer_invoice_id: uuid.UUID,
    user: User = Depends(require_perm("invoice.create")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> CustomerInvoiceStatusResult:
    result = await mark_customer_invoice_sent(
        db, org_id=user.org_id, customer_invoice_id=customer_invoice_id, actor_id=user.id
    )
    await db.commit()
    return result


@router.post(
    "/counter-sales",
    response_model=CreateCounterSaleResult,
    status_code=201,
    operation_id="createCounterSale",
    summary="Record a walk-in/till counter sale",
    description=(
        "Skips Sales Order + Delivery entirely: creates a customer invoice directly and "
        "reduces stock immediately, all-or-nothing across every line (no partial "
        "reservation — see services/sales.py)."
    ),
)
async def create_counter_sale_endpoint(
    payload: CreateCounterSaleRequest,
    user: User = Depends(require_perm("sales_order.create")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> CreateCounterSaleResult:
    result = await create_counter_sale(db, org_id=user.org_id, request=payload)
    await db.commit()
    return result
