"""FastAPI dependency providers for auth, RBAC, and n8n's API-key auth.

Roles/permissions are re-queried from the DB on every call (never trusted
from JWT claims) so a role change or deactivation takes effect on the very
next request rather than waiting out the access token's 15-minute life.
"""

import secrets
import uuid

import jwt
from fastapi import Depends
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import Forbidden, NotFoundError, Unauthorized
from app.core.permissions import has_permission
from app.core.security import decode_token
from app.db.models.org import Organization, Role, User, UserRole
from app.db.session import get_db

bearer_scheme = HTTPBearer(auto_error=False)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> User:
    if credentials is None:
        raise Unauthorized("missing bearer token")
    settings = get_settings()
    try:
        payload = decode_token(credentials.credentials, settings.jwt_secret)
    except jwt.PyJWTError as exc:
        raise Unauthorized("invalid or expired token") from exc
    if payload.get("type") != "access":
        raise Unauthorized("not an access token")
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise Unauthorized("invalid token payload") from exc
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise Unauthorized("user not found or inactive")
    return user


async def _role_names(db: AsyncSession, user_id: uuid.UUID) -> set[str]:
    result = await db.execute(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    )
    return set(result.scalars().all())


def require_role(*role_names: str):
    async def _check(
        user: User = Depends(get_current_user),  # noqa: B008
        db: AsyncSession = Depends(get_db),  # noqa: B008
    ) -> User:
        granted = await _role_names(db, user.id)
        if not granted & set(role_names):
            raise Forbidden(f"requires one of roles: {', '.join(role_names)}")
        return user

    return _check


def require_perm(*perms: str):
    async def _check(
        user: User = Depends(get_current_user),  # noqa: B008
        db: AsyncSession = Depends(get_db),  # noqa: B008
    ) -> User:
        granted_roles = await _role_names(db, user.id)
        if not has_permission(granted_roles, set(perms)):
            raise Forbidden(f"requires one of permissions: {', '.join(perms)}")
        return user

    return _check


async def get_api_key_org(
    api_key: str | None = Depends(api_key_header),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> uuid.UUID:
    settings = get_settings()
    if (
        not api_key
        or not settings.n8n_api_key
        or not secrets.compare_digest(api_key, settings.n8n_api_key)
    ):
        raise Unauthorized("invalid api key")
    if settings.n8n_org_id:
        return uuid.UUID(settings.n8n_org_id)
    org_id = (await db.execute(select(Organization.id))).scalars().first()
    if org_id is None:
        raise NotFoundError("no organization configured")
    return org_id
