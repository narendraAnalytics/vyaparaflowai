import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

from .enums import StockMovementType, check_values
from .mixins import TimestampMixin, UUIDPkMixin


class InventoryItem(UUIDPkMixin, TimestampMixin, Base):
    """Current stock position for one product at one warehouse.

    Never UPDATE on_hand/reserved directly from application code — every
    change must go through a StockLedger row in the same transaction (see
    services/inventory.py, Phase 2). This table is the current-state
    snapshot; stock_ledger is the append-only source of truth.
    """

    __tablename__ = "inventory_items"
    __table_args__ = (
        UniqueConstraint(
            "product_id", "warehouse_id", name="uq_inventory_items_product_id_warehouse_id"
        ),
        CheckConstraint("on_hand >= 0", name="ck_inventory_items_on_hand_non_negative"),
        CheckConstraint("reserved >= 0", name="ck_inventory_items_reserved_non_negative"),
        CheckConstraint("reserved <= on_hand", name="ck_inventory_items_reserved_lte_on_hand"),
    )

    product_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    on_hand: Mapped[float] = mapped_column(Numeric(14, 3), default=0, nullable=False)
    reserved: Mapped[float] = mapped_column(Numeric(14, 3), default=0, nullable=False)
    available: Mapped[float] = mapped_column(
        Numeric(14, 3), Computed("on_hand - reserved", persisted=True)
    )
    reorder_level: Mapped[float] = mapped_column(Numeric(14, 3), default=0, nullable=False)
    safety_stock: Mapped[float] = mapped_column(Numeric(14, 3), default=0, nullable=False)


class StockLedger(UUIDPkMixin, Base):
    """Append-only. One row per unit of stock movement, ever. No updated_at,
    no UPDATE, no DELETE — this is the audit trail everything else derives
    from.
    """

    __tablename__ = "stock_ledger"
    __table_args__ = (
        CheckConstraint(
            f"movement_type IN ({check_values(*StockMovementType)})",
            name="ck_stock_ledger_movement_type",
        ),
    )

    product_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    movement_type: Mapped[str] = mapped_column(String(20), nullable=False)
    qty_delta: Mapped[float] = mapped_column(Numeric(14, 3), nullable=False)
    balance_after: Mapped[float] = mapped_column(Numeric(14, 3), nullable=False)
    ref_type: Mapped[str] = mapped_column(String(50), nullable=False)
    ref_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    note: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
