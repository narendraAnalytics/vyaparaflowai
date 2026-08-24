"""Approval chain HTTP surface: wraps services/approvals.py. Polymorphic
on entity_type/entity_id, same as the underlying table — this router
never interprets what the entity actually is.
"""

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_perm
from app.db.models.org import User
from app.db.models.workflow import Approval
from app.db.session import get_db
from app.services.approvals import (
    ApprovalChainStatus,
    CreateApprovalChainResult,
    create_approval_chain,
    decide_approval,
    delegate_approval,
    escalate_overdue_approvals,
    get_approval_chain_status,
)

router = APIRouter(prefix="/approvals", tags=["approvals"])


class CreateApprovalChainRequest(BaseModel):
    entity_type: str
    entity_id: uuid.UUID
    amount: Decimal
    category: str | None = None
    supplier_risk_score: int | None = None


class DecideApprovalRequest(BaseModel):
    decision: str
    comment: str | None = None


class DelegateApprovalRequest(BaseModel):
    to_approver_id: uuid.UUID
    comment: str | None = None


class ApprovalOut(BaseModel):
    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    level: int
    approver_id: uuid.UUID | None
    status: str
    comment: str | None

    model_config = {"from_attributes": True}


@router.post(
    "",
    response_model=CreateApprovalChainResult,
    status_code=201,
    operation_id="createApprovalChain",
    summary="Create an approval chain for an entity",
    description=(
        "Runs the threshold rules engine (amount, category, supplier risk) and persists "
        "one Approval row per required level. `auto_approved=true` with an empty `levels` "
        "list means nothing crossed a threshold — there's nothing to route."
    ),
)
async def create_approval_chain_endpoint(
    payload: CreateApprovalChainRequest,
    user: User = Depends(require_perm("approval.manage")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> CreateApprovalChainResult:
    result = await create_approval_chain(
        db,
        org_id=user.org_id,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        amount=payload.amount,
        category=payload.category,
        supplier_risk_score=payload.supplier_risk_score,
    )
    await db.commit()
    return result


@router.post(
    "/{approval_id}/decide",
    response_model=ApprovalOut,
    operation_id="decideApproval",
    summary="Approve or reject one approval level",
    description=(
        "Approving level N is blocked until every level below it is APPROVED. Rejecting "
        "cascades: every other still-open level of the same entity's chain is auto-rejected."
    ),
)
async def decide_approval_endpoint(
    approval_id: uuid.UUID,
    payload: DecideApprovalRequest,
    user: User = Depends(require_perm("approval.manage")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Approval:
    approval = await decide_approval(
        db,
        org_id=user.org_id,
        approval_id=approval_id,
        approver_id=user.id,
        decision=payload.decision,
        comment=payload.comment,
    )
    await db.commit()
    return approval


@router.post(
    "/{approval_id}/delegate",
    response_model=ApprovalOut,
    operation_id="delegateApproval",
    summary="Delegate an approval to another approver",
    description=(
        "The original row flips to DELEGATED (a permanent audit record); a fresh PENDING "
        "row is created at the same level for the new approver, which this endpoint returns."
    ),
)
async def delegate_approval_endpoint(
    approval_id: uuid.UUID,
    payload: DelegateApprovalRequest,
    user: User = Depends(require_perm("approval.manage")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Approval:
    new_approval = await delegate_approval(
        db,
        org_id=user.org_id,
        approval_id=approval_id,
        to_approver_id=payload.to_approver_id,
        comment=payload.comment,
    )
    await db.commit()
    return new_approval


@router.post(
    "/escalate",
    response_model=list[ApprovalOut],
    operation_id="escalateOverdueApprovals",
    summary="Escalate every overdue pending approval",
    description=(
        "Flips every PENDING approval past its SLA to ESCALATED, best-effort reassigning "
        "it to an active Owner-role user for the org."
    ),
)
async def escalate_overdue_approvals_endpoint(
    user: User = Depends(require_perm("approval.manage")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> list[Approval]:
    escalated = await escalate_overdue_approvals(db, org_id=user.org_id)
    await db.commit()
    return escalated


@router.get(
    "/{entity_type}/{entity_id}/status",
    response_model=ApprovalChainStatus,
    operation_id="getApprovalChainStatus",
    summary="Get an entity's overall approval chain status",
)
async def get_approval_chain_status_endpoint(
    entity_type: str,
    entity_id: uuid.UUID,
    user: User = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> ApprovalChainStatus:
    return await get_approval_chain_status(
        db, org_id=user.org_id, entity_type=entity_type, entity_id=entity_id
    )
