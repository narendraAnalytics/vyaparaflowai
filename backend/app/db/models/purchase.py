import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

from .enums import (
    MatchVerdict,
    PurchaseOrderStatus,
    PurchaseRequisitionStatus,
    SupplierInvoiceStatus,
    check_values,
)
from .mixins import TimestampMixin, UUIDPkMixin

_QTY_POSITIVE = "quantity > 0"
_PRICE_NON_NEGATIVE = "unit_price >= 0"


class PurchaseRequisition(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "purchase_requisitions"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "requisition_number", name="uq_purchase_requisitions_org_id_number"
        ),
        CheckConstraint(
            f"status IN ({check_values(*PurchaseRequisitionStatus)})",
            name="ck_purchase_requisitions_status",
        ),
        Index(
            "ix_purchase_requisitions_pending_approval",
            "org_id",
            postgresql_where=text("status = 'pending_approval'"),
        ),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    requisition_number: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), default=PurchaseRequisitionStatus.DRAFT, nullable=False
    )
    # The sales order that triggered this (Phase 2 shortage-routing) — nullable
    # because a requisition can also be a manual/planned reorder.
    triggered_by_sales_order_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sales_orders.id", ondelete="SET NULL")
    )
    notes: Mapped[str | None] = mapped_column(String(500))


class PurchaseRequisitionItem(UUIDPkMixin, Base):
    __tablename__ = "purchase_requisition_items"
    __table_args__ = (
        CheckConstraint(_QTY_POSITIVE, name="ck_purchase_requisition_items_quantity_positive"),
    )

    purchase_requisition_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("purchase_requisitions.id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[float] = mapped_column(Numeric(14, 3), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(200))


class PurchaseOrder(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        UniqueConstraint("org_id", "po_number", name="uq_purchase_orders_org_id_po_number"),
        CheckConstraint(
            f"status IN ({check_values(*PurchaseOrderStatus)})", name="ck_purchase_orders_status"
        ),
        # The purchase dashboard's "what's still open" query — everything
        # short of received/invoiced/paid/cancelled.
        Index(
            "ix_purchase_orders_open",
            "org_id",
            postgresql_where=text("status NOT IN ('received', 'invoiced', 'paid', 'cancelled')"),
        ),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False
    )
    purchase_requisition_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("purchase_requisitions.id", ondelete="SET NULL")
    )
    po_number: Mapped[str] = mapped_column(String(30), nullable=False)
    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), default=PurchaseOrderStatus.DRAFT, nullable=False
    )
    subtotal: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    tax_total: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    total: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    pdf_storage_uri: Mapped[str | None] = mapped_column(String(500))


class PurchaseOrderItem(UUIDPkMixin, Base):
    __tablename__ = "purchase_order_items"
    __table_args__ = (
        CheckConstraint(_QTY_POSITIVE, name="ck_purchase_order_items_quantity_positive"),
        CheckConstraint(
            _PRICE_NON_NEGATIVE, name="ck_purchase_order_items_unit_price_non_negative"
        ),
    )

    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[float] = mapped_column(Numeric(14, 3), nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    gst_rate: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    line_subtotal: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    line_tax: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    line_total: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)


class GoodsReceipt(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "goods_receipts"
    __table_args__ = (
        UniqueConstraint("org_id", "grn_number", name="uq_goods_receipts_org_id_grn_number"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="RESTRICT"), nullable=False
    )
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    grn_number: Mapped[str] = mapped_column(String(30), nullable=False)
    received_date: Mapped[date] = mapped_column(Date, nullable=False)


class GoodsReceiptItem(UUIDPkMixin, Base):
    __tablename__ = "goods_receipt_items"
    __table_args__ = (
        CheckConstraint("ordered_quantity > 0", name="ck_grn_items_ordered_quantity_positive"),
        CheckConstraint(
            "received_quantity >= 0 AND received_quantity <= ordered_quantity",
            name="ck_grn_items_received_lte_ordered",
        ),
        CheckConstraint(
            "accepted_quantity >= 0 AND accepted_quantity <= received_quantity",
            name="ck_grn_items_accepted_lte_received",
        ),
        CheckConstraint(
            "rejected_quantity >= 0 AND damaged_quantity >= 0",
            name="ck_grn_items_rejected_damaged_non_negative",
        ),
        CheckConstraint(
            "accepted_quantity + rejected_quantity + damaged_quantity <= received_quantity",
            name="ck_grn_items_disposition_sums_lte_received",
        ),
    )

    goods_receipt_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("goods_receipts.id", ondelete="CASCADE"), nullable=False
    )
    purchase_order_item_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("purchase_order_items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    ordered_quantity: Mapped[float] = mapped_column(Numeric(14, 3), nullable=False)
    received_quantity: Mapped[float] = mapped_column(Numeric(14, 3), nullable=False)
    accepted_quantity: Mapped[float] = mapped_column(Numeric(14, 3), nullable=False)
    rejected_quantity: Mapped[float] = mapped_column(Numeric(14, 3), default=0, nullable=False)
    damaged_quantity: Mapped[float] = mapped_column(Numeric(14, 3), default=0, nullable=False)


class SupplierInvoice(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "supplier_invoices"
    __table_args__ = (
        UniqueConstraint(
            "supplier_id", "invoice_number", name="uq_supplier_invoices_supplier_id_invoice_number"
        ),
        CheckConstraint(
            f"status IN ({check_values(*SupplierInvoiceStatus)})",
            name="ck_supplier_invoices_status",
        ),
        CheckConstraint(
            "amount_paid >= 0 AND amount_paid <= total",
            name="ck_supplier_invoices_amount_paid_range",
        ),
        # AP's review/payment queue — invoices not yet approved or paid.
        Index(
            "ix_supplier_invoices_actionable",
            "org_id",
            "due_date",
            postgresql_where=text("status NOT IN ('paid')"),
        ),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False
    )
    purchase_order_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="SET NULL")
    )
    invoice_number: Mapped[str] = mapped_column(String(50), nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), default=SupplierInvoiceStatus.RECEIVED, nullable=False
    )
    subtotal: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    tax_total: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    total: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    amount_paid: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )


class SupplierInvoiceItem(UUIDPkMixin, Base):
    __tablename__ = "supplier_invoice_items"
    __table_args__ = (
        CheckConstraint(_QTY_POSITIVE, name="ck_supplier_invoice_items_quantity_positive"),
        CheckConstraint(
            _PRICE_NON_NEGATIVE, name="ck_supplier_invoice_items_unit_price_non_negative"
        ),
    )

    supplier_invoice_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("supplier_invoices.id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT")
    )
    quantity: Mapped[float] = mapped_column(Numeric(14, 3), nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    gst_rate: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    line_total: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)


class ThreeWayMatchResult(UUIDPkMixin, Base):
    __tablename__ = "three_way_match_results"
    __table_args__ = (
        CheckConstraint(
            f"verdict IN ({check_values(*MatchVerdict)})", name="ck_three_way_match_results_verdict"
        ),
        CheckConstraint(
            "risk_score >= 0 AND risk_score <= 100", name="ck_three_way_match_results_risk_score"
        ),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="RESTRICT"), nullable=False
    )
    goods_receipt_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("goods_receipts.id", ondelete="RESTRICT"), nullable=False
    )
    supplier_invoice_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("supplier_invoices.id", ondelete="RESTRICT"),
        nullable=False,
    )
    qty_variance: Mapped[float] = mapped_column(Numeric(14, 3), default=0, nullable=False)
    price_variance: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    amount_variance: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    risk_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    verdict: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_codes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
