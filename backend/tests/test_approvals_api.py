import uuid
from datetime import date

import pytest
from sqlalchemy import delete, select

from app.core.security import hash_secret
from app.db.models.org import Organization, Role, User, UserRole
from app.db.models.workflow import Approval
from app.db.session import AsyncSessionLocal

TELANGANA = "36"
PASSWORD = "TestPassw0rd!"
TODAY = date(2026, 8, 24)


async def _login(client, email: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def rig():
    async with AsyncSessionLocal() as session:
        org = Organization(name=f"test-approvals-api-{uuid.uuid4()}", state_code=TELANGANA)
        session.add(org)
        await session.flush()

        manager_role = (
            await session.execute(select(Role).where(Role.name == "Manager"))
        ).scalar_one_or_none()
        if manager_role is None:
            manager_role = Role(name="Manager")
            session.add(manager_role)
        owner_role = (
            await session.execute(select(Role).where(Role.name == "Owner"))
        ).scalar_one_or_none()
        if owner_role is None:
            owner_role = Role(name="Owner")
            session.add(owner_role)
        sales_role = (
            await session.execute(select(Role).where(Role.name == "Sales"))
        ).scalar_one_or_none()
        if sales_role is None:
            sales_role = Role(name="Sales")
            session.add(sales_role)
        await session.flush()

        manager_user = User(
            org_id=org.id,
            email=f"mgr-{uuid.uuid4().hex[:8]}@test.com",
            full_name="Manager",
            hashed_password=hash_secret(PASSWORD),
        )
        owner_user = User(
            org_id=org.id,
            email=f"owner-{uuid.uuid4().hex[:8]}@test.com",
            full_name="Owner",
            hashed_password=hash_secret(PASSWORD),
        )
        sales_user = User(
            org_id=org.id,
            email=f"sales-{uuid.uuid4().hex[:8]}@test.com",
            full_name="Sales",
            hashed_password=hash_secret(PASSWORD),
        )
        session.add_all([manager_user, owner_user, sales_user])
        await session.flush()
        session.add_all(
            [
                UserRole(user_id=manager_user.id, role_id=manager_role.id),
                UserRole(user_id=owner_user.id, role_id=owner_role.id),
                UserRole(user_id=sales_user.id, role_id=sales_role.id),
            ]
        )
        await session.commit()

        ids = {
            "org_id": org.id,
            "manager_id": manager_user.id,
            "manager_email": manager_user.email,
            "owner_id": owner_user.id,
            "owner_email": owner_user.email,
            "sales_email": sales_user.email,
        }

    yield ids

    async with AsyncSessionLocal() as session:
        await session.execute(delete(Approval).where(Approval.org_id == ids["org_id"]))
        user_id_list = (
            (await session.execute(select(User.id).where(User.org_id == ids["org_id"])))
            .scalars()
            .all()
        )
        if user_id_list:
            await session.execute(delete(UserRole).where(UserRole.user_id.in_(user_id_list)))
        await session.execute(delete(User).where(User.org_id == ids["org_id"]))
        await session.execute(delete(Organization).where(Organization.id == ids["org_id"]))
        await session.commit()


@pytest.mark.asyncio
async def test_create_approval_chain_denied_for_sales_role(client, rig):
    token = await _login(client, rig["sales_email"])
    response = await client.post(
        "/api/v1/approvals",
        headers=_auth(token),
        json={"entity_type": "purchase_order", "entity_id": str(uuid.uuid4()), "amount": "50000"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_create_chain_decide_and_check_status(client, rig):
    token = await _login(client, rig["manager_email"])
    entity_id = str(uuid.uuid4())

    created = await client.post(
        "/api/v1/approvals",
        headers=_auth(token),
        json={"entity_type": "purchase_order", "entity_id": entity_id, "amount": "50000"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["auto_approved"] is False
    [level] = body["levels"]
    assert level["role"] == "Manager"
    approval_id = level["approval_id"]

    decided = await client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        headers=_auth(token),
        json={"decision": "approve", "comment": "looks fine"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved"

    status = await client.get(
        f"/api/v1/approvals/purchase_order/{entity_id}/status", headers=_auth(token)
    )
    assert status.status_code == 200, status.text
    assert status.json()["overall_status"] == "approved"


@pytest.mark.asyncio
async def test_delegate_approval_creates_new_pending_row(client, rig):
    token = await _login(client, rig["manager_email"])
    entity_id = str(uuid.uuid4())

    created = await client.post(
        "/api/v1/approvals",
        headers=_auth(token),
        json={"entity_type": "purchase_order", "entity_id": entity_id, "amount": "50000"},
    )
    approval_id = created.json()["levels"][0]["approval_id"]

    delegated = await client.post(
        f"/api/v1/approvals/{approval_id}/delegate",
        headers=_auth(token),
        json={"to_approver_id": str(rig["owner_id"]), "comment": "OOO"},
    )
    assert delegated.status_code == 200, delegated.text
    body = delegated.json()
    assert body["status"] == "pending"
    assert body["approver_id"] == str(rig["owner_id"])


@pytest.mark.asyncio
async def test_escalate_overdue_approvals(client, rig):
    token = await _login(client, rig["manager_email"])
    entity_id = str(uuid.uuid4())

    created = await client.post(
        "/api/v1/approvals",
        headers=_auth(token),
        json={"entity_type": "purchase_order", "entity_id": entity_id, "amount": "50000"},
    )
    approval_id = created.json()["levels"][0]["approval_id"]

    async with AsyncSessionLocal() as session:
        row = await session.get(Approval, uuid.UUID(approval_id))
        row.sla_due_at = row.sla_due_at.replace(year=2020)
        await session.commit()

    escalated = await client.post("/api/v1/approvals/escalate", headers=_auth(token))
    assert escalated.status_code == 200, escalated.text
    [row] = [a for a in escalated.json() if a["id"] == approval_id]
    assert row["status"] == "escalated"
    assert row["approver_id"] == str(rig["owner_id"])
