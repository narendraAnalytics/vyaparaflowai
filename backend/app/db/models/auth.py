import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

from .mixins import TimestampMixin, UUIDPkMixin


class RefreshToken(UUIDPkMixin, TimestampMixin, Base):
    """One row per issued refresh token. The token handed to the client is
    "{id}.{secret}" — id is this row's primary key (O(1) lookup), secret is
    a random value whose argon2 hash is stored here (never the raw secret).
    Refresh rotates: using a token revokes it and issues a fresh pair, so a
    stolen-and-reused-later token is detectable (its row is already
    revoked).
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    secret_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
