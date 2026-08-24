import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.core.security import hash_secret
from app.db.models.catalog import Product, ProductSupplier, Warehouse
from app.db.models.inventory import InventoryItem, StockLedger
from app.db.models.numbering import DocumentSequence
from app.db.models.org import Organization, Role, User, UserRole
from app.db.models.partners import Supplier
from app.db.models.purchase import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseRequisition,
    PurchaseRequisitionItem,
)
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
        org = Organization(name=f"test-procurement-api-{uuid.uuid4()}", state_code=TELANGANA)
        session.add(org)
        await session.flush()

        warehouse = Warehouse(
            org_id=org.id, code=f"WH-{uuid.uuid4().hex[:8]}", name="Test Warehouse"
        )
        product = Product(
            org_id=org.id,
            sku=f"TEST-PROC-API-{uuid.uuid4().hex[:8]}",
            name="Test Product",
            hsn_code="8544",
            uom="PCS",
            gst_rate=Decimal("18"),
        )
        supplier = Supplier(
            org_id=org.id, name="Test Supplier", state_code=TELANGANA, lead_time_days=5
        )
        session.add_all([warehouse, product, supplier])
        await session.flush()

        session.add(
            ProductSupplier(
                product_id=product.id,
                supplier_id=supplier.id,
                unit_price=Decimal("100"),
                moq=10,
                lead_time_days=5,
                is_preferred=True,
            )
        )

        manager_role = (
            await session.execute(select(Role).where(Role.name == "Manager"))
        ).scalar_one_or_none()
        if manager_role is None:
            manager_role = Role(name="Manager")
            session.add(manager_role)
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
        sales_user = User(
            org_id=org.id,
            email=f"sales-{uuid.uuid4().hex[:8]}@test.com",
            full_name="Sales",
            hashed_password=hash_secret(PASSWORD),
        )
        session.add_all([manager_user, sales_user])
        await session.flush()
        session.add_all(
            [
                UserRole(user_id=manager_user.id, role_id=manager_role.id),
                UserRole(user_id=sales_user.id, role_id=sales_role.id),
            ]
        )
        await session.commit()

        ids = {
            "org_id": org.id,
            "warehouse_id": warehouse.id,
            "product_id": product.id,
            "supplier_id": supplier.id,
            "manager_email": manager_user.email,
            "sales_email": sales_user.email,
        }

    async with AsyncSessionLocal() as session:
        await receive(
            session,
            lines=[
                InventoryLine(
                    product_id=ids["product_id"],
                    warehouse_id=ids["warehouse_id"],
                    quantity=Decimal("5"),
                )
            ],
            ref_type="goods_receipt",
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        item = (
            await session.execute(
                select(InventoryItem).where(
                    InventoryItem.product_id == ids["product_id"],
                    InventoryItem.warehouse_id == ids["warehouse_id"],
                )
            )
        ).scalar_one()
        item.reorder_level = Decimal("30")
        item.safety_stock = Decimal("10")
        await session.commit()

    yield ids

    async with AsyncSessionLocal() as session:
        po_id_list = (
            (
                await session.execute(
                    select(PurchaseOrder.id).where(PurchaseOrder.org_id == ids["org_id"])
                )
            )
            .scalars()
            .all()
        )
        if po_id_list:
            await session.execute(
                delete(PurchaseOrderItem).where(PurchaseOrderItem.purchase_order_id.in_(po_id_list))
            )
            await session.execute(delete(PurchaseOrder).where(PurchaseOrder.id.in_(po_id_list)))
        pr_id_list = (
            (
                await session.execute(
                    select(PurchaseRequisition.id).where(
                        PurchaseRequisition.org_id == ids["org_id"]
                    )
                )
            )
            .scalars()
            .all()
        )
        if pr_id_list:
            await session.execute(
                delete(PurchaseRequisitionItem).where(
                    PurchaseRequisitionItem.purchase_requisition_id.in_(pr_id_list)
                )
            )
            await session.execute(
                delete(PurchaseRequisition).where(PurchaseRequisition.id.in_(pr_id_list))
            )
        await session.execute(
            delete(ProductSupplier).where(ProductSupplier.product_id == ids["product_id"])
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
        await session.execute(delete(Supplier).where(Supplier.id == ids["supplier_id"]))
        await session.execute(delete(Product).where(Product.id == ids["product_id"]))
        await session.execute(delete(Warehouse).where(Warehouse.id == ids["warehouse_id"]))
        await session.execute(delete(Organization).where(Organization.id == ids["org_id"]))
        await session.commit()


@pytest.mark.asyncio
async def test_list_shortages_requires_auth(client, rig):
    response = await client.get(
        "/api/v1/shortages", params={"warehouse_id": str(rig["warehouse_id"])}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_list_shortages_finds_below_reorder_product(client, rig):
    token = await _login(client, rig["sales_email"])
    response = await client.get(
        "/api/v1/shortages",
        headers=_auth(token),
        params={"warehouse_id": str(rig["warehouse_id"])},
    )
    assert response.status_code == 200, response.text
    [line] = [line for line in response.json() if line["product_id"] == str(rig["product_id"])]
    assert Decimal(line["shortage_qty"]) == Decimal("25")  # 30 reorder - 5 on hand


@pytest.mark.asyncio
async def test_list_supplier_scores(client, rig):
    token = await _login(client, rig["sales_email"])
    response = await client.get(
        f"/api/v1/products/{rig['product_id']}/supplier-scores", headers=_auth(token)
    )
    assert response.status_code == 200, response.text
    [score] = response.json()
    assert score["supplier_id"] == str(rig["supplier_id"])
    assert score["is_preferred"] is True


@pytest.mark.asyncio
async def test_create_purchase_requisition_denied_for_sales_role(client, rig):
    token = await _login(client, rig["sales_email"])
    response = await client.post(
        "/api/v1/purchase-requisitions",
        headers=_auth(token),
        json={"lines": [{"product_id": str(rig["product_id"]), "quantity": "20"}]},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_create_and_convert_requisition(client, rig):
    token = await _login(client, rig["manager_email"])
    created = await client.post(
        "/api/v1/purchase-requisitions",
        headers=_auth(token),
        json={"lines": [{"product_id": str(rig["product_id"]), "quantity": "22"}]},
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "pending_approval"
    requisition_id = created.json()["purchase_requisition_id"]

    converted = await client.post(
        f"/api/v1/purchase-requisitions/{requisition_id}/convert", headers=_auth(token)
    )
    assert converted.status_code == 201, converted.text
    [po] = converted.json()
    assert po["supplier_id"] == str(rig["supplier_id"])
    [line] = po["lines"]
    assert Decimal(line["ordered_qty"]) == Decimal("30")  # ceil(22/10)*10
