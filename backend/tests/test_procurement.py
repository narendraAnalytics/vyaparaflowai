import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models.catalog import Product, ProductSupplier, Warehouse
from app.db.models.inventory import InventoryItem, StockLedger
from app.db.models.numbering import DocumentSequence
from app.db.models.org import Organization
from app.db.models.partners import Customer, Supplier
from app.db.models.purchase import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseRequisition,
    PurchaseRequisitionItem,
)
from app.db.models.sales import SalesOrder, SalesOrderItem
from app.db.session import AsyncSessionLocal
from app.services.inventory import AdjustmentLine, InventoryLine, adjust, receive
from app.services.procurement import (
    CreateRequisitionRequest,
    RequisitionLineInput,
    create_purchase_orders_from_requisition,
    create_requisition,
    detect_shortage_from_sales_order,
    detect_shortages,
    reorder_quantity,
    score_suppliers,
)
from app.services.sales import CreateSalesOrderRequest, SalesOrderLineInput, create_sales_order

TELANGANA = "36"


@pytest.fixture
async def rig():
    async with AsyncSessionLocal() as session:
        org = Organization(name=f"test-procurement-{uuid.uuid4()}", state_code=TELANGANA)
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
        pipe = Product(
            org_id=org.id,
            sku=f"TEST-PIPE-{uuid.uuid4().hex[:8]}",
            name="Test Pipe",
            hsn_code="3917",
            uom="PCS",
            gst_rate=Decimal("18"),
        )
        no_supplier_product = Product(
            org_id=org.id,
            sku=f"TEST-NOSUP-{uuid.uuid4().hex[:8]}",
            name="No Supplier Product",
            hsn_code="7318",
            uom="PCS",
            gst_rate=Decimal("18"),
        )
        customer = Customer(
            org_id=org.id,
            name="Test Customer",
            state_code=TELANGANA,
            credit_limit=Decimal("1000000"),
        )
        supplier_a = Supplier(
            org_id=org.id,
            name="Supplier A (pricier, faster, less reliable)",
            state_code=TELANGANA,
            lead_time_days=5,
            reliability_score=Decimal("70"),
        )
        supplier_b = Supplier(
            org_id=org.id,
            name="Supplier B (cheaper, slower, preferred, most reliable)",
            state_code=TELANGANA,
            lead_time_days=8,
            reliability_score=Decimal("95"),
        )
        supplier_c = Supplier(
            org_id=org.id,
            name="Supplier C (only pipe supplier)",
            state_code=TELANGANA,
            lead_time_days=3,
            reliability_score=Decimal("80"),
        )
        session.add_all(
            [
                warehouse,
                wire,
                pipe,
                no_supplier_product,
                customer,
                supplier_a,
                supplier_b,
                supplier_c,
            ]
        )
        await session.flush()

        session.add_all(
            [
                ProductSupplier(
                    product_id=wire.id,
                    supplier_id=supplier_a.id,
                    unit_price=Decimal("100"),
                    moq=50,
                    lead_time_days=5,
                    is_preferred=False,
                ),
                ProductSupplier(
                    product_id=wire.id,
                    supplier_id=supplier_b.id,
                    unit_price=Decimal("90"),
                    moq=20,
                    lead_time_days=8,
                    is_preferred=True,
                ),
                ProductSupplier(
                    product_id=pipe.id,
                    supplier_id=supplier_c.id,
                    unit_price=Decimal("200"),
                    moq=10,
                    lead_time_days=3,
                    is_preferred=False,
                ),
            ]
        )
        await session.commit()

        ids = {
            "org_id": org.id,
            "warehouse_id": warehouse.id,
            "wire_id": wire.id,
            "pipe_id": pipe.id,
            "no_supplier_product_id": no_supplier_product.id,
            "customer_id": customer.id,
            "supplier_a_id": supplier_a.id,
            "supplier_b_id": supplier_b.id,
            "supplier_c_id": supplier_c.id,
        }

    yield ids

    async with AsyncSessionLocal() as session:
        po_ids = (
            (
                await session.execute(
                    select(PurchaseOrder.id).where(PurchaseOrder.org_id == ids["org_id"])
                )
            )
            .scalars()
            .all()
        )
        if po_ids:
            await session.execute(
                delete(PurchaseOrderItem).where(PurchaseOrderItem.purchase_order_id.in_(po_ids))
            )
            await session.execute(delete(PurchaseOrder).where(PurchaseOrder.id.in_(po_ids)))
        pr_ids = (
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
        if pr_ids:
            await session.execute(
                delete(PurchaseRequisitionItem).where(
                    PurchaseRequisitionItem.purchase_requisition_id.in_(pr_ids)
                )
            )
            await session.execute(
                delete(PurchaseRequisition).where(PurchaseRequisition.id.in_(pr_ids))
            )
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
        await session.execute(
            delete(ProductSupplier).where(
                ProductSupplier.product_id.in_([ids["wire_id"], ids["pipe_id"]])
            )
        )
        await session.execute(
            delete(StockLedger).where(StockLedger.warehouse_id == ids["warehouse_id"])
        )
        await session.execute(
            delete(InventoryItem).where(InventoryItem.warehouse_id == ids["warehouse_id"])
        )
        await session.execute(
            delete(DocumentSequence).where(DocumentSequence.org_id == ids["org_id"])
        )
        await session.execute(delete(Customer).where(Customer.id == ids["customer_id"]))
        await session.execute(
            delete(Supplier).where(
                Supplier.id.in_([ids["supplier_a_id"], ids["supplier_b_id"], ids["supplier_c_id"]])
            )
        )
        await session.execute(
            delete(Product).where(
                Product.id.in_([ids["wire_id"], ids["pipe_id"], ids["no_supplier_product_id"]])
            )
        )
        await session.execute(delete(Warehouse).where(Warehouse.id == ids["warehouse_id"]))
        await session.execute(delete(Organization).where(Organization.id == ids["org_id"]))
        await session.commit()


def test_reorder_quantity_pure_formula():
    result = reorder_quantity(
        shortage=Decimal("10"),
        avg_daily_sales=Decimal("2"),
        lead_time_days=5,
        safety_stock=Decimal("3"),
    )
    assert result == Decimal("23")  # 10 + 2*5 + 3


def test_reorder_quantity_rejects_negative_inputs():
    with pytest.raises(ValueError, match="non-negative"):
        reorder_quantity(
            shortage=Decimal("-1"),
            avg_daily_sales=Decimal("0"),
            lead_time_days=0,
            safety_stock=Decimal("0"),
        )


@pytest.mark.asyncio
async def test_score_suppliers_ranks_cheaper_preferred_reliable_supplier_first(rig):
    async with AsyncSessionLocal() as session:
        scores = await score_suppliers(session, product_id=rig["wire_id"])

    assert len(scores) == 2
    assert scores[0].supplier_id == rig["supplier_b_id"]
    assert scores[0].is_preferred is True
    assert scores[0].total_score > scores[1].total_score
    assert any("preferred" in reason.lower() for reason in scores[0].reasoning)


@pytest.mark.asyncio
async def test_score_suppliers_empty_when_none_configured(rig):
    async with AsyncSessionLocal() as session:
        scores = await score_suppliers(session, product_id=rig["no_supplier_product_id"])
    assert scores == []


@pytest.mark.asyncio
async def test_detect_shortages_finds_products_below_reorder_level(rig):
    async with AsyncSessionLocal() as session:
        await receive(
            session,
            lines=[
                InventoryLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
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
                    InventoryItem.product_id == rig["wire_id"],
                    InventoryItem.warehouse_id == rig["warehouse_id"],
                )
            )
        ).scalar_one()
        item.reorder_level = Decimal("30")
        item.safety_stock = Decimal("10")
        await session.commit()

    async with AsyncSessionLocal() as session:
        lines = await detect_shortages(
            session, org_id=rig["org_id"], warehouse_id=rig["warehouse_id"]
        )

    [wire_shortage] = [line for line in lines if line.product_id == rig["wire_id"]]
    assert wire_shortage.available == Decimal("5")
    assert wire_shortage.shortage_qty == Decimal("25")  # 30 - 5
    assert wire_shortage.recommended_qty == Decimal("35")  # 25 + 0*lead_time + 10 safety stock


@pytest.mark.asyncio
async def test_detect_shortage_from_sales_order_reports_unfulfilled_lines(rig):
    async with AsyncSessionLocal() as session:
        await receive(
            session,
            lines=[
                InventoryLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("5"),
                )
            ],
            ref_type="goods_receipt",
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        order = await create_sales_order(
            session,
            org_id=rig["org_id"],
            request=CreateSalesOrderRequest(
                customer_id=rig["customer_id"],
                warehouse_id=rig["warehouse_id"],
                lines=[
                    SalesOrderLineInput(
                        product_id=rig["wire_id"], quantity=Decimal("10"), unit_price=Decimal("100")
                    )
                ],
            ),
        )
        await session.commit()
    assert order.status == "partially_reserved"

    async with AsyncSessionLocal() as session:
        shortages = await detect_shortage_from_sales_order(
            session,
            org_id=rig["org_id"],
            sales_order_id=order.sales_order_id,
            warehouse_id=rig["warehouse_id"],
        )

    [line] = shortages
    assert line.shortage_qty == Decimal("5")  # ordered 10, reserved 5


@pytest.mark.asyncio
async def test_detect_shortage_from_sales_order_rejects_draft_quote(rig):
    async with AsyncSessionLocal() as session:
        quote = await create_sales_order(
            session,
            org_id=rig["org_id"],
            request=CreateSalesOrderRequest(
                customer_id=rig["customer_id"],
                warehouse_id=rig["warehouse_id"],
                lines=[
                    SalesOrderLineInput(
                        product_id=rig["wire_id"], quantity=Decimal("10"), unit_price=Decimal("100")
                    )
                ],
                is_quote=True,
            ),
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        with pytest.raises(ConflictError):
            await detect_shortage_from_sales_order(
                session,
                org_id=rig["org_id"],
                sales_order_id=quote.sales_order_id,
                warehouse_id=rig["warehouse_id"],
            )


@pytest.mark.asyncio
async def test_detect_shortage_from_unknown_sales_order_rejected(rig):
    async with AsyncSessionLocal() as session:
        with pytest.raises(NotFoundError):
            await detect_shortage_from_sales_order(
                session,
                org_id=rig["org_id"],
                sales_order_id=uuid.uuid4(),
                warehouse_id=rig["warehouse_id"],
            )


@pytest.mark.asyncio
async def test_create_requisition_persists_lines(rig):
    async with AsyncSessionLocal() as session:
        result = await create_requisition(
            session,
            org_id=rig["org_id"],
            request=CreateRequisitionRequest(
                lines=[
                    RequisitionLineInput(
                        product_id=rig["wire_id"], quantity=Decimal("40"), reason="reorder"
                    )
                ]
            ),
        )
        await session.commit()

    assert result.status == "pending_approval"
    assert result.requisition_number.startswith("PR-")

    async with AsyncSessionLocal() as session:
        items = (
            (
                await session.execute(
                    select(PurchaseRequisitionItem).where(
                        PurchaseRequisitionItem.purchase_requisition_id
                        == result.purchase_requisition_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(items) == 1
    assert items[0].quantity == Decimal("40")


@pytest.mark.asyncio
async def test_create_requisition_empty_lines_rejected(rig):
    async with AsyncSessionLocal() as session:
        with pytest.raises(ValidationError):
            await create_requisition(
                session, org_id=rig["org_id"], request=CreateRequisitionRequest(lines=[])
            )


@pytest.mark.asyncio
async def test_create_requisition_unknown_product_rejected(rig):
    async with AsyncSessionLocal() as session:
        with pytest.raises(NotFoundError):
            await create_requisition(
                session,
                org_id=rig["org_id"],
                request=CreateRequisitionRequest(
                    lines=[RequisitionLineInput(product_id=uuid.uuid4(), quantity=Decimal("1"))]
                ),
            )


@pytest.mark.asyncio
async def test_create_requisition_unknown_triggering_sales_order_rejected(rig):
    async with AsyncSessionLocal() as session:
        with pytest.raises(NotFoundError):
            await create_requisition(
                session,
                org_id=rig["org_id"],
                request=CreateRequisitionRequest(
                    lines=[RequisitionLineInput(product_id=rig["wire_id"], quantity=Decimal("1"))],
                    triggered_by_sales_order_id=uuid.uuid4(),
                ),
            )


@pytest.mark.asyncio
async def test_create_po_rounds_to_moq_and_marks_requisition_converted(rig):
    async with AsyncSessionLocal() as session:
        requisition = await create_requisition(
            session,
            org_id=rig["org_id"],
            request=CreateRequisitionRequest(
                lines=[RequisitionLineInput(product_id=rig["wire_id"], quantity=Decimal("22"))]
            ),
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        [po] = await create_purchase_orders_from_requisition(
            session,
            org_id=rig["org_id"],
            purchase_requisition_id=requisition.purchase_requisition_id,
        )
        await session.commit()

    assert po.supplier_id == rig["supplier_b_id"]  # best-scored supplier for wire
    [line] = po.lines
    assert line.requisitioned_qty == Decimal("22")
    assert line.ordered_qty == Decimal("40")  # ceil(22/20)*20, supplier B's MOQ is 20
    assert line.unit_price == Decimal("90.00")

    async with AsyncSessionLocal() as session:
        requisition_row = await session.get(
            PurchaseRequisition, requisition.purchase_requisition_id
        )
    assert requisition_row.status == "converted"


@pytest.mark.asyncio
async def test_create_po_groups_lines_by_best_supplier_into_multiple_pos(rig):
    async with AsyncSessionLocal() as session:
        requisition = await create_requisition(
            session,
            org_id=rig["org_id"],
            request=CreateRequisitionRequest(
                lines=[
                    RequisitionLineInput(product_id=rig["wire_id"], quantity=Decimal("20")),
                    RequisitionLineInput(product_id=rig["pipe_id"], quantity=Decimal("5")),
                ]
            ),
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        purchase_orders = await create_purchase_orders_from_requisition(
            session,
            org_id=rig["org_id"],
            purchase_requisition_id=requisition.purchase_requisition_id,
        )
        await session.commit()

    assert len(purchase_orders) == 2
    supplier_ids = {po.supplier_id for po in purchase_orders}
    assert supplier_ids == {rig["supplier_b_id"], rig["supplier_c_id"]}


@pytest.mark.asyncio
async def test_create_po_no_supplier_configured_rejected(rig):
    async with AsyncSessionLocal() as session:
        requisition = await create_requisition(
            session,
            org_id=rig["org_id"],
            request=CreateRequisitionRequest(
                lines=[
                    RequisitionLineInput(
                        product_id=rig["no_supplier_product_id"], quantity=Decimal("1")
                    )
                ]
            ),
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        with pytest.raises(ConflictError):
            await create_purchase_orders_from_requisition(
                session,
                org_id=rig["org_id"],
                purchase_requisition_id=requisition.purchase_requisition_id,
            )


@pytest.mark.asyncio
async def test_create_po_already_converted_requisition_rejected(rig):
    async with AsyncSessionLocal() as session:
        requisition = await create_requisition(
            session,
            org_id=rig["org_id"],
            request=CreateRequisitionRequest(
                lines=[RequisitionLineInput(product_id=rig["wire_id"], quantity=Decimal("20"))]
            ),
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        await create_purchase_orders_from_requisition(
            session,
            org_id=rig["org_id"],
            purchase_requisition_id=requisition.purchase_requisition_id,
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        with pytest.raises(ConflictError):
            await create_purchase_orders_from_requisition(
                session,
                org_id=rig["org_id"],
                purchase_requisition_id=requisition.purchase_requisition_id,
            )


@pytest.mark.asyncio
async def test_create_po_unknown_requisition_rejected(rig):
    async with AsyncSessionLocal() as session:
        with pytest.raises(NotFoundError):
            await create_purchase_orders_from_requisition(
                session, org_id=rig["org_id"], purchase_requisition_id=uuid.uuid4()
            )


@pytest.mark.asyncio
async def test_avg_daily_sales_feeds_recommended_qty_via_adjust(rig):
    # simulate 30 days of no ledger history except a single large adjustment
    # (not an ISSUE movement) — recommended_qty should NOT count it as sales.
    async with AsyncSessionLocal() as session:
        await adjust(
            session,
            lines=[
                AdjustmentLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    qty_delta=Decimal("50"),
                )
            ],
            ref_type="stock_take",
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        item = (
            await session.execute(
                select(InventoryItem).where(
                    InventoryItem.product_id == rig["wire_id"],
                    InventoryItem.warehouse_id == rig["warehouse_id"],
                )
            )
        ).scalar_one()
        item.reorder_level = Decimal("60")
        await session.commit()

    async with AsyncSessionLocal() as session:
        lines = await detect_shortages(
            session, org_id=rig["org_id"], warehouse_id=rig["warehouse_id"]
        )
    [wire_shortage] = [line for line in lines if line.product_id == rig["wire_id"]]
    # shortage=10, avg_daily_sales=0 (an adjustment is not a sale), safety_stock=0
    assert wire_shortage.recommended_qty == wire_shortage.shortage_qty
