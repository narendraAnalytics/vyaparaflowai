import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from app.db.models.auth import RefreshToken
from app.db.models.org import Organization, User
from app.db.session import AsyncSessionLocal


@pytest.fixture
async def throwaway_user():
    async with AsyncSessionLocal() as session:
        org = Organization(name=f"test-auth-{uuid.uuid4()}")
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, email=f"{uuid.uuid4()}@test.local", full_name="Test User")
        session.add(user)
        await session.commit()
        org_id, user_id = org.id, user.id

    yield user_id

    async with AsyncSessionLocal() as session:
        await session.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.execute(delete(Organization).where(Organization.id == org_id))
        await session.commit()


@pytest.mark.asyncio
async def test_refresh_token_create_and_read(throwaway_user):
    async with AsyncSessionLocal() as session:
        row = RefreshToken(
            user_id=throwaway_user,
            secret_hash="fake-hash",
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
        session.add(row)
        await session.commit()
        row_id = row.id

    async with AsyncSessionLocal() as session:
        fetched = await session.get(RefreshToken, row_id)
        assert fetched is not None
        assert fetched.user_id == throwaway_user
        assert fetched.revoked_at is None
