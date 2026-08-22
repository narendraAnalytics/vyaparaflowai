import uuid

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

from .mixins import TimestampMixin, UUIDPkMixin

# GSTIN format: 2-digit state code + 10-char PAN + 1-digit entity number +
# default 'Z' + 1 checksum char. Format-only (not full checksum validation —
# that's an app-layer concern for Phase 4). Nullable: unregistered ("URP")
# counterparties are valid per the Aug-2026 e-Way Bill spec (see ADR 0001).
_GSTIN_CHECK = "gstin IS NULL OR gstin ~ '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$'"


class Customer(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (
        CheckConstraint(_GSTIN_CHECK, name="ck_customers_gstin_format"),
        CheckConstraint("credit_limit >= 0", name="ck_customers_credit_limit_non_negative"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    gstin: Mapped[str | None] = mapped_column(String(15))
    state_code: Mapped[str | None] = mapped_column(String(2))
    phone: Mapped[str | None] = mapped_column(String(20))
    email: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(String(500))
    credit_limit: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Supplier(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "suppliers"
    __table_args__ = (
        CheckConstraint(_GSTIN_CHECK, name="ck_suppliers_gstin_format"),
        CheckConstraint("lead_time_days >= 0", name="ck_suppliers_lead_time_non_negative"),
        CheckConstraint(
            "reliability_score >= 0 AND reliability_score <= 100",
            name="ck_suppliers_reliability_score_range",
        ),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    gstin: Mapped[str | None] = mapped_column(String(15))
    state_code: Mapped[str | None] = mapped_column(String(2))
    phone: Mapped[str | None] = mapped_column(String(20))
    email: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(String(500))
    lead_time_days: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    reliability_score: Mapped[float] = mapped_column(Numeric(5, 2), default=100, nullable=False)
    bank_account_number: Mapped[str | None] = mapped_column(String(30))
    bank_ifsc: Mapped[str | None] = mapped_column(String(11))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
