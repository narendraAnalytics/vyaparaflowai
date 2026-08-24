import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

from .enums import ApprovalStatus, NotificationChannel, NotificationStatus, check_values
from .mixins import TimestampMixin, UUIDPkMixin


class Approval(UUIDPkMixin, TimestampMixin, Base):
    """Polymorphic — entity_type/entity_id point at whatever needs approval
    (a purchase order, a payment run, ...) without a dedicated FK per type.
    """

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(f"status IN ({check_values(*ApprovalStatus)})", name="ck_approvals_status"),
        # The approvals inbox always filters WHERE status = 'pending' — a
        # partial index keeps that query cheap regardless of how many
        # historical (approved/rejected) rows pile up.
        Index(
            "ix_approvals_pending",
            "org_id",
            "sla_due_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(20), default=ApprovalStatus.PENDING, nullable=False)
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column(String(500))


class AuditLog(UUIDPkMixin, Base):
    """Immutable. No updated_at — an audit log entry is never edited."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_entity", "entity_type", "entity_id"),)

    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    before_json: Mapped[dict | None] = mapped_column(JSONB)
    after_json: Mapped[dict | None] = mapped_column(JSONB)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkflowEvent(UUIDPkMixin, Base):
    """One row per n8n workflow execution touching this org's data —
    correlates n8n's own execution id with what happened on our side.
    """

    __tablename__ = "workflow_events"

    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    n8n_execution_id: Mapped[str | None] = mapped_column(String(50))
    workflow_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IdempotencyKey(Base):
    """Backs the Idempotency-Key middleware (Phase 2) — a repeated POST with
    the same key returns the stored response instead of re-executing.
    """

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict | None] = mapped_column(JSONB)
    status_code: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OutboxEvent(UUIDPkMixin, Base):
    """Transactional outbox: a service writes a row here in the same
    transaction as its domain change, and a separate worker (Phase 2)
    publishes it to n8n with retry — decouples "the DB write committed"
    from "n8n was successfully notified".
    """

    __tablename__ = "outbox_events"
    __table_args__ = (
        # The publisher worker's only query: "give me everything not yet
        # published". A partial index means that query stays fast forever,
        # not just while the table is small.
        Index(
            "ix_outbox_events_unpublished",
            "created_at",
            postgresql_where=text("published_at IS NULL"),
        ),
    )

    aggregate_type: Mapped[str] = mapped_column(String(50), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Phase 2.12: when the most recent publish attempt happened — the
    # exponential-backoff clock runs from this, not created_at, so a
    # retry schedule reflects actual attempt history rather than just
    # how old the event is. Nullable: never-attempted rows are always
    # immediately eligible (see workers/outbox_publisher.py).
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Document(UUIDPkMixin, TimestampMixin, Base):
    """Metadata for a file in object storage (MinIO/GCS) — invoice PDFs, PO
    PDFs, GRN photos. The actual bytes never touch Postgres.
    """

    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("checksum", name="uq_documents_checksum"),)

    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    doc_type: Mapped[str] = mapped_column(String(50), nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int | None] = mapped_column(Integer)


class Notification(UUIDPkMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(
            f"channel IN ({check_values(*NotificationChannel)})", name="ck_notifications_channel"
        ),
        CheckConstraint(
            f"status IN ({check_values(*NotificationStatus)})", name="ck_notifications_status"
        ),
        Index(
            "ix_notifications_pending",
            "org_id",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)
    template: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default=NotificationStatus.PENDING, nullable=False
    )
    retries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
