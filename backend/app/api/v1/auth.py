import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_current_user, require_role
from app.core.exceptions import Unauthorized
from app.core.ratelimit import RateLimiter
from app.core.security import (
    create_access_token,
    decode_token,  # noqa: F401  (re-exported for callers that need it)
    generate_refresh_secret,
    hash_secret,  # noqa: F401
    verify_secret,
)
from app.db.models.auth import RefreshToken
from app.db.models.org import Role, User, UserRole
from app.db.session import get_db
from app.schemas.auth import LoginRequest, RefreshRequest, TokenPair, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _user_roles(db: AsyncSession, user_id: uuid.UUID) -> list[str]:
    result = await db.execute(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    )
    return list(result.scalars().all())


async def _issue_tokens(db: AsyncSession, user: User) -> TokenPair:
    settings = get_settings()
    access_token = create_access_token(
        user_id=user.id,
        org_id=user.org_id,
        secret=settings.jwt_secret,
        expires_minutes=settings.access_token_expires_minutes,
    )
    secret = generate_refresh_secret()
    refresh_row = RefreshToken(
        user_id=user.id,
        secret_hash=hash_secret(secret),
        expires_at=_utcnow() + timedelta(days=settings.refresh_token_expires_days),
    )
    db.add(refresh_row)
    await db.flush()
    refresh_token = f"{refresh_row.id}.{secret}"
    await db.commit()
    return TokenPair(access_token=access_token, refresh_token=refresh_token)


@router.post(
    "/login",
    response_model=TokenPair,
    operation_id="login",
    dependencies=[Depends(RateLimiter(key_prefix="login", limit=10, window_seconds=60))],
)
async def login(
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> TokenPair:
    result = await db.execute(select(User).where(User.email == payload.email).limit(1))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active or user.hashed_password is None:
        raise Unauthorized("invalid email or password")
    if not verify_secret(payload.password, user.hashed_password):
        raise Unauthorized("invalid email or password")
    return await _issue_tokens(db, user)


@router.post("/refresh", response_model=TokenPair, operation_id="refreshToken")
async def refresh(
    payload: RefreshRequest,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> TokenPair:
    try:
        token_id_str, secret = payload.refresh_token.split(".", 1)
        token_id = uuid.UUID(token_id_str)
    except ValueError as exc:
        raise Unauthorized("malformed refresh token") from exc

    row = await db.get(RefreshToken, token_id)
    if row is None or row.revoked_at is not None or row.expires_at < _utcnow():
        raise Unauthorized("invalid or expired refresh token")
    if not verify_secret(secret, row.secret_hash):
        raise Unauthorized("invalid or expired refresh token")

    row.revoked_at = _utcnow()
    user = await db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise Unauthorized("invalid or expired refresh token")
    return await _issue_tokens(db, user)


@router.post("/logout", status_code=204, operation_id="logout")
async def logout(
    payload: RefreshRequest,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> None:
    try:
        token_id = uuid.UUID(payload.refresh_token.split(".", 1)[0])
    except ValueError:
        return None
    row = await db.get(RefreshToken, token_id)
    if row is not None and row.revoked_at is None:
        row.revoked_at = _utcnow()
        await db.commit()
    return None


@router.get("/me", response_model=UserOut, operation_id="getCurrentUser")
async def me(
    user: User = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> UserOut:
    roles = await _user_roles(db, user.id)
    return UserOut(
        id=user.id, org_id=user.org_id, email=user.email, full_name=user.full_name, roles=roles
    )


@router.get("/admin-ping", operation_id="adminPing")
async def admin_ping(
    user: User = Depends(require_role("Owner", "Manager")),  # noqa: B008
) -> dict[str, bool]:
    return {"ok": True}
