import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.core.security import hash_secret
from app.db.models.catalog import Product, Warehouse
from app.db.models.inventory import InventoryItem, StockLedger
from app.db.models.numbering import DocumentSequence
from app.db.models.org import Organization, Role, User, UserRole
from app.db.models.partners import Customer
from app.db.models.sales import CustomerInvoice, SalesOrder, SalesOrderItem
from app.db.session import AsyncSessionLocal
from app.services.inventory import InventoryLine, receive

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
        org = Organization(name=f"test-sales-api-{uuid.uuid4()}", state_code=TELANGANA)
        session.add(org)
        await session.flush()

        warehouse = Warehouse(
            org_id=org.id, code=f"WH-{uuid.uuid4().hex[:8]}", name="Test Warehouse"
        )
        product = Product(
            org_id=org.id,
            sku=f"TEST-SO-API-{uuid.uuid4().hex[:8]}",
            name="Test Product",
            hsn_code="8544",
            uom="PCS",
            gst_rate=Decimal("18"),
        )
        customer = Customer(
            org_id=org.id,
            name="Test Customer",
            state_code=TELANGANA,
            credit_limit=Decimal("1000000"),
        )
        session.add_all([warehouse, product, customer])
        await session.flush()

        # Role names are globally unique and ROLE_PERMISSIONS is keyed by
        # the exact canonical names ("Sales"/"Warehouse") — get-or-create
        # rather than insert a differently-named role, same pattern as
        # tests/test_deps.py and tests/test_approvals.py.
        sales_role = (
            await session.execute(select(Role).where(Role.name == "Sales"))
        ).scalar_one_or_none()
        if sales_role is None:
            sales_role = Role(name="Sales")
            session.add(sales_role)
        warehouse_role = (
            await session.execute(select(Role).where(Role.name == "Warehouse"))
        ).scalar_one_or_none()
        if warehouse_role is None:
            warehouse_role = Role(name="Warehouse")
            session.add(warehouse_role)
        await session.flush()

        sales_user = User(
            org_id=org.id,
            email=f"sales-{uuid.uuid4().hex[:8]}@test.com",
            full_name="Sales",
            hashed_password=hash_secret(PASSWORD),
        )
        warehouse_user = User(
            org_id=org.id,
            email=f"wh-{uuid.uuid4().hex[:8]}@test.com",
            full_name="Warehouse",
            hashed_password=hash_secret(PASSWORD),
        )
        session.add_all([sales_user, warehouse_user])
        await session.flush()
        session.add_all(
            [
                UserRole(user_id=sales_user.id, role_id=sales_role.id),
                UserRole(user_id=warehouse_user.id, role_id=warehouse_role.id),
            ]
        )
        await session.commit()

        ids = {
            "org_id": org.id,
            "warehouse_id": warehouse.id,
            "product_id": product.id,
            "customer_id": customer.id,
            "sales_email": sales_user.email,
            "warehouse_email": warehouse_user.email,
        }

    async with AsyncSessionLocal() as session:
        await receive(
            session,
            lines=[
                InventoryLine(
                    product_id=ids["product_id"],
                    warehouse_id=ids["warehouse_id"],
                    quantity=Decimal("100"),
                )
            ],
            ref_type="goods_receipt",
        )
        await session.commit()

    yield ids

    async with AsyncSessionLocal() as session:
        so_id_list = (
            (await session.execute(select(SalesOrder.id).where(SalesOrder.org_id == ids["org_id"])))
            .scalars()
            .all()
        )
        if so_id_list:
            await session.execute(
                delete(SalesOrderItem).where(SalesOrderItem.sales_order_id.in_(so_id_list))
            )
            await session.execute(delete(SalesOrder).where(SalesOrder.id.in_(so_id_list)))
        await session.execute(
            delete(CustomerInvoice).where(CustomerInvoice.org_id == ids["org_id"])
        )
        await session.execute(
            delete(StockLedger).where(StockLedger.warehouse_id == ids["warehouse_id"])
        )
        await session.execute(
            delete(InventoryItem).where(InventoryItem.warehouse_id == ids["warehouse_id"])
        )
        user_id_list = (
            (await session.execute(select(User.id).where(User.org_id == ids["org_id"])))
            .scalars()
            .all()
        )
        if user_id_list:
            await session.execute(delete(UserRole).where(UserRole.user_id.in_(user_id_list)))
        await session.execute(delete(User).where(User.org_id == ids["org_id"]))
        await session.execute(
            delete(DocumentSequence).where(DocumentSequence.org_id == ids["org_id"])
        )
        await session.execute(delete(Customer).where(Customer.id == ids["customer_id"]))
        await session.execute(delete(Product).where(Product.id == ids["product_id"]))
        await session.execute(delete(Warehouse).where(Warehouse.id == ids["warehouse_id"]))
        await session.execute(delete(Organization).where(Organization.id == ids["org_id"]))
        await session.commit()


@pytest.mark.asyncio
async def test_create_sales_order_requires_auth(client, rig):
    response = await client.post(
        "/api/v1/sales-orders",
        json={
            "customer_id": str(rig["customer_id"]),
            "warehouse_id": str(rig["warehouse_id"]),
            "lines": [{"product_id": str(rig["product_id"]), "quantity": "5", "unit_price": "100"}],
        },
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_create_sales_order_denied_for_warehouse_role(client, rig):
    token = await _login(client, rig["warehouse_email"])
    response = await client.post(
        "/api/v1/sales-orders",
        headers=_auth(token),
        json={
            "customer_id": str(rig["customer_id"]),
            "warehouse_id": str(rig["warehouse_id"]),
            "lines": [{"product_id": str(rig["product_id"]), "quantity": "5", "unit_price": "100"}],
        },
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_create_sales_order_full_reservation(client, rig):
    token = await _login(client, rig["sales_email"])
    response = await client.post(
        "/api/v1/sales-orders",
        headers=_auth(token),
        json={
            "customer_id": str(rig["customer_id"]),
            "warehouse_id": str(rig["warehouse_id"]),
            "lines": [
                {"product_id": str(rig["product_id"]), "quantity": "10", "unit_price": "100"}
            ],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "reserved"
    assert body["has_shortage"] is False
    [line] = body["lines"]
    assert Decimal(line["reserved_qty"]) == Decimal("10")


@pytest.mark.asyncio
async def test_create_quote_then_confirm(client, rig):
    token = await _login(client, rig["sales_email"])
    quote = await client.post(
        "/api/v1/sales-orders",
        headers=_auth(token),
        json={
            "customer_id": str(rig["customer_id"]),
            "warehouse_id": str(rig["warehouse_id"]),
            "lines": [{"product_id": str(rig["product_id"]), "quantity": "5", "unit_price": "100"}],
            "is_quote": True,
        },
    )
    assert quote.status_code == 201, quote.text
    assert quote.json()["status"] == "draft"
    sales_order_id = quote.json()["sales_order_id"]

    confirmed = await client.post(
        f"/api/v1/sales-orders/{sales_order_id}/confirm",
        headers=_auth(token),
        json={"warehouse_id": str(rig["warehouse_id"])},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "reserved"


@pytest.mark.asyncio
async def test_create_counter_sale(client, rig):
    token = await _login(client, rig["sales_email"])
    response = await client.post(
        "/api/v1/counter-sales",
        headers=_auth(token),
        json={
            "customer_id": str(rig["customer_id"]),
            "warehouse_id": str(rig["warehouse_id"]),
            "lines": [{"product_id": str(rig["product_id"]), "quantity": "3", "unit_price": "100"}],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "issued"
    assert Decimal(body["total"]) == Decimal("354.00")  # 300 + 18% GST
