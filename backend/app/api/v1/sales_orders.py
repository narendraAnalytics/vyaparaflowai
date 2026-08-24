"""Order-to-cash HTTP surface: wraps services/sales.py. Request/response
bodies reuse that module's own Pydantic models directly (CreateSalesOrder
Request/Result, CreateCounterSaleRequest/Result) rather than duplicating
them into app/schemas/ — those models already ARE the wire contract, the
same way master-data CRUD's app/schemas/master_data.py models are.
"""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_perm
from app.db.models.org import User
from app.db.session import get_db
from app.services.sales import (
    CreateCounterSaleRequest,
    CreateCounterSaleResult,
    CreateSalesOrderRequest,
    CreateSalesOrderResult,
    confirm_sales_order,
    create_counter_sale,
    create_sales_order,
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
