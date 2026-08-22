import uuid

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

from .mixins import UUIDPkMixin


class DocumentSequence(UUIDPkMixin, Base):
    """One row per (org, doc_type, financial_year) counter. Allocating the
    next number is a SELECT ... FOR UPDATE + UPDATE in a single transaction
    (see services/numbering.py, Phase 2) — the row lock serializes concurrent
    allocators, which is what makes "PO-2026-00452" gapless rather than just
    unique. A plain Postgres SEQUENCE cannot do this: a rolled-back
    transaction still burns a sequence value, leaving a gap.
    """

    __tablename__ = "document_sequences"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "doc_type", "financial_year", name="uq_document_sequences_org_doc_type_fy"
        ),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    doc_type: Mapped[str] = mapped_column(String(20), nullable=False)
    financial_year: Mapped[str] = mapped_column(String(9), nullable=False)  # e.g. "2026-2027"
    last_value: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
