import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.core.security import hash_secret
from app.db.models.catalog import Product, Warehouse
from app.db.models.org import Organization, Role, User, UserRole
from app.db.models.partners import Supplier
from app.db.models.purchase import (
    GoodsReceipt,
    GoodsReceiptItem,
    PurchaseOrder,
    PurchaseOrderItem,
    SupplierInvoice,
    SupplierInvoiceItem,
    ThreeWayMatchResult,
)
from app.db.session import AsyncSessionLocal

TELANGANA = "36"
PASSWORD = "TestPassw0rd!"
A_WEEKDAY = date(2026, 8, 24)


async def _login(client, email: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def rig():
    async with AsyncSessionLocal() as session:
        org = Organization(name=f"test-matching-api-{uuid.uuid4()}", state_code=TELANGANA)
        session.add(org)
        await session.flush()

        warehouse = Warehouse(
            org_id=org.id, code=f"WH-{uuid.uuid4().hex[:8]}", name="Test Warehouse"
        )
        product = Product(
            org_id=org.id,
            sku=f"TEST-MATCH-API-{uuid.uuid4().hex[:8]}",
            name="Test Product",
            hsn_code="8544",
            uom="PCS",
            gst_rate=Decimal("18"),
        )
        supplier = Supplier(org_id=org.id, name="Test Supplier", state_code=TELANGANA)
        session.add_all([warehouse, product, supplier])
        await session.flush()

        po = PurchaseOrder(
            org_id=org.id,
            supplier_id=supplier.id,
            po_number=f"PO-TEST-{uuid.uuid4().hex[:8]}",
            order_date=A_WEEKDAY,
            status="approved",
            subtotal=Decimal("1005.00"),
            tax_total=Decimal("0"),
            total=Decimal("1005.00"),
        )
        session.add(po)
        await session.flush()
        po_item = PurchaseOrderItem(
            purchase_order_id=po.id,
            product_id=product.id,
            quantity=Decimal("10"),
            unit_price=Decimal("100.50"),
            gst_rate=Decimal("0"),
            line_subtotal=Decimal("1005.00"),
            line_tax=Decimal("0"),
            line_total=Decimal("1005.00"),
        )
        session.add(po_item)

        grn = GoodsReceipt(
            org_id=org.id,
            purchase_order_id=po.id,
            warehouse_id=warehouse.id,
            grn_number=f"GRN-TEST-{uuid.uuid4().hex[:8]}",
            received_date=A_WEEKDAY,
        )
        session.add(grn)
        await session.flush()
        session.add(
            GoodsReceiptItem(
                goods_receipt_id=grn.id,
                purchase_order_item_id=po_item.id,
                product_id=product.id,
                ordered_quantity=Decimal("10"),
                received_quantity=Decimal("10"),
                accepted_quantity=Decimal("10"),
            )
        )

        invoice = SupplierInvoice(
            org_id=org.id,
            supplier_id=supplier.id,
            purchase_order_id=po.id,
            invoice_number=f"INV-TEST-{uuid.uuid4().hex[:8]}",
            invoice_date=A_WEEKDAY,
            due_date=A_WEEKDAY,
            status="received",
            subtotal=Decimal("1005.00"),
            tax_total=Decimal("0"),
            total=Decimal("1005.00"),
        )
        session.add(invoice)
        await session.flush()
        session.add(
            SupplierInvoiceItem(
                supplier_invoice_id=invoice.id,
                product_id=product.id,
                quantity=Decimal("10"),
                unit_price=Decimal("100.50"),
                gst_rate=Decimal("0"),
                line_total=Decimal("1005.00"),
            )
        )

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
            "warehouse_id": warehouse.id,
            "product_id": product.id,
            "supplier_id": supplier.id,
            "po_id": po.id,
            "grn_id": grn.id,
            "invoice_id": invoice.id,
            "accounts_email": accounts_user.email,
            "sales_email": sales_user.email,
        }

    yield ids

    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(ThreeWayMatchResult).where(ThreeWayMatchResult.org_id == ids["org_id"])
        )
        await session.execute(
            delete(SupplierInvoiceItem).where(
                SupplierInvoiceItem.supplier_invoice_id == ids["invoice_id"]
            )
        )
        await session.execute(
            delete(SupplierInvoice).where(SupplierInvoice.id == ids["invoice_id"])
        )
        await session.execute(
            delete(GoodsReceiptItem).where(GoodsReceiptItem.goods_receipt_id == ids["grn_id"])
        )
        await session.execute(delete(GoodsReceipt).where(GoodsReceipt.id == ids["grn_id"]))
        await session.execute(
            delete(PurchaseOrderItem).where(PurchaseOrderItem.purchase_order_id == ids["po_id"])
        )
        await session.execute(delete(PurchaseOrder).where(PurchaseOrder.id == ids["po_id"]))
        user_id_list = (
            (await session.execute(select(User.id).where(User.org_id == ids["org_id"])))
            .scalars()
            .all()
        )
        if user_id_list:
            await session.execute(delete(UserRole).where(UserRole.user_id.in_(user_id_list)))
        await session.execute(delete(User).where(User.org_id == ids["org_id"]))
        await session.execute(delete(Supplier).where(Supplier.id == ids["supplier_id"]))
        await session.execute(delete(Product).where(Product.id == ids["product_id"]))
        await session.execute(delete(Warehouse).where(Warehouse.id == ids["warehouse_id"]))
        await session.execute(delete(Organization).where(Organization.id == ids["org_id"]))
        await session.commit()


@pytest.mark.asyncio
async def test_run_three_way_match_requires_auth(client, rig):
    response = await client.post(
        "/api/v1/matching/three-way",
        json={
            "purchase_order_id": str(rig["po_id"]),
            "goods_receipt_id": str(rig["grn_id"]),
            "supplier_invoice_id": str(rig["invoice_id"]),
        },
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_run_three_way_match_denied_for_sales_role(client, rig):
    token = await _login(client, rig["sales_email"])
    response = await client.post(
        "/api/v1/matching/three-way",
        headers=_auth(token),
        json={
            "purchase_order_id": str(rig["po_id"]),
            "goods_receipt_id": str(rig["grn_id"]),
            "supplier_invoice_id": str(rig["invoice_id"]),
        },
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_run_three_way_match_auto_approves_perfect_match(client, rig):
    token = await _login(client, rig["accounts_email"])
    response = await client.post(
        "/api/v1/matching/three-way",
        headers=_auth(token),
        json={
            "purchase_order_id": str(rig["po_id"]),
            "goods_receipt_id": str(rig["grn_id"]),
            "supplier_invoice_id": str(rig["invoice_id"]),
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["outcome"]["verdict"] == "auto_approve"
