import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models.catalog import Product, Warehouse
from app.db.models.inventory import InventoryItem, StockLedger
from app.db.models.numbering import DocumentSequence
from app.db.models.org import Organization
from app.db.models.partners import Customer
from app.db.models.sales import CustomerInvoice, CustomerInvoiceItem, SalesOrder, SalesOrderItem
from app.db.session import AsyncSessionLocal
from app.services import inventory
from app.services.inventory import InventoryLine, receive
from app.services.sales import (
    CreateCounterSaleRequest,
    CreateSalesOrderRequest,
    SalesOrderLineInput,
    confirm_sales_order,
    create_counter_sale,
    create_sales_order,
)

TELANGANA = "36"
KARNATAKA = "29"


@pytest.fixture
async def rig():
    async with AsyncSessionLocal() as session:
        org = Organization(name=f"test-sales-{uuid.uuid4()}", state_code=TELANGANA)
        session.add(org)
        await session.flush()

        warehouse = Warehouse(
            org_id=org.id, code=f"WH-{uuid.uuid4().hex[:8]}", name="Test Warehouse"
        )
        wire = Product(
            org_id=org.id,
            sku=f"TEST-WIRE-{uuid.uuid4().hex[:8]}",
            name="Test Wire",
            hsn_code="8544",
            uom="MTR",
            gst_rate=Decimal("18"),
        )
        inactive_product = Product(
            org_id=org.id,
            sku=f"TEST-INACTIVE-{uuid.uuid4().hex[:8]}",
            name="Discontinued Product",
            hsn_code="7318",
            uom="PCS",
            gst_rate=Decimal("18"),
            is_active=False,
        )
        customer = Customer(
            org_id=org.id,
            name="Good Standing Customer",
            state_code=TELANGANA,
            credit_limit=Decimal("100000"),
        )
        low_credit_customer = Customer(
            org_id=org.id,
            name="Low Credit Customer",
            state_code=TELANGANA,
            credit_limit=Decimal("50"),
        )
        inactive_customer = Customer(
            org_id=org.id,
            name="Inactive Customer",
            state_code=TELANGANA,
            credit_limit=Decimal("100000"),
            is_active=False,
        )
        inter_state_customer = Customer(
            org_id=org.id,
            name="Interstate Customer",
            state_code=KARNATAKA,
            credit_limit=Decimal("100000"),
        )
        session.add_all(
            [
                warehouse,
                wire,
                inactive_product,
                customer,
                low_credit_customer,
                inactive_customer,
                inter_state_customer,
            ]
        )
        await session.commit()
        ids = {
            "org_id": org.id,
            "warehouse_id": warehouse.id,
            "wire_id": wire.id,
            "inactive_product_id": inactive_product.id,
            "customer_id": customer.id,
            "low_credit_customer_id": low_credit_customer.id,
            "inactive_customer_id": inactive_customer.id,
            "inter_state_customer_id": inter_state_customer.id,
        }

    yield ids

    async with AsyncSessionLocal() as session:
        so_ids = (
            (await session.execute(select(SalesOrder.id).where(SalesOrder.org_id == ids["org_id"])))
            .scalars()
            .all()
        )
        if so_ids:
            await session.execute(
                delete(SalesOrderItem).where(SalesOrderItem.sales_order_id.in_(so_ids))
            )
            await session.execute(delete(SalesOrder).where(SalesOrder.id.in_(so_ids)))
        inv_ids = (
            (
                await session.execute(
                    select(CustomerInvoice.id).where(CustomerInvoice.org_id == ids["org_id"])
                )
            )
            .scalars()
            .all()
        )
        if inv_ids:
            await session.execute(
                delete(CustomerInvoiceItem).where(
                    CustomerInvoiceItem.customer_invoice_id.in_(inv_ids)
                )
            )
            await session.execute(delete(CustomerInvoice).where(CustomerInvoice.id.in_(inv_ids)))
        await session.execute(
            delete(StockLedger).where(StockLedger.warehouse_id == ids["warehouse_id"])
        )
        await session.execute(
            delete(InventoryItem).where(InventoryItem.warehouse_id == ids["warehouse_id"])
        )
        await session.execute(
            delete(DocumentSequence).where(DocumentSequence.org_id == ids["org_id"])
        )
        await session.execute(
            delete(Customer).where(
                Customer.id.in_(
                    [
                        ids["customer_id"],
                        ids["low_credit_customer_id"],
                        ids["inactive_customer_id"],
                        ids["inter_state_customer_id"],
                    ]
                )
            )
        )
        await session.execute(
            delete(Product).where(Product.id.in_([ids["wire_id"], ids["inactive_product_id"]]))
        )
        await session.execute(delete(Warehouse).where(Warehouse.id == ids["warehouse_id"]))
        await session.execute(delete(Organization).where(Organization.id == ids["org_id"]))
        await session.commit()


def _request(
    rig, *, quantity: str = "10", customer_key: str = "customer_id"
) -> CreateSalesOrderRequest:
    return CreateSalesOrderRequest(
        customer_id=rig[customer_key],
        warehouse_id=rig["warehouse_id"],
        lines=[
            SalesOrderLineInput(
                product_id=rig["wire_id"], quantity=Decimal(quantity), unit_price=Decimal("100")
            )
        ],
    )


@pytest.mark.asyncio
async def test_full_stock_reserves_completely(rig):
    async with AsyncSessionLocal() as session:
        await receive(
            session,
            lines=[
                InventoryLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("100"),
                )
            ],
            ref_type="goods_receipt",
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        result = await create_sales_order(session, org_id=rig["org_id"], request=_request(rig))
        await session.commit()

    assert result.status == "reserved"
    assert result.has_shortage is False
    assert result.subtotal == Decimal("1000.00")
    assert result.tax_total == Decimal("180.00")
    assert result.total == Decimal("1180")
    [line] = result.lines
    assert line.reserved_qty == Decimal("10")
    assert line.shortage_qty == Decimal("0")


@pytest.mark.asyncio
async def test_partial_stock_reserves_what_it_can(rig):
    async with AsyncSessionLocal() as session:
        await receive(
            session,
            lines=[
                InventoryLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("4"),
                )
            ],
            ref_type="goods_receipt",
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        result = await create_sales_order(session, org_id=rig["org_id"], request=_request(rig))
        await session.commit()

    assert result.status == "partially_reserved"
    assert result.has_shortage is True
    [line] = result.lines
    assert line.reserved_qty == Decimal("4")
    assert line.shortage_qty == Decimal("6")


@pytest.mark.asyncio
async def test_zero_stock_confirms_with_full_shortage(rig):
    async with AsyncSessionLocal() as session:
        result = await create_sales_order(session, org_id=rig["org_id"], request=_request(rig))
        await session.commit()

    assert result.status == "confirmed"
    assert result.has_shortage is True
    [line] = result.lines
    assert line.reserved_qty == Decimal("0")
    assert line.shortage_qty == Decimal("10")


@pytest.mark.asyncio
async def test_credit_limit_exceeded_rejects_before_creating_anything(rig):
    async with AsyncSessionLocal() as session:
        with pytest.raises(ConflictError):
            await create_sales_order(
                session,
                org_id=rig["org_id"],
                request=_request(rig, customer_key="low_credit_customer_id"),
            )

    async with AsyncSessionLocal() as session:
        count = (
            (
                await session.execute(
                    select(SalesOrder).where(
                        SalesOrder.customer_id == rig["low_credit_customer_id"]
                    )
                )
            )
            .scalars()
            .all()
        )
    assert count == []


@pytest.mark.asyncio
async def test_inactive_customer_rejected(rig):
    async with AsyncSessionLocal() as session:
        with pytest.raises(ConflictError):
            await create_sales_order(
                session,
                org_id=rig["org_id"],
                request=_request(rig, customer_key="inactive_customer_id"),
            )


@pytest.mark.asyncio
async def test_unknown_customer_rejected(rig):
    request = _request(rig)
    request = request.model_copy(update={"customer_id": uuid.uuid4()})
    async with AsyncSessionLocal() as session:
        with pytest.raises(NotFoundError):
            await create_sales_order(session, org_id=rig["org_id"], request=request)


@pytest.mark.asyncio
async def test_unknown_warehouse_rejected(rig):
    request = _request(rig)
    request = request.model_copy(update={"warehouse_id": uuid.uuid4()})
    async with AsyncSessionLocal() as session:
        with pytest.raises(NotFoundError):
            await create_sales_order(session, org_id=rig["org_id"], request=request)


@pytest.mark.asyncio
async def test_unknown_product_rejected(rig):
    async with AsyncSessionLocal() as session:
        with pytest.raises(NotFoundError):
            await create_sales_order(
                session,
                org_id=rig["org_id"],
                request=CreateSalesOrderRequest(
                    customer_id=rig["customer_id"],
                    warehouse_id=rig["warehouse_id"],
                    lines=[
                        SalesOrderLineInput(
                            product_id=uuid.uuid4(), quantity=Decimal("1"), unit_price=Decimal("10")
                        )
                    ],
                ),
            )


@pytest.mark.asyncio
async def test_inactive_product_rejected(rig):
    async with AsyncSessionLocal() as session:
        with pytest.raises(ConflictError):
            await create_sales_order(
                session,
                org_id=rig["org_id"],
                request=CreateSalesOrderRequest(
                    customer_id=rig["customer_id"],
                    warehouse_id=rig["warehouse_id"],
                    lines=[
                        SalesOrderLineInput(
                            product_id=rig["inactive_product_id"],
                            quantity=Decimal("1"),
                            unit_price=Decimal("10"),
                        )
                    ],
                ),
            )


@pytest.mark.asyncio
async def test_empty_lines_rejected(rig):
    async with AsyncSessionLocal() as session:
        with pytest.raises(ValidationError):
            await create_sales_order(
                session,
                org_id=rig["org_id"],
                request=CreateSalesOrderRequest(
                    customer_id=rig["customer_id"], warehouse_id=rig["warehouse_id"], lines=[]
                ),
            )


@pytest.mark.asyncio
async def test_document_numbers_are_sequential_and_gapless_across_orders(rig):
    async with AsyncSessionLocal() as session:
        await receive(
            session,
            lines=[
                InventoryLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("1000"),
                )
            ],
            ref_type="goods_receipt",
        )
        await session.commit()

    numbers = []
    for _ in range(2):
        async with AsyncSessionLocal() as session:
            result = await create_sales_order(
                session, org_id=rig["org_id"], request=_request(rig, quantity="1")
            )
            await session.commit()
            numbers.append(result.order_number)

    suffixes = [int(n.rsplit("-", 1)[-1]) for n in numbers]
    assert suffixes[1] == suffixes[0] + 1


@pytest.mark.asyncio
async def test_interstate_order_still_prices_and_reserves_correctly(rig):
    async with AsyncSessionLocal() as session:
        await receive(
            session,
            lines=[
                InventoryLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("10"),
                )
            ],
            ref_type="goods_receipt",
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        result = await create_sales_order(
            session,
            org_id=rig["org_id"],
            request=_request(rig, customer_key="inter_state_customer_id"),
        )
        await session.commit()

    assert result.status == "reserved"
    assert result.tax_total == Decimal("180.00")  # 18% IGST, same total as intra-state CGST+SGST


@pytest.mark.asyncio
async def test_quote_persists_without_touching_inventory(rig):
    async with AsyncSessionLocal() as session:
        await receive(
            session,
            lines=[
                InventoryLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("100"),
                )
            ],
            ref_type="goods_receipt",
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        request = _request(rig).model_copy(update={"is_quote": True})
        result = await create_sales_order(session, org_id=rig["org_id"], request=request)
        await session.commit()

    assert result.status == "draft"
    [line] = result.lines
    assert line.reserved_qty == Decimal("0")

    async with AsyncSessionLocal() as session:
        # a quote must not touch inventory at all — full 100 still available
        available = await inventory.get_available(
            session, product_id=rig["wire_id"], warehouse_id=rig["warehouse_id"]
        )
    assert available == Decimal("100")


@pytest.mark.asyncio
async def test_confirm_quote_reserves_stock_and_locks_original_price(rig):
    async with AsyncSessionLocal() as session:
        await receive(
            session,
            lines=[
                InventoryLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("100"),
                )
            ],
            ref_type="goods_receipt",
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        request = _request(rig).model_copy(update={"is_quote": True})
        quote = await create_sales_order(session, org_id=rig["org_id"], request=request)
        await session.commit()

    async with AsyncSessionLocal() as session:
        confirmed = await confirm_sales_order(
            session,
            org_id=rig["org_id"],
            sales_order_id=quote.sales_order_id,
            warehouse_id=rig["warehouse_id"],
        )
        await session.commit()

    assert confirmed.status == "reserved"
    assert confirmed.total == quote.total  # price locked at quote time, not re-priced
    [line] = confirmed.lines
    assert line.reserved_qty == Decimal("10")


@pytest.mark.asyncio
async def test_confirm_non_draft_order_rejected(rig):
    async with AsyncSessionLocal() as session:
        await receive(
            session,
            lines=[
                InventoryLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("100"),
                )
            ],
            ref_type="goods_receipt",
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        order = await create_sales_order(session, org_id=rig["org_id"], request=_request(rig))
        await session.commit()

    async with AsyncSessionLocal() as session:
        with pytest.raises(ConflictError):
            await confirm_sales_order(
                session,
                org_id=rig["org_id"],
                sales_order_id=order.sales_order_id,
                warehouse_id=rig["warehouse_id"],
            )


@pytest.mark.asyncio
async def test_confirm_unknown_order_rejected(rig):
    async with AsyncSessionLocal() as session:
        with pytest.raises(NotFoundError):
            await confirm_sales_order(
                session,
                org_id=rig["org_id"],
                sales_order_id=uuid.uuid4(),
                warehouse_id=rig["warehouse_id"],
            )


def _counter_sale_request(rig, *, quantity: str = "5") -> CreateCounterSaleRequest:
    return CreateCounterSaleRequest(
        customer_id=rig["customer_id"],
        warehouse_id=rig["warehouse_id"],
        lines=[
            SalesOrderLineInput(
                product_id=rig["wire_id"], quantity=Decimal(quantity), unit_price=Decimal("100")
            )
        ],
    )


@pytest.mark.asyncio
async def test_counter_sale_creates_invoice_and_reduces_stock_immediately(rig):
    async with AsyncSessionLocal() as session:
        await receive(
            session,
            lines=[
                InventoryLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("20"),
                )
            ],
            ref_type="goods_receipt",
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        result = await create_counter_sale(
            session, org_id=rig["org_id"], request=_counter_sale_request(rig)
        )
        await session.commit()

    assert result.status == "issued"
    assert result.total == Decimal("590")  # 500 + 18% GST
    [line] = result.lines
    assert line.quantity == Decimal("5")

    async with AsyncSessionLocal() as session:
        available = await inventory.get_available(
            session, product_id=rig["wire_id"], warehouse_id=rig["warehouse_id"]
        )
    assert available == Decimal("15")  # 20 received - 5 sold, no reservation involved


@pytest.mark.asyncio
async def test_counter_sale_is_all_or_nothing_on_insufficient_stock(rig):
    async with AsyncSessionLocal() as session:
        await receive(
            session,
            lines=[
                InventoryLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("2"),
                )
            ],
            ref_type="goods_receipt",
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        with pytest.raises(ConflictError):
            await create_counter_sale(
                session, org_id=rig["org_id"], request=_counter_sale_request(rig, quantity="5")
            )

    async with AsyncSessionLocal() as session:
        # nothing committed — stock untouched
        available = await inventory.get_available(
            session, product_id=rig["wire_id"], warehouse_id=rig["warehouse_id"]
        )
    assert available == Decimal("2")


@pytest.mark.asyncio
async def test_counter_sale_credit_limit_exceeded_rejected(rig):
    request = CreateCounterSaleRequest(
        customer_id=rig["low_credit_customer_id"],
        warehouse_id=rig["warehouse_id"],
        lines=[
            SalesOrderLineInput(
                product_id=rig["wire_id"], quantity=Decimal("5"), unit_price=Decimal("100")
            )
        ],
    )
    async with AsyncSessionLocal() as session:
        with pytest.raises(ConflictError):
            await create_counter_sale(session, org_id=rig["org_id"], request=request)
