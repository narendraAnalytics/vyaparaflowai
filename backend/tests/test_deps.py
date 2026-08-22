import uuid

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.deps import get_api_key_org, get_current_user, require_perm, require_role
from app.core.exceptions import Forbidden, Unauthorized
from app.core.security import create_access_token
from app.db.models.org import Organization, Role, User, UserRole
from app.db.session import AsyncSessionLocal


@pytest.fixture
async def org_and_manager():
    async with AsyncSessionLocal() as session:
        org = Organization(name=f"test-deps-{uuid.uuid4()}")
        session.add(org)
        await session.flush()

        role = (
            await session.execute(select(Role).where(Role.name == "Manager"))
        ).scalar_one_or_none()
        if role is None:
            role = Role(name="Manager")
            session.add(role)
            await session.flush()

        user = User(org_id=org.id, email=f"{uuid.uuid4()}@test.local", full_name="Test Manager")
        session.add(user)
        await session.flush()
        session.add(UserRole(user_id=user.id, role_id=role.id))
        await session.commit()
        org_id, user_id = org.id, user.id

    yield org_id, user_id

    async with AsyncSessionLocal() as session:
        await session.execute(delete(UserRole).where(UserRole.user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.execute(delete(Organization).where(Organization.id == org_id))
        await session.commit()


@pytest.mark.asyncio
async def test_get_current_user_valid_token(org_and_manager):
    org_id, user_id = org_and_manager
    settings = get_settings()
    token = create_access_token(
        user_id=user_id,
        org_id=org_id,
        secret=settings.jwt_secret,
        expires_minutes=15,
    )
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    async with AsyncSessionLocal() as session:
        user = await get_current_user(credentials=creds, db=session)
    assert user.id == user_id


@pytest.mark.asyncio
async def test_get_current_user_missing_token_raises():
    async with AsyncSessionLocal() as session:
        with pytest.raises(Unauthorized):
            await get_current_user(credentials=None, db=session)


@pytest.mark.asyncio
async def test_get_current_user_bad_token_raises():
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="not-a-real-jwt")
    async with AsyncSessionLocal() as session:
        with pytest.raises(Unauthorized):
            await get_current_user(credentials=creds, db=session)


@pytest.mark.asyncio
async def test_require_role_allows_manager(org_and_manager):
    _org_id, user_id = org_and_manager
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        checker = require_role("Manager", "Owner")
        result = await checker(user=user, db=session)
    assert result.id == user_id


@pytest.mark.asyncio
async def test_require_role_rejects_wrong_role(org_and_manager):
    _org_id, user_id = org_and_manager
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        checker = require_role("Warehouse")
        with pytest.raises(Forbidden):
            await checker(user=user, db=session)


@pytest.mark.asyncio
async def test_require_perm_allows_manager_po_approve(org_and_manager):
    _org_id, user_id = org_and_manager
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        checker = require_perm("po.approve")
        result = await checker(user=user, db=session)
    assert result.id == user_id


@pytest.mark.asyncio
async def test_get_api_key_org_rejects_bad_key():
    async with AsyncSessionLocal() as session:
        with pytest.raises(Unauthorized):
            await get_api_key_org(api_key="wrong-key", db=session)
