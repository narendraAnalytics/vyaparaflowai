"""The approval chain: a threshold rules engine (amount, category, supplier
risk) that decides HOW MANY levels an entity needs and WHO (which role)
signs off at each, plus the state machine that drives one entity's chain
through pending -> approved/rejected/delegated/escalated.

This is polymorphic on purpose, matching `approvals.entity_type`/
`entity_id` (see `app/db/models/workflow.py`): a purchase order, a
purchase requisition, a supplier invoice, a payment run — anything that
needs a human sign-off before something irreversible happens (the
roadmap's own guardrail: "AI never takes an irreversible action alone —
a deterministic rule gates it"). Nothing here interprets `entity_type` —
that's the caller's job (procurement.py, a future payments run, etc.).

Split like every other services/ module: `determine_approval_chain()` is a
pure function (no DB access, table-driven-tested) that turns
(amount, category, supplier_risk_score) into an `ApprovalChainPlan`.
`create_approval_chain()`, `decide_approval()`, `delegate_approval()`,
`escalate_overdue_approvals()` and `get_approval_chain_status()` are thin
async wrappers. Nothing here commits — the caller owns the transaction.

**Threshold rules** (roadmap 2.11's "amount, category, supplier risk"):
  amount > Rs.10,000            -> Manager level required
  amount > Rs.1,00,000          -> Owner level required (in addition)
  category == "capital"         -> Owner level required, any amount
  supplier_risk_score >= 60      -> Owner level required (matches
                                    matching.py's own BLOCK threshold —
                                    the same score, the same "this is
                                    serious" line)
  supplier_risk_score >= 20      -> Manager level required (matches
                                    matching.py's REVIEW threshold)
  Below every threshold: `auto_approved=True`, no `Approval` rows
  persisted — nothing to route, nothing for a human to act on. Every
  triggered rule adds a plain-English line to `ApprovalChainPlan.
  reasoning`, the same "show your work" convention as procurement.py's
  `SupplierScore.reasoning` and matching.py's `reason_codes`.

**No `required_role` column on `approvals`**: role information for each
level is returned in `CreateApprovalChainResult` (structured, for an
immediate caller) and also written into the row's `comment` (human-
readable, for an approvals-inbox UI reading straight from the table) —
deliberately not a new schema column, since nothing in Phase 2 needs to
*query* by role yet (that's the dashboard/Phase 6 or the n8n approval-
routing workflow, WF-03, which gets it from this service's return value,
not a raw SQL filter). Same "don't half-build ahead of the consumer"
call procurement.py made about gating PO creation on approval.

**RBAC is NOT enforced here.** Whether the `approver_id` passed to
`decide_approval()` actually holds the required role is an API-layer
concern (`app/core/deps.py`'s `require_role`/`require_perm`, the same
gate every other endpoint uses) — services/ modules never check
permissions themselves anywhere else in this codebase, and this one
doesn't start.

**Rejection cascades**: rejecting one level rejects every other still-
open (PENDING/ESCALATED) level of the same entity's chain — there's no
reason to leave a level-2 approval sitting open once level 1 has killed
the whole request. Cascaded rows get `approver_id=None` (nobody actually
decided them) and a comment noting the cascade, so the audit trail still
shows what happened and why.

**Delegation creates a new row rather than mutating the existing one**:
the original row flips to DELEGATED (terminal, permanent audit record of
who handed it off and to whom) and a fresh PENDING row is created at the
same level for the new approver. `approvals` has no self-referential FK
to link the two, so the comment on each carries the cross-reference —
simple, and sufficient for the volume this system will ever see.

**SLA escalation**: `escalate_overdue_approvals()` finds every PENDING
approval whose `sla_due_at` has passed and flips it to ESCALATED,
best-effort reassigning `approver_id` to an active Owner-role user for
the org if one exists (falls back to leaving it unset — a human routes
it manually). Notifying anyone about the escalation is Phase 3's job
(n8n's WF-99-style alerting) — this function's contract ends at "the
row now correctly reflects that it's overdue."
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models.enums import ApprovalStatus
from app.db.models.org import Role, User, UserRole
from app.db.models.workflow import Approval

ROLE_MANAGER = "Manager"
ROLE_OWNER = "Owner"

_AUTO_APPROVE_LIMIT = Decimal("10000")
_OWNER_REQUIRED_AMOUNT = Decimal("100000")
_CAPITAL_CATEGORY = "capital"
_HIGH_RISK_THRESHOLD = 60
_ELEVATED_RISK_THRESHOLD = 20

_DEFAULT_SLA_HOURS_PER_LEVEL = 48

DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"

_OPEN_STATUSES = (ApprovalStatus.PENDING, ApprovalStatus.ESCALATED)


class ApprovalLevelRule(BaseModel):
    level: int
    role: str


class ApprovalChainPlan(BaseModel):
    levels: list[ApprovalLevelRule]
    auto_approved: bool
    reasoning: list[str]


class CreatedApprovalLevel(BaseModel):
    approval_id: uuid.UUID
    level: int
    role: str
    sla_due_at: datetime | None


class CreateApprovalChainResult(BaseModel):
    entity_type: str
    entity_id: uuid.UUID
    auto_approved: bool
    levels: list[CreatedApprovalLevel]
    reasoning: list[str]


class ApprovalLevelStatus(BaseModel):
    approval_id: uuid.UUID
    level: int
    status: str
    approver_id: uuid.UUID | None
    sla_due_at: datetime | None


class ApprovalChainStatus(BaseModel):
    entity_type: str
    entity_id: uuid.UUID
    overall_status: str  # "no_chain" | "pending" | "approved" | "rejected"
    levels: list[ApprovalLevelStatus]


def determine_approval_chain(
    *,
    amount: Decimal,
    category: str | None = None,
    supplier_risk_score: int | None = None,
) -> ApprovalChainPlan:
    """Pure function — no DB access. See module docstring for the
    threshold table this implements.
    """
    reasoning: list[str] = []
    requires_manager = False
    requires_owner = False

    if amount > _OWNER_REQUIRED_AMOUNT:
        requires_manager = True
        requires_owner = True
        reasoning.append(
            f"amount Rs.{amount} exceeds the owner threshold Rs.{_OWNER_REQUIRED_AMOUNT}"
        )
    elif amount > _AUTO_APPROVE_LIMIT:
        requires_manager = True
        reasoning.append(
            f"amount Rs.{amount} exceeds the auto-approve threshold Rs.{_AUTO_APPROVE_LIMIT}"
        )

    if category == _CAPITAL_CATEGORY:
        requires_owner = True
        reasoning.append(f"category '{category}' always requires owner approval")

    if supplier_risk_score is not None:
        if supplier_risk_score >= _HIGH_RISK_THRESHOLD:
            requires_owner = True
            reasoning.append(
                f"supplier risk score {supplier_risk_score} >= {_HIGH_RISK_THRESHOLD} (high risk)"
            )
        elif supplier_risk_score >= _ELEVATED_RISK_THRESHOLD:
            requires_manager = True
            reasoning.append(
                f"supplier risk score {supplier_risk_score} >= {_ELEVATED_RISK_THRESHOLD} "
                "(elevated risk)"
            )

    levels: list[ApprovalLevelRule] = []
    if requires_manager:
        levels.append(ApprovalLevelRule(level=1, role=ROLE_MANAGER))
    if requires_owner:
        levels.append(ApprovalLevelRule(level=len(levels) + 1, role=ROLE_OWNER))

    return ApprovalChainPlan(levels=levels, auto_approved=not levels, reasoning=reasoning)


async def _find_role_user_id(
    session: AsyncSession, *, org_id: uuid.UUID, role_name: str
) -> uuid.UUID | None:
    row = (
        await session.execute(
            select(User.id)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(User.org_id == org_id, User.is_active, Role.name == role_name)
            .limit(1)
        )
    ).first()
    return row[0] if row is not None else None


async def create_approval_chain(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    amount: Decimal,
    category: str | None = None,
    supplier_risk_score: int | None = None,
    sla_hours_per_level: int = _DEFAULT_SLA_HOURS_PER_LEVEL,
    now: datetime | None = None,
) -> CreateApprovalChainResult:
    existing = (
        await session.execute(
            select(Approval.id).where(
                Approval.org_id == org_id,
                Approval.entity_type == entity_type,
                Approval.entity_id == entity_id,
            )
        )
    ).first()
    if existing is not None:
        raise ConflictError(f"an approval chain already exists for {entity_type} {entity_id}")

    plan = determine_approval_chain(
        amount=amount, category=category, supplier_risk_score=supplier_risk_score
    )
    if plan.auto_approved:
        return CreateApprovalChainResult(
            entity_type=entity_type,
            entity_id=entity_id,
            auto_approved=True,
            levels=[],
            reasoning=plan.reasoning,
        )

    now = now or datetime.now(UTC)
    created_levels: list[CreatedApprovalLevel] = []
    for rule in plan.levels:
        sla_due_at = now + timedelta(hours=sla_hours_per_level * rule.level)
        approval = Approval(
            org_id=org_id,
            entity_type=entity_type,
            entity_id=entity_id,
            level=rule.level,
            status=ApprovalStatus.PENDING,
            sla_due_at=sla_due_at,
            comment=f"requires {rule.role} approval — {'; '.join(plan.reasoning)}",
        )
        session.add(approval)
        await session.flush()
        created_levels.append(
            CreatedApprovalLevel(
                approval_id=approval.id, level=rule.level, role=rule.role, sla_due_at=sla_due_at
            )
        )

    return CreateApprovalChainResult(
        entity_type=entity_type,
        entity_id=entity_id,
        auto_approved=False,
        levels=created_levels,
        reasoning=plan.reasoning,
    )


async def decide_approval(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    approval_id: uuid.UUID,
    approver_id: uuid.UUID,
    decision: str,
    comment: str | None = None,
    now: datetime | None = None,
) -> Approval:
    if decision not in (DECISION_APPROVE, DECISION_REJECT):
        raise ValidationError(f"decision must be 'approve' or 'reject', got {decision!r}")

    approval = await session.get(Approval, approval_id)
    if approval is None or approval.org_id != org_id:
        raise NotFoundError(f"approval {approval_id} not found")
    if approval.status not in _OPEN_STATUSES:
        raise ConflictError(
            f"approval {approval_id} is already {approval.status} and cannot be decided again"
        )

    now = now or datetime.now(UTC)

    if decision == DECISION_APPROVE:
        prior_open = (
            await session.execute(
                select(Approval.id).where(
                    Approval.org_id == org_id,
                    Approval.entity_type == approval.entity_type,
                    Approval.entity_id == approval.entity_id,
                    Approval.level < approval.level,
                    Approval.status != ApprovalStatus.APPROVED,
                )
            )
        ).first()
        if prior_open is not None:
            raise ConflictError(
                f"approval {approval_id} is level {approval.level} — a prior level for "
                f"{approval.entity_type} {approval.entity_id} has not been approved yet"
            )
        approval.status = ApprovalStatus.APPROVED
        approval.approver_id = approver_id
        approval.decided_at = now
        approval.comment = _append_comment(approval.comment, comment)
        await session.flush()
        return approval

    # reject — cascade to every other still-open level of this entity's chain
    approval.status = ApprovalStatus.REJECTED
    approval.approver_id = approver_id
    approval.decided_at = now
    approval.comment = _append_comment(approval.comment, comment)

    other_open = (
        (
            await session.execute(
                select(Approval).where(
                    Approval.org_id == org_id,
                    Approval.entity_type == approval.entity_type,
                    Approval.entity_id == approval.entity_id,
                    Approval.id != approval.id,
                    Approval.status.in_([s.value for s in _OPEN_STATUSES]),
                )
            )
        )
        .scalars()
        .all()
    )
    for other in other_open:
        other.status = ApprovalStatus.REJECTED
        other.decided_at = now
        other.comment = _append_comment(
            other.comment, "auto-rejected: an earlier approval level was rejected"
        )

    await session.flush()
    return approval


async def delegate_approval(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    approval_id: uuid.UUID,
    to_approver_id: uuid.UUID,
    comment: str | None = None,
    now: datetime | None = None,
) -> Approval:
    approval = await session.get(Approval, approval_id)
    if approval is None or approval.org_id != org_id:
        raise NotFoundError(f"approval {approval_id} not found")
    if approval.status not in _OPEN_STATUSES:
        raise ConflictError(
            f"approval {approval_id} is already {approval.status} and cannot be delegated"
        )

    now = now or datetime.now(UTC)
    sla_window = (
        approval.sla_due_at - approval.created_at
        if approval.sla_due_at is not None
        else timedelta(hours=_DEFAULT_SLA_HOURS_PER_LEVEL)
    )

    delegation_note = f"delegated to {to_approver_id}: {comment or ''}".strip(": ")
    approval.status = ApprovalStatus.DELEGATED
    approval.decided_at = now
    approval.comment = _append_comment(approval.comment, delegation_note)

    new_approval = Approval(
        org_id=org_id,
        entity_type=approval.entity_type,
        entity_id=approval.entity_id,
        level=approval.level,
        approver_id=to_approver_id,
        status=ApprovalStatus.PENDING,
        sla_due_at=now + sla_window,
        comment=f"delegated from approval {approval.id}",
    )
    session.add(new_approval)
    await session.flush()
    return new_approval


async def escalate_overdue_approvals(
    session: AsyncSession, *, org_id: uuid.UUID, as_of: datetime | None = None
) -> list[Approval]:
    as_of = as_of or datetime.now(UTC)
    owner_id = await _find_role_user_id(session, org_id=org_id, role_name=ROLE_OWNER)

    overdue = (
        (
            await session.execute(
                select(Approval).where(
                    Approval.org_id == org_id,
                    Approval.status == ApprovalStatus.PENDING,
                    Approval.sla_due_at.is_not(None),
                    Approval.sla_due_at < as_of,
                )
            )
        )
        .scalars()
        .all()
    )
    for approval in overdue:
        approval.status = ApprovalStatus.ESCALATED
        note = (
            f"SLA breached, escalated to owner {owner_id}"
            if owner_id is not None
            else "SLA breached, escalated (no owner-role user found to reassign to)"
        )
        approval.comment = _append_comment(approval.comment, note)
        if owner_id is not None:
            approval.approver_id = owner_id

    await session.flush()
    return list(overdue)


async def get_approval_chain_status(
    session: AsyncSession, *, org_id: uuid.UUID, entity_type: str, entity_id: uuid.UUID
) -> ApprovalChainStatus:
    rows = (
        (
            await session.execute(
                select(Approval)
                .where(
                    Approval.org_id == org_id,
                    Approval.entity_type == entity_type,
                    Approval.entity_id == entity_id,
                    Approval.status != ApprovalStatus.DELEGATED,
                )
                .order_by(Approval.level)
            )
        )
        .scalars()
        .all()
    )

    if not rows:
        return ApprovalChainStatus(
            entity_type=entity_type, entity_id=entity_id, overall_status="no_chain", levels=[]
        )

    if any(row.status == ApprovalStatus.REJECTED for row in rows):
        overall = "rejected"
    elif all(row.status == ApprovalStatus.APPROVED for row in rows):
        overall = "approved"
    else:
        overall = "pending"

    return ApprovalChainStatus(
        entity_type=entity_type,
        entity_id=entity_id,
        overall_status=overall,
        levels=[
            ApprovalLevelStatus(
                approval_id=row.id,
                level=row.level,
                status=row.status,
                approver_id=row.approver_id,
                sla_due_at=row.sla_due_at,
            )
            for row in rows
        ],
    )


def _append_comment(existing: str | None, addition: str | None) -> str | None:
    if not addition:
        return existing
    return f"{existing} | {addition}" if existing else addition
