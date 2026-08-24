"""AR/AP HTTP surface: wraps services/payments.py. Symmetric across
customer and supplier payments via `party_type`, same as the service layer.
"""

import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_perm
from app.core.exceptions import ValidationError
from app.db.models.org import User
from app.db.session import get_db
from app.services.payments import (
    PARTY_CUSTOMER,
    PARTY_SUPPLIER,
    AgingLine,
    AllocationLine,
    RecordPaymentResult,
    aging_report,
    outstanding_balance,
    record_payment,
)

router = APIRouter(tags=["payments"])

_VALID_PARTY_TYPES = (PARTY_CUSTOMER, PARTY_SUPPLIER)


def _validate_party_type_param(party_type: str) -> None:
    if party_type not in _VALID_PARTY_TYPES:
        raise ValidationError(f"party_type must be 'customer' or 'supplier', got {party_type!r}")


class RecordPaymentRequest(BaseModel):
    party_type: str
    party_id: uuid.UUID
    amount: Decimal
    payment_date: date
    method: str | None = None
    reference: str | None = None
    razorpay_payment_id: str | None = None
    razorpay_payment_link_id: str | None = None
    allocations: list[AllocationLine] | None = None


@router.post(
    "/payments",
    response_model=RecordPaymentResult,
    status_code=201,
    operation_id="recordPayment",
    summary="Record a customer or supplier payment",
    description=(
        "Allocates the payment across open invoices — explicit `allocations` (caller picks "
        "which invoice(s)) or, if omitted, auto-allocated oldest-due-first. Any amount "
        "beyond what open invoices can absorb comes back as `unapplied_amount`."
    ),
)
async def record_payment_endpoint(
    payload: RecordPaymentRequest,
    user: User = Depends(require_perm("payment.record")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> RecordPaymentResult:
    result = await record_payment(
        db,
        org_id=user.org_id,
        party_type=payload.party_type,
        party_id=payload.party_id,
        amount=payload.amount,
        payment_date=payload.payment_date,
        method=payload.method,
        reference=payload.reference,
        razorpay_payment_id=payload.razorpay_payment_id,
        razorpay_payment_link_id=payload.razorpay_payment_link_id,
        allocations=payload.allocations,
    )
    await db.commit()
    return result


@router.get(
    "/payments/outstanding-balance",
    response_model=Decimal,
    operation_id="getOutstandingBalance",
    summary="Get a customer's or supplier's total outstanding balance",
)
async def get_outstanding_balance_endpoint(
    party_type: str = Query(...),
    party_id: uuid.UUID = Query(...),  # noqa: B008
    user: User = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Decimal:
    _validate_party_type_param(party_type)
    return await outstanding_balance(
        db, org_id=user.org_id, party_type=party_type, party_id=party_id
    )


@router.get(
    "/payments/aging-report",
    response_model=list[AgingLine],
    operation_id="getAgingReport",
    summary="AR or AP aging report, org-wide or for one party",
    description=(
        "Every open invoice with an outstanding balance, bucketed into 0-30/31-60/61-90/90+ "
        "days overdue. Omit `party_id` for the org-wide report."
    ),
)
async def get_aging_report_endpoint(
    party_type: str = Query(...),
    party_id: uuid.UUID | None = Query(default=None),  # noqa: B008
    as_of: date | None = Query(default=None),  # noqa: B008
    user: User = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> list[AgingLine]:
    _validate_party_type_param(party_type)
    return await aging_report(
        db, org_id=user.org_id, party_type=party_type, party_id=party_id, as_of=as_of
    )
