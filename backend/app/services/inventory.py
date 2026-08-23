"""Inventory movements — the sole place on_hand/reserved ever change.

Every mutation (reserve/release/receive/issue/adjust) processes a list of
lines — a whole order/GRN/delivery/stock-take, not just one SKU — inside
ONE caller-owned transaction (this module never commits; mirrors
services/numbering.py). Each line is applied as a single atomic
`UPDATE inventory_items SET ... WHERE <availability guard> RETURNING ...`:
Postgres takes the row's lock implicitly for the statement's duration and
the WHERE guard re-checks the *current* committed value once that lock is
acquired, so overselling is structurally impossible — no separate
`SELECT ... FOR UPDATE` step is needed even though the effect is the same
(current 2026 guidance favors this single-statement form for a plain
counter mutation; see the module's test file for the concurrency proof).
Zero rows returned means the guard failed (insufficient stock / over-
release / over-issue / adjustment would go negative), and callers get a
`ConflictError`.

Deadlock avoidance: when a transaction touches MULTIPLE inventory_items
rows (a 5-line sales order, say), Postgres acquires each row's lock in the
order the UPDATEs are actually issued. Two concurrent transactions that
touch the same products in different order can deadlock (Postgres detects
this within `deadlock_timeout` and aborts one with error 40P01 — the
caller must retry the whole transaction, not just one statement). This
module removes that risk entirely by always sorting lines by
`(product_id, warehouse_id)` before applying them, so every transaction
that goes through this module acquires locks in the same global order.

Every mutation also writes one append-only `stock_ledger` row per line, in
the same transaction as its `inventory_items` update.  `balance_after`
records whichever column the movement actually changed: RESERVE/RELEASE
movements record `reserved` after the change; RECEIPT/ISSUE/ADJUSTMENT
record `on_hand` after the change.

Trust boundary: this module assumes `product_id`/`warehouse_id` already
reference real rows (the caller fetched/validated them earlier in the same
transaction, same convention as services/numbering.py trusting `org_id`).
An invalid id surfaces as a raw FK-violation IntegrityError, not a friendly
4xx — that's intentional; catching and prettifying it is the caller's job.

Known production gap (not built here — would need a new table, and
Phase 1's schema is locked/done): reservations don't expire. A real
e-commerce/warehouse system typically auto-releases a stale reservation
after N minutes (cart abandonment, unconfirmed order) via a background
sweep job. `inventory_items.reserved` is just a running counter with no
per-reservation row/TTL to sweep — if this is needed, it belongs on
sales_orders (Phase 2.7) as a status+timestamp-driven job, not here.
"""

import uuid
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import Row, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.db.models.enums import StockMovementType
from app.db.models.inventory import InventoryItem, StockLedger

ZERO = Decimal("0")


class InventoryLine(BaseModel):
    product_id: uuid.UUID
    warehouse_id: uuid.UUID
    quantity: Decimal = Field(gt=0)


class AdjustmentLine(BaseModel):
    product_id: uuid.UUID
    warehouse_id: uuid.UUID
    qty_delta: Decimal

    def __init__(self, **data: object) -> None:
        super().__init__(**data)
        if self.qty_delta == 0:
            raise ValueError("qty_delta must be non-zero")


class InventoryMutationResult(BaseModel):
    product_id: uuid.UUID
    warehouse_id: uuid.UUID
    on_hand: Decimal
    reserved: Decimal


def _sort_key(line: InventoryLine | AdjustmentLine) -> tuple[str, str]:
    return (str(line.product_id), str(line.warehouse_id))


async def _ensure_row(
    session: AsyncSession, *, product_id: uuid.UUID, warehouse_id: uuid.UUID
) -> None:
    await session.execute(
        pg_insert(InventoryItem)
        .values(product_id=product_id, warehouse_id=warehouse_id, on_hand=0, reserved=0)
        .on_conflict_do_nothing(index_elements=["product_id", "warehouse_id"])
    )


async def get_available(
    session: AsyncSession, *, product_id: uuid.UUID, warehouse_id: uuid.UUID
) -> Decimal:
    result = await session.execute(
        select(InventoryItem.available).where(
            InventoryItem.product_id == product_id, InventoryItem.warehouse_id == warehouse_id
        )
    )
    available = result.scalar_one_or_none()
    return Decimal(available) if available is not None else ZERO


async def check_availability(
    session: AsyncSession, *, product_id: uuid.UUID, warehouse_id: uuid.UUID, quantity: Decimal
) -> bool:
    available = await get_available(session, product_id=product_id, warehouse_id=warehouse_id)
    return available >= quantity


async def _apply(
    session: AsyncSession,
    *,
    lines: list[InventoryLine] | list[AdjustmentLine],
    movement_type: StockMovementType,
    ref_type: str,
    ref_id: uuid.UUID | None,
    note: str | None,
    from_reservation: bool = True,
) -> list[InventoryMutationResult]:
    results: list[InventoryMutationResult] = []
    for line in sorted(lines, key=_sort_key):
        await _ensure_row(session, product_id=line.product_id, warehouse_id=line.warehouse_id)
        stmt, qty_delta = _build_statement(line, movement_type, from_reservation=from_reservation)
        row = (await session.execute(stmt)).first()
        if row is None:
            raise ConflictError(
                f"{movement_type.value} rejected for product {line.product_id} at "
                f"warehouse {line.warehouse_id}: insufficient stock or would violate "
                f"reserved <= on_hand / on_hand >= 0"
            )
        on_hand, reserved = _unpack(row)
        balance_after = reserved if movement_type in _RESERVED_MOVEMENTS else on_hand
        session.add(
            StockLedger(
                product_id=line.product_id,
                warehouse_id=line.warehouse_id,
                movement_type=movement_type.value,
                qty_delta=qty_delta,
                balance_after=balance_after,
                ref_type=ref_type,
                ref_id=ref_id,
                note=note,
            )
        )
        results.append(
            InventoryMutationResult(
                product_id=line.product_id,
                warehouse_id=line.warehouse_id,
                on_hand=on_hand,
                reserved=reserved,
            )
        )
    await session.flush()
    return results


_RESERVED_MOVEMENTS = {StockMovementType.RESERVE, StockMovementType.RELEASE}


def _unpack(row: Row) -> tuple[Decimal, Decimal]:
    # every statement returns (on_hand, reserved), in that order — see
    # _build_statement's .returning() clauses.
    return Decimal(row[0]), Decimal(row[1])


def _build_statement(
    line: InventoryLine | AdjustmentLine,
    movement_type: StockMovementType,
    *,
    from_reservation: bool = True,
):
    item = InventoryItem
    if movement_type is StockMovementType.RESERVE:
        assert isinstance(line, InventoryLine)
        stmt = (
            update(item)
            .where(
                item.product_id == line.product_id,
                item.warehouse_id == line.warehouse_id,
                item.available >= line.quantity,
            )
            .values(reserved=item.reserved + line.quantity)
            .returning(item.on_hand, item.reserved)
        )
        return stmt, line.quantity

    if movement_type is StockMovementType.RELEASE:
        assert isinstance(line, InventoryLine)
        stmt = (
            update(item)
            .where(
                item.product_id == line.product_id,
                item.warehouse_id == line.warehouse_id,
                item.reserved >= line.quantity,
            )
            .values(reserved=item.reserved - line.quantity)
            .returning(item.on_hand, item.reserved)
        )
        return stmt, -line.quantity

    if movement_type is StockMovementType.RECEIPT:
        assert isinstance(line, InventoryLine)
        stmt = (
            update(item)
            .where(item.product_id == line.product_id, item.warehouse_id == line.warehouse_id)
            .values(on_hand=item.on_hand + line.quantity)
            .returning(item.on_hand, item.reserved)
        )
        return stmt, line.quantity

    if movement_type is StockMovementType.ISSUE:
        assert isinstance(line, InventoryLine)
        if from_reservation:
            # dispatch against a prior reservation: both columns move together
            stmt = (
                update(item)
                .where(
                    item.product_id == line.product_id,
                    item.warehouse_id == line.warehouse_id,
                    item.reserved >= line.quantity,
                )
                .values(
                    on_hand=item.on_hand - line.quantity, reserved=item.reserved - line.quantity
                )
                .returning(item.on_hand, item.reserved)
            )
        else:
            # direct/counter sale: nothing was ever reserved, only on_hand moves
            stmt = (
                update(item)
                .where(
                    item.product_id == line.product_id,
                    item.warehouse_id == line.warehouse_id,
                    item.on_hand >= line.quantity,
                )
                .values(on_hand=item.on_hand - line.quantity)
                .returning(item.on_hand, item.reserved)
            )
        return stmt, -line.quantity

    if movement_type is StockMovementType.ADJUSTMENT:
        assert isinstance(line, AdjustmentLine)
        stmt = (
            update(item)
            .where(
                item.product_id == line.product_id,
                item.warehouse_id == line.warehouse_id,
                (item.on_hand + line.qty_delta) >= 0,
                (item.on_hand + line.qty_delta) >= item.reserved,
            )
            .values(on_hand=item.on_hand + line.qty_delta)
            .returning(item.on_hand, item.reserved)
        )
        return stmt, line.qty_delta

    raise ValueError(f"unsupported movement_type: {movement_type!r}")


async def reserve(
    session: AsyncSession,
    *,
    lines: list[InventoryLine],
    ref_type: str,
    ref_id: uuid.UUID | None = None,
    note: str | None = None,
) -> list[InventoryMutationResult]:
    """Reserve stock for every line of an order, atomically and deadlock-safe.

    Raises ConflictError (no partial state committed — the caller must
    still roll back the transaction) on the first line with insufficient
    available stock.
    """
    return await _apply(
        session,
        lines=lines,
        movement_type=StockMovementType.RESERVE,
        ref_type=ref_type,
        ref_id=ref_id,
        note=note,
    )


async def release(
    session: AsyncSession,
    *,
    lines: list[InventoryLine],
    ref_type: str,
    ref_id: uuid.UUID | None = None,
    note: str | None = None,
) -> list[InventoryMutationResult]:
    """Release a prior reservation (order cancelled, line removed, etc.)."""
    return await _apply(
        session,
        lines=lines,
        movement_type=StockMovementType.RELEASE,
        ref_type=ref_type,
        ref_id=ref_id,
        note=note,
    )


async def receive(
    session: AsyncSession,
    *,
    lines: list[InventoryLine],
    ref_type: str,
    ref_id: uuid.UUID | None = None,
    note: str | None = None,
) -> list[InventoryMutationResult]:
    """Goods receipt: increase on_hand. Always succeeds (no upper bound)."""
    return await _apply(
        session,
        lines=lines,
        movement_type=StockMovementType.RECEIPT,
        ref_type=ref_type,
        ref_id=ref_id,
        note=note,
    )


async def issue(
    session: AsyncSession,
    *,
    lines: list[InventoryLine],
    ref_type: str,
    ref_id: uuid.UUID | None = None,
    note: str | None = None,
    from_reservation: bool = True,
) -> list[InventoryMutationResult]:
    """Stock physically leaves the warehouse — a delivery dispatch or a
    direct/counter sale.

    `from_reservation=True` (default): dispatch against a prior
    reservation — decrease on_hand AND reserved together, guarded by
    `reserved >= quantity`. `from_reservation=False`: a direct sale with no
    prior reservation (walk-in/counter sale) — decrease on_hand only,
    guarded by `on_hand >= quantity`; `reserved` is untouched. Both record
    the same StockMovementType.ISSUE in the ledger — the distinction is in
    which columns moved and which guard applied, not in the movement type.
    """
    return await _apply(
        session,
        lines=lines,
        movement_type=StockMovementType.ISSUE,
        ref_type=ref_type,
        ref_id=ref_id,
        note=note,
        from_reservation=from_reservation,
    )


async def adjust(
    session: AsyncSession,
    *,
    lines: list[AdjustmentLine],
    ref_type: str = "stock_adjustment",
    ref_id: uuid.UUID | None = None,
    note: str | None = None,
) -> list[InventoryMutationResult]:
    """Stock take / damage / shrinkage correction: arbitrary +/- delta to
    on_hand, never touches reserved. Rejected if it would push on_hand
    below reserved or below zero.
    """
    return await _apply(
        session,
        lines=lines,
        movement_type=StockMovementType.ADJUSTMENT,
        ref_type=ref_type,
        ref_id=ref_id,
        note=note,
    )
