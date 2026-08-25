import uuid

import pytest
from sqlalchemy import delete, select

from app.core.security import hash_secret
from app.db.models.org import Organization, Role, User, UserRole
from app.db.models.workflow import WorkflowEvent
from app.db.session import AsyncSessionLocal

TELANGANA = "36"
PASSWORD = "TestPassw0rd!"


async def _login(client, email: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def rig():
    async with AsyncSessionLocal() as session:
        org = Organization(name=f"test-workflow-events-{uuid.uuid4()}", state_code=TELANGANA)
        session.add(org)
        await session.flush()

        automation_role = (
            await session.execute(select(Role).where(Role.name == "Automation"))
        ).scalar_one_or_none()
        if automation_role is None:
            automation_role = Role(name="Automation")
            session.add(automation_role)
        sales_role = (
            await session.execute(select(Role).where(Role.name == "Sales"))
        ).scalar_one_or_none()
        if sales_role is None:
            sales_role = Role(name="Sales")
            session.add(sales_role)
        await session.flush()

        automation_user = User(
            org_id=org.id,
            email=f"automation-{uuid.uuid4().hex[:8]}@test.com",
            full_name="Automation",
            hashed_password=hash_secret(PASSWORD),
        )
        sales_user = User(
            org_id=org.id,
            email=f"sales-{uuid.uuid4().hex[:8]}@test.com",
            full_name="Sales",
            hashed_password=hash_secret(PASSWORD),
        )
        session.add_all([automation_user, sales_user])
        await session.flush()
        session.add_all(
            [
                UserRole(user_id=automation_user.id, role_id=automation_role.id),
                UserRole(user_id=sales_user.id, role_id=sales_role.id),
            ]
        )
        await session.commit()

        ids = {
            "org_id": org.id,
            "automation_email": automation_user.email,
            "sales_email": sales_user.email,
        }

    yield ids

    async with AsyncSessionLocal() as session:
        await session.execute(delete(WorkflowEvent).where(WorkflowEvent.org_id == ids["org_id"]))
        await session.execute(
            delete(UserRole).where(
                UserRole.user_id.in_(select(User.id).where(User.org_id == ids["org_id"]))
            )
        )
        await session.execute(delete(User).where(User.org_id == ids["org_id"]))
        await session.execute(delete(Organization).where(Organization.id == ids["org_id"]))
        await session.commit()


async def test_log_workflow_event_requires_auth(client, rig):
    response = await client.post(
        "/api/v1/workflow-events",
        json={"workflow_name": "WF-99 Global Error Handler", "status": "error"},
    )
    assert response.status_code == 401


async def test_log_workflow_event_denied_for_sales_role(client, rig):
    token = await _login(client, rig["sales_email"])
    response = await client.post(
        "/api/v1/workflow-events",
        headers=_auth(token),
        json={"workflow_name": "WF-99 Global Error Handler", "status": "error"},
    )
    assert response.status_code == 403


async def test_log_workflow_event_persists_row(client, rig):
    token = await _login(client, rig["automation_email"])
    execution_id = str(uuid.uuid4().int)[:10]
    response = await client.post(
        "/api/v1/workflow-events",
        headers=_auth(token),
        json={
            "n8n_execution_id": execution_id,
            "workflow_name": "WF-04 Send PO to Supplier",
            "status": "error",
            "payload": {
                "failed_node": "Send PO Email via Resend",
                "error_message": "403 - sandbox domain rejected",
            },
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["workflow_name"] == "WF-04 Send PO to Supplier"
    assert body["status"] == "error"

    async with AsyncSessionLocal() as session:
        event = (
            await session.execute(
                select(WorkflowEvent).where(WorkflowEvent.id == uuid.UUID(body["id"]))
            )
        ).scalar_one()
        assert event.org_id == rig["org_id"]
        assert event.n8n_execution_id == execution_id
        assert event.payload["failed_node"] == "Send PO Email via Resend"
