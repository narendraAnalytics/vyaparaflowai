import asyncio
import sys
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

if sys.platform == "win32":
    # psycopg3's async mode requires SelectorEventLoop; Windows defaults to
    # ProactorEventLoop. Centralized here (rather than duplicated per
    # entrypoint) because every asyncio.run()-based entrypoint that touches
    # the DB imports this module — pytest, alembic/env.py, this seed script.
    # Does NOT fix uvicorn serving (uvicorn>=0.36 bypasses this policy with
    # its own loop factory) — that's app/core/winloop.py + app/dev.py.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

settings = get_settings()

engine = create_async_engine(settings.database_url, pool_pre_ping=True, echo=False)

AsyncSessionLocal = async_sessionmaker(
    bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
