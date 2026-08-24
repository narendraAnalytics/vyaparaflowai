import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.core.security import hash_secret
from app.db.models.finance import Payment, PaymentAllocation
from app.db.models.org import Organization, Role, User, UserRole
from app.db.models.partners import Customer
from app.db.models.sales import CustomerInvoice
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
        org = Organization(name=f"test-payments-api-{uuid.uuid4()}", state_code=TELANGANA)
        session.add(org)
        await session.flush()

        customer = Customer(org_id=org.id, name="Test Customer", state_code=TELANGANA)
        session.add(customer)
        await session.flush()

        invoice = CustomerInvoice(
            org_id=org.id,
            customer_id=customer.id,
            invoice_number=f"INV-TEST-{uuid.uuid4().hex[:8]}",
            invoice_date=TODAY,
            due_date=TODAY,
            status="issued",
            subtotal=Decimal("1000"),
            tax_total=Decimal("0"),
            total=Decimal("1000"),
            amount_paid=Decimal("0"),
        )
        session.add(invoice)
        await session.flush()

        accounts_role = (
            await session.execute(select(Role).where(Role.name == "Accounts"))
        ).scalar_one_or_none()
        if accounts_role is None:
            accounts_role = Role(name="Accounts")
            session.add(accounts_role)
        sales_role = (
            await session.execute(select(Role).where(Role.name == "Sales"))
        ).scalar_one_or_none()
        if sales_role is None:
            sales_role = Role(name="Sales")
            session.add(sales_role)
        await session.flush()

        accounts_user = User(
            org_id=org.id,
            email=f"acct-{uuid.uuid4().hex[:8]}@test.com",
            full_name="Accounts",
            hashed_password=hash_secret(PASSWORD),
        )
        sales_user = User(
            org_id=org.id,
            email=f"sales-{uuid.uuid4().hex[:8]}@test.com",
            full_name="Sales",
            hashed_password=hash_secret(PASSWORD),
        )
        session.add_all([accounts_user, sales_user])
        await session.flush()
        session.add_all(
            [
                UserRole(user_id=accounts_user.id, role_id=accounts_role.id),
                UserRole(user_id=sales_user.id, role_id=sales_role.id),
            ]
        )
        await session.commit()

        ids = {
            "org_id": org.id,
            "customer_id": customer.id,
            "invoice_id": invoice.id,
            "accounts_email": accounts_user.email,
            "sales_email": sales_user.email,
        }

    yield ids

    async with AsyncSessionLocal() as session:
        payment_id_list = (
            (await session.execute(select(Payment.id).where(Payment.org_id == ids["org_id"])))
            .scalars()
            .all()
        )
        if payment_id_list:
            await session.execute(
                delete(PaymentAllocation).where(PaymentAllocation.payment_id.in_(payment_id_list))
            )
            await session.execute(delete(Payment).where(Payment.id.in_(payment_id_list)))
        await session.execute(
            delete(CustomerInvoice).where(CustomerInvoice.org_id == ids["org_id"])
        )
        user_id_list = (
            (await session.execute(select(User.id).where(User.org_id == ids["org_id"])))
            .scalars()
            .all()
        )
        if user_id_list:
            await session.execute(delete(UserRole).where(UserRole.user_id.in_(user_id_list)))
        await session.execute(delete(User).where(User.org_id == ids["org_id"]))
        await session.execute(delete(Customer).where(Customer.id == ids["customer_id"]))
        await session.execute(delete(Organization).where(Organization.id == ids["org_id"]))
        await session.commit()


@pytest.mark.asyncio
async def test_record_payment_denied_for_sales_role(client, rig):
    token = await _login(client, rig["sales_email"])
    response = await client.post(
        "/api/v1/payments",
        headers=_auth(token),
        json={
            "party_type": "customer",
            "party_id": str(rig["customer_id"]),
            "amount": "1000",
            "payment_date": str(TODAY),
        },
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_record_payment_full_amount_marks_invoice_paid(client, rig):
    token = await _login(client, rig["accounts_email"])
    response = await client.post(
        "/api/v1/payments",
        headers=_auth(token),
        json={
            "party_type": "customer",
            "party_id": str(rig["customer_id"]),
            "amount": "1000",
            "payment_date": str(TODAY),
            "method": "upi",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert Decimal(body["unapplied_amount"]) == Decimal("0")
    [line] = body["invoices"]
    assert line["new_status"] == "paid"


@pytest.mark.asyncio
async def test_outstanding_balance_before_payment(client, rig):
    token = await _login(client, rig["accounts_email"])
    response = await client.get(
        "/api/v1/payments/outstanding-balance",
        headers=_auth(token),
        params={"party_type": "customer", "party_id": str(rig["customer_id"])},
    )
    assert response.status_code == 200, response.text
    assert Decimal(response.json()) == Decimal("1000")


@pytest.mark.asyncio
async def test_aging_report_buckets_the_invoice(client, rig):
    token = await _login(client, rig["accounts_email"])
    response = await client.get(
        "/api/v1/payments/aging-report",
        headers=_auth(token),
        params={
            "party_type": "customer",
            "party_id": str(rig["customer_id"]),
            "as_of": str(TODAY),
        },
    )
    assert response.status_code == 200, response.text
    [line] = response.json()
    assert line["invoice_id"] == str(rig["invoice_id"])
    assert line["bucket"] == "0-30"
