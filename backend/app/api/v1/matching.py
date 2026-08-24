"""Three-way match HTTP surface: wraps services/matching.py."""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_perm
from app.db.models.org import User
from app.db.session import get_db
from app.services.matching import (
    DEFAULT_TOLERANCES,
    MatchTolerances,
    RunThreeWayMatchResult,
    match_three_way,
)

router = APIRouter(prefix="/matching", tags=["matching"])


class ThreeWayMatchRequest(BaseModel):
    purchase_order_id: uuid.UUID
    goods_receipt_id: uuid.UUID
    supplier_invoice_id: uuid.UUID
    tolerances: MatchTolerances = DEFAULT_TOLERANCES


@router.post(
    "/three-way",
    response_model=RunThreeWayMatchResult,
    status_code=201,
    operation_id="runThreeWayMatch",
    summary="Run the three-way match for a PO/GRN/invoice triple",
    description=(
        "Compares a purchase order, one goods receipt raised against it, and a supplier "
        "invoice, line by line. Persists a ThreeWayMatchResult and updates the supplier "
        "invoice's status: AUTO_APPROVE -> matched, BLOCK -> blocked, REVIEW leaves it "
        "as-is pending a human look."
    ),
)
async def run_three_way_match_endpoint(
    payload: ThreeWayMatchRequest,
    user: User = Depends(require_perm("supplier_invoice.match")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> RunThreeWayMatchResult:
    result = await match_three_way(
        db,
        org_id=user.org_id,
        purchase_order_id=payload.purchase_order_id,
        goods_receipt_id=payload.goods_receipt_id,
        supplier_invoice_id=payload.supplier_invoice_id,
        tolerances=payload.tolerances,
    )
    await db.commit()
    return result
