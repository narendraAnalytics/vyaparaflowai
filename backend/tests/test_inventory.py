import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.exceptions import ConflictError
from app.db.models.catalog import Product, Warehouse
from app.db.models.inventory import InventoryItem, StockLedger
from app.db.models.org import Organization
from app.db.session import AsyncSessionLocal
from app.services.inventory import (
    AdjustmentLine,
    InventoryLine,
    adjust,
    check_availability,
    issue,
    receive,
    release,
    reserve,
)


@pytest.fixture
async def rig():
    """A throwaway org + warehouse + two products, cleaned up after the test."""
    async with AsyncSessionLocal() as session:
        org = Organization(name=f"test-inventory-{uuid.uuid4()}")
        session.add(org)
        await session.flush()
        warehouse = Warehouse(
            org_id=org.id, code=f"WH-{uuid.uuid4().hex[:8]}", name="Test Warehouse"
        )
        session.add(warehouse)
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
        session.add_all([wire, pipe])
        await session.commit()
        ids = {
            "org_id": org.id,
            "warehouse_id": warehouse.id,
            "wire_id": wire.id,
            "pipe_id": pipe.id,
        }

    yield ids

    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(StockLedger).where(StockLedger.warehouse_id == ids["warehouse_id"])
        )
        await session.execute(
            delete(InventoryItem).where(InventoryItem.warehouse_id == ids["warehouse_id"])
        )
        await session.execute(
            delete(Product).where(Product.id.in_([ids["wire_id"], ids["pipe_id"]]))
        )
        await session.execute(delete(Warehouse).where(Warehouse.id == ids["warehouse_id"]))
        await session.execute(delete(Organization).where(Organization.id == ids["org_id"]))
        await session.commit()


@pytest.mark.asyncio
async def test_check_availability_false_when_no_stock(rig):
    async with AsyncSessionLocal() as session:
        available = await check_availability(
            session,
            product_id=rig["wire_id"],
            warehouse_id=rig["warehouse_id"],
            quantity=Decimal("1"),
        )
    assert available is False


@pytest.mark.asyncio
async def test_receive_increases_on_hand_and_writes_ledger(rig):
    async with AsyncSessionLocal() as session:
        results = await receive(
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
    assert results[0].on_hand == Decimal("100")
    assert results[0].reserved == Decimal("0")

    async with AsyncSessionLocal() as session:
        ledger = (
            await session.execute(
                select(StockLedger).where(StockLedger.product_id == rig["wire_id"])
            )
        ).scalar_one()
    assert ledger.movement_type == "receipt"
    assert ledger.qty_delta == Decimal("100")
    assert ledger.balance_after == Decimal("100")
    assert ledger.ref_type == "goods_receipt"


@pytest.mark.asyncio
async def test_reserve_then_release_round_trips(rig):
    async with AsyncSessionLocal() as session:
        await receive(
            session,
            lines=[
                InventoryLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("50"),
                )
            ],
            ref_type="goods_receipt",
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        [result] = await reserve(
            session,
            lines=[
                InventoryLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("20"),
                )
            ],
            ref_type="sales_order",
        )
        await session.commit()
    assert result.on_hand == Decimal("50")
    assert result.reserved == Decimal("20")

    async with AsyncSessionLocal() as session:
        available = await check_availability(
            session,
            product_id=rig["wire_id"],
            warehouse_id=rig["warehouse_id"],
            quantity=Decimal("31"),
        )
        assert available is False

    async with AsyncSessionLocal() as session:
        [result] = await release(
            session,
            lines=[
                InventoryLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("20"),
                )
            ],
            ref_type="sales_order",
        )
        await session.commit()
    assert result.reserved == Decimal("0")


@pytest.mark.asyncio
async def test_reserve_insufficient_stock_raises_conflict_and_commits_nothing(rig):
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
        with pytest.raises(ConflictError):
            await reserve(
                session,
                lines=[
                    InventoryLine(
                        product_id=rig["wire_id"],
                        warehouse_id=rig["warehouse_id"],
                        quantity=Decimal("6"),
                    )
                ],
                ref_type="sales_order",
            )
        await session.rollback()

    async with AsyncSessionLocal() as session:
        available = await check_availability(
            session,
            product_id=rig["wire_id"],
            warehouse_id=rig["warehouse_id"],
            quantity=Decimal("5"),
        )
    assert available is True  # untouched — the failed reserve left no trace


@pytest.mark.asyncio
async def test_release_more_than_reserved_raises_conflict(rig):
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
        await reserve(
            session,
            lines=[
                InventoryLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("3"),
                )
            ],
            ref_type="sales_order",
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        with pytest.raises(ConflictError):
            await release(
                session,
                lines=[
                    InventoryLine(
                        product_id=rig["wire_id"],
                        warehouse_id=rig["warehouse_id"],
                        quantity=Decimal("4"),
                    )
                ],
                ref_type="sales_order",
            )


@pytest.mark.asyncio
async def test_issue_decreases_on_hand_and_reserved_together(rig):
    async with AsyncSessionLocal() as session:
        await receive(
            session,
            lines=[
                InventoryLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("40"),
                )
            ],
            ref_type="goods_receipt",
        )
        await reserve(
            session,
            lines=[
                InventoryLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("15"),
                )
            ],
            ref_type="sales_order",
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        [result] = await issue(
            session,
            lines=[
                InventoryLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("15"),
                )
            ],
            ref_type="delivery",
        )
        await session.commit()
    assert result.on_hand == Decimal("25")
    assert result.reserved == Decimal("0")


@pytest.mark.asyncio
async def test_issue_more_than_reserved_raises_conflict(rig):
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
        with pytest.raises(ConflictError):
            await issue(
                session,
                lines=[
                    InventoryLine(
                        product_id=rig["wire_id"],
                        warehouse_id=rig["warehouse_id"],
                        quantity=Decimal("1"),
                    )
                ],
                ref_type="delivery",
            )


@pytest.mark.asyncio
async def test_adjust_positive_and_negative(rig):
    async with AsyncSessionLocal() as session:
        [up] = await adjust(
            session,
            lines=[
                AdjustmentLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    qty_delta=Decimal("30"),
                )
            ],
            ref_type="stock_take",
        )
        await session.commit()
    assert up.on_hand == Decimal("30")

    async with AsyncSessionLocal() as session:
        [down] = await adjust(
            session,
            lines=[
                AdjustmentLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    qty_delta=Decimal("-5"),
                )
            ],
            ref_type="damage",
        )
        await session.commit()
    assert down.on_hand == Decimal("25")


@pytest.mark.asyncio
async def test_adjust_below_reserved_raises_conflict(rig):
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
        await reserve(
            session,
            lines=[
                InventoryLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("8"),
                )
            ],
            ref_type="sales_order",
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        with pytest.raises(ConflictError):
            await adjust(
                session,
                lines=[
                    AdjustmentLine(
                        product_id=rig["wire_id"],
                        warehouse_id=rig["warehouse_id"],
                        qty_delta=Decimal("-5"),
                    )
                ],
                ref_type="stock_take",
            )


def test_adjustment_line_rejects_zero_delta():
    with pytest.raises(ValueError, match="non-zero"):
        AdjustmentLine(product_id=uuid.uuid4(), warehouse_id=uuid.uuid4(), qty_delta=Decimal("0"))


@pytest.mark.asyncio
async def test_multi_line_reservation_sorted_lock_order_still_applies_both(rig):
    async with AsyncSessionLocal() as session:
        await receive(
            session,
            lines=[
                InventoryLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("10"),
                ),
                InventoryLine(
                    product_id=rig["pipe_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("10"),
                ),
            ],
            ref_type="goods_receipt",
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        # Deliberately unsorted input order — the service must sort
        # internally, but both lines must still be applied correctly.
        results = await reserve(
            session,
            lines=[
                InventoryLine(
                    product_id=rig["pipe_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("3"),
                ),
                InventoryLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("4"),
                ),
            ],
            ref_type="sales_order",
        )
        await session.commit()
    by_product = {r.product_id: r for r in results}
    assert by_product[rig["wire_id"]].reserved == Decimal("4")
    assert by_product[rig["pipe_id"]].reserved == Decimal("3")


@pytest.mark.asyncio
async def test_100_concurrent_reservations_never_oversell(rig):
    """The proof: 100 genuinely concurrent reservation attempts (each its
    own DB connection/transaction, not one session calling itself) against
    a row with exactly 50 units of stock. Exactly 50 must succeed, exactly
    50 must fail with ConflictError, and the final row must show on_hand
    unchanged and reserved == 50 — no oversell, no lost update.

    Uses a dedicated engine with a pool large enough to hand out 100 real
    connections at once; the shared app engine's default pool (5 + 10
    overflow) would queue most of these behind a handful of connections,
    which would still pass but wouldn't actually prove concurrent-row
    contention is handled — see backend/CLAUDE.md's testing notes.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_size=100, max_overflow=0)
    SessionLocal = async_sessionmaker(bind=engine, autoflush=False, autocommit=False)

    async with SessionLocal() as session:
        await receive(
            session,
            lines=[
                InventoryLine(
                    product_id=rig["wire_id"],
                    warehouse_id=rig["warehouse_id"],
                    quantity=Decimal("50"),
                )
            ],
            ref_type="goods_receipt",
        )
        await session.commit()

    async def attempt() -> str:
        async with SessionLocal() as session:
            try:
                await reserve(
                    session,
                    lines=[
                        InventoryLine(
                            product_id=rig["wire_id"],
                            warehouse_id=rig["warehouse_id"],
                            quantity=Decimal("1"),
                        )
                    ],
                    ref_type="concurrency_test",
                )
            except ConflictError:
                return "conflict"
            else:
                await session.commit()
                return "ok"

    try:
        outcomes = await asyncio.gather(*[attempt() for _ in range(100)])
    finally:
        await engine.dispose()

    assert outcomes.count("ok") == 50, "oversold or undersold — expected exactly 50 successes"
    assert outcomes.count("conflict") == 50

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(InventoryItem).where(
                    InventoryItem.product_id == rig["wire_id"],
                    InventoryItem.warehouse_id == rig["warehouse_id"],
                )
            )
        ).scalar_one()
    assert row.on_hand == Decimal("50")
    assert row.reserved == Decimal("50")
    assert row.available == Decimal("0")
