import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models.org import Organization, Role, User, UserRole
from app.db.models.workflow import Approval
from app.db.session import AsyncSessionLocal
from app.services.approvals import (
    ROLE_MANAGER,
    ROLE_OWNER,
    create_approval_chain,
    decide_approval,
    delegate_approval,
    determine_approval_chain,
    escalate_overdue_approvals,
    get_approval_chain_status,
)

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Pure determine_approval_chain() table-driven tests
# ---------------------------------------------------------------------------


def test_small_amount_auto_approves():
    plan = determine_approval_chain(amount=Decimal("5000"))
    assert plan.auto_approved is True
    assert plan.levels == []


def test_amount_exactly_at_auto_approve_limit_still_auto_approves():
    plan = determine_approval_chain(amount=Decimal("10000"))
    assert plan.auto_approved is True


def test_amount_just_over_auto_approve_limit_requires_manager():
    plan = determine_approval_chain(amount=Decimal("10000.01"))
    assert plan.auto_approved is False
    assert [lv.role for lv in plan.levels] == [ROLE_MANAGER]
    assert plan.levels[0].level == 1


def test_amount_exactly_at_owner_threshold_requires_only_manager():
    plan = determine_approval_chain(amount=Decimal("100000"))
    assert [lv.role for lv in plan.levels] == [ROLE_MANAGER]


def test_amount_just_over_owner_threshold_requires_manager_then_owner():
    plan = determine_approval_chain(amount=Decimal("100000.01"))
    assert [(lv.level, lv.role) for lv in plan.levels] == [(1, ROLE_MANAGER), (2, ROLE_OWNER)]


def test_capital_category_requires_owner_even_at_tiny_amount():
    plan = determine_approval_chain(amount=Decimal("1"), category="capital")
    assert plan.auto_approved is False
    assert [lv.role for lv in plan.levels] == [ROLE_OWNER]
    assert any("capital" in r for r in plan.reasoning)


def test_non_capital_category_has_no_effect():
    plan = determine_approval_chain(amount=Decimal("1"), category="recurring")
    assert plan.auto_approved is True


def test_high_risk_supplier_requires_owner_even_at_tiny_amount():
    plan = determine_approval_chain(amount=Decimal("1"), supplier_risk_score=60)
    assert [lv.role for lv in plan.levels] == [ROLE_OWNER]


def test_risk_just_below_high_threshold_is_elevated_not_high():
    plan = determine_approval_chain(amount=Decimal("1"), supplier_risk_score=59)
    assert [lv.role for lv in plan.levels] == [ROLE_MANAGER]


def test_risk_exactly_at_elevated_threshold_requires_manager():
    plan = determine_approval_chain(amount=Decimal("1"), supplier_risk_score=20)
    assert [lv.role for lv in plan.levels] == [ROLE_MANAGER]


def test_risk_just_below_elevated_threshold_has_no_effect():
    plan = determine_approval_chain(amount=Decimal("1"), supplier_risk_score=19)
    assert plan.auto_approved is True


def test_risk_score_zero_has_no_effect():
    plan = determine_approval_chain(amount=Decimal("1"), supplier_risk_score=0)
    assert plan.auto_approved is True


def test_manager_amount_and_manager_risk_do_not_duplicate_the_level():
    plan = determine_approval_chain(amount=Decimal("50000"), supplier_risk_score=30)
    assert [lv.role for lv in plan.levels] == [ROLE_MANAGER]
    assert len(plan.reasoning) == 2


def test_owner_amount_and_high_risk_do_not_duplicate_the_level():
    plan = determine_approval_chain(amount=Decimal("200000"), supplier_risk_score=90)
    assert [(lv.level, lv.role) for lv in plan.levels] == [(1, ROLE_MANAGER), (2, ROLE_OWNER)]


def test_capital_plus_owner_amount_still_two_levels():
    plan = determine_approval_chain(amount=Decimal("200000"), category="capital")
    assert [(lv.level, lv.role) for lv in plan.levels] == [(1, ROLE_MANAGER), (2, ROLE_OWNER)]


def test_reasoning_empty_when_auto_approved():
    plan = determine_approval_chain(amount=Decimal("1"))
    assert plan.reasoning == []


# ---------------------------------------------------------------------------
# DB-backed integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def rig():
    async with AsyncSessionLocal() as session:
        org = Organization(name=f"test-approvals-{uuid.uuid4()}", state_code="36")
        session.add(org)
        await session.flush()

        # Role names are globally unique (see app/db/models/org.py) and
        # ROLE_MANAGER/ROLE_OWNER are fixed strings ("Manager"/"Owner") —
        # get-or-create rather than insert, same pattern as test_deps.py.
        manager_role = (
            await session.execute(select(Role).where(Role.name == ROLE_MANAGER))
        ).scalar_one_or_none()
        if manager_role is None:
            manager_role = Role(name=ROLE_MANAGER)
            session.add(manager_role)
        owner_role = (
            await session.execute(select(Role).where(Role.name == ROLE_OWNER))
        ).scalar_one_or_none()
        if owner_role is None:
            owner_role = Role(name=ROLE_OWNER)
            session.add(owner_role)
        await session.flush()

        owner_user = User(
            org_id=org.id, email=f"owner-{uuid.uuid4().hex[:8]}@test.com", full_name="Owner"
        )
        manager_user = User(
            org_id=org.id, email=f"manager-{uuid.uuid4().hex[:8]}@test.com", full_name="Manager"
        )
        session.add_all([owner_user, manager_user])
        await session.flush()

        session.add_all(
            [
                UserRole(user_id=owner_user.id, role_id=owner_role.id),
                UserRole(user_id=manager_user.id, role_id=manager_role.id),
            ]
        )
        await session.commit()

        ids = {
            "org_id": org.id,
            "owner_role_id": owner_role.id,
            "manager_role_id": manager_role.id,
            "owner_user_id": owner_user.id,
            "manager_user_id": manager_user.id,
        }

    yield ids

    async with AsyncSessionLocal() as session:
        await session.execute(delete(Approval).where(Approval.org_id == ids["org_id"]))
        await session.execute(
            delete(UserRole).where(
                UserRole.user_id.in_([ids["owner_user_id"], ids["manager_user_id"]])
            )
        )
        await session.execute(
            delete(User).where(User.id.in_([ids["owner_user_id"], ids["manager_user_id"]]))
        )
        # Role rows are global (shared with other tests/seed data) — not
        # deleted here, same convention as tests/test_deps.py.
        await session.execute(delete(Organization).where(Organization.id == ids["org_id"]))
        await session.commit()


@pytest.mark.asyncio
async def test_create_approval_chain_auto_approves_small_amount(rig):
    async with AsyncSessionLocal() as session:
        result = await create_approval_chain(
            session,
            org_id=rig["org_id"],
            entity_type="purchase_order",
            entity_id=uuid.uuid4(),
            amount=Decimal("500"),
        )
    assert result.auto_approved is True
    assert result.levels == []


@pytest.mark.asyncio
async def test_create_approval_chain_persists_levels(rig):
    entity_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        result = await create_approval_chain(
            session,
            org_id=rig["org_id"],
            entity_type="purchase_order",
            entity_id=entity_id,
            amount=Decimal("200000"),
            now=NOW,
        )
        await session.commit()

    assert result.auto_approved is False
    assert [lv.role for lv in result.levels] == [ROLE_MANAGER, ROLE_OWNER]
    assert result.levels[0].sla_due_at == NOW + timedelta(hours=48)
    assert result.levels[1].sla_due_at == NOW + timedelta(hours=96)

    async with AsyncSessionLocal() as session:
        status = await get_approval_chain_status(
            session, org_id=rig["org_id"], entity_type="purchase_order", entity_id=entity_id
        )
    assert status.overall_status == "pending"
    assert len(status.levels) == 2


@pytest.mark.asyncio
async def test_create_approval_chain_duplicate_rejected(rig):
    entity_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        await create_approval_chain(
            session,
            org_id=rig["org_id"],
            entity_type="purchase_order",
            entity_id=entity_id,
            amount=Decimal("50000"),
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        with pytest.raises(ConflictError):
            await create_approval_chain(
                session,
                org_id=rig["org_id"],
                entity_type="purchase_order",
                entity_id=entity_id,
                amount=Decimal("50000"),
            )


@pytest.mark.asyncio
async def test_decide_approval_approve_single_level(rig):
    entity_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        result = await create_approval_chain(
            session,
            org_id=rig["org_id"],
            entity_type="purchase_order",
            entity_id=entity_id,
            amount=Decimal("50000"),
        )
        await session.commit()
        approval_id = result.levels[0].approval_id

    async with AsyncSessionLocal() as session:
        decided = await decide_approval(
            session,
            org_id=rig["org_id"],
            approval_id=approval_id,
            approver_id=rig["manager_user_id"],
            decision="approve",
            comment="looks fine",
        )
        await session.commit()

    assert decided.status == "approved"
    assert decided.approver_id == rig["manager_user_id"]
    assert "looks fine" in decided.comment

    async with AsyncSessionLocal() as session:
        status = await get_approval_chain_status(
            session, org_id=rig["org_id"], entity_type="purchase_order", entity_id=entity_id
        )
    assert status.overall_status == "approved"


@pytest.mark.asyncio
async def test_decide_approval_level_2_blocked_until_level_1_approved(rig):
    entity_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        result = await create_approval_chain(
            session,
            org_id=rig["org_id"],
            entity_type="purchase_order",
            entity_id=entity_id,
            amount=Decimal("200000"),
        )
        await session.commit()
        level_2_id = result.levels[1].approval_id

    async with AsyncSessionLocal() as session:
        with pytest.raises(ConflictError):
            await decide_approval(
                session,
                org_id=rig["org_id"],
                approval_id=level_2_id,
                approver_id=rig["owner_user_id"],
                decision="approve",
            )


@pytest.mark.asyncio
async def test_decide_approval_level_2_succeeds_after_level_1_approved(rig):
    entity_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        result = await create_approval_chain(
            session,
            org_id=rig["org_id"],
            entity_type="purchase_order",
            entity_id=entity_id,
            amount=Decimal("200000"),
        )
        await session.commit()
        level_1_id, level_2_id = result.levels[0].approval_id, result.levels[1].approval_id

    async with AsyncSessionLocal() as session:
        await decide_approval(
            session,
            org_id=rig["org_id"],
            approval_id=level_1_id,
            approver_id=rig["manager_user_id"],
            decision="approve",
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        decided = await decide_approval(
            session,
            org_id=rig["org_id"],
            approval_id=level_2_id,
            approver_id=rig["owner_user_id"],
            decision="approve",
        )
        await session.commit()
    assert decided.status == "approved"


@pytest.mark.asyncio
async def test_decide_approval_reject_cascades_to_other_open_levels(rig):
    entity_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        result = await create_approval_chain(
            session,
            org_id=rig["org_id"],
            entity_type="purchase_order",
            entity_id=entity_id,
            amount=Decimal("200000"),
        )
        await session.commit()
        level_1_id, level_2_id = result.levels[0].approval_id, result.levels[1].approval_id

    async with AsyncSessionLocal() as session:
        await decide_approval(
            session,
            org_id=rig["org_id"],
            approval_id=level_1_id,
            approver_id=rig["manager_user_id"],
            decision="reject",
            comment="bad deal",
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        level_2 = await session.get(Approval, level_2_id)
        assert level_2.status == "rejected"
        assert "auto-rejected" in level_2.comment

        status = await get_approval_chain_status(
            session, org_id=rig["org_id"], entity_type="purchase_order", entity_id=entity_id
        )
    assert status.overall_status == "rejected"


@pytest.mark.asyncio
async def test_decide_approval_already_decided_rejected(rig):
    entity_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        result = await create_approval_chain(
            session,
            org_id=rig["org_id"],
            entity_type="purchase_order",
            entity_id=entity_id,
            amount=Decimal("50000"),
        )
        await session.commit()
        approval_id = result.levels[0].approval_id

    async with AsyncSessionLocal() as session:
        await decide_approval(
            session,
            org_id=rig["org_id"],
            approval_id=approval_id,
            approver_id=rig["manager_user_id"],
            decision="approve",
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        with pytest.raises(ConflictError):
            await decide_approval(
                session,
                org_id=rig["org_id"],
                approval_id=approval_id,
                approver_id=rig["manager_user_id"],
                decision="approve",
            )


@pytest.mark.asyncio
async def test_decide_approval_invalid_decision_rejected(rig):
    entity_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        result = await create_approval_chain(
            session,
            org_id=rig["org_id"],
            entity_type="purchase_order",
            entity_id=entity_id,
            amount=Decimal("50000"),
        )
        await session.commit()
        approval_id = result.levels[0].approval_id

    async with AsyncSessionLocal() as session:
        with pytest.raises(ValidationError):
            await decide_approval(
                session,
                org_id=rig["org_id"],
                approval_id=approval_id,
                approver_id=rig["manager_user_id"],
                decision="maybe",
            )


@pytest.mark.asyncio
async def test_decide_approval_unknown_approval_rejected(rig):
    async with AsyncSessionLocal() as session:
        with pytest.raises(NotFoundError):
            await decide_approval(
                session,
                org_id=rig["org_id"],
                approval_id=uuid.uuid4(),
                approver_id=rig["manager_user_id"],
                decision="approve",
            )


@pytest.mark.asyncio
async def test_delegate_approval_creates_new_pending_row_for_new_approver(rig):
    entity_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        result = await create_approval_chain(
            session,
            org_id=rig["org_id"],
            entity_type="purchase_order",
            entity_id=entity_id,
            amount=Decimal("50000"),
            now=NOW,
        )
        await session.commit()
        original_id = result.levels[0].approval_id

    async with AsyncSessionLocal() as session:
        new_approval = await delegate_approval(
            session,
            org_id=rig["org_id"],
            approval_id=original_id,
            to_approver_id=rig["owner_user_id"],
            comment="I'm OOO",
            now=NOW + timedelta(hours=1),
        )
        await session.commit()
        new_id = new_approval.id

    assert new_approval.status == "pending"
    assert new_approval.approver_id == rig["owner_user_id"]
    assert new_approval.level == 1

    async with AsyncSessionLocal() as session:
        original = await session.get(Approval, original_id)
        assert original.status == "delegated"
        assert "delegated to" in original.comment

        status = await get_approval_chain_status(
            session, org_id=rig["org_id"], entity_type="purchase_order", entity_id=entity_id
        )
    # the delegated (superseded) row is excluded from chain status
    assert len(status.levels) == 1
    assert status.levels[0].approval_id == new_id


@pytest.mark.asyncio
async def test_escalate_overdue_approvals_flips_status_and_reassigns_to_owner(rig):
    entity_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        result = await create_approval_chain(
            session,
            org_id=rig["org_id"],
            entity_type="purchase_order",
            entity_id=entity_id,
            amount=Decimal("50000"),
            sla_hours_per_level=1,
            now=NOW,
        )
        await session.commit()
        approval_id = result.levels[0].approval_id

    async with AsyncSessionLocal() as session:
        escalated = await escalate_overdue_approvals(
            session, org_id=rig["org_id"], as_of=NOW + timedelta(hours=2)
        )
        await session.commit()

    assert [a.id for a in escalated] == [approval_id]

    async with AsyncSessionLocal() as session:
        row = await session.get(Approval, approval_id)
        assert row.status == "escalated"
        assert row.approver_id == rig["owner_user_id"]
        assert "escalated" in row.comment.lower()


@pytest.mark.asyncio
async def test_escalate_overdue_approvals_ignores_approvals_not_yet_due(rig):
    entity_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        await create_approval_chain(
            session,
            org_id=rig["org_id"],
            entity_type="purchase_order",
            entity_id=entity_id,
            amount=Decimal("50000"),
            sla_hours_per_level=48,
            now=NOW,
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        escalated = await escalate_overdue_approvals(
            session, org_id=rig["org_id"], as_of=NOW + timedelta(hours=1)
        )
    assert escalated == []


@pytest.mark.asyncio
async def test_get_approval_chain_status_no_chain(rig):
    async with AsyncSessionLocal() as session:
        status = await get_approval_chain_status(
            session,
            org_id=rig["org_id"],
            entity_type="purchase_order",
            entity_id=uuid.uuid4(),
        )
    assert status.overall_status == "no_chain"
    assert status.levels == []
