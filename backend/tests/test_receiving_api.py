import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.core.security import hash_secret
from app.db.models.catalog import Product, Warehouse
from app.db.models.inventory import InventoryItem, StockLedger
from app.db.models.numbering import DocumentSequence
from app.db.models.org import Organization, Role, User, UserRole
from app.db.models.partners import Customer, Supplier
from app.db.models.purchase import (
    GoodsReceipt,
    GoodsReceiptItem,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseRequisition,
    SupplierInvoice,
    SupplierInvoiceItem,
)
from app.db.models.sales import SalesOrder
from app.db.session import AsyncSessionLocal

TELANGANA = "36"
PASSWORD = "TestPassw0rd!"


async def _login(client, email: str) -> str:
    """POST /auth/login is rate-limited per client IP, and httpx's
    ASGITransport gives every test in the whole session the same client
    identity (see test_ratelimit_api.py) - a file that alphabetically
    sorts right before this one deliberately floods that shared bucket to
    prove the limit works. Waiting out the bucket's own Retry-After on a
    429 (rather than treating it as a failure) is how a real client would
    behave, and keeps this file's tests correct regardless of run order.
    """
    for _ in range(3):
        response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )
        if response.status_code != 429:
            assert response.status_code == 200, response.text
            return str(response.json()["access_token"])
        retry_after = int(response.headers.get("Retry-After", "5"))
        await asyncio.sleep(min(retry_after, 65) + 1)
    raise AssertionError(f"login for {email} still rate-limited after retries")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def rig():
    async with AsyncSessionLocal() as session:
        org = Organization(name=f"test-receiving-api-{uuid.uuid4()}", state_code=TELANGANA)
        session.add(org)
        await session.flush()

        warehouse = Warehouse(
            org_id=org.id, code=f"WH-{uuid.uuid4().hex[:8]}", name="Test Warehouse"
        )
        product = Product(
            org_id=org.id,
            sku=f"TEST-RECV-API-{uuid.uuid4().hex[:8]}",
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
            po_number=f"PO-TEST-RECV-{uuid.uuid4().hex[:8]}",
            order_date="2026-08-01",
            status="approved",
            subtotal=Decimal("1000.00"),
            tax_total=Decimal("0"),
            total=Decimal("1000.00"),
        )
        session.add(po)
        await session.flush()
        po_item = PurchaseOrderItem(
            purchase_order_id=po.id,
            product_id=product.id,
            quantity=Decimal("10"),
            unit_price=Decimal("100.00"),
            gst_rate=Decimal("0"),
            line_subtotal=Decimal("1000.00"),
            line_tax=Decimal("0"),
            line_total=Decimal("1000.00"),
        )
        session.add(po_item)
        await session.flush()

        warehouse_role = (
            await session.execute(select(Role).where(Role.name == "Warehouse"))
        ).scalar_one_or_none()
        if warehouse_role is None:
            warehouse_role = Role(name="Warehouse")
            session.add(warehouse_role)
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

        warehouse_user = User(
            org_id=org.id,
            email=f"wh-{uuid.uuid4().hex[:8]}@test.com",
            full_name="Warehouse Clerk",
            hashed_password=hash_secret(PASSWORD),
        )
        accounts_user = User(
            org_id=org.id,
            email=f"acct-{uuid.uuid4().hex[:8]}@test.com",
            full_name="Accounts Clerk",
            hashed_password=hash_secret(PASSWORD),
        )
        sales_user = User(
            org_id=org.id,
            email=f"sales-{uuid.uuid4().hex[:8]}@test.com",
            full_name="Sales",
            hashed_password=hash_secret(PASSWORD),
        )
        session.add_all([warehouse_user, accounts_user, sales_user])
        await session.flush()
        session.add_all(
            [
                UserRole(user_id=warehouse_user.id, role_id=warehouse_role.id),
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
            "po_item_id": po_item.id,
            "warehouse_email": warehouse_user.email,
            "accounts_email": accounts_user.email,
            "sales_email": sales_user.email,
        }

    yield ids

    async with AsyncSessionLocal() as session:
        grn_id_list = (
            (
                await session.execute(
                    select(GoodsReceipt.id).where(GoodsReceipt.org_id == ids["org_id"])
                )
            )
            .scalars()
            .all()
        )
        if grn_id_list:
            await session.execute(
                delete(GoodsReceiptItem).where(GoodsReceiptItem.goods_receipt_id.in_(grn_id_list))
            )
            await session.execute(delete(GoodsReceipt).where(GoodsReceipt.id.in_(grn_id_list)))
        invoice_id_list = (
            (
                await session.execute(
                    select(SupplierInvoice.id).where(SupplierInvoice.org_id == ids["org_id"])
                )
            )
            .scalars()
            .all()
        )
        if invoice_id_list:
            await session.execute(
                delete(SupplierInvoiceItem).where(
                    SupplierInvoiceItem.supplier_invoice_id.in_(invoice_id_list)
                )
            )
            await session.execute(
                delete(SupplierInvoice).where(SupplierInvoice.id.in_(invoice_id_list))
            )
        await session.execute(
            delete(PurchaseOrderItem).where(PurchaseOrderItem.purchase_order_id == ids["po_id"])
        )
        await session.execute(delete(PurchaseOrder).where(PurchaseOrder.id == ids["po_id"]))
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
async def test_create_goods_receipt_requires_auth(client, rig):
    response = await client.post(
        "/api/v1/goods-receipts",
        json={
            "purchase_order_id": str(rig["po_id"]),
            "warehouse_id": str(rig["warehouse_id"]),
            "received_date": "2026-08-05",
            "lines": [],
        },
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_create_goods_receipt_denied_for_sales_role(client, rig):
    token = await _login(client, rig["sales_email"])
    response = await client.post(
        "/api/v1/goods-receipts",
        headers=_auth(token),
        json={
            "purchase_order_id": str(rig["po_id"]),
            "warehouse_id": str(rig["warehouse_id"]),
            "received_date": "2026-08-05",
            "lines": [
                {
                    "purchase_order_item_id": str(rig["po_item_id"]),
                    "received_quantity": "10",
                    "accepted_quantity": "10",
                }
            ],
        },
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_create_partial_then_full_goods_receipt_updates_po_and_stock(client, rig):
    token = await _login(client, rig["warehouse_email"])

    partial = await client.post(
        "/api/v1/goods-receipts",
        headers=_auth(token),
        json={
            "purchase_order_id": str(rig["po_id"]),
            "warehouse_id": str(rig["warehouse_id"]),
            "received_date": "2026-08-05",
            "lines": [
                {
                    "purchase_order_item_id": str(rig["po_item_id"]),
                    "received_quantity": "6",
                    "accepted_quantity": "5",
                    "rejected_quantity": "1",
                }
            ],
        },
    )
    assert partial.status_code == 201, partial.text
    assert partial.json()["purchase_order_status"] == "partially_received"
    assert partial.json()["grn_number"].startswith("GRN-")
    # This rig's PO has no purchase_requisition_id (created directly, not
    # via convert_purchase_requisition_to_orders) - WF-05's "which sales
    # order was this for" lookup must resolve to None, not error, when
    # there's no requisition to trace back through.
    assert partial.json()["triggered_by_sales_order_id"] is None

    async with AsyncSessionLocal() as session:
        item = (
            await session.execute(
                select(InventoryItem).where(
                    InventoryItem.product_id == rig["product_id"],
                    InventoryItem.warehouse_id == rig["warehouse_id"],
                )
            )
        ).scalar_one()
        assert item.on_hand == Decimal("5")

    over_receipt = await client.post(
        "/api/v1/goods-receipts",
        headers=_auth(token),
        json={
            "purchase_order_id": str(rig["po_id"]),
            "warehouse_id": str(rig["warehouse_id"]),
            "received_date": "2026-08-06",
            "lines": [
                {
                    "purchase_order_item_id": str(rig["po_item_id"]),
                    "received_quantity": "6",
                    "accepted_quantity": "6",
                }
            ],
        },
    )
    assert over_receipt.status_code == 409

    final = await client.post(
        "/api/v1/goods-receipts",
        headers=_auth(token),
        json={
            "purchase_order_id": str(rig["po_id"]),
            "warehouse_id": str(rig["warehouse_id"]),
            "received_date": "2026-08-06",
            "lines": [
                {
                    "purchase_order_item_id": str(rig["po_item_id"]),
                    "received_quantity": "5",
                    "accepted_quantity": "5",
                }
            ],
        },
    )
    assert final.status_code == 201, final.text
    assert final.json()["purchase_order_status"] == "received"

    async with AsyncSessionLocal() as session:
        item = (
            await session.execute(
                select(InventoryItem).where(
                    InventoryItem.product_id == rig["product_id"],
                    InventoryItem.warehouse_id == rig["warehouse_id"],
                )
            )
        ).scalar_one()
        assert item.on_hand == Decimal("10")


@pytest.mark.asyncio
async def test_create_supplier_invoice_denied_for_sales_role(client, rig):
    token = await _login(client, rig["sales_email"])
    response = await client.post(
        "/api/v1/supplier-invoices",
        headers=_auth(token),
        json={
            "supplier_id": str(rig["supplier_id"]),
            "invoice_number": f"INV-{uuid.uuid4().hex[:8]}",
            "invoice_date": "2026-08-06",
            "due_date": "2026-09-05",
            "lines": [
                {
                    "product_id": str(rig["product_id"]),
                    "quantity": "10",
                    "unit_price": "100.00",
                    "gst_rate": "18",
                    "line_total": "1180.00",
                }
            ],
        },
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_create_supplier_invoice_happy_path_and_duplicate_rejected(client, rig):
    token = await _login(client, rig["accounts_email"])
    invoice_number = f"INV-{uuid.uuid4().hex[:8]}"
    payload = {
        "supplier_id": str(rig["supplier_id"]),
        "purchase_order_id": str(rig["po_id"]),
        "invoice_number": invoice_number,
        "invoice_date": "2026-08-06",
        "due_date": "2026-09-05",
        "lines": [
            {
                "product_id": str(rig["product_id"]),
                "quantity": "10",
                "unit_price": "100.00",
                "gst_rate": "18",
                "line_total": "1180.00",
            }
        ],
    }
    created = await client.post("/api/v1/supplier-invoices", headers=_auth(token), json=payload)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "received"
    assert Decimal(body["subtotal"]) == Decimal("1000.00")
    assert Decimal(body["tax_total"]) == Decimal("180.00")
    assert Decimal(body["total"]) == Decimal("1180.00")

    duplicate = await client.post("/api/v1/supplier-invoices", headers=_auth(token), json=payload)
    assert duplicate.status_code == 409


@pytest.mark.asyncio
async def test_goods_receipt_resolves_triggered_by_sales_order_id(client, rig):
    """A PO that traces back through a requisition to the sales order
    whose shortage created it (reactive procurement, 2.8) should surface
    that sales_order_id on the GRN response - this is what lets WF-05
    decide whether to call retry-reservation at all.
    """
    async with AsyncSessionLocal() as session:
        sales_order = SalesOrder(
            org_id=rig["org_id"],
            customer_id=None,
            order_number=f"SO-TEST-RECV-{uuid.uuid4().hex[:8]}",
            order_date="2026-08-01",
            status="confirmed",
        )
        # customer_id is NOT NULL in the schema - build a minimal customer
        # inline rather than pulling in test_sales.py's whole rig.
        customer = Customer(org_id=rig["org_id"], name="Retry Test Customer", state_code=TELANGANA)
        session.add(customer)
        await session.flush()
        sales_order.customer_id = customer.id
        session.add(sales_order)
        await session.flush()

        requisition = PurchaseRequisition(
            org_id=rig["org_id"],
            requisition_number=f"PR-TEST-RECV-{uuid.uuid4().hex[:8]}",
            status="converted",
            triggered_by_sales_order_id=sales_order.id,
        )
        session.add(requisition)
        await session.flush()

        po = (
            await session.execute(select(PurchaseOrder).where(PurchaseOrder.id == rig["po_id"]))
        ).scalar_one()
        po.purchase_requisition_id = requisition.id
        await session.commit()

        ids = {
            "sales_order_id": sales_order.id,
            "customer_id": customer.id,
            "requisition_id": requisition.id,
        }

    token = await _login(client, rig["warehouse_email"])
    response = await client.post(
        "/api/v1/goods-receipts",
        headers=_auth(token),
        json={
            "purchase_order_id": str(rig["po_id"]),
            "warehouse_id": str(rig["warehouse_id"]),
            "received_date": "2026-08-07",
            "lines": [
                {
                    "purchase_order_item_id": str(rig["po_item_id"]),
                    "received_quantity": "10",
                    "accepted_quantity": "10",
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["triggered_by_sales_order_id"] == str(ids["sales_order_id"])

    async with AsyncSessionLocal() as session:
        po = (
            await session.execute(select(PurchaseOrder).where(PurchaseOrder.id == rig["po_id"]))
        ).scalar_one()
        po.purchase_requisition_id = None
        await session.execute(
            delete(PurchaseRequisition).where(PurchaseRequisition.id == ids["requisition_id"])
        )
        await session.execute(delete(SalesOrder).where(SalesOrder.id == ids["sales_order_id"]))
        await session.execute(delete(Customer).where(Customer.id == ids["customer_id"]))
        await session.commit()
