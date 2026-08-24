"""Procure-to-pay HTTP surface: wraps services/procurement.py. Reads
(shortages, supplier scores) are open to any authenticated org member,
same convention as master-data CRUD; writes (requisition/PO creation) are
gated by po.create (Manager).
"""

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_perm
from app.core.exceptions import NotFoundError
from app.db.models.catalog import Product
from app.db.models.org import User
from app.db.session import get_db
from app.services.procurement import (
    CreatePurchaseOrderResult,
    CreateRequisitionRequest,
    CreateRequisitionResult,
    PurchaseOrderStatusResult,
    ShortageLine,
    SupplierScore,
    create_purchase_orders_from_requisition,
    create_requisition,
    detect_shortage_from_sales_order,
    detect_shortages,
    generate_purchase_order_pdf,
    mark_purchase_order_approved,
    mark_purchase_order_rejected,
    mark_purchase_order_sent,
    score_suppliers,
)

router = APIRouter(tags=["procurement"])


@router.get(
    "/shortages",
    response_model=list[ShortageLine],
    operation_id="listShortages",
    summary="List products below reorder level at a warehouse",
    description=(
        "Proactive shortage scan: every product at the warehouse currently below its "
        "reorder point, each with a recommended reorder quantity already computed."
    ),
)
async def list_shortages(
    warehouse_id: uuid.UUID,
    lookback_days: int = Query(default=30, ge=1, le=365),
    user: User = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> list[ShortageLine]:
    return await detect_shortages(
        db, org_id=user.org_id, warehouse_id=warehouse_id, lookback_days=lookback_days
    )


@router.get(
    "/sales-orders/{sales_order_id}/shortages",
    response_model=list[ShortageLine],
    operation_id="listShortagesForSalesOrder",
    summary="List a sales order's unfulfilled lines as shortages",
    description=(
        "Reactive shortage scan: this order's unfulfilled lines (ordered minus reserved), "
        "each with a recommended reorder quantity. Rejects an unconfirmed quote — nothing "
        "has been reserved yet, so there's no committed shortage to act on."
    ),
)
async def list_shortages_for_sales_order(
    sales_order_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    lookback_days: int = Query(default=30, ge=1, le=365),
    user: User = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> list[ShortageLine]:
    return await detect_shortage_from_sales_order(
        db,
        org_id=user.org_id,
        sales_order_id=sales_order_id,
        warehouse_id=warehouse_id,
        lookback_days=lookback_days,
    )


@router.get(
    "/products/{product_id}/supplier-scores",
    response_model=list[SupplierScore],
    operation_id="listSupplierScores",
    summary="Rank a product's suppliers, best first",
    description=(
        "Explainable 0-100 ranking across every active supplier configured for this "
        "product (price 40% + lead time 25% + reliability 35% + preferred bonus). Empty "
        "list means no supplier is configured for this product at all."
    ),
)
async def list_supplier_scores(
    product_id: uuid.UUID,
    user: User = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> list[SupplierScore]:
    product = await db.get(Product, product_id)
    if product is None or product.org_id != user.org_id:
        raise NotFoundError(f"product {product_id} not found")
    return await score_suppliers(db, product_id=product_id)


@router.post(
    "/purchase-requisitions",
    response_model=CreateRequisitionResult,
    status_code=201,
    operation_id="createPurchaseRequisition",
    summary="Create a purchase requisition",
    description=(
        "Persists a requisition (status=PENDING_APPROVAL) from a list of product/quantity lines."
    ),
)
async def create_purchase_requisition_endpoint(
    payload: CreateRequisitionRequest,
    user: User = Depends(require_perm("po.create")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> CreateRequisitionResult:
    result = await create_requisition(db, org_id=user.org_id, request=payload)
    await db.commit()
    return result


@router.post(
    "/purchase-requisitions/{purchase_requisition_id}/convert",
    response_model=list[CreatePurchaseOrderResult],
    status_code=201,
    operation_id="convertPurchaseRequisitionToOrders",
    summary="Convert a requisition into one or more purchase orders",
    description=(
        "Groups requisition lines by best-scored supplier (one PO per supplier), rounds "
        "each line to that supplier's MOQ, prices via the GST engine, and marks the "
        "requisition CONVERTED."
    ),
)
async def convert_purchase_requisition_endpoint(
    purchase_requisition_id: uuid.UUID,
    order_date: date | None = Query(default=None),  # noqa: B008
    user: User = Depends(require_perm("po.create")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> list[CreatePurchaseOrderResult]:
    result = await create_purchase_orders_from_requisition(
        db,
        org_id=user.org_id,
        purchase_requisition_id=purchase_requisition_id,
        order_date=order_date,
    )
    await db.commit()
    return result


class RejectPurchaseOrderRequest(BaseModel):
    reason: str | None = None


@router.post(
    "/purchase-orders/{purchase_order_id}/approve",
    response_model=PurchaseOrderStatusResult,
    operation_id="approvePurchaseOrder",
    summary="Sync a PO's status once its approval chain clears",
    description=(
        "Called by WF-03 once the approval chain auto-approves or every level decides "
        "APPROVE - approvals.py never touches the entity it approves, so this is the "
        "explicit sync step. Fires purchase_order.approved so WF-04 can send it."
    ),
)
async def approve_purchase_order_endpoint(
    purchase_order_id: uuid.UUID,
    user: User = Depends(require_perm("approval.manage")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> PurchaseOrderStatusResult:
    result = await mark_purchase_order_approved(
        db, org_id=user.org_id, purchase_order_id=purchase_order_id, actor_id=user.id
    )
    await db.commit()
    return result


@router.post(
    "/purchase-orders/{purchase_order_id}/reject",
    response_model=PurchaseOrderStatusResult,
    operation_id="rejectPurchaseOrder",
    summary="Sync a PO's status once its approval chain is rejected",
    description="The reject counterpart to /approve - status becomes CANCELLED.",
)
async def reject_purchase_order_endpoint(
    purchase_order_id: uuid.UUID,
    payload: RejectPurchaseOrderRequest,
    user: User = Depends(require_perm("approval.manage")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> PurchaseOrderStatusResult:
    result = await mark_purchase_order_rejected(
        db,
        org_id=user.org_id,
        purchase_order_id=purchase_order_id,
        reason=payload.reason,
        actor_id=user.id,
    )
    await db.commit()
    return result


@router.post(
    "/purchase-orders/{purchase_order_id}/pdf",
    operation_id="generatePurchaseOrderPdf",
    summary="Render a PO to PDF, store it, and return the bytes",
    description=(
        "Renders the PO, uploads it to object storage, persists a documents row, and "
        "returns the raw PDF bytes in the response body so a caller (WF-04) can attach "
        "it directly to a supplier email without needing its own storage credentials."
    ),
)
async def generate_purchase_order_pdf_endpoint(
    purchase_order_id: uuid.UUID,
    user: User = Depends(require_perm("po.create")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    result = await generate_purchase_order_pdf(
        db, org_id=user.org_id, purchase_order_id=purchase_order_id
    )
    await db.commit()
    return Response(
        content=result.pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{result.po_number}.pdf"'},
    )


@router.post(
    "/purchase-orders/{purchase_order_id}/mark-sent",
    response_model=PurchaseOrderStatusResult,
    operation_id="markPurchaseOrderSent",
    summary="Mark an approved PO as sent to the supplier",
    description="WF-04's last step, once the supplier email has actually sent.",
)
async def mark_purchase_order_sent_endpoint(
    purchase_order_id: uuid.UUID,
    user: User = Depends(require_perm("po.create")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> PurchaseOrderStatusResult:
    result = await mark_purchase_order_sent(
        db, org_id=user.org_id, purchase_order_id=purchase_order_id, actor_id=user.id
    )
    await db.commit()
    return result
