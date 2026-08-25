import uuid
from datetime import date

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

from .enums import InvoiceStatus, SalesOrderStatus, check_values
from .mixins import TimestampMixin, UUIDPkMixin

_QTY_POSITIVE = "quantity > 0"
_PRICE_NON_NEGATIVE = "unit_price >= 0"


class SalesOrder(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "sales_orders"
    __table_args__ = (
        UniqueConstraint("org_id", "order_number", name="uq_sales_orders_org_id_order_number"),
        CheckConstraint(
            f"status IN ({check_values(*SalesOrderStatus)})", name="ck_sales_orders_status"
        ),
        # Sales dashboard's "what still needs fulfilling" query.
        Index(
            "ix_sales_orders_open",
            "org_id",
            "order_date",
            postgresql_where=text("status NOT IN ('invoiced', 'cancelled')"),
        ),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    order_number: Mapped[str] = mapped_column(String(30), nullable=False)
    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default=SalesOrderStatus.DRAFT, nullable=False)
    subtotal: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    tax_total: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    total: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(500))


class SalesOrderItem(UUIDPkMixin, Base):
    __tablename__ = "sales_order_items"
    __table_args__ = (
        CheckConstraint(_QTY_POSITIVE, name="ck_sales_order_items_quantity_positive"),
        CheckConstraint(_PRICE_NON_NEGATIVE, name="ck_sales_order_items_unit_price_non_negative"),
        CheckConstraint(
            "reserved_qty >= 0 AND reserved_qty <= quantity",
            name="ck_sales_order_items_reserved_qty_range",
        ),
    )

    sales_order_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sales_orders.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[float] = mapped_column(Numeric(14, 3), nullable=False)
    reserved_qty: Mapped[float] = mapped_column(Numeric(14, 3), default=0, nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    gst_rate: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    line_subtotal: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    line_tax: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    line_total: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)


class Delivery(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("org_id", "delivery_number", name="uq_deliveries_org_id_delivery_number"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    sales_order_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sales_orders.id", ondelete="RESTRICT"), nullable=False
    )
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    delivery_number: Mapped[str] = mapped_column(String(30), nullable=False)
    dispatched_at: Mapped[date | None] = mapped_column(Date)
    delivered_at: Mapped[date | None] = mapped_column(Date)


class DeliveryItem(UUIDPkMixin, Base):
    __tablename__ = "delivery_items"
    __table_args__ = (CheckConstraint(_QTY_POSITIVE, name="ck_delivery_items_quantity_positive"),)

    delivery_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("deliveries.id", ondelete="CASCADE"), nullable=False
    )
    sales_order_item_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sales_order_items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[float] = mapped_column(Numeric(14, 3), nullable=False)


class CustomerInvoice(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "customer_invoices"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "invoice_number", name="uq_customer_invoices_org_id_invoice_number"
        ),
        # One invoice per delivery (roadmap.txt 3.9, WF-06) — the DB-level
        # guard against double-billing the same shipment, same role the
        # NULL-excluded unique index below plays for the FK itself.
        UniqueConstraint("delivery_id", name="uq_customer_invoices_delivery_id"),
        CheckConstraint(
            f"status IN ({check_values(*InvoiceStatus)})", name="ck_customer_invoices_status"
        ),
        CheckConstraint(
            "amount_paid >= 0 AND amount_paid <= total",
            name="ck_customer_invoices_amount_paid_range",
        ),
        # AR aging report's hot query — every unpaid invoice, by due date.
        Index(
            "ix_customer_invoices_unpaid",
            "org_id",
            "due_date",
            postgresql_where=text("status NOT IN ('paid', 'void')"),
        ),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    sales_order_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sales_orders.id", ondelete="RESTRICT")
    )
    # Which delivery this invoice bills (roadmap.txt 3.9, WF-06) — nullable
    # because a counter sale (2.7) and any pre-3.9 invoice have no delivery
    # at all. Mirrors purchase_orders.pdf_storage_uri's pattern below.
    delivery_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("deliveries.id", ondelete="RESTRICT")
    )
    invoice_number: Mapped[str] = mapped_column(String(30), nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default=InvoiceStatus.DRAFT, nullable=False)
    subtotal: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    tax_total: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    total: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    amount_paid: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    # Set once generate_customer_invoice_pdf() renders + uploads it (3.9) -
    # same pattern as purchase_orders.pdf_storage_uri (3.7).
    pdf_storage_uri: Mapped[str | None] = mapped_column(String(500))
    # GST e-Invoice (Phase 5) — nullable until the IRP call is actually made.
    irn: Mapped[str | None] = mapped_column(String(64))
    irn_qr_code: Mapped[str | None] = mapped_column(String)
    eway_bill_number: Mapped[str | None] = mapped_column(String(20))


class CustomerInvoiceItem(UUIDPkMixin, Base):
    __tablename__ = "customer_invoice_items"
    __table_args__ = (
        CheckConstraint(_QTY_POSITIVE, name="ck_customer_invoice_items_quantity_positive"),
        CheckConstraint(
            _PRICE_NON_NEGATIVE, name="ck_customer_invoice_items_unit_price_non_negative"
        ),
    )

    customer_invoice_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("customer_invoices.id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    hsn_code: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(14, 3), nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    gst_rate: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    line_subtotal: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    line_tax: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    line_total: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
